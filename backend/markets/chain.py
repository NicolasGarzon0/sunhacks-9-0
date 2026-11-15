"""ChainClient: the only way the backend talks to the pool contract. The backend never holds user funds or keys.

Side 0 is the away team, side 1 is the home team. All amounts are int USDC base units (6 decimals).
The real deposit is a user signed transaction made from the deposit page, so `deposit` exists on the mock only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

ERROR_CODES = (
    "BELOW_MIN", "ABOVE_MAX", "MARKET_FULL", "ALREADY_OTHER_SIDE", "MARKET_NOT_OPEN", "WRONG_STATE",
    "ALREADY_CLAIMED", "NOT_A_WINNER", "NOT_FOUND", "INVALID_WALLET", "INVALID_SIDE",
    # chunk 3A: the contract and the chain service
    "FUNDING_CLOSED", "PAST_REFUND_DEADLINE", "REFUND_TOO_EARLY", "BOTH_SIDES_REQUIRED", "UNTRUSTED_BET",
    "INVALID_PARAMS", "RPC_ERROR",
    # chunk 3B: sponsored deposits and the airdrop
    "NO_TOKEN_ACCOUNT", "INSUFFICIENT_USDC", "TX_EXPIRED", "TX_REJECTED", "SUBMIT_FAILED", "RATE_LIMITED",
    "AIRDROP_DISABLED", "AIRDROP_LIMIT",
)
STATES = ("open", "locked", "settled", "refunded")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # Solana style public key


class ChainError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        assert code in ERROR_CODES, code
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail or code


def is_valid_wallet(wallet: object) -> bool:
    return isinstance(wallet, str) and _BASE58.match(wallet) is not None


@dataclass(frozen=True)
class BetParams:
    fee_bps: int
    treasury: str
    min_stake_base: int
    max_stake_base: int
    max_participants: int
    # contract timings (chunk 3A). None skips the time rules (older callers and tests).
    funding_closes_at: datetime | None = None
    refund_after: datetime | None = None


@dataclass(frozen=True)
class ChainBet:
    bet_id: str
    state: str  # open | locked | settled | refunded
    side_totals: tuple[int, int]
    participant_count: int
    fee_paid_base: int
    distributable_base: int
    winning_side: int | None
    vault_base: int = 0
    treasury_balance_base: int = 0
    positions_open: int = 0

    @property
    def total_pool_base(self) -> int:
        return self.side_totals[0] + self.side_totals[1]


@dataclass(frozen=True)
class Position:
    bet_id: str
    wallet: str
    side: int
    amount_base: int
    claimed: bool
    payout_base: int
    closed: bool


@dataclass(frozen=True)
class Claim:
    bet_id: str
    wallet: str
    payout_base: int
    kind: str  # "winnings" | "refund"
    tx_ref: str | None = None


class ChainClient(Protocol):
    mode: str

    async def create_bet(self, bet_id: str, params: BetParams) -> ChainBet:
        """Idempotent by bet_id: a second call returns the existing bet unchanged."""
        ...

    async def get_bet(self, bet_id: str) -> ChainBet: ...
    async def get_positions(self, bet_id: str) -> list[Position]: ...
    async def lock(self, bet_id: str) -> ChainBet: ...
    async def settle(self, bet_id: str, winning_side: int) -> ChainBet: ...
    async def refund_all(self, bet_id: str) -> ChainBet: ...
    async def claim(self, bet_id: str, wallet: str) -> Claim:
        """Raises ChainError("ALREADY_CLAIMED") when the position is already gone (claimed or closed)."""
        ...

    async def close_loser_position(self, bet_id: str, wallet: str) -> None:
        """Settled markets only. Closes a losing position (rent back to the payer, no transfer). Idempotent."""
        ...

    async def close_bet(self, bet_id: str) -> None:
        """Settled or refunded with every position closed. Sweeps dust, closes the accounts. Idempotent."""
        ...

    async def health(self) -> dict | None:
        """Payer and oracle balances for the worker's SOL alert. None when the client has nothing to report."""
        ...


def build_chain_client(settings, pool, now=None, http_client=None) -> ChainClient:
    if settings.chain_mode == "mock":
        from .mock_chain import MockChainClient

        return MockChainClient(pool, now=now)
    from .real_chain import RealChainClient

    return RealChainClient(settings.chain_service_url, settings.chain_service_secret, client=http_client)
