"""The rehearsal driver's refusal rules and its expected-money helpers (pure functions, no network, no database)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rehearsal  # noqa: E402

OK_SETTINGS = SimpleNamespace(game_provider="simulated", chain_mode="real")
DEVNET = {"ok": True, "cluster": "devnet"}


def test_runs_only_on_simulated_provider_real_chain_and_devnet():
    assert rehearsal.refusals(OK_SETTINGS, DEVNET) == []


@pytest.mark.parametrize(
    "settings,health,fragment",
    [
        (SimpleNamespace(game_provider="realtime", chain_mode="real"), DEVNET, "GAME_PROVIDER"),
        (SimpleNamespace(game_provider="simulated", chain_mode="mock"), DEVNET, "CHAIN_MODE"),
        (OK_SETTINGS, {"ok": True, "cluster": "mainnet-beta"}, "cluster"),
        (OK_SETTINGS, {"ok": True}, "cluster"),
        (OK_SETTINGS, None, "did not answer"),
    ],
)
def test_refuses_anything_else(settings, health, fragment):
    reasons = rehearsal.refusals(settings, health)
    assert len(reasons) == 1 and fragment in reasons[0]


def test_every_problem_is_listed():
    assert len(rehearsal.refusals(SimpleNamespace(game_provider="realtime", chain_mode="mock"), None)) == 3


def test_expected_settlement_of_the_rehearsal_market():
    # 10 USDC on Texas Tech (side 0), 5 on Arizona State (side 1), Arizona State wins, 5 percent of the losing side
    e = rehearsal.expected_settlement((10_000_000, 5_000_000), 1, 500, {"w": 5_000_000})
    assert e["fee"] == 500_000
    assert e["distributable"] == 14_500_000
    assert e["payouts"] == {"w": 14_500_000}
    assert e["dust"] == 0


def test_expected_settlement_rounding_dust_stays_in_the_vault():
    e = rehearsal.expected_settlement((7_000_000, 3_000_001), 1, 500, {"a": 1_000_000, "b": 2_000_001})
    assert sum(e["payouts"].values()) + e["dust"] == e["distributable"]
    assert 0 <= e["dust"] < 2


def test_refund_reason_follows_the_participant_count():
    assert rehearsal.refund_reason_for(1, 1) == "too_few_participants"
    assert rehearsal.refund_reason_for(2, 1) == "one_sided"


def test_usd_and_explorer_formatting():
    assert rehearsal.usd(14_500_000) == "14.50" and rehearsal.usd(None) == "n/a"
    assert rehearsal.explorer("abc").endswith("/tx/abc?cluster=devnet")
