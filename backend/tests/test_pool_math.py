from __future__ import annotations

import pytest

from markets.money import base_to_usd, bps_to_percent, usd_to_base
from markets.pool_math import (
    compute_distributable,
    compute_fee,
    compute_payout,
    implied_multiplier,
    preview_payout_for_deposit,
)


def test_money_is_integer_based() -> None:
    assert usd_to_base("12.50") == 1250
    assert base_to_usd(1250) == "12.50"
    assert bps_to_percent(500) == "5%"
    with pytest.raises(TypeError):
        usd_to_base(1.25)


def test_pool_fee_and_payout_floor() -> None:
    assert compute_fee(1000, 500) == 50
    assert compute_distributable(3000, 1000, 500) == 2950
    assert compute_payout(100, 1000, 2950) == 295
    assert implied_multiplier(100, 195) == "1.95"


def test_preview_preserves_stake_and_handles_empty_loser() -> None:
    assert preview_payout_for_deposit(0, 100, 0, 0, 500) == 100
    assert preview_payout_for_deposit(0, 100, 100, 100, 500) >= 100
