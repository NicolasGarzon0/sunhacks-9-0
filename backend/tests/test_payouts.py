"""Settlement amounts and payout tasks: fee to the treasury, winners only, retries, completion, manual retry."""

from __future__ import annotations

from datetime import timedelta

from markets.chain import ChainError
from markets.pool_math import compute_payout, settle_amounts

from .conftest import ADMIN_SECRET
from .helpers import locked_market, sent, w

MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}
# Awkward base units so the division leaves dust: side 0 holds 10.000001, side 1 holds 3.333333 + 3.333335.
DUST_ROWS = [(1, 0, 10_000_001), (2, 1, 3_333_333), (3, 1, 3_333_335)]


async def dust_market(market, provider, repo, chain):
    from .helpers import open_bet

    bet = await open_bet(market, provider, repo)
    for n, side, amount in DUST_ROWS:
        await chain.deposit(bet.id, w(n), side, amount)
    return await market.close_funding(bet.id)


async def claim_tasks(repo, bet_id):
    """The win and refund tasks (the money), without the close_loser tasks (rent only)."""
    return [t for t in await repo.payout_tasks(bet_id) if t.kind != "close_loser"]


async def test_settlement_pays_the_fee_to_the_treasury_and_winners_get_pool_math_amounts(
    worker, market, provider, scripted, repo, chain, clock
):
    bet = await dust_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)  # home wins: side 1
    await worker.tick()
    done = await repo.get_bet(bet.id)
    s = settle_amounts((10_000_001, 6_666_668), 1, 500)
    cb = await chain.get_bet(bet.id)
    assert (done.status, done.winning_side, done.fee_paid_base, done.total_pool_base) == ("settled", 1, s.fee, 16_666_669)
    assert cb.treasury_balance_base == s.fee == 500_000
    all_tasks = await repo.payout_tasks(bet.id)
    tasks = {t.wallet: t for t in all_tasks if t.kind == "win"}
    assert set(tasks) == {w(2), w(3)}  # only the winners are paid
    [close] = [t for t in all_tasks if t.kind == "close_loser"]  # the loser's position is closed, no money moves
    assert (close.wallet, close.status, close.amount_base) == (w(1), "done", 0)
    for n, _, stake in DUST_ROWS[1:]:
        assert tasks[w(n)].amount_base == compute_payout(stake, s.distributable, s.winning_total)
        assert tasks[w(n)].status == "done" and tasks[w(n)].tx_ref.startswith("mock:")
    paid = sum(t.amount_base for t in tasks.values())
    assert cb.vault_base == s.distributable - paid and 0 <= cb.vault_base < len(tasks)  # only rounding dust is left
    assert done.payouts_completed_at == clock.current


async def test_refund_gives_every_participant_a_task_and_the_full_stake(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.set_status(bet.game_id, "cancelled")
    await worker.tick()
    tasks = await claim_tasks(repo, bet.id)
    assert {(t.wallet, t.kind, t.amount_base, t.status) for t in tasks} == {
        (w(1), "refund", 10_000_000, "done"), (w(2), "refund", 5_000_000, "done"), (w(3), "refund", 20_000_000, "done")}
    cb = await chain.get_bet(bet.id)
    assert (cb.vault_base, cb.treasury_balance_base) == (0, 0)
    assert (await repo.get_bet(bet.id)).payouts_completed_at is not None


async def _settled_with_broken_claims(worker, market, provider, scripted, repo, chain, clock, monkeypatch, error):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)
    real = chain.claim
    state = {"fail": True}

    async def claim(bet_id, wallet):
        if state["fail"]:
            raise error
        return await real(bet_id, wallet)

    monkeypatch.setattr(chain, "claim", claim)
    return bet, state


async def test_a_transient_claim_failure_retries_with_exponential_backoff(
    make_worker, market, provider, scripted, repo, chain, clock, monkeypatch
):
    worker = make_worker(claim_backoff_seconds=30, claim_max_attempts=8)
    bet, state = await _settled_with_broken_claims(worker, market, provider, scripted, repo, chain, clock, monkeypatch, RuntimeError("rpc down"))
    t0 = clock.current
    await worker.tick()
    tasks = [t for t in await repo.payout_tasks(bet.id) if t.kind != "close_loser"]
    assert [(t.status, t.attempts) for t in tasks] == [("pending", 1), ("pending", 1)]
    assert all(t.next_attempt_at == t0 + 30 * SEC for t in tasks)
    clock.current = t0 + 29 * SEC
    await worker.tick()
    assert [t.attempts for t in await claim_tasks(repo, bet.id)] == [1, 1]  # not due yet
    clock.current = t0 + 30 * SEC
    await worker.tick()
    tasks = await claim_tasks(repo, bet.id)
    assert [t.attempts for t in tasks] == [2, 2] and all(t.next_attempt_at == t0 + 90 * SEC for t in tasks)  # 30 s then 60 s
    state["fail"] = False
    clock.current = t0 + 90 * SEC
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert {t.status for t in await claim_tasks(repo, bet.id)} == {"done"} and done.payouts_completed_at == clock.current


