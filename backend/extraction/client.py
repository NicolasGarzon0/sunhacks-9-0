"""HaikuExtractor: one Claude Haiku call with one strict tool. Never raises; returns None on any failure."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from app.logging import Logger, err_detail

from .prompt import build_system_prompt
from .schema import PROPOSE_BET_TOOL, Extraction

MAX_INPUT_CHARS = 500


class MessagesAPI(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class HaikuExtractor:
    def __init__(
        self,
        api_key: str,
        model: str,
        logger: Logger,
        *,
        timeout: float = 10.0,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.logger = logger
        self.timeout = timeout
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    def build_request(self, text: str, today: date, tz: str, pending_field: str | None) -> dict:
        return {
            "model": self.model,
            "max_tokens": 1024,
            "system": build_system_prompt(today, tz, pending_field),
            "tools": [PROPOSE_BET_TOOL],
            # Haiku 5.5 accepts a forced tool choice (it skips thinking for that call). No sampling params:
            # Haiku 5.5 rejects non-default temperature/top_p/top_k.
            "tool_choice": {"type": "tool", "name": PROPOSE_BET_TOOL["name"]},
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": text}],
        }

    async def extract(self, text: str, today: date, tz: str, pending_field: str | None = None) -> Extraction | None:
        if len(text) > MAX_INPUT_CHARS:
            self.logger.warn("extract.too_long", textLength=len(text))
            return None
        try:
            response = await asyncio.wait_for(
                self._client.messages.create(**self.build_request(text, today, tz, pending_field)),
                timeout=self.timeout + 5,
            )
        except anthropic.RateLimitError as e:
            self.logger.warn("extract.rate_limited", detail=err_detail(e))
            return None
        except anthropic.APIStatusError as e:
            self.logger.error("extract.api_error", status=e.status_code, detail=err_detail(e))
            return None
        except (anthropic.APIConnectionError, asyncio.TimeoutError) as e:  # APITimeoutError is a connection error
            self.logger.warn("extract.unreachable", detail=err_detail(e))
            return None
        except Exception as e:
            self.logger.error("extract.error", detail=err_detail(e))
            return None
        return self.parse(response)

    def parse(self, response: Any) -> Extraction | None:
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            self.logger.warn("extract.refusal")
            return None
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == PROPOSE_BET_TOOL["name"]:
                try:
                    result = Extraction.model_validate(block.input)
                except ValidationError as e:
                    self.logger.warn("extract.invalid_output", errors=e.error_count())
                    return None
                self.logger.info(
                    "extract.ok", classification=result.classification, stopReason=stop, **{f"has_{k}": v for k, v in result.present().items()}
                )
                return result
        self.logger.warn("extract.no_tool_call", stopReason=stop)
        return None
