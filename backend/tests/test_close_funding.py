"""close_funding, early close when full, lazy close, and the admin endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app import templates as T

from .conftest import ADMIN_SECRET
from .helpers import ALICE, deposits, open_bet, w

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def texts(fake_gateway, suffix):
    return [s for s in fake_gateway.sends if s["idempotencyKey"].endswith(suffix)]


async def test_lock_path_records_totals_and_texts_the_proposer_once(market, provider, repo, chain, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 10), (2, 1, 5), (3, 1, 20), (4, 0, 3.5)])
    closed = await market.close_funding(bet.id)
    assert closed.status == "locked" and closed.total_pool_base == 38_500_000 and closed.locked_at is not None
    assert (await chain.get_bet(bet.id)).state == "locked"
    [msg] = texts(fake_gateway, ":locked")
    assert msg["to"] == ALICE and msg["idempotencyKey"] == f"bet:{bet.id}:locked"
    assert msg["text"] == (
        "Betting is closed for Seattle Seahawks at Arizona Cardinals. The pool is $38.50: $13.50 on Seattle Seahawks "
        "and $25 on Arizona Cardinals. The bet is locked until the game ends."
    )


@pytest.mark.parametrize(
    "rows, reason",
    [
        ([(1, 0, 5), (2, 0, 7)], "one_sided"),  # two people, same side
        ([(1, 1, 5)], "too_few_participants"),  # a single participant
        ([], "too_few_participants"),  # nobody joined
    ],
)
async def test_refund_paths(market, provider, repo, chain, fake_gateway, rows, reason):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, rows)
    closed = await market.close_funding(bet.id)
    assert (closed.status, closed.refund_reason, closed.fee_paid_base) == ("refunded", reason, 0)
    cb = await chain.get_bet(bet.id)
    assert (cb.state, cb.fee_paid_base, cb.treasury_balance_base) == ("refunded", 0, 0)
    [msg] = texts(fake_gateway, ":refunded")
    assert msg["text"] == (
        "Betting is closed for Seattle Seahawks at Arizona Cardinals. "
        "The bet did not have people on both sides, so everyone is refunded. Nothing was charged."
    )
    assert "-" not in msg["text"] and not texts(fake_gateway, ":locked")


async def test_close_funding_is_idempotent_and_concurrency_safe(market, provider, repo, chain, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    results = await asyncio.gather(*(market.close_funding(bet.id) for _ in range(5)))
    assert {r.status for r in results} == {"locked"}
    again = await market.close_funding(bet.id)
    assert again.status == "locked" and again.locked_at == results[0].locked_at
    assert len(texts(fake_gateway, ":locked")) == 1


async def test_close_funding_resumes_if_the_chain_already_moved(market, provider, repo, chain, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    await chain.lock(bet.id)  # crashed after the chain call, before the database update
    assert (await market.close_funding(bet.id)).status == "locked"
    assert len(texts(fake_gateway, ":locked")) == 1


async def test_unknown_bet(market):
    from markets.service import MarketError

    with pytest.raises(MarketError) as e:
        await market.close_funding("4cd57c06-fc4a-406d-9bdb-ae48507af330")
    assert e.value.code == "NOT_FOUND"


async def full_market(market, provider, repo, n=4):
    market.settings = replace(market.settings, max_participants=n)
    return await open_bet(market, provider, repo)


async def test_early_close_two_sided_full_market_locks_on_the_last_participant(market, provider, repo, chain, fake_gateway):
    bet = await full_market(market, provider, repo)
    assert bet.max_participants == 4
    for n, side in [(1, 0), (2, 1), (3, 0)]:
        await chain.deposit(bet.id, w(n), side, 5_000_000)
        assert (await market.maybe_close_when_full(bet.id)).status == "open"  # below the cap: nothing happens
    await chain.deposit(bet.id, w(4), 1, 5_000_000)
    closed = await market.maybe_close_when_full(bet.id)
    assert closed.status == "locked" and len(texts(fake_gateway, ":locked")) == 1
    assert (await market.maybe_close_when_full(bet.id)).status == "locked"  # repeated call: no change
    assert len(texts(fake_gateway, ":locked")) == 1


async def test_early_close_one_sided_full_market_refunds(market, provider, repo, chain, fake_gateway):
    bet = await full_market(market, provider, repo)
    await deposits(chain, bet.id, [(1, 1, 5), (2, 1, 5), (3, 1, 5), (4, 1, 5)])
    closed = await market.maybe_close_when_full(bet.id)
    assert (closed.status, closed.refund_reason) == ("refunded", "one_sided")
    assert len(texts(fake_gateway, ":refunded")) == 1


async def test_deposit_through_the_api_closes_the_market_automatically(client, market, provider, repo, fake_gateway):
    bet = await full_market(market, provider, repo)
    token = market.signer.token(bet.id)
    for n, side in [(1, 0), (2, 1), (3, 0), (4, 1)]:
        r = await client.post(f"/markets/{token}/deposits", json={"wallet": w(n), "side": side, "amount_usd": "5"})
        assert r.status_code == 200
    assert r.json()["status"] == "locked" and r.json()["participant_count"] == 4
    late = await client.post(f"/markets/{token}/deposits", json={"wallet": w(5), "side": 0, "amount_usd": "5"})
    assert (late.status_code, late.json()["code"], late.json()["status"]) == (409, "MARKET_NOT_OPEN", "locked")
    assert len(texts(fake_gateway, ":locked")) == 1


async def test_late_deposit_closes_funding_lazily_without_a_worker(client, market, provider, repo, chain, clock, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    clock.current = bet.funding_closes_at + timedelta(seconds=1)
    r = await client.post(
        f"/markets/{market.signer.token(bet.id)}/deposits", json={"wallet": w(3), "side": 0, "amount_usd": "5"}
    )
    assert (r.status_code, r.json()["code"], r.json()["status"]) == (409, "MARKET_NOT_OPEN", "locked")
    assert (await repo.get_bet(bet.id)).status == "locked" and len(texts(fake_gateway, ":locked")) == 1
    assert (await chain.get_bet(bet.id)).participant_count == 2


# --- admin endpoints ---

async def test_admin_close_funding_and_view(client, market, provider, repo, chain, fake_gateway):
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    assert (await client.post(f"/admin/bets/{bet.id}/close_funding")).status_code == 401
    assert (await client.get(f"/admin/bets/{bet.id}")).status_code == 401
    r = await client.post(f"/admin/bets/{bet.id}/close_funding", headers=ADMIN)
    body = r.json()
    assert r.status_code == 200 and body["bet"]["status"] == "locked" and body["chain"]["state"] == "locked"
    assert body["bet"]["proposer_handle"] == "***1111"  # masked
    assert len(body["positions"]) == 2 and body["link"] == market.signer.url(bet.id)
    assert [(n["kind"], n["status"]) for n in body["notices"]] == [("link", "sent"), ("locked", "sent")]
    again = await client.post(f"/admin/bets/{bet.id}/close_funding", headers=ADMIN)
    assert again.status_code == 200 and again.json()["bet"]["status"] == "locked"
    assert len(texts(fake_gateway, ":locked")) == 1
    assert (await client.get(f"/admin/bets/{bet.id}", headers=ADMIN)).json()["bet"]["total_pool_base"] == 10_000_000
    assert (await client.get("/admin/bets/4cd57c06-fc4a-406d-9bdb-ae48507af330", headers=ADMIN)).status_code == 404
    assert (await client.get("/admin/bets/nope", headers=ADMIN)).status_code == 404


async def test_admin_mark_payouts_complete_only_for_settled_or_refunded(client, market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    r = await client.post(f"/admin/bets/{bet.id}/mark_payouts_complete", headers=ADMIN)
    assert (r.status_code, r.json()["code"]) == (409, "WRONG_STATE")
    assert (await client.post(f"/admin/bets/{bet.id}/mark_payouts_complete")).status_code == 401
    await market.close_funding(bet.id)  # empty market: refunded
    r = await client.post(f"/admin/bets/{bet.id}/mark_payouts_complete", headers=ADMIN)
    assert r.status_code == 200 and r.json()["bet"]["payouts_completed_at"] is not None
    assert r.json()["link_expires_at"] is not None


async def test_admin_adds_a_simulated_game(client, repo):
    r = await client.post("/admin/games", headers=ADMIN, json={"home_id": "nfl-ari", "away_id": "nfl-sea", "minutes_from_now": 20})
    g = r.json()["game"]
    assert r.status_code == 200 and g["home"]["id"] == "nfl-ari" and g["status"] == "scheduled"
    assert (await repo.get_sim_game(g["game_id"]))["kickoff_utc"] == datetime.fromisoformat(g["kickoff_utc"])
    bad = await client.post("/admin/games", headers=ADMIN, json={"home_id": "x", "away_id": "nfl-sea", "minutes_from_now": 5})
    assert bad.status_code == 400
    assert (await client.post("/admin/games", json={"home_id": "nfl-ari", "away_id": "nfl-sea", "minutes_from_now": 5})).status_code == 401
