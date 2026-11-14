"""Pool money: integer math only. Amounts below are USDC base units (1 USDC = 1_000_000)."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from markets.money import MoneyError, bps_label, format_usd, parse_usd, usd_label
from markets.pool_math import (
    compute_distributable, compute_fee, compute_payout, implied_multiplier, preview_payout_for_deposit, settle_amounts,
)

USDC = 1_000_000


def test_balanced_pool():
    s = settle_amounts((100 * USDC, 100 * USDC), 0, 500)
    assert (s.fee, s.distributable) == (5 * USDC, 195 * USDC)  # 5% of the LOSING side only
    assert compute_payout(100 * USDC, s.distributable, s.winning_total) == 195 * USDC


def test_lopsided_pool():
    # side 1 (100) wins against a 300 losing side: fee 15, the winner takes everything else
    s = settle_amounts((300 * USDC, 100 * USDC), 1, 500)
    assert (s.fee, s.distributable) == (15 * USDC, 385 * USDC)
    assert compute_payout(100 * USDC, s.distributable, s.winning_total) == 385 * USDC
    # the big side winning gets back only a little more than its stake
    s = settle_amounts((300 * USDC, 100 * USDC), 0, 500)
    assert (s.fee, s.distributable) == (5 * USDC, 395 * USDC)
    assert compute_payout(30 * USDC, s.distributable, s.winning_total) == 39_500_000  # 30 of 300, pool 395


def test_zero_losing_pool_charges_no_fee_and_returns_stakes():
    s = settle_amounts((100 * USDC, 0), 0, 500)
    assert s.fee == 0 and s.distributable == 100 * USDC
    assert compute_payout(40 * USDC, s.distributable, s.winning_total) == 40 * USDC


def test_zero_winning_pool_has_no_division_error():
    assert compute_payout(5, 100, 0) == 0
    s = settle_amounts((0, 100 * USDC), 0, 500)
    assert s.winning_total == 0
    assert implied_multiplier((0, 100 * USDC), 0, 500) is None
    assert preview_payout_for_deposit((0, 0), 0, 10 * USDC, 500) == 10 * USDC  # empty market: stake back


def test_rounding_dust_stays_in_the_vault():
    # three equal winners of 1 base unit each against 1 losing unit: distributable 4, each gets floor(4/3) = 1
    s = settle_amounts((3, 1), 0, 0)
    payouts = [compute_payout(1, s.distributable, s.winning_total)] * 3
    assert payouts == [1, 1, 1] and s.distributable - sum(payouts) == 1


def test_winner_never_receives_less_than_stake_and_never_more_than_pool():
    rng = random.Random(7)
    for _ in range(3000):
        n = rng.randint(1, 8)
        stakes = [rng.randint(1, 20 * USDC) for _ in range(n)]
        losing = rng.choice([0, rng.randint(1, 160 * USDC)])
        bps = rng.choice([0, 1, 250, 500, 999, 1000])
        s = settle_amounts((sum(stakes), losing), 0, bps)
        paid = [compute_payout(x, s.distributable, s.winning_total) for x in stakes]
        assert all(p >= x for p, x in zip(paid, stakes))
        assert sum(paid) <= s.distributable
        assert s.distributable - sum(paid) < n  # dust is under one base unit per winner
        assert s.fee == losing * bps // 10_000 and s.fee * 10_000 <= losing * bps


def test_fee_is_exactly_five_percent_of_the_losing_side():
    assert compute_fee(20 * USDC, 500) == USDC
    assert compute_fee(0, 500) == 0
    assert compute_fee(19, 500) == 0  # floors, never rounds up
    assert compute_distributable(100, 5) == 95


def test_fee_cap():
    assert compute_fee(1000, 1000) == 100
    with pytest.raises(ValueError):
        compute_fee(1000, 1001)
    with pytest.raises(ValueError):
        compute_distributable(10, 11)


def test_floats_and_bools_are_rejected():
    for bad in (1.5, True, Decimal("1")):
        with pytest.raises(TypeError):
            compute_fee(bad, 500)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_payout(1, 2.0, 3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compute_fee(-1, 500)


def test_preview_matches_a_real_settlement():
    # existing pools 50 / 30; a new 20 on side 1 wins: pools become 50 / 50, fee 2.5, distributable 97.5, stake 20 of 50
    assert preview_payout_for_deposit((50 * USDC, 30 * USDC), 1, 20 * USDC, 500) == 39 * USDC
    # a top up counts the wallet's whole position on that side
    # pools 50 / 40 after a 10 top up, fee 2.5, distributable 87.5; the wallet's 20 of 40 pays 43.75
    assert preview_payout_for_deposit((50 * USDC, 30 * USDC), 1, 10 * USDC, 500, existing_stake=10 * USDC) == 43_750_000
    with pytest.raises(ValueError):
        preview_payout_for_deposit((0, 0), 2, 1, 500)


def test_implied_multiplier():
    assert implied_multiplier((100 * USDC, 100 * USDC), 0, 500) == "1.95"
    assert implied_multiplier((100 * USDC, 0), 0, 500) == "1.00"
    assert implied_multiplier((300 * USDC, 100 * USDC), 1, 500) == "3.85"
    assert implied_multiplier((0, 0), 1, 500) is None


def test_money_boundary_conversions():
    assert parse_usd("12.50") == 12_500_000 and parse_usd(20) == 20_000_000 and parse_usd(Decimal("0.000001")) == 1
    assert format_usd(12_500_000) == "12.50" and format_usd(1_234_567) == "1.234567" and format_usd(0) == "0.00"
    assert usd_label(20_000_000) == "$20" and usd_label(12_500_000) == "$12.50"
    assert bps_label(500) == "5%" and bps_label(250) == "2.5%" and bps_label(125) == "1.25%"
    for bad in (5.5, True, "abc", "-1", "1.0000001", "NaN", "Infinity", ""):
        with pytest.raises(MoneyError):
            parse_usd(bad)
