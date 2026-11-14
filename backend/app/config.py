"""Settings loaded from the repo-root .env (shared with the gateway) overlaid by the process environment.

PORT in that file belongs to the gateway, so the backend port comes from BACKEND_PORT, else the port in
BACKEND_URL, else 8788.

Values are never logged. Missing required values raise ConfigError naming every one of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = BACKEND_DIR.parent / ".env"

REQUIRED = ("DATABASE_URL", "GATEWAY_SHARED_SECRET", "ADMIN_SECRET", "ANTHROPIC_API_KEY")
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
    min_stake_base: int = 100
    max_stake_base: int = 2000
    max_participants: int = 20
    fee_bps: int = 500
    treasury_pubkey: str = "MockTreasury1111111111111111111111111111111"
    link_signing_secret: str = ""
    deposit_page_url: str = ""
    deposit_page_origin: str = ""
    link_grace_hours: int = 2
    funding_minutes_before_kickoff: int = 60
    settle_hours_after_kickoff: int = 6
    chain_mode: str = "mock"

    def __repr__(self) -> str:  # never leak secrets through repr
        return (
            f"Settings(gateway_url={self.gateway_url!r}, port={self.port}, log_level={self.log_level!r}, "
            f"haiku_model={self.haiku_model!r}, local_timezone={self.local_timezone!r}, "
            f"game_provider={self.game_provider!r})"
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

    def decimal_base(name: str, default: str) -> int:
        raw = get(name) or default
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise ConfigError(f"{name} must be a nonnegative USD amount") from None
        if value < 0 or value.as_tuple().exponent < -2:
            raise ConfigError(f"{name} must be a nonnegative USD amount with at most two decimals")
        return int(value * 100)

    min_stake_base = decimal_base("MIN_STAKE_USD", "1")
    max_stake_base = decimal_base("MAX_STAKE_USD", "20")
    if min_stake_base <= 0 or max_stake_base < min_stake_base:
        raise ConfigError("MIN_STAKE_USD and MAX_STAKE_USD define an invalid range")
    try:
        max_participants = int(get("MAX_PARTICIPANTS") or "20")
        fee_bps = int(get("FEE_BPS") or "500")
        link_grace_hours = int(get("LINK_GRACE_HOURS") or "2")
        funding_minutes = int(get("FUNDING_MINUTES_BEFORE_KICKOFF") or "60")
        settle_hours = int(get("SETTLE_HOURS_AFTER_KICKOFF") or "6")
    except ValueError:
        raise ConfigError("market timing and limit settings must be integers") from None
    if max_participants < 2:
        raise ConfigError("MAX_PARTICIPANTS must be at least 2")
    if not 0 <= fee_bps <= 1000:
        raise ConfigError("FEE_BPS must be between 0 and 1000")
    if min(link_grace_hours, funding_minutes, settle_hours) < 0:
        raise ConfigError("market timing settings must be nonnegative")
    chain_mode = (get("CHAIN_MODE") or "mock").lower()
    if chain_mode not in ("mock", "real"):
        raise ConfigError("CHAIN_MODE must be 'mock' or 'real'")
    link_secret = get("LINK_SIGNING_SECRET")
    deposit_url = get("DEPOSIT_PAGE_URL")
    if not link_secret or len(link_secret) < 32:
        raise ConfigError("LINK_SIGNING_SECRET is required and must be at least 32 characters")
    if not deposit_url:
        raise ConfigError("DEPOSIT_PAGE_URL is required")
    try:
        deposit_origin = get("DEPOSIT_PAGE_ORIGIN") or f"{urlparse(deposit_url).scheme}://{urlparse(deposit_url).netloc}"
        if urlparse(deposit_url).scheme not in ("http", "https") or not urlparse(deposit_url).netloc:
            raise ValueError
    except ValueError:
        raise ConfigError("DEPOSIT_PAGE_URL is not a valid http(s) URL") from None
    treasury = get("TREASURY_PUBKEY") or "MockTreasury1111111111111111111111111111111"
    if chain_mode == "real" and not get("TREASURY_PUBKEY"):
        raise ConfigError("TREASURY_PUBKEY is required when CHAIN_MODE=real")

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
        min_stake_base=min_stake_base,
        max_stake_base=max_stake_base,
        max_participants=max_participants,
        fee_bps=fee_bps,
        treasury_pubkey=treasury,
        link_signing_secret=link_secret,
        deposit_page_url=deposit_url,
        deposit_page_origin=deposit_origin.rstrip("/"),
        link_grace_hours=link_grace_hours,
        funding_minutes_before_kickoff=funding_minutes,
        settle_hours_after_kickoff=settle_hours,
        chain_mode=chain_mode,
    )
