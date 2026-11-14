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


# --- markets (migration 0005) ---

async def test_one_active_market_per_proposer_and_game_but_failed_ones_do_not_block(repo, provider, market):
    [game] = await provider.find_candidates("cardinals", None, None)
    snap = market._snapshot(game)
    a, created = await repo.create_or_get_bet("+15550001111", game.to_json(), snap)
    b, created2 = await repo.create_or_get_bet("+15550001111", game.to_json(), snap)
    assert created and not created2 and a.id == b.id
    assert await repo.set_bet_open(a.id, a.id, game.kickoff_utc)  # open still counts as active
    assert (await repo.create_or_get_bet("+15550001111", game.to_json(), snap))[1] is False
    assert (await repo.create_or_get_bet("+15550002222", game.to_json(), snap))[1] is True  # other proposer
    assert await repo.fail_bet(a.id, "x")
    c, created3 = await repo.create_or_get_bet("+15550001111", game.to_json(), snap)
    assert created3 and c.id != a.id


async def test_status_changes_are_conditional(repo, provider, market):
    [game] = await provider.find_candidates("cardinals", None, None)
    bet, _ = await repo.create_or_get_bet("+15550001111", game.to_json(), market._snapshot(game))
    now = game.kickoff_utc
    assert not await repo.lock_bet(bet.id, 5, now)  # only open bets lock
    assert await repo.set_bet_open(bet.id, bet.id, now) and not await repo.set_bet_open(bet.id, bet.id, now)
    assert await repo.lock_bet(bet.id, 5, now) and not await repo.lock_bet(bet.id, 5, now)
    assert not await repo.fail_bet(bet.id, "late")  # locked markets are never marked failed
    assert not await repo.mark_payouts_complete(bet.id, now)  # only settled or refunded
    assert await repo.get_bet("not-a-uuid") is None


async def test_database_rejects_bad_fee_and_status(repo, provider, market):
    import psycopg

    [game] = await provider.find_candidates("cardinals", None, None)
    bet, _ = await repo.create_or_get_bet("+15550001111", game.to_json(), market._snapshot(game))
    async with repo.pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("update bets set fee_bps = 1001 where id = %s", (bet.id,))
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("update bets set status = 'OPEN' where id = %s", (bet.id,))
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("update bets set refund_reason = 'because' where id = %s", (bet.id,))
