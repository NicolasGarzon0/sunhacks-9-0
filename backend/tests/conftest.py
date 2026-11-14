"""Test fixtures.

Database: TEST_DATABASE_URL if set, otherwise an ephemeral local Postgres cluster (initdb in a temp dir,
unix socket only, random port). Each test gets its own schema with the migration applied, dropped afterwards.
Tests never read .env and never touch the live roster.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import psycopg
import pytest

from app.config import Settings
from app.db import make_pool
from app.gateway_client import GatewayClient
from app.logging import Logger
from app.main import create_app
from app.repository import Repository
from extraction.schema import Extraction
from games.simulated import SimulatedProvider
from games.teams import TeamDirectory

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
TEST_SCHEDULE = Path(__file__).resolve().parent / "fixtures" / "schedule_test.json"
GATEWAY_SECRET = "test-gateway-secret"
ADMIN_SECRET = "test-admin-secret"
SIGNING_SECRET = "test-signing-secret-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_url():
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("no TEST_DATABASE_URL and no local postgres binaries (initdb/pg_ctl)")
    tmp = Path(tempfile.mkdtemp(prefix="hb-pg-"))
    data, sock = tmp / "data", tmp / "sock"
    sock.mkdir()
    port = _free_port()
    subprocess.run([initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        [pg_ctl, "-D", str(data), "-w", "-l", str(tmp / "pg.log"),
         "-o", f"-p {port} -k {sock} -c listen_addresses=''", "start"],
        check=True, stdout=subprocess.DEVNULL,
    )
    try:
        yield f"postgresql://postgres@/postgres?host={sock}&port={port}"
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
async def repo(pg_url):
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f'create schema "{schema}"')
        conn.execute(f'set search_path to "{schema}"')
        for f in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(f.read_text())
    pool = make_pool(pg_url, options=f"-c search_path={schema}")
    await pool.open()
    try:
        yield Repository(pool)
    finally:
        await pool.close()
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f'drop schema "{schema}" cascade')


class LogCapture:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.logger = Logger("debug", write=self.lines.append)

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        parsed = [json.loads(line) for line in self.lines]
        return [e for e in parsed if name is None or e["event"] == name]


@pytest.fixture
def logs() -> LogCapture:
    return LogCapture()


class FakeGateway:
    """Records /send calls; responds via a swappable responder."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.responder: Callable[[dict[str, Any]], httpx.Response] = lambda body: httpx.Response(
            200, json={"ok": True, "messageId": f"out-{len(self.sends)}"}
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        self.sends.append(body)
        self.headers.append(request.headers)
        return self.responder(body)


@pytest.fixture
def fake_gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
async def gateway(fake_gateway, logs, sleeps):
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_gateway.handle))
    gw = GatewayClient("http://gateway.test", GATEWAY_SECRET, logs.logger, client=client, sleep=fake_sleep)
    yield gw
    await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql://unused",
        gateway_url="http://gateway.test",
        gateway_shared_secret=GATEWAY_SECRET,
        admin_secret=ADMIN_SECRET,
        port=8788,
        log_level="debug",
        anthropic_api_key="test-anthropic-key",
        local_timezone="America/Phoenix",
        link_signing_secret=SIGNING_SECRET,
        deposit_page_url="https://deposit.test",
        deposit_page_origin="https://deposit.test",
    )


# Saturday 2026-11-14, 12:00 in Phoenix (UTC-7, no DST).
FIXED_NOW = datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 14)


class Clock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self.current = now

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> Clock:
    return Clock()


class FakeExtractor:
    """Scripted extractor: queue Extraction objects (or None for failure, or an Exception to raise)."""

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def push(self, classification: str = "bet_proposal", **fields: Any) -> None:
        self.queue.append(Extraction(classification=classification, **fields))

    def fail(self) -> None:
        self.queue.append(None)

    async def extract(self, text, today, tz, pending_field=None):
        self.calls.append({"text": text, "today": today, "tz": tz, "pending_field": pending_field})
        item = self.queue.pop(0) if self.queue else None
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def extractor() -> FakeExtractor:
    return FakeExtractor()


@pytest.fixture
async def app(settings, repo, gateway, logs, extractor, clock, provider):
    application = create_app(
        settings, repo=repo, gateway=gateway, logger=logs.logger, extractor=extractor, provider=provider, now=clock
    )
    application.state.test_provider = provider
    return application


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test") as c:
        yield c


@pytest.fixture
def market(app):
    """The MarketService the app uses (mock chain on the test schema)."""
    return app.state.get_market()


@pytest.fixture
def chain(market):
    return market.chain


@pytest.fixture
async def provider(repo, clock):
    p = SimulatedProvider(repo, TeamDirectory.from_file(), "America/Phoenix", now=clock, schedule_file=TEST_SCHEDULE)
    await p.seed_if_empty()
    return p
