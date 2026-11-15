"""Opening a market when the proposer confirms a game (through POST /inbound, like the chunk 3 flow tests)."""

from __future__ import annotations

import re
from datetime import timedelta

import httpx
import pytest

from app import templates as T
from markets.chain import ChainError

from .conftest import GATEWAY_SECRET
from .helpers import ALICE, w

GW = {"X-Gateway-Secret": GATEWAY_SECRET}
KICKOFF_CARDS = "Sunday Nov 15, 2:05 PM MST"


@pytest.fixture
async def roster(repo):
    await repo.upsert_roster("alice", "Alice", ALICE)


class Chat:
    def __init__(self, client, gw):
        self.client, self.gw, self.n = client, gw, 0

    async def say(self, text: str = "msg", message_id: str | None = None):
        self.n += 1
        before = len(self.gw.sends)
        r = await self.client.post(
            "/inbound",
            json={"messageId": message_id or f"m-{self.n}", "handle": ALICE, "text": text, "receivedAt": "2026-11-14T19:00:00Z"},
            headers=GW,
        )
        assert r.status_code == 200
        return self.gw.sends[before:]


@pytest.fixture
def chat(client, fake_gateway, roster):
    return Chat(client, fake_gateway)


async def confirm_cardinals(chat, extractor):
    extractor.push(team="cardinals")
    [offer] = await chat.say("bet on the cardinals")
    assert "Is this the game?" in offer["text"]
    return await chat.say("yes")


async def test_success_opens_market_and_texts_the_link_once(chat, extractor, repo, market, chain, logs):
    sends = await confirm_cardinals(chat, extractor)
    [bet] = await repo.list_bets(ALICE)
    assert len(sends) == 1 and sends[0]["to"] == ALICE and sends[0]["idempotencyKey"] == f"bet:{bet.id}:link"
    text = sends[0]["text"]
    url = market.signer.url(bet.id)
    assert text == (
        f"Your bet is open: Seattle Seahawks at Arizona Cardinals, {KICKOFF_CARDS}.\n"
        "Betting closes Sunday Nov 15, 1:05 PM MST.\n"
        "Forward this link to anyone who wants in.\n"
        f"{url}"
    )
    assert url.startswith("https://deposit.test/b/") and market.signer.bet_id(url.rsplit("/", 1)[1]) == bet.id
    # the row: snapshot of config, sides, chain state, open
    assert (bet.status, bet.side0_name, bet.side1_name, bet.fee_bps) == ("open", "Seattle Seahawks", "Arizona Cardinals", 500)
    assert (bet.min_stake_base, bet.max_stake_base, bet.max_participants) == (1_000_000, 20_000_000, 20)
    assert bet.funding_closes_at == bet.kickoff_utc - timedelta(minutes=60)
    assert bet.settle_after == bet.kickoff_utc + timedelta(hours=6) and bet.opened_at and bet.chain_bet_id == bet.id
    assert (await chain.get_bet(bet.id)).state == "open"
    assert await repo.get_draft(ALICE) is None
    assert [(n["kind"], n["status"], n["attempts"]) for n in await repo.list_notices(bet.id)] == [("link", "sent", 1)]
    assert not any(url.rsplit("/", 1)[1] in line for line in logs.lines)  # tokens are never logged


async def test_game_that_is_no_longer_upcoming_creates_nothing(chat, extractor, repo, provider):
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    await repo.set_sim_game_status("sim:t-sea-ari", "postponed")
    [reply] = await chat.say("yes")
    assert reply["text"] == T.GAME_UNAVAILABLE and reply["idempotencyKey"].startswith("reply:")
    assert await repo.list_bets(ALICE) == []
    assert (await repo.get_draft(ALICE)).pending_field == "team"  # back to "which game?"


async def test_game_in_progress_creates_nothing(chat, extractor, repo):
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    await repo.set_sim_game_status("sim:t-sea-ari", "in_progress")
    [reply] = await chat.say("yes")
    assert reply["text"] == T.GAME_STARTED and await repo.list_bets(ALICE) == []


