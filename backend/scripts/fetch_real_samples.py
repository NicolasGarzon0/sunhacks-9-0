"""Save real realtimesportsapi.com NFL responses as test fixtures (stretch provider). Never prints the key.

    .venv/bin/python scripts/fetch_real_samples.py weeks --year 2026
    .venv/bin/python scripts/fetch_real_samples.py teams
    .venv/bin/python scripts/fetch_real_samples.py schedule --year 2026 --week 11
    .venv/bin/python scripts/fetch_real_samples.py event --id 401772123 --name event_final

Reads SPORTS_API_KEY from the repo-root .env (or the environment). Waits >= 1.1 s between calls
(free plan: 1 call/second). Each call counts against the monthly quota, so fetch only what you need.
Saves to tests/fixtures/realtime/<name>.json and prints the HTTP status, rate-limit headers, meta, and a
structural outline of the body.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import BACKEND_DIR, read_env_file  # noqa: E402

BASE = "https://www.realtimesportsapi.com/api/v1"
LEAGUE = "/sports/football/leagues/nfl"
OUT = ROOT / "tests" / "fixtures" / "realtime"
_last_call = [0.0]


def api_key() -> str:
    import os

    key = os.environ.get("SPORTS_API_KEY") or read_env_file(BACKEND_DIR.parent / ".env").get("SPORTS_API_KEY")
    if not key:
        print("Missing SPORTS_API_KEY in the repo-root .env", file=sys.stderr)
        sys.exit(2)
    return key


def outline(v, depth: int = 0, max_depth: int = 6) -> str:
    pad = "  " * depth
    if depth > max_depth:
        return pad + "…"
    if isinstance(v, dict):
        lines = []
        for k, x in v.items():
            if isinstance(x, (dict, list)):
                lines.append(f"{pad}{k}: {type(x).__name__}" + (f"[{len(x)}]" if isinstance(x, list) else ""))
                lines.append(outline(x, depth + 1, max_depth))
            else:
                s = json.dumps(x)
                lines.append(f"{pad}{k}: {s if len(s) <= 70 else s[:67] + '…'}")
        return "\n".join(l for l in lines if l)
    if isinstance(v, list):
        return outline(v[0], depth, max_depth) if v else pad + "(empty)"
    return pad + json.dumps(v)


def get(path: str, params: dict | None, name: str) -> dict | None:
    wait = 1.1 - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    url = f"{BASE}{path}"
    with httpx.Client(timeout=20, headers={"Authorization": f"Bearer {api_key()}", "Accept": "application/json"}) as c:
        r = c.get(url, params=params)
    print(f"GET {path} {params or ''} -> HTTP {r.status_code}")
    for h in ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
        if h in r.headers:
            print(f"  header {h}: {r.headers[h]}")
    try:
        body = r.json()
    except ValueError:
        print(f"  non-JSON body ({len(r.content)} bytes)")
        return None
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{name}.json"
    out.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"  saved {out.relative_to(ROOT)}")
    print(outline(body))
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["weeks", "teams", "schedule", "event"])
    ap.add_argument("--year", type=int)
    ap.add_argument("--week", type=int)
    ap.add_argument("--season-type", type=int, default=2)
    ap.add_argument("--id")
    ap.add_argument("--name", help="fixture file name (without .json)")
    a = ap.parse_args()
    if a.what == "weeks":
        get(f"{LEAGUE}/seasons/{a.year}/weeks", None, a.name or f"weeks_{a.year}")
    elif a.what == "teams":
        get(f"{LEAGUE}/teams", None, a.name or "teams")
    elif a.what == "schedule":
        get(f"{LEAGUE}/seasons/{a.year}/schedule", {"week": a.week, "seasonType": a.season_type},
            a.name or f"schedule_{a.year}_w{a.week}")
    else:
        get(f"{LEAGUE}/events/{a.id}", None, a.name or f"event_{a.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
