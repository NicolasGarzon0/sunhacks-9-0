"""RealtimeSportsProvider: NFL games from realtimesportsapi.com (free plan, REST only).

Response shapes verified against live responses on 2026-11-14 (fixtures in tests/fixtures/realtime/):
- Envelope {success, data, meta}; meta.rateLimit = {limit, remaining, reset (epoch ms)}.
- Game: id (str), date "2026-11-15T21:05Z", homeTeam/awayTeam {id, name, abbreviation, score},
  status {state "pre"|"post"|..., completed (absent in the schedule list), detail "Final" | "Sun, ... EST"}.
- Teams: {id, abbreviation, displayName, shortDisplayName, name, nickname, location, slug}.

Status mapping (VERIFIED: pre, post/Final; UNVERIFIED until a live sample: "in", postponed, cancelled):
  detail contains "postpon" -> postponed; detail contains "cancel" -> cancelled;
  state "in" -> in_progress; state "post" and (completed or detail starts with "final") -> final;
  state "pre" -> scheduled; anything else -> scheduled (lookups still drop games whose kickoff passed).
A "final" here is PROVISIONAL: the provider can correct it later (chunk 5 must re-read before settling).

Free plan: 1,000 calls in the first month, then 125/month, 1 call/second. Teams/weeks/schedules are cached
(6 h; weeks 24 h), calls are spaced >= 1.1 s, the remaining quota from meta.rateLimit is logged on every call,
and a 429 blocks further calls until Retry-After passes (lookups get the fixed apology meanwhile).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from app.logging import Logger, err_detail

from .models import Game, GameResult, TeamRef
from .provider import ProviderError, UnknownGame
from .teams import DEFAULT_TEAMS_FILE, TeamDirectory

BASE_URL = "https://www.realtimesportsapi.com/api/v1"
LEAGUE_PATH = "/sports/football/leagues/nfl"
NICKNAMES_FILE = Path(__file__).resolve().parent / "data" / "nfl_nicknames.json"
CACHE_TTL = 6 * 3600
WEEKS_TTL = 24 * 3600
EVENT_TTL = 60  # get_status/get_result: never poll the API faster than this per game
MIN_SPACING = 1.1
LOOKAHEAD = timedelta(days=14)
DEFAULT_RETRY_AFTER = 60.0
_NOT_FOUND = object()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def map_status(status: dict | None) -> str:
    s = status or {}
    state = str(s.get("state") or "").lower()
    detail = str(s.get("detail") or "").lower()
    if "postpon" in detail:
        return "postponed"
    if "cancel" in detail:
        return "cancelled"
    if state == "in":
        return "in_progress"
    if state == "post" and (s.get("completed") is True or detail.startswith("final")):
        return "final"
    return "scheduled"


def team_id(abbreviation: str) -> str:
    return f"nfl-{abbreviation.lower()}"


def build_directory(api_teams: list[dict], nicknames: dict[str, list[str]], hand_teams: dict) -> TeamDirectory:
    """Alias table = API names/cities/abbreviations + hand nicknames + nfl-* aliases from teams.json."""
    hand_aliases: dict[str, list[str]] = {}
    for t in hand_teams.get("teams", []):
        if t.get("league") == "nfl":
            hand_aliases[t["id"]] = t.get("aliases", [])
    teams = []
    for t in api_teams:
        abbr = t["abbreviation"]
        tid = team_id(abbr)
        aliases = [
            t.get("displayName"), t.get("shortDisplayName"), t.get("name"), t.get("nickname"),
            t.get("location"), abbr, (t.get("slug") or "").replace("-", " "),
            *nicknames.get(abbr, []), *hand_aliases.get(tid, []),
        ]
        teams.append({"id": tid, "league": "nfl", "name": t.get("displayName") or abbr,
                      "aliases": [a for a in aliases if a]})
    return TeamDirectory.from_data({"teams": teams})


class RealtimeSportsProvider:
    name = "realtime"

    def __init__(
        self,
        api_key: str,
        tz: str,
        logger: Logger,
        *,
        now: Callable[[], datetime] | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        base_url: str = BASE_URL,
        meter=None,
    ) -> None:
        self._key = api_key
        self.tz = tz
        self.logger = logger
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._client = client or httpx.AsyncClient(timeout=10)
        self._sleep = sleep
        self._mono = monotonic
        self.base_url = base_url.rstrip("/")
        self.meter = meter  # UsageMeter: monthly call budget (None in tests that do not need it)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()  # one call at a time: spacing + no duplicate fetches
        self._last_call = -1e9
        self._blocked_until = 0.0
        self.calls = 0
        self.remaining: int | None = None
        self._teams: TeamDirectory | None = None
        self._teams_at = -1e9

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- HTTP with cache, spacing, quota tracking and 429 handling ---

    async def _get(self, path: str, params: dict | None, ttl: float) -> Any:
        key = path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
        async with self._lock:
            hit = self._cache.get(key)
            if hit and self._mono() - hit[0] < ttl:
                if hit[1] is _NOT_FOUND:
                    raise UnknownGame(path)
                return hit[1]
            if self._mono() < self._blocked_until:
                raise ProviderError("rate limited (waiting for Retry-After)")
            if self.meter is not None and not await self.meter.reserve():
                raise ProviderError("sports call budget used up for this month")
            wait = MIN_SPACING - (self._mono() - self._last_call)
            if wait > 0:
                await self._sleep(wait)
            self._last_call = self._mono()
            self.calls += 1
            try:
                r = await self._client.get(
                    self.base_url + path, params=params,
                    headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
                )
            except Exception as e:
                self.logger.warn("rts.call_failed", path=path, detail=err_detail(e))
                raise ProviderError(f"network error: {type(e).__name__}") from e
            if r.status_code == 429:
                try:
                    retry = float(r.headers.get("retry-after", DEFAULT_RETRY_AFTER))
                except ValueError:
                    retry = DEFAULT_RETRY_AFTER
                self._blocked_until = self._mono() + retry
                self.logger.warn("rts.rate_limited", path=path, retryAfter=retry, calls=self.calls)
                raise ProviderError("rate limited")
            try:
                body = r.json()
            except ValueError:
                body = None
            meta_rl = (body or {}).get("meta", {}).get("rateLimit", {}) if isinstance(body, dict) else {}
            if "remaining" in meta_rl:
                self.remaining = meta_rl["remaining"]
            self.logger.info("rts.call", path=path, status=r.status_code, calls=self.calls,
                             quotaRemaining=self.remaining, quotaLimit=meta_rl.get("limit"))
            if r.status_code == 404:
                self._cache[key] = (self._mono(), _NOT_FOUND)  # negative cache: do not pay for it again
                raise UnknownGame(path)
            if r.status_code != 200 or not isinstance(body, dict) or body.get("success") is not True:
                raise ProviderError(f"bad response: HTTP {r.status_code}")
            data = body.get("data")
            self._cache[key] = (self._mono(), data)
            return data

    # --- teams ---

    async def teams(self) -> TeamDirectory:
        if self._teams is None or self._mono() - self._teams_at >= CACHE_TTL:
            api_teams = await self._get(f"{LEAGUE_PATH}/teams", None, CACHE_TTL)
            nick = json.loads(NICKNAMES_FILE.read_text(encoding="utf-8"))["nicknames"]
            hand = json.loads(DEFAULT_TEAMS_FILE.read_text(encoding="utf-8"))
            self._teams = build_directory(api_teams or [], nick, hand)
            self._teams_at = self._mono()
        return self._teams

    # --- schedule ---

    def _to_game(self, g: dict, teams: TeamDirectory) -> Game:
        def ref(side: dict) -> TeamRef:
            tid = team_id(side["abbreviation"])
            return TeamRef(tid, teams.teams[tid].name if tid in teams.teams else side.get("name", tid))

        return Game(
            game_id=f"rts:nfl:{g['id']}",
            league="nfl",
            home=ref(g["homeTeam"]),
            away=ref(g["awayTeam"]),
            kickoff_utc=parse_ts(g["date"]),
            status=map_status(g.get("status")),
        )

    async def _weeks_overlapping(self, start: datetime, end: datetime) -> list[dict]:
        local = start.astimezone(ZoneInfo(self.tz))
        # The NFL season "year" spans into January/February: only look at last season early in the year.
        years = [local.year - 1, local.year] if local.month <= 3 else [local.year]
        weeks: list[dict] = []
        for y in years:
            try:
                weeks += [dict(w, year=y) for w in await self._get(f"{LEAGUE_PATH}/seasons/{y}/weeks", None, WEEKS_TTL) or []]
            except UnknownGame:
                continue
        return [w for w in weeks if parse_ts(w["startDate"]) < end and parse_ts(w["endDate"]) > start]

    async def find_candidates(self, team: str, opponent: str | None, day: date | None) -> list[Game]:
        teams = await self.teams()
        team_ids = teams.resolve(team)
        if not team_ids:
            return []
        opp_ids = teams.resolve(opponent) if opponent else set()
        if opponent and not opp_ids:
            return []
        now = self.now()
        if day is not None:
            zone = ZoneInfo(self.tz)
            start = max(now, datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc))
            end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        else:
            start, end = now, now + LOOKAHEAD
        games: dict[str, Game] = {}
        for w in await self._weeks_overlapping(start, end):
            raw = await self._get(
                f"{LEAGUE_PATH}/seasons/{w['year']}/schedule",
                {"week": w["number"], "seasonType": w["seasonType"]},
                CACHE_TTL,
            )
            for g in raw or []:
                try:
                    game = self._to_game(g, teams)
                except (KeyError, ValueError, TypeError):
                    self.logger.warn("rts.bad_game", gameId=str(g.get("id")))
                    continue
                games[game.game_id] = game
        out = [
            g for g in games.values()
            if g.status == "scheduled" and start < g.kickoff_utc <= end and ({g.home.id, g.away.id} & team_ids)
        ]
        if opp_ids:
            out = [
                g for g in out
                if (g.home.id in team_ids and g.away.id in opp_ids) or (g.away.id in team_ids and g.home.id in opp_ids)
            ]
        return sorted(out, key=lambda g: (g.kickoff_utc, g.game_id))

    # --- single game ---

    async def _event(self, game_id: str) -> dict:
        prefix = "rts:nfl:"
        if not game_id.startswith(prefix):
            raise UnknownGame(game_id)
        return await self._get(f"{LEAGUE_PATH}/events/{game_id[len(prefix):]}", None, EVENT_TTL)

    async def get_status(self, game_id: str) -> str:
        return map_status((await self._event(game_id)).get("status"))

    async def get_result(self, game_id: str) -> GameResult:
        e = await self._event(game_id)
        status = map_status(e.get("status"))
        return GameResult(
            game_id=game_id,
            status=status,
            home_score=e.get("homeTeam", {}).get("score"),
            away_score=e.get("awayTeam", {}).get("score"),
            official=status == "final",  # provisional: chunk 5 re-reads before settling
        )
