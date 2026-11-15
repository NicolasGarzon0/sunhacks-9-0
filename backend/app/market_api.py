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
from markets.deposits import DepositFlow
from markets.service import MarketError, MarketService

from .logging import Logger, err_detail
from .ratelimit import RateLimiter

HTTP = {
    "BAD_TOKEN": 404, "LINK_EXPIRED": 410, "NOT_FOUND": 404,
    # malformed input: the request itself is wrong, whatever state the market is in
    "BAD_REQUEST": 400, "BAD_DISPLAY_NAME": 400, "INVALID_WALLET": 400, "INVALID_SIDE": 400, "INVALID_PARAMS": 400,
    "TX_REJECTED": 400,
    "RATE_LIMITED": 429,
    # the chain or the chain service failed (not the caller's fault)
    "RPC_ERROR": 502, "SUBMIT_FAILED": 502, "UNTRUSTED_BET": 502,
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


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def build_market_app(
    get_service: Callable[[], MarketService], logger: Logger, origin: str,
    get_deposits: Callable[[], DepositFlow] | None = None, settings: Any = None,
) -> FastAPI:
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

    # ---- sponsored deposits and the airdrop (chunk 3B): real chain only, 404 on the mock ----
    ip_limit = RateLimiter(settings.public_rate_per_ip_per_min) if settings is not None else None
    wallet_limit = RateLimiter(settings.public_rate_per_wallet_per_min) if settings is not None else None

    async def real_run(request: Request, token: str, wallet: Any, fn: Callable[[DepositFlow, Any, str], Awaitable[dict]]) -> JSONResponse:
        if get_deposits is None or get_service().chain.mode != "real":
            return _error(404, "NOT_FOUND", "not found")
        ip = client_ip(request, bool(settings and settings.trust_proxy_headers))
        # limits are per process (in memory): a second backend instance counts on its own
        if ip_limit is not None and not ip_limit.hit(f"ip:{ip}"):
            return _error(429, "RATE_LIMITED", "too many requests from this address, slow down")
        if isinstance(wallet, str) and wallet_limit is not None and not wallet_limit.hit(f"w:{wallet}"):
            return _error(429, "RATE_LIMITED", "too many requests for this wallet, slow down")
        return await run(token, lambda svc, bet: fn(get_deposits(), bet, ip))  # type: ignore[misc]

    @api.get("/{token}/wallet")
    async def wallet_view(request: Request, token: str, wallet: str = "") -> Any:
        return await real_run(request, token, wallet, lambda flow, bet, ip: flow.wallet_view(bet, wallet))

    @api.post("/{token}/airdrop")
    async def airdrop(request: Request, token: str) -> Any:
        try:
            body = await _body(request)
        except MarketError as e:
            return _error(400, e.code, e.detail)
        return await real_run(request, token, body.get("wallet"), lambda flow, bet, ip: flow.airdrop(bet, body.get("wallet"), ip))

    @api.post("/{token}/deposit_tx")
    async def deposit_tx(request: Request, token: str) -> Any:
        try:
            body = await _body(request)
        except MarketError as e:
            return _error(400, e.code, e.detail)
        return await real_run(
            request, token, body.get("wallet"),
            lambda flow, bet, ip: flow.deposit_tx(bet, body.get("wallet"), body.get("side"), body.get("amount_usd")),
        )

    @api.post("/{token}/deposit_submit")
    async def deposit_submit(request: Request, token: str) -> Any:
        try:
            body = await _body(request)
        except MarketError as e:
            return _error(400, e.code, e.detail)
        return await real_run(
            request, token, body.get("wallet"),
            lambda flow, bet, ip: flow.deposit_submit(
                bet, body.get("wallet"), body.get("side"), body.get("amount_usd"), signed_tx_b64=body.get("signed_tx_b64"),
                message_b64=body.get("message_b64"), user_signature_b64=body.get("user_signature_b64"), display_name=body.get("display_name"),
            ),
        )

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
