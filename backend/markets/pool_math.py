"""Single source of truth for pool money. Pure functions, integers only (floats and bools are rejected).

    fee          = floor(losing_pool * fee_bps / 10000)
    distributable = total_pool - fee
    payout       = floor(stake * distributable / winning_side_total)

The fee exists only when a market settles with a winner and the losing side is non empty. When the losing
pool is zero the fee is zero, so a winner's payout equals their stake. A winner never receives less than
their stake: distributable >= winning_total because fee <= losing_pool. Rounding dust stays in the vault.
"""

from __future__ import annotations

from dataclasses import dataclass

BPS = 10_000
MAX_FEE_BPS = 1000


def _ints(*values: object) -> None:
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"money values must be int base units, got {type(v).__name__}")
        if v < 0:
            raise ValueError("money values must not be negative")


def compute_fee(losing_pool: int, fee_bps: int) -> int:
    _ints(losing_pool, fee_bps)
    if fee_bps > MAX_FEE_BPS:
        raise ValueError(f"fee_bps above the hard cap of {MAX_FEE_BPS}")
    return losing_pool * fee_bps // BPS


def compute_distributable(total_pool: int, fee: int) -> int:
    _ints(total_pool, fee)
    if fee > total_pool:
        raise ValueError("fee cannot exceed the total pool")
    return total_pool - fee


def compute_payout(stake: int, distributable: int, winning_total: int) -> int:
    """Payout for one winning position. A zero winning side has no winners, so nothing to pay."""
    _ints(stake, distributable, winning_total)
    if winning_total == 0:
        return 0
    if stake > winning_total:
        raise ValueError("stake cannot exceed the winning side total")
    return stake * distributable // winning_total


@dataclass(frozen=True)
class Settlement:
    winning_side: int
    winning_total: int
    losing_total: int
    fee: int
    distributable: int


def settle_amounts(side_totals: tuple[int, int], winning_side: int, fee_bps: int) -> Settlement:
    if winning_side not in (0, 1):
        raise ValueError("winning_side must be 0 or 1")
    _ints(*side_totals)
    winning, losing = side_totals[winning_side], side_totals[1 - winning_side]
    fee = compute_fee(losing, fee_bps)
    return Settlement(winning_side, winning, losing, fee, compute_distributable(winning + losing, fee))


def preview_payout_for_deposit(
    side_totals: tuple[int, int], side: int, amount: int, fee_bps: int, existing_stake: int = 0
) -> int:
    """What a wallet would be paid if `side` wins, after adding `amount` to the current pools (and to its own
    existing stake on that side), net of the fee. Zero opposing money means no fee and a payout equal to the stake."""
    if side not in (0, 1):
        raise ValueError("side must be 0 or 1")
    _ints(*side_totals, amount, existing_stake)
    totals = list(side_totals)
    totals[side] += amount
    s = settle_amounts((totals[0], totals[1]), side, fee_bps)
    return compute_payout(existing_stake + amount, s.distributable, s.winning_total)


def implied_multiplier(side_totals: tuple[int, int], side: int, fee_bps: int) -> str | None:
    """Payout per 1 staked if `side` wins, net of fee, as a two decimal string ("1.95"), floored. Integer math.
    None when that side has no money (no multiplier exists yet)."""
    if side not in (0, 1):
        raise ValueError("side must be 0 or 1")
    _ints(*side_totals)
    if side_totals[side] == 0:
        return None
    s = settle_amounts(side_totals, side, fee_bps)
    hundredths = s.distributable * 100 // s.winning_total
    return f"{hundredths // 100}.{hundredths % 100:02d}"
