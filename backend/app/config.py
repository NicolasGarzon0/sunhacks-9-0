"""Settings loaded from the repo-root .env (shared with the gateway) overlaid by the process environment.

PORT in that file belongs to the gateway, so the backend port comes from BACKEND_PORT, else the port in
BACKEND_URL, else 8788.

Values are never logged. Missing required values raise ConfigError naming every one of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from markets.money import MoneyError, parse_usd
from markets.pool_math import MAX_FEE_BPS

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = BACKEND_DIR.parent / ".env"

REQUIRED = (
    "DATABASE_URL", "GATEWAY_SHARED_SECRET", "ADMIN_SECRET", "ANTHROPIC_API_KEY", "LINK_SIGNING_SECRET", "DEPOSIT_PAGE_URL",
)
MOCK_TREASURY = "MockTreasury1111111111111111111111111111111"  # clearly fake; only used when CHAIN_MODE=mock
MIN_SIGNING_SECRET_LEN = 32
DEFAULT_HAIKU_MODEL = "claude-haiku-5-5"  # verified in the Claude API docs (model table, Haiku 5.5 guide)
LOG_LEVELS = ("debug", "info", "warn", "error")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    gateway_url: str
    gateway_shared_secret: str
    admin_secret: str
    port: int
    log_level: str
    anthropic_api_key: str = ""
    haiku_model: str = DEFAULT_HAIKU_MODEL
    local_timezone: str = "America/Phoenix"
    game_provider: str = "simulated"
    sports_api_key: str | None = None
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    # pool markets (chunk 4). Money is int USDC base units; dollars exist only in the env file.
    min_stake_base: int = 1_000_000
    max_stake_base: int = 20_000_000
    max_participants: int = 20
    fee_bps: int = 500
    treasury_pubkey: str = MOCK_TREASURY
    chain_mode: str = "mock"
    link_signing_secret: str = ""
    deposit_page_url: str = ""
    deposit_page_origin: str = ""
    link_grace_hours: int = 2
    funding_minutes_before_kickoff: int = 60
    settle_hours_after_kickoff: int = 6

    def __repr__(self) -> str:  # never leak secrets through repr
        return (
            f"Settings(gateway_url={self.gateway_url!r}, port={self.port}, log_level={self.log_level!r}, "
            f"haiku_model={self.haiku_model!r}, local_timezone={self.local_timezone!r}, "
            f"game_provider={self.game_provider!r}, chain_mode={self.chain_mode!r})"
        )


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments, blank lines, optional quotes)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(env: dict[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(read_env_file(env_file))
    merged.update(os.environ if env is None else env)

    def get(name: str) -> str | None:
        v = merged.get(name)
        return v.strip() if v and v.strip() else None

    missing = [n for n in REQUIRED if get(n) is None]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in the repo-root .env (see .env.example)."
        )

    gateway_url = (get("GATEWAY_URL") or "http://localhost:8787").rstrip("/")
    parsed = urlparse(gateway_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("GATEWAY_URL is not a valid http(s) URL")

    port_raw = get("BACKEND_PORT")
    if port_raw is None and (backend_url := get("BACKEND_URL")):
        try:
            p = urlparse(backend_url).port
        except ValueError:
            p = None
        port_raw = str(p) if p else None
    try:
        port = int(port_raw or "8788")
    except ValueError:
        raise ConfigError("BACKEND_PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise ConfigError("BACKEND_PORT must be an integer between 1 and 65535")

    tz = get("LOCAL_TIMEZONE") or "America/Phoenix"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("LOCAL_TIMEZONE is not a valid IANA timezone (e.g. America/Phoenix)") from None

    game_provider = (get("GAME_PROVIDER") or "simulated").lower()
    if game_provider not in ("simulated", "realtime"):
        raise ConfigError("GAME_PROVIDER must be 'simulated' or 'realtime'")
    if game_provider == "realtime" and get("SPORTS_API_KEY") is None:
        raise ConfigError("Missing required environment variable(s): SPORTS_API_KEY (needed when GAME_PROVIDER=realtime)")

    def money(name: str, default: str) -> int:
        try:
            return parse_usd(get(name) or default)
        except MoneyError as e:
            raise ConfigError(f"{name} is not a valid dollar amount ({e})") from None

    def integer(name: str, default: int, minimum: int) -> int:
        raw = get(name)
        try:
            v = int(raw) if raw is not None else default
        except ValueError:
            raise ConfigError(f"{name} must be an integer") from None
        if v < minimum:
            raise ConfigError(f"{name} must be at least {minimum}")
        return v

    min_stake = money("MIN_STAKE_USD", "1")
    max_stake = money("MAX_STAKE_USD", "20")
    if min_stake <= 0:
        raise ConfigError("MIN_STAKE_USD must be greater than 0")
    if max_stake < min_stake:
        raise ConfigError("MAX_STAKE_USD must be at least MIN_STAKE_USD")
    max_participants = integer("MAX_PARTICIPANTS", 20, 2)
    fee_bps = integer("FEE_BPS", 500, 0)
    if fee_bps > MAX_FEE_BPS:
        raise ConfigError(f"FEE_BPS {fee_bps} is above the hard cap of {MAX_FEE_BPS} (10%)")

    chain_mode = (get("CHAIN_MODE") or "mock").lower()
    if chain_mode not in ("mock", "real"):
        raise ConfigError("CHAIN_MODE must be 'mock' or 'real'")
    treasury = get("TREASURY_PUBKEY")
    if chain_mode == "real":
        if treasury is None:
            raise ConfigError("Missing required environment variable(s): TREASURY_PUBKEY (needed when CHAIN_MODE=real)")
        raise ConfigError("CHAIN_MODE=real is not implemented yet. Use CHAIN_MODE=mock.")
    treasury = treasury or MOCK_TREASURY

    secret = get("LINK_SIGNING_SECRET")
    if len(secret or "") < MIN_SIGNING_SECRET_LEN:
        raise ConfigError(f"LINK_SIGNING_SECRET must be at least {MIN_SIGNING_SECRET_LEN} characters")
    page_url = (get("DEPOSIT_PAGE_URL") or "").rstrip("/")
    page = urlparse(page_url)
    if page.scheme not in ("http", "https") or not page.netloc:
        raise ConfigError("DEPOSIT_PAGE_URL is not a valid http(s) URL")
    origin = (get("DEPOSIT_PAGE_ORIGIN") or f"{page.scheme}://{page.netloc}").rstrip("/")
    o = urlparse(origin)
    if o.scheme not in ("http", "https") or not o.netloc or o.path not in ("", "/"):
        raise ConfigError("DEPOSIT_PAGE_ORIGIN must be an origin like https://example.com (no path)")

    log_level = (get("LOG_LEVEL") or "info").lower()
    if log_level not in LOG_LEVELS:
        log_level = "info"

    return Settings(
        database_url=get("DATABASE_URL"),  # type: ignore[arg-type]
        gateway_url=gateway_url,
        gateway_shared_secret=get("GATEWAY_SHARED_SECRET"),  # type: ignore[arg-type]
        admin_secret=get("ADMIN_SECRET"),  # type: ignore[arg-type]
        port=port,
        log_level=log_level,
        anthropic_api_key=get("ANTHROPIC_API_KEY"),  # type: ignore[arg-type]
        haiku_model=get("HAIKU_MODEL") or DEFAULT_HAIKU_MODEL,
        local_timezone=tz,
        game_provider=game_provider,
        sports_api_key=get("SPORTS_API_KEY"),
        supabase_url=get("SUPABASE_URL"),
        supabase_service_role_key=get("SUPABASE_SERVICE_ROLE_KEY"),
        min_stake_base=min_stake,
        max_stake_base=max_stake,
        max_participants=max_participants,
        fee_bps=fee_bps,
        treasury_pubkey=treasury,
        chain_mode=chain_mode,
        link_signing_secret=secret,  # type: ignore[arg-type]
        deposit_page_url=page_url,
        deposit_page_origin=origin,
        link_grace_hours=integer("LINK_GRACE_HOURS", 2, 0),
        funding_minutes_before_kickoff=integer("FUNDING_MINUTES_BEFORE_KICKOFF", 60, 0),
        settle_hours_after_kickoff=integer("SETTLE_HOURS_AFTER_KICKOFF", 6, 1),
    )
