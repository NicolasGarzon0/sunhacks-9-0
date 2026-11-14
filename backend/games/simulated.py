"""SimulatedProvider: a fixed schedule (data/schedule.json) anchored to today, with state persisted in the
sim_games table so a restart keeps finished/postponed games."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .models import Game, GameResult, TeamRef
from .provider import ProviderError, UnknownGame
from .teams import TeamDirectory

DEFAULT_SCHEDULE_FILE = Path(__file__).resolve().parent / "data" / "schedule.json"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LOOKAHEAD = timedelta(days=14)


def schedule_rows(schedule: dict, teams: TeamDirectory, today: date, tz: str) -> list[dict]:
    """Expand schedule entries to concrete sim_games rows with UTC kickoffs."""
    zone = ZoneInfo(tz)
    rows = []
    for g in schedule["games"]:
        if "weekday" in g:
            delta = (WEEKDAYS.index(g["weekday"].lower()) - today.weekday()) % 7 or 7
            day = today + timedelta(days=delta)
        else:
            day = today + timedelta(days=int(g.get("day_offset", 0)))
        hh, mm = (int(x) for x in g["local_time"].split(":"))
        kickoff = datetime.combine(day, time(hh, mm), tzinfo=zone).astimezone(timezone.utc)
        for side in ("home", "away"):
            if g[side] not in teams.teams:
                raise ValueError(f"schedule entry {g['key']!r}: unknown team id {g[side]!r}")
        rows.append(
            {
                "game_id": f"sim:{g['key']}",
                "league": g["league"],
                "home_id": g["home"],
                "home_name": teams.name(g["home"]),
                "away_id": g["away"],
                "away_name": teams.name(g["away"]),
                "kickoff_utc": kickoff,
            }
        )
    return rows


def row_to_game(r: dict) -> Game:
    return Game(
        game_id=r["game_id"],
        league=r["league"],
        home=TeamRef(r["home_id"], r["home_name"]),
        away=TeamRef(r["away_id"], r["away_name"]),
        kickoff_utc=r["kickoff_utc"],
        status=r["status"],
    )


class SimulatedProvider:
    name = "simulated"

    def __init__(
        self,
        repo,
        teams: TeamDirectory,
        tz: str,
        *,
        now: Callable[[], datetime] | None = None,
        schedule_file: Path = DEFAULT_SCHEDULE_FILE,
    ) -> None:
        self.repo = repo
        self.teams = teams
        self.tz = tz
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.schedule_file = schedule_file

    def _today(self) -> date:
        return self.now().astimezone(ZoneInfo(self.tz)).date()

    def _rows(self) -> list[dict]:
        schedule = json.loads(self.schedule_file.read_text(encoding="utf-8"))
        return schedule_rows(schedule, self.teams, self._today(), self.tz)

    async def seed_if_empty(self) -> bool:
        if await self.repo.count_sim_games() > 0:
            return False
        await self.repo.replace_sim_games(self._rows())
        return True

    async def reset(self) -> int:
        rows = self._rows()
        await self.repo.replace_sim_games(rows)
        return len(rows)

    async def find_candidates(self, team: str, opponent: str | None, day: date | None) -> list[Game]:
        team_ids = self.teams.resolve(team)
        if not team_ids:
            return []
        opp_ids = self.teams.resolve(opponent) if opponent else set()
        if opponent and not opp_ids:
            return []
        now = self.now()
        if day is not None:
            zone = ZoneInfo(self.tz)
            start = max(now, datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc))
            end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        else:
            start, end = now, now + LOOKAHEAD
        try:
            rows = await self.repo.find_sim_games(sorted(team_ids), start, end)
        except Exception as e:
            raise ProviderError(f"simulated schedule unavailable: {type(e).__name__}") from e
        games = [row_to_game(r) for r in rows]
        if opp_ids:
            games = [
                g for g in games
                if (g.home.id in team_ids and g.away.id in opp_ids) or (g.away.id in team_ids and g.home.id in opp_ids)
            ]
        return games

    async def _row(self, game_id: str) -> dict:
        try:
            r = await self.repo.get_sim_game(game_id)
        except Exception as e:
            raise ProviderError(f"simulated schedule unavailable: {type(e).__name__}") from e
        if r is None:
            raise UnknownGame(game_id)
        return r

    async def get_status(self, game_id: str) -> str:
        return (await self._row(game_id))["status"]

    async def get_result(self, game_id: str) -> GameResult:
        r = await self._row(game_id)
        return GameResult(
            game_id=game_id,
            status=r["status"],
            home_score=r["home_score"],
            away_score=r["away_score"],
            official=bool(r["official"]) and r["status"] == "final",
        )
