"""Team normalizer: typed names and aliases -> canonical team ids, from data/teams.json."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TEAMS_FILE = Path(__file__).resolve().parent / "data" / "teams.json"


def normalize(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    t = " ".join(t.split())
    if t.startswith("the "):
        t = t[4:]
    return t


def _variants(key: str) -> set[str]:
    """Allow a trailing 's' either way ("chief" <-> "chiefs") without fuzzy matching."""
    out = {key}
    if key.endswith("s") and len(key) > 3:
        out.add(key[:-1])
    else:
        out.add(key + "s")
    return out


@dataclass(frozen=True)
class Team:
    id: str
    league: str
    name: str


class TeamDirectory:
    def __init__(self, teams: list[Team], aliases: dict[str, set[str]]) -> None:
        self.teams = {t.id: t for t in teams}
        self._aliases = aliases  # normalized alias -> team ids

    @classmethod
    def from_file(cls, path: Path = DEFAULT_TEAMS_FILE) -> "TeamDirectory":
        return cls.from_data(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_data(cls, data: dict) -> "TeamDirectory":
        teams: list[Team] = []
        aliases: dict[str, set[str]] = {}
        for t in data["teams"]:
            team = Team(t["id"], t["league"], t["name"])
            teams.append(team)
            for a in [t["name"], *t.get("aliases", [])]:
                aliases.setdefault(normalize(a), set()).add(team.id)
        return cls(teams, aliases)

    def resolve(self, typed: str | None) -> set[str]:
        """Canonical ids for a typed name. Unknown -> empty set. Ambiguous ("arizona") -> several ids."""
        if not typed:
            return set()
        key = normalize(typed)
        if not key:
            return set()
        found: set[str] = set()
        for v in _variants(key):
            found |= self._aliases.get(v, set())
        return found

    def name(self, team_id: str) -> str:
        t = self.teams.get(team_id)
        return t.name if t else team_id
