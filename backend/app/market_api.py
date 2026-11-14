"""Public market API for the deposit page, mounted at /markets (its own sub app with its own CORS policy).

Thin on purpose: every rule lives in markets.service. Responses are {ok: true, ...} or {ok: false, code, detail}.
Money appears as integer base units and as decimal strings. Request bodies are parsed with Decimal, never float.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from markets.chain import ChainError
from markets.service import MarketError, MarketService

from .logging import Logger, err_detail

HTTP = {
    "BAD_TOKEN": 404, "LINK_EXPIRED": 410, "NOT_FOUND": 404,
    # malformed input: the request itself is wrong, whatever state the market is in
    "BAD_REQUEST": 400, "BAD_DISPLAY_NAME": 400, "INVALID_WALLET": 400, "INVALID_SIDE": 400,
}  # every other chain error (BELOW_MIN, ABOVE_MAX, MARKET_FULL, ALREADY_OTHER_SIDE, MARKET_NOT_OPEN, ...) is 409


def _error(http_status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"ok": False, "code": code, "detail": detail, **extra})


async def _body(request: Request) -> dict:
    try:
        data = json.loads(await request.body(), parse_float=Decimal)
    except ValueError:
        raise MarketError("BAD_REQUEST", "body must be a JSON object") from None
    if not isinstance(data, dict):
        raise MarketError("BAD_REQUEST", "body must be a JSON object")
    return data


def build_market_app(get_service: Callable[[], MarketService], logger: Logger, origin: str) -> FastAPI:
    api = FastAPI(title="Handshake Bets market API", docs_url=None, redoc_url=None)
    api.add_middleware(
        CORSMiddleware, allow_origins=[origin], allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )

    async def run(token: str, fn: Callable[[MarketService, Any], Awaitable[dict]]) -> JSONResponse:
        svc = get_service()
        try:
            bet = await svc.bet_for_token(token)
            return JSONResponse(content=await fn(svc, bet))
        except MarketError as e:
            extra = {"status": e.status} if e.status else {}
            return _error(HTTP.get(e.code, 409), e.code, e.detail, **extra)
        except ChainError as e:
            return _error(HTTP.get(e.code, 409), e.code, e.detail)
        except Exception as e:  # never leak internals or tokens
            logger.error("market_api.error", detail=err_detail(e))
            return _error(500, "INTERNAL", "unexpected error")

    @api.get("/{token}")
    async def market(token: str) -> Any:
        return await run(token, lambda svc, bet: svc.market_view(bet))

    @api.get("/{token}/participants")
    async def participants(token: str) -> Any:
        return await run(token, lambda svc, bet: svc.participants(bet))

    @api.get("/{token}/position")
    async def position(token: str, wallet: str = "") -> Any:
        return await run(token, lambda svc, bet: svc.position(bet, wallet))

    @api.get("/{token}/preview")
    async def preview(token: str, side: str = "", amount_usd: str = "") -> Any:
        return await run(token, lambda svc, bet: svc.preview(bet, side, amount_usd))

    @api.post("/{token}/participants")
    async def set_name(token: str, request: Request) -> Any:
        async def go(svc: MarketService, bet: Any) -> dict:
            body = await _body(request)
            return await svc.set_display_name(bet, body.get("wallet"), body.get("display_name"))

        return await run(token, go)

    @api.post("/{token}/deposits")
    async def deposit(token: str, request: Request) -> Any:
        if get_service().chain.mode != "mock":
            return _error(404, "NOT_FOUND", "not found")

        async def go(svc: MarketService, bet: Any) -> dict:
            body = await _body(request)
            return await svc.deposit(
                bet, body.get("wallet"), body.get("side"), body.get("amount_usd"), body.get("display_name")
            )

        return await run(token, go)

    return api
