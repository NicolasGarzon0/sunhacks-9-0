from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

STATUSES = ("scheduled", "in_progress", "final", "postponed", "cancelled")


@dataclass(frozen=True)
class TeamRef:
    id: str  # canonical, e.g. "nfl-ari"
    name: str  # display name, e.g. "Arizona Cardinals"


@dataclass(frozen=True)
class Game:
    game_id: str  # canonical and stable, e.g. "sim:nfl-buf-kc"
    league: str
    home: TeamRef
    away: TeamRef
    kickoff_utc: datetime
    status: str  # one of STATUSES

    def to_json(self) -> dict:
        d = asdict(self)
        d["kickoff_utc"] = self.kickoff_utc.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Game":
        return cls(
            game_id=d["game_id"],
            league=d["league"],
            home=TeamRef(**d["home"]),
            away=TeamRef(**d["away"]),
            kickoff_utc=datetime.fromisoformat(d["kickoff_utc"]),
            status=d["status"],
        )


@dataclass(frozen=True)
class GameResult:
    game_id: str
    status: str
    home_score: int | None
    away_score: int | None
    official: bool  # True only when the provider reports the result as final/official
