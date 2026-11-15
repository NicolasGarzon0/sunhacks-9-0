"""MockChainClient: the pool contract's rules over the mock_chain_* tables (own SQL; drop the tables to remove it).

State lives in Postgres so a restart keeps it. Every mutation locks the bet row (select ... for update), so the
participant cap and per wallet max hold under concurrent deposits.
"""

from __future__ import annotations

import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .chain import BetParams, ChainBet, ChainError, Claim, Position, is_valid_wallet
from .pool_math import compute_payout, settle_amounts

BET_SELECT = "select bet_id::text as bet_id, state, params, vault_base, fee_paid_base, distributable_base, winning_side, treasury_balance_base from mock_chain_bets"
POS_SELECT = "select bet_id::text as bet_id, wallet, side, amount_base, claimed, payout_base, closed from mock_chain_positions"


def _bet(row: dict, positions: list[dict]) -> ChainBet:
    totals = [0, 0]
    for p in positions:
        totals[p["side"]] += p["amount_base"]
    return ChainBet(
        bet_id=row["bet_id"], state=row["state"], side_totals=(totals[0], totals[1]),
        participant_count=len(positions), fee_paid_base=row["fee_paid_base"],
        distributable_base=row["distributable_base"], winning_side=row["winning_side"],
        vault_base=row["vault_base"], treasury_balance_base=row["treasury_balance_base"],
    )


class MockChainClient:
    mode = "mock"

    def __init__(self, pool) -> None:
        self.pool = pool

    # --- reads ---

    async def _load(self, cur, bet_id: str, lock: bool = False) -> tuple[dict, list[dict]]:
        try:
            uuid.UUID(str(bet_id))
        except ValueError:
            raise ChainError("NOT_FOUND", "unknown bet") from None
        await cur.execute(BET_SELECT + " where bet_id = %s" + (" for update" if lock else ""), (bet_id,))
        row = await cur.fetchone()
        if row is None:
            raise ChainError("NOT_FOUND", "unknown bet")
        await cur.execute(POS_SELECT + " where bet_id = %s order by created_at, wallet", (bet_id,))
        return row, list(await cur.fetchall())

    async def get_bet(self, bet_id: str) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            return _bet(*await self._load(cur, bet_id))

    async def get_positions(self, bet_id: str) -> list[Position]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            _, positions = await self._load(cur, bet_id)
            return [Position(**p) for p in positions]

    # --- writes ---

    async def create_bet(self, bet_id: str, params: BetParams) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "insert into mock_chain_bets (bet_id, state, params) values (%s, 'open', %s) "
                "on conflict (bet_id) do nothing",
                (bet_id, Jsonb(params.__dict__)),
            )
            return _bet(*await self._load(cur, bet_id))

    async def deposit(self, bet_id: str, wallet: str, side: int, amount_base: int) -> Position:
        """Mock only. Mirrors what the contract enforces on a user signed deposit."""
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        if isinstance(side, bool) or side not in (0, 1):
            raise ChainError("INVALID_SIDE", "side must be 0 or 1")
        if isinstance(amount_base, bool) or not isinstance(amount_base, int) or amount_base <= 0:
            raise ChainError("BELOW_MIN", "amount must be a positive integer of base units")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                row, positions = await self._load(cur, bet_id, lock=True)
                p = row["params"]
                if row["state"] != "open":
                    raise ChainError("MARKET_NOT_OPEN", f"market is {row['state']}")
                if amount_base < p["min_stake_base"]:
                    raise ChainError("BELOW_MIN", "deposit is below the minimum")
                mine = next((x for x in positions if x["wallet"] == wallet), None)
                if mine is not None and mine["side"] != side:
                    raise ChainError("ALREADY_OTHER_SIDE", "this wallet already holds the other side")
                if (mine["amount_base"] if mine else 0) + amount_base > p["max_stake_base"]:
                    raise ChainError("ABOVE_MAX", "total position would exceed the maximum")
                if mine is None and len(positions) >= p["max_participants"]:
                    raise ChainError("MARKET_FULL", "no room for another participant")
                await cur.execute(
                    "insert into mock_chain_positions (bet_id, wallet, side, amount_base) values (%s, %s, %s, %s) "
                    "on conflict (bet_id, wallet) do update set amount_base = mock_chain_positions.amount_base + excluded.amount_base",
                    (bet_id, wallet, side, amount_base),
                )
                await cur.execute(
                    "update mock_chain_bets set vault_base = vault_base + %s where bet_id = %s", (amount_base, bet_id)
                )
                await cur.execute(POS_SELECT + " where bet_id = %s and wallet = %s", (bet_id, wallet))
                return Position(**(await cur.fetchone()))  # type: ignore[arg-type]

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

    async def lock(self, bet_id: str) -> ChainBet:
        async def apply(cur, row, positions):
            await cur.execute("update mock_chain_bets set state = 'locked' where bet_id = %s", (bet_id,))

        return await self._transition(bet_id, ("open",), apply)

    async def settle(self, bet_id: str, winning_side: int) -> ChainBet:
        if isinstance(winning_side, bool) or winning_side not in (0, 1):
            raise ChainError("INVALID_SIDE", "winning side must be 0 or 1")

        async def apply(cur, row, positions):
            totals = [0, 0]
            for p in positions:
                totals[p["side"]] += p["amount_base"]
            if totals[winning_side] == 0:
                raise ChainError("WRONG_STATE", "winning side has no stake; refund instead")
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

    async def claim(self, bet_id: str, wallet: str) -> Claim:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                row, positions = await self._load(cur, bet_id, lock=True)
                mine = next((x for x in positions if x["wallet"] == wallet), None)
                if mine is None:
                    raise ChainError("NOT_FOUND", "no position for this wallet")
                if row["state"] not in ("settled", "refunded"):
                    raise ChainError("WRONG_STATE", f"market is {row['state']}")
                if mine["claimed"]:
                    raise ChainError("ALREADY_CLAIMED", "this position was already claimed")
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
