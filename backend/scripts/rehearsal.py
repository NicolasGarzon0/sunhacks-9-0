"""Rehearsal driver (chunk 6): prove lock, settle, payouts, refunds and the expired link on real devnet without waiting for a game.

DEV ONLY. Never exposed over HTTP. It does not fake the chain: it moves the simulated game and the market's timestamps in the
database, and the RUNNING backend worker (tick every WORKER_TICK_SECONDS) does the real work against chain-service and devnet.
This script only changes rows, waits, and then verifies against devnet, the database and the deposit page.

Needs the stack running: backend (with its worker), chain-service (CHAIN_MODE=real, devnet), gateway and the deposit page (Vite).
It refuses to run unless GAME_PROVIDER=simulated, CHAIN_MODE=real and chain-service reports cluster devnet.
Secrets come from the app's own settings loader and are never printed. It sends the usual proposer texts to the roster entry `nicolas`.

  .venv/bin/python scripts/rehearsal.py check
  .venv/bin/python scripts/rehearsal.py settle [--bet ID | --fresh] [--winner-side 1]   # scenario 1 (lock, settle, payouts, close_bet, 410)
  .venv/bin/python scripts/rehearsal.py refund-one-sided [--wallets 1|2]                # scenario 2 (1 wallet: too_few_participants, 2: one_sided)
  .venv/bin/python scripts/rehearsal.py refund-game cancelled|postponed                 # scenario 3
  .venv/bin/python scripts/rehearsal.py cleanup [--purge-games]

Every database change prints a line starting with `CHANGED`. Every command can be run again: each stage looks at the current state first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db import make_pool  # noqa: E402
from app.gateway_client import GatewayClient  # noqa: E402
from app.logging import Logger  # noqa: E402
from app.repository import Repository  # noqa: E402
from games.simulated import SimulatedProvider, row_to_game  # noqa: E402
from games.teams import TeamDirectory  # noqa: E402
from markets.chain import build_chain_client  # noqa: E402
from markets.links import LinkSigner  # noqa: E402
from markets.pool_math import compute_payout, settle_amounts  # noqa: E402
from markets.service import MarketService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHAIN_DIR = ROOT / "chain-service"
PAGE_DIR = ROOT / "deposit-page"
STATE_DIR = Path(__file__).resolve().parent / ".rehearsal_state"  # gitignored: balances taken before a scenario, for resuming
DEVNET = json.loads((ROOT / "contracts" / "devnet.json").read_text())
RPC = os.environ.get("SOLANA_RPC_URL") or DEVNET["rpcDefault"]
BACKEND = "http://127.0.0.1:8788"
USDC = 1_000_000
GAME_PREFIX = "sim:rehearsal-"
POOL = ("rehearsal-pool-0", "rehearsal-pool-1")  # local wallets in chain-service/.dev-wallets, reused by every scenario
REFUND_COPY = {  # deposit-page/src/lib/status.ts, the words the page shows for each refund reason
    "one_sided": "Everyone picked the same team",
    "too_few_participants": "Not enough people joined",
    "cancelled": "The game was cancelled",
    "postponed": "The game was postponed",
    "tie": "The game ended in a tie",
    "no_result": "We could not get a final result",
}


# ---------------------------------------------------------------- guards and pure helpers (unit tested)


def refusals(settings, chain_health: dict | None) -> list[str]:
    """Why this script must not run, empty when it may. The checks that do not need the network come first."""
    out = []
    if settings.game_provider != "simulated":
        out.append(f"GAME_PROVIDER is {settings.game_provider!r}, rehearsals need 'simulated'")
    if settings.chain_mode != "real":
        out.append(f"CHAIN_MODE is {settings.chain_mode!r}, rehearsals need 'real' (devnet)")
    if chain_health is None:
        out.append("chain-service did not answer GET /health")
    elif chain_health.get("cluster") != "devnet":
        out.append(f"chain-service reports cluster {chain_health.get('cluster')!r}, rehearsals need 'devnet'")
    return out


def expected_settlement(side_totals: tuple[int, int], winning_side: int, fee_bps: int, stakes: dict[str, int]) -> dict:
    """Fee, distributable and each winner's payout, from the same pool math the chain mirrors. `stakes` are the winning wallets' stakes.
    The payout is also computed with the plain formula floor(stake * (total - fee) / winning_total) as an independent cross check."""
    s = settle_amounts(side_totals, winning_side, fee_bps)
    total = side_totals[0] + side_totals[1]
    payouts = {w: compute_payout(stake, s.distributable, s.winning_total) for w, stake in stakes.items()}
    plain = {w: stake * (total - s.fee) // s.winning_total for w, stake in stakes.items()}
    assert payouts == plain, "pool_math disagrees with the plain formula"
    return {
        "fee": s.fee, "distributable": s.distributable, "winning_total": s.winning_total, "payouts": payouts,
        "dust": s.distributable - sum(payouts.values()),
    }


def refund_reason_for(wallet_count: int, sides_used: int) -> str:
    """What close_funding records when it refunds at the deadline: fewer than 2 wallets, or two or more all on one side."""
    return "too_few_participants" if wallet_count < 2 else ("one_sided" if sides_used < 2 else "")


def usd(base: int | None) -> str:
    return "n/a" if base is None else f"{base / USDC:.2f}"


def explorer(sig: str) -> str:
    return f"https://explorer.solana.com/tx/{sig}?cluster=devnet"


# ---------------------------------------------------------------- results


@dataclass
class Report:
    rows: list[tuple[str, str, str, bool]] = field(default_factory=list)  # item, expected, actual, ok

    def add(self, item: str, expected: str, actual: str, ok: bool) -> bool:
        self.rows.append((item, expected, actual, bool(ok)))
        print(f"  {'PASS' if ok else 'FAIL'}  {item}: expected {expected}, got {actual}", flush=True)
        return bool(ok)

    def table(self, title: str) -> str:
        lines = [f"\n### {title}", "", "| Result | Check | Expected | Actual |", "|---|---|---|---|"]
        for item, exp, act, ok in self.rows:
            lines.append(f"| {'PASS' if ok else 'FAIL'} | {item} | {exp} | {act} |")
        failed = sum(1 for r in self.rows if not r[3])
        lines.append(f"\n{len(self.rows) - failed}/{len(self.rows)} checks passed")
        return "\n".join(lines)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if not r[3])


def say(msg: str) -> None:
    print(msg, flush=True)


def changed(msg: str) -> None:
    print(f"CHANGED  {msg}", flush=True)


def ts(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S") if dt else "none"


# ---------------------------------------------------------------- the driver


class Rehearsal:
    def __init__(self, settings, repo: Repository, http: httpx.AsyncClient):
        self.s, self.repo, self.http = settings, repo, http
        self.admin = {"X-Admin-Secret": settings.admin_secret}
        self.chain_headers = {"X-Chain-Secret": settings.chain_service_secret}
        self.signer = LinkSigner(settings.link_signing_secret, settings.deposit_page_url, settings.link_grace)
        self.logger = Logger("warn")
        self.mint = DEVNET["testUsdcMint"]
        self.service: MarketService | None = None
        self._closers: list = []

    # ---- plumbing

    async def wait(self, what: str, fn, timeout: float = 240, every: float = 3):
        say(f"  ... waiting for {what} (worker tick is {self.s.worker_tick_seconds}s)")
        end = time.monotonic() + timeout
        while True:
            value = await fn()
            if value:
                return value
            if time.monotonic() > end:
                raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
            await asyncio.sleep(every)

    async def sql(self, query: str, params: tuple = ()) -> int:
        async with self.repo.pool.connection() as conn:
            return (await conn.execute(query, params)).rowcount

    async def chain_get(self, path: str) -> tuple[int, dict]:
        r = await self.http.get(f"{self.s.chain_service_url}{path}", headers=self.chain_headers, timeout=30)
        return r.status_code, r.json()

    async def chain_post(self, path: str, body: dict) -> tuple[int, dict]:
        r = await self.http.post(f"{self.s.chain_service_url}{path}", headers=self.chain_headers, json=body, timeout=30)
        return r.status_code, r.json()

    async def chain_bet(self, bet_id: str) -> dict | None:
        code, body = await self.chain_get(f"/bets/{bet_id}")
        return body if code == 200 else None

    async def chain_positions(self, bet_id: str, wallets: list[str]) -> list[dict]:
        code, body = await self.chain_post(f"/bets/{bet_id}/positions", {"depositors": wallets})
        return body.get("positions", []) if code == 200 else []

    async def token_balance(self, owner: str) -> int:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [owner, {"mint": self.mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}]}
        for attempt in range(6):
            r = await self.http.post(RPC, json=body, timeout=30)
            if r.status_code == 429:
                await asyncio.sleep(2 * 2 ** attempt)
                continue
            accts = r.json()["result"]["value"]
            return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accts)
        raise RuntimeError("devnet RPC rate limited while reading token balances")

    async def subprocess_json(self, cwd: Path, *cmd: str, timeout: float = 240) -> dict:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        lines = [l for l in out.decode().splitlines() if l.startswith("{")]
        if not lines:
            raise RuntimeError(f"{cmd[1:3]} printed no JSON (exit {proc.returncode}): {err.decode()[-300:]}")
        return json.loads(lines[-1])

    async def page(self, link: str, expect: str, name: str, seconds: int = 45) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "node", "e2e/rehearsal_page.mjs", link, expect, name, str(seconds), cwd=PAGE_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        lines = [l for l in out.decode().splitlines() if l.startswith("{")]
        if not lines:
            return {"ok": False, "text": None, "body": f"page checker failed: {err.decode()[-200:]}"}
        return json.loads(lines[-1])

    async def tx_list(self, bet_id: str, wallets: list[str]) -> dict:
        return await self.subprocess_json(CHAIN_DIR, "npx", "tsx", "scripts/rehearsal_txs.ts", bet_id, *wallets)

    def link(self, bet_id: str) -> str:
        return self.signer.url(bet_id)

    def token(self, bet_id: str) -> str:
        return self.signer.token(bet_id)

    def state_file(self, bet_id: str) -> Path:
        STATE_DIR.mkdir(exist_ok=True)
        return STATE_DIR / f"{bet_id}.json"

    def load_state(self, bet_id: str) -> dict:
        f = self.state_file(bet_id)
        return json.loads(f.read_text()) if f.exists() else {}

    def save_state(self, bet_id: str, state: dict) -> None:
        self.state_file(bet_id).write_text(json.dumps(state, indent=1))

    async def market_service(self) -> MarketService:
        if self.service is None:
            chain = build_chain_client(self.s, self.repo.pool)
            gateway = GatewayClient(self.s.gateway_url, self.s.gateway_shared_secret, self.logger)
            provider = SimulatedProvider(self.repo, TeamDirectory.from_file(), self.s.local_timezone)
            self.service = MarketService(self.repo, chain, self.signer, gateway, self.logger, self.s, provider=provider)
            self._closers += [chain.aclose, gateway.aclose]
        return self.service

    async def aclose(self) -> None:
        for c in self._closers:
            await c()

    # ---- guard

    async def guard(self) -> dict:
        try:
            r = await self.http.get(f"{self.s.chain_service_url}/health", timeout=10)
            health = r.json()
        except Exception:
            health = None
        problems = refusals(self.s, health)
        if problems:
            print("REFUSED: this script only runs with the simulated provider on devnet.\n  - " + "\n  - ".join(problems))
            raise SystemExit(2)
        try:
            backend = (await self.http.get(f"{BACKEND}/health", timeout=10)).json()
            status = (await self.http.get(f"{BACKEND}/admin/worker/status", headers=self.admin, timeout=10)).json()
        except Exception:
            print("REFUSED: the backend is not answering on 127.0.0.1:8788")
            raise SystemExit(2)
        if not (backend.get("database") and status.get("enabled") and status.get("last_tick")):
            print(f"REFUSED: the backend is up but its worker is not ticking: {status}")
            raise SystemExit(2)
        say(f"guard ok: provider=simulated chain_mode=real cluster=devnet payer_sol={health.get('payer_sol'):.3f} "
            f"worker_tick={self.s.worker_tick_seconds}s settle_confirm_wait={self.s.settle_confirm_wait_minutes}min "
            f"link_grace={self.s.link_grace}")
        return health

    async def probe_running_provider(self, game_id: str) -> None:
        """The running backend must also be on the simulated provider: its admin endpoint answers 409 on the real one.
        Setting a game to the status it already has changes nothing."""
        g = await self.repo.get_sim_game(game_id)
        if g is None:
            print(f"REFUSED: {game_id} is not in sim_games, so the running worker cannot be on the simulated provider for it")
            raise SystemExit(2)
        r = await self.http.post(f"{BACKEND}/admin/games/{game_id}/status", headers=self.admin, json={"status": g["status"]}, timeout=10)
        if r.status_code != 200:
            print(f"REFUSED: the running backend answered {r.status_code} to a simulated game admin call, it is not on the simulated provider")
            raise SystemExit(2)
        say(f"running backend confirmed on the simulated provider (game {game_id} is {g['status']})")

    # ---- creating a fresh market (the same open_market the text flow's YES calls)

    async def new_market(self, tag: str, away: str, home: str, minutes: int = 120):
        entry = await self.repo.get_roster_by_name("nicolas")
        assert entry is not None, "roster entry 'nicolas' not found"
        teams = TeamDirectory.from_file()
        kickoff = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
        row = {
            "game_id": f"{GAME_PREFIX}{tag}", "league": teams.teams[home].league,
            "home_id": home, "home_name": teams.name(home), "away_id": away, "away_name": teams.name(away), "kickoff_utc": kickoff,
        }
        await self.repo.insert_sim_game(row)
        changed(f"sim_games: inserted {row['game_id']} ({row['away_name']} at {row['home_name']}), kickoff {ts(kickoff)} UTC (+{minutes} min)")
        svc = await self.market_service()
        result = await svc.open_market(entry.handle, row_to_game({**row, "status": "scheduled"}))
        if result.kind != "opened":
            raise RuntimeError(f"open_market returned {result.kind}")
        bets = [b for b in await self.repo.list_bets(entry.handle) if b.game_id == row["game_id"]]
        bet = bets[-1]
        changed(f"bets: opened market {bet.id} on devnet (status {bet.status}); link text sent to the proposer")
        return bet

    async def deposit(self, bet_id: str, wallet_file: str, side: int, usd_amount: str, name: str) -> str:
        """The deposit page's own sponsored flow (deposit_tx, user signature, deposit_submit) with a local wallet. The wallet is
        funded from the test USDC airdrop only when it holds less than the stake: the airdrop is limited per address per hour
        (AIRDROP_PER_IP_PER_HOUR) and per wallet for life, so the rehearsal wallets are reused and refunds put the money back."""
        base = ("npx", "tsx", "scripts/deposit_client.ts")
        common = ("--link", self.token(bet_id), "--wallet-file", wallet_file, "--api", BACKEND)
        info = await self.subprocess_json(CHAIN_DIR, *base, "wallet", *common)
        have = int(info.get("body", {}).get("usdc_base") or 0)
        need = int(float(usd_amount) * USDC)
        if have < need:
            a = await self.subprocess_json(CHAIN_DIR, *base, "airdrop", *common)
            if not a.get("ok"):
                raise RuntimeError(f"wallet {wallet_file} holds {usd(have)} and the airdrop failed: {a.get('body')} "
                                   "(wait for the hourly limit, or delete chain-service/.dev-wallets/" + wallet_file + ".json for a new wallet)")
            changed(f"airdrop: {wallet_file} had {usd(have)} USDC, now {usd(a['body'].get('usdc_base'))}")
        d = await self.subprocess_json(CHAIN_DIR, *base, "deposit", *common, "--side", str(side), "--usd", usd_amount, "--name", name)
        if not d.get("ok"):
            raise RuntimeError(f"deposit failed: {d.get('body')}")
        changed(f"chain: {d['wallet'][:4]}...{d['wallet'][-4:]} deposited ${usd_amount} on side {side}  {explorer(d['body'].get('signature', ''))}")
        return d["wallet"]

    # ---- shared checks

    async def notices(self, bet_id: str) -> dict[str, str]:
        return {n["kind"]: n["status"] for n in await self.repo.list_notices(bet_id)}

    async def notice_sent(self, bet_id: str, kind: str, timeout: float = 90) -> bool:
        async def sent():
            return (await self.notices(bet_id)).get(kind) == "sent"
        try:
            await self.wait(f"the {kind!r} text to the proposer", sent, timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def expire_and_close(self, bet_id: str, rep: Report, label: str, wallets: list[str]) -> None:
        """After the link grace: the worker calls close_bet and the link answers 410. The grace is moved, not waited out: the
        running process was started with LINK_GRACE_HOURS (no restart needed), so payouts_completed_at moves back past the grace."""
        bet = await self.repo.get_bet(bet_id)
        assert bet is not None and bet.payouts_completed_at is not None
        new = datetime.now(timezone.utc) - self.s.link_grace - timedelta(minutes=2)
        if bet.chain_closed_at is None:
            await self.sql("update bets set payouts_completed_at = %s where id = %s and chain_closed_at is null", (new, bet_id))
            changed(f"bets.payouts_completed_at {ts(bet.payouts_completed_at)} -> {ts(new)} UTC (link grace {self.s.link_grace} has passed)")
        view = (await self.http.get(f"{BACKEND}/admin/bets/{bet_id}", headers=self.admin, timeout=30)).json()
        exp = view.get("link_expires_at")
        rep.add(f"{label}: the running backend sees the link as expired", "link_expires_at in the past",
                str(exp), bool(exp) and datetime.fromisoformat(exp.replace("Z", "+00:00")) < datetime.now(timezone.utc))

        async def closed():
            b = await self.repo.get_bet(bet_id)
            return b if b and b.chain_closed_at else None
        try:
            b = await self.wait("the worker to run close_bet", closed, timeout=240)
            rep.add(f"{label}: worker ran close_bet", "chain_closed_at set", ts(b.chain_closed_at), True)
        except TimeoutError:
            b = await self.repo.get_bet(bet_id)
            rep.add(f"{label}: worker ran close_bet", "chain_closed_at set", f"attempts {b.close_bet_attempts} error {b.close_bet_last_error}", False)
        code, body = await self.chain_get(f"/bets/{bet_id}")
        rep.add(f"{label}: the Bet account is gone on devnet", "NOT_FOUND", body.get("code", str(code)), code == 404 and body.get("code") == "NOT_FOUND")
        r = await self.http.get(f"{BACKEND}/markets/{self.token(bet_id)}", timeout=30)
        rep.add(f"{label}: the link now answers 410 LINK_EXPIRED", "410 LINK_EXPIRED", f"{r.status_code} {r.json().get('code')}",
                r.status_code == 410 and r.json().get("code") == "LINK_EXPIRED")
        pg = await self.page(self.link(bet_id), "expired", f"{label.split()[0].lower()}-expired")
        rep.add(f"{label}: the page shows the expired view", "This bet is over", (pg.get("text") or pg.get("body", ""))[:60], pg["ok"])

    async def tx_table(self, bet_id: str, wallets: list[str]) -> dict:
        data = await self.tx_list(bet_id, wallets)
        say("\n  transactions on the Bet account (oldest first):")
        for t in data["txs"]:
            say(f"    {t['instruction']:<22} {'ok ' if t['ok'] else 'ERR'} {t['link']}")
        return data

    # ================================================================ scenario 1: settle with a winner

    async def settle(self, bet_id: str | None, fresh: bool, winner: int) -> Report:
        rep = Report()
        away, home = (None, None)
        if fresh:
            tag = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
            bet = await self.new_market(f"settle-{tag}", "ncaaf-byu", "ncaaf-ariz")
            await self.probe_running_provider(bet.game_id)
            wa = await self.deposit(bet.id, POOL[0], 0, "10", "Away10")
            wh = await self.deposit(bet.id, POOL[1], 1, "5", "Home5")
            say(f"fresh market {bet.id}: {wa[:4]} on side 0 ($10), {wh[:4]} on side 1 ($5)")
        else:
            bet = await self.repo.get_bet(bet_id)  # type: ignore[arg-type]
            assert bet is not None, "unknown bet"
            await self.probe_running_provider(bet.game_id)
        bet_id = bet.id
        label = "Settle"
        say(f"\n== Scenario 1: settle with a winner, bet {bet_id} ({bet.away_name} at {bet.home_name}), status {bet.status} ==")
        state = self.load_state(bet_id)

        # --- preflight
        wallets = await self.repo.participant_wallets(bet_id)
        if bet.status == "open":
            cb = await self.chain_bet(bet_id)
            assert cb is not None
            pos = await self.chain_positions(bet_id, wallets)
            state["positions"] = {p["wallet"]: {"side": p["side"], "amount": p["amount_base"]} for p in pos}
            state["balances_before"] = {w: await self.token_balance(w) for w in wallets}
            state["treasury_before"] = await self.token_balance(bet.treasury)
            state["pools"] = list(cb["side_totals"])
            self.save_state(bet_id, state)
            say(f"preflight: chain {cb['state']}, pools {usd(cb['side_totals'][0])}/{usd(cb['side_totals'][1])}, "
                f"{cb['participant_count']} participants, vault {usd(cb['vault_base'])}")
            rep.add(f"{label}: preflight open on chain with both sides funded", "open, 2 participants, both pools > 0",
                    f"{cb['state']}, {cb['participant_count']}, {cb['side_totals']}",
                    cb["state"] == "open" and cb["participant_count"] == 2 and min(cb["side_totals"]) > 0)
        if "positions" not in state:
            snaps = await self.repo.snapshots(bet_id)
            state["positions"] = {s.wallet: {"side": s.side, "amount": s.amount_base} for s in snaps}
            state["pools"] = [sum(s.amount_base for s in snaps if s.side == i) for i in (0, 1)]
        pools = (state["pools"][0], state["pools"][1])
        win_stakes = {w: p["amount"] for w, p in state["positions"].items() if p["side"] == winner}
        lose_wallets = [w for w, p in state["positions"].items() if p["side"] != winner]
        exp = expected_settlement(pools, winner, bet.fee_bps, win_stakes)
        say(f"expected: fee {usd(exp['fee'])}, distributable {usd(exp['distributable'])}, payouts "
            f"{ {w[:4]: usd(v) for w, v in exp['payouts'].items()} }, dust {exp['dust']} base units")

        # --- lock
        if bet.status == "open":
            new = datetime.now(timezone.utc) - timedelta(minutes=1)
            await self.sql("update bets set funding_closes_at = %s where id = %s and status = 'open'", (new, bet_id))
            changed(f"bets.funding_closes_at {ts(bet.funding_closes_at)} -> {ts(new)} UTC (the funding window has closed)")
            bet = await self._wait_status(bet_id, "locked", "settled", "refunded")
        if bet.status in ("locked", "settled"):
            cb = await self.chain_bet(bet_id)
            if bet.status == "locked":
                rep.add(f"{label}: worker locked the market", "bets.status locked, chain locked",
                        f"{bet.status}, {cb['state'] if cb else 'gone'}", bet.status == "locked" and bool(cb) and cb["state"] == "locked")
                snaps = await self.repo.snapshots(bet_id)
                rep.add(f"{label}: position_snapshots taken at the lock", f"{len(wallets)} held", f"{len(snaps)} {sorted({s.status for s in snaps})}",
                        len(snaps) == len(wallets) and {s.status for s in snaps} == {"held"})
                rep.add(f"{label}: locked text sent to the proposer", "bet_notices locked = sent", str(await self.notice_sent(bet_id, "locked")),
                        (await self.notices(bet_id)).get("locked") == "sent")
                pg = await self.page(self.link(bet_id), "locked", "settle-locked")
                rep.add(f"{label}: the page shows the locked view", "Betting is closed. Waiting for the game.", pg.get("text") or pg.get("body", "")[:80], pg["ok"])
        elif bet.status == "refunded":
            rep.add(f"{label}: worker locked the market", "locked", f"refunded ({bet.refund_reason})", False)
            return rep

        # --- result
        bet = await self.repo.get_bet(bet_id)
        if bet.status == "locked":
            kick = datetime.now(timezone.utc) - timedelta(minutes=10)
            await self.sql("update bets set kickoff_utc = %s where id = %s and status = 'locked'", (kick, bet_id))
            await self.sql("update sim_games set kickoff_utc = %s where game_id = %s", (kick, bet.game_id))
            changed(f"bets.kickoff_utc and sim_games.kickoff_utc {ts(bet.kickoff_utc)} -> {ts(kick)} UTC (the game has started; settle_after stays {ts(bet.settle_after)})")
            home_score, away_score = (27, 24) if winner == 1 else (24, 27)
            await self.repo.finish_sim_game(bet.game_id, home_score, away_score)
            changed(f"sim_games {bet.game_id}: final, {bet.home_name} {home_score} - {bet.away_name} {away_score}, official (side {winner} wins)")
            bet = await self._wait_status(bet_id, "settled", "refunded")
        rep.add(f"{label}: worker settled the market on chain", f"settled, winning side {winner}", f"{bet.status}, winning side {bet.winning_side}",
                bet.status == "settled" and bet.winning_side == winner)
        say(f"  result reads: first seen {ts(bet.result_first_seen_at)}, confirmed {ts(bet.final_confirmed_at)} "
            f"(SETTLE_CONFIRM_WAIT_MINUTES={self.s.settle_confirm_wait_minutes}: with 0 one read is enough, see the README)")
        rep.add(f"{label}: result recorded from the simulated game", "score 24-27 or 27-24, confirmed", f"{bet.result_away_score}-{bet.result_home_score}, confirmed {ts(bet.final_confirmed_at)}",
                bet.result_home_score is not None and bet.final_confirmed_at is not None and bet.result_winner_side == winner)
        rep.add(f"{label}: fee recorded on the bet", usd(exp["fee"]), usd(bet.fee_paid_base), bet.fee_paid_base == exp["fee"])

        async def paid():
            b = await self.repo.get_bet(bet_id)
            return b if b and b.payouts_completed_at else None
        bet = await self.wait("payouts to complete", paid, timeout=300)

        # --- payouts
        tasks = await self.repo.payout_tasks(bet_id)
        say("\n  payout_tasks:")
        for t in tasks:
            say(f"    {t.wallet[:4]}...{t.wallet[-4:]} {t.kind:<11} {usd(t.amount_base):>6} {t.status:<5} tx {t.tx_ref[:12] + '...' if t.tx_ref else '-'}")
        wins = [t for t in tasks if t.kind == "win"]
        closes = [t for t in tasks if t.kind == "close_loser"]
        rep.add(f"{label}: payout_tasks", f"{len(win_stakes)} win + {len(lose_wallets)} close_loser, all done",
                f"{len(wins)} win + {len(closes)} close_loser, {sorted({t.status for t in tasks})}",
                len(wins) == len(win_stakes) and len(closes) == len(lose_wallets) and all(t.status == "done" for t in tasks))
        snaps = await self.repo.snapshots(bet_id)
        say("  position_snapshots:")
        for sn in snaps:
            say(f"    {sn.wallet[:4]}...{sn.wallet[-4:]} side {sn.side} stake {usd(sn.amount_base):>6} payout {usd(sn.payout_base):>6} {sn.status}")
        ok_snaps = all(
            (sn.status == "paid" and sn.payout_base == exp["payouts"].get(sn.wallet)) if sn.wallet in win_stakes else (sn.status == "closed_loser")
            for sn in snaps
        ) and len(snaps) == len(wallets)
        rep.add(f"{label}: position_snapshots", "winners paid with the expected payout, losers closed_loser",
                ", ".join(f"{sn.wallet[:4]}:{sn.status}" for sn in snaps), ok_snaps)

        for w, want in exp["payouts"].items():
            task = next((t for t in wins if t.wallet == w), None)
            after = await self.token_balance(w)
            before = state.get("balances_before", {}).get(w)
            delta = None if before is None else after - before
            rep.add(f"{label}: winner {w[:4]}...{w[-4:]} payout (USDC)", f"{usd(want)} = floor({win_stakes[w]} * ({sum(pools)} - {exp['fee']}) / {exp['winning_total']}) base units",
                    f"task {usd(task.amount_base if task else None)}, balance {usd(before)} -> {usd(after)} (change {usd(delta)})",
                    task is not None and task.amount_base == want and delta == want)
        for w in lose_wallets:
            after = await self.token_balance(w)
            before = state.get("balances_before", {}).get(w)
            rep.add(f"{label}: loser {w[:4]}...{w[-4:]} balance unchanged", "change 0.00", f"{usd(before)} -> {usd(after)}", before == after)
        positions_on_chain = (await self.tx_list(bet_id, list(state["positions"])))["positions"]
        rep.add(f"{label}: no Position account is left on devnet (winner claim and loser close both closed theirs)", "all closed",
                str({w[:4]: ("open" if v else "closed") for w, v in positions_on_chain.items()}), not any(positions_on_chain.values()))

        cb = await self.chain_bet(bet_id)
        treasury_now = await self.token_balance(bet.treasury)
        fee_in = treasury_now - state.get("treasury_before", treasury_now)
        rep.add(f"{label}: treasury received the fee (USDC)", usd(exp["fee"]), usd(fee_in), fee_in == exp["fee"])
        rep.add(f"{label}: vault holds only rounding dust before close_bet (base units)", f"{exp['dust']}", str(cb["vault_base"] if cb else "gone"),
                bool(cb) and cb["vault_base"] == exp["dust"] and cb["vault_base"] < 2)
        n = await self.notices(bet_id)
        sent = await self.notice_sent(bet_id, "result")
        rep.add(f"{label}: proposer notices in bet_notices", "link, locked, result all sent", str(n), sent and (await self.notices(bet_id)).get("locked") == "sent")
        svc = await self.market_service()
        say("  result text: " + await svc.render_notice(await self.repo.get_bet(bet_id), "result"))
        pg = await self.page(self.link(bet_id), "settled", "settle-settled")
        winner_name = bet.away_name if winner == 0 else bet.home_name
        rep.add(f"{label}: the page shows the settled view", f"{winner_name} won.", pg.get("text") or pg.get("body", "")[:80], pg["ok"] and winner_name in (pg.get("text") or ""))

        # --- link grace and close_bet
        await self.expire_and_close(bet_id, rep, label, list(state["positions"]))
        treasury_end = await self.token_balance(bet.treasury)
        gained = treasury_end - state.get("treasury_before", treasury_end)
        rep.add(f"{label}: treasury after close_bet holds fee plus dust (USDC)", usd(exp["fee"] + exp["dust"]), usd(gained), gained == exp["fee"] + exp["dust"])
        txs = await self.tx_table(bet_id, list(state["positions"]))
        by = {}
        for t in txs["txs"]:
            by.setdefault(t["instruction"], []).append(t)
        for name in ("Lock", "Settle", "CloseBet"):
            t = (by.get(name) or [None])[0]
            rep.add(f"{label}: {name} transaction on devnet", "confirmed, no error", t["link"] if t else "missing", bool(t) and t["ok"])
        n_claims = len(by.get("Claim", []))
        rep.add(f"{label}: claim transactions", f"{len(win_stakes)}", f"{n_claims} " + ", ".join(t["link"] for t in by.get("Claim", [])), n_claims == len(win_stakes))
        n_close = len(by.get("CloseLoserPosition", []))
        rep.add(f"{label}: close_loser_position transactions", f"{len(lose_wallets)}", f"{n_close} " + ", ".join(t["link"] for t in by.get("CloseLoserPosition", [])), n_close == len(lose_wallets))
        return rep

    async def _wait_status(self, bet_id: str, *statuses: str):
        async def reached():
            b = await self.repo.get_bet(bet_id)
            return b if b and b.status in statuses else None
        return await self.wait(f"the worker to move the bet to {'/'.join(statuses)}", reached, timeout=240)

    # ================================================================ scenarios 2 and 3: refunds

    async def refund_one_sided(self, wallets_n: int) -> Report:
        rep = Report()
        label = f"Refund one sided ({wallets_n} wallet{'s' if wallets_n > 1 else ''})"
        tag = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
        bet = await self.new_market(f"onesided{wallets_n}-{tag}", "ncaaf-ttu", "ncaaf-ariz")
        await self.probe_running_provider(bet.game_id)
        say(f"\n== Scenario 2: {label}, bet {bet.id} ==")
        funded: dict[str, int] = {}
        for i in range(wallets_n):
            w = await self.deposit(bet.id, POOL[i], 0, "5", f"Rehearsal{i}")
            funded[w] = 5 * USDC
        return await self._finish_refund(bet.id, rep, label, funded, expected_reason=refund_reason_for(wallets_n, 1), cancel_status=None)

    async def refund_game(self, status: str) -> Report:
        rep = Report()
        label = f"Refund {status} game"
        tag = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
        bet = await self.new_market(f"{status}-{tag}", "ncaaf-byu", "ncaaf-ariz")
        await self.probe_running_provider(bet.game_id)
        say(f"\n== Scenario 3: {label}, bet {bet.id} ==")
        # two wallets on opposite sides, so only the game status can be the reason (one sided would be a different reason)
        funded = {}
        w0 = await self.deposit(bet.id, POOL[0], 0, "5", "RehearsalA")
        w1 = await self.deposit(bet.id, POOL[1], 1, "3", "RehearsalB")
        funded[w0], funded[w1] = 5 * USDC, 3 * USDC
        return await self._finish_refund(bet.id, rep, label, funded, expected_reason=status, cancel_status=status)

    async def _finish_refund(self, bet_id: str, rep: Report, label: str, funded: dict[str, int], expected_reason: str, cancel_status: str | None) -> Report:
        bet = await self.repo.get_bet(bet_id)
        wallets = list(funded)
        # the deposits are on chain and each wallet's balance is lower by its stake; the balances after the refund must be the old ones
        after_deposit = {w: await self.token_balance(w) for w in wallets}
        treasury0 = await self.token_balance(bet.treasury)
        cb = await self.chain_bet(bet_id)
        rep.add(f"{label}: deposits are in the pool on chain", "open, " + usd(sum(funded.values())), f"{cb['state']}, {usd(sum(cb['side_totals']))}",
                cb["state"] == "open" and sum(cb["side_totals"]) == sum(funded.values()))
        before = {w: after_deposit[w] + funded[w] for w in wallets}  # what each wallet held before it deposited
        if cancel_status:
            await self.repo.set_sim_game_status(bet.game_id, cancel_status)
            changed(f"sim_games {bet.game_id}: status scheduled -> {cancel_status} (before the lock; funding is still open until {ts(bet.funding_closes_at)} UTC)")
        else:
            new = datetime.now(timezone.utc) - timedelta(minutes=1)
            await self.sql("update bets set funding_closes_at = %s where id = %s and status = 'open'", (new, bet_id))
            changed(f"bets.funding_closes_at {ts(bet.funding_closes_at)} -> {ts(new)} UTC (the funding window has closed)")
        bet = await self._wait_status(bet_id, "refunded", "locked", "settled")
        rep.add(f"{label}: worker refunded the market", f"refunded, reason {expected_reason}", f"{bet.status}, {bet.refund_reason}",
                bet.status == "refunded" and bet.refund_reason == expected_reason)
        cb = await self.chain_bet(bet_id)

        async def paid():
            b = await self.repo.get_bet(bet_id)
            return b if b and b.payouts_completed_at else None
        bet = await self.wait("refund payouts to complete", paid, timeout=300)
        cb = await self.chain_bet(bet_id)
        treasury1 = await self.token_balance(bet.treasury)
        rep.add(f"{label}: no fee, vault empty, treasury unchanged", "fee 0, vault 0, treasury +0.00",
                f"fee {usd(cb['fee_paid_base'])}, vault {usd(cb['vault_base'])}, treasury {usd(treasury1 - treasury0)}",
                cb["state"] == "refunded" and cb["fee_paid_base"] == 0 and cb["vault_base"] == 0 and treasury1 == treasury0)
        tasks = await self.repo.payout_tasks(bet_id)
        rep.add(f"{label}: payout_tasks", f"{len(wallets)} refund, all done", f"{[(t.kind, t.status, usd(t.amount_base)) for t in tasks]}",
                len(tasks) == len(wallets) and all(t.kind == "refund" and t.status == "done" and t.amount_base == funded[t.wallet] for t in tasks))
        snaps = await self.repo.snapshots(bet_id)
        rep.add(f"{label}: position_snapshots", "refunded in full", str([(s.wallet[:4], s.status, usd(s.payout_base)) for s in snaps]),
                len(snaps) == len(wallets) and all(s.status == "refunded" and s.payout_base == funded[s.wallet] for s in snaps))
        for w in wallets:
            now_bal = await self.token_balance(w)
            rep.add(f"{label}: wallet {w[:4]}...{w[-4:]} balance returned (USDC)", f"{usd(before[w])} (before the deposit)",
                    f"{usd(before[w])} -> {usd(after_deposit[w])} after deposit -> {usd(now_bal)}", now_bal == before[w])
        kind = "refunded" if expected_reason in ("one_sided", "too_few_participants") else "refund_result"
        sent = await self.notice_sent(bet_id, kind)
        rep.add(f"{label}: proposer notice in bet_notices", f"{kind} sent", str(await self.notices(bet_id)), sent)
        svc = await self.market_service()
        say(f"  {kind} text: " + await svc.render_notice(await self.repo.get_bet(bet_id), kind))
        pg = await self.page(self.link(bet_id), "refunded", f"refund-{expected_reason}")
        rep.add(f"{label}: the page shows the refunded view", REFUND_COPY[expected_reason], pg.get("text") or pg.get("body", "")[:80],
                pg["ok"] and REFUND_COPY[expected_reason] in (pg.get("text") or ""))
        await self.expire_and_close(bet_id, rep, label, wallets)
        txs = await self.tx_table(bet_id, wallets)
        names = [t["instruction"] for t in txs["txs"]]
        rep.add(f"{label}: RefundAll and CloseBet transactions on devnet", "both confirmed", ", ".join(f"{t['instruction']} {t['link']}" for t in txs["txs"] if t["instruction"] in ("RefundAll", "CloseBet")),
                all(t["ok"] for t in txs["txs"]) and "RefundAll" in names and "CloseBet" in names)
        return rep

    # ================================================================ cleanup

    async def cleanup(self, purge: bool) -> None:
        """Drive every unfinished rehearsal market to the end (cancel the game so the worker refunds, expire the link so the worker
        closes it), then optionally delete the finished rehearsal games. Safe to run any number of times."""
        async with self.repo.pool.connection() as conn:
            cur = await conn.execute("select game_id, status from sim_games where game_id like %s order by game_id", (GAME_PREFIX + "%",))
            games = await cur.fetchall()
        say(f"{len(games)} rehearsal game(s) in sim_games")
        entry = await self.repo.get_roster_by_name("nicolas")
        bets = [b for b in await self.repo.list_bets(entry.handle) if b.game_id.startswith(GAME_PREFIX)] if entry else []
        for b in bets:
            if b.status in ("open", "locked"):
                await self.repo.set_sim_game_status(b.game_id, "cancelled")
                changed(f"sim_games {b.game_id}: -> cancelled so the worker refunds bet {b.id[:8]} ({b.status})")
        for b in bets:
            if b.status in ("open", "locked"):
                try:
                    await self._wait_status(b.id, "refunded", "settled")
                except TimeoutError:
                    say(f"  bet {b.id[:8]} did not finish refunding in time, rerun cleanup")
        for b in [await self.repo.get_bet(x.id) for x in bets]:
            if b.status in ("settled", "refunded") and b.chain_closed_at is None and b.payouts_completed_at is not None:
                new = datetime.now(timezone.utc) - self.s.link_grace - timedelta(minutes=2)
                await self.sql("update bets set payouts_completed_at = %s where id = %s and chain_closed_at is null", (new, b.id))
                changed(f"bets.payouts_completed_at of {b.id[:8]} -> {ts(new)} UTC so the worker closes it on chain")
        open_left = [b for b in [await self.repo.get_bet(x.id) for x in bets] if b.status in ("open", "locked") or (b.status in ("settled", "refunded") and b.chain_closed_at is None)]
        say(f"{len(bets)} rehearsal bet(s), {len(open_left)} not fully closed yet" + ("" if not open_left else " (the worker finishes them within a minute or two, rerun cleanup)"))
        if purge and not open_left:
            n = await self.sql("delete from sim_games where game_id like %s", (GAME_PREFIX + "%",))
            changed(f"sim_games: deleted {n} rehearsal game row(s)")
        elif purge:
            say("not purging games while bets are still open")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="run the guards and print the state of the stack")
    p = sub.add_parser("settle", help="scenario 1")
    p.add_argument("--bet", help="an open market to use (default: the newest open market of the live test number)")
    p.add_argument("--fresh", action="store_true", help="make a new market with two local wallets ($10 away, $5 home) instead")
    p.add_argument("--winner-side", type=int, choices=[0, 1], default=1, help="1 = home team (default), 0 = away team")
    p = sub.add_parser("refund-one-sided", help="scenario 2")
    p.add_argument("--wallets", type=int, choices=[1, 2], default=1, help="wallets on the same side: 1 gives too_few_participants, 2 gives one_sided")
    p = sub.add_parser("refund-game", help="scenario 3")
    p.add_argument("status", choices=["cancelled", "postponed"])
    p = sub.add_parser("cleanup", help="finish and close unfinished rehearsal markets")
    p.add_argument("--purge-games", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    pool = make_pool(settings.database_url)
    await pool.open()
    repo = Repository(pool)
    code = 0
    async with httpx.AsyncClient() as http:
        r = Rehearsal(settings, repo, http)
        try:
            await r.guard()
            if args.cmd == "check":
                entry = await repo.get_roster_by_name("nicolas")
                bets = await repo.list_bets(entry.handle) if entry else []
                for b in bets[-6:]:
                    say(f"  bet {b.id} {b.game_id:<34} {b.status:<9} reason {b.refund_reason} closed_on_chain {ts(b.chain_closed_at)}")
            elif args.cmd == "settle":
                bet_id = args.bet
                if not bet_id and not args.fresh:
                    entry = await repo.get_roster_by_name("nicolas")
                    live = [b for b in await repo.list_bets(entry.handle) if b.status in ("open", "locked") and not b.game_id.startswith(GAME_PREFIX)]
                    if not live:
                        print("no open market to use: pass --bet ID or --fresh")
                        return 2
                    bet_id = live[-1].id
                rep = await r.settle(bet_id, args.fresh, args.winner_side)
                print(rep.table("Scenario 1: settle with a winner"))
                code = 1 if rep.failed else 0
            elif args.cmd == "refund-one-sided":
                rep = await r.refund_one_sided(args.wallets)
                print(rep.table(f"Scenario 2: refund, one side empty ({args.wallets} wallet)"))
                code = 1 if rep.failed else 0
            elif args.cmd == "refund-game":
                rep = await r.refund_game(args.status)
                print(rep.table(f"Scenario 3: {args.status} game"))
                code = 1 if rep.failed else 0
            elif args.cmd == "cleanup":
                await r.cleanup(args.purge_games)
        finally:
            await r.aclose()
    await pool.close()
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