async def test_kickoff_too_close_creates_nothing(chat, extractor, repo, market):
    from dataclasses import replace

    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    # betting would have to close 33 hours before kickoff, which is already past (kickoff is about 26 hours away)
    market.settings = replace(market.settings, funding_minutes_before_kickoff=2000)
    [reply] = await chat.say("yes")
    assert reply["text"] == T.funding_closed(2000) and await repo.list_bets(ALICE) == []
    assert "-" not in reply["text"]


async def test_lookup_skips_games_whose_funding_window_closed(chat, extractor, clock, repo):
    clock.current += timedelta(hours=25)  # 20:00Z Sunday: funding closes at 20:05Z, still open
    extractor.push(team="cardinals")
    [offer] = await chat.say("bet on the cardinals")
    assert "Is this the game?" in offer["text"]
    clock.current += timedelta(minutes=10)  # now past the funding close, before kickoff
    await repo.delete_draft(ALICE)
    extractor.push(team="cardinals")
    [again] = await chat.say("bet on the cardinals")
    assert "could not find a game" in again["text"]


async def test_replayed_message_never_makes_a_second_market_or_text(chat, extractor, repo, fake_gateway):
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    first = await chat.say("yes", message_id="confirm-1")
    replay = await chat.say("yes", message_id="confirm-1")  # same messageId: dropped by inbound dedup
    assert len(first) == 1 and replay == []
    assert len(await repo.list_bets(ALICE)) == 1
    assert len([s for s in fake_gateway.sends if s["idempotencyKey"].endswith(":link")]) == 1


async def test_confirming_the_same_game_again_resends_the_existing_link(chat, extractor, repo, chain):
    [first] = await confirm_cardinals(chat, extractor)
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    [second] = await chat.say("yes")
    assert second["text"] == "You already have a bet open on that game.\n" + first["text"]
    assert second["idempotencyKey"].startswith("reply:")  # a new message: normal reply, not the notice key
    [bet] = await repo.list_bets(ALICE)
    assert len([b for b in await repo.list_bets(ALICE)]) == 1
    assert [n["status"] for n in await repo.list_notices(bet.id)] == ["sent"]


async def test_a_second_proposer_gets_their_own_market(chat, extractor, repo, market, provider):
    await confirm_cardinals(chat, extractor)
    [game] = await provider.find_candidates("cardinals", None, None)
    res = await market.open_market("+15550002222", game)
    assert res.kind == "opened"
    assert {b.proposer_handle for b in await repo.list_bets("+15550002222")} == {"+15550002222"}


async def test_crashed_game_confirmed_row_resumes_on_confirm(repo, market, provider, fake_gateway):
    [game] = await provider.find_candidates("cardinals", None, None)
    bet, _ = await repo.create_or_get_bet(ALICE, game.to_json(), market._snapshot(game))  # crash after the row
    assert bet.status == "game_confirmed"
    res = await market.open_market(ALICE, game)
    assert res.kind == "opened"
    fresh = await repo.get_bet(bet.id)
    assert fresh.status == "open" and len(await repo.list_bets(ALICE)) == 1
    assert len(fake_gateway.sends) == 1


async def test_failure_after_row_creation_ends_failed_with_a_message(chat, extractor, repo, chain, fake_gateway, monkeypatch):
    async def boom(*a, **k):
        raise ChainError("WRONG_STATE", "chain exploded")

    monkeypatch.setattr(chain, "create_bet", boom)
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    [msg] = await chat.say("yes")
    [bet] = await repo.list_bets(ALICE)
    assert bet.status == "failed" and bet.failure_reason.startswith("open_failed")
    assert msg["text"] == T.MARKET_FAILED and msg["idempotencyKey"] == f"bet:{bet.id}:failed"
    assert await repo.get_draft(ALICE) is None
    with pytest.raises(ChainError):
        await chain.get_bet(bet.id)  # no half open market left on the chain
    # a failed market does not block a fresh attempt on the same game
    monkeypatch.undo()
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    [ok] = await chat.say("yes")
    assert ok["text"].startswith("Your bet is open:")
    assert [b.status for b in await repo.list_bets(ALICE)] == ["failed", "open"]


