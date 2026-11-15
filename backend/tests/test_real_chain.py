"""RealChainClient: code mapping, retry rules, request shape. The service is an httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from markets.chain import BetParams, ChainError
from markets.real_chain import RealChainClient

from .helpers import w

BET = "0b6f3c2e-5d1a-4c7e-9a41-2f8d6e7b1a90"
VIEW = {
    "ok": True, "state": "open", "side_totals": [5_000_000, 7_000_000], "participant_count": 2, "positions_open": 2,
    "fee_bps": 500, "fee_paid_base": 0, "distributable_base": 0, "winning_side": None, "vault_base": 12_000_000, "treasury_balance_base": 0,
}


def make(handler, **kw):
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RealChainClient("http://chain.test", "s3cret", client=http, sleep=sleep, **kw), sleeps


async def test_get_bet_maps_fields_and_sends_the_secret_header():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=VIEW)

    c, _ = make(handler)
    cb = await c.get_bet(BET)
    assert (cb.state, cb.side_totals, cb.participant_count, cb.positions_open) == ("open", (5_000_000, 7_000_000), 2, 2)
    assert cb.winning_side is None and cb.vault_base == 12_000_000 and cb.total_pool_base == 12_000_000
    assert seen[0].headers["x-chain-secret"] == "s3cret" and seen[0].url.path == f"/bets/{BET}"


async def test_create_bet_sends_unix_seconds_and_requires_timings():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={**VIEW, "already": False})

    c, _ = make(handler)
    closes = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
    cb = await c.create_bet(BET, BetParams(500, "T", 1_000_000, 20_000_000, 20, funding_closes_at=closes, refund_after=closes + timedelta(hours=18)))
    assert cb.state == "open"
    assert bodies[0] == {
        "bet_id": BET, "funding_closes_at": int(closes.timestamp()), "refund_after": int(closes.timestamp()) + 18 * 3600,
        "fee_bps": 500, "min_stake_base": 1_000_000, "max_stake_base": 20_000_000, "max_participants": 20,
    }  # the treasury is the service's own configuration, never sent
    with pytest.raises(ChainError) as e:
        await c.create_bet(BET, BetParams(500, "T", 1, 2, 2))
    assert e.value.code == "INVALID_PARAMS" and len(bodies) == 1


@pytest.mark.parametrize("service_code,expected", [
    ("BELOW_MIN", "BELOW_MIN"), ("ABOVE_MAX", "ABOVE_MAX"), ("MARKET_FULL", "MARKET_FULL"), ("ALREADY_OTHER_SIDE", "ALREADY_OTHER_SIDE"),
    ("MARKET_NOT_OPEN", "MARKET_NOT_OPEN"), ("WRONG_STATE", "WRONG_STATE"), ("NOT_A_WINNER", "NOT_A_WINNER"), ("NOT_FOUND", "NOT_FOUND"),
    ("FUNDING_CLOSED", "FUNDING_CLOSED"), ("PAST_REFUND_DEADLINE", "PAST_REFUND_DEADLINE"), ("REFUND_TOO_EARLY", "REFUND_TOO_EARLY"),
    ("BOTH_SIDES_REQUIRED", "BOTH_SIDES_REQUIRED"), ("UNTRUSTED_BET", "UNTRUSTED_BET"), ("INVALID_PARAMS", "INVALID_PARAMS"),
    ("BAD_REQUEST", "INVALID_PARAMS"), ("SOMETHING_NEW", "RPC_ERROR"),
])
async def test_service_codes_map_to_typed_errors(service_code, expected):
    status = 404 if service_code == "NOT_FOUND" else 409
    c, _ = make(lambda req: httpx.Response(status, json={"ok": False, "code": service_code, "detail": "why"}), retries=0)
    with pytest.raises(ChainError) as e:
        await c.lock(BET)
    assert e.value.code == expected


async def test_business_errors_are_final_and_never_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(409, json={"ok": False, "code": "WRONG_STATE", "detail": "x"})

    c, sleeps = make(handler)
    with pytest.raises(ChainError):
        await c.settle(BET, 1)
    assert len(calls) == 1 and sleeps == []


async def test_transient_trouble_is_retried_with_backoff_then_succeeds():
    attempts = []

    def handler(req):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("refused")
        if len(attempts) == 2:
            return httpx.Response(503, text="busy")
        if len(attempts) == 3:
            return httpx.Response(502, json={"ok": False, "code": "RPC_ERROR", "detail": "rpc down"})
        return httpx.Response(200, json=VIEW)

    c, sleeps = make(handler, retries=3, backoff=0.5)
    assert (await c.get_bet(BET)).state == "open"
    assert len(attempts) == 4 and sleeps == [0.5, 1.0, 2.0]


async def test_gives_up_with_rpc_error_after_the_retries():
    c, sleeps = make(lambda req: httpx.Response(503, text="busy"), retries=2)
    with pytest.raises(ChainError) as e:
        await c.get_bet(BET)
    assert e.value.code == "RPC_ERROR" and len(sleeps) == 2


async def test_timeouts_are_retried_too_and_end_as_rpc_error():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    c, sleeps = make(handler, retries=1)
    with pytest.raises(ChainError) as e:
        await c.claim(BET, w(1))
    assert e.value.code == "RPC_ERROR" and len(sleeps) == 1


async def test_a_rejected_secret_is_a_clear_rpc_error_not_a_retry_loop():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"ok": False, "code": "UNAUTHORIZED", "detail": "bad"})

    c, _ = make(handler)
    with pytest.raises(ChainError) as e:
        await c.get_bet(BET)
    assert e.value.code == "RPC_ERROR" and "CHAIN_SERVICE_SECRET" in e.value.detail and len(calls) == 1


async def test_claim_already_means_already_claimed_and_a_real_claim_returns_the_payout():
    answers = [
        {"ok": True, "already": True, "payout_base": None, "kind": None},
        {"ok": True, "already": False, "payout_base": 25_350_000, "kind": "winnings", "signature": "5abc"},
    ]
    c, _ = make(lambda req: httpx.Response(200, json=answers.pop(0)))
    with pytest.raises(ChainError) as e:
        await c.claim(BET, w(1))
    assert e.value.code == "ALREADY_CLAIMED"
    claim = await c.claim(BET, w(1))
    assert (claim.payout_base, claim.kind, claim.tx_ref) == (25_350_000, "winnings", "5abc")


async def test_invalid_wallet_and_side_fail_locally_without_a_request():
    calls = []
    c, _ = make(lambda req: calls.append(1) or httpx.Response(200, json=VIEW))
    for coro, code in ((c.claim(BET, "bad"), "INVALID_WALLET"), (c.close_loser_position(BET, "bad"), "INVALID_WALLET"), (c.settle(BET, 2), "INVALID_SIDE")):
        with pytest.raises(ChainError) as e:
            await coro
        assert e.value.code == code
    assert calls == []


async def test_positions_close_calls_and_health():
    def handler(req):
        p = req.url.path
        if p.endswith("/positions"):
            return httpx.Response(200, json={"ok": True, "positions": [{"wallet": w(1), "side": 1, "amount_base": 6_000_000}]})
        if p == "/health":
            return httpx.Response(200, json={"ok": True, "payer_sol": 1.5, "oracle_sol": 0.4, "program_id": "P"})
        return httpx.Response(200, json={"ok": True, "already": False})

    c, _ = make(handler)
    [pos] = await c.get_positions(BET)
    assert (pos.wallet, pos.side, pos.amount_base, pos.closed) == (w(1), 1, 6_000_000, False)
    assert await c.close_loser_position(BET, w(1)) is None and await c.close_bet(BET) is None
    assert await c.health() == {"payer_sol": 1.5, "oracle_sol": 0.4, "program_id": "P"}


async def test_unreadable_responses_are_rpc_errors():
    c, _ = make(lambda req: httpx.Response(200, text="<html>"), retries=0)
    with pytest.raises(ChainError) as e:
        await c.get_bet(BET)
    assert e.value.code == "RPC_ERROR"
