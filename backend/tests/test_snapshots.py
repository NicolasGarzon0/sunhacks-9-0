"""position_snapshots (migration 0007): taken when funding closes, followed through claims, and the source of every
view of a locked, settled or refunded market (on chain a claimed or closed position account no longer exists)."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from markets.chain import ChainError

from .conftest import ADMIN_SECRET
from .helpers import deposit_known, USDC, deposits, locked_market, open_bet, w
from .test_close_funding import ADMIN

MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


async def snap(repo, bet_id):
    return {s.wallet: s for s in await repo.snapshots(bet_id)}


async def get(client, token, path="", **params):
    r = await client.get(f"/markets/{token}{path}", params=params)
    return r.status_code, r.json()


# ---------- taken at lock and at refund ----------

async def test_snapshot_is_taken_when_funding_closes_and_lock_happens(market, provider, repo, chain):
    bet = await locked_market(market, provider, repo, chain)  # w1 side 0: 10, w2 side 1: 5, w3 side 1: 20
    s = await snap(repo, bet.id)
    assert {k: (v.side, v.amount_base, v.payout_base, v.status) for k, v in s.items()} == {
        w(1): (0, 10 * USDC, None, "held"), w(2): (1, 5 * USDC, None, "held"), w(3): (1, 20 * USDC, None, "held")}


async def test_a_deposit_that_lands_just_before_the_lock_is_in_the_snapshot(market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 10), (2, 1, 5)])
    real_lock = chain.lock

    async def lock_after_a_late_deposit(bet_id):
        await deposit_known(chain, bet_id, w(3), 1, 7 * USDC)  # slips in after the first snapshot, before the lock
        return await real_lock(bet_id)

    chain.lock = lock_after_a_late_deposit  # type: ignore[method-assign]
    closed = await market.close_funding(bet.id)
    assert closed.status == "locked" and closed.total_pool_base == 22 * USDC
    assert set(await snap(repo, bet.id)) == {w(1), w(2), w(3)}


async def test_refund_at_funding_close_snapshots_the_positions(market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 6), (2, 0, 4)])  # one sided
    closed = await market.close_funding(bet.id)
    assert closed.status == "refunded"
    assert {k: v.amount_base for k, v in (await snap(repo, bet.id)).items()} == {w(1): 6 * USDC, w(2): 4 * USDC}


async def test_refund_market_snapshots_when_none_exist(market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 6), (2, 1, 4)])
    assert await snap(repo, bet.id) == {}  # an open market has no snapshot yet
    refunded = await market.refund_market(bet.id, "cancelled")
    assert refunded.status == "refunded"
    assert set(await snap(repo, bet.id)) == {w(1), w(2)}


async def test_reconcile_path_takes_missing_snapshots_for_a_locked_market(worker, market, provider, repo, chain):
    bet = await locked_market(market, provider, repo, chain)
    async with repo.pool.connection() as conn:
        await conn.execute("delete from position_snapshots where bet_id = %s", (bet.id,))  # a market locked before 0007
    summary = await worker.tick()
    assert summary["snapshots_taken"] == 1
    assert set(await snap(repo, bet.id)) == {w(1), w(2), w(3)}


async def test_snapshot_never_moves_a_row_that_was_already_paid(market, provider, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    async with repo.pool.connection() as conn:
        await conn.execute("update position_snapshots set status = 'paid', payout_base = 99 where bet_id = %s and wallet = %s", (bet.id, w(2)))
    await repo.upsert_snapshots(bet.id, [(w(2), 1, 123)])
    assert (await snap(repo, bet.id))[w(2)].amount_base == 5 * USDC


# ---------- followed through claims and loser closes ----------

async def test_snapshot_status_follows_the_tasks_on_a_settled_market(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)  # home wins: side 1
    await worker.tick()
    s = await snap(repo, bet.id)
    assert (s[w(1)].status, s[w(1)].payout_base) == ("closed_loser", 0)
    assert s[w(2)].status == "paid" and s[w(3)].status == "paid"
    assert s[w(2)].payout_base + s[w(3)].payout_base == (await repo.get_bet(bet.id)).total_pool_base - (await repo.get_bet(bet.id)).fee_paid_base - (
        (await chain.get_bet(bet.id)).vault_base)  # what was paid out is the distributable minus the dust left in the vault


async def test_snapshot_status_is_refunded_on_a_refund(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.set_status(bet.game_id, "cancelled")
    await worker.tick()
    s = await snap(repo, bet.id)
    assert {k: (v.status, v.payout_base) for k, v in s.items()} == {
        w(1): ("refunded", 10 * USDC), w(2): ("refunded", 5 * USDC), w(3): ("refunded", 20 * USDC)}


# ---------- views read the snapshots, not the chain ----------

async def test_views_of_a_finished_market_come_from_snapshots_after_the_positions_are_gone(
    worker, client, market, provider, scripted, repo, chain, clock
):
    bet = await locked_market(market, provider, repo, chain)
    token = market.signer.token(bet.id)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)
    await worker.tick()
    assert await chain.get_positions(bet.id, [w(1), w(2), w(3)]) == []  # every position was claimed or closed on the chain
    code, view = await get(client, token)
    assert code == 200 and view["status"] == "settled" and view["participant_count"] == 3
    assert view["pools"]["side0_base"] == 10 * USDC and view["pools"]["side1_base"] == 25 * USDC
    assert view["winning_side"] == 1 and view["fee_paid_base"] == 500_000 and view["distributable_base"] == 34_500_000
    code, listing = await get(client, token, "/participants")
    assert {p["wallet"]: (p["side"], p["stake_base"]) for p in listing["participants"]} == {w(1): (0, 10 * USDC), w(2): (1, 5 * USDC), w(3): (1, 20 * USDC)}
    code, winner = await get(client, token, "/position", wallet=w(3))
    assert winner["has_position"] and winner["paid"] is True and winner["stake_base"] == 20 * USDC
    assert winner["payout_base"] == 27_600_000  # 20 of 25 over 34.5
    code, loser = await get(client, token, "/position", wallet=w(1))
    assert loser["has_position"] and loser["paid"] is False and loser["payout_base"] == 0 and loser["side"] == 0


async def test_refunded_views_come_from_snapshots(worker, client, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    token = market.signer.token(bet.id)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.set_status(bet.game_id, "cancelled")
    await worker.tick()
    assert await chain.get_positions(bet.id, [w(1), w(2), w(3)]) == []
    code, view = await get(client, token)
    assert view["status"] == "refunded" and view["participant_count"] == 3 and view["total_pool_base"] == 35 * USDC
    code, pos = await get(client, token, "/position", wallet=w(2))
    assert pos["paid"] is True and pos["payout_base"] == 5 * USDC


async def test_a_locked_view_reads_the_snapshot_even_if_the_chain_cannot_be_reached(market, provider, repo, chain, client):
    bet = await locked_market(market, provider, repo, chain)
    token = market.signer.token(bet.id)

    async def broken(*a, **k):
        raise ChainError("RPC_ERROR", "down")

    chain.get_bet = chain.get_positions = broken  # type: ignore[method-assign]
    code, view = await get(client, token)
    assert code == 200 and view["participant_count"] == 3 and view["total_pool_base"] == 35 * USDC


async def test_an_open_market_still_reads_the_chain(market, provider, repo, chain, client):
    bet = await open_bet(market, provider, repo)
    token = market.signer.token(bet.id)
    await deposits(chain, bet.id, [(1, 0, 4)])
    code, view = await get(client, token)
    assert view["status"] == "open" and view["pools"]["side0_base"] == 4 * USDC
    assert await snap(repo, bet.id) == {}


async def test_display_names_stay_in_bet_participants(worker, client, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    token = market.signer.token(bet.id)
    await repo.set_display_name(bet.id, w(2), "Dana")
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)
    await worker.tick()
    code, listing = await get(client, token, "/participants")
    assert {p["wallet"]: p["display_name"] for p in listing["participants"]}[w(2)] == "Dana"


async def test_admin_view_lists_snapshots_and_survives_a_closed_bet(worker, client, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)
    await worker.tick()
    clock.current += 3 * HOUR  # past the link grace
    await worker.tick()  # close_bet
    view = (await client.get(f"/admin/bets/{bet.id}", headers=ADMIN)).json()
    assert view["chain"] is None and len(view["snapshots"]) == 3 and view["bet"]["chain_closed_at"] is not None


# ---------- refund_after and the deadline fallbacks ----------

async def test_open_market_gives_the_chain_funding_close_and_refund_after(market, provider, repo, chain):
    object.__setattr__(market.settings, "contract_refund_buffer_hours", 7)
    bet = await open_bet(market, provider, repo)
    async with repo.pool.connection() as conn:
        row = await (await conn.execute("select params from mock_chain_bets where bet_id = %s", (bet.id,))).fetchone()
    params = row[0]
    from datetime import datetime
    assert datetime.fromisoformat(params["funding_closes_at"]) == bet.funding_closes_at
    assert datetime.fromisoformat(params["refund_after"]) == bet.settle_after + timedelta(hours=7)


async def test_funding_close_after_refund_after_refunds_instead_of_locking(market, provider, repo, chain, clock):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 10), (2, 1, 5)])
    clock.current = bet.settle_after + timedelta(hours=13)  # the worker was down past refund_after
    closed = await market.close_funding(bet.id)
    assert closed.status == "refunded" and closed.refund_reason == "no_result"
    assert set(await snap(repo, bet.id)) == {w(1), w(2)}


async def test_settling_after_refund_after_refunds_everyone(market, provider, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.settle_after + timedelta(hours=13)
    done = await market.settle_market(bet.id, 1)
    assert done.status == "refunded" and done.refund_reason == "no_result" and (await chain.get_bet(bet.id)).state == "refunded"
    assert {t.kind for t in await repo.payout_tasks(bet.id)} == {"refund"}


# ---------- migration 0007 ----------

SQL = (Path(__file__).resolve().parent.parent / "migrations" / "0007_snapshots.sql").read_text()


def test_migration_0007_is_rerunnable_and_locked_down(pg_url):
    schema = f"m7_{uuid.uuid4().hex[:10]}"
    migrations = sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f'create schema "{schema}"')
        conn.execute(f'set search_path to "{schema}"')
        try:
            for f in migrations:
                conn.execute(f.read_text())
            conn.execute(SQL)
            conn.execute(SQL)  # a second run changes nothing and does not fail
            rls = conn.execute(
                "select relrowsecurity from pg_class where relname = 'position_snapshots' and relnamespace = (select oid from pg_namespace where nspname = %s)", (schema,)
            ).fetchone()
            assert rls[0] is True
            policies = conn.execute("select count(*) from pg_policies where schemaname = %s and tablename = 'position_snapshots'", (schema,)).fetchone()
            assert policies[0] == 0
            kinds = conn.execute("select pg_get_constraintdef(oid) from pg_constraint where conname = 'payout_tasks_kind_check'").fetchone()[0]
            assert "close_loser" in kinds
            cols = {r[0] for r in conn.execute("select column_name from information_schema.columns where table_schema = %s and table_name = 'bets'", (schema,)).fetchall()}
            assert {"chain_closed_at", "close_bet_attempts", "close_bet_next_attempt_at", "close_bet_last_error"} <= cols
            uniq = conn.execute("select count(*) from pg_constraint where conrelid = 'position_snapshots'::regclass and contype = 'u'").fetchone()[0]
            assert uniq == 1  # unique (bet_id, wallet)
            with pytest.raises(psycopg.errors.CheckViolation):
                bet = uuid.uuid4()
                conn.execute("insert into bets (id, proposer_handle, game_id, league, home_id, home_name, away_id, away_name, kickoff_utc, status) "
                             "values (%s, 'h', 'g', 'nfl', 'a', 'A', 'b', 'B', now(), 'open')", (bet,))
                conn.execute("insert into position_snapshots (bet_id, wallet, side, amount_base, status) values (%s, 'w', 0, 1, 'bogus')", (bet,))
        finally:
            conn.execute(f'drop schema "{schema}" cascade')
