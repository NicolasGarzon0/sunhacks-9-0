from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .chain import BetParams, ChainBet, ChainClient, ChainError, Claim, Position
from .pool_math import settle_amounts

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _wallet(wallet: str) -> str:
    if not isinstance(wallet, str) or not BASE58_RE.fullmatch(wallet):
        raise ChainError("INVALID_WALLET", "wallet must be a base58 address between 32 and 44 characters")
    return wallet


def _row_bet(row: dict) -> ChainBet:
    raw = row["params"]
    closes = raw.get("funding_closes_at")
    if isinstance(closes, str):
        closes = datetime.fromisoformat(closes)
    params = BetParams(
        min_stake_base=raw["min_stake_base"],
        max_stake_base=raw["max_stake_base"],
        max_participants=raw["max_participants"],
        fee_bps=raw["fee_bps"],
        treasury=raw["treasury"],
        funding_closes_at=closes,
    )
    return ChainBet(
        bet_id=str(row["bet_id"]),
        state=row["state"],
        params=params,
        vault_base=row["vault_base"],
        fee_paid_base=row["fee_paid_base"],
        distributable_base=row["distributable_base"],
        winning_side=row["winning_side"],
        treasury_balance_base=row["treasury_balance_base"],
    )


class MockChainClient(ChainClient):
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.pool = pool
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def create_bet(self, bet_id: str, params: BetParams) -> ChainBet:
        payload = asdict(params)
        if payload["funding_closes_at"] is not None:
            payload["funding_closes_at"] = payload["funding_closes_at"].isoformat()
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into mock_chain_bets (bet_id, state, params) values (%s, 'open', %s) "
                "on conflict (bet_id) do nothing",
                (bet_id, Jsonb(payload)),
            )
        return await self.get_bet(bet_id)

    async def get_bet(self, bet_id: str) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select bet_id, state, params, vault_base, fee_paid_base, distributable_base, "
                "winning_side, treasury_balance_base from mock_chain_bets where bet_id = %s",
                (bet_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise ChainError("BET_NOT_FOUND", "unknown bet")
        return _row_bet(dict(row) if hasattr(row, "keys") else {
            "bet_id": row[0], "state": row[1], "params": row[2], "vault_base": row[3],
            "fee_paid_base": row[4], "distributable_base": row[5], "winning_side": row[6],
            "treasury_balance_base": row[7],
        })

    async def list_positions(self, bet_id: str) -> list[Position]:
        await self.get_bet(bet_id)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select wallet, side, amount_base, claimed, payout_base, closed "
                "from mock_chain_positions where bet_id = %s order by created_at, wallet",
                (bet_id,),
            )
            rows = await cur.fetchall()
        return [Position(*row) for row in rows]

    async def deposit(self, bet_id: str, wallet: str, side: int, amount_base: int, now: datetime) -> Position:
        wallet = _wallet(wallet)
        if side not in (0, 1):
            raise ChainError("INVALID_SIDE", "side must be 0 or 1")
        if isinstance(amount_base, bool) or not isinstance(amount_base, int) or amount_base <= 0:
            raise ChainError("INVALID_AMOUNT", "amount must be a positive integer")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "select bet_id, state, params, vault_base, fee_paid_base, distributable_base, "
                    "winning_side, treasury_balance_base from mock_chain_bets where bet_id = %s for update",
                    (bet_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ChainError("BET_NOT_FOUND", "unknown bet")
                bet = _row_bet(dict(row) if hasattr(row, "keys") else {
                    "bet_id": row[0], "state": row[1], "params": row[2], "vault_base": row[3],
                    "fee_paid_base": row[4], "distributable_base": row[5], "winning_side": row[6],
                    "treasury_balance_base": row[7],
                })
                if bet.state != "open":
                    raise ChainError("BET_NOT_OPEN", "market is not open")
                if bet.params.funding_closes_at is not None and now >= bet.params.funding_closes_at:
                    raise ChainError("BET_NOT_OPEN", "funding has closed")
                if amount_base < bet.params.min_stake_base:
                    raise ChainError("INVALID_AMOUNT", "deposit is below the minimum")
                position_cur = await conn.execute(
                    "select wallet, side, amount_base, claimed, payout_base, closed "
                    "from mock_chain_positions where bet_id = %s and wallet = %s for update",
                    (bet_id, wallet),
                )
                existing = await position_cur.fetchone()
                if existing is not None:
                    if existing[1] != side:
                        raise ChainError("ONE_SIDE_ONLY", "wallet may only choose one side")
                    if existing[2] + amount_base > bet.params.max_stake_base:
                        raise ChainError("POSITION_LIMIT", "wallet position exceeds the maximum")
                    await conn.execute(
                        "update mock_chain_positions set amount_base = amount_base + %s, updated_at = now() "
                        "where bet_id = %s and wallet = %s",
                        (amount_base, bet_id, wallet),
                    )
                else:
                    count_cur = await conn.execute(
                        "select count(*) from mock_chain_positions where bet_id = %s", (bet_id,)
                    )
                    if int((await count_cur.fetchone())[0]) >= bet.params.max_participants:
                        raise ChainError("PARTICIPANT_CAP", "participant cap reached")
                    if amount_base > bet.params.max_stake_base:
                        raise ChainError("POSITION_LIMIT", "wallet position exceeds the maximum")
                    await conn.execute(
                        "insert into mock_chain_positions (bet_id, wallet, side, amount_base) "
                        "values (%s, %s, %s, %s)",
                        (bet_id, wallet, side, amount_base),
                    )
                await conn.execute(
                    "update mock_chain_bets set vault_base = vault_base + %s where bet_id = %s",
                    (amount_base, bet_id),
                )
                result = await conn.execute(
                    "select wallet, side, amount_base, claimed, payout_base, closed "
                    "from mock_chain_positions where bet_id = %s and wallet = %s",
                    (bet_id, wallet),
                )
                return Position(*(await result.fetchone()))

    async def _transition(self, bet_id: str, expected: str, target: str) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "update mock_chain_bets set state = %s where bet_id = %s and state = %s",
                (target, bet_id, expected),
            )
            if cur.rowcount == 0:
                bet = await self.get_bet(bet_id)
                if bet.state != target:
                    raise ChainError("BET_NOT_OPEN", f"market is not {expected}")
        return await self.get_bet(bet_id)

    async def lock(self, bet_id: str) -> ChainBet:
        return await self._transition(bet_id, "open", "locked")

    async def settle(self, bet_id: str, winning_side: int) -> ChainBet:
        if winning_side not in (0, 1):
            raise ChainError("INVALID_SIDE", "winning side must be 0 or 1")
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "select bet_id, state, params, vault_base, fee_paid_base, distributable_base, "
                    "winning_side, treasury_balance_base from mock_chain_bets where bet_id = %s for update",
                    (bet_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ChainError("BET_NOT_FOUND", "unknown bet")
                bet = _row_bet(dict(row) if hasattr(row, "keys") else {
                    "bet_id": row[0], "state": row[1], "params": row[2], "vault_base": row[3],
                    "fee_paid_base": row[4], "distributable_base": row[5], "winning_side": row[6],
                    "treasury_balance_base": row[7],
                })
                if bet.state == "settled":
                    return bet
                if bet.state != "locked":
                    raise ChainError("BET_NOT_OPEN", "market must be locked before settlement")
                positions = await conn.execute(
                    "select wallet, side, amount_base from mock_chain_positions where bet_id = %s",
                    (bet_id,),
                )
                settlement = settle_amounts(await positions.fetchall(), winning_side, bet.params.fee_bps)
                await conn.execute(
                    "update mock_chain_bets set state = 'settled', fee_paid_base = %s, "
                    "distributable_base = %s, winning_side = %s, treasury_balance_base = %s where bet_id = %s",
                    (settlement.fee, settlement.distributable, winning_side, settlement.fee, bet_id),
                )
                for wallet, payout in settlement.payouts.items():
                    await conn.execute(
                        "update mock_chain_positions set payout_base = %s, closed = true, updated_at = now() "
                        "where bet_id = %s and wallet = %s",
                        (payout, bet_id, wallet),
                    )
        return await self.get_bet(bet_id)

    async def refund_all(self, bet_id: str) -> ChainBet:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "update mock_chain_bets set state = 'refunded' where bet_id = %s and state in ('open', 'locked')",
                (bet_id,),
            )
            if cur.rowcount == 0:
                bet = await self.get_bet(bet_id)
                if bet.state != "refunded":
                    raise ChainError("BET_NOT_OPEN", "market cannot be refunded")
        return await self.get_bet(bet_id)

    async def claim(self, bet_id: str, wallet: str) -> Claim:
        wallet = _wallet(wallet)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                bet = await self.get_bet(bet_id)
                cur = await conn.execute(
                    "select side, amount_base, claimed, payout_base from mock_chain_positions "
                    "where bet_id = %s and wallet = %s for update",
                    (bet_id, wallet),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ChainError("NOT_A_WINNER", "wallet has no position")
                if row[2]:
                    raise ChainError("ALREADY_CLAIMED", "position was already claimed")
                if bet.state == "settled":
                    if row[0] != bet.winning_side:
                        raise ChainError("NOT_A_WINNER", "wallet is not on the winning side")
                    amount = row[3]
                elif bet.state == "refunded":
                    amount = row[1]
                else:
                    raise ChainError("NOT_A_WINNER", "market is not claimable")
                await conn.execute(
                    "update mock_chain_positions set claimed = true, updated_at = now() "
                    "where bet_id = %s and wallet = %s",
                    (bet_id, wallet),
                )
                return Claim(wallet, amount)
