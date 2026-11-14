"""RealtimeSportsProvider against saved real responses only (tests/fixtures/realtime/). Never calls the API.

Fixtures were fetched 2026-11-14 with scripts/fetch_real_samples.py. now = Sat 2026-11-14 19:00 UTC, which is
inside NFL 2026 week 10. Weeks 11/12 overlap the 14-day window; they are served as empty schedules (same
envelope as the real week 10 response) since only week 10 was fetched.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from games.provider import ProviderError, UnknownGame
from games.realtime import RealtimeSportsProvider, map_status

FIX = Path(__file__).resolve().parent / "fixtures" / "realtime"
NOW = datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc)
PREFIX = "/api/v1/sports/football/leagues/nfl"


def load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeAPI:
    def __init__(self):
        self.calls: list[str] = []
        self.override: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path + ("?" + request.url.query.decode() if request.url.query else ""))
        assert request.headers["authorization"] == "Bearer test-key"
        if path in self.override:
            return self.override[path]
        if path == f"{PREFIX}/teams":
            return httpx.Response(200, json=load("teams"))
        if path == f"{PREFIX}/seasons/2026/weeks":
            return httpx.Response(200, json=load("weeks_2026"))
        if path == f"{PREFIX}/seasons/2025/weeks":
            return httpx.Response(404, json={"success": False})
        if path == f"{PREFIX}/seasons/2026/schedule":
            if request.url.params["week"] == "10" and request.url.params["seasonType"] == "2":
                return httpx.Response(200, json=load("schedule_2026_w10"))
            empty = load("schedule_2026_w10")
            empty["data"] = []
            return httpx.Response(200, json=empty)
        if path == f"{PREFIX}/events/401873052":
            return httpx.Response(200, json=load("event_final"))
        if path == f"{PREFIX}/events/401873061":
            return httpx.Response(200, json=load("event_scheduled"))
        return httpx.Response(404, json={"success": False, "error": "not found"})


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def mono():
    return Mono()


@pytest.fixture
async def rts(api, logs, mono):
    async def no_sleep(s):
        mono.t += s

    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    p = RealtimeSportsProvider("test-key", "America/Phoenix", logs.logger, now=lambda: NOW, client=client,
                               sleep=no_sleep, monotonic=mono)
    yield p
    await client.aclose()


async def test_cardinals_from_real_schedule(rts):
    [g] = await rts.find_candidates("cardinals", None, None)
    assert g.game_id == "rts:nfl:401873061" and g.league == "nfl" and g.status == "scheduled"
    assert (g.away.id, g.away.name, g.home.id, g.home.name) == (
        "nfl-lar", "Los Angeles Rams", "nfl-ari", "Arizona Cardinals"
    )
    assert g.kickoff_utc == datetime(2026, 11, 15, 21, 5, tzinfo=timezone.utc)




async def test_aliases(rts):
    t = await rts.teams()
    assert t.resolve("cards") == {"nfl-ari"}  # hand nickname
    assert t.resolve("Arizona Cardinals") == {"nfl-ari"}  # API displayName
    assert t.resolve("ARI") == {"nfl-ari"}  # API abbreviation
    assert t.resolve("commanders") == {"nfl-wsh"}
    assert t.resolve("new york") == {"nfl-nyg", "nfl-nyj"}  # API location shared -> ambiguous
    assert t.resolve("los angeles") == {"nfl-lar", "nfl-lac"}
    assert t.resolve("asu") == set()  # college teams are not in the NFL provider
    assert len(t.teams) == 32


async def test_final_games_never_offered(rts):
    assert await rts.find_candidates("giants", None, None) == []  # WSH @ NYG is final
    assert await rts.find_candidates("commanders", None, None) == []


async def test_ambiguous_location_offers_both_and_drops_final(rts):
    games = await rts.find_candidates("new york", None, None)
    assert [g.game_id for g in games] == ["rts:nfl:401873054"]  # BUF @ NYJ; the Giants game is final


async def test_opponent_and_date(rts):
    assert [g.game_id for g in await rts.find_candidates("chiefs", "falcons", None)] == ["rts:nfl:401873058"]
    assert await rts.find_candidates("chiefs", "eagles", None) == []
    sunday = date(2026, 11, 15)
    assert [g.game_id for g in await rts.find_candidates("steelers", None, sunday)] == ["rts:nfl:401873064"]  # 6:20 PM MST
    assert await rts.find_candidates("ravens", None, sunday) == []  # Monday night game
    assert [g.game_id for g in await rts.find_candidates("ravens", None, date(2026, 11, 16))] == ["rts:nfl:401873065"]


async def test_unknown_team(rts):
    assert await rts.find_candidates("suns", None, None) == []


async def test_status_and_result_from_events(rts):
    assert await rts.get_status("rts:nfl:401873061") == "scheduled"
    assert await rts.get_status("rts:nfl:401873052") == "final"
    r = await rts.get_result("rts:nfl:401873052")
    assert (r.status, r.home_score, r.away_score, r.official) == ("final", 21, 24, True)
    with pytest.raises(UnknownGame):
        await rts.get_status("sim:nfl-sea-ari")


def test_status_mapping():
    assert map_status({"state": "pre", "completed": False, "detail": "Sun, November 15th at 4:05 PM EST"}) == "scheduled"
    assert map_status({"state": "post", "completed": True, "detail": "Final"}) == "final"
    assert map_status({"state": "post", "period": 4, "detail": "Final"}) == "final"  # schedule list omits completed
    assert map_status({"state": "post", "detail": "Final/OT"}) == "final"
    # Not yet seen live (mapping approved; verify with a live sample):
    assert map_status({"state": "in", "detail": "2nd Quarter"}) == "in_progress"
    assert map_status({"state": "post", "detail": "Postponed"}) == "postponed"
    assert map_status({"state": "post", "detail": "Canceled"}) == "cancelled"
    assert map_status({}) == "scheduled"


async def test_caching_limits_calls(rts, api, mono):
    await rts.find_candidates("cardinals", None, None)
    first = len(api.calls)
    # teams + weeks 2026 + 3 schedule weeks (10, 11, 12) overlap the 14-day window
    assert first == 5
    await rts.find_candidates("chiefs", None, None)
    await rts.find_candidates("eagles", None, None)
    assert len(api.calls) == first  # all cached
    mono.t += 6 * 3600 + 1
    await rts.find_candidates("cardinals", None, None)
    assert len(api.calls) > first  # schedule cache expired after 6 h


async def test_calls_are_spaced(rts, api, mono):
    times = []
    orig = rts._client.get

    async def timed(*a, **k):
        times.append(mono.t)
        return await orig(*a, **k)

    rts._client.get = timed
    await rts.find_candidates("cardinals", None, None)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and all(g >= 1.1 - 1e-9 for g in gaps)


async def test_rate_limit_counter_logged(rts, logs):
    await rts.get_status("rts:nfl:401873061")
    ev = logs.events("rts.call")[-1]
    assert ev["quotaRemaining"] == 995 and ev["quotaLimit"] == 1000 and ev["calls"] == 1
    assert "test-key" not in "\n".join(logs.lines)


async def test_429_honors_retry_after(rts, api, mono):
    api.override[f"{PREFIX}/teams"] = httpx.Response(429, headers={"Retry-After": "30"}, json={"success": False})
    with pytest.raises(ProviderError):
        await rts.find_candidates("cardinals", None, None)
    n = len(api.calls)
    with pytest.raises(ProviderError):
        await rts.find_candidates("cardinals", None, None)
    assert len(api.calls) == n  # blocked: no call before Retry-After
    del api.override[f"{PREFIX}/teams"]
    mono.t += 31
    assert len(await rts.find_candidates("cardinals", None, None)) == 1


async def test_server_error_is_provider_error(rts, api):
    api.override[f"{PREFIX}/teams"] = httpx.Response(500, text="oops")
    with pytest.raises(ProviderError):
        await rts.find_candidates("cardinals", None, None)


async def test_network_error_is_provider_error(logs, mono):
    def down(req):
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as c:
        p = RealtimeSportsProvider("test-key", "America/Phoenix", logs.logger, now=lambda: NOW, client=c,
                                   sleep=lambda s: _noop(), monotonic=mono)
        with pytest.raises(ProviderError):
            await p.find_candidates("cardinals", None, None)


async def _noop():
    return None
