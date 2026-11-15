"""The same scenarios against the mock chain and against RealChainClient (talking to a fake chain service with the
contract's rules). Anything that differs between the two is a bug in one of them: this is the parity contract."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest

from markets.chain import BetParams, ChainError
from markets.mock_chain import MockChainClient
from markets.real_chain import RealChainClient

from .fake_chain_service import SECRET, FakeChainService
from .helpers import USDC, w

HOUR = timedelta(hours=1)
ALL = [w(n) for n in range(1, 6)]  # every wallet the scenarios deposit with


class Harness:
    def __init__(self, kind, client, deposit, clock):
        self.kind, self.client, self.deposit, self.clock = kind, client, deposit, clock

    def params(self, **over) -> BetParams:
        now = self.clock.current
        base = dict(fee_bps=500, treasury="T", min_stake_base=1 * USDC, max_stake_base=20 * USDC, max_participants=4,
                    funding_closes_at=now + HOUR, refund_after=now + HOUR + 12 * HOUR)
        return BetParams(**{**base, **over})

    async def open(self, **over) -> str:
        bet_id = str(uuid.uuid4())
        await self.client.create_bet(bet_id, self.params(**over))
        return bet_id

    async def fill(self, bet_id, rows):
        for n, side, usd in rows:
            await self.deposit(bet_id, w(n), side, int(usd * USDC))

    async def code(self, coro) -> str:
        with pytest.raises(ChainError) as e:
            await coro
        return e.value.code


@pytest.fixture(params=["mock", "real"])
async def h(request, repo, clock):
    if request.param == "mock":
        client = MockChainClient(repo.pool, now=clock)
        yield Harness("mock", client, client.deposit, clock)
        return
    fake = FakeChainService(clock)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))

    async def nosleep(_):
        pass

    client = RealChainClient("http://chain.test", SECRET, client=http, sleep=nosleep)
    harness = Harness("real", client, lambda b, wl, s, a: _wrap(fake.deposit, b, wl, s, a), clock)
    harness.fake = fake
    yield harness
    await http.aclose()


async def _wrap(fn, *a):
    fn(*a)


# ---------------- create ----------------

async def test_create_is_idempotent_and_starts_open(h):
    bet_id = str(uuid.uuid4())
    first = await h.client.create_bet(bet_id, h.params())
    await h.fill(bet_id, [(1, 0, 5)])
    again = await h.client.create_bet(bet_id, h.params())
    assert first.state == "open" and first.participant_count == 0
    assert again.side_totals == (5 * USDC, 0) and again.participant_count == 1  # the existing bet, unchanged


async def test_create_rejects_what_the_contract_rejects(h):
    for over in (dict(fee_bps=1001), dict(min_stake_base=0), dict(min_stake_base=5 * USDC, max_stake_base=4 * USDC),
                 dict(max_participants=1), dict(max_participants=101),
                 dict(funding_closes_at=h.clock.current - HOUR), dict(refund_after=h.clock.current + HOUR)):
        assert await h.code(h.client.create_bet(str(uuid.uuid4()), h.params(**over))) == "INVALID_PARAMS", over


# ---------------- deposits (user transactions) ----------------

async def test_deposit_rules_and_their_order(h):
    b = await h.open(max_participants=2)
    assert await h.code(h.deposit(b, w(1), 0, USDC // 2)) == "BELOW_MIN"
    assert await h.code(h.deposit(b, w(1), 0, 21 * USDC)) == "ABOVE_MAX"
    assert await h.code(h.deposit(b, w(1), 2, 5 * USDC)) == "INVALID_SIDE"
    await h.deposit(b, w(1), 0, 15 * USDC)
    assert await h.code(h.deposit(b, w(1), 1, 5 * USDC)) == "ALREADY_OTHER_SIDE"
    assert await h.code(h.deposit(b, w(1), 0, 6 * USDC)) == "ABOVE_MAX"  # a top up past the max
    await h.deposit(b, w(1), 0, 5 * USDC)  # exactly the max
    await h.deposit(b, w(2), 1, 5 * USDC)
    assert await h.code(h.deposit(b, w(3), 0, 5 * USDC)) == "MARKET_FULL"
    await h.deposit(b, w(2), 1, 1 * USDC)  # a top up on a full market is fine
    cb = await h.client.get_bet(b)
    assert cb.participant_count == 2 and cb.side_totals == (20 * USDC, 6 * USDC)


async def test_deposits_stop_at_funding_closes_at_and_after_lock(h):
    b = await h.open()
    await h.fill(b, [(1, 0, 5), (2, 1, 5)])
    h.clock.current += HOUR - timedelta(seconds=1)
    await h.deposit(b, w(3), 0, USDC)  # one second before
    h.clock.current += timedelta(seconds=1)
    assert await h.code(h.deposit(b, w(4), 0, USDC)) == "FUNDING_CLOSED"
    await h.client.lock(b)
    assert await h.code(h.deposit(b, w(4), 0, USDC)) == "MARKET_NOT_OPEN"  # state beats the funding window


# ---------------- lock ----------------

async def test_lock_needs_both_sides_and_two_participants(h):
    one_sided = await h.open()
    await h.fill(one_sided, [(1, 0, 5), (2, 0, 5)])
    assert await h.code(h.client.lock(one_sided)) == "BOTH_SIDES_REQUIRED"
    single = await h.open()
    await h.fill(single, [(1, 0, 5)])
    assert await h.code(h.client.lock(single)) == "BOTH_SIDES_REQUIRED"
    assert await h.code(h.client.lock(await h.open())) == "BOTH_SIDES_REQUIRED"
    ok = await h.open()
    await h.fill(ok, [(1, 0, 5), (2, 1, 5)])
    assert (await h.client.lock(ok)).state == "locked"


async def test_lock_and_settle_stop_at_refund_after(h):
    late_lock = await h.open()
    await h.fill(late_lock, [(1, 0, 5), (2, 1, 5)])
    late_settle = await h.open()
    await h.fill(late_settle, [(1, 0, 5), (2, 1, 5)])
    await h.client.lock(late_settle)
    h.clock.current += HOUR + 12 * HOUR - timedelta(seconds=1)
    assert (await h.client.get_bet(late_lock)).state == "open"
    h.clock.current += timedelta(seconds=1)
    assert await h.code(h.client.lock(late_lock)) == "PAST_REFUND_DEADLINE"
    assert await h.code(h.client.settle(late_settle, 1)) == "PAST_REFUND_DEADLINE"
    assert (await h.client.refund_all(late_settle)).state == "refunded"  # the oracle can still refund


# ---------------- settle and claims ----------------

async def settled_13(h, winner=1):
    b = await h.open(max_participants=20)
    await h.fill(b, [(1, 0, 13), (2, 1, 13)])
    await h.client.lock(b)
    return b, await h.client.settle(b, winner)


async def test_settle_matches_the_pool_math_13_vs_13(h):
    b, cb = await settled_13(h)
    assert (cb.state, cb.winning_side, cb.fee_paid_base, cb.distributable_base) == ("settled", 1, 650_000, 25_350_000)
    assert cb.vault_base == 25_350_000 and cb.treasury_balance_base == 650_000
    claim = await h.client.claim(b, w(2))
    assert (claim.kind, claim.payout_base) == ("winnings", 25_350_000)  # 1.95 times the stake


async def test_settle_states_and_sides(h):
    b = await h.open()
    await h.fill(b, [(1, 0, 5), (2, 1, 5)])
    assert await h.code(h.client.settle(b, 0)) == "WRONG_STATE"  # not locked yet
    await h.client.lock(b)
    assert await h.code(h.client.settle(b, 2)) in ("INVALID_SIDE", "INVALID_PARAMS")
    await h.client.settle(b, 0)
    assert await h.code(h.client.settle(b, 1)) == "WRONG_STATE"  # a settled market can not be settled the other way


async def test_claims_are_single_use_and_a_loser_cannot_claim(h):
    b, _ = await settled_13(h)
    assert await h.code(h.client.claim(b, w(1))) == "NOT_A_WINNER"
    await h.client.claim(b, w(2))
    assert await h.code(h.client.claim(b, w(2))) == "ALREADY_CLAIMED"
    assert await h.code(h.client.claim(b, w(9))) == "ALREADY_CLAIMED"  # no position account: looks the same as a claimed one


async def test_claims_before_settlement_are_wrong_state(h):
    b = await h.open()
    await h.fill(b, [(1, 0, 5), (2, 1, 5)])
    assert await h.code(h.client.claim(b, w(1))) == "WRONG_STATE"
    await h.client.lock(b)
    assert await h.code(h.client.claim(b, w(1))) == "WRONG_STATE"


async def test_positions_are_read_for_the_named_depositors_only(h):
    b, _ = await settled_13(h)
    assert {p.wallet for p in await h.client.get_positions(b, [w(1), w(99)])} == {w(1)}  # w(99) never deposited
    assert {p.wallet for p in await h.client.get_positions(b, [w(2), w(2)])} == {w(2)}  # a repeated wallet is read once
    assert await h.client.get_positions(b, []) == []
    assert await h.client.get_positions(b, [w(99)]) == []
    assert await h.code(h.client.get_positions(b, ["not a wallet"])) == "INVALID_WALLET"
    assert await h.code(h.client.get_positions(str(uuid.uuid4()), [w(1)])) == "NOT_FOUND"


async def test_positions_disappear_when_claimed_or_closed_but_totals_stay(h):
    b, _ = await settled_13(h)
    assert {p.wallet for p in await h.client.get_positions(b, ALL)} == {w(1), w(2)}
    await h.client.claim(b, w(2))
    assert {p.wallet for p in await h.client.get_positions(b, ALL)} == {w(1)}
    cb = await h.client.get_bet(b)
    assert cb.positions_open == 1 and cb.side_totals == (13 * USDC, 13 * USDC) and cb.participant_count == 2


# ---------------- refunds ----------------

async def test_refund_pays_stakes_with_no_fee_from_open_and_locked(h):
    from_open = await h.open()
    await h.fill(from_open, [(1, 0, 9), (2, 0, 4)])
    cb = await h.client.refund_all(from_open)
    assert (cb.state, cb.fee_paid_base) == ("refunded", 0)
    assert [(await h.client.claim(from_open, x)).payout_base for x in (w(1), w(2))] == [9 * USDC, 4 * USDC]
    cb = await h.client.get_bet(from_open)
    assert cb.vault_base == 0 and cb.treasury_balance_base == 0
    locked = await h.open()
    await h.fill(locked, [(1, 0, 5), (2, 1, 5)])
    await h.client.lock(locked)
    assert (await h.client.refund_all(locked)).state == "refunded"
    assert await h.code(h.client.refund_all(await settled_id(h))) == "WRONG_STATE"


async def settled_id(h):
    b, _ = await settled_13(h)
    return b


# ---------------- close_loser_position ----------------

async def test_close_loser_position_moves_no_money_and_rejects_winners(h):
    b, _ = await settled_13(h)
    vault = (await h.client.get_bet(b)).vault_base
    assert await h.code(h.client.close_loser_position(b, w(2))) == "WRONG_STATE"  # the winner claims instead
    assert await h.client.close_loser_position(b, w(1)) is None
    assert await h.client.close_loser_position(b, w(1)) is None  # idempotent
    assert (await h.client.get_bet(b)).vault_base == vault
    assert await h.code(h.client.claim(b, w(1))) == "ALREADY_CLAIMED"  # a closed position cannot be claimed
    assert {p.wallet for p in await h.client.get_positions(b, ALL)} == {w(2)}


async def test_close_loser_position_needs_a_settled_market(h):
    b = await h.open()
    await h.fill(b, [(1, 0, 5), (2, 1, 5)])
    assert await h.code(h.client.close_loser_position(b, w(1))) == "WRONG_STATE"
    await h.client.lock(b)
    await h.client.refund_all(b)
    assert await h.code(h.client.close_loser_position(b, w(1))) == "WRONG_STATE"


# ---------------- close_bet ----------------

async def test_close_bet_after_every_position_is_closed_and_sweeps_the_dust(h):
    b = await h.open(max_participants=20)
    await h.fill(b, [(1, 0, 10), (2, 1, 3.333333), (3, 1, 3.333335)])  # awkward amounts leave rounding dust
    await h.client.lock(b)
    cb = await h.client.settle(b, 1)
    assert await h.code(h.client.close_bet(b)) == "WRONG_STATE"  # positions are still open
    await h.client.claim(b, w(2))
    await h.client.claim(b, w(3))
    assert await h.code(h.client.close_bet(b)) == "WRONG_STATE"  # the loser's position is still open
    await h.client.close_loser_position(b, w(1))
    before = await h.client.get_bet(b)
    assert before.positions_open == 0 and 0 < before.vault_base < 3
    assert await h.client.close_bet(b) is None
    assert await h.client.close_bet(b) is None  # idempotent
    assert await h.code(h.client.get_bet(b)) == "NOT_FOUND"  # the Bet account is gone
    assert cb.fee_paid_base == 500_000


async def test_close_bet_needs_settled_or_refunded(h):
    b = await h.open()
    assert await h.code(h.client.close_bet(b)) == "WRONG_STATE"
    await h.fill(b, [(1, 0, 5), (2, 1, 5)])
    await h.client.lock(b)
    assert await h.code(h.client.close_bet(b)) == "WRONG_STATE"
    await h.client.refund_all(b)
    assert await h.code(h.client.close_bet(b)) == "WRONG_STATE"  # claims first
    await h.client.claim(b, w(1))
    await h.client.claim(b, w(2))
    assert await h.client.close_bet(b) is None


async def test_unknown_bets_are_not_found(h):
    assert await h.code(h.client.get_bet(str(uuid.uuid4()))) == "NOT_FOUND"
    assert await h.code(h.client.lock(str(uuid.uuid4()))) == "NOT_FOUND"


# ---------------- mock only: emergency refund ----------------

async def test_mock_emergency_refund_is_open_to_anyone_at_refund_after(h):
    if h.kind != "mock":
        pytest.skip("the real service has no emergency_refund endpoint: the oracle's refund_all has no deadline")
    b = await h.open()
    await h.fill(b, [(1, 0, 5), (2, 1, 5)])
    assert await h.code(h.client.emergency_refund(b)) == "REFUND_TOO_EARLY"
    h.clock.current += HOUR + 12 * HOUR
    assert (await h.client.emergency_refund(b)).state == "refunded"
    assert (await h.client.claim(b, w(1))).payout_base == 5 * USDC
    assert await h.code(h.client.emergency_refund(b)) == "WRONG_STATE"
