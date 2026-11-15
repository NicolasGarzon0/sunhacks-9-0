"""Monthly budget for real sports API calls. Every cache miss that reaches the network is counted in provider_usage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable


class UsageMeter:
    def __init__(self, repo, budget: int, logger, now: Callable[[], datetime] | None = None) -> None:
        self.repo, self.budget, self.logger = repo, budget, logger
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._warned_month: str | None = None

    def month(self) -> str:
        return self.now().astimezone(timezone.utc).strftime("%Y-%m")

    async def reserve(self) -> bool:
        """Count one call. False means the budget is used up: the caller must not make the call."""
        month = self.month()
        if await self.repo.reserve_provider_call(month, self.budget):
            return True
        if self._warned_month != month:
            self._warned_month = month
            self.logger.error("rts.budget_exhausted", month=month, budget=self.budget)
        return False
