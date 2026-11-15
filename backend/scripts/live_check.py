"""Live check of the market lifecycle on devnet (CHAIN_MODE=real). Dev tool, not part of the app.

Needs the chain service and the backend running (see chain-service/README.md and README.md "Live check"), with short
timings. It drives three markets at once: a settled one, a one sided one (refund) and a cancelled game (refund).
Secrets come from the app's own settings loader and are never printed. Deposits come from chain-service's dev_deposit script.

  .venv/bin/python scripts/live_check.py --chain-log /path/to/chain.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db import make_pool  # noqa: E402
from app.repository import Repository  # noqa: E402
from markets.links import LinkSigner  # noqa: E402
from markets.pool_math import compute_payout, settle_amounts  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHAIN_DIR = ROOT / "chain-service"
USDC = 1_000_000
RESULTS: list[tuple[str, bool, str]] = []
RPC = "https://api.devnet.solana.com"
BACKEND = "http://127.0.0.1:8788"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  (' + detail + ')') if detail else ''}", flush=True)


def explorer(sig: str) -> str:
    return f"https://explorer.solana.com/tx/{sig}?cluster=devnet"


def env_live() -> dict[str, str]:
    out = dict(os.environ)
    for line in (CHAIN_DIR / ".env.live").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


class Live:
    def __init__(self, settings, repo: Repository, http: httpx.AsyncClient, chain: httpx.AsyncClient, env: dict[str, str]):
        self.s, self.repo, self.http, self.chain, self.env = settings, repo, http, chain, env
        self.admin = {"X-Admin-Secret": settings.admin_secret}
        self.signer = LinkSigner(settings.link_signing_secret, settings.deposit_page_url, settings.link_grace)
        self.mint = env["TEST_USDC_MINT"]
        self.deposit_sigs: dict[str, list[str]] = {}
        self.minutes = 6  # kickoff of the simulated game, minutes from now
        self.dm_lock = asyncio.Lock()  # one bet draft per sender: the DM conversations must not overlap

    async def wait(self, what: str, fn, timeout: float = 300, every: float = 4):
        end = asyncio.get_event_loop().time() + timeout
        while True:
            value = await fn()
            if value:
                return value
            if asyncio.get_event_loop().time() > end:
                raise TimeoutError(f"timed out waiting for {what}")
            await asyncio.sleep(every)

    async def chain_get(self, path: str) -> tuple[int, dict]:
        r = await self.chain.get(path)
        return r.status_code, r.json()

    async def token_balance(self, wallet: str) -> int:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [wallet, {"mint": self.mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}]}
        for attempt in range(5):
            r = await self.http.post(RPC, json=body)
            if r.status_code == 429:
                await asyncio.sleep(2 * 2 ** attempt)
                continue
            accts = r.json()["result"]["value"]
            return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accts)
        raise RuntimeError("rate limited reading token balances")

    async def payer_sol(self) -> float:
        return (await self.chain.get("/health")).json()["payer_sol"]

    async def dev_deposit(self, bet_id: str, specs: list[str]) -> list[dict]:
        proc = await asyncio.create_subprocess_exec(
            "npx", "tsx", "scripts/dev_deposit.ts", bet_id, *specs, cwd=CHAIN_DIR, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("dev_deposit failed: " + err.decode()[-300:])
        rows = [json.loads(l) for l in out.decode().splitlines() if l.startswith("{")]
        self.deposit_sigs[bet_id] = [r["signature"] for r in rows]
        return rows

    async def open_market(self, label: str, handle: str, away: str, home: str, away_name: str, home_name: str):
        r = await self.http.post(f"{BACKEND}/admin/games", headers=self.admin,
                                 json={"home_id": home, "away_id": away, "minutes_from_now": self.minutes})
        game = r.json()["game"]
        gid = game["game_id"]

        async def say(text: str) -> None:
            r = await self.http.post(f"{BACKEND}/inbound", headers={"X-Gateway-Secret": self.s.gateway_shared_secret},
                                     json={"messageId": f"live-{uuid.uuid4()}", "handle": handle, "text": text,
                                           "receivedAt": datetime.now(timezone.utc).isoformat()})
            assert r.status_code == 200, r.text

        async def find():
            for b in await self.repo.list_bets(handle):
                if b.game_id == gid and b.status in ("open", "locked", "refunded", "settled"):
                    return b
            return None

        async with self.dm_lock:
            await say(f"create a bet for {away_name} vs {home_name}")
            await asyncio.sleep(9)
            await say("yes")
            bet = await self.wait(f"{label} market to open", find, timeout=90, every=3)
        return bet, gid


    # ---------- shared verification ----------

    async def views(self, bet, label: str, expect: dict[str, tuple[int, int | None, bool]]) -> None:
        """expect: wallet -> (side, payout_base, paid). Read from the link views, after the chain positions are gone."""
        token = self.signer.token(bet.id)
        code, positions = await self.chain_get(f"/bets/{bet.id}/positions")
        check(f"{label}: no position accounts are left on chain", code == 200 and positions["positions"] == [])
        r = await self.http.get(f"{BACKEND}/markets/{token}")
        view = r.json()
        check(f"{label}: market view still works and lists every participant", r.status_code == 200 and view["participant_count"] == len(expect), f"status {view.get('status')}")
        r = await self.http.get(f"{BACKEND}/markets/{token}/participants")
        listed = {p["wallet"]: (p["side"], p["stake_base"]) for p in r.json()["participants"]}
        check(f"{label}: participants come from snapshots", set(listed) == set(expect), f"{len(listed)} wallets")
        for wallet, (side, payout, paid) in expect.items():
            r = await self.http.get(f"{BACKEND}/markets/{token}/position", params={"wallet": wallet})
            pos = r.json()
            check(f"{label}: position {wallet[:6]} side {side} payout {payout}",
                  pos.get("has_position") and pos["side"] == side and pos["payout_base"] == payout and pos["paid"] is paid,
                  f"payout {pos.get('payout_base')} paid {pos.get('paid')}")

    async def notices(self, bet, label: str, kinds: set[str]) -> None:
        async def all_sent():
            got = {n["kind"]: n["status"] for n in await self.repo.list_notices(bet.id)}
            return got if kinds <= {k for k, st in got.items() if st == "sent"} else None

        try:
            got = await self.wait(f"{label} texts", all_sent, timeout=90, every=4)
            check(f"{label}: texts sent to the proposer", True, ", ".join(sorted(got)))
        except TimeoutError:
            got = {n["kind"]: n["status"] for n in await self.repo.list_notices(bet.id)}
            check(f"{label}: texts sent to the proposer", False, str(got))

    async def closed_on_chain(self, bet, label: str) -> None:
        async def closed():
            b = await self.repo.get_bet(bet.id)
            return b if b.chain_closed_at else None

        try:
            await self.wait(f"{label} close_bet", closed, timeout=240, every=5)
            code, body = await self.chain_get(f"/bets/{bet.id}")
            check(f"{label}: close_bet ran after the link expired, the Bet account is gone", code == 404 and body.get("code") == "NOT_FOUND")
        except TimeoutError:
            b = await self.repo.get_bet(bet.id)
            check(f"{label}: close_bet ran after the link expired", False, f"attempts {b.close_bet_attempts} err {b.close_bet_last_error}")

    async def tasks_done(self, bet, label: str, claim_kind: str, n_claims: int, n_closes: int) -> None:
        tasks = await self.repo.payout_tasks(bet.id)
        claims = [t for t in tasks if t.kind == claim_kind]
        closes = [t for t in tasks if t.kind == "close_loser"]
        check(f"{label}: {n_claims} claim tasks and {n_closes} loser close tasks, all done",
              len(claims) == n_claims and len(closes) == n_closes and all(t.status == "done" for t in tasks), str([(t.kind, t.status) for t in tasks]))

    # ---------- scenarios ----------

    async def run_settled(self, handle: str) -> dict:
        L = "A settled"
        print(f"\n== {L} market ==", flush=True)
        bet, gid = await self.open_market(L, handle, "nfl-den", "nfl-buf", "broncos", "bills")
        code, c0 = await self.chain_get(f"/bets/{bet.id}")
        check(f"{L}: market exists on devnet and passes the trust check", code == 200 and c0["state"] == "open")
        treasury0 = c0["treasury_balance_base"]
        rows = await self.dev_deposit(bet.id, ["0:5", "0:7", "1:6", "1:9.25"])
        by = {r["wallet"]: r for r in rows}
        check(f"{L}: 4 zero SOL wallets deposited on both sides", len(rows) == 4)
        b = await self.wait(f"{L} lock", lambda: self._status(bet.id, "locked", "refunded", "settled"), timeout=300)
        check(f"{L}: the worker closed funding and locked", b.status == "locked", b.status)
        code, c1 = await self.chain_get(f"/bets/{bet.id}")
        check(f"{L}: chain is locked with the deposited pools", c1["state"] == "locked" and c1["side_totals"] == [12 * USDC, 15_250_000])
        r = await self.http.post(f"{BACKEND}/admin/games/{gid}/finish", headers=self.admin, json={"home_score": 21, "away_score": 14})  # home wins: side 1
        check(f"{L}: result set on the simulated game", r.status_code == 200)
        b = await self.wait(f"{L} payouts", lambda: self._payouts_done(bet.id), timeout=420)
        check(f"{L}: the worker settled and finished the payouts", b.status == "settled" and b.winning_side == 1, b.status)
        s = settle_amounts((12 * USDC, 15_250_000), 1, 500)
        code, c2 = await self.chain_get(f"/bets/{bet.id}")
        check(f"{L}: fee is 5% of the losing side and sits in the treasury", c2["fee_paid_base"] == s.fee == 600_000 and c2["treasury_balance_base"] - treasury0 == s.fee,
              f"fee {c2['fee_paid_base']}")
        tasks = {t.wallet: t for t in await self.repo.payout_tasks(bet.id) if t.kind == "win"}
        paid = 0
        expect: dict[str, tuple[int, int | None, bool]] = {}
        for wallet, r in by.items():
            if r["side"] == 1:
                want = compute_payout(r["amount_base"], s.distributable, s.winning_total)
                bal = await self.token_balance(wallet)
                paid += want
                check(f"{L}: winner {wallet[:6]} was paid the pool math amount", tasks[wallet].amount_base == want and bal == want, f"{want} (balance {bal})")
                expect[wallet] = (1, want, True)
            else:
                bal = await self.token_balance(wallet)
                check(f"{L}: loser {wallet[:6]} keeps no tokens", bal == 0)
                expect[wallet] = (0, 0, False)
        dust = c2["vault_base"]
        check(f"{L}: vault holds only rounding dust", dust == s.distributable - paid and dust < 2, f"{dust} base units")
        check(f"{L}: every position closed on chain", c2["positions_open"] == 0)
        await self.tasks_done(bet, L, "win", 2, 2)
        await self.views(bet, L, expect)
        await self.notices(bet, L, {"link", "locked", "result"})
        await self.closed_on_chain(bet, L)
        return {"bet": bet.id}

    async def run_one_sided(self, handle: str) -> dict:
        L = "B one sided"
        print(f"\n== {L} market (refund) ==", flush=True)
        bet, gid = await self.open_market(L, handle, "nfl-nyg", "nfl-bal", "giants", "ravens")
        code, c0 = await self.chain_get(f"/bets/{bet.id}")
        treasury0 = c0["treasury_balance_base"]
        rows = await self.dev_deposit(bet.id, ["0:5", "0:3"])
        b = await self.wait(f"{L} refund", lambda: self._status(bet.id, "refunded", "settled"), timeout=320)
        check(f"{L}: the worker refunded at funding close", b.status == "refunded" and b.refund_reason == "one_sided", f"{b.status} {b.refund_reason}")
        await self.wait(f"{L} payouts", lambda: self._payouts_done(bet.id), timeout=240)
        code, c1 = await self.chain_get(f"/bets/{bet.id}")
        check(f"{L}: no fee, vault empty, treasury unchanged", c1["state"] == "refunded" and c1["fee_paid_base"] == 0 and c1["vault_base"] == 0 and c1["treasury_balance_base"] == treasury0)
        expect = {}
        for r in rows:
            bal = await self.token_balance(r["wallet"])
            check(f"{L}: {r['wallet'][:6]} got the full stake back", bal == r["amount_base"], str(bal))
            expect[r["wallet"]] = (0, r["amount_base"], True)
        await self.tasks_done(bet, L, "refund", 2, 0)
        await self.views(bet, L, expect)
        await self.notices(bet, L, {"link", "refunded"})
        await self.closed_on_chain(bet, L)
        return {"bet": bet.id}

    async def run_cancelled(self, handle: str) -> dict:
        L = "C cancelled"
        print(f"\n== {L} game (refund) ==", flush=True)
        bet, gid = await self.open_market(L, handle, "nfl-pit", "nfl-kc", "steelers", "chiefs")
        code, c0 = await self.chain_get(f"/bets/{bet.id}")
        treasury0 = c0["treasury_balance_base"]
        rows = await self.dev_deposit(bet.id, ["0:4", "1:6"])
        r = await self.http.post(f"{BACKEND}/admin/games/{gid}/status", headers=self.admin, json={"status": "cancelled"})
        check(f"{L}: game cancelled", r.status_code == 200)
        b = await self.wait(f"{L} refund", lambda: self._status(bet.id, "refunded"), timeout=200)
        check(f"{L}: the worker refunded", b.status == "refunded" and b.refund_reason == "cancelled", f"{b.status} {b.refund_reason}")
        await self.wait(f"{L} payouts", lambda: self._payouts_done(bet.id), timeout=240)
        code, c1 = await self.chain_get(f"/bets/{bet.id}")
        check(f"{L}: no fee, vault empty, treasury unchanged", c1["state"] == "refunded" and c1["fee_paid_base"] == 0 and c1["vault_base"] == 0 and c1["treasury_balance_base"] == treasury0)
        expect = {}
        for r in rows:
            bal = await self.token_balance(r["wallet"])
            check(f"{L}: {r['wallet'][:6]} got the full stake back", bal == r["amount_base"], str(bal))
            expect[r["wallet"]] = (r["side"], r["amount_base"], True)
        await self.tasks_done(bet, L, "refund", 2, 0)
        await self.views(bet, L, expect)
        await self.notices(bet, L, {"link", "refund_result"})
        await self.closed_on_chain(bet, L)
        return {"bet": bet.id}

    async def _status(self, bet_id: str, *statuses: str):
        b = await self.repo.get_bet(bet_id)
        return b if b.status in statuses else None

    async def _payouts_done(self, bet_id: str):
        b = await self.repo.get_bet(bet_id)
        if b.payouts_completed_at is None:
            return None
        tasks = await self.repo.payout_tasks(bet_id)
        return b if tasks and all(t.status == "done" for t in tasks) else None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain-log", required=True)
    ap.add_argument("--only", choices=["settled", "one_sided", "cancelled"], action="append", help="run only these scenarios")
    args = ap.parse_args()
    settings = load_settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    repo = Repository(pool)
    env = env_live()
    async with httpx.AsyncClient(timeout=30) as http, httpx.AsyncClient(base_url="http://127.0.0.1:8789", headers={"X-Chain-Secret": env["CHAIN_SERVICE_SECRET"]}, timeout=30) as chain:
        live = Live(settings, repo, http, chain, env)
        entry = await repo.get_roster_by_name("nicolas")
        assert entry is not None, "roster entry 'nicolas' not found"
        handle = entry.handle
        sol0 = await live.payer_sol()
        print(f"payer SOL before: {sol0:.6f}", flush=True)
        scenarios = {"settled": live.run_settled, "one_sided": live.run_one_sided, "cancelled": live.run_cancelled}
        chosen = args.only or list(scenarios)
        outs = await asyncio.gather(*(scenarios[k](handle) for k in chosen), return_exceptions=True)
        for o in outs:
            if isinstance(o, BaseException):
                check("scenario finished without an exception", False, f"{type(o).__name__}: {o}")
        sol1 = await live.payer_sol()
        print(f"\npayer SOL after: {sol1:.6f}  spent: {sol0 - sol1:.6f}")
        bets = [o["bet"] for o in outs if isinstance(o, dict)]
        print("\n== explorer links (from the chain service log) ==")
        for line in Path(args.chain_log).read_text().splitlines():
            if '"event":"tx.sent"' in line:
                e = json.loads(line)
                if e.get("betId") in bets:
                    print(f"  {e['betId'][:8]} {e['instruction']:<22} {explorer(e['signature'])}")
        for bid, sigs in live.deposit_sigs.items():
            for s in sigs:
                print(f"  {bid[:8]} deposit                {explorer(s)}")
    await pool.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
