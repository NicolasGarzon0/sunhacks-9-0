# Handshake Bets backend (chunks 1-3: skeleton, bet extraction, game lookup)

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
| `GAME_PROVIDER` | no | `simulated` (default) or `realtime` |
| `SPORTS_API_KEY` | only if realtime | realtimesportsapi.com key; never logged |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | not yet | reserved for later chunks; never logged |

Startup fails with a message naming every missing required variable.

## Database
Direct Postgres via psycopg 3 (all queries in `app/repository.py`). Apply `migrations/*.sql` in order in the
Supabase SQL editor (or via the Supabase MCP): `0001_init` (inbound_messages, roster), `0002_drafts`
(one bet request draft per sender handle) `0003_drafts_game_only` (status `collecting|ready`, pending
field `team|date`) and `0004_games_and_bets` (draft candidates for confirmation, `sim_games`, `bets`). All tables have RLS enabled with no policies, so the
anon/authenticated API keys see nothing; the backend connects as the table owner.
Migration `0005_markets` is safe to run more than once. The code does not rely on column defaults or on
`bet_notices.updated_at`, so it also works on a database whose 0005 tables were created by an earlier variant.

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
- When the request is valid, the agent looks up the game right away (chunk 3, below). That reply replaces
  the old chunk 2 readback, so the sender gets one message.
- If extraction fails (API error, timeout, refusal, invalid output), the user gets the fixed fallback:
  "I could not read that, try: create a bet for ASU vs Texas Tech".

## Game lookup and confirmation (chunk 3)
Package `games/`:
- **Provider interface** (`games/provider.py`), `GameProvider`:
  - `find_candidates(team, opponent, date) -> list[Game]` returns scheduled games with a kickoff after now
    that involve the typed team. The opponent narrows the matches. With a date, it searches that local
    calendar day; without one, now through +14 days. Results are soonest first. An unknown team or opponent
    returns `[]`, and any failure raises `ProviderError`.
  - `get_status(game_id)` returns `scheduled | in_progress | final | postponed | cancelled`.
  - `get_result(game_id)` returns `GameResult(home_score, away_score, official)`. Chunk 5 settles on this.
  - A `Game` holds a canonical `game_id`, `league`, `home`/`away` (canonical id + display name),
    `kickoff_utc` and `status`.
- **Provider selection:** `GAME_PROVIDER=simulated` (default) or `realtime` (NFL only; stretch, see below).
- **Team aliases** (`games/data/teams.json`):
  - Each team has a canonical `id`, a `league`, a display `name` and `aliases`.
  - Matching lowercases the typed name, strips punctuation, drops a leading "the", tolerates a trailing "s"
    either way, then matches exactly.
  - An alias shared by several teams ("arizona") offers all of their games, so the sender can choose.
  - To add a nickname, append it to `aliases`. To add a team, append an object.
- **Simulated schedule** (`games/data/schedule.json`):
  - Each game has a `key` (its id becomes `sim:<key>`), `league`, `away`, `home`, `local_time` "HH:MM"
    (in `LOCAL_TIMEZONE`), and either `day_offset` (days from today) or `weekday` (the next occurrence,
    1-7 days ahead). Keep games within 14 days.
  - Kickoffs are computed at seed time and stored in `sim_games`, so state survives a restart.
  - Seeding happens on startup when the table is empty, or with `POST /admin/games/reset`. After editing
    the file, call reset.
- **Flow:**
  - 0 games: "I could not find a game for X in the next 14 days. Which team or day…" The draft stays
    collecting.
  - 1 game: "I found <away> at <home>, <day, time>. Is this the game? Reply YES or NO."
  - 2-5 games (soonest first): a numbered list, "Reply with a number, or NO."
  - Confirmations are parsed by code: yes/y/yep/yeah, no/n/nope, or a number. "yes" only counts when one
    game was offered.
  - Other text is a miss. The first miss repeats the question; from the second on, the reply explains how
    to answer. The model only checks whether the text is a new bet request, which then gets the
    "Replace it? YES/NO" question.
  - NO asks which game was meant.
  - A game that kicked off before the confirmation is refused.
  - Confirming opens a pool market (chunk 4, below) and texts the proposer the bet link.
  - Confirming the same game twice returns the existing market's link.
  - If the provider fails, the sender gets a fixed apology and the draft stays collecting.
