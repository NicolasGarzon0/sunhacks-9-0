from __future__ import annotations

from datetime import date
from typing import Protocol

from .models import Game, GameResult


class ProviderError(Exception):
    """Any lookup failure (network, rate limit, bad data). The flow replies with a fixed apology."""


class UnknownGame(ProviderError):
    pass


class GameProvider(Protocol):
    name: str

    async def find_candidates(self, team: str, opponent: str | None, day: date | None) -> list[Game]:
        """Upcoming scheduled games (kickoff after now) involving `team` (typed name), narrowed by `opponent`.
        With `day`, games on that local calendar day; without, now through +14 days. Soonest first.
        Unknown team or opponent -> []. Raises ProviderError on failure."""
        ...

    async def get_status(self, game_id: str) -> str: ...

    async def get_result(self, game_id: str) -> GameResult: ...