async def test_failure_when_marking_open_refunds_the_chain_market(repo, market, provider, chain, monkeypatch):
    async def nope(*a, **k):
        return False

    monkeypatch.setattr(repo, "set_bet_open", nope)
    [game] = await provider.find_candidates("cardinals", None, None)
    res = await market.open_market(ALICE, game)
    [bet] = await repo.list_bets(ALICE)
    assert res.kind == "failed" and bet.status == "failed"
    assert (await chain.get_bet(bet.id)).state == "refunded"


async def test_link_text_failure_keeps_the_draft_and_confirming_again_resends(chat, extractor, repo, fake_gateway):
    import httpx as _h

    fake_gateway.responder = lambda body: _h.Response(200, json={"ok": False, "code": "UNREACHABLE", "detail": "down"})
    sends = await confirm_cardinals(chat, extractor)
    assert sends and sends[0]["idempotencyKey"].endswith(":link")
    [bet] = await repo.list_bets(ALICE)
    assert bet.status == "open"
    assert [(n["status"], n["attempts"]) for n in await repo.list_notices(bet.id)] == [("failed", 1)]
    assert (await repo.get_draft(ALICE)) is not None  # still waiting: YES again resends
    fake_gateway.responder = lambda body: _h.Response(200, json={"ok": True, "messageId": "x"})
    [again] = await chat.say("yes")
    assert again["text"].startswith("You already have a bet open on that game.")


def test_no_hyphens_in_any_user_facing_template(market, repo):
    import datetime as dt

    from app.repository import Bet

    now = dt.datetime(2026, 11, 15, 21, 5, tzinfo=dt.timezone.utc)
    bet = Bet(
        id="i", proposer_handle="h", game_id="g", league="nfl", home_id="h", home_name="Arizona Cardinals",
        away_id="a", away_name="Seattle Seahawks", kickoff_utc=now, status="open", created_at=now,
        fee_bps=500, min_stake_base=1_000_000, max_stake_base=20_000_000, funding_closes_at=now,
    )
    texts = [
        v for k, v in vars(T).items()
        if k.isupper() and isinstance(v, str) and k not in ("EXAMPLE",)
    ]
    texts += [
        T.market_open(bet, "America/Phoenix", 2, "LINK"), T.market_exists(bet, "America/Phoenix", 2, "LINK"),
        T.market_locked(bet, (30_000_000, 39_500_000)), T.market_refunded(bet), T.funding_closed(60),
        T.fee_line(500), T.fee_line(0), T.fee_line(250),
    ]
    assert all("-" not in t for t in texts), [t for t in texts if "-" in t]


async def test_notice_table_without_a_status_default_still_works(chat, extractor, repo):
    """Regression from the live check: a bet_notices table whose status column has no default must not break opening."""
    async with repo.pool.connection() as conn:
        await conn.execute("alter table bet_notices alter column status drop default")
    sends = await confirm_cardinals(chat, extractor)
    [bet] = await repo.list_bets(ALICE)
    assert bet.status == "open" and sends[0]["text"].startswith("Your bet is open:")
    assert [(n["kind"], n["status"], n["attempts"]) for n in await repo.list_notices(bet.id)] == [("link", "sent", 1)]
    async with repo.pool.connection() as conn:
        row = await (await conn.execute("select sent_at is not null from bet_notices where bet_id = %s", (bet.id,))).fetchone()
    assert row[0] is True


async def test_proposer_is_still_told_when_the_notice_record_cannot_be_written(chat, extractor, repo, fake_gateway, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("bet_notices unavailable")

    monkeypatch.setattr(repo, "ensure_notice", broken)
    extractor.push(team="cardinals")
    await chat.say("bet on the cardinals")
    sends = await chat.say("yes")
    [bet] = await repo.list_bets(ALICE)
    assert bet.status == "failed" and bet.failure_reason.startswith("open_failed")
    assert [(s["text"], s["idempotencyKey"]) for s in sends] == [(T.MARKET_FAILED, f"bet:{bet.id}:failed")]  # never silent
