# Handshake Bets backend (chunks 1-2: skeleton + bet extraction)

FastAPI service that receives iMessage DMs from the gateway (`../src`, contract in `../docs/imessage-gateway.md`),
dedups them in Postgres, looks the sender up in a demo roster, and replies through the gateway's `POST /send`.

Chunk 2 turns a roster member's DM into a bet request for a **game**. One Claude Haiku call per message
(`extraction/`) extracts the game (`team`, optional `opponent`, optional `date`) and classifies the message
(`bet_proposal`, `reply_to_pending`, `other`) through a single strict tool, `propose_bet`. Stake and side are
not part of the agent; they are chosen later on the betting side. Deterministic code (`app/validation.py`,
`app/drafts.py`) does everything else, and every reply is a fixed template in `app/templates.py`, sent only to
the sender of the inbound message. The LLM never decides anything about money, bet state or whom to contact.

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
| `ANTHROPIC_API_KEY` | yes | Haiku extraction; never logged |
| `HAIKU_MODEL` | no | default `claude-haiku-5-5` (current Haiku model ID) |
| `LOCAL_TIMEZONE` | no | default `America/Phoenix`; used to resolve "tomorrow", "Saturday", etc. |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | not yet | reserved for later chunks; never logged |

Startup fails with a message naming every missing required variable.

## Database
Direct Postgres via psycopg 3 (all queries in `app/repository.py`). Apply `migrations/*.sql` in order in the
Supabase SQL editor (or via the Supabase MCP): `0001_init` (inbound_messages, roster), `0002_drafts`
(one bet request draft per sender handle) and `0003_drafts_game_only` (status `collecting|ready`, pending
field `team|date`). All tables have RLS enabled with no policies, so the
anon/authenticated API keys see nothing; the backend connects as the table owner.

## Run
```sh
.venv/bin/python -m app                      # listens on 8788
.venv/bin/python scripts/seed_roster.py alice=+15550001111 "bob:Bobby=+15550002222"
```

Extraction dev tool (real Haiku call, sends no iMessage):
```sh
.venv/bin/python scripts/try_extract.py "create a bet for asu vs texas tech tomorrow"
.venv/bin/python scripts/try_extract.py "ASU vs Texas Tech" --pending team
.venv/bin/python scripts/try_extract.py --eval      # evals/extraction_cases.json, expected vs actual
```

## Bet request flow (chunk 2)
- Messages over 500 characters get a fixed "too long" reply and no model call.
- `team` is the only required field. With no game ("create a bet", "new bet") the agent asks
  "Which game is the bet on? For example: ASU vs Texas Tech." The next message is treated as the answer
  ("ASU vs Texas Tech" means team ASU, opponent Texas Tech) and merged into the draft.
- `date` is optional. If given, it must fall from today through today+14 days in `LOCAL_TIMEZONE`, or the
  agent asks for the day again. If absent, chunk 3 finds the next upcoming game.
- Ignored: amounts, odds, who the sender is backing. Never accepted: phone numbers, emails or contacts.
- A new request while one is still waiting for its game asks "Reply YES or NO", parsed without the model.
  A request that was already read back is simply replaced.
- One draft per sender. It is deleted after 30 minutes of silence.
- When the request is ready, the agent reads it back: "Got it: a bet on ASU vs Texas Tech, on Sunday Nov 15.
  Next I will look up the game." Game lookup is chunk 3.
- If extraction fails (API error, timeout, refusal, invalid output), the user gets the fixed fallback:
  "I could not read that, try: create a bet for ASU vs Texas Tech".

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
e.g. Homebrew `postgresql`). Each test uses its own schema. The gateway and Anthropic are mocked. Never touches
the live roster.
