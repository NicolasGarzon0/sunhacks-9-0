"""Lookup + confirmation through POST /inbound (extractor, gateway mocked; simulated provider on a test schema)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import templates as T
from games.provider import ProviderError

from .conftest import ADMIN_SECRET, GATEWAY_SECRET, TODAY

ALICE = "+15550001111"
BOB = "+15550002222"
GW = {"X-Gateway-Secret": GATEWAY_SECRET}
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}

OFFER_CARDS = "I found Seattle Seahawks at Arizona Cardinals, Sunday Nov 15, 2:05 PM MST. Is this the game? Reply YES or NO."
OPENED_CARDS = (  # the market link text (chunk 4); the rest of it is covered in test_open_market
    "Your bet is open: Seattle Seahawks at Arizona Cardinals, Sunday Nov 15, 2:05 PM MST.\n"
    "Betting closes Sunday Nov 15, 1:05 PM MST.\n"
)
EXISTING_CARDS = "You already have a bet open on that game.\n" + OPENED_CARDS
OFFER_ASU = (
    "I found these games:\n"
    "1. BYU at Arizona State, Tuesday Nov 17, 5:00 PM MST\n"
    "2. Texas Tech at Arizona State, Saturday Nov 21, 7:30 PM MST\n"
    "3. Arizona State at Arizona Wildcats, Friday Nov 27, 8:00 PM MST\n"
    "Reply with a number, or NO."
)


@pytest.fixture
async def roster(repo):
    await repo.upsert_roster("alice", "Alice", ALICE)
    await repo.upsert_roster("bob", "Bob", BOB)


class Chat:
    def __init__(self, client, fake_gateway):
        self.client, self.gw, self.n = client, fake_gateway, 0

    async def say(self, text: str = "msg", handle: str = ALICE) -> str:
        self.n += 1
        before = len(self.gw.sends)
        r = await self.client.post(
            "/inbound",
            json={"messageId": f"m-{self.n}", "handle": handle, "text": text, "receivedAt": "2026-11-14T19:00:00Z"},
            headers=GW,
        )
        assert r.status_code == 200
        new = self.gw.sends[before:]
        assert len(new) == 1 and new[0]["to"] == handle
        return new[0]["text"]


@pytest.fixture
def chat(client, fake_gateway, roster):
    return Chat(client, fake_gateway)


async def test_one_candidate_yes_stores_bet_and_deletes_draft(chat, extractor, repo):
    extractor.push(team="cardinals")
    assert await chat.say("create a bet for the cardinals") == OFFER_CARDS
    assert (await chat.say("YES")).startswith(OPENED_CARDS)
    assert len(extractor.calls) == 1  # confirmation parsed without the model
    assert await repo.get_draft(ALICE) is None
    [bet] = await repo.list_bets(ALICE)
    assert (bet.game_id, bet.status, bet.home_id, bet.away_name) == (
        "sim:t-sea-ari", "open", "nfl-ari", "Seattle Seahawks"
    )
    assert bet.kickoff_utc == datetime(2026, 11, 15, 21, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("word", ["y", "yep", "yeah", "Yes!", "1", "ok", "Okay", "yup", "ya", "sure"])
async def test_yes_variants(chat, extractor, word):
    extractor.push(team="cardinals")
    await chat.say()
    assert (await chat.say(word)).startswith(OPENED_CARDS)


async def test_several_candidates_pick_by_number(chat, extractor, repo):
    extractor.push(team="asu")
    assert await chat.say("bet on asu") == OFFER_ASU
    assert (await chat.say("2")).startswith(
        "Your bet is open: Texas Tech at Arizona State, Saturday Nov 21, 7:30 PM MST.\n"
    )
    assert [b.game_id for b in await repo.list_bets(ALICE)] == ["sim:t-ttu-asu"]


async def test_yes_with_several_candidates_is_ambiguous(chat, extractor, repo):
    extractor.push(team="asu")
    await chat.say()
    extractor.push("other")
    assert await chat.say("yes") == OFFER_ASU  # never advance on an ambiguous answer
    assert await repo.list_bets(ALICE) == []


async def test_out_of_range_number_is_a_miss(chat, extractor, repo):
    extractor.push(team="asu")
    await chat.say()
    extractor.push("other")
    assert await chat.say("7") == OFFER_ASU
    assert (await repo.get_draft(ALICE)).confirm_misses == 1


async def test_ambiguous_team_lists_all_matches(chat, extractor):
    extractor.push(team="arizona")  # Cardinals, ASU and UofA all match
    reply = await chat.say("bet on arizona")
    assert reply.startswith("I found these games:\n1. Seattle Seahawks at Arizona Cardinals")
    assert "4. Arizona State at Arizona Wildcats" in reply


async def test_cap_at_five_candidates(chat, extractor, repo, provider, monkeypatch):
    base = await provider.find_candidates("asu", None, None)
    many = [base[0]] * 2 + [base[1]] * 3 + [base[2]] * 2  # 7 games, unsorted-safe
    async def seven(*a, **k):
        return list(reversed(many))
    monkeypatch.setattr(provider, "find_candidates", seven)
    extractor.push(team="asu")
    reply = await chat.say()
    assert "5. " in reply and "6. " not in reply
    assert len((await repo.get_draft(ALICE)).candidates) == 5
    assert reply.index("1. BYU") < reply.index("5. Texas Tech")  # soonest first


async def test_soonest_first_when_no_date(chat, extractor):
    extractor.push(team="chiefs")
    reply = await chat.say()
    assert reply.index("Buffalo Bills at Kansas City Chiefs") < reply.index("Kansas City Chiefs at Denver Broncos")


async def test_date_narrows_to_one(chat, extractor):
    extractor.push(team="chiefs", date=(TODAY + timedelta(days=6)).isoformat())
    assert await chat.say("create a bet on the chiefs game friday") == (
        "I found Buffalo Bills at Kansas City Chiefs, Friday Nov 20, 6:15 PM MST. Is this the game? Reply YES or NO."
    )


async def test_opponent_narrows(chat, extractor):
    extractor.push(team="asu", opponent="texas tech")
    assert (await chat.say()).startswith("I found Texas Tech at Arizona State, Saturday Nov 21")


async def test_zero_candidates_returns_to_collecting(chat, extractor, repo):
    extractor.push(team="suns")
    assert await chat.say("bet on the suns") == "I could not find a game for suns in the next 14 days. Which team or day should I look for?"
    d = await repo.get_draft(ALICE)
    assert d.status == "collecting" and d.pending_field == "team" and d.candidates is None
    extractor.push("reply_to_pending", team="cardinals")
    assert await chat.say("cardinals") == OFFER_CARDS


async def test_new_request_after_dead_end_starts_fresh(chat, extractor, repo):
    extractor.push(team="suns")
    assert (await chat.say("bet on the suns")).startswith("I could not find a game for suns")
    extractor.push(team="chiefs", date=(TODAY + timedelta(days=6)).isoformat())
    # No "Replace it?": the failed search left nothing worth keeping.
    assert (await chat.say("create a bet on the chiefs game friday")).startswith("I found Buffalo Bills at Kansas City Chiefs")


async def test_new_request_after_apology_starts_fresh(chat, extractor, provider, monkeypatch):
    real = provider.find_candidates

    async def boom(*a, **k):
        raise ProviderError("down")

    monkeypatch.setattr(provider, "find_candidates", boom)
    extractor.push(team="cardinals")
    assert await chat.say() == T.LOOKUP_APOLOGY
    monkeypatch.setattr(provider, "find_candidates", real)
    extractor.push(team="asu", opponent="texas tech")
    assert (await chat.say()).startswith("I found Texas Tech at Arizona State")


async def test_dead_end_followup_answer_still_merges(chat, extractor):
    extractor.push(team="cardinals", date=(TODAY + timedelta(days=2)).isoformat())
    assert (await chat.say()).startswith("I could not find a game for cardinals on Monday")
    extractor.push("reply_to_pending", date=(TODAY + timedelta(days=1)).isoformat())
    assert await chat.say("sunday then") == OFFER_CARDS


async def test_zero_candidates_with_date(chat, extractor):
    extractor.push(team="cardinals", date=(TODAY + timedelta(days=2)).isoformat())
    assert await chat.say() == "I could not find a game for cardinals on Monday Nov 16. Which team or day should I look for?"


async def test_new_team_drops_stale_opponent(chat, extractor):
    extractor.push(team="asu", opponent="lakers")
    assert (await chat.say()).startswith("I could not find a game for asu")
    extractor.push("reply_to_pending", team="cardinals")
    assert await chat.say("cardinals then") == OFFER_CARDS


async def test_no_then_correction(chat, extractor, repo):
    extractor.push(team="asu")
    assert await chat.say() == OFFER_ASU
    assert await chat.say("nope") == T.CONFIRM_NO
    d = await repo.get_draft(ALICE)
    assert d.pending_field == "team" and d.candidates is None
    extractor.push("reply_to_pending", team="cardinals")
    assert await chat.say("the cardinals game") == OFFER_CARDS
    assert (await chat.say("yes")).startswith(OPENED_CARDS)


async def test_invalid_answer_twice_explains(chat, extractor, repo):
    extractor.push(team="cardinals")
    await chat.say()
    extractor.push("other")
    assert await chat.say("hmm maybe") == OFFER_CARDS
    extractor.push("other")
    assert await chat.say("what?") == T.offer_help(1) + "\n" + OFFER_CARDS
    extractor.push("other")
    assert (await chat.say("idk")).startswith("Please reply YES")
    assert await repo.list_bets(ALICE) == []
    assert (await chat.say("yes")).startswith(OPENED_CARDS)


async def test_model_failure_during_confirm_is_a_miss(chat, extractor):
    extractor.push(team="cardinals")
    await chat.say()
    extractor.fail()
    assert await chat.say("sure thing buddy") == OFFER_CARDS


async def test_new_request_while_waiting_asks_replace(chat, extractor, repo):
    extractor.push(team="cardinals")
    await chat.say()
    extractor.push(team="asu")
    assert await chat.say("actually bet on asu") == T.REPLACE_ASK
    assert await chat.say("YES") == OFFER_ASU
    assert len(extractor.calls) == 2


async def test_replace_no_while_waiting_reasks_offer(chat, extractor):
    extractor.push(team="cardinals")
    await chat.say()
    extractor.push(team="asu")
    await chat.say()
    assert await chat.say("no") == T.REPLACE_KEPT_PREFIX + OFFER_CARDS
    assert (await chat.say("yes")).startswith(OPENED_CARDS)


async def test_waiting_draft_expires(chat, extractor, repo, clock):
    extractor.push(team="cardinals")
    await chat.say()
    clock.current += timedelta(minutes=31)
    extractor.push("other")
    assert await chat.say("yes") == T.HELP  # the offer expired; "yes" is just chatter now
    assert await repo.get_draft(ALICE) is None and await repo.list_bets(ALICE) == []


async def test_game_started_before_confirmation(chat, extractor, repo, clock):
    extractor.push(team="ravens")  # today 18:00
    assert (await chat.say()).startswith("I found Baltimore Ravens at Pittsburgh Steelers, Saturday Nov 14, 6:00 PM MST")
    # Simulate kickoff passing while the offer is still fresh (< 30 min old).
    d = await repo.get_draft(ALICE)
    d.candidates[0]["kickoff_utc"] = (clock.current - timedelta(minutes=1)).isoformat()
    await repo.save_draft(d)
    assert await chat.say("yes") == T.GAME_STARTED
    assert await repo.list_bets(ALICE) == []
    assert (await repo.get_draft(ALICE)).pending_field == "team"


async def test_duplicate_confirmation_returns_existing_bet(chat, extractor, repo, logs):
    extractor.push(team="cardinals")
    await chat.say()
    await chat.say("yes")
    extractor.push(team="cardinals")
    await chat.say()
    assert (await chat.say("yes")).startswith(EXISTING_CARDS)
    bets = await repo.list_bets(ALICE)
    assert len(bets) == 1
    assert [e["outcome"] for e in logs.events("bet.confirmed")] == ["opened", "existing"]
    assert {e["betId"] for e in logs.events("market.opened")} == {bets[0].id}


async def test_bets_are_per_proposer(chat, extractor, repo):
    for who in (ALICE, BOB):
        extractor.push(team="cardinals")
        await chat.say(handle=who)
        await chat.say("yes", handle=who)
    assert len(await repo.list_bets(ALICE)) == 1 and len(await repo.list_bets(BOB)) == 1


async def test_provider_failure_sends_apology(chat, extractor, repo, provider, monkeypatch):
    async def boom(*a, **k):
        raise ProviderError("down")

    monkeypatch.setattr(provider, "find_candidates", boom)
    extractor.push(team="cardinals")
    assert await chat.say() == T.LOOKUP_APOLOGY
    d = await repo.get_draft(ALICE)
    assert d.status == "collecting" and d.pending_field == "team" and d.fields["team"] == "cardinals"
    assert d.fields["lookup_dead_end"] is True


async def test_only_sender_texted_through_whole_flow(chat, extractor, fake_gateway):
    extractor.push(team="asu")
    await chat.say()
    await chat.say("3")
    assert {s["to"] for s in fake_gateway.sends} == {ALICE}


# --- admin endpoints ---


async def test_admin_finish_status_reset(client, provider):
    r = await client.post("/admin/games/sim:t-bal-pit/finish", json={"home_score": 27, "away_score": 24})
    assert r.status_code == 401
    r = await client.post("/admin/games/sim:t-bal-pit/finish", json={"home_score": 27, "away_score": 24}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["result"] == {
        "game_id": "sim:t-bal-pit", "status": "final", "home_score": 27, "away_score": 24, "official": True
    }
    r = await client.post("/admin/games/sim:t-sea-ari/status", json={"status": "postponed"}, headers=ADMIN)
    assert r.json() == {"ok": True, "game_id": "sim:t-sea-ari", "status": "postponed"}
    assert await provider.get_status("sim:t-sea-ari") == "postponed"
    r = await client.post("/admin/games/sim:t-sea-ari/status", json={"status": "final"}, headers=ADMIN)
    assert r.status_code == 400  # final only via /finish with scores
    r = await client.post("/admin/games/sim:nope/finish", json={"home_score": 1, "away_score": 0}, headers=ADMIN)
    assert r.status_code == 404
    r = await client.post("/admin/games/sim:t-sea-ari/finish", json={"home_score": -1, "away_score": 0}, headers=ADMIN)
    assert r.status_code == 400
    r = await client.post("/admin/games/reset", headers=ADMIN)
    assert r.json() == {"ok": True, "games": 10}
    assert await provider.get_status("sim:t-sea-ari") == "scheduled"
    assert await provider.get_status("sim:t-bal-pit") == "scheduled"


async def test_admin_games_conflict_with_real_provider(settings, repo, gateway, logs, extractor):
    from app.main import create_app

    class FakeReal:
        name = "realtime"

    app = create_app(settings, repo=repo, gateway=gateway, logger=logs.logger, extractor=extractor, provider=FakeReal())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/games/reset", headers=ADMIN)
    assert r.status_code == 409
