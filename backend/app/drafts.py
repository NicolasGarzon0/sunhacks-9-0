"""Bet request flow for a roster-known sender. Code decides everything; the extractor only reads the text.

Each inbound message produces exactly one fixed-template reply, sent only to the sender of that message.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from extraction.client import MAX_INPUT_CHARS
from extraction.schema import Extraction
from games.models import Game
from games.provider import GameProvider

from . import templates as T
from .confirm import parse_confirmation
from .gateway_client import GatewayClient
from .logging import Logger
from .repository import Draft, Repository, RosterEntry
from .validation import evaluate, parse_date

MAX_CANDIDATES = 5
# Marks a draft whose last lookup found nothing (or failed). It stays collecting so a follow-up answer is
# merged, but a brand-new request replaces it without the "Replace it?" question: there is nothing to lose.
DEAD_END = "lookup_dead_end"

DRAFT_TTL = timedelta(minutes=30)
YES_RE = re.compile(r"^\s*(yes|y)\s*[.!]*\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(no|n)\s*[.!]*\s*$", re.IGNORECASE)


class Extractor(Protocol):
    async def extract(self, text: str, today: date, tz: str, pending_field: str | None = None) -> Extraction | None: ...


def parse_yes_no(text: str) -> bool | None:
    if YES_RE.match(text):
        return True
    if NO_RE.match(text):
        return False
    return None


def merge_fields(old: dict, new: dict) -> dict:
    """Merge a follow-up answer into a draft. A newly named team brings its own opponent (possibly none),
    so a stale opponent from the previous search does not stick."""
    merged = dict(old)
    if new.get("team"):
        merged["team"] = new["team"]
        merged["opponent"] = new.get("opponent")
    elif new.get("opponent"):
        merged["opponent"] = new["opponent"]
    if new.get("date"):
        merged["date"] = new["date"]
    return merged


class BetFlow:
    def __init__(
        self,
        repo: Repository,
        gateway: GatewayClient,
        extractor: Extractor,
        logger: Logger,
        *,
        provider: GameProvider,
        tz: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.gateway = gateway
        self.extractor = extractor
        self.provider = provider
        self.logger = logger
        self.tz = tz
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = {}

    def today(self) -> date:
        return self.now().astimezone(ZoneInfo(self.tz)).date()

    async def handle(self, proposer: RosterEntry, text: str, message_id: str) -> None:
        # Serialize per sender: the gateway's per-sender ordering ends once /inbound returns 200.
        # One lock per roster handle (the roster is small); the reply is sent inside it to keep reply order.
        lock = self._locks.setdefault(proposer.handle, asyncio.Lock())
        async with lock:
            reply = await self._process(proposer, text, message_id)
            result = await self.gateway.send_text(proposer.handle, reply, f"reply:{message_id}")
        if not result.ok:
            self.logger.warn("bet.reply_failed", handle=proposer.handle, messageId=message_id, code=result.code)

    async def _active_draft(self, handle: str) -> Draft | None:
        draft = await self.repo.get_draft(handle)
        if draft is not None and self.now() - draft.updated_at > DRAFT_TTL:
            await self.repo.delete_draft(handle)
            self.logger.info("draft.expired", handle=handle, status=draft.status)
            return None
        return draft

    async def _process(self, proposer: RosterEntry, text: str, message_id: str) -> str:
        log = dict(handle=proposer.handle, messageId=message_id)
        if len(text) > MAX_INPUT_CHARS:
            self.logger.info("bet.too_long", textLength=len(text), **log)
            return T.TOO_LONG

        draft = await self._active_draft(proposer.handle)
        collecting = draft if draft is not None and draft.status == "collecting" else None

        # A pending replace question is answered deterministically, without the model.
        if collecting is not None and collecting.pending_replacement is not None:
            answer = parse_yes_no(text)
            self.logger.info("bet.replace_answer", answer={True: "yes", False: "no", None: "unclear"}[answer], **log)
            if answer is None:
                await self._touch(collecting)
                return T.REPLACE_UNCLEAR
            if answer is False:
                collecting.pending_replacement = None
                await self._touch(collecting)
                return T.REPLACE_KEPT_PREFIX + self._pending_question(collecting)
            new_fields = collecting.pending_replacement
            return await self._evaluate_and_save(proposer, new_fields, log)

        # Waiting on a game confirmation: parsed by code; the model is only used to spot a new bet request.
        if collecting is not None and collecting.pending_field == "confirm":
            return await self._confirm(proposer, collecting, text, log)

        pending = collecting.pending_field if collecting is not None else None
        extraction = await self.extractor.extract(text, self.today(), self.tz, pending)
        if extraction is None:
            self.logger.info("bet.fallback", **log)
            return T.FALLBACK

        kind = extraction.classification
        self.logger.info("bet.classified", classification=kind, hasDraft=collecting is not None, **log)

        if kind == "bet_proposal":
            if collecting is not None and not collecting.fields.get(DEAD_END):
                collecting.pending_replacement = extraction.fields()
                await self._touch(collecting)
                return T.REPLACE_ASK
            return await self._evaluate_and_save(proposer, extraction.fields(), log)

        if kind == "reply_to_pending" and collecting is not None:
            return await self._evaluate_and_save(proposer, merge_fields(collecting.fields, extraction.fields()), log)

        # "other", or a reply with nothing pending.
        if collecting is not None and collecting.pending_field:
            await self._touch(collecting)
            return self._pending_question(collecting)
        return T.HELP

    def _pending_question(self, draft: Draft) -> str:
        if draft.pending_field == "confirm" and draft.candidates:
            return T.offer([Game.from_json(c) for c in draft.candidates], self.tz)
        return T.question(draft.pending_field or "team")

    async def _confirm(self, proposer: RosterEntry, draft: Draft, text: str, log: dict) -> str:
        games = [Game.from_json(c) for c in draft.candidates or []]
        answer = parse_confirmation(text, len(games))
        if answer is None:
            extraction = await self.extractor.extract(text, self.today(), self.tz, "confirm")
            if extraction is not None and extraction.classification == "bet_proposal":
                self.logger.info("bet.confirm_answer", answer="new_request", **log)
                draft.pending_replacement = extraction.fields()
                await self._touch(draft)
                return T.REPLACE_ASK
            draft.confirm_misses += 1
            self.logger.info("bet.confirm_answer", answer="unclear", misses=draft.confirm_misses, **log)
            await self._touch(draft)
            question = T.offer(games, self.tz)
            return question if draft.confirm_misses < 2 else f"{T.offer_help(len(games))}\n{question}"

        self.logger.info("bet.confirm_answer", answer=answer.kind, **log)
        if answer.kind == "no":
            self._back_to_team(draft)
            await self._touch(draft)
            return T.CONFIRM_NO

        game = games[answer.index]
        if game.kickoff_utc <= self.now():
            self._back_to_team(draft)
            await self._touch(draft)
            self.logger.info("bet.confirm_started", gameId=game.game_id, **log)
            return T.GAME_STARTED
        bet, created = await self.repo.create_or_get_bet(proposer.handle, game.to_json())
        await self.repo.delete_draft(proposer.handle)
        self.logger.info("bet.confirmed", betId=bet.id, gameId=game.game_id, created=created, **log)
        return T.confirmed(game, self.tz)

    @staticmethod
    def _back_to_team(draft: Draft) -> None:
        draft.status = "collecting"
        draft.pending_field = "team"
        draft.candidates = None
        draft.confirm_misses = 0

    async def _lookup(self, proposer: RosterEntry, fields: dict, log: dict) -> str:
        """Find candidate games for a valid request; the reply replaces the chunk 2 readback."""
        draft = Draft(
            handle=proposer.handle, fields=fields, status="collecting", pending_field="team",
            pending_replacement=None, updated_at=self.now(),
        )
        day = parse_date(fields.get("date"))
        try:
            found = await self.provider.find_candidates(fields["team"], fields.get("opponent"), day)
        except Exception as e:
            draft.fields = {**fields, DEAD_END: True}
            await self.repo.save_draft(draft)
            self.logger.warn("bet.lookup_failed", provider=self.provider.name, detail=type(e).__name__, **log)
            return T.LOOKUP_APOLOGY
        now = self.now()
        games = [g for g in found if g.status == "scheduled" and g.kickoff_utc > now]
        games.sort(key=lambda g: g.kickoff_utc)
        games = games[:MAX_CANDIDATES]
        self.logger.info("bet.lookup", provider=self.provider.name, found=len(found), offered=len(games), **log)
        if not games:
            draft.fields = {**fields, DEAD_END: True}
            await self.repo.save_draft(draft)
            return T.no_games(fields["team"], day)
        draft.pending_field = "confirm"
        draft.candidates = [g.to_json() for g in games]
        await self.repo.save_draft(draft)
        return T.offer(games, self.tz)

    async def _touch(self, draft: Draft) -> None:
        draft.updated_at = self.now()
        await self.repo.save_draft(draft)

    async def _evaluate_and_save(self, proposer: RosterEntry, fields: dict, log: dict) -> str:
        """Validate and store the single draft for this sender (replaces any previous one)."""
        outcome = evaluate(fields, self.today())
        draft = Draft(
            handle=proposer.handle,
            fields=outcome.fields,
            status="ready" if outcome.ready else "collecting",
            pending_field=outcome.pending_field,
            pending_replacement=None,
            updated_at=self.now(),
        )
        self.logger.info("bet.validated", status=draft.status, reason=outcome.reason, pendingField=outcome.pending_field, **log)
        self.logger.debug("bet.fields", fields=str({k: v for k, v in outcome.fields.items()}), **log)
        if outcome.ready:
            return await self._lookup(proposer, outcome.fields, log)
        await self.repo.save_draft(draft)
        return outcome.reply
