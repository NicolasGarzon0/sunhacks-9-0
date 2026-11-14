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

from . import templates as T
from .gateway_client import GatewayClient
from .logging import Logger
from .repository import Draft, Repository, RosterEntry
from .validation import evaluate

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


class BetFlow:
    def __init__(
        self,
        repo: Repository,
        gateway: GatewayClient,
        extractor: Extractor,
        logger: Logger,
        *,
        tz: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.gateway = gateway
        self.extractor = extractor
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
                return T.REPLACE_KEPT_PREFIX + T.question(collecting.pending_field or "team")
            new_fields = collecting.pending_replacement
            return await self._evaluate_and_save(proposer, new_fields, log)

        pending = collecting.pending_field if collecting is not None else None
        extraction = await self.extractor.extract(text, self.today(), self.tz, pending)
        if extraction is None:
            self.logger.info("bet.fallback", **log)
            return T.FALLBACK

        kind = extraction.classification
        self.logger.info("bet.classified", classification=kind, hasDraft=collecting is not None, **log)

        if kind == "bet_proposal":
            if collecting is not None:
                collecting.pending_replacement = extraction.fields()
                await self._touch(collecting)
                return T.REPLACE_ASK
            return await self._evaluate_and_save(proposer, extraction.fields(), log)

        if kind == "reply_to_pending" and collecting is not None:
            merged = dict(collecting.fields)
            merged.update({k: v for k, v in extraction.fields().items() if v is not None})
            return await self._evaluate_and_save(proposer, merged, log)

        # "other", or a reply with nothing pending.
        if collecting is not None and collecting.pending_field:
            await self._touch(collecting)
            return T.question(collecting.pending_field)
        return T.HELP

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
        await self.repo.save_draft(draft)
        self.logger.info("bet.validated", status=draft.status, reason=outcome.reason, pendingField=outcome.pending_field, **log)
        self.logger.debug("bet.fields", fields=str({k: v for k, v in outcome.fields.items()}), **log)
        return outcome.reply
