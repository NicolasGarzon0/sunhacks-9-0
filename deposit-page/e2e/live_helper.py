"""Live check helper for the deposit page. Dev tool, not part of the app (it only imports the backend's own modules).

Run with the backend venv from the backend folder, with the same env overrides as the running backend:
  .venv/bin/python ../deposit-page/e2e/live_helper.py open|finish|status ...
Prints one JSON line. Secrets come from the app's settings loader and are never printed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR / "scripts"))
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.db import make_pool  # noqa: E402
from app.repository import Repository  # noqa: E402
from live_check import BACKEND, Live, env_live  # noqa: E402


async def main() -> int:
    cmd = sys.argv[1]
    settings = load_settings()
    admin = {"X-Admin-Secret": settings.admin_secret}
    async with httpx.AsyncClient(timeout=30) as http:
        if cmd == "finish":  # finish <game_id> <home_score> <away_score>
            r = await http.post(f"{BACKEND}/admin/games/{sys.argv[2]}/finish", headers=admin,
                                json={"home_score": int(sys.argv[3]), "away_score": int(sys.argv[4])})
            print(json.dumps({"ok": r.status_code == 200, "http": r.status_code}))
            return 0
        if cmd == "status":  # status <bet_id>
            r = await http.get(f"{BACKEND}/admin/bets/{sys.argv[2]}", headers=admin)
            bet = r.json().get("bet", {})
            print(json.dumps({"status": bet.get("status"), "winning_side": bet.get("winning_side"), "fee_paid_base": bet.get("fee_paid_base"),
                              "tasks": [(t.get("kind"), t.get("status"), t.get("amount_base")) for t in r.json().get("payout_tasks", [])],
                              "link_expires_at": r.json().get("link_expires_at")}))
            return 0
        if cmd == "say":  # say <text>: one inbound DM from the live-test number (to answer a pending question)
            from datetime import datetime, timezone
            import uuid
            pool = make_pool(settings.database_url)
            await pool.open()
            entry = await Repository(pool).get_roster_by_name("nicolas")
            r = await http.post(f"{BACKEND}/inbound", headers={"X-Gateway-Secret": settings.gateway_shared_secret},
                                json={"messageId": f"live-{uuid.uuid4()}", "handle": entry.handle, "text": " ".join(sys.argv[2:]),
                                      "receivedAt": datetime.now(timezone.utc).isoformat()})
            print(json.dumps({"ok": r.status_code == 200}))
            await pool.close()
            return 0
        if cmd == "latest":  # the newest open market of the live-test number (used when the DM flow was answered by hand)
            pool = make_pool(settings.database_url)
            await pool.open()
            repo = Repository(pool)
            entry = await repo.get_roster_by_name("nicolas")
            env = env_live()
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8789", headers={"X-Chain-Secret": env["CHAIN_SERVICE_SECRET"]}, timeout=30) as chain:
                live = Live(settings, repo, http, chain, env)
                bets = [b for b in await repo.list_bets(entry.handle) if b.status == "open"]
                bet = max(bets, key=lambda b: b.funding_closes_at)
                print(json.dumps({"link": live.signer.url(bet.id), "bet_id": bet.id, "game_id": bet.game_id, "funding_closes_at": bet.funding_closes_at.isoformat()}))
            await pool.close()
            return 0
        if cmd == "open":  # open <minutes_from_now>
            pool = make_pool(settings.database_url)
            await pool.open()
            repo = Repository(pool)
            env = env_live()
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8789", headers={"X-Chain-Secret": env["CHAIN_SERVICE_SECRET"]}, timeout=30) as chain:
                live = Live(settings, repo, http, chain, env)
                live.minutes = int(sys.argv[2])
                entry = await repo.get_roster_by_name("nicolas")
                assert entry is not None, "roster entry 'nicolas' not found"
                bet, gid = await live.open_market("page", entry.handle, "nfl-ari", "nfl-phi", "cardinals", "eagles")
                link = live.signer.url(bet.id)
                print(json.dumps({"link": link, "bet_id": bet.id, "game_id": gid, "funding_closes_at": bet.funding_closes_at.isoformat()}))
            await pool.close()
            return 0
    return 2


raise SystemExit(asyncio.run(main()))
