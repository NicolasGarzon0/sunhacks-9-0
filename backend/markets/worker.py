"""The market lifecycle worker. One tick, in order:

  a) reconcile   the bets table with the chain (repairs a crash between a chain call and its database write)
  b) funding     refund cancelled or postponed games, close funding at the deadline, close early when full
  c) results     read the game result, confirm a final with two matching reads, settle or refund
  e) payouts     one claim per wallet owed money, with retries; payouts_completed_at when every task is done
  f) notices     send queued proposer texts, retrying transient failures

(Step d, settlement itself, is MarketService.settle_market.) Every step is conditional on the current status,
so a tick can run twice, or crash halfway, without changing anything the second time.
No model calls. Texts are fixed templates sent only to the proposer. Money is int base units.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.logging import Logger, err_detail
from app.repository import Bet, PayoutTask, Repository
from games.provider import GameProvider

from .chain import ChainError
from .service import MarketError, MarketService

LOCK_KEY = 0x4842_5F57_4B52  # "HB_WKR": one worker loop across all app instances
PAYOUT_CONCURRENCY = 3
PERMANENT_CLAIM_CODES = {"NOT_A_WINNER", "NOT_FOUND", "INVALID_WALLET"}


class Worker:
    def __init__(
        self,
        repo: Repository,
        market: MarketService,
        provider: GameProvider,
        settings,
        logger: Logger,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo, self.market, self.provider, self.settings, self.logger = repo, market, provider, settings, logger
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.last_tick: dict | None = None
        self._ticking = asyncio.Lock()  # one tick at a time inside this process

    # --- loop ---

    async def run_forever(self) -> None:
        self.logger.info("worker.started", tickSeconds=self.settings.worker_tick_seconds)
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # never let the loop die
                    self.logger.error("worker.tick_failed", detail=err_detail(e))
                await asyncio.sleep(self.settings.worker_tick_seconds)
        finally:
            self.logger.info("worker.stopped")

    async def tick(self) -> dict:
        async with self._ticking:
            started = self.now()
            try:
                async with self.repo.pool.connection() as conn:
                    async with conn.transaction():
                        row = await (await conn.execute("select pg_try_advisory_xact_lock(%s)", (LOCK_KEY,))).fetchone()
                        if not row[0]:
                            summary = {"skipped": "locked"}
                        else:
                            summary = await self._run_steps()
            except Exception as e:
                self.last_tick = {"at": started.isoformat(), "error": type(e).__name__}
                raise
            self.last_tick = {"at": started.isoformat(), **summary}
            return summary

    async def _run_steps(self) -> dict:
        counts: dict[str, int] = {}

        def bump(name: str, n: int = 1) -> None:
            counts[name] = counts.get(name, 0) + n

        for name, step in (
            ("reconcile", self._reconcile), ("funding", self._funding), ("results", self._results),
            ("payouts", self._payouts), ("notices", self._notices),
        ):
            try:
                await step(bump)
            except Exception as e:  # one failing step must not starve the others
                bump(f"{name}_errors")
                self.logger.error("worker.step_failed", step=name, detail=err_detail(e))
        return counts

    async def _each(self, bets: list[Bet], fn, bump, name: str) -> None:
        for bet in bets:
            try:
                await fn(bet)
            except Exception as e:  # one bad bet never stops the rest
                bump(f"{name}_errors")
                self.logger.error("worker.bet_failed", step=name, betId=bet.id, detail=err_detail(e))

    # --- a) reconcile ---

    async def _reconcile(self, bump) -> None:
        async def one(bet: Bet) -> None:
            try:
                cb = await self.market.chain.get_bet(bet.id)
            except ChainError as e:
                self.logger.warn("worker.chain_missing", betId=bet.id, code=e.code)
                return
            if bet.status == "open":
                if cb.state == "locked":
                    await self.market.close_funding(bet.id)
                    bump("repaired")
                elif cb.state == "refunded":
                    reason = bet.refund_reason or ("too_few_participants" if cb.participant_count < 2 else "one_sided")
                    await self.market.refund_market(bet.id, reason)
                    bump("repaired")
            elif bet.status == "locked":
                if cb.state == "settled" and cb.winning_side is not None:
                    await self.market.settle_market(bet.id, cb.winning_side)
                    bump("repaired")
                elif cb.state == "refunded":
                    await self.market.refund_market(bet.id, bet.refund_reason or "no_result")
                    bump("repaired")

        await self._each(await self.repo.bets_by_status("open", "locked"), one, bump, "reconcile")

        async def tasks(bet: Bet) -> None:
            bump("tasks_created", await self.market.ensure_payout_tasks(bet))

        await self._each(await self.repo.unpaid_closed_bets(), tasks, bump, "reconcile")

    # --- b) close funding ---

    async def _funding(self, bump) -> None:
        simulated = getattr(self.provider, "name", "") == "simulated"

        async def one(bet: Bet) -> None:
            now = self.now()
            due = bet.funding_closes_at is not None and bet.funding_closes_at <= now
            if simulated or due:  # the real provider is only asked once, at the deadline (call budget)
                try:
                    status = await self.provider.get_status(bet.game_id)
                except Exception as e:
                    self.logger.warn("worker.status_failed", betId=bet.id, detail=type(e).__name__)
                    status = "scheduled"
                if status in ("cancelled", "postponed"):
                    await self.market.refund_market(bet.id, status)
                    bump("refunded")
                    return
            if due:
                closed = await self.market.close_funding(bet.id)
                bump("closed_" + closed.status)
            else:
                await self.market.maybe_close_when_full(bet.id)

        await self._each(await self.repo.bets_by_status("open"), one, bump, "funding")

    # --- c) results ---

    async def _results(self, bump) -> None:
        s = self.settings
        poll_start = timedelta(hours=s.result_poll_start_hours_after_kickoff)
        interval = timedelta(minutes=s.result_poll_interval_minutes)

        async def one(bet: Bet) -> None:
            now = self.now()
            due = now >= bet.kickoff_utc + poll_start and (bet.last_polled_at is None or now >= bet.last_polled_at + interval)
            if due:
                await self._poll(bet, bump)
                bet = await self.repo.get_bet(bet.id)  # type: ignore[assignment]
            if bet.status == "locked" and bet.settle_after is not None and self.now() >= bet.settle_after:
                await self.market.refund_market(bet.id, "no_result")
                bump("refunded")

        await self._each(await self.repo.bets_by_status("locked"), one, bump, "results")

    async def _poll(self, bet: Bet, bump) -> None:
        now = self.now()
        await self.repo.mark_polled(bet.id, now)
        try:
            result = await self.provider.get_result(bet.game_id)
        except Exception as e:  # includes the call budget being used up: the settle_after rule refunds
            self.logger.warn("worker.result_unavailable", betId=bet.id, detail=err_detail(e))
            bump("poll_errors")
            return
        if result.status in ("cancelled", "postponed"):
            await self.market.refund_market(bet.id, result.status)
            bump("refunded")
            return
        if result.status != "final" or not result.official or result.home_score is None or result.away_score is None:
            await self.repo.clear_result_read(bet.id)  # a status that leaves final restarts the confirmation
            return
        home, away = result.home_score, result.away_score
        winner = None if home == away else (0 if away > home else 1)  # side 0 = away team, side 1 = home team
        same = (
            bet.result_first_seen_at is not None
            and (bet.result_home_score, bet.result_away_score) == (home, away)
        )
        first_seen = bet.result_first_seen_at if same else now
        if not same:  # first read, or the score changed: restart the wait
            await self.repo.record_result_read(bet.id, home, away, winner, now)
            bump("final_seen")
        if now - first_seen < timedelta(minutes=self.settings.settle_confirm_wait_minutes):
            return
        await self.repo.mark_final_confirmed(bet.id, now)
        if winner is None:
            await self.market.refund_market(bet.id, "tie")
            bump("refunded")
        else:
            await self.market.settle_market(bet.id, winner)
            bump("settled")

    # --- e) payouts ---

    async def _payouts(self, bump) -> None:
        tasks = await self.repo.due_payout_tasks(self.now())
        sem = asyncio.Semaphore(PAYOUT_CONCURRENCY)

        async def run(task: PayoutTask) -> None:
            async with sem:
                try:
                    await self._claim(task, bump)
                except Exception as e:
                    bump("payout_errors")
                    self.logger.error("worker.payout_failed", taskId=task.id, detail=err_detail(e))

        await asyncio.gather(*(run(t) for t in tasks))

        async def finish(bet: Bet) -> None:
            if await self.repo.open_task_count(bet.id) == 0 and await self.repo.mark_payouts_complete(bet.id, self.now()):
                self.logger.info("worker.payouts_complete", betId=bet.id)
                bump("completed")

        await self._each(await self.repo.unpaid_closed_bets(), finish, bump, "payouts")

    async def _claim(self, task: PayoutTask, bump) -> None:
        now = self.now()
        try:
            claim = await self.market.chain.claim(task.bet_id, task.wallet)
        except ChainError as e:
            if e.code == "ALREADY_CLAIMED":
                positions = await self.market.chain.get_positions(task.bet_id)
                mine = next((p for p in positions if p.wallet == task.wallet), None)
                await self.repo.task_done(task.id, mine.payout_base if mine else task.amount_base, None, now)
                bump("claims_done")
                return
            await self._claim_failed(task, e.code, e.code in PERMANENT_CLAIM_CODES, bump)
            return
        except Exception as e:
            await self._claim_failed(task, type(e).__name__, False, bump)
            return
        await self.repo.task_done(task.id, claim.payout_base, claim.tx_ref, now)
        bump("claims_done")

    async def _claim_failed(self, task: PayoutTask, error: str, permanent: bool, bump) -> None:
        now = self.now()
        attempts = task.attempts + 1
        if permanent or attempts >= self.settings.claim_max_attempts:
            await self.repo.task_failed(task.id, error, now)
            self.logger.error("worker.claim_gave_up", taskId=task.id, betId=task.bet_id, attempts=attempts, error=error)
            bump("claims_failed")
            bet = await self.repo.get_bet(task.bet_id)
            if bet is not None:
                await self.market.queue_notice(bet, "payouts_delayed")  # primary key (bet_id, kind): sent once
            return
        delay = self.settings.claim_backoff_seconds * 2 ** (attempts - 1)
        await self.repo.task_retry_later(task.id, error, now + timedelta(seconds=delay), now)
        self.logger.warn("worker.claim_retry", taskId=task.id, attempts=attempts, error=error, delaySeconds=delay)
        bump("claims_retried")

    # --- f) notices ---

    async def _notices(self, bump) -> None:
        for n in await self.repo.due_notices(self.now(), self.settings.notice_max_attempts):
            try:
                if await self.market.send_queued_notice(n["bet_id"], n["kind"], n["attempts"]):
                    bump("notices_sent")
                else:
                    bump("notices_failed")
            except Exception as e:
                bump("notice_errors")
                self.logger.error("worker.notice_error", betId=n["bet_id"], kind=n["kind"], detail=err_detail(e))

    # --- status ---

    async def status(self) -> dict:
        counts = await self.repo.worker_counts()
        return {"ok": True, "enabled": self.settings.worker_enabled, "last_tick": self.last_tick, **counts}


__all__ = ["Worker", "MarketError"]
