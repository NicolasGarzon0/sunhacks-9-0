from __future__ import annotations

from dataclasses import dataclass


def _integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def compute_fee(losing_pool: int, fee_bps: int) -> int:
    _integer(losing_pool, "losing_pool")
    _integer(fee_bps, "fee_bps")
    if fee_bps > 10000:
        raise ValueError("fee_bps must be at most 10000")
    return losing_pool * fee_bps // 10000


def compute_distributable(total_pool: int, losing_pool: int, fee_bps: int) -> int:
    _integer(total_pool, "total_pool")
    fee = compute_fee(losing_pool, fee_bps)
    if fee > total_pool:
        raise ValueError("fee exceeds total pool")
    return total_pool - fee


def compute_payout(stake: int, winning_total: int, distributable: int) -> int:
    _integer(stake, "stake")
    _integer(winning_total, "winning_total")
    _integer(distributable, "distributable")
    if winning_total == 0:
        return 0
    return stake * distributable // winning_total


def preview_payout_for_deposit(
    side: int,
    amount: int,
    side0_total: int,
    side1_total: int,
    fee_bps: int,
) -> int:
    _integer(amount, "amount")
    if side not in (0, 1):
        raise ValueError("side must be 0 or 1")
    totals = [side0_total, side1_total]
    totals[side] += amount
    losing = totals[1 - side]
    distributable = compute_distributable(sum(totals), losing, fee_bps)
    return compute_payout(amount, totals[side], distributable)


def implied_multiplier(stake: int, payout: int) -> str:
    _integer(stake, "stake")
    _integer(payout, "payout")
    if stake == 0:
        return "0.00"
    return f"{payout // stake}.{(payout % stake) * 100 // stake:02d}"


@dataclass(frozen=True)
class Settlement:
    fee: int
    distributable: int
    payouts: dict[str, int]


def settle_amounts(
    positions: list[tuple[str, int, int]],
    winning_side: int,
    fee_bps: int,
) -> Settlement:
    if winning_side not in (0, 1):
        raise ValueError("winning_side must be 0 or 1")
    total = sum(amount for _, _, amount in positions)
    losing = sum(amount for _, side, amount in positions if side != winning_side)
    winning = sum(amount for _, side, amount in positions if side == winning_side)
    fee = compute_fee(losing, fee_bps)
    distributable = compute_distributable(total, losing, fee_bps)
    payouts = {
        wallet: compute_payout(amount, winning, distributable)
        for wallet, side, amount in positions
        if side == winning_side
    }
    return Settlement(fee, distributable, payouts)
