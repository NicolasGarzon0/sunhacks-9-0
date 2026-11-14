from __future__ import annotations

import httpx

from app.gateway_client import GatewayClient
from app.logging import Logger


async def test_ok_maps_message_id(gateway, fake_gateway):
    r = await gateway.send_text("+15550001111", "hi", "k1")
    assert r.ok and r.message_id == "out-1"


async def test_ok_with_null_message_id(gateway, fake_gateway):
    fake_gateway.responder = lambda b: httpx.Response(200, json={"ok": True, "messageId": None})
    r = await gateway.send_text("+15550001111", "hi", "k1")
    assert r.ok and r.message_id is None


async def test_failure_codes_returned_not_raised(gateway, fake_gateway):
    for status, code in [(403, "NOT_ALLOWED"), (502, "SEND_FAILED"), (400, "BAD_REQUEST"), (401, "UNAUTHORIZED")]:
        fake_gateway.responder = lambda b, s=status, c=code: httpx.Response(s, json={"ok": False, "code": c, "detail": "d"})
        r = await gateway.send_text("+15550001111", "hi", "k")
        assert not r.ok and r.code == code


async def test_rate_limited_retries_once_after_retry_after(gateway, fake_gateway, sleeps):
    calls = iter([
        httpx.Response(429, json={"ok": False, "code": "RATE_LIMITED", "detail": "slow", "retryAfter": 3}),
        httpx.Response(200, json={"ok": True, "messageId": "m"}),
    ])
    fake_gateway.responder = lambda b: next(calls)
    r = await gateway.send_text("+15550001111", "hi", "k")
    assert r.ok
    assert sleeps == [3.0]
    assert [s["idempotencyKey"] for s in fake_gateway.sends] == ["k", "k"]


async def test_rate_limited_uses_header_and_only_one_retry(gateway, fake_gateway, sleeps):
    fake_gateway.responder = lambda b: httpx.Response(
        429, json={"ok": False, "code": "RATE_LIMITED", "detail": "slow"}, headers={"Retry-After": "2"}
    )
    r = await gateway.send_text("+15550001111", "hi", "k")
    assert not r.ok and r.code == "RATE_LIMITED"
    assert sleeps == [2.0]
    assert len(fake_gateway.sends) == 2


async def test_rate_limited_without_retry_after_does_not_retry(gateway, fake_gateway, sleeps):
    fake_gateway.responder = lambda b: httpx.Response(429, json={"ok": False, "code": "RATE_LIMITED", "detail": "x"})
    r = await gateway.send_text("+15550001111", "hi", "k")
    assert r.code == "RATE_LIMITED" and sleeps == [] and len(fake_gateway.sends) == 1


async def test_non_json_and_transport_errors(gateway, fake_gateway):
    fake_gateway.responder = lambda b: httpx.Response(500, text="<html>")
    assert (await gateway.send_text("+15550001111", "hi", "k")).code == "UNREACHABLE"

    def raise_timeout(b):
        raise httpx.ReadTimeout("timed out")

    fake_gateway.responder = raise_timeout
    assert (await gateway.send_text("+15550001111", "hi", "k")).code == "UNREACHABLE"


async def test_health_false_when_down():
    def down(req):
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as c:
        gw = GatewayClient("http://x", "s", Logger("error"), client=c)
        assert await gw.health() is False
