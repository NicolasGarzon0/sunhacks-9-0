"""MarketService: all market logic (opening, view, deposits, closing funding). Route handlers stay thin.

No model calls here. Every text is a fixed template from app.templates, sent only to the proposer.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import templates as T
from app.gateway_client import GatewayClient
from app.logging import Logger, err_detail, mask_handle
from app.repository import Bet, Repository, Snapshot
from games.models import Game
from games.provider import GameProvider

from .chain import BetParams, ChainBet, ChainClient, ChainError, Position, is_valid_wallet
from .links import LinkSigner
from .money import MoneyError, format_usd, parse_usd
from .pool_math import compute_payout, implied_multiplier, preview_payout_for_deposit, settle_amounts

PERMANENT_SEND_CODES = {"NOT_ALLOWED", "BAD_REQUEST"}  # retrying cannot help
NOTICE_BACKOFF_BASE_S = 30
NOTICE_BACKOFF_MAX_S = 3600
SHORT_REFUND_REASONS = ("one_sided", "too_few_participants")  # texted by the chunk 4 'refunded' notice

NAME_MAX = 24
_URL_RE = re.compile(r"(://|\bwww\.|\b[\w-]{2,}\.[a-z]{2,}\b)", re.IGNORECASE)


class MarketError(Exception):
    """A request the API maps to an error response. code is one of ChainError codes or the extras below."""

    def __init__(self, code: str, detail: str, status: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.status = code, detail, status


def clean_display_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise MarketError("BAD_DISPLAY_NAME", "display_name must be text")
    name = " ".join(raw.split())
    if not name:
        raise MarketError("BAD_DISPLAY_NAME", "display_name is empty")
    if len(name) > NAME_MAX:
        raise MarketError("BAD_DISPLAY_NAME", f"display_name is longer than {NAME_MAX} characters")
    if any(not c.isprintable() for c in name) or _URL_RE.search(name):
        raise MarketError("BAD_DISPLAY_NAME", "display_name must be plain text without links")
    return name


@dataclass(frozen=True)
class OpenResult:
    # opened | failed | existing | game_started | game_unavailable | funding_closed | lookup_error
    kind: str
    text: str | None  # reply to send for this message; None when the proposer notice already went out
    keep_draft: bool = False  # True when the link text could not be sent: confirming again resends it


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


class MarketService:
    def __init__(
        self,
        repo: Repository,
        chain: ChainClient,
        signer: LinkSigner,
        gateway: GatewayClient,
        logger: Logger,
        settings,
        *,
        provider: GameProvider,
        now=None,
    ) -> None:
        self.repo, self.chain, self.signer, self.gateway = repo, chain, signer, gateway
        self.logger, self.settings, self.provider = logger, settings, provider
        self.tz = settings.local_timezone
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def funding_lead(self) -> timedelta:
        return timedelta(minutes=self.settings.funding_minutes_before_kickoff)

    # --- token -> bet ---

    async def bet_for_token(self, token: str) -> Bet:
        bet_id = self.signer.bet_id(token)
        bet = await self.repo.get_bet(bet_id) if bet_id else None
        if bet is None:
            raise MarketError("BAD_TOKEN", "unknown or invalid link")
        if self.signer.is_expired(bet, self.now()):
            self.logger.info("market.link_expired", betId=bet.id)
            raise MarketError("LINK_EXPIRED", "this link has expired")
        return bet

    # --- notices (durable send once to the proposer) ---

    async def notify_once(self, bet: Bet, kind: str, text: str) -> bool:
        key = f"bet:{bet.id}:{kind}"
        status, attempts = await self.repo.ensure_notice(bet.id, kind, key)
        if status == "sent":
            return True
        return await self._deliver(bet, kind, text, key, attempts)

    async def _deliver(self, bet: Bet, kind: str, text: str, key: str, attempts: int) -> bool:
        result = await self.gateway.send_text(bet.proposer_handle, text, key)
        if result.ok:
            await self.repo.mark_notice(bet.id, kind, "sent")
            return True
        permanent = result.code in PERMANENT_SEND_CODES
        delay = min(NOTICE_BACKOFF_BASE_S * 2 ** attempts, NOTICE_BACKOFF_MAX_S)
        await self.repo.mark_notice(
            bet.id, kind, "failed", result.code,
            next_attempt_at=None if permanent else self.now() + timedelta(seconds=delay), permanent=permanent,
        )
        self.logger.warn("market.notice_failed", betId=bet.id, kind=kind, code=result.code, permanent=permanent)
        return False

    async def queue_notice(self, bet: Bet, kind: str) -> None:
        """Record a notice for the worker to send (and retry). The text is rendered at send time."""
        await self.repo.ensure_notice(bet.id, kind, f"bet:{bet.id}:{kind}")

    async def render_notice(self, bet: Bet, kind: str) -> str:
        if kind == "link":
            return T.market_open(bet, self.tz, self.settings.link_grace_hours, self.signer.url(bet.id))
        if kind == "failed":
            return T.MARKET_FAILED
        if kind == "locked":
            return T.market_locked(bet, self._totals(await self.snapshots_for(bet)))
        if kind == "refunded":
            return T.market_refunded(bet)
        if kind == "result":
            return T.market_result(bet, self.settings.link_grace)
        if kind == "refund_result":
            return T.market_refund_result(bet, bet.refund_reason or "", self.settings.link_grace)
        if kind == "payouts_delayed":
            return T.PAYOUTS_DELAYED
        raise ValueError(f"unknown notice kind {kind}")

    async def send_queued_notice(self, bet_id: str, kind: str, attempts: int) -> bool:
        bet = await self.repo.get_bet(bet_id)
        if bet is None:
            return False
        return await self._deliver(bet, kind, await self.render_notice(bet, kind), f"bet:{bet.id}:{kind}", attempts)

    # --- opening ---

    def _snapshot(self, game: Game) -> dict:
        s = self.settings
        return {
            "fee_bps": s.fee_bps, "treasury": s.treasury_pubkey,
            "min_stake_base": s.min_stake_base, "max_stake_base": s.max_stake_base,
            "max_participants": s.max_participants,
            "funding_closes_at": game.kickoff_utc - timedelta(minutes=s.funding_minutes_before_kickoff),
            "settle_after": game.kickoff_utc + timedelta(hours=s.settle_hours_after_kickoff),
        }

    async def open_market(self, proposer_handle: str, game: Game) -> OpenResult:
        """Called when the proposer confirms a game. Idempotent: a second call never makes a second market."""
        try:
            status = await self.provider.get_status(game.game_id)
        except Exception as e:
            self.logger.warn("market.status_check_failed", gameId=game.game_id, detail=type(e).__name__)
            return OpenResult("lookup_error", T.LOOKUP_APOLOGY)
        now = self.now()
        if status != "scheduled":
            if status in ("in_progress", "final"):
                return OpenResult("game_started", T.GAME_STARTED)
            return OpenResult("game_unavailable", T.GAME_UNAVAILABLE)
        if game.kickoff_utc <= now:
            return OpenResult("game_started", T.GAME_STARTED)
        snap = self._snapshot(game)
        if snap["funding_closes_at"] <= now:
            return OpenResult("funding_closed", T.funding_closed(self.settings.funding_minutes_before_kickoff))

        bet, created = await self.repo.create_or_get_bet(proposer_handle, game.to_json(), snap)
        if bet.status in ("open", "locked"):  # same game confirmed again: resend the link as this message's reply
            self.logger.info("market.exists", betId=bet.id, status=bet.status)
            return OpenResult("existing", T.market_exists(bet, self.tz, self.settings.link_grace_hours, self.signer.url(bet.id)))
        try:
            await self.chain.create_bet(
                bet.id,
                BetParams(
                    bet.fee_bps, bet.treasury, bet.min_stake_base, bet.max_stake_base, bet.max_participants,
                    funding_closes_at=bet.funding_closes_at,
                    # the oracle gets until refund_after to settle; after it anyone can refund on chain
                    refund_after=bet.settle_after + timedelta(hours=self.settings.contract_refund_buffer_hours),
                ),
            )
            if not await self.repo.set_bet_open(bet.id, bet.id, now):
                raise RuntimeError("bet was not in game_confirmed")
            bet = await self.repo.get_bet(bet.id)
            self.logger.info("market.opened", betId=bet.id, created=created, gameId=game.game_id)
            sent = await self.notify_once(
                bet, "link", T.market_open(bet, self.tz, self.settings.link_grace_hours, self.signer.url(bet.id))
            )
        except Exception as e:
            return await self._fail(bet, f"open_failed: {type(e).__name__}", e)
        return OpenResult("opened", None, keep_draft=not sent)

    async def _fail(self, bet: Bet, reason: str, err: BaseException | None = None) -> OpenResult:
        """Never leave a half open market: mark failed, refund on the chain best effort, tell the proposer."""
        self.logger.error("market.open_failed", betId=bet.id, reason=reason, detail=err_detail(err) if err else None)
        await self.repo.fail_bet(bet.id, reason)
        try:
            await self.chain.refund_all(bet.id)
        except Exception as e:  # nothing to refund yet, or already closed
            self.logger.info("market.fail_refund_skipped", betId=bet.id, detail=type(e).__name__)
        try:
            await self.notify_once(bet, "failed", T.MARKET_FAILED)
        except Exception as e:
            # The durable record could not be written (database trouble). Still tell the proposer: a direct send
            # with the same idempotency key, so the gateway never delivers it twice within its window.
            self.logger.error("market.fail_notice_error", betId=bet.id, detail=err_detail(e))
            result = await self.gateway.send_text(bet.proposer_handle, T.MARKET_FAILED, f"bet:{bet.id}:failed")
            if not result.ok:
                self.logger.error("market.fail_direct_send_failed", betId=bet.id, code=result.code)
        return OpenResult("failed", None)

    # --- closing funding ---

    def _lock_for(self, bet_id: str) -> asyncio.Lock:
        return self._locks.setdefault(bet_id, asyncio.Lock())

    async def close_funding(self, bet_id: str) -> Bet:
        """Lock the market when both sides have money and there are at least 2 participants, else refund everyone.

        Idempotent and serialized per bet: a call on a market that is not open changes nothing.
        The worker calls this at funding_closes_at.
        """
        async with self._lock_for(bet_id):
            bet = await self.repo.get_bet(bet_id)
            if bet is None:
                raise MarketError("NOT_FOUND", "unknown bet")
            if bet.status != "open":
                return bet
            cb = await self.chain.get_bet(bet.id)
            both = cb.side_totals[0] > 0 and cb.side_totals[1] > 0
            should_lock = both and cb.participant_count >= 2
            at = self.now()
            if cb.state in ("open", "locked"):
                await self.take_snapshot(bet.id)  # before lock or refund: the positions as they stand
            if cb.state == "open" and should_lock:
                try:
                    cb = await self.chain.lock(bet.id)
                except ChainError as e:
                    if e.code != "PAST_REFUND_DEADLINE":
                        raise
                    # the worker was down past refund_after: the oracle can no longer lock, so refund everyone
                    self.logger.warn("market.lock_past_deadline", betId=bet.id)
                    await self.repo.set_refund_intent(bet.id, "no_result")
                    bet = await self.repo.get_bet(bet.id)  # type: ignore[assignment]
                    cb = await self.chain.refund_all(bet.id)
            elif cb.state == "open":
                cb = await self.chain.refund_all(bet.id)
            # a chain that is already locked or refunded (a crashed earlier call) just gets recorded below
            if cb.state in ("locked", "refunded"):
                await self.take_snapshot(bet.id)  # again: a last deposit may have landed between the read and the lock
            if cb.state == "locked":
                if await self.repo.lock_bet(bet.id, cb.total_pool_base, at):
                    self.logger.info("market.locked", betId=bet.id, pool=cb.total_pool_base, participants=cb.participant_count)
                    fresh = await self.repo.get_bet(bet.id)
                    await self.notify_once(fresh, "locked", T.market_locked(fresh, cb.side_totals))
            elif cb.state == "refunded":
                reason = bet.refund_reason or ("too_few_participants" if cb.participant_count < 2 else "one_sided")
                if await self.repo.refund_bet(bet.id, reason, cb.total_pool_base, at):
                    self.logger.info("market.refunded", betId=bet.id, reason=reason, participants=cb.participant_count)
                    fresh = await self.repo.get_bet(bet.id)
                    await self.ensure_payout_tasks(fresh)
                    if reason in SHORT_REFUND_REASONS:
                        await self.notify_once(fresh, "refunded", T.market_refunded(fresh))
                    else:  # a refund that was already decided (for example a cancelled game) before the chain call finished
                        await self.queue_notice(fresh, "refund_result")
            else:
                self.logger.error("market.close_unexpected_state", betId=bet.id, chainState=cb.state)
            return await self.repo.get_bet(bet.id)

    async def maybe_close_when_full(self, bet_id: str) -> Bet | None:
        """Close funding once participant_count reaches max_participants. Safe to call after every deposit."""
        bet = await self.repo.get_bet(bet_id)
        if bet is None or bet.status != "open":
            return bet
        cb = await self.chain.get_bet(bet.id)
        if cb.participant_count >= bet.max_participants:
            self.logger.info("market.full", betId=bet.id, participants=cb.participant_count)
            return await self.close_funding(bet.id)
        return bet

    async def refund_market(self, bet_id: str, reason: str) -> Bet:
        """Refund everyone with no fee from open or locked. Safe to call twice and after a crash."""
        async with self._lock_for(bet_id):
            bet = await self.repo.get_bet(bet_id)
            if bet is None:
                raise MarketError("NOT_FOUND", "unknown bet")
            if bet.status not in ("open", "locked"):
                return bet
            await self.repo.set_refund_intent(bet.id, reason)
            cb = await self.chain.get_bet(bet.id)
            if cb.state in ("open", "locked"):
                await self.take_snapshot(bet.id)  # before the refund, so the positions are on record
                cb = await self.chain.refund_all(bet.id)
            if cb.state != "refunded":
                raise MarketError("WRONG_STATE", f"chain is {cb.state}; cannot refund")
            await self.take_snapshot(bet.id)  # no-op when nothing changed; catches a deposit that raced the refund
            fresh = await self.repo.get_bet(bet.id)
            final_reason = fresh.refund_reason or reason
            if await self.repo.refund_bet(bet.id, final_reason, cb.total_pool_base, self.now()):
                self.logger.info("market.refunded", betId=bet.id, reason=final_reason, participants=cb.participant_count)
                fresh = await self.repo.get_bet(bet.id)
                await self.ensure_payout_tasks(fresh)
                await self.queue_notice(fresh, "refunded" if final_reason in SHORT_REFUND_REASONS else "refund_result")
            return await self.repo.get_bet(bet.id)

    async def settle_market(self, bet_id: str, winning_side: int) -> Bet:
        """Settle a locked market on the chain, record it, create winner tasks, queue the result text. Safe twice.
        Past refund_after the oracle can no longer settle, so everyone is refunded instead (reason no_result)."""
        try:
            return await self._settle_locked(bet_id, winning_side)
        except ChainError as e:
            if e.code != "PAST_REFUND_DEADLINE":
                raise
            self.logger.warn("market.settle_past_deadline", betId=bet_id)
            return await self.refund_market(bet_id, "no_result")

    async def _settle_locked(self, bet_id: str, winning_side: int) -> Bet:
        async with self._lock_for(bet_id):
            bet = await self.repo.get_bet(bet_id)
            if bet is None:
                raise MarketError("NOT_FOUND", "unknown bet")
            if bet.status == "settled":
                return bet
            if bet.status != "locked":
                raise MarketError("WRONG_STATE", f"market is {bet.status}")
            cb = await self.chain.get_bet(bet.id)
            if cb.state == "locked":
                cb = await self.chain.settle(bet.id, winning_side)
            if cb.state != "settled":
                raise MarketError("WRONG_STATE", f"chain is {cb.state}; cannot settle")
            if await self.repo.settle_bet(bet.id, cb.winning_side, cb.fee_paid_base, cb.total_pool_base, self.now()):  # type: ignore[arg-type]
                self.logger.info("market.settled", betId=bet.id, winningSide=cb.winning_side, fee=cb.fee_paid_base, pool=cb.total_pool_base)
                fresh = await self.repo.get_bet(bet.id)
                await self.ensure_payout_tasks(fresh)
                await self.queue_notice(fresh, "result")
            return await self.repo.get_bet(bet.id)  # type: ignore[return-value]

    # --- snapshots ---

    async def _positions(self, bet_id: str, wallets: list[str] | None = None) -> list[Position]:
        """Open chain positions of the wallets the backend knows (participants table), or of `wallets` when given."""
        return await self.chain.get_positions(bet_id, await self.repo.participant_wallets(bet_id) if wallets is None else wallets)

    async def take_snapshot(self, bet_id: str) -> int:
        """Copy the chain's open positions into position_snapshots. Adds and follows top ups, never moves a paid row."""
        try:
            positions = await self._positions(bet_id)
        except ChainError as e:
            if e.code == "NOT_FOUND":
                return 0
            raise
        return await self.repo.upsert_snapshots(bet_id, [(p.wallet, p.side, p.amount_base) for p in positions])

    async def take_snapshot_if_missing(self, bet: Bet) -> bool:
        """For the worker's reconcile pass: a locked market with no snapshots (locked before 0007) gets them now."""
        if await self.repo.snapshots(bet.id):
            return False
        return await self.take_snapshot(bet.id) > 0

    async def snapshots_for(self, bet: Bet) -> list[Snapshot]:
        """The snapshots of a locked, settled or refunded market; takes them from the chain when none exist yet
        (a market that closed funding before migration 0007, or a crash)."""
        snaps = await self.repo.snapshots(bet.id)
        if not snaps and bet.status in ("locked", "settled", "refunded"):
            await self.take_snapshot(bet.id)
            snaps = await self.repo.snapshots(bet.id)
        return snaps

    @staticmethod
    def _totals(snaps: list[Snapshot]) -> tuple[int, int]:
        t = [0, 0]
        for x in snaps:
            t[x.side] += x.amount_base
        return t[0], t[1]

    async def ensure_payout_tasks(self, bet: Bet) -> int:
        """Claim tasks for every wallet owed money (winners after settled, everyone after refunded) and, after settled,
        a close_loser task per losing position (it only returns rent). Idempotent. Reads the snapshots."""
        if bet.status not in ("settled", "refunded"):
            return 0
        try:
            cb = await self.chain.get_bet(bet.id)
        except ChainError as e:
            if e.code == "NOT_FOUND":
                return 0  # the Bet account is already closed: every task was done
            raise
        snaps = await self.snapshots_for(bet)
        rows: list[tuple[str, str, int]] = []
        if cb.state == "refunded":
            rows = [(x.wallet, "refund", x.amount_base) for x in snaps]
        elif cb.state == "settled":
            ws = cb.winning_side
            totals = self._totals(snaps)
            for x in snaps:
                if x.side == ws:
                    rows.append((x.wallet, "win", compute_payout(x.amount_base, cb.distributable_base, totals[ws])))  # type: ignore[index]
                else:
                    rows.append((x.wallet, "close_loser", 0))
        return await self.repo.create_payout_tasks(bet.id, rows)

    def funding_has_closed(self, bet: Bet) -> bool:
        return bet.funding_closes_at is not None and self.now() >= bet.funding_closes_at

    # --- views ---

    async def _chain_view(self, bet: Bet) -> ChainBet | None:
        if bet.status in ("game_confirmed", "failed") and bet.chain_bet_id is None:
            return None
        try:
            return await self.chain.get_bet(bet.id)
        except ChainError as e:
            if e.code == "NOT_FOUND":
                return None
            raise

    def _money(self, base: int) -> dict:
        return {"base": base, "usd": format_usd(base)}

    async def _pools(self, bet: Bet) -> tuple[tuple[int, int], int]:
        """(side totals, participant count). Open markets read the chain; closed ones read the snapshots."""
        if bet.status in ("locked", "settled", "refunded"):
            snaps = await self.snapshots_for(bet)
            return self._totals(snaps), len(snaps)
        cb = await self._chain_view(bet)
        return (cb.side_totals, cb.participant_count) if cb else ((0, 0), 0)

    async def market_view(self, bet: Bet) -> dict:
        totals, count = await self._pools(bet)
        fee_bps = bet.fee_bps or 0
        view: dict = {
            "ok": True,
            "bet_id": bet.id,
            "status": bet.status,
            "league": bet.league,
            "teams": {
                "side0": {"name": bet.side0_name, "role": "away"},
                "side1": {"name": bet.side1_name, "role": "home"},
            },
            "kickoff_utc": _iso(bet.kickoff_utc),
            "funding_closes_at": _iso(bet.funding_closes_at),
            # the server's clock (ISO UTC, ms): the page offsets its countdown by (server_time - its own clock at fetch)
            "server_time": self.now().astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "pools": {
                "side0_base": totals[0], "side1_base": totals[1],
                "side0_usd": format_usd(totals[0]), "side1_usd": format_usd(totals[1]),
            },
            "total_pool_base": totals[0] + totals[1],
            "total_pool_usd": format_usd(totals[0] + totals[1]),
            "participant_count": count,
            "limits": {
                "min_stake_base": bet.min_stake_base, "min_stake_usd": format_usd(bet.min_stake_base or 0),
                "max_stake_base": bet.max_stake_base, "max_stake_usd": format_usd(bet.max_stake_base or 0),
                "max_participants": bet.max_participants,
            },
            "fee_bps": fee_bps,
            "fee_line": T.fee_line(fee_bps),
            "multipliers": (
                None
                if bet.status in ("refunded", "failed", "game_confirmed")
                else {"side0": implied_multiplier(totals, 0, fee_bps), "side1": implied_multiplier(totals, 1, fee_bps)}
            ),
        }
        if bet.status == "locked":
            view["game_pending"] = True
        if bet.status == "settled" and bet.winning_side is not None:
            fee = bet.fee_paid_base or 0
            distributable = totals[0] + totals[1] - fee
            view["winning_side"] = bet.winning_side
            view["fee_paid_base"] = fee
            view["fee_paid_usd"] = format_usd(fee)
            view["distributable_base"] = distributable
            view["distributable_usd"] = format_usd(distributable)
            view["final_multiplier"] = implied_multiplier(totals, bet.winning_side, fee_bps)
        if bet.status == "refunded":
            view["refund_reason"] = bet.refund_reason
        if bet.status == "failed":
            view["failed"] = True  # internal failure_reason is not public
        return view

    async def participants(self, bet: Bet) -> dict:
        names = await self.repo.display_names(bet.id)
        rows: list[tuple[str, int, int]]
        if bet.status in ("locked", "settled", "refunded"):
            rows = [(x.wallet, x.side, x.amount_base) for x in await self.snapshots_for(bet)]
        else:
            try:
                positions = await self._positions(bet.id) if bet.chain_bet_id else []
            except ChainError as e:
                if e.code != "NOT_FOUND":
                    raise
                positions = []
            rows = [(p.wallet, p.side, p.amount_base) for p in positions]
        return {
            "ok": True,
            "bet_id": bet.id,
            "participants": [
                {
                    "wallet": w, "side": side, "stake_base": amount,
                    "stake_usd": format_usd(amount), "display_name": names.get(w),
                }
                for w, side, amount in rows
            ],
        }

    async def position(self, bet: Bet, wallet: str) -> dict:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        names = await self.repo.display_names(bet.id)
        out = {"ok": True, "bet_id": bet.id, "status": bet.status, "wallet": wallet,
               "display_name": names.get(wallet), "has_position": False, "side": None,
               "stake_base": None, "stake_usd": None, "payout_base": None, "payout_usd": None, "paid": False}
        if bet.status not in ("locked", "settled", "refunded"):
            cb = await self._chain_view(bet)
            positions = await self._positions(bet.id, [wallet]) if cb else []
            mine = next((p for p in positions if p.wallet == wallet), None)
            if mine is not None:
                out.update(has_position=True, side=mine.side, stake_base=mine.amount_base, stake_usd=format_usd(mine.amount_base))
            return out
        snaps = await self.snapshots_for(bet)  # the chain position may be gone (claimed or closed): the snapshot stays
        mine = next((x for x in snaps if x.wallet == wallet), None)
        if mine is None:
            return out
        out.update(has_position=True, side=mine.side, stake_base=mine.amount_base, stake_usd=format_usd(mine.amount_base),
                   paid=mine.status in ("paid", "refunded"))
        payout: int | None = None
        if bet.status == "refunded":
            payout = mine.amount_base
        elif bet.status == "settled":
            if mine.payout_base is not None:
                payout = mine.payout_base
            elif mine.side == bet.winning_side:
                totals = self._totals(snaps)
                fee = bet.fee_paid_base or 0
                payout = compute_payout(mine.amount_base, totals[0] + totals[1] - fee, totals[bet.winning_side])
            else:
                payout = 0
        if payout is not None:
            out.update(payout_base=payout, payout_usd=format_usd(payout))
        return out

    # --- writes ---

    async def set_display_name(self, bet: Bet, wallet: object, display_name: object) -> dict:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        name = clean_display_name(display_name)
        await self.repo.set_display_name(bet.id, wallet, name)  # type: ignore[arg-type]
        self.logger.info("market.display_name_set", betId=bet.id, wallet=mask_handle(wallet))  # type: ignore[arg-type]
        return {"ok": True, "bet_id": bet.id, "wallet": wallet, "display_name": name}

    @staticmethod
    def _parse_side(side: object) -> int:
        if isinstance(side, bool) or isinstance(side, float):
            raise ChainError("INVALID_SIDE", "side must be 0 or 1")
        try:
            value = int(str(side).strip())
        except ValueError:
            raise ChainError("INVALID_SIDE", "side must be 0 or 1") from None
        if value not in (0, 1):
            raise ChainError("INVALID_SIDE", "side must be 0 or 1")
        return value

    @staticmethod
    def _parse_amount(amount_usd: object) -> int:
        try:
            return parse_usd(amount_usd)
        except MoneyError as e:
            raise MarketError("BAD_REQUEST", str(e)) from None

    async def _require_open(self, bet: Bet) -> Bet:
        if bet.status == "open" and self.funding_has_closed(bet):
            bet = await self.close_funding(bet.id)  # no worker yet: close lazily so a late request cannot slip in
        if bet.status != "open":
            raise MarketError("MARKET_NOT_OPEN", f"market is {bet.status}", status=bet.status)
        return bet

    async def preview(self, bet: Bet, side: object, amount_usd: object) -> dict:
        bet = await self._require_open(bet)
        s = self._parse_side(side)
        amount = self._parse_amount(amount_usd)
        if amount < (bet.min_stake_base or 0):
            raise ChainError("BELOW_MIN", "amount is below the minimum")
        if amount > (bet.max_stake_base or 0):
            raise ChainError("ABOVE_MAX", "amount is above the maximum")
        cb = await self.chain.get_bet(bet.id)
        fee_bps = bet.fee_bps or 0
        payout = preview_payout_for_deposit(cb.side_totals, s, amount, fee_bps)
        after = list(cb.side_totals)
        after[s] += amount
        return {
            "ok": True, "bet_id": bet.id, "status": bet.status, "side": s,
            "amount_base": amount, "amount_usd": format_usd(amount),
            "payout_base": payout, "payout_usd": format_usd(payout),
            "profit_base": payout - amount, "profit_usd": format_usd(payout - amount),
            "multiplier": implied_multiplier((after[0], after[1]), s, fee_bps),
            "fee_bps": fee_bps, "fee_line": T.fee_line(fee_bps),
        }

    async def deposit(self, bet: Bet, wallet: object, side: object, amount_usd: object, display_name: object = None) -> dict:
        """Mock chain only (the route returns 404 otherwise). Validates, deposits, names, then maybe closes."""
        bet = await self._require_open(bet)
        s = self._parse_side(side)
        amount = self._parse_amount(amount_usd)
        name = clean_display_name(display_name) if display_name is not None else None
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        await self.repo.record_participant(bet.id, wallet)  # before the deposit, like the real flow
        pos: Position = await self.chain.deposit(bet.id, wallet, s, amount)  # type: ignore[attr-defined]
        self.logger.info("market.deposit", betId=bet.id, wallet=mask_handle(pos.wallet), side=s, amountBase=amount)
        if name is not None:
            await self.repo.set_display_name(bet.id, pos.wallet, name)
        await self.maybe_close_when_full(bet.id)
        return await self.market_view(await self.repo.get_bet(bet.id))  # type: ignore[arg-type]

    # --- admin ---

    async def admin_view(self, bet: Bet) -> dict:
        cb = await self._chain_view(bet)
        positions = await self._positions(bet.id) if cb else []
        snapshots = await self.repo.snapshots(bet.id)
        row = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in bet.__dict__.items()}
        row["proposer_handle"] = mask_handle(bet.proposer_handle)
        return {
            "ok": True,
            "bet": row,
            "chain": None if cb is None else {**cb.__dict__, "side_totals": list(cb.side_totals)},
            "positions": [p.__dict__ for p in positions],
            "snapshots": [x.__dict__ for x in snapshots],
            "notices": await self.repo.list_notices(bet.id),
            "payout_tasks": [t.__dict__ for t in await self.repo.payout_tasks(bet.id)],
            "link": self.signer.url(bet.id),
            "link_expires_at": _iso(self.signer.expires_at(bet)),
        }
