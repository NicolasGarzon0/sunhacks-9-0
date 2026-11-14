"""FastAPI app: POST /inbound, admin roster endpoints, GET /health.

Run (from backend/): .venv/bin/python -m app
"""

from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Callable, Literal

import psycopg
from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from extraction.client import HaikuExtractor
from games.factory import build_provider
from games.provider import GameProvider
from games.simulated import SimulatedProvider

from .config import Settings, load_settings
from .drafts import BetFlow, Extractor
from .db import make_pool
from .gateway_client import GatewayClient
from .handlers import Inbound, handle_inbound
from .logging import Logger, err_detail
from .repository import Repository

NonEmpty = Annotated[str, StringConstraints(min_length=1, max_length=5000)]
HandleStr = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^\S+$")]


class InboundPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    messageId: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    handle: HandleStr
    text: str = Field(max_length=10000)
    receivedAt: datetime


class FinishIn(BaseModel):
    home_score: int = Field(ge=0, le=999)
    away_score: int = Field(ge=0, le=999)


class StatusIn(BaseModel):
    status: Literal["postponed", "cancelled", "in_progress"]


class RosterIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None
    handle: HandleStr


def _secret_ok(given: str | None, expected: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


def _err(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "code": code, "detail": detail})


def create_app(
    settings: Settings | None = None,
    *,
    repo: Repository | None = None,
    gateway: GatewayClient | None = None,
    logger: Logger | None = None,
    extractor: Extractor | None = None,
    provider: GameProvider | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Pass repo/gateway/extractor in tests; otherwise they are built from settings in the lifespan."""
    settings = settings or load_settings()
    logger = logger or Logger(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = None
        owns_gateway = False
        if app.state.repo is None:
            pool = make_pool(settings.database_url)
            await pool.open(wait=False)
            app.state.repo = Repository(pool)
            try:
                await asyncio.wait_for(app.state.repo.ping(), timeout=10)
                logger.info("db.connected")
            except Exception as e:
                logger.error("db.unreachable", detail=err_detail(e))
        if app.state.gateway is None:
            app.state.gateway = GatewayClient(settings.gateway_url, settings.gateway_shared_secret, logger)
            owns_gateway = True
        owns_extractor = False
        if app.state.extractor is None:
            app.state.extractor = HaikuExtractor(settings.anthropic_api_key, settings.haiku_model, logger)
            owns_extractor = True
        owns_provider = False
        if app.state.provider is None:
            app.state.provider = build_provider(settings, app.state.repo, logger, now=now)
            owns_provider = True
        if isinstance(app.state.provider, SimulatedProvider):
            try:
                if await app.state.provider.seed_if_empty():
                    logger.info("games.seeded", provider="simulated")
            except Exception as e:
                logger.error("games.seed_failed", detail=err_detail(e))
        logger.info(
            "backend.started", gameProvider=app.state.provider.name, port=settings.port, gatewayUrl=settings.gateway_url, model=settings.haiku_model,
            timezone=settings.local_timezone,
        )
        try:
            yield
        finally:
            if owns_provider and hasattr(app.state.provider, "aclose"):
                await app.state.provider.aclose()
            if owns_extractor:
                await app.state.extractor.aclose()
            if owns_gateway:
                await app.state.gateway.aclose()
            if pool is not None:
                await pool.close()
            logger.info("backend.stopped")

    app = FastAPI(title="Handshake Bets backend", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.repo = repo
    app.state.gateway = gateway
    app.state.logger = logger
    app.state.extractor = extractor
    app.state.provider = provider
    app.state.flow = None

    def get_flow() -> BetFlow:
        if app.state.flow is None:
            app.state.flow = BetFlow(
                app.state.repo, app.state.gateway, app.state.extractor, logger,
                provider=app.state.provider, tz=settings.local_timezone, now=now,
            )
        return app.state.flow

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _err(400, "BAD_REQUEST", "invalid request body")

    @app.post("/inbound")
    async def inbound(
        request: Request,
        background: BackgroundTasks,
        x_gateway_secret: Annotated[str | None, Header()] = None,
    ) -> Any:
        if not _secret_ok(x_gateway_secret, settings.gateway_shared_secret):
            logger.warn("inbound.unauthorized")
            return _err(401, "UNAUTHORIZED", "missing or invalid X-Gateway-Secret")
        try:
            payload = InboundPayload.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            logger.warn("inbound.bad_request", detail=type(e).__name__)
            return _err(400, "BAD_REQUEST", "invalid inbound payload")

        repo_: Repository = app.state.repo
        try:
            is_new = await repo_.record_inbound(payload.messageId, payload.handle, payload.receivedAt)
        except Exception as e:
            # 503 -> the gateway retries with backoff.
            logger.error("inbound.db_error", messageId=payload.messageId, detail=err_detail(e))
            return _err(503, "UNAVAILABLE", "database unavailable")

        if not is_new:
            logger.info("inbound.duplicate", handle=payload.handle, messageId=payload.messageId)
            return {"ok": True, "duplicate": True}

        logger.info(
            "inbound.accepted", handle=payload.handle, messageId=payload.messageId, textLength=len(payload.text)
        )
        msg = Inbound(payload.messageId, payload.handle, payload.text, payload.receivedAt)
        background.add_task(handle_inbound, msg, repo_, app.state.gateway, logger, get_flow())
        return {"ok": True}

    def _admin_denied(secret: str | None) -> JSONResponse | None:
        if _secret_ok(secret, settings.admin_secret):
            return None
        logger.warn("admin.unauthorized")
        return _err(401, "UNAUTHORIZED", "missing or invalid X-Admin-Secret")

    @app.post("/admin/roster")
    async def admin_upsert_roster(
        body: RosterIn, x_admin_secret: Annotated[str | None, Header()] = None
    ) -> Any:
        if denied := _admin_denied(x_admin_secret):
            return denied
        display = body.display_name or body.name
        try:
            entry = await app.state.repo.upsert_roster(body.name, display, body.handle)
        except psycopg.errors.UniqueViolation:
            return _err(409, "CONFLICT", "handle already belongs to another roster name")
        except psycopg.errors.CheckViolation:
            return _err(400, "BAD_REQUEST", "invalid name or handle")
        logger.info("admin.roster_upserted", name=entry.name, handle=entry.handle)
        return {"ok": True, "entry": entry.__dict__}

    @app.get("/admin/roster")
    async def admin_list_roster(x_admin_secret: Annotated[str | None, Header()] = None) -> Any:
        if denied := _admin_denied(x_admin_secret):
            return denied
        entries = await app.state.repo.list_roster()
        return {"ok": True, "roster": [e.__dict__ for e in entries]}

    def _sim_provider() -> SimulatedProvider | JSONResponse:
        p = app.state.provider
        if not isinstance(p, SimulatedProvider):
            return _err(409, "CONFLICT", "game admin endpoints only work with GAME_PROVIDER=simulated")
        return p

    @app.post("/admin/games/reset")
    async def admin_games_reset(x_admin_secret: Annotated[str | None, Header()] = None) -> Any:
        if denied := _admin_denied(x_admin_secret):
            return denied
        p = _sim_provider()
        if isinstance(p, JSONResponse):
            return p
        n = await p.reset()
        logger.info("admin.games_reset", games=n)
        return {"ok": True, "games": n}

    @app.post("/admin/games/{game_id}/finish")
    async def admin_games_finish(
        game_id: str, body: FinishIn, x_admin_secret: Annotated[str | None, Header()] = None
    ) -> Any:
        if denied := _admin_denied(x_admin_secret):
            return denied
        p = _sim_provider()
        if isinstance(p, JSONResponse):
            return p
        if not await app.state.repo.finish_sim_game(game_id, body.home_score, body.away_score):
            return _err(404, "NOT_FOUND", "unknown game_id")
        logger.info("admin.game_finished", gameId=game_id)
        result = await p.get_result(game_id)
        return {"ok": True, "result": result.__dict__}

    @app.post("/admin/games/{game_id}/status")
    async def admin_games_status(
        game_id: str, body: StatusIn, x_admin_secret: Annotated[str | None, Header()] = None
    ) -> Any:
        if denied := _admin_denied(x_admin_secret):
            return denied
        p = _sim_provider()
        if isinstance(p, JSONResponse):
            return p
        if not await app.state.repo.set_sim_game_status(game_id, body.status):
            return _err(404, "NOT_FOUND", "unknown game_id")
        logger.info("admin.game_status", gameId=game_id, status=body.status)
        return {"ok": True, "game_id": game_id, "status": await p.get_status(game_id)}

    @app.get("/health")
    async def health() -> Any:
        async def db_ok() -> bool:
            try:
                return await asyncio.wait_for(app.state.repo.ping(), timeout=2.0)
            except Exception:
                return False

        database, gateway_ok = await asyncio.gather(db_ok(), app.state.gateway.health())
        status = "ok" if database and gateway_ok else "degraded"
        return {"status": status, "database": database, "gateway": gateway_ok}

    return app


def app_factory() -> FastAPI:
    """For `uvicorn app.main:app_factory --factory`."""
    return create_app()
