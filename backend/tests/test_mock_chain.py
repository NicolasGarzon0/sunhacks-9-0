"""MockChainClient: the contract's rules over the mock_chain_* tables (throwaway Postgres schema per test)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from markets.chain import BetParams, ChainError, build_chain_client
from markets.mock_chain import MockChainClient

USDC = 1_000_000
PARAMS = BetParams(fee_bps=500, treasury="MockTreasury", min_stake_base=1 * USDC, max_stake_base=20 * USDC, max_participants=4)


def w(n: int) -> str:
    return f"W{n}" + "x" * 40  # valid base58 public key shape (32 to 44 chars, no 0 O I l)


@pytest.fixture
def mock(repo) -> MockChainClient:
    return MockChainClient(repo.pool)


@pytest.fixture
async def bet_id(mock) -> str:
    bid = str(uuid.uuid4())
    await mock.create_bet(bid, PARAMS)
    return bid


async def code_of(coro) -> str:
    with pytest.raises(ChainError) as e:
        await coro
    return e.value.code


async def test_create_bet_is_idempotent(mock, bet_id):
    again = await mock.create_bet(bet_id, BetParams(1, "other", 5, 6, 2))  # ignored: the first params stay
    assert again.state == "open" and again.participant_count == 0
    await mock.deposit(bet_id, w(1), 0, 5 * USDC)
    assert (await mock.create_bet(bet_id, PARAMS)).side_totals == (5 * USDC, 0)


async def test_deposit_error_codes(mock, bet_id):
    assert await code_of(mock.deposit(bet_id, w(1), 0, USDC // 2)) == "BELOW_MIN"
    assert await code_of(mock.deposit(bet_id, w(1), 0, 21 * USDC)) == "ABOVE_MAX"
    assert await code_of(mock.deposit(bet_id, "short", 0, USDC)) == "INVALID_WALLET"
    assert await code_of(mock.deposit(bet_id, "0" * 40, 0, USDC)) == "INVALID_WALLET"  # 0 is not base58
    assert await code_of(mock.deposit(bet_id, w(1), 2, USDC)) == "INVALID_SIDE"
    assert await code_of(mock.deposit(bet_id, w(1), True, USDC)) == "INVALID_SIDE"
    assert await code_of(mock.deposit(str(uuid.uuid4()), w(1), 0, USDC)) == "NOT_FOUND"
    assert await code_of(mock.deposit("not-a-uuid", w(1), 0, USDC)) == "NOT_FOUND"
    assert await code_of(mock.get_bet(str(uuid.uuid4()))) == "NOT_FOUND"
    await mock.deposit(bet_id, w(1), 0, 5 * USDC)
    assert await code_of(mock.deposit(bet_id, w(1), 1, 5 * USDC)) == "ALREADY_OTHER_SIDE"
    await mock.deposit(bet_id, w(9), 1, 5 * USDC)  # lock now needs both sides and 2 wallets (contract parity)
    await mock.lock(bet_id)
    assert await code_of(mock.deposit(bet_id, w(2), 0, USDC)) == "MARKET_NOT_OPEN"


async def test_one_side_per_wallet_and_top_up(mock, bet_id):
    await mock.deposit(bet_id, w(1), 1, 5 * USDC)
    p = await mock.deposit(bet_id, w(1), 1, 7 * USDC)  # top up the same side
    assert (p.side, p.amount_base) == (1, 12 * USDC)
    cb = await mock.get_bet(bet_id)
    assert cb.side_totals == (0, 12 * USDC) and cb.participant_count == 1 and cb.vault_base == 12 * USDC


async def test_top_up_over_max_is_rejected_and_changes_nothing(mock, bet_id):
    await mock.deposit(bet_id, w(1), 0, 15 * USDC)
    assert await code_of(mock.deposit(bet_id, w(1), 0, 6 * USDC)) == "ABOVE_MAX"
    await mock.deposit(bet_id, w(1), 0, 5 * USDC)  # exactly the max is fine
    assert (await mock.get_bet(bet_id)).side_totals == (20 * USDC, 0)


async def test_participant_cap_applies_to_new_wallets_only(mock, bet_id):
    for i in range(1, 5):
        await mock.deposit(bet_id, w(i), i % 2, 2 * USDC)
    assert await code_of(mock.deposit(bet_id, w(5), 0, USDC)) == "MARKET_FULL"
    p = await mock.deposit(bet_id, w(1), 1, 3 * USDC)  # a top up on a full (not yet closed) market is allowed
    assert p.amount_base == 5 * USDC
    assert (await mock.get_bet(bet_id)).participant_count == 4


async def test_concurrent_deposits_respect_the_cap(mock, bet_id):
    results = await asyncio.gather(
        *(mock.deposit(bet_id, w(i), 0, USDC) for i in range(1, 9)), return_exceptions=True
    )
    assert sum(isinstance(r, ChainError) and r.code == "MARKET_FULL" for r in results) == 4
    assert (await mock.get_bet(bet_id)).participant_count == 4


async def fill(mock, bet_id):
    # side 0: w1 10, w2 20 (pool 30)   side 1: w3 20, w4 15 + 4 top up (pool 39)
    for n, side, amt in [(1, 0, 10), (2, 0, 20), (3, 1, 20), (4, 1, 15)]:
        await mock.deposit(bet_id, w(n), side, amt * USDC)
    await mock.deposit(bet_id, w(4), 1, 4 * USDC)


async def test_lock_settle_claim_leaves_only_dust_and_treasury_gets_the_fee(mock, bet_id):
    await fill(mock, bet_id)
    assert (await mock.lock(bet_id)).state == "locked"
    assert await code_of(mock.lock(bet_id)) == "WRONG_STATE"
    assert await code_of(mock.claim(bet_id, w(1))) == "WRONG_STATE"  # nothing to claim before settlement
    cb = await mock.settle(bet_id, 1)  # side 1 wins (39 vs losing 30): fee = 5% of 30 = 1.5
    assert (cb.state, cb.winning_side, cb.fee_paid_base, cb.distributable_base) == (
        "settled", 1, 1_500_000, 67_500_000
    )
    assert cb.treasury_balance_base == 1_500_000 and cb.vault_base == 67_500_000  # the fee left the vault
    c3, c4 = await mock.claim(bet_id, w(3)), await mock.claim(bet_id, w(4))
    assert (c3.kind, c3.payout_base, c4.payout_base) == ("winnings", 34_615_384, 32_884_615)
    done = await mock.get_bet(bet_id)
    assert done.vault_base == 67_500_000 - c3.payout_base - c4.payout_base == 1  # one base unit of dust
    assert done.treasury_balance_base == done.fee_paid_base == 1_500_000
    # claimed positions are gone like on chain; only the losers' positions are still open
    assert {p.wallet for p in await mock.get_positions(bet_id, [w(n) for n in range(1, 5)])} == {w(1), w(2)}
    assert done.positions_open == 2


async def test_double_claim_and_loser_claim(mock, bet_id):
    await fill(mock, bet_id)
    await mock.lock(bet_id)
    await mock.settle(bet_id, 0)  # side 0 wins: fee = 5% of 39 = 1.95
    assert await code_of(mock.claim(bet_id, w(3))) == "NOT_A_WINNER"
    # a wallet that never held a position looks exactly like a claimed one on chain (no account): ALREADY_CLAIMED
    assert await code_of(mock.claim(bet_id, w(9))) == "ALREADY_CLAIMED"
    assert await code_of(mock.claim(bet_id, "bad")) == "INVALID_WALLET"
    first = await mock.claim(bet_id, w(1))
    assert first.payout_base == 22_350_000  # 10 of 30 over 67.05
    assert await code_of(mock.claim(bet_id, w(1))) == "ALREADY_CLAIMED"
    assert (await mock.get_bet(bet_id)).vault_base == 67_050_000 - 22_350_000


async def test_settle_rules(mock, bet_id):
    assert await code_of(mock.settle(bet_id, 0)) == "WRONG_STATE"  # must lock first
    await mock.deposit(bet_id, w(1), 0, 5 * USDC)
    assert await code_of(mock.lock(bet_id)) == "BOTH_SIDES_REQUIRED"  # one sided: refund_all is the only way out
    await mock.deposit(bet_id, w(2), 1, 5 * USDC)
    await mock.lock(bet_id)
    assert await code_of(mock.settle(bet_id, 2)) == "INVALID_SIDE"


async def test_refund_path_has_no_fee_and_returns_stakes(mock, bet_id):
    await mock.deposit(bet_id, w(1), 0, 9 * USDC)
    await mock.deposit(bet_id, w(2), 0, 4 * USDC)
    cb = await mock.refund_all(bet_id)
    assert (cb.state, cb.fee_paid_base, cb.treasury_balance_base) == ("refunded", 0, 0)
    assert await code_of(mock.refund_all(bet_id)) == "WRONG_STATE"
    assert await code_of(mock.deposit(bet_id, w(3), 0, USDC)) == "MARKET_NOT_OPEN"
    assert [(await mock.claim(bet_id, x)).payout_base for x in (w(1), w(2))] == [9 * USDC, 4 * USDC]
    assert await code_of(mock.claim(bet_id, w(1))) == "ALREADY_CLAIMED"
    assert (await mock.get_bet(bet_id)).vault_base == 0


async def test_refund_from_locked(mock, bet_id):
    await fill(mock, bet_id)
    await mock.lock(bet_id)
    assert (await mock.refund_all(bet_id)).state == "refunded"  # e.g. a cancelled game


async def test_state_persists_across_a_fresh_client(repo, mock, bet_id):
    await fill(mock, bet_id)
    await mock.lock(bet_id)
    fresh = MockChainClient(repo.pool)
    cb = await fresh.get_bet(bet_id)
    assert cb.state == "locked" and cb.side_totals == (30 * USDC, 39 * USDC) and cb.participant_count == 4
    assert len(await fresh.get_positions(bet_id, [w(n) for n in range(1, 5)])) == 4


async def test_factory(repo, settings):
    from dataclasses import replace

    assert isinstance(build_chain_client(settings, repo.pool), MockChainClient)
    from markets.real_chain import RealChainClient

    real = build_chain_client(replace(settings, chain_mode="real", chain_service_url="http://chain.test", chain_service_secret="s"), repo.pool)
    assert isinstance(real, RealChainClient) and real.mode == "real"
