from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


ERROR_CODES = (
    "INVALID_WALLET",
    "INVALID_SIDE",
    "INVALID_AMOUNT",
    "BET_NOT_FOUND",
    "BET_NOT_OPEN",
    "ALREADY_PARTICIPANT",
    "PARTICIPANT_CAP",
    "POSITION_LIMIT",
    "ONE_SIDE_ONLY",
    "ALREADY_CLAIMED",
    "NOT_A_WINNER",
)


class ChainError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown chain error code: {code}")
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BetParams:
    min_stake_base: int
    max_stake_base: int
    max_participants: int
    fee_bps: int
    treasury: str
    funding_closes_at: datetime | None = None


@dataclass(frozen=True)
class ChainBet:
    bet_id: str
    state: str
    params: BetParams
    vault_base: int
    fee_paid_base: int
    distributable_base: int
    winning_side: int | None
    treasury_balance_base: int


@dataclass(frozen=True)
class Position:
    wallet: str
    side: int
    amount_base: int
    claimed: bool
    payout_base: int
    closed: bool


@dataclass(frozen=True)
class Claim:
    wallet: str
    amount_base: int


class ChainClient(Protocol):
    async def create_bet(self, bet_id: str, params: BetParams) -> ChainBet: ...
    async def get_bet(self, bet_id: str) -> ChainBet: ...
    async def list_positions(self, bet_id: str) -> list[Position]: ...
    async def deposit(self, bet_id: str, wallet: str, side: int, amount_base: int, now: datetime) -> Position: ...
    async def lock(self, bet_id: str) -> ChainBet: ...
    async def settle(self, bet_id: str, winning_side: int) -> ChainBet: ...
    async def refund_all(self, bet_id: str) -> ChainBet: ...
    async def claim(self, bet_id: str, wallet: str) -> Claim: ...


def build_chain_client(settings, pool, *, now=None) -> ChainClient:
    if settings.chain_mode == "mock":
        from .mock_chain import MockChainClient

        return MockChainClient(pool, now=now)
    raise NotImplementedError("CHAIN_MODE=real is not implemented")
