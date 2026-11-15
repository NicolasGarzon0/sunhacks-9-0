# Handshake Bets backend (chunks 1-5 and 3A: skeleton, extraction, game lookup, pool markets, lifecycle worker, real chain)

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
Direct Postgres via psycopg 3 (all queries in `app/repository.py`). Apply `migrations/*.sql` (through `0008_airdrops`) in order in the
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

> **Provisional finals:** on the real provider a `final` result can be corrected after it is first reported.
> The worker (chunk 5) settles only after two matching final reads, see "Settlement rules" below.

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
(stored for chunk 5), `CHAIN_MODE=mock` or `real` (see "Real chain" below). Fee, treasury, limits and
times are copied onto the bet at creation, so config changes never touch an open market.

**State machine** (lowercase in DB and API): `game_confirmed -> open -> locked -> settled`, or
`open|locked -> refunded`, or `game_confirmed|open -> failed`. `close_funding` (idempotent, serialized per bet)
locks when both sides have money and at least 2 participants, otherwise refunds (`too_few_participants` or
`one_sided`). It runs early when `max_participants` is reached (`maybe_close_when_full` after every deposit),
via `POST /admin/bets/{id}/close_funding`, and lazily when a deposit arrives after `funding_closes_at`
(and by the worker, chunk 5). Each proposer text goes out once, recorded in `bet_notices` (keys `bet:{id}:link|locked|
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

**Handoff, real chain and deposit page.** Implement `ChainClient` (`markets/chain.py`) against the program and
wire `build_chain_client`; drop the `mock_chain_*` tables; the page reads `/markets/{t}`, previews via
`/preview`, signs the deposit itself, then `POST /markets/{t}/participants` for the display name. Names are
unauthenticated (no wallet signature) until the page adds one.

## Lifecycle worker (chunk 5)

A background asyncio task started in the app lifespan (`WORKER_ENABLED`, default true) runs one tick every
`WORKER_TICK_SECONDS`. A Postgres advisory transaction lock means only one worker loop runs across app
instances; a tick that cannot get the lock does nothing. All time based rules use an injected clock. No model
calls. Every text is a fixed template sent only to the proposer, in `LOCAL_TIMEZONE`, with no hyphens.

```
DM -> confirm -> game_confirmed -> open ---(funding_closes_at, or full)--> locked --(confirmed final)--> settled
                                    |  \--(game cancelled / postponed)--> refunded     |  \--(tie, cancelled,
                                    |  \--(one sided / < 2 people)------> refunded     |     postponed, no result by
                                    \--(failed to open)--> failed                       |     settle_after)--> refunded
settled | refunded --> payout tasks (one claim per wallet) --> all done --> payouts_completed_at --> link grace --> 410
```

One tick, in order, each step idempotent and conditional on the current status:
a) **Reconcile**: for open and locked bets compare the DB with the chain; a chain that is already locked, settled or
refunded (a crash between the chain call and the DB write) is recorded, with its notices and tasks.
b) **Funding**: open bets whose game is cancelled or postponed are refunded; `close_funding` at `funding_closes_at`;
`maybe_close_when_full` for the rest. The simulated provider is asked every tick; the real one only at the
deadline (call budget).
c) **Results**: locked bets are polled from `kickoff + RESULT_POLL_START_HOURS_AFTER_KICKOFF` every
`RESULT_POLL_INTERVAL_MINUTES`. d) `MarketService.settle_market` settles on the chain, records winner, fee and
pool, creates winner tasks and queues the result text. e) **Payouts**: due `payout_tasks` run with concurrency 3.
f) **Notices**: queued texts are sent, transient failures retried.

**Settlement rules.** A final is provisional. The first final read stores score and winner (`result_first_seen_at`);
the market settles only when a later read reports the same score and winner at least
`SETTLE_CONFIRM_WAIT_MINUTES` after that first read. A different score or winner, or a status that leaves final,
restarts the wait. With a wait of 0 (the simulated default) one read is enough. Side 0 = away team, side 1 = home
team. Equal scores refund everyone (`tie`). No confirmed final by `settle_after` (kickoff +
`SETTLE_HOURS_AFTER_KICKOFF`) refunds with `no_result`. Fee and payout math come only from `pool_math` and the
chain client.

**Refund reasons** (`bets.refund_reason`): `one_sided`, `too_few_participants` (chunk 4 text), `cancelled`,
`postponed`, `tie`, `no_result` (the `refund_result` text). Refunds never charge a fee.

**Payout tasks** (`payout_tasks`, unique per bet and wallet): after settled only winners get a `win` task, after
refunded every participant gets a `refund` task. `chain.claim` success marks it done with `tx_ref`;
`ALREADY_CLAIMED` counts as done; `NOT_A_WINNER`, `NOT_FOUND` and `INVALID_WALLET` fail at once; anything else
retries with `CLAIM_BACKOFF_SECONDS` doubling, and after `CLAIM_MAX_ATTEMPTS` the task is `failed`, an error is
logged and one `payouts_delayed` text goes to the proposer. When no task is pending or failed the bet gets
`payouts_completed_at`, which starts the link grace countdown.

**Notices** (`bet_notices`): kinds `link locked refunded failed result refund_result payouts_delayed`, idempotency
key `bet:{id}:{kind}`. `RATE_LIMITED`, `SEND_FAILED`, `UNREACHABLE` retry with backoff (30 s doubling) up to
`NOTICE_MAX_ATTEMPTS`; `NOT_ALLOWED` and `BAD_REQUEST` are permanent (`permanent_failure`) and never retried.
Known gap: if a send times out and is retried after the gateway's 10 minute idempotency window, the proposer could
get the same text twice.

**Real provider budget.** Every real HTTP call is counted per calendar month (UTC) in `provider_usage`.
`SPORTS_CALL_BUDGET_MONTHLY` (default 100) caps them; when used up the provider raises, a `rts.budget_exhausted`
error is logged once, results stop being polled and the `settle_after` rule refunds the market. Lookups get the
fixed apology meanwhile.

**Env vars** (all optional, in `.env.example`): `WORKER_ENABLED=true`, `WORKER_TICK_SECONDS=15`,
`RESULT_POLL_START_HOURS_AFTER_KICKOFF=3`, `RESULT_POLL_INTERVAL_MINUTES=10`, `SETTLE_CONFIRM_WAIT_MINUTES=10`
(the last three default to 0 with `GAME_PROVIDER=simulated`), `SPORTS_CALL_BUDGET_MONTHLY=100`,
`CLAIM_MAX_ATTEMPTS=8`, `CLAIM_BACKOFF_SECONDS=30`, `NOTICE_MAX_ATTEMPTS=6`, `LINK_GRACE_MINUTES` (overrides
`LINK_GRACE_HOURS`, for rehearsals). Invalid values stop startup with a clear message. Migration `0006_worker.sql`.

**Worker admin endpoints** (`X-Admin-Secret`): `POST /admin/worker/tick` (one tick now), `GET /admin/worker/status`
(last tick, bets, tasks and notices per status, permanent notice failures), `POST /admin/payout_tasks/{id}/retry`
(failed tasks only), `POST /admin/games/{id}/result` `{home_score, away_score}` (simulated; equal scores make a
tie; `/finish` is the older alias), `POST /admin/games/{id}/status` `{status: scheduled|in_progress|postponed|
cancelled}` (simulated). `GET /admin/bets/{id}` now lists the payout tasks.

## Real chain (chunk 3A)

`CHAIN_MODE=real` makes the factory return `RealChainClient` (`markets/real_chain.py`): the `ChainClient` interface over
HTTP to **chain-service** (`../chain-service`), which holds the payer and oracle keys and talks to the `handshake_pool`
program on devnet. The backend never holds a key. Env: `CHAIN_SERVICE_URL`, `CHAIN_SERVICE_SECRET` (both required in real
mode), `TREASURY_PUBKEY` (required; must equal the chain service's, which rejects bets with another treasury),
`CONTRACT_REFUND_BUFFER_HOURS` (default 12), `PAYER_MIN_SOL_ALERT` (default 0.3), `CHAIN_HEALTH_EVERY_TICKS` (default 4).

- **Client rules.** Service codes map to `ChainError` codes (the old ones plus `FUNDING_CLOSED`, `PAST_REFUND_DEADLINE`,
  `REFUND_TOO_EARLY`, `BOTH_SIDES_REQUIRED`, `UNTRUSTED_BET`, `INVALID_PARAMS`, `RPC_ERROR`). Every service call is idempotent, so
  transport errors, timeouts, 429, 5xx and `RPC_ERROR` are retried (0.5, 1, 2 s); business errors are final. A claim whose
  position is gone raises `ALREADY_CLAIMED`, which the worker treats as done.
- **Interface additions.** `close_loser_position(bet_id, wallet)`, `close_bet(bet_id)`, `health()`. `BetParams` now carries
  `funding_closes_at` and `refund_after`; `open_market` sets `refund_after = settle_after + CONTRACT_REFUND_BUFFER_HOURS`.
  After `refund_after` the oracle cannot lock or settle any more (anyone can refund on chain), so a late `close_funding` or
  `settle_market` refunds everyone with reason `no_result` instead.
- **Mock parity** (`markets/mock_chain.py`, see `contracts/README.md` for the full list): lock needs both sides and 2
  participants; deposits stop at `funding_closes_at` in the contract's error order; lock and settle stop at `refund_after`;
  claiming closes the position (a second claim, or a claim for a wallet with no position, is `ALREADY_CLAIMED`); losing positions
  stay until `close_loser_position` (no transfer); `get_positions(bet_id, depositors)` lists the open positions of the named wallets only (the real chain never scans the program: it derives each Position PDA and batch-reads them; the backend passes `bet_participants`, which every deposit fills before it is sent); `close_bet` sweeps the dust and
  makes later reads `NOT_FOUND`; `emergency_refund` (anyone at or after `refund_after`) exists on the mock for tests.
  `tests/test_chain_parity.py` runs the same scenarios against the mock and against `RealChainClient` over a fake chain service
  (`tests/fake_chain_service.py`); `tests/test_real_chain.py` covers mapping and retries.
- **Snapshots** (migration `0007_snapshots.sql`, apply like the earlier ones; re-runnable, RLS on, no policies). On chain a
  position disappears when it is claimed or closed, so `position_snapshots(bet_id, wallet, side, amount_base, payout_base,
  status held|paid|refunded|closed_loser)` is filled from `chain.get_positions` when funding closes (inside `close_funding`,
  before and right after the lock or refund), in `refund_market`, and by the worker's reconcile pass if a locked market has none.
  Task completion updates `payout_base` and `status`. Views (`/markets/{token}`, `/participants`, `/position`) of locked, settled
  and refunded markets read the snapshots and the `bets` row, not the chain. Open markets still read the chain. Display names
  stay in `bet_participants`.
- **Worker additions.** After settle, every losing position gets a `close_loser` task (kind added by 0007): retried like a
  claim, no text on failure, and it does not block `payouts_completed_at` (only `win` and `refund` tasks do). Once the link has
  expired and no task is pending the worker calls `close_bet` once (vault and bet rent back to the payer), recorded in
  `bets.chain_closed_at`, failures logged and retried with backoff (60 s doubling, max 6 h). Every `CHAIN_HEALTH_EVERY_TICKS`
  ticks the worker reads the chain service balances; a payer below `PAYER_MIN_SOL_ALERT` logs `worker.payer_sol_low` at error
  level and shows in `GET /admin/worker/status` under `chain_health`.
- **Live check** (devnet): start `chain-service`, start this backend with `CHAIN_MODE=real` and short timings
  (`FUNDING_MINUTES_BEFORE_KICKOFF=3`, `SETTLE_HOURS_AFTER_KICKOFF=1`, `CONTRACT_REFUND_BUFFER_HOURS=1`, `LINK_GRACE_MINUTES=1`,
  `WORKER_TICK_SECONDS=10`), then `.venv/bin/python scripts/live_check.py --chain-log <chain service log> [--only settled]`.
  It drives a settled, a one sided and a cancelled market through the DM flow, `dev_deposit` and the worker. It sends real
  texts to the roster entry `nicolas`.
- **Deposits** come from the deposit page in chunk 3B (user signs, sponsor co-signs). Until then
  `chain-service/scripts/dev_deposit.ts` makes them.

## Sponsored deposits and the airdrop (chunk 3B)

With `CHAIN_MODE=real` the market API also serves the deposit page (full reference in `docs/deposit-page-api.md`; on the mock these
four answer 404 and the mock `POST /markets/{token}/deposits` stays):
`GET /markets/{token}/wallet?wallet=`, `POST /markets/{token}/airdrop`, `POST /markets/{token}/deposit_tx`,
`POST /markets/{token}/deposit_submit` (`markets/deposits.py`, `DepositFlow`). The backend never signs: `deposit_tx` asks chain-service
for the one unsigned transaction and adds a summary (team, amount, fee line, payout preview net of fee including the wallet's existing
stake); `deposit_submit` forwards the user's signature, and chain-service verifies the exact bytes before the sponsor co-signs (rules in
`chain-service/README.md`). On success it stores the display name (validated before anything is sent), calls `maybe_close_when_full`
and returns the signature, the explorer link and the updated market view. Dollars become base units with `parse_usd` (more than 6
decimals, negatives and non numbers are `BAD_REQUEST`). HTTP: rule errors 409 with the code, bad input 400, bad token 404, expired
link 410, rate limit 429, chain or chain service failure 502 (`RPC_ERROR`, `SUBMIT_FAILED`, `UNTRUSTED_BET`).

**Airdrop policy** (`POST /airdrop`, only when `AIRDROP_ENABLED=true`; table `airdrops`, migration `0008_airdrops.sql`, re-runnable, RLS on,
no policies): a valid link and a valid base58 wallet are required. The allowance is `AIRDROP_MAX_PER_WALLET_USD` (default 25) for the
whole life of a wallet; a repeat request with the allowance used up returns `ok`, `minted_base: 0` and the current balance and mints
nothing; a partly used allowance is topped up to the cap. `AIRDROP_DAILY_CAP_USD` (default 500, UTC day) limits all wallets together
(`AIRDROP_LIMIT`), `AIRDROP_PER_IP_PER_HOUR` (default 5) limits one address (`RATE_LIMITED`). The reservation is taken under an advisory
lock before minting and released again when the mint fails; a mint is never retried after the chain service answered.

**Rate limits are in memory, so per process** (a second backend instance counts on its own): the public endpoints above are limited per
IP (`PUBLIC_RATE_PER_IP_PER_MIN`, default 30) and per wallet (`PUBLIC_RATE_PER_WALLET_PER_MIN`, default 10) per minute. The client IP is
the peer address; `TRUST_PROXY_HEADERS=true` takes the first `X-Forwarded-For` entry instead, only turn it on behind a proxy you control.

Env: `AIRDROP_ENABLED`, `AIRDROP_MAX_PER_WALLET_USD`, `AIRDROP_DAILY_CAP_USD`, `AIRDROP_PER_IP_PER_HOUR`, `PUBLIC_RATE_PER_IP_PER_MIN`,
`PUBLIC_RATE_PER_WALLET_PER_MIN`, `TRUST_PROXY_HEADERS`. chain-service additionally takes `MINT_AUTHORITY_SECRET_KEY` (airdrop),
`DEPOSIT_CUTOFF_SECONDS`, `DEPOSIT_BUILD_TTL_SECONDS` and its own rate limit settings.

**Other notes for the real client.**
- The worker watches deposits through `maybe_close_when_full` and `chain.get_bet` on every tick for every open bet: those are
  single account reads on chain.
- If a kickoff time changes after a market opens, `funding_closes_at` and `settle_after` are not recomputed (and the chain
  already has them).

## Rehearsals on devnet (chunk 6)

`scripts/rehearsal.py` proves the stages that need a finished game (lock, settle, payouts, refunds, close_bet, expired link) on real
devnet, without waiting for one. **It does not fake the chain.** It moves the simulated game and the market's timestamps in the database,
and the *running* backend worker does the real work (chain-service, devnet, the deposit page). The script only changes rows, waits, and
then verifies against devnet (RPC), the database and the page. Dev only: nothing in it is reachable over HTTP and it adds no endpoint.

**Refusals.** Every command first checks `GAME_PROVIDER=simulated`, `CHAIN_MODE=real` and that chain-service's `/health` says
`cluster: devnet` (the service itself aborts on any other genesis hash), that the backend worker is ticking, and that the running backend
answers the simulated game admin endpoint (it answers 409 on the real provider). Otherwise it prints `REFUSED` and exits 2.

**What it changes** (each change prints a line starting `CHANGED`):

| Stage | Change in the database | What the worker then does |
|---|---|---|
| lock | `bets.funding_closes_at` to one minute ago | `close_funding`: snapshots, `lock` on chain, `locked` text |
| result | `bets.kickoff_utc` and `sim_games.kickoff_utc` ten minutes back, `sim_games` row set to final (`finish_sim_game`) | polls the result, `settle` on chain, winner `claim`s, loser `close_loser_position`s, `result` text |
| cancelled / postponed | `sim_games.status` (before the lock) | `refund_all` on chain, refund payouts, `refund_result` text |
| link grace | `bets.payouts_completed_at` moved back past the grace (the running process keeps `LINK_GRACE_HOURS`, so no restart) | `close_bet` (vault dust to the treasury, rent back to the payer); the link answers 410 |

The on chain `funding_closes_at` is not touched (the contract's `lock` only checks `refund_after`), and `settle_after` stays in the future.

```sh
.venv/bin/python scripts/rehearsal.py check                       # guards + the newest bets
.venv/bin/python scripts/rehearsal.py settle                      # scenario 1 on the open market of the live test number
.venv/bin/python scripts/rehearsal.py settle --fresh              # ... or on a new market with two local wallets ($10 away, $5 home)
.venv/bin/python scripts/rehearsal.py refund-one-sided --wallets 1   # scenario 2, one wallet: refund reason too_few_participants
.venv/bin/python scripts/rehearsal.py refund-one-sided --wallets 2   # two wallets, same side: reason one_sided
.venv/bin/python scripts/rehearsal.py refund-game cancelled       # scenario 3 (also: postponed), two wallets on opposite sides
.venv/bin/python scripts/rehearsal.py cleanup [--purge-games]
```

New markets are opened through `MarketService.open_market` (the function the text flow's YES calls, so the proposer gets the real link
text), on a simulated game `sim:rehearsal-<tag>` that kicks off in two hours. Deposits use the page's own sponsored flow through
`chain-service/scripts/deposit_client.ts` (airdrop, `deposit_tx`, local signature, `deposit_submit`) with two reused wallets (`rehearsal-pool-0/1`) in the gitignored
`chain-service/.dev-wallets/`, airdropped only when they hold less than the stake (the airdrop is limited to 5 per hour per address). Verification reads devnet through `chain-service/scripts/rehearsal_txs.ts` (transactions of the Bet
account with their instruction names, whether each Position account still exists) and
`deposit-page/e2e/rehearsal_page.mjs` (real Chrome, reports which view the page shows, screenshots in `deposit-page/screenshots/`,
gitignored). Results of the first full run: `docs/chunk-6-rehearsal-report.md`. The command exits 1 if any check fails and prints a markdown table of every check.

**Cleanup and reruns.** Each stage looks at the current state first, so a command interrupted halfway can be run again (for `settle`, the
balances taken before the scenario are kept in the gitignored `scripts/.rehearsal_state/<bet>.json`). A settled market cannot be settled
twice: to repeat scenario 1 use `settle --fresh`. `cleanup` drives any unfinished rehearsal market to the end (cancels its game so the
worker refunds it, moves the grace so the worker closes the Bet account) and, with `--purge-games`, deletes the finished `sim:rehearsal-*`
game rows. `POST /admin/games/reset` restores the normal schedule (it replaces every `sim_games` row). Every market the script opens
ends with `close_bet`, so no rent stays locked. Each fresh wallet costs the payer about 0.0015 SOL (token account) and each market
about 0.01 SOL, and the airdrop gives 25 test USDC per wallet.

**Limits of a rehearsal.** `SETTLE_CONFIRM_WAIT_MINUTES` is 0 for the simulated provider, so the settle needs one result read, not two.
To rehearse the two read rule restart the backend with `SETTLE_CONFIRM_WAIT_MINUTES=1` (a process env override, the `.env` file is
not touched). The grace is moved in the database rather than waited out, so the real countdown is covered by the unit tests only.

## Endpoints
- `POST /inbound` (`X-Gateway-Secret`): 401 bad secret, 400 bad payload, 200 duplicate (no-op), 200 accepted
  (reply sent in a background task with idempotency key `reply:<messageId>`), 503 if the DB is down (gateway retries).
- `POST /admin/roster` `{name, display_name?, handle}` and `GET /admin/roster` (`X-Admin-Secret`).
- `GET /health`: `{status, database, gateway}`.

## Tests
```sh
.venv/bin/pytest -q            # 491 tests with the mock chain (and the fake chain service)
```
Uses `TEST_DATABASE_URL` if set, otherwise spins up a throwaway local Postgres (needs `initdb`/`pg_ctl`,
e.g. Homebrew `postgresql`). Each test uses its own schema. The gateway and Anthropic are mocked. Never touches
the live roster.
