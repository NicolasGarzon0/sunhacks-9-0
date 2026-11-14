"""Bet request flow end to end through POST /inbound, with the extractor and gateway mocked."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app import templates as T
from app.logging import Logger
from app.main import create_app

from .conftest import GATEWAY_SECRET, TODAY

ALICE = "+15550001111"
BOB = "+15550002222"
GW = {"X-Gateway-Secret": GATEWAY_SECRET}

GAME = dict(team="ASU", opponent="Texas Tech")
# Chunk 3: a valid request is looked up right away; the offer replaces the old readback.
READBACK = "I found Texas Tech at Arizona State, Saturday Nov 21, 7:30 PM MST. Is this the game? Reply YES or NO."
OFFER_CARDS = "I found Seattle Seahawks at Arizona Cardinals, Sunday Nov 15, 2:05 PM MST. Is this the game? Reply YES or NO."


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
        assert len(new) == 1, "exactly one reply per inbound message"
        assert new[0]["to"] == handle and new[0]["idempotencyKey"] == f"reply:m-{self.n}"
        return new[0]["text"]


@pytest.fixture
def chat(client, fake_gateway, roster):
    return Chat(client, fake_gateway)


async def test_complete_request_is_looked_up(chat, extractor, repo):
    extractor.push(**GAME)
    assert await chat.say("create a bet for asu vs texas tech") == READBACK
    d = await repo.get_draft(ALICE)
    assert d.status == "collecting" and d.pending_field == "confirm" and len(d.candidates) == 1
    assert d.fields == {"team": "ASU", "opponent": "Texas Tech", "date": None}
    assert extractor.calls[0]["today"] == TODAY and extractor.calls[0]["tz"] == "America/Phoenix"
    assert extractor.calls[0]["pending_field"] is None


async def test_no_game_asks_then_pending_answer_is_merged(chat, extractor, repo):
    extractor.push()  # "create a bet": bet_proposal with nothing
    assert await chat.say("create a bet") == T.ASK_GAME
    d = await repo.get_draft(ALICE)
    assert d.status == "collecting" and d.pending_field == "team"
    extractor.push("reply_to_pending", **GAME)
    assert await chat.say("ASU vs Texas Tech") == READBACK
    assert extractor.calls[1]["pending_field"] == "team"
    assert (await repo.get_draft(ALICE)).pending_field == "confirm"


async def test_pending_answer_keeps_earlier_date(chat, extractor):
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    extractor.push(date=tomorrow)  # "make a bet for tomorrow"
    assert await chat.say() == T.ASK_GAME
    extractor.push("reply_to_pending", team="cardinals")
    assert await chat.say("cardinals") == OFFER_CARDS


async def test_team_only(chat, extractor):
    extractor.push(team="ASU")
    reply = await chat.say("bet on ASU")
    assert reply.startswith("I found these games:\n1. BYU at Arizona State, Tuesday Nov 17")
    assert reply.endswith("Reply with a number, or NO.")


async def test_relative_date_resolved_by_model(chat, extractor):
    extractor.push(team="cards", date=(TODAY + timedelta(days=1)).isoformat())
    assert await chat.say("make me a bet on the cards game tomorrow") == OFFER_CARDS


@pytest.mark.parametrize("delta", [-1, -7, 15])
async def test_date_outside_window_is_reasked(chat, extractor, repo, delta):
    extractor.push(**GAME, date=(TODAY + timedelta(days=delta)).isoformat())
    assert await chat.say() == "I can only look up games from today through Saturday Nov 28. What day is the game?"
    d = await repo.get_draft(ALICE)
    assert d.status == "collecting" and d.pending_field == "date" and d.fields["date"] is None
    assert d.fields["team"] == "ASU"
    extractor.push("reply_to_pending", date=(TODAY + timedelta(days=7)).isoformat())
    assert await chat.say("next saturday") == READBACK


async def test_phone_number_team_is_rejected(chat, extractor, fake_gateway):
    extractor.push(team="480-555-0199")
    assert await chat.say("bet with 480-555-0199") == T.ASK_GAME
    assert {s["to"] for s in fake_gateway.sends} == {ALICE}


async def test_replace_yes_while_collecting(chat, extractor, repo):
    extractor.push()
    assert await chat.say("new bet") == T.ASK_GAME
    extractor.push(team="UofA", opponent="ASU")
    assert await chat.say("bet on uofa vs asu") == T.REPLACE_ASK
    assert len(extractor.calls) == 2
    assert await chat.say("YES") == (
        "I found Arizona State at Arizona Wildcats, Friday Nov 27, 8:00 PM MST. Is this the game? Reply YES or NO."
    )
    assert len(extractor.calls) == 2  # YES/NO parsed without the model
    assert (await repo.get_draft(ALICE)).pending_field == "confirm"


async def test_replace_no_keeps_old_and_reasks(chat, extractor, repo):
    extractor.push()
    await chat.say("new bet")
    extractor.push(**GAME)
    assert await chat.say() == T.REPLACE_ASK
    assert await chat.say("no") == T.REPLACE_KEPT_PREFIX + T.ASK_GAME
    d = await repo.get_draft(ALICE)
    assert d.status == "collecting" and d.pending_replacement is None and d.pending_field == "team"


async def test_replace_unclear_answer(chat, extractor):
    extractor.push()
    await chat.say()
    extractor.push(**GAME)
    await chat.say()
    assert await chat.say("maybe?") == T.REPLACE_UNCLEAR
    assert len(extractor.calls) == 2


async def test_confirmed_bet_lets_next_request_start_fresh(chat, extractor, repo):
    extractor.push(**GAME)
    assert await chat.say() == READBACK
    assert (await chat.say("yes")).startswith("Game confirmed:")
    extractor.push(team="cardinals")
    assert await chat.say() == OFFER_CARDS  # no replace question: the confirmed bet is not a draft


async def test_draft_expires_after_30_minutes(chat, extractor, repo, clock):
    extractor.push()
    await chat.say("create a bet")
    clock.current += timedelta(minutes=31)
    extractor.push(**GAME)
    # A new request after expiry starts fresh instead of asking to replace.
    assert await chat.say() == READBACK
    assert extractor.calls[1]["pending_field"] is None


async def test_expired_draft_is_deleted(chat, extractor, repo, clock):
    extractor.push()
    await chat.say("create a bet")
    clock.current += timedelta(minutes=31)
    extractor.push("other")
    assert await chat.say("hey") == T.HELP
    assert await repo.get_draft(ALICE) is None


async def test_draft_within_30_minutes_is_kept(chat, extractor, clock):
    extractor.push()
    await chat.say()
    clock.current += timedelta(minutes=29)
    extractor.push("reply_to_pending", **GAME)
    assert await chat.say() == READBACK


async def test_extraction_failure_falls_back(chat, extractor, repo):
    extractor.fail()
    assert await chat.say() == T.FALLBACK
    assert await repo.get_draft(ALICE) is None


async def test_extractor_exception_does_not_crash(chat, extractor, logs):
    extractor.queue.append(RuntimeError("boom"))
    r = await chat.client.post(
        "/inbound", json={"messageId": "x-1", "handle": ALICE, "text": "hi", "receivedAt": "2026-11-14T19:00:00Z"}, headers=GW
    )
    assert r.status_code == 200
    assert logs.events("handler.error")


async def test_chatter_gets_help(chat, extractor):
    extractor.push("other")
    assert await chat.say("lol did you see the game") == T.HELP


async def test_chatter_with_pending_request_reasks(chat, extractor):
    extractor.push()
    await chat.say("create a bet")
    extractor.push("other")
    assert await chat.say("lol") == T.ASK_GAME


async def test_reply_without_draft_gets_help(chat, extractor):
    extractor.push("reply_to_pending", **GAME)
    assert await chat.say("ASU vs Texas Tech") == T.HELP


async def test_input_length_cap_skips_model(chat, extractor):
    assert await chat.say("x" * 501) == T.TOO_LONG
    assert extractor.calls == []


async def test_only_the_sender_is_ever_texted(chat, extractor, fake_gateway):
    extractor.push(**GAME)
    await chat.say(handle=ALICE)
    extractor.push()
    await chat.say(handle=BOB)
    extractor.push("other")
    await chat.say(handle=ALICE)
    assert [s["to"] for s in fake_gateway.sends] == [ALICE, BOB, ALICE]


async def test_drafts_are_per_sender(chat, extractor, repo):
    extractor.push()
    await chat.say("create a bet", handle=ALICE)
    extractor.push(**GAME)
    assert await chat.say(handle=BOB) == READBACK  # Bob is not asked to replace Alice's draft
    assert (await repo.get_draft(ALICE)).status == "collecting"


async def test_logs_have_no_field_values_at_info(roster, extractor, settings, repo, gateway):
    lines: list[str] = []
    app = create_app(settings, repo=repo, gateway=gateway, logger=Logger("info", write=lines.append), extractor=extractor)
    extractor.push(team="Wildcats", opponent="Sun Devils")  # resolves to UofA vs ASU
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/inbound", json={"messageId": "q", "handle": ALICE, "text": "secret bet text", "receivedAt": "2026-11-14T19:00:00Z"}, headers=GW)
    blob = "\n".join(lines)
    assert "Wildcats" not in blob and "Sun Devils" not in blob and "secret bet text" not in blob
    assert ALICE not in blob and "bet.validated" in blob
