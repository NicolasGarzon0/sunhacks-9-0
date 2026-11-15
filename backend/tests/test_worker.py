"""Worker tick: ordering, idempotency, funding close, result confirmation, refunds, the budget guard, the lock."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest

from games.provider import ProviderError
from markets.worker import LOCK_KEY

from .helpers import ALICE, deposits, locked_market, open_bet, sent, w

MIN = timedelta(minutes=1)


async def snapshot(repo, fake_gateway, bet_id):
    bet = await repo.get_bet(bet_id)
    tasks = await repo.payout_tasks(bet_id)
    return (bet, [(t.wallet, t.status, t.amount_base, t.attempts) for t in tasks], len(fake_gateway.sends))


# --- funding ---

async def test_tick_closes_funding_at_the_deadline_and_not_before(worker, market, provider, repo, chain, clock, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    clock.current = bet.funding_closes_at - MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "open" and not sent(fake_gateway, ":locked")
    clock.current = bet.funding_closes_at
    await worker.tick()
    closed = await repo.get_bet(bet.id)
    assert closed.status == "locked" and closed.total_pool_base == 10_000_000
    [msg] = sent(fake_gateway, ":locked")
    assert msg["to"] == ALICE


async def test_deadline_with_one_side_refunds_with_the_chunk_4_text_and_refund_tasks(worker, market, provider, repo, chain, clock, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 0, 7)])
    clock.current = bet.funding_closes_at
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason, done.fee_paid_base) == ("refunded", "one_sided", 0)
    assert sent(fake_gateway, ":refunded") and not sent(fake_gateway, ":refund_result")
    assert {(t.wallet, t.kind, t.amount_base) for t in await repo.payout_tasks(bet.id)} == {
        (w(1), "refund", 5_000_000), (w(2), "refund", 7_000_000)}


async def test_early_close_when_the_market_fills_without_waiting_for_the_deadline(worker, market, provider, repo, chain, fake_gateway):
    market.settings = replace(market.settings, max_participants=2)
    bet = await open_bet(market, provider, repo)
    await chain.deposit(bet.id, w(1), 0, 5_000_000)
    await chain.deposit(bet.id, w(2), 1, 5_000_000)  # a real deposit made outside the API: nothing closed it yet
    assert (await repo.get_bet(bet.id)).status == "open"
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked" and len(sent(fake_gateway, ":locked")) == 1


@pytest.mark.parametrize("status", ["cancelled", "postponed"])
async def test_cancelled_or_postponed_before_close_refunds_an_open_market_with_no_fee(
    worker, market, provider, scripted, repo, chain, fake_gateway, status
):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 9)])
    scripted.statuses[bet.game_id] = status
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason, done.fee_paid_base) == ("refunded", status, 0)
    cb = await chain.get_bet(bet.id)
    assert (cb.state, cb.fee_paid_base, cb.treasury_balance_base) == ("refunded", 0, 0)
    [msg] = sent(fake_gateway, ":refund_result")
    assert f"was {status}" in msg["text"] and "no fee" in msg["text"] and msg["to"] == ALICE
    assert len(await repo.payout_tasks(bet.id)) == 2


async def test_a_real_provider_is_asked_for_status_only_at_the_deadline(make_worker, scripted, market, provider, repo, chain, clock):
    scripted.name = "realtime"
    worker = make_worker()
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    await worker.tick()
    await worker.tick()
    assert scripted.status_calls == 0
    clock.current = bet.funding_closes_at
    await worker.tick()
    assert scripted.status_calls == 1 and (await repo.get_bet(bet.id)).status == "locked"


# --- idempotency and ordering ---

async def test_a_second_tick_changes_nothing(worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 24, 17)
    await worker.tick()
    first = await snapshot(repo, fake_gateway, bet.id)
    assert first[0].status == "settled" and first[0].payouts_completed_at is not None
    again = await worker.tick()
    assert await snapshot(repo, fake_gateway, bet.id) == first
    assert again == {}  # nothing to do


async def test_tick_summary_and_status_endpoint(worker, client, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 24, 17)
    summary = await worker.tick()
    assert summary["settled"] == 1 and summary["claims_done"] == 2 and summary["completed"] == 1
    assert summary["closes_done"] == 1  # the loser's position is closed too
    status = await worker.status()
    assert status["bets"] == {"settled": 1} and status["payout_tasks"] == {"done": 3} and status["last_tick"]["settled"] == 1


# --- result polling and confirmation ---

async def test_polling_starts_after_the_start_delay_and_respects_the_interval(make_worker, market, provider, scripted, repo, chain, clock):
    worker = make_worker(result_poll_start_hours_after_kickoff=3, result_poll_interval_minutes=10, settle_confirm_wait_minutes=10)
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + timedelta(hours=2, minutes=59)
    await worker.tick()
    assert scripted.result_calls == 0
    clock.current = bet.kickoff_utc + timedelta(hours=3)
    await worker.tick()
    await worker.tick()
    assert scripted.result_calls == 1  # the interval blocks the second read
    clock.current += 10 * MIN
    await worker.tick()
    assert scripted.result_calls == 2


async def test_final_needs_two_matching_reads_apart(make_worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    worker = make_worker(settle_confirm_wait_minutes=10)
    bet = await locked_market(market, provider, repo, chain)
    t0 = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 24, 17)
    clock.current = t0
    await worker.tick()
    seen = await repo.get_bet(bet.id)
    assert seen.status == "locked" and seen.result_first_seen_at == t0 and seen.final_confirmed_at is None
    clock.current = t0 + 5 * MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"
    clock.current = t0 + 10 * MIN
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert done.status == "settled" and done.final_confirmed_at is not None
    assert (done.result_home_score, done.result_away_score, done.winning_side) == (24, 17, 1)  # home team = side 1


async def test_a_changed_score_restarts_the_wait(make_worker, market, provider, scripted, repo, chain, clock):
    worker = make_worker(settle_confirm_wait_minutes=10)
    bet = await locked_market(market, provider, repo, chain)
    t0 = bet.kickoff_utc + 60 * MIN
    clock.current = t0
    scripted.final(bet.game_id, 24, 17)
    await worker.tick()
    clock.current = t0 + 10 * MIN
    scripted.final(bet.game_id, 24, 20)  # corrected
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"
    clock.current = t0 + 19 * MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"
    clock.current = t0 + 20 * MIN
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert done.status == "settled" and done.result_away_score == 20


async def test_a_winner_change_restarts_the_wait(make_worker, market, provider, scripted, repo, chain, clock):
    worker = make_worker(settle_confirm_wait_minutes=10)
    bet = await locked_market(market, provider, repo, chain)
    t0 = bet.kickoff_utc + 60 * MIN
    clock.current = t0
    scripted.final(bet.game_id, 24, 17)
    await worker.tick()
    clock.current = t0 + 10 * MIN
    scripted.final(bet.game_id, 17, 24)  # swapped: a different winner
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"
    clock.current = t0 + 20 * MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).winning_side == 0


async def test_leaving_final_restarts_the_wait(make_worker, market, provider, scripted, repo, chain, clock):
    worker = make_worker(settle_confirm_wait_minutes=10)
    bet = await locked_market(market, provider, repo, chain)
    t0 = bet.kickoff_utc + 60 * MIN
    clock.current = t0
    scripted.final(bet.game_id, 24, 17)
    await worker.tick()
    clock.current = t0 + 5 * MIN
    scripted.set_status(bet.game_id, "in_progress")
    await worker.tick()
    assert (await repo.get_bet(bet.id)).result_first_seen_at is None
    clock.current = t0 + 10 * MIN
    scripted.final(bet.game_id, 24, 17)
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"  # the wait started again at t0 + 10
    clock.current = t0 + 20 * MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "settled"


async def test_side_0_is_the_away_team(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 10, 31)  # away wins
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert done.winning_side == 0 and {t.wallet for t in await repo.payout_tasks(bet.id) if t.kind == "win"} == {w(1)}


# --- refunds ---

async def test_a_tie_refunds_everyone_with_no_fee(make_worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    worker = make_worker(settle_confirm_wait_minutes=5)
    bet = await locked_market(market, provider, repo, chain)
    t0 = bet.kickoff_utc + 60 * MIN
    clock.current = t0
    scripted.final(bet.game_id, 20, 20)
    await worker.tick()
    clock.current = t0 + 5 * MIN
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason, done.fee_paid_base) == ("refunded", "tie", 0)
    assert (await chain.get_bet(bet.id)).treasury_balance_base == 0
    [msg] = sent(fake_gateway, ":refund_result")
    assert "tie" in msg["text"]
    assert done.payouts_completed_at is not None
    assert (await chain.get_bet(bet.id)).vault_base == 0  # every stake went back


@pytest.mark.parametrize("status", ["cancelled", "postponed"])
async def test_cancelled_or_postponed_while_locked_refunds(worker, market, provider, scripted, repo, chain, clock, status):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.set_status(bet.game_id, status)
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason, done.fee_paid_base) == ("refunded", status, 0)


async def test_no_confirmed_final_by_settle_after_refunds_with_no_result(worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    bet = await locked_market(market, provider, repo, chain)
    scripted.set_status(bet.game_id, "in_progress")
    clock.current = bet.settle_after - MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"
    clock.current = bet.settle_after
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason, done.fee_paid_base) == ("refunded", "no_result", 0)
    [msg] = sent(fake_gateway, ":refund_result")
    assert "no final result in time" in msg["text"]


async def test_an_unconfirmed_final_at_settle_after_also_refunds(make_worker, market, provider, scripted, repo, chain, clock):
    worker = make_worker(settle_confirm_wait_minutes=10)
    bet = await locked_market(market, provider, repo, chain)
    scripted.final(bet.game_id, 24, 17)
    clock.current = bet.settle_after
    await worker.tick()  # first read at settle_after, the wait is not over, the deadline has passed
    assert (await repo.get_bet(bet.id)).refund_reason == "no_result"


async def test_budget_exhausted_stops_polling_and_the_settle_after_rule_refunds(worker, market, provider, scripted, repo, chain, clock, logs):
    scripted.fail = ProviderError("sports call budget used up for this month")
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked"
    clock.current = bet.settle_after
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason) == ("refunded", "no_result")
    assert logs.events("worker.result_unavailable")


# --- the single worker lock ---

async def test_a_second_worker_does_nothing_while_the_lock_is_held_elsewhere(worker, make_worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 24, 17)
    async with repo.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            assert await worker.tick() == {"skipped": "locked"}
            assert (await repo.get_bet(bet.id)).status == "locked"
    await worker.tick()  # lock released: the work happens
    assert (await repo.get_bet(bet.id)).status == "settled"


# --- crash repair ---

async def test_settle_on_the_chain_without_the_db_write_is_repaired(worker, market, provider, repo, chain, fake_gateway, scripted):
    bet = await locked_market(market, provider, repo, chain)
    await chain.settle(bet.id, 1)  # crash here: the database still says locked
    assert (await repo.get_bet(bet.id)).status == "locked"
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.winning_side, done.fee_paid_base) == ("settled", 1, 500_000)
    assert len(sent(fake_gateway, ":result")) == 1 and done.payouts_completed_at is not None


async def test_refund_on_the_chain_without_the_db_write_is_repaired(worker, market, provider, repo, chain, fake_gateway):
    bet = await locked_market(market, provider, repo, chain)
    await repo.set_refund_intent(bet.id, "cancelled")
    await chain.refund_all(bet.id)  # crash here
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason) == ("refunded", "cancelled")
    assert len(sent(fake_gateway, ":refund_result")) == 1 and done.payouts_completed_at is not None


async def test_lock_on_the_chain_without_the_db_write_is_repaired(worker, market, provider, repo, chain, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    await chain.lock(bet.id)
    await worker.tick()
    assert (await repo.get_bet(bet.id)).status == "locked" and len(sent(fake_gateway, ":locked")) == 1


async def test_one_failing_bet_does_not_stop_the_others(worker, market, provider, scripted, repo, chain, clock, monkeypatch, logs):
    # the two games are about a day apart; a long refund buffer keeps the earlier one inside its refund deadline
    object.__setattr__(market.settings, "contract_refund_buffer_hours", 48)
    a = await locked_market(market, provider, repo, chain)
    [g2] = await provider.find_candidates("steelers", None, None)
    await market.open_market("+15550002222", g2)
    b = [x for x in await repo.list_bets("+15550002222")][-1]
    await deposits(chain, b.id, [(11, 0, 5), (12, 1, 5)])
    await market.close_funding(b.id)
    clock.current = max(a.kickoff_utc, b.kickoff_utc) + 60 * MIN
    scripted.final(a.game_id, 24, 17)
    scripted.default = None
    real = scripted.get_result

    async def flaky(game_id):
        if game_id == a.game_id:
            raise RuntimeError("boom")
        return await real(game_id)

    monkeypatch.setattr(scripted, "get_result", flaky)
    scripted.final(b.game_id, 3, 10)
    await worker.tick()
    assert (await repo.get_bet(a.id)).status == "locked"
    assert (await repo.get_bet(b.id)).status == "settled"