- **Dev tool:** `.venv/bin/python scripts/try_lookup.py asu --opponent "texas tech" [--date YYYY-MM-DD]`
  prints the candidates and the reply. It sends nothing.

Simulated game admin (`X-Admin-Secret`; returns 409 with the real provider):
- `POST /admin/games/{game_id}/finish` `{"home_score": 27, "away_score": 24}` sets status `final`, official.
- `POST /admin/games/{game_id}/status` `{"status": "postponed" | "cancelled" | "in_progress"}`.
- `POST /admin/games/reset` restores the schedule from `schedule.json`, anchored to today.

Real provider (`GAME_PROVIDER=realtime`, NFL only), `games/realtime.py`:
- **API:** realtimesportsapi.com, called with `Authorization: Bearer $SPORTS_API_KEY`. It uses the teams,
  weeks, schedule (`?week=N&seasonType=T`) and single-event endpoints. Game ids look like `rts:nfl:<eventId>`.
- **Verified response shapes** (fixtures in `tests/fixtures/realtime/`, fetched 2026-11-14 with
  `scripts/fetch_real_samples.py`):
  - Envelope `{success, data, meta}`, where `meta.rateLimit = {limit, remaining, reset}`.
  - A game has `date` like `"2026-11-15T21:05Z"`, `status {state, completed, detail}`, and
    `homeTeam`/`awayTeam` with `abbreviation` and `score`.
- **Status mapping:**
  - `state: "pre"` → scheduled (verified)
  - `state: "post"` with `completed` or a detail starting "Final" → final (verified). It is **provisional**.
  - `state: "in"` → in_progress (**unverified**: no live sample yet)
  - detail containing "Postponed" → postponed, and "Cancel" → cancelled (**unverified**)
- **Aliases:** the API's display names, short names, cities, abbreviations and slugs, plus
  `games/data/nfl_nicknames.json`, plus the `nfl-*` aliases in `teams.json`. Canonical ids are `nfl-<abbr>`.
- **Quota protection (free plan: 1,000 calls in month 1, then 125/month, 1 call/s):**
  - Teams and schedules are cached for 6 h, weeks for 24 h, and a 404 is cached too. Single events are
    cached for 60 s.
  - Calls are spaced at least 1.1 s apart.
  - Every call logs `rts.call` with `quotaRemaining`.
  - An HTTP 429 blocks calls until `Retry-After` passes, and lookups send the fixed apology meanwhile.
  - WebSockets and webhooks aren't used (they need a paid plan).
  - A cold lookup costs about 5 calls: teams, weeks, and up to 3 schedule weeks.
- **Selecting it without editing `.env`:** `GAME_PROVIDER=realtime .venv/bin/python -m app`. Process
  environment variables override the `.env` file.

> **Chunk 5 warning, provisional finals:** on the real provider a `final` result can be corrected after it
> is first reported. Settlement must treat `final` as provisional: wait a few minutes, re-read the
> result, and settle only if it is unchanged.

## Pool markets (chunk 4)

**Pool design.** One shared pool per market. Anyone with the link picks a side (side 0 = away team, side 1 =
home team) and deposits their own amount. Min applies per deposit, max to a wallet's total position, a wallet
may top up one side but never hold both. The fee is `floor(losing_pool * fee_bps / 10000)`, charged only when a
market settles with a winner; `distributable = total - fee`; a winner gets `floor(stake * distributable /
winning_total)` (never below their stake; rounding dust stays in the vault). One sided markets, fewer than 2
participants, cancelled games and ties refund everyone with no fee. All money is integer USDC base units (6
decimals); dollars exist only at the API and config boundary. `markets/pool_math.py` is the single source of
truth. The backend never holds user funds or keys: deposits come from the user's wallet via the deposit page
(the mock simulates this).

**Env vars** (placeholders in `.env.example`): `LINK_SIGNING_SECRET` (required, 32+ chars), `DEPOSIT_PAGE_URL`
(required), `DEPOSIT_PAGE_ORIGIN` (CORS; default origin of the URL), `MIN_STAKE_USD=1`, `MAX_STAKE_USD=20`,
`MAX_PARTICIPANTS=20`, `FEE_BPS=500` (above 1000 fails at startup), `TREASURY_PUBKEY` (mock falls back to a
fake constant), `LINK_GRACE_HOURS=2`, `FUNDING_MINUTES_BEFORE_KICKOFF=60`, `SETTLE_HOURS_AFTER_KICKOFF=6`
(stored for chunk 5), `CHAIN_MODE=mock` (`real` fails with a clear message for now). Fee, treasury, limits and
times are copied onto the bet at creation, so config changes never touch an open market.

