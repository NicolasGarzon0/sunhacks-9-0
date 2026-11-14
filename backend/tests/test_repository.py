from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


async def test_record_inbound_dedups(repo):
    assert await repo.record_inbound("a", "+15550001111", NOW) is True
    assert await repo.record_inbound("a", "+15550001111", NOW) is False
    assert await repo.record_inbound("b", "+15550001111", NOW) is True


async def test_roster_crud(repo):
    assert await repo.get_roster_by_handle("+15550001111") is None
    e = await repo.upsert_roster("  Alice ", "Alice", "+15550001111")
    assert e.name == "alice"
    assert (await repo.get_roster_by_name("ALICE")).handle == "+15550001111"
    assert (await repo.get_roster_by_handle("+15550001111")).display_name == "Alice"
    await repo.upsert_roster("alice", "Al", "+15550002222")
    assert await repo.get_roster_by_handle("+15550001111") is None
    await repo.upsert_roster("bob", "Bob", "+15550003333")
    assert [r.name for r in await repo.list_roster()] == ["alice", "bob"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        await repo.upsert_roster("carol", "Carol", "+15550003333")


async def test_rls_enabled(repo):
    async with repo.pool.connection() as conn:
        cur = await conn.execute(
            "select relname, relrowsecurity from pg_class where relname in ('roster','inbound_messages') "
            "and relnamespace = (select oid from pg_namespace where nspname = current_schema())"
        )
        assert dict(await cur.fetchall()) == {"roster": True, "inbound_messages": True}
