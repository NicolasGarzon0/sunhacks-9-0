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

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = BACKEND_DIR.parent / ".env"

REQUIRED = ("DATABASE_URL", "GATEWAY_SHARED_SECRET", "ADMIN_SECRET")
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
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None

    def __repr__(self) -> str:  # never leak secrets through repr
        return f"Settings(gateway_url={self.gateway_url!r}, port={self.port}, log_level={self.log_level!r})"


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
        supabase_url=get("SUPABASE_URL"),
        supabase_service_role_key=get("SUPABASE_SERVICE_ROLE_KEY"),
    )
