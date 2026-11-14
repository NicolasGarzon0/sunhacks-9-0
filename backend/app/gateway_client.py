"""Client for the iMessage gateway's POST /send. Never raises on a failed send."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .logging import Logger, err_detail

KNOWN_CODES = {"NOT_ALLOWED", "RATE_LIMITED", "SEND_FAILED", "BAD_REQUEST", "UNAUTHORIZED"}
# Local code for transport errors, timeouts, and unparseable responses.
UNREACHABLE = "UNREACHABLE"
MAX_RETRY_AFTER_S = 30.0


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    code: str | None = None
    detail: str | None = None
    retry_after: float | None = None


def _parse_retry_after(body_value: object, header_value: str | None) -> float | None:
    for v in (body_value, header_value):
        if v is None:
            continue
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if f >= 0:
            return f
    return None


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        secret: str,
        logger: Logger,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self.logger = logger
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post_send(self, to: str, text: str, idempotency_key: str) -> SendResult:
        try:
            res = await self._client.post(
                f"{self.base_url}/send",
                json={"to": to, "text": text, "idempotencyKey": idempotency_key},
                headers={"X-Gateway-Secret": self._secret},
            )
        except Exception as e:  # timeouts, connection refused, etc.
            return SendResult(ok=False, code=UNREACHABLE, detail=err_detail(e))
        try:
            body = res.json()
        except Exception:
            return SendResult(ok=False, code=UNREACHABLE, detail=f"non-JSON response (HTTP {res.status_code})")
        if not isinstance(body, dict):
            return SendResult(ok=False, code=UNREACHABLE, detail=f"unexpected response (HTTP {res.status_code})")
        if body.get("ok") is True:
            mid = body.get("messageId")
            return SendResult(ok=True, message_id=mid if isinstance(mid, str) else None)
        code = body.get("code")
        if not isinstance(code, str) or not code:
            code = "SEND_FAILED"
        detail = body.get("detail")
        return SendResult(
            ok=False,
            code=code,
            detail=detail if isinstance(detail, str) else None,
            retry_after=_parse_retry_after(body.get("retryAfter"), res.headers.get("retry-after")),
        )

    async def send_text(self, to: str, text: str, idempotency_key: str) -> SendResult:
        """Send a text through the gateway. `to` is passed through exactly as given."""
        try:
            self.logger.info("send.requested", to=to, textLength=len(text), idempotencyKey=idempotency_key)
            result = await self._post_send(to, text, idempotency_key)
            if (
                not result.ok
                and result.code == "RATE_LIMITED"
                and result.retry_after is not None
                and result.retry_after <= MAX_RETRY_AFTER_S
            ):
                self.logger.warn("send.rate_limited_retry", to=to, retryAfter=result.retry_after)
                await self._sleep(result.retry_after)
                result = await self._post_send(to, text, idempotency_key)
            if result.ok:
                self.logger.info("send.ok", to=to, messageId=result.message_id)
            else:
                self.logger.warn(
                    "send.failed", to=to, code=result.code, detail=result.detail, retryAfter=result.retry_after
                )
            return result
        except Exception as e:  # belt and braces: never raise
            self.logger.error("send.error", to=to, detail=err_detail(e))
            return SendResult(ok=False, code=UNREACHABLE, detail=err_detail(e))

    async def health(self, timeout: float = 2.0) -> bool:
        try:
            res = await self._client.get(f"{self.base_url}/health", timeout=timeout)
            return res.status_code == 200
        except Exception:
            return False
