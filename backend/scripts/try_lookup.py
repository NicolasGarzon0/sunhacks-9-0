"""Dev tool: print game candidates from the configured provider. Sends no iMessage.

    .venv/bin/python scripts/try_lookup.py cardinals
    .venv/bin/python scripts/try_lookup.py asu --opponent "texas tech"
    .venv/bin/python scripts/try_lookup.py chiefs --date 2026-11-20
    .venv/bin/python scripts/try_lookup.py arizona --json

Reads the repo-root .env (GAME_PROVIDER, LOCAL_TIMEZONE, DATABASE_URL for the simulated schedule,
SPORTS_API_KEY for realtime). The simulated provider seeds its schedule if the table is empty.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import templates as T  # noqa: E402
from app.config import ConfigError, load_settings  # noqa: E402
from app.db import make_pool  # noqa: E402
from app.logging import Logger  # noqa: E402
from app.repository import Repository  # noqa: E402
from games.factory import build_provider  # noqa: E402
from games.provider import ProviderError  # noqa: E402
from games.simulated import SimulatedProvider  # noqa: E402


async def run(args, settings) -> int:
    pool = make_pool(settings.database_url, max_size=1)
    await pool.open()
    try:
        provider = build_provider(settings, Repository(pool), Logger("warn"))
        if isinstance(provider, SimulatedProvider) and await provider.seed_if_empty():
            print("(seeded the simulated schedule)")
        day = date.fromisoformat(args.date) if args.date else None
        try:
            games = await provider.find_candidates(args.team, args.opponent, day)
        except ProviderError as e:
            print(f"provider error: {e}")
            return 1
        print(f"provider={provider.name}  team={args.team!r}  opponent={args.opponent!r}  date={day}  found={len(games)}")
        if args.json:
            print(json.dumps([g.to_json() for g in games], indent=2))
        else:
            for g in games:
                print(f"  {g.game_id:<24} {g.status:<10} {T.game_line(g, settings.local_timezone)}")
            if games:
                print("\nreply would be:\n" + T.offer(games[:5], settings.local_timezone))
            else:
                print("\nreply would be:\n" + T.no_games(args.team, day))
        return 0
    finally:
        await pool.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("team")
    ap.add_argument("--opponent")
    ap.add_argument("--date", help="YYYY-MM-DD (local)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    return asyncio.run(run(args, settings))


if __name__ == "__main__":
    sys.exit(main())
