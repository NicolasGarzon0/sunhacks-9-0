"""Entry point: `python -m app` (from backend/). Reads the repo-root .env, listens on BACKEND_PORT / BACKEND_URL port (default 8788)."""

import sys

import uvicorn

from .config import ConfigError, load_settings
from .main import create_app


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(2)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
