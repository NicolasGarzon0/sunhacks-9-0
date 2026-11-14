# Handshake Bets backend (chunk 1: skeleton)

FastAPI service that receives iMessage DMs from the gateway (`../src`, contract in `../docs/imessage-gateway.md`),
dedups them in Postgres, looks the sender up in a demo roster, and replies through the gateway's `POST /send`.

## Setup
```sh
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```
The backend reads the repo-root `.env`, shared with the gateway (see the root `.env.example`).
`PORT` in that file is the gateway's and is ignored here.

## Env vars (names only)
| Name | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Supabase Postgres connection string |
| `GATEWAY_SHARED_SECRET` | yes | same value as the gateway's |
| `ADMIN_SECRET` | yes | `X-Admin-Secret` for `/admin/*` |
| `GATEWAY_URL` | no | default `http://localhost:8787` |
| `BACKEND_PORT` | no | else the port in `BACKEND_URL`, else `8788` |
| `LOG_LEVEL` | no | `debug` also logs message text |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | not yet | reserved for later chunks; never logged |

Startup fails with a message naming every missing required variable.

## Database
Direct Postgres via psycopg 3 (all queries in `app/repository.py`). Apply `migrations/*.sql` in order in the
Supabase SQL editor (or via the Supabase MCP). Both tables have RLS enabled with no policies, so the
anon/authenticated API keys see nothing; the backend connects as the table owner.

## Run
```sh
.venv/bin/python -m app                      # listens on 8788
.venv/bin/python scripts/seed_roster.py alice=+15550001111 "bob:Bobby=+15550002222"
```

## Endpoints
- `POST /inbound` (`X-Gateway-Secret`): 401 bad secret, 400 bad payload, 200 duplicate (no-op), 200 accepted
  (reply sent in a background task with idempotency key `reply:<messageId>`), 503 if the DB is down (gateway retries).
- `POST /admin/roster` `{name, display_name?, handle}` and `GET /admin/roster` (`X-Admin-Secret`).
- `GET /health`: `{status, database, gateway}`.

## Tests
```sh
.venv/bin/pytest -q
```
Uses `TEST_DATABASE_URL` if set, otherwise spins up a throwaway local Postgres (needs `initdb`/`pg_ctl`,
e.g. Homebrew `postgresql`). Each test uses its own schema. The gateway is mocked. Never touches the live roster.
