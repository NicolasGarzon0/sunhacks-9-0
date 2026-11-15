"""Live check of sponsored deposits and the airdrop on devnet (CHAIN_MODE=real). Dev tool, not part of the app.

The reference client (chain-service/scripts/deposit_client.ts) plays the deposit page with local zero SOL keypairs.
Needs chain-service and the backend running with AIRDROP_ENABLED=true, MAX_PARTICIPANTS=6 and short timings
(see README "Live check"). Secrets come from the app's settings loader and are never printed.

  .venv/bin/python scripts/live_check_3b.py --chain-log /path/to/chain.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.db import make_pool  # noqa: E402
from app.repository import Repository  # noqa: E402
from live_check import BACKEND, CHAIN_DIR, RESULTS, RPC, USDC, Live, check, env_live, explorer  # noqa: E402
from markets.pool_math import compute_payout, settle_amounts  # noqa: E402

RUN = uuid.uuid4().hex[:6]


class Live3B(Live):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.minutes = 8
        self.npx = shutil.which("npx") or "npx"
        self.signatures: list[tuple[str, str]] = []  # (label, signature)
        self.deposit_sigs_by_wallet: list[tuple[str, str]] = []  # (wallet, signature) of every client deposit
        self.atas = 0

    async def client(self, cmd: str, link: str, name: str, *extra: str) -> dict:
        proc = await asyncio.create_subprocess_exec(
            self.npx, "tsx", "scripts/deposit_client.ts", cmd, "--link", link, "--wallet-file", f"live3b-{RUN}-{name}", *extra,
            cwd=CHAIN_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        lines = [l for l in out.decode().splitlines() if l.startswith("{")]
        if not lines:
            raise RuntimeError(f"deposit_client {cmd} printed no result: {err.decode()[-300:]}")
        return json.loads(lines[-1])

    async def airdrop(self, link: str, name: str) -> dict:
        r = await self.client("airdrop", link, name)
        if r["ok"] and r["body"].get("minted_base"):
            self.atas += 1
            self.signatures.append((f"airdrop {name}", r["body"]["signature"]))
        return r

    async def deposit(self, link: str, name: str, side: int, usd: str, label: str, *extra: str) -> dict:
        r = await self.client("deposit", link, name, "--side", str(side), "--usd", usd, *extra)
        if r["ok"]:
            self.signatures.append((f"{label} {name} deposit", r["body"]["signature"]))
            self.deposit_sigs_by_wallet.append((r["wallet"], r["body"]["signature"]))
        return r

    async def rpc(self, method: str, params: list):
        for attempt in range(6):
            r = await self.http.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code == 429:
                await asyncio.sleep(2 * 2 ** attempt)
                continue
            return r.json().get("result")
        raise RuntimeError("rate limited")

    async def run(self, handle: str) -> None:
        sol0 = await self.payer_sol()  # before the markets exist: their creation rent is returned by close_bet
        print(f"payer SOL before: {sol0:.6f}", flush=True)
        print("\n== opening two markets by DM ==", flush=True)
        a, gid_a = await self.open_market("A", handle, "nfl-den", "nfl-buf", "broncos", "bills")
        b, gid_b = await self.open_market("B", handle, "nfl-nyg", "nfl-bal", "giants", "ravens")
        link_a = self.signer.url(a.id)
        admin_link = (await self.http.get(f"{BACKEND}/admin/bets/{a.id}", headers=self.admin)).json()["link"]
        check("A: the link derived with the app's signer equals the link in the text", admin_link == link_a)
        check("A: max participants is 6 for this run", a.max_participants == 6)
        code, c0 = await self.chain_get(f"/bets/{a.id}")
        treasury0 = c0["treasury_balance_base"]

        async def phase_a():
            print("\n== A: airdrops ==", flush=True)
            for n in ("w1", "w2", "w3", "w4", "w5", "w7"):
                r = await self.airdrop(link_a, n)
                check(f"A: airdrop {n} gives 25 test USDC", r["ok"] and r["body"]["usdc_base"] == 25 * USDC, f"status {r['status']}")
            again = await self.airdrop(link_a, "w1")
            check("A: a repeat airdrop mints nothing and returns the balance", again["ok"] and again["body"]["minted_base"] == 0 and again["body"]["usdc_base"] == 25 * USDC)
            info = await self.client("wallet", link_a, "w8")
            check("A: a wallet that never got the airdrop has no token account", info["ok"] and info["body"]["has_token_account"] is False)

            print("\n== A: rejections ==", flush=True)
            r = await self.deposit(link_a, "w1", 0, "5", "A", "--tamper", "amount")
            check("A: tampered amount is rejected (TX_REJECTED)", r["status"] == 400 and r["body"].get("code") == "TX_REJECTED", f"{r['status']} {r['body'].get('code')}")
            r = await self.deposit(link_a, "w4", 1, "25", "A")
            check("A: above max is rejected (ABOVE_MAX)", r["status"] == 409 and r["body"].get("code") == "ABOVE_MAX", f"{r['body'].get('code')}")
            r = await self.deposit(link_a, "w8", 0, "5", "A")
            check("A: a wallet with no token account is rejected (NO_TOKEN_ACCOUNT)", r["status"] == 409 and r["body"].get("code") == "NO_TOKEN_ACCOUNT", f"{r['body'].get('code')}")

            print("\n== A: deposits ==", flush=True)
            r = await self.deposit(link_a, "w1", 0, "5", "A", "--shape", "message", "--name", "Ana")
            check("A: w1 deposits 5 on side 0 (message + signature shape)", r["ok"], f"{r['status']} {r['body'].get('code')}")
            r = await self.deposit(link_a, "w2", 0, "4", "A", "--shape", "signed")
            check("A: w2 deposits 4 on side 0 (signed transaction shape)", r["ok"], f"{r['status']} {r['body'].get('code')}")
            r = await self.deposit(link_a, "w2", 1, "4", "A")
            check("A: the other side is rejected for w2 (ALREADY_OTHER_SIDE)", r["status"] == 409 and r["body"].get("code") == "ALREADY_OTHER_SIDE", f"{r['body'].get('code')}")
            r = await self.deposit(link_a, "w1", 0, "2", "A", "--shape", "signed")
            check("A: w1 tops up 2 on the same side", r["ok"] and r["body"]["market"]["participant_count"] == 2, f"{r['status']} {r['body'].get('code')}")
            for n, usd, shape in (("w3", "6", "message"), ("w4", "7", "message"), ("w5", "3", "signed")):
                r = await self.deposit(link_a, n, 1, usd, "A", "--shape", shape)
                check(f"A: {n} deposits {usd} on side 1 ({shape} shape)", r["ok"], f"{r['status']} {r['body'].get('code')}")
            rows = await self.dev_deposit(a.id, ["0:2"])  # the 6th participant, out of band: fills the market
            check("A: a 6th participant fills the market (dev script)", len(rows) == 1)
            self.deposit_sigs_by_wallet.append((rows[0]["wallet"], rows[0]["signature"]))
            r = await self.client("deposit", link_a, "w7", "--side", "1", "--usd", "5")
            check("A: the market is full (MARKET_FULL)", r["status"] == 409 and r["body"].get("code") == "MARKET_FULL", f"{r['body'].get('code')}")

        async def cutoff_b():
            wait_until = b.funding_closes_at - __import__("datetime").timedelta(seconds=22)
            await self.airdrop(self.signer.url(b.id), "w9")
            while datetime.now(timezone.utc) < wait_until:
                await asyncio.sleep(2)
            r = await self.client("deposit", self.signer.url(b.id), "w9", "--side", "0", "--usd", "5")
            check("B: a deposit inside the 30 second funding cutoff is rejected (FUNDING_CLOSED)", r["status"] == 409 and r["body"].get("code") == "FUNDING_CLOSED", f"{r['body'].get('code')}")

        await asyncio.gather(phase_a(), cutoff_b())

        print("\n== A: lock, result, settle, payouts ==", flush=True)
        locked = await self.wait("A lock", lambda: self._status(a.id, "locked", "settled", "refunded"), timeout=240)
        check("A: the worker locked the market", locked.status == "locked", locked.status)
        code, c1 = await self.chain_get(f"/bets/{a.id}")
        side0, side1 = 13 * USDC, 16 * USDC
        check("A: chain pools match what was deposited", c1["side_totals"] == [side0, side1] and c1["participant_count"] == 6, str(c1["side_totals"]))
        r = await self.http.post(f"{BACKEND}/admin/games/{gid_a}/finish", headers=self.admin, json={"home_score": 21, "away_score": 14})
        check("A: result set on the simulated game (home wins, side 1)", r.status_code == 200)
        done = await self.wait("A payouts", lambda: self._payouts_done(a.id), timeout=600)
        check("A: the worker settled and finished the payouts", done.status == "settled" and done.winning_side == 1, done.status)
        s = settle_amounts((side0, side1), 1, 500)
        code, c2 = await self.chain_get(f"/bets/{a.id}")
        check("A: fee is 5% of the losing side and sits in the treasury", c2["fee_paid_base"] == s.fee == 650_000 and c2["treasury_balance_base"] - treasury0 == s.fee, f"{c2['fee_paid_base']}")
        tasks = [t for t in await self.repo.payout_tasks(a.id)]
        wins = {t.wallet: t for t in tasks if t.kind == "win"}
        stakes = {}
        for snap in await self.repo.snapshots(a.id):
            stakes[snap.wallet] = (snap.side, snap.amount_base, snap.payout_base, snap.status)
        paid = 0
        for wallet, (side, amount, payout, status) in stakes.items():
            if side == 1:
                want = compute_payout(amount, s.distributable, s.winning_total)
                bal = await self.token_balance(wallet)
                paid += want
                # the wallet got 25 test USDC, put `amount` in and was paid `want`
                check(f"A: winner {wallet[:6]} was paid the pool math amount", wins[wallet].amount_base == want and bal == 25 * USDC - amount + want and payout == want and status == "paid", f"{want} (balance {bal})")
        check("A: three winners and three losers", sum(1 for v in stakes.values() if v[0] == 1) == 3 and sum(1 for v in stakes.values() if v[0] == 0) == 3)
        dust = c2["vault_base"]
        check("A: vault holds only rounding dust", dust == s.distributable - paid and dust < 3, f"{dust} base units")
        check("A: every position closed on chain", c2["positions_open"] == 0 and all(t.status == "done" for t in tasks))
        token_a = self.signer.token(a.id)
        code, positions = await self.chain_get(f"/bets/{a.id}/positions")
        check("A: no position accounts are left on chain", positions["positions"] == [])
        view = (await self.http.get(f"{BACKEND}/markets/{token_a}")).json()
        check("A: the market view still works from snapshots", view["status"] == "settled" and view["participant_count"] == 6 and view["winning_side"] == 1)
        listing = (await self.http.get(f"{BACKEND}/markets/{token_a}/participants")).json()["participants"]
        check("A: participants (with the display name) come from snapshots", len(listing) == 6 and any(p["display_name"] == "Ana" for p in listing))
        await self.notices(a, "A", {"link", "locked", "result"})

        # every client deposit: signed by a wallet holding zero SOL, with the sponsor as the other signer
        print("\n== A: zero SOL check on every deposit ==", flush=True)
        payer_pub = (await self.chain.get("/health")).json()["payer"]
        ok_all = True
        for wallet, sig in self.deposit_sigs_by_wallet:
            tx = await self.rpc("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
            keys = tx["transaction"]["message"]["accountKeys"]
            i = keys.index(wallet)
            signers = keys[: tx["transaction"]["message"]["header"]["numRequiredSignatures"]]
            good = tx["meta"]["preBalances"][i] == 0 and tx["meta"]["postBalances"][i] == 0 and set(signers) == {payer_pub, wallet} and tx["meta"]["err"] is None
            ok_all = ok_all and good
            if not good:
                check(f"A: deposit {sig[:8]} signed by a zero SOL wallet and the sponsor", False, str(signers))
        check(f"A: all {len(self.deposit_sigs_by_wallet)} deposits were signed by a wallet holding zero SOL (sponsor is the other signer)", ok_all)

        # B: cutoff market refunds on its own
        print("\n== B: funding closed, refund ==", flush=True)
        bb = await self.wait("B refund", lambda: self._status(b.id, "refunded", "settled", "locked"), timeout=300)
        check("B: the market closed and refunded (nobody deposited)", bb.status == "refunded", f"{bb.status} {bb.refund_reason}")

        print("\n== close_bet after the link grace window ==", flush=True)
        await self.closed_on_chain(a, "A")
        await self.closed_on_chain(b, "B")
        sol1 = await self.payer_sol()
        spent = sol0 - sol1
        atas = self.atas + 1  # + the dev wallet's token account
        ata_rent = await self.rpc("getMinimumBalanceForRentExemption", [165])  # devnet's own rent, not mainnet's
        fees = spent - atas * ata_rent / 1e9
        check("payer got every position and bet rent back (what remains is token account rent and fees)", fees < 0.002, f"spent {spent:.6f} SOL, {atas} token accounts = {atas * ata_rent / 1e9:.6f}, fees {fees:.6f}")
        print(f"\npayer SOL after: {sol1:.6f}  spent: {spent:.6f}", flush=True)

    async def explorer_links(self, log_path: str, bet_ids: list[str]) -> None:
        print("\n== explorer links ==")
        for line in Path(log_path).read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "tx.sent" and e.get("betId") in bet_ids:
                print(f"  {e['betId'][:8]} {e['instruction']:<22} {explorer(e['signature'])}")
            if e.get("event") == "deposit.sent" and e.get("betId") in bet_ids:
                print(f"  {e['betId'][:8]} {'deposit (sponsored)':<22} {explorer(e['signature'])}")
        for label, sig in self.signatures:
            if "airdrop" in label:
                print(f"  {label:<31} {explorer(sig)}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain-log", required=True)
    args = ap.parse_args()
    settings = load_settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    repo = Repository(pool)
    env = env_live()
    async with httpx.AsyncClient(timeout=60) as http, httpx.AsyncClient(base_url="http://127.0.0.1:8789", headers={"X-Chain-Secret": env["CHAIN_SERVICE_SECRET"]}, timeout=60) as chain:
        live = Live3B(settings, repo, http, chain, env)
        entry = await repo.get_roster_by_name("nicolas")
        assert entry is not None
        try:
            await live.run(entry.handle)
        except BaseException as e:  # keep the report
            check("live check finished without an exception", False, f"{type(e).__name__}: {e}")
        bets = [b.id for b in (await repo.list_bets(entry.handle))[-4:]]
        await live.explorer_links(args.chain_log, bets)
        log = Path(args.chain_log).read_text()
        print(f"\nRPC retries logged by the chain service (rate limits): {log.count(chr(34) + 'rpc.retry' + chr(34))}")
    await pool.close()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
