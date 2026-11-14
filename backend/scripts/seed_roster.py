"""Add or update roster entries through the running backend's POST /admin/roster.

Names and handles come from the command line only, so no real numbers are committed.

    .venv/bin/python scripts/seed_roster.py alice=+15551234567 "bob:Bobby B=+15557654321"

Each argument is NAME[:DISPLAY NAME]=HANDLE. The handle is sent exactly as typed.
Reads ADMIN_SECRET (and the backend port) from the repo-root .env or the environment.
Use --url to target another backend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_settings  # noqa: E402
from app.logging import mask_handle  # noqa: E402


def parse_entry(arg: str) -> dict[str, str]:
    left, sep, handle = arg.rpartition("=")
    if not sep or not left or not handle:
        raise ValueError(f"expected NAME[:DISPLAY]=HANDLE, got {arg.split('=')[0]!r}=...")
    name, _, display = left.partition(":")
    entry = {"name": name.strip(), "handle": handle}
    if display.strip():
        entry["display_name"] = display.strip()
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entries", nargs="+", help="NAME[:DISPLAY NAME]=HANDLE")
    ap.add_argument("--url", help="backend base URL (default http://localhost:$PORT)")
    args = ap.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    base = (args.url or f"http://localhost:{settings.port}").rstrip("/")

    try:
        entries = [parse_entry(a) for a in args.entries]
    except ValueError as e:
        print(f"Bad argument: {e}", file=sys.stderr)
        return 2

    failures = 0
    with httpx.Client(timeout=10) as client:
        for entry in entries:
            masked = mask_handle(entry["handle"])
            try:
                r = client.post(f"{base}/admin/roster", json=entry, headers={"X-Admin-Secret": settings.admin_secret})
            except httpx.HTTPError as e:
                print(f"{entry['name']} ({masked}): request failed: {type(e).__name__}", file=sys.stderr)
                failures += 1
                continue
            if r.status_code == 200:
                print(f"{entry['name']} ({masked}): ok")
            else:
                failures += 1
                print(f"{entry['name']} ({masked}): HTTP {r.status_code} {r.json().get('code', '')}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
