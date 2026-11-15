"""MockChainClient: the pool contract's rules over the mock_chain_* tables (own SQL; drop the tables to remove it).

State lives in Postgres so a restart keeps it. Every mutation locks the bet row (select ... for update), so the
participant cap and per wallet max hold under concurrent deposits.

Parity with programs/handshake_pool (chunk 3A), see contracts/README.md "Differences between the mock and the contract":
  * lock needs both sides funded and 2 or more participants; lock and settle stop at refund_after
  * deposits stop at funding_closes_at, with the contract's error order
  * claiming closes the position and a loser's position stays until close_loser_position (no transfer)
  * get_positions lists only open positions; side totals and participant_count never go down
  * close_bet sweeps the dust and closes the bet: reads then return NOT_FOUND
  * emergency_refund (anyone, at or after refund_after) exists on the mock for tests
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .chain import BetParams, ChainBet, ChainError, Claim, Position, is_valid_wallet
from .pool_math import compute_payout, settle_amounts

BET_SELECT = (
    "select bet_id::text as bet_id, state, params, vault_base, fee_paid_base, distributable_base, winning_side, "
    "treasury_balance_base, closed from mock_chain_bets"
)
POS_SELECT = "select bet_id::text as bet_id, wallet, side, amount_base, claimed, payout_base, closed from mock_chain_positions"
MAX_FEE_BPS = 1000


def _bet(row: dict, positions: list[dict]) -> ChainBet:
    totals = [0, 0]
    for p in positions:  # the chain's side totals never go down, even after positions close
        totals[p["side"]] += p["amount_base"]
    return ChainBet(
        bet_id=row["bet_id"], state=row["state"], side_totals=(totals[0], totals[1]),
        participant_count=len(positions), fee_paid_base=row["fee_paid_base"],
        distributable_base=row["distributable_base"], winning_side=row["winning_side"],
        vault_base=row["vault_base"], treasury_balance_base=row["treasury_balance_base"],
        positions_open=sum(1 for p in positions if not p["closed"]),
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _when(params: dict, key: str) -> datetime | None:
    raw = params.get(key)
    return datetime.fromisoformat(raw) if raw else None


class MockChainClient:
    mode = "mock"

    def __init__(self, pool, now=None) -> None:
        self.pool = pool
        self.now = now or (lambda: datetime.now(timezone.utc))

    # --- reads ---

    async def _load(self, cur, bet_id: str, lock: bool = False, allow_closed: bool = False) -> tuple[dict, list[dict]]:
        try:
            uuid.UUID(str(bet_id))
        except ValueError:
            raise ChainError("NOT_FOUND", "unknown bet") from None
        await cur.execute(BET_SELECT + " where bet_id = %s" + (" for update" if lock else ""), (bet_id,))
        row = await cur.fetchone()
        if row is None or (row["closed"] and not allow_closed):
            raise ChainError("NOT_FOUND", "unknown bet")
        await cur.execute(POS_SELECT + " where bet_id = %s order by created_at, wallet", (bet_id,))
        return row, list(await cur.fetchall())

    async def get_bet(self, bet_id: str) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            return _bet(*await self._load(cur, bet_id))

    async def get_positions(self, bet_id: str) -> list[Position]:
        """Open positions only, like the chain: a claimed or closed position account no longer exists."""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            _, positions = await self._load(cur, bet_id)
            return [Position(**{**p, "claimed": False, "payout_base": 0, "closed": False}) for p in positions if not p["closed"]]

    async def health(self) -> dict | None:
        return None

    # --- writes ---

    def _validate(self, p: BetParams) -> None:
        if p.fee_bps > MAX_FEE_BPS:
            raise ChainError("INVALID_PARAMS", "fee_bps above the 10% cap")
        if p.min_stake_base <= 0 or p.min_stake_base > p.max_stake_base or not 2 <= p.max_participants <= 100:
            raise ChainError("INVALID_PARAMS", "bad stake limits or participant cap")
        if p.funding_closes_at is not None and p.funding_closes_at <= self.now():
            raise ChainError("INVALID_PARAMS", "funding_closes_at must be in the future")
        if p.refund_after is not None and p.funding_closes_at is not None and p.refund_after <= p.funding_closes_at:
            raise ChainError("INVALID_PARAMS", "refund_after must be after funding_closes_at")

    async def create_bet(self, bet_id: str, params: BetParams) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            try:
                uuid.UUID(str(bet_id))
            except ValueError:
                raise ChainError("INVALID_PARAMS", "bet_id must be a UUID") from None
            await cur.execute("select 1 from mock_chain_bets where bet_id = %s", (bet_id,))
            if await cur.fetchone() is None:
                self._validate(params)
                stored = {**params.__dict__, "funding_closes_at": _iso(params.funding_closes_at), "refund_after": _iso(params.refund_after)}
                await cur.execute(
                    "insert into mock_chain_bets (bet_id, state, params) values (%s, 'open', %s) on conflict (bet_id) do nothing",
                    (bet_id, Jsonb(stored)),
                )
            return _bet(*await self._load(cur, bet_id))

    async def deposit(self, bet_id: str, wallet: str, side: int, amount_base: int) -> Position:
        """Mock only. Mirrors what the contract enforces on a user signed deposit, in the contract's error order."""
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                row, positions = await self._load(cur, bet_id, lock=True)
                p = row["params"]
                if row["state"] != "open":
                    raise ChainError("MARKET_NOT_OPEN", f"market is {row['state']}")
                closes = _when(p, "funding_closes_at")
                if closes is not None and self.now() >= closes:
                    raise ChainError("FUNDING_CLOSED", "funding has closed")
                if isinstance(side, bool) or side not in (0, 1):
                    raise ChainError("INVALID_SIDE", "side must be 0 or 1")
                if isinstance(amount_base, bool) or not isinstance(amount_base, int) or amount_base < p["min_stake_base"]:
                    raise ChainError("BELOW_MIN", "deposit is below the minimum")
                mine = next((x for x in positions if x["wallet"] == wallet), None)
                if mine is None and len(positions) >= p["max_participants"]:
                    raise ChainError("MARKET_FULL", "no room for another participant")
                if mine is not None and mine["side"] != side:
                    raise ChainError("ALREADY_OTHER_SIDE", "this wallet already holds the other side")
                if (mine["amount_base"] if mine else 0) + amount_base > p["max_stake_base"]:
                    raise ChainError("ABOVE_MAX", "total position would exceed the maximum")
                await cur.execute(
                    "insert into mock_chain_positions (bet_id, wallet, side, amount_base) values (%s, %s, %s, %s) "
                    "on conflict (bet_id, wallet) do update set amount_base = mock_chain_positions.amount_base + excluded.amount_base",
                    (bet_id, wallet, side, amount_base),
                )
                await cur.execute(
                    "update mock_chain_bets set vault_base = vault_base + %s where bet_id = %s", (amount_base, bet_id)
                )
                await cur.execute(POS_SELECT + " where bet_id = %s and wallet = %s", (bet_id, wallet))
                return Position(**{**(await cur.fetchone()), "claimed": False, "payout_base": 0, "closed": False})  # type: ignore[dict-item]

    async def _transition(self, bet_id: str, allowed: tuple[str, ...], apply) -> ChainBet:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                row, positions = await self._load(cur, bet_id, lock=True)
                if row["state"] not in allowed:
                    raise ChainError("WRONG_STATE", f"market is {row['state']}")
                await apply(cur, row, positions)
                row, positions = await self._load(cur, bet_id)
                return _bet(row, positions)

    def _past_refund_deadline(self, row: dict) -> bool:
        refund_after = _when(row["params"], "refund_after")
        return refund_after is not None and self.now() >= refund_after

    async def lock(self, bet_id: str) -> ChainBet:
        async def apply(cur, row, positions):
            if self._past_refund_deadline(row):
                raise ChainError("PAST_REFUND_DEADLINE", "the refund deadline has passed")
            totals = [0, 0]
            for p in positions:
                totals[p["side"]] += p["amount_base"]
            if totals[0] == 0 or totals[1] == 0 or len(positions) < 2:
                raise ChainError("BOTH_SIDES_REQUIRED", "both sides need money and at least 2 participants")
            await cur.execute("update mock_chain_bets set state = 'locked' where bet_id = %s", (bet_id,))

        return await self._transition(bet_id, ("open",), apply)

    async def settle(self, bet_id: str, winning_side: int) -> ChainBet:
        async def apply(cur, row, positions):
            if self._past_refund_deadline(row):
                raise ChainError("PAST_REFUND_DEADLINE", "the refund deadline has passed")
            if isinstance(winning_side, bool) or winning_side not in (0, 1):
                raise ChainError("INVALID_SIDE", "winning side must be 0 or 1")
            totals = [0, 0]
            for p in positions:
                totals[p["side"]] += p["amount_base"]
            s = settle_amounts((totals[0], totals[1]), winning_side, row["params"]["fee_bps"])
            await cur.execute(
                "update mock_chain_bets set state = 'settled', winning_side = %s, fee_paid_base = %s, "
                "distributable_base = %s, vault_base = vault_base - %s, treasury_balance_base = treasury_balance_base + %s "
                "where bet_id = %s",
                (winning_side, s.fee, s.distributable, s.fee, s.fee, bet_id),
            )

        return await self._transition(bet_id, ("locked",), apply)

    async def refund_all(self, bet_id: str) -> ChainBet:
        async def apply(cur, row, positions):
            await cur.execute("update mock_chain_bets set state = 'refunded' where bet_id = %s", (bet_id,))

        return await self._transition(bet_id, ("open", "locked"), apply)

    async def emergency_refund(self, bet_id: str) -> ChainBet:
        """Mock only: the contract lets anyone refund an Open or Locked bet at or after refund_after."""
        async def apply(cur, row, positions):
            refund_after = _when(row["params"], "refund_after")
            if refund_after is None or self.now() < refund_after:
                raise ChainError("REFUND_TOO_EARLY", "the refund deadline has not been reached")
            await cur.execute("update mock_chain_bets set state = 'refunded' where bet_id = %s", (bet_id,))

        return await self._transition(bet_id, ("open", "locked"), apply)

    async def claim(self, bet_id: str, wallet: str) -> Claim:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                row, positions = await self._load(cur, bet_id, lock=True)
                if row["state"] not in ("settled", "refunded"):
                    raise ChainError("WRONG_STATE", f"market is {row['state']}")
                mine = next((x for x in positions if x["wallet"] == wallet and not x["closed"]), None)
                if mine is None:  # never held, or already claimed or closed: the position account does not exist
                    raise ChainError("ALREADY_CLAIMED", "no open position for this wallet")
                if row["state"] == "refunded":
                    payout, kind = mine["amount_base"], "refund"
                else:
                    if mine["side"] != row["winning_side"]:
                        raise ChainError("NOT_A_WINNER", "this position is on the losing side")
                    winning_total = sum(p["amount_base"] for p in positions if p["side"] == row["winning_side"])
                    payout = compute_payout(mine["amount_base"], row["distributable_base"], winning_total)
                    kind = "winnings"
                await cur.execute(
                    "update mock_chain_positions set claimed = true, closed = true, payout_base = %s "
                    "where bet_id = %s and wallet = %s",
                    (payout, bet_id, wallet),
                )
                await cur.execute(
                    "update mock_chain_bets set vault_base = vault_base - %s where bet_id = %s", (payout, bet_id)
                )
                return Claim(bet_id, wallet, payout, kind, f"mock:{uuid.uuid4()}")

    async def close_loser_position(self, bet_id: str, wallet: str) -> None:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                row, positions = await self._load(cur, bet_id, lock=True)
                mine = next((x for x in positions if x["wallet"] == wallet and not x["closed"]), None)
                if mine is None:
                    return  # already closed: nothing to do
                if row["state"] != "settled":
                    raise ChainError("WRONG_STATE", f"market is {row['state']}")
                if mine["side"] == row["winning_side"]:
                    raise ChainError("WRONG_STATE", "a winning position is claimed, not closed")
                await cur.execute(
                    "update mock_chain_positions set closed = true where bet_id = %s and wallet = %s", (bet_id, wallet)
                )

    async def close_bet(self, bet_id: str) -> None:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                try:
                    row, positions = await self._load(cur, bet_id, lock=True, allow_closed=True)
                except ChainError:
                    return  # no such bet: already gone
                if row["closed"]:
                    return
                if row["state"] not in ("settled", "refunded"):
                    raise ChainError("WRONG_STATE", f"market is {row['state']}")
                if any(not p["closed"] for p in positions):
                    raise ChainError("WRONG_STATE", "positions are still open")
                await cur.execute(
                    "update mock_chain_bets set closed = true, treasury_balance_base = treasury_balance_base + vault_base, "
                    "vault_base = 0 where bet_id = %s",
                    (bet_id,),
                )
