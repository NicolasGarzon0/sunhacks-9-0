"""Loser close tasks, close_bet after the link expired, and the payer SOL alert."""

from __future__ import annotations

from datetime import timedelta

import pytest

from markets.chain import ChainError

from .conftest import ADMIN_SECRET
from .helpers import locked_market, w
from .test_close_funding import ADMIN

MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
SEC = timedelta(seconds=1)


async def settled(worker, market, provider, scripted, repo, chain, clock, winner_home=True):
    bet = await locked_market(market, provider, repo, chain)  # w1 side 0 (loser), w2 + w3 side 1
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14) if winner_home else scripted.final(bet.game_id, 10, 31)
    return bet


async def tasks(repo, bet_id, kind=None):
    return [t for t in await repo.payout_tasks(bet_id) if kind is None or t.kind == kind]


# ---------- loser close tasks ----------

async def test_a_close_loser_task_is_created_for_every_losing_position(worker, market, provider, scripted, repo, chain, clock):
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)
    summary = await worker.tick()
    [close] = await tasks(repo, bet.id, "close_loser")
    assert (close.wallet, close.amount_base, close.status) == (w(1), 0, "done") and summary["closes_done"] == 1
    assert {t.wallet for t in await tasks(repo, bet.id, "win")} == {w(2), w(3)}


async def test_close_tasks_do_not_block_payouts_completed_at(make_worker, market, provider, scripted, repo, chain, clock, monkeypatch):
    worker = make_worker(claim_backoff_seconds=30)
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)

    async def broken(bet_id, wallet):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(chain, "close_loser_position", broken)
    await worker.tick()
    [close] = await tasks(repo, bet.id, "close_loser")
    assert close.status == "pending" and close.attempts == 1 and close.next_attempt_at == clock.current + 30 * SEC
    assert (await repo.get_bet(bet.id)).payouts_completed_at == clock.current  # the claims are all that count


async def test_close_tasks_retry_with_backoff_and_give_up_without_a_text(make_worker, market, provider, scripted, repo, chain, clock, monkeypatch, fake_gateway, logs):
    worker = make_worker(claim_backoff_seconds=30, claim_max_attempts=2)
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)

    async def broken(bet_id, wallet):
        raise ChainError("RPC_ERROR", "down")

    monkeypatch.setattr(chain, "close_loser_position", broken)
    t0 = clock.current
    await worker.tick()
    clock.current = t0 + 30 * SEC
    await worker.tick()
    [close] = await tasks(repo, bet.id, "close_loser")
    assert close.status == "failed" and close.attempts == 2
    assert not [s for s in fake_gateway.sends if s["idempotencyKey"].endswith(":payouts_delayed")]  # rent only: no text
    assert logs.events("worker.claim_gave_up")


async def test_a_permanent_close_error_fails_at_once(worker, market, provider, scripted, repo, chain, clock, monkeypatch):
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)

    async def wrong(bet_id, wallet):
        raise ChainError("WRONG_STATE", "x")

    monkeypatch.setattr(chain, "close_loser_position", wrong)
    await worker.tick()
    [close] = await tasks(repo, bet.id, "close_loser")
    assert (close.status, close.attempts) == ("failed", 1)


async def test_winners_who_are_sides_are_not_given_close_tasks_and_refunds_have_none(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.set_status(bet.game_id, "cancelled")
    await worker.tick()
    assert {t.kind for t in await tasks(repo, bet.id)} == {"refund"}


# ---------- close_bet ----------

async def test_close_bet_runs_once_after_the_link_has_expired(worker, market, provider, scripted, repo, chain, clock, logs):
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert done.payouts_completed_at is not None and done.chain_closed_at is None
    expires = market.signer.expires_at(done)
    clock.current = expires - SEC
    summary = await worker.tick()
    assert "closed_bets" not in summary and (await chain.get_bet(bet.id)).state == "settled"  # the link is still open
    clock.current = expires
    summary = await worker.tick()
    assert summary["closed_bets"] == 1
    assert (await repo.get_bet(bet.id)).chain_closed_at == clock.current
    with pytest.raises(ChainError) as e:
        await chain.get_bet(bet.id)  # the Bet account is gone
    assert e.value.code == "NOT_FOUND"
    assert logs.events("worker.bet_closed")
    assert "closed_bets" not in await worker.tick()  # once only


async def test_close_bet_sweeps_the_dust_to_the_treasury(worker, market, provider, scripted, repo, chain, clock):
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)
    await worker.tick()
    before = await chain.get_bet(bet.id)
    clock.current += 3 * HOUR
    await worker.tick()
    async with repo.pool.connection() as conn:
        row = await (await conn.execute("select treasury_balance_base, vault_base, closed from mock_chain_bets where bet_id = %s", (bet.id,))).fetchone()
    assert row[2] is True and row[1] == 0 and row[0] == before.treasury_balance_base + before.vault_base