async def test_already_claimed_counts_as_done_with_the_position_amount(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    await chain.settle(bet.id, 1)
    claimed = await chain.claim(bet.id, w(2))  # claimed outside the worker
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)
    await worker.tick()
    tasks = {t.wallet: t for t in await claim_tasks(repo, bet.id)}
    assert tasks[w(2)].status == "done" and tasks[w(2)].amount_base == claimed.payout_base
    assert (await repo.get_bet(bet.id)).payouts_completed_at is not None


async def test_attempts_exhausted_fails_the_task_and_queues_payouts_delayed_once_then_manual_retry_succeeds(
    make_worker, client, market, provider, scripted, repo, chain, clock, monkeypatch, fake_gateway, logs
):
    worker = make_worker(claim_backoff_seconds=30, claim_max_attempts=3)
    bet, state = await _settled_with_broken_claims(worker, market, provider, scripted, repo, chain, clock, monkeypatch, RuntimeError("rpc down"))
    t0 = clock.current
    for offset in (0, 30, 90):  # attempt 1 now, attempt 2 after 30 s, attempt 3 after a further 60 s
        clock.current = t0 + offset * SEC
        await worker.tick()
    tasks = [t for t in await repo.payout_tasks(bet.id) if t.kind != "close_loser"]
    assert {x.status for x in tasks} == {"failed"} and all(x.attempts == 3 for x in tasks)
    assert (await repo.get_bet(bet.id)).payouts_completed_at is None  # failed tasks keep the market open
    [msg] = sent(fake_gateway, ":payouts_delayed")
    assert msg["text"] == "Some payouts are taking longer than expected. I am retrying them and will keep trying."
    assert logs.events("worker.claim_gave_up")
    clock.current += 10 * MIN
    await worker.tick()
    assert len(sent(fake_gateway, ":payouts_delayed")) == 1  # once, however many tasks failed or ticks pass

    state["fail"] = False
    for task in tasks:
        r = await client.post(f"/admin/payout_tasks/{task.id}/retry", headers=ADMIN)
        assert r.status_code == 200 and r.json()["task"]["status"] == "pending" and r.json()["task"]["attempts"] == 0
    again = await client.post(f"/admin/payout_tasks/{tasks[0].id}/retry", headers=ADMIN)
    assert again.status_code == 409
    assert (await client.post("/admin/payout_tasks/not-a-uuid/retry", headers=ADMIN)).status_code == 404
    await worker.tick()
    assert {x.status for x in await claim_tasks(repo, bet.id)} == {"done"}
    assert (await repo.get_bet(bet.id)).payouts_completed_at == clock.current


async def test_payouts_complete_only_when_every_task_is_done(worker, market, provider, scripted, repo, chain, clock, monkeypatch):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)
    real = chain.claim

    async def claim(bet_id, wallet):
        if wallet == w(3):
            raise RuntimeError("rpc down")
        return await real(bet_id, wallet)

    monkeypatch.setattr(chain, "claim", claim)
    await worker.tick()
    assert sorted(t.status for t in await repo.payout_tasks(bet.id) if t.kind != "close_loser") == ["done", "pending"]
    assert (await repo.get_bet(bet.id)).payouts_completed_at is None
    monkeypatch.setattr(chain, "claim", real)
    clock.current += 31 * SEC
    await worker.tick()
    assert (await repo.get_bet(bet.id)).payouts_completed_at == clock.current


async def test_a_permanent_claim_error_fails_the_task_at_once(worker, market, provider, scripted, repo, chain, clock, monkeypatch, fake_gateway):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 21, 14)

    async def claim(bet_id, wallet):
        raise ChainError("INVALID_WALLET", "bad")

    monkeypatch.setattr(chain, "claim", claim)
    await worker.tick()
    assert {(t.status, t.attempts) for t in await repo.payout_tasks(bet.id) if t.kind != "close_loser"} == {("failed", 1)}
    assert len(sent(fake_gateway, ":payouts_delayed")) == 1


async def test_a_refund_with_nobody_in_it_completes_immediately(worker, market, provider, repo, clock):
    from .helpers import open_bet

    bet = await open_bet(market, provider, repo)
    clock.current = bet.funding_closes_at
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert (done.status, done.refund_reason) == ("refunded", "too_few_participants")
    assert await claim_tasks(repo, bet.id) == [] and done.payouts_completed_at == clock.current
