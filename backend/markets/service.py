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
from app.repository import Bet, Repository
from games.models import Game
from games.provider import GameProvider

from .chain import BetParams, ChainBet, ChainClient, ChainError, Position, is_valid_wallet
from .links import LinkSigner
from .money import MoneyError, format_usd, parse_usd
from .pool_math import compute_payout, implied_multiplier, preview_payout_for_deposit, settle_amounts

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
        status, _ = await self.repo.ensure_notice(bet.id, kind, key)
        if status == "sent":
            return True
        result = await self.gateway.send_text(bet.proposer_handle, text, key)
        await self.repo.mark_notice(bet.id, kind, "sent" if result.ok else "failed", None if result.ok else result.code)
        if not result.ok:
            self.logger.warn("market.notice_failed", betId=bet.id, kind=kind, code=result.code)
        return result.ok

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
                BetParams(bet.fee_bps, bet.treasury, bet.min_stake_base, bet.max_stake_base, bet.max_participants),
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
        TODO(chunk 5): the background worker calls this at funding_closes_at and retries pending notices.
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
            if cb.state == "open" and should_lock:
                cb = await self.chain.lock(bet.id)
            elif cb.state == "open":
                cb = await self.chain.refund_all(bet.id)
            # a chain that is already locked or refunded (a crashed earlier call) just gets recorded below
            if cb.state == "locked":
                if await self.repo.lock_bet(bet.id, cb.total_pool_base, at):
                    self.logger.info("market.locked", betId=bet.id, pool=cb.total_pool_base, participants=cb.participant_count)
                    fresh = await self.repo.get_bet(bet.id)
                    await self.notify_once(fresh, "locked", T.market_locked(fresh, cb.side_totals))
            elif cb.state == "refunded":
                reason = "too_few_participants" if cb.participant_count < 2 else "one_sided"
                if await self.repo.refund_bet(bet.id, reason, cb.total_pool_base, at):
                    self.logger.info("market.refunded", betId=bet.id, reason=reason, participants=cb.participant_count)
                    fresh = await self.repo.get_bet(bet.id)
                    await self.notify_once(fresh, "refunded", T.market_refunded(fresh))
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

    async def market_view(self, bet: Bet) -> dict:
        cb = await self._chain_view(bet)
        totals = cb.side_totals if cb else (0, 0)
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
            "pools": {
                "side0_base": totals[0], "side1_base": totals[1],
                "side0_usd": format_usd(totals[0]), "side1_usd": format_usd(totals[1]),
            },
            "total_pool_base": totals[0] + totals[1],
            "total_pool_usd": format_usd(totals[0] + totals[1]),
            "participant_count": cb.participant_count if cb else 0,
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
        if bet.status == "settled" and cb is not None:
            view["winning_side"] = cb.winning_side
            view["fee_paid_base"] = cb.fee_paid_base
            view["fee_paid_usd"] = format_usd(cb.fee_paid_base)
            view["distributable_base"] = cb.distributable_base
            view["distributable_usd"] = format_usd(cb.distributable_base)
            view["final_multiplier"] = implied_multiplier(totals, cb.winning_side, fee_bps)
        if bet.status == "refunded":
            view["refund_reason"] = bet.refund_reason
        if bet.status == "failed":
            view["failed"] = True  # internal failure_reason is not public
        return view

    async def participants(self, bet: Bet) -> dict:
        names = await self.repo.display_names(bet.id)
        try:
            positions = await self.chain.get_positions(bet.id) if bet.chain_bet_id else []
        except ChainError as e:
            if e.code != "NOT_FOUND":
                raise
            positions = []
        return {
            "ok": True,
            "bet_id": bet.id,
            "participants": [
                {
                    "wallet": p.wallet, "side": p.side, "stake_base": p.amount_base,
                    "stake_usd": format_usd(p.amount_base), "display_name": names.get(p.wallet),
                }
                for p in positions
            ],
        }

    async def position(self, bet: Bet, wallet: str) -> dict:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        cb = await self._chain_view(bet)
        positions = await self.chain.get_positions(bet.id) if cb else []
        mine = next((p for p in positions if p.wallet == wallet), None)
        names = await self.repo.display_names(bet.id)
        out = {"ok": True, "bet_id": bet.id, "status": bet.status, "wallet": wallet,
               "display_name": names.get(wallet), "has_position": mine is not None, "side": None,
               "stake_base": None, "stake_usd": None, "payout_base": None, "payout_usd": None, "paid": False}
        if mine is None:
            return out
        out.update(side=mine.side, stake_base=mine.amount_base, stake_usd=format_usd(mine.amount_base), paid=mine.claimed)
        payout: int | None = None
        if cb.state == "refunded":
            payout = mine.amount_base
        elif cb.state == "settled":
            if mine.claimed:
                payout = mine.payout_base
            elif mine.side == cb.winning_side:
                payout = compute_payout(mine.amount_base, cb.distributable_base, cb.side_totals[cb.winning_side])
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
        pos: Position = await self.chain.deposit(bet.id, wallet, s, amount)  # type: ignore[attr-defined]
        self.logger.info("market.deposit", betId=bet.id, wallet=mask_handle(pos.wallet), side=s, amountBase=amount)
        if name is not None:
            await self.repo.set_display_name(bet.id, pos.wallet, name)
        await self.maybe_close_when_full(bet.id)
        return await self.market_view(await self.repo.get_bet(bet.id))  # type: ignore[arg-type]

    # --- admin ---

    async def admin_view(self, bet: Bet) -> dict:
        cb = await self._chain_view(bet)
        positions = await self.chain.get_positions(bet.id) if cb else []
        row = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in bet.__dict__.items()}
        row["proposer_handle"] = mask_handle(bet.proposer_handle)
        return {
            "ok": True,
            "bet": row,
            "chain": None if cb is None else {**cb.__dict__, "side_totals": list(cb.side_totals)},
            "positions": [p.__dict__ for p in positions],
            "notices": await self.repo.list_notices(bet.id),
            "link": self.signer.url(bet.id),
            "link_expires_at": _iso(self.signer.expires_at(bet)),
        }