async def test_close_bet_waits_while_any_task_is_pending(make_worker, market, provider, scripted, repo, chain, clock, monkeypatch):
    worker = make_worker(claim_backoff_seconds=30, claim_max_attempts=8)
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)

    async def broken(bet_id, wallet):
        raise RuntimeError("down")

    monkeypatch.setattr(chain, "close_loser_position", broken)
    await worker.tick()
    clock.current += 3 * HOUR  # past the link grace, but the loser's position is still open
    summary = await worker.tick()
    assert "closed_bets" not in summary and (await repo.get_bet(bet.id)).chain_closed_at is None


async def test_close_bet_failure_is_logged_and_retried_later(worker, market, provider, scripted, repo, chain, clock, monkeypatch, logs):
    bet = await settled(worker, market, provider, scripted, repo, chain, clock)
    await worker.tick()
    real = chain.close_bet

    async def broken(bet_id):
        raise ChainError("RPC_ERROR", "down")

    monkeypatch.setattr(chain, "close_bet", broken)
    clock.current += 3 * HOUR
    t0 = clock.current
    summary = await worker.tick()
    failed = await repo.get_bet(bet.id)
    assert summary["close_failed"] == 1 and failed.close_bet_attempts == 1 and failed.close_bet_last_error == "RPC_ERROR"
    assert failed.close_bet_next_attempt_at == t0 + 60 * SEC and failed.chain_closed_at is None
    assert logs.events("worker.close_bet_failed")
    clock.current = t0 + 59 * SEC
    assert "close_failed" not in await worker.tick()  # not due yet
    monkeypatch.setattr(chain, "close_bet", real)
    clock.current = t0 + 60 * SEC
    assert (await worker.tick())["closed_bets"] == 1
    assert (await repo.get_bet(bet.id)).chain_closed_at is not None


async def test_a_refunded_market_is_closed_after_the_link_expires(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.set_status(bet.game_id, "cancelled")
    await worker.tick()
    clock.current += 3 * HOUR
    assert (await worker.tick())["closed_bets"] == 1


# ---------- payer SOL alert ----------

class Health:
    def __init__(self, payer):
        self.payer, self.calls = payer, 0

    async def __call__(self):
        self.calls += 1
        return {"payer_sol": self.payer, "oracle_sol": 0.4}


async def test_low_payer_sol_is_logged_and_shown_in_the_worker_status(make_worker, market, client, chain, monkeypatch, logs):
    worker = make_worker(payer_min_sol_alert=0.3)
    monkeypatch.setattr(chain, "health", Health(0.1))
    summary = await worker.tick()
    assert summary["payer_sol_low"] == 1
    [event] = logs.events("worker.payer_sol_low")
    assert event["payerSol"] == 0.1 and event["level"] == "error"
    status = await worker.status()
    assert status["chain_health"]["payer_sol_low"] is True and status["chain_health"]["payer_sol"] == 0.1


async def test_the_health_check_runs_every_few_ticks_and_a_healthy_balance_raises_nothing(make_worker, chain, monkeypatch, logs):
    worker = make_worker(chain_health_every_ticks=3, payer_min_sol_alert=0.3)
    health = Health(1.5)
    monkeypatch.setattr(chain, "health", health)
    for _ in range(7):
        await worker.tick()
    assert health.calls == 3  # ticks 1, 4 and 7
    assert not logs.events("worker.payer_sol_low") and (await worker.status())["chain_health"]["payer_sol_low"] is False


async def test_the_admin_status_endpoint_includes_chain_health(app, client, monkeypatch, chain):
    monkeypatch.setattr(chain, "health", Health(0.05))
    await client.post("/admin/worker/tick", headers=ADMIN)
    body = (await client.get("/admin/worker/status", headers=ADMIN)).json()
    assert body["chain_health"]["payer_sol_low"] is True


async def test_an_unreachable_chain_service_is_recorded_not_fatal(make_worker, chain, monkeypatch, logs):
    async def down():
        raise ChainError("RPC_ERROR", "unreachable")

    monkeypatch.setattr(chain, "health", down)
    worker = make_worker()
    summary = await worker.tick()
    assert summary["health_errors"] == 1
    assert (await worker.status())["chain_health"]["ok"] is False
