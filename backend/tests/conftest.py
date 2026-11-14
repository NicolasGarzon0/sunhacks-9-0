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

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
GATEWAY_SECRET = "test-gateway-secret"
ADMIN_SECRET = "test-admin-secret"


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
    )


@pytest.fixture
async def client(settings, repo, gateway, logs):
    app = create_app(settings, repo=repo, gateway=gateway, logger=logs.logger)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test") as c:
        yield c
