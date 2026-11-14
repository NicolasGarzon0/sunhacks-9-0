from __future__ import annotations

import httpx

from app.handlers import UNKNOWN_SENDER_REPLY
from app.templates import HELP

from .conftest import ADMIN_SECRET, GATEWAY_SECRET

# Fake 555 numbers only.
ALICE = "+15550001111"
STRANGER = "+15550009999"
GW = {"X-Gateway-Secret": GATEWAY_SECRET}
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def payload(message_id: str = "m-1", handle: str = ALICE, text: str = "hello") -> dict:
    return {"messageId": message_id, "handle": handle, "text": text, "receivedAt": "2026-11-14T12:00:00.000Z"}


async def test_bad_secret_returns_401(client, fake_gateway, repo):
    r = await client.post("/inbound", json=payload(), headers={"X-Gateway-Secret": "wrong"})
    assert r.status_code == 401
    r = await client.post("/inbound", json=payload())
    assert r.status_code == 401
    assert fake_gateway.sends == []
    async with repo.pool.connection() as conn:
        assert (await (await conn.execute("select count(*) from inbound_messages")).fetchone())[0] == 0


async def test_bad_payload_returns_400(client, fake_gateway):
    for bad in ({"messageId": "x"}, {**payload(), "receivedAt": "not a date"}, {**payload(), "handle": "+1 555"}):
        r = await client.post("/inbound", json=bad, headers=GW)
        assert r.status_code == 400, bad
    r = await client.post("/inbound", content=b"not json", headers={**GW, "content-type": "application/json"})
    assert r.status_code == 400
    assert fake_gateway.sends == []


async def test_known_sender_goes_to_bet_flow(client, repo, fake_gateway, extractor):
    await repo.upsert_roster("Alice", "Alice A", ALICE)
    extractor.push("other")
    r = await client.post("/inbound", json=payload(text="hey"), headers=GW)
    assert r.status_code == 200
    assert len(extractor.calls) == 1
    assert fake_gateway.sends == [{"to": ALICE, "text": HELP, "idempotencyKey": "reply:m-1"}]
    assert fake_gateway.headers[0]["x-gateway-secret"] == GATEWAY_SECRET


async def test_unknown_sender_gets_roster_message(client, fake_gateway):
    r = await client.post("/inbound", json=payload(handle=STRANGER), headers=GW)
    assert r.status_code == 200
    assert fake_gateway.sends == [{"to": STRANGER, "text": UNKNOWN_SENDER_REPLY, "idempotencyKey": "reply:m-1"}]


async def test_duplicate_message_id_is_ignored(client, fake_gateway, logs):
    r1 = await client.post("/inbound", json=payload(), headers=GW)
    r2 = await client.post("/inbound", json=payload(), headers=GW)
    assert r1.status_code == r2.status_code == 200
    assert r2.json().get("duplicate") is True
    assert len(fake_gateway.sends) == 1
    assert len(logs.events("inbound.duplicate")) == 1


async def test_processed_at_is_set(client, repo):
    await client.post("/inbound", json=payload(), headers=GW)
    async with repo.pool.connection() as conn:
        row = await (await conn.execute("select processed_at from inbound_messages where message_id='m-1'")).fetchone()
    assert row[0] is not None


async def test_gateway_failure_is_logged_and_does_not_crash(client, repo, fake_gateway, logs):
    fake_gateway.responder = lambda body: httpx.Response(
        502, json={"ok": False, "code": "SEND_FAILED", "detail": f"boom for {body['to']}"}
    )
    r = await client.post("/inbound", json=payload(), headers=GW)
    assert r.status_code == 200
    failed = logs.events("send.failed")
    assert failed and failed[0]["code"] == "SEND_FAILED"
    assert logs.events("handler.reply_failed")
    # Next message still works.
    fake_gateway.responder = lambda body: httpx.Response(200, json={"ok": True, "messageId": "x"})
    r = await client.post("/inbound", json=payload("m-2"), headers=GW)
    assert r.status_code == 200
    assert len(fake_gateway.sends) == 2


async def test_gateway_unreachable_does_not_crash(client, fake_gateway, logs):
    def boom(body):
        raise httpx.ConnectError("connection refused")

    fake_gateway.responder = boom
    r = await client.post("/inbound", json=payload(), headers=GW)
    assert r.status_code == 200
    assert logs.events("send.failed")[0]["code"] == "UNREACHABLE"
    assert not logs.events("handler.error")


async def test_handler_exception_is_logged(client, repo, fake_gateway, logs, monkeypatch):
    async def explode(handle):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(repo, "get_roster_by_handle", explode)
    r = await client.post("/inbound", json=payload(), headers=GW)
    assert r.status_code == 200
    assert logs.events("handler.error")


async def test_db_down_returns_503(client, repo, monkeypatch):
    async def down(*a):
        raise OSError("db down")

    monkeypatch.setattr(repo, "record_inbound", down)
    r = await client.post("/inbound", json=payload(), headers=GW)
    assert r.status_code == 503


async def test_logs_never_contain_full_handle(client, repo, fake_gateway, logs):
    await repo.upsert_roster("alice", "Alice", ALICE)
    await client.post("/inbound", json=payload(), headers=GW)
    await client.post("/admin/roster", json={"name": "bob", "handle": STRANGER}, headers=ADMIN)
    assert logs.lines
    for line in logs.lines:
        assert ALICE not in line and ALICE[1:] not in line
        assert STRANGER not in line and STRANGER[1:] not in line


async def test_admin_roster(client):
    r = await client.post("/admin/roster", json={"name": "Alice", "handle": ALICE})
    assert r.status_code == 401
    r = await client.get("/admin/roster", headers={"X-Admin-Secret": "nope"})
    assert r.status_code == 401

    r = await client.post("/admin/roster", json={"name": "Alice", "display_name": "Ali", "handle": ALICE}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["entry"] == {"name": "alice", "display_name": "Ali", "handle": ALICE}

    # Update by name; default display name = name.
    r = await client.post("/admin/roster", json={"name": "ALICE", "handle": ALICE}, headers=ADMIN)
    assert r.json()["entry"]["display_name"] == "ALICE"

    # Same handle under another name -> 409.
    r = await client.post("/admin/roster", json={"name": "eve", "handle": ALICE}, headers=ADMIN)
    assert r.status_code == 409

    # Whitespace in handle rejected, never reformatted.
    r = await client.post("/admin/roster", json={"name": "bob", "handle": "+1 555 000"}, headers=ADMIN)
    assert r.status_code == 400

    r = await client.get("/admin/roster", headers=ADMIN)
    assert r.json()["roster"] == [{"name": "alice", "display_name": "ALICE", "handle": ALICE}]


async def test_health(client):
    r = await client.get("/health")
    assert r.json() == {"status": "ok", "database": True, "gateway": True}
