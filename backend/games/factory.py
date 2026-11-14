from __future__ import annotations

from .provider import GameProvider
from .simulated import SimulatedProvider
from .teams import TeamDirectory


def build_provider(settings, repo, logger, *, now=None) -> GameProvider:
    if settings.game_provider == "realtime":
        from .realtime import RealtimeSportsProvider  # stretch; imported lazily

        return RealtimeSportsProvider(settings.sports_api_key, settings.local_timezone, logger, now=now)
    return SimulatedProvider(repo, TeamDirectory.from_file(), settings.local_timezone, now=now)
