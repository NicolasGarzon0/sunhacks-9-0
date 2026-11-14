"""Team normalizer and SimulatedProvider (DB-backed), with now = Sat 2026-11-14 12:00 America/Phoenix."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from games.provider import UnknownGame
from games.simulated import SimulatedProvider, schedule_rows
from games.teams import TeamDirectory, normalize

from .conftest import TEST_SCHEDULE, TODAY

TEAMS = TeamDirectory.from_file()


@pytest.mark.parametrize("typed", ["cards", "Cardinals", "arizona cardinals", "birds", "The Cardinals!", "cardinal"])
def test_cardinals_aliases(typed):
    assert TEAMS.resolve(typed) == {"nfl-ari"}


@pytest.mark.parametrize("typed", ["asu", "ASU", "sun devils", "Arizona State", "sun devil"])
def test_asu_aliases(typed):
    assert TEAMS.resolve(typed) == {"ncaaf-asu"}


def test_ambiguous_and_unknown():
    assert TEAMS.resolve("arizona") == {"nfl-ari", "ncaaf-asu", "ncaaf-ariz"}
    assert TEAMS.resolve("suns") == set()
    assert TEAMS.resolve("") == set() and TEAMS.resolve(None) == set()
    assert normalize("  The  Chiefs!! ") == "chiefs" and TEAMS.resolve("chief") == {"nfl-kc"}


def test_data_files_are_consistent():
    # Every real schedule entry references known teams; every alias list is non-empty.
    schedule = json.loads((TEST_SCHEDULE.parent.parent.parent / "games" / "data" / "schedule.json").read_text())
    rows = schedule_rows(schedule, TEAMS, TODAY, "America/Phoenix")
    assert rows and all(r["game_id"].startswith("sim:") for r in rows)
    assert len({r["game_id"] for r in rows}) == len(rows)


def test_weekday_and_offset_resolution():
    rows = {r["game_id"]: r for r in schedule_rows(json.loads(TEST_SCHEDULE.read_text()), TEAMS, TODAY, "America/Phoenix")}
    kc = rows["sim:t-buf-kc"]["kickoff_utc"]
    assert kc.isoformat() == "2026-11-21T01:15:00+00:00"  # Fri Nov 20 18:15 MST
    assert rows["sim:t-sea-ari"]["kickoff_utc"].isoformat() == "2026-11-15T21:05:00+00:00"


async def test_one_candidate(provider):
    games = await provider.find_candidates("cards", None, None)
    assert [g.game_id for g in games] == ["sim:t-sea-ari"]
    g = games[0]
    assert g.home.id == "nfl-ari" and g.home.name == "Arizona Cardinals" and g.away.name == "Seattle Seahawks"
    assert g.status == "scheduled" and g.league == "nfl"


async def test_several_candidates_soonest_first(provider):
    games = await provider.find_candidates("asu", None, None)
    assert [g.game_id for g in games] == ["sim:t-byu-asu", "sim:t-ttu-asu", "sim:t-asu-ariz"]


async def test_ambiguous_team_offers_all(provider):
    ids = {g.game_id for g in await provider.find_candidates("arizona", None, None)}
    assert ids == {"sim:t-sea-ari", "sim:t-byu-asu", "sim:t-ttu-asu", "sim:t-asu-ariz"}


async def test_unknown_team_or_opponent_gives_nothing(provider):
    assert await provider.find_candidates("suns", None, None) == []
    assert await provider.find_candidates("asu", "lakers", None) == []


async def test_opponent_narrows(provider):
    games = await provider.find_candidates("asu", "texas tech", None)
    assert [g.game_id for g in games] == ["sim:t-ttu-asu"]
    # Order of team/opponent does not matter.
    assert [g.game_id for g in await provider.find_candidates("ttu", "sun devils", None)] == ["sim:t-ttu-asu"]


async def test_date_filters_by_local_day(provider):
    fri = date(2026, 11, 20)
    assert [g.game_id for g in await provider.find_candidates("chiefs", None, fri)] == ["sim:t-buf-kc"]
    assert await provider.find_candidates("chiefs", None, fri + timedelta(days=1)) == []
    # No date: both Chiefs games in the 14-day window, soonest first.
    assert [g.game_id for g in await provider.find_candidates("chiefs", None, None)] == ["sim:t-buf-kc", "sim:t-kc-den"]


async def test_window_is_14_days(provider):
    assert await provider.find_candidates("49ers", None, None) == []  # t-sf-lar-far is 20 days out


async def test_started_and_finished_excluded(provider, repo):
    # Eagles/Cowboys today at 09:00 already started (now is 12:00); only the day-12 game is offered.
    assert [g.game_id for g in await provider.find_candidates("eagles", None, None)] == ["sim:t-phi-dal"]
    assert [g.game_id for g in await provider.find_candidates("eagles", None, TODAY)] == []
    await repo.finish_sim_game("sim:t-bal-pit", 20, 17)
    assert await provider.find_candidates("ravens", None, None) == []
    await repo.set_sim_game_status("sim:t-sea-ari", "postponed")
    assert await provider.find_candidates("cards", None, None) == []


async def test_status_and_result(provider, repo):
    assert await provider.get_status("sim:t-bal-pit") == "scheduled"
    r = await provider.get_result("sim:t-bal-pit")
    assert r.official is False and r.home_score is None
    await repo.finish_sim_game("sim:t-bal-pit", 24, 21)
    r = await provider.get_result("sim:t-bal-pit")
    assert (r.status, r.home_score, r.away_score, r.official) == ("final", 24, 21, True)
    with pytest.raises(UnknownGame):
        await provider.get_status("sim:nope")


async def test_state_persists_across_restart(provider, repo, clock):
    await repo.finish_sim_game("sim:t-bal-pit", 3, 0)
    restarted = SimulatedProvider(repo, TEAMS, "America/Phoenix", now=clock, schedule_file=TEST_SCHEDULE)
    assert await restarted.seed_if_empty() is False  # table not empty: no reseed
    assert await restarted.get_status("sim:t-bal-pit") == "final"
    assert await restarted.reset() == 10
    assert await restarted.get_status("sim:t-bal-pit") == "scheduled"
