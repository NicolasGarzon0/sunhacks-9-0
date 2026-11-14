from __future__ import annotations

import json
from decimal import Decimal
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from markets.money import usd_to_base
from markets.service import MarketError, MarketService


def _error(exc: MarketError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "code": exc.code, "detail": exc.detail},
    )


def create_market_api(get_service: Callable[[], MarketService | None], origin: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

    async def resolve(token: str):
        service = get_service()
        if service is None:
            raise MarketError("UNAVAILABLE", "market service unavailable", 503)
        return service, await service.resolve(token)

    @app.get("/{token}")
    async def market(token: str):
        try:
            service, bet = await resolve(token)
            return {"ok": True, "market": await service.view(bet)}
        except MarketError as exc:
            return _error(exc)

    @app.get("/{token}/participants")
    async def participants(token: str):
        try:
            service, bet = await resolve(token)
            return {"ok": True, "participants": await service.participants(bet)}
        except MarketError as exc:
            return _error(exc)

    @app.get("/{token}/position")
    async def position(token: str, wallet: str):
        try:
            service, bet = await resolve(token)
            return {"ok": True, "position": await service.position(bet, wallet)}
        except MarketError as exc:
            return _error(exc)

    @app.post("/{token}/participants")
    async def participant_name(token: str, request: Request):
        try:
            service, bet = await resolve(token)
            body = json.loads(await request.body())
            wallet = body.get("wallet")
            display_name = body.get("displayName")
            if not isinstance(wallet, str) or not isinstance(display_name, str) or not display_name.strip():
                raise MarketError("BAD_REQUEST", "wallet and displayName are required", 400)
            return {"ok": True, "participant": await service.set_display_name(bet, wallet, display_name)}
        except (json.JSONDecodeError, AttributeError):
            return JSONResponse(status_code=400, content={"ok": False, "code": "BAD_REQUEST", "detail": "invalid JSON body"})
        except MarketError as exc:
            return _error(exc)

    async def amount_body(request: Request) -> tuple[int, int]:
        body = json.loads((await request.body()).decode(), parse_float=Decimal)
        side = body.get("side")
        if side not in (0, 1):
            raise MarketError("INVALID_SIDE", "side must be 0 or 1", 409)
        if "amountBase" in body:
            amount = body["amountBase"]
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise MarketError("INVALID_AMOUNT", "amountBase must be an integer", 409)
        else:
            amount = usd_to_base(body.get("amount"))
        return side, amount

    @app.get("/{token}/preview")
    async def preview(token: str, side: int, amountBase: int | None = None):
        try:
            service, bet = await resolve(token)
            amount = amountBase
            if amount is None:
                raise MarketError("BAD_REQUEST", "amountBase is required", 400)
            return {"ok": True, "preview": await service.preview(bet, side, amount)}
        except (ValueError, TypeError) as exc:
            return JSONResponse(status_code=409, content={"ok": False, "code": "INVALID_AMOUNT", "detail": str(exc)})
        except MarketError as exc:
            return _error(exc)

    @app.post("/{token}/deposits")
    async def deposit(token: str, request: Request):
        try:
            service, bet = await resolve(token)
            if service.settings.chain_mode != "mock":
                raise MarketError("NOT_FOUND", "deposits are unavailable", 404)
            body = json.loads((await request.body()).decode(), parse_float=Decimal)
            wallet = body.get("wallet")
            if not isinstance(wallet, str):
                raise MarketError("INVALID_WALLET", "wallet is required", 409)
            side, amount = await amount_body(request)
            return {"ok": True, "position": await service.deposit(bet, wallet, side, amount)}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "code": "BAD_REQUEST", "detail": str(exc)})
        except MarketError as exc:
            return _error(exc)

    return app