**State machine** (lowercase in DB and API): `game_confirmed -> open -> locked -> settled`, or
`open|locked -> refunded`, or `game_confirmed|open -> failed`. `close_funding` (idempotent, serialized per bet)
locks when both sides have money and at least 2 participants, otherwise refunds (`too_few_participants` or
`one_sided`). It runs early when `max_participants` is reached (`maybe_close_when_full` after every deposit),
via `POST /admin/bets/{id}/close_funding`, and lazily when a deposit arrives after `funding_closes_at`
(no worker yet). Each proposer text goes out once, recorded in `bet_notices` (keys `bet:{id}:link|locked|
refunded|failed`), because the gateway's own idempotency is in memory only.

**Link lifetime.** `DEPOSIT_PAGE_URL/b/<token>`, token = signed bet id (itsdangerous), multi use. Valid while
the signature is good, the bet exists, and now is before `payouts_completed_at + LINK_GRACE_HOURS`, else
`settle_after + 7 days`. Funding closing never invalidates it. Bad token: 404 `BAD_TOKEN`; expired: 410
`LINK_EXPIRED`. Logs carry the bet id, never the token.

**Public API** (`/markets`, CORS for `DEPOSIT_PAGE_ORIGIN` only; `{ok:true,...}` or `{ok:false,code,detail}`):
- `GET /markets/{t}` status, teams, kickoff, funding_closes_at, pools, participant_count, limits, fee_bps,
  multipliers; plus `game_pending` (locked), `winning_side/fee_paid/distributable/final_multiplier` (settled),
  `refund_reason` (refunded).
- `GET /markets/{t}/participants`, `GET /markets/{t}/position?wallet=`, `POST /markets/{t}/participants`
  `{wallet, display_name}` (max 24 chars, trimmed, no URLs).
- `GET /markets/{t}/preview?side=1&amount_usd=20` (open only), `POST /markets/{t}/deposits`
  `{wallet, side, amount_usd, display_name?}` (open only, `CHAIN_MODE=mock` only, else 404).
- Example: `{"ok":true,"status":"open","pools":{"side0_base":10000000,"side1_base":10000000,"side0_usd":"10.00",
  "side1_usd":"10.00"},"multipliers":{"side0":"1.95","side1":"1.95"},"fee_bps":500,...}`.

**Error codes.** Chain: `BELOW_MIN ABOVE_MAX MARKET_FULL ALREADY_OTHER_SIDE MARKET_NOT_OPEN WRONG_STATE
ALREADY_CLAIMED NOT_A_WINNER NOT_FOUND INVALID_WALLET INVALID_SIDE`. In the public API `INVALID_WALLET` and
`INVALID_SIDE` are HTTP 400 (malformed input); every other chain error is 409 (`MARKET_NOT_OPEN` also returns
the current `status`). API: `BAD_TOKEN` 404, `LINK_EXPIRED` 410, `BAD_REQUEST` / `BAD_DISPLAY_NAME` 400.

**Admin** (`X-Admin-Secret`): `GET /admin/bets/{id}`, `POST /admin/bets/{id}/close_funding`, dev only
`POST /admin/bets/{id}/mark_payouts_complete`, and (simulated provider) `POST /admin/games`
`{home_id, away_id, minutes_from_now}`.

**Handoff, chunk 5.** The worker must call `close_funding` at `funding_closes_at` (marked `TODO(chunk 5)` in
`markets/service.py`), retry `bet_notices` rows that are `pending` or `failed`, then at `settle_after` re-read
the game (finals are provisional), call `chain.settle(winner)` or `refund_all` (`cancelled`, `tie`), trigger
claims, send the result text, and set `payouts_completed_at` once every position is claimed.
**Handoff, real chain and deposit page.** Implement `ChainClient` (`markets/chain.py`) against the program and
wire `build_chain_client`; drop the `mock_chain_*` tables; the page reads `/markets/{t}`, previews via
`/preview`, signs the deposit itself, then `POST /markets/{t}/participants` for the display name. Names are
unauthenticated (no wallet signature) until the page adds one.

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
