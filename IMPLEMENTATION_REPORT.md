# Chunk 4 implementation report

## 1. Executive summary

This implementation extends the existing Handshake Bets application from game confirmation
(chunk 3) into a persisted shared pool market layer (chunk 4).

The implementation adds:

- Integer-only USD and pool arithmetic.
- Signed, multi-use deposit links.
- A `ChainClient` abstraction and a PostgreSQL-backed mock chain.
- Market lifecycle management.
- Participant, position, preview, and deposit APIs.
- Durable proposer notification idempotency.
- Funding close, lock, refund, and participant-cap behavior.
- Market configuration and startup validation.
- Market-specific database tables and columns.
- Focused tests and backend documentation.

The implementation does not add model calls to the market layer. The existing Claude extraction
flow remains responsible only for identifying games. All market, money, participant, and state
decisions are deterministic.

No commit was created. The repository `.env` file was not read or modified.

## 2. Starting architecture

The repository contains two cooperating processes:

1. A TypeScript Spectrum/iMessage gateway.
2. A Python FastAPI/PostgreSQL backend.

The gateway receives iMessage DMs through Spectrum and forwards them to the backend using
`POST /inbound`. The backend performs roster lookup, game extraction, validation, game lookup,
and sends replies back through the gateway's `POST /send` endpoint.

Before this implementation, a confirmed game created a `bets` row with:

```text
status = game_confirmed
```

and returned a fixed message indicating that a deposit link would be created next.

The market implementation changes this confirmation boundary so a confirmed game can become an
open market with a signed deposit link.

## 3. Plan alignment decisions

The implementation followed the supplied chunk 4 plan and adapted it to the actual repository.

### 3.1 Lowercase statuses

The existing database uses lowercase status values. The market state machine therefore uses:

```text
game_confirmed
open
locked
settled
refunded
failed
```

The active-market uniqueness rule applies to:

```text
game_confirmed
open
locked
```

Legacy chunk 3 `game_confirmed` rows are converted to `failed` during migration because they do
not have the market snapshot and chain state required by the new layer.

### 3.2 Configuration compatibility

Production configuration loaded through `load_settings()` requires:

- `LINK_SIGNING_SECRET`
- `DEPOSIT_PAGE_URL`

Directly constructed test `Settings` objects without these values continue to operate in the
legacy mode, which preserves existing chunk 1 through chunk 3 tests and fixtures. Market services
are only instantiated when the new market configuration is present.

### 3.3 Dependency handling

The plan explicitly approved adding `itsdangerous==2.2.0`. It was:

- Added to [`backend/pyproject.toml`](backend/pyproject.toml).
- Installed into the existing backend virtual environment.

The existing virtual environment still contains an x86_64 `pydantic_core` binary, which cannot
load on the current arm64 machine. That environment issue prevented the full Python test suite
from collecting.

## 4. Files added

### 4.1 `backend/markets/__init__.py`

Defines the new market package.

The package intentionally contains no FastAPI or Anthropic dependencies in its pure domain
modules.

### 4.2 `backend/markets/money.py`

Provides integer money conversion helpers:

- `usd_to_base`
  - Accepts `Decimal`, string, or integer values.
  - Rejects floats.
  - Rejects negative, non-finite, or more-than-two-decimal values.
  - Converts USD to cents.
- `base_to_usd`
  - Converts nonnegative integer cents to a two-decimal string.
- `format_usd`
  - Adds a dollar sign to an integer amount.
- `bps_to_percent`
  - Converts basis points to a percent string such as `500 -> "5%"`.

The market layer uses integer cents internally and does not use floating-point arithmetic.

### 4.3 `backend/markets/pool_math.py`

Implements pure pool calculations:

- `compute_fee`
  - Calculates `floor(losing_pool * fee_bps / 10000)`.
- `compute_distributable`
  - Subtracts the losing-side fee from the total pool.
- `compute_payout`
  - Calculates `floor(stake * distributable / winning_total)`.
- `preview_payout_for_deposit`
  - Calculates the projected payout after adding a deposit.
- `implied_multiplier`
  - Returns a two-decimal string multiplier.
- `settle_amounts`
  - Calculates the fee, distributable amount, and per-wallet payouts.

Zero winning pools are handled without division errors. Integer division leaves any rounding dust
in the vault.

### 4.4 `backend/markets/chain.py`

Defines the chain abstraction:

- `ChainError`
- `BetParams`
- `ChainBet`
- `Position`
- `Claim`
- `ChainClient` protocol
- `build_chain_client`

The protocol supports:

- Bet creation.
- Bet lookup.
- Position lookup.
- Deposits.
- Locking.
- Settlement.
- Refunds.
- Claims.

`CHAIN_MODE=mock` selects the implemented mock chain. `CHAIN_MODE=real` raises a clear
`NotImplementedError`, as required by the plan.

Typed chain error codes include:

```text
INVALID_WALLET
INVALID_SIDE
INVALID_AMOUNT
BET_NOT_FOUND
BET_NOT_OPEN
ALREADY_PARTICIPANT
PARTICIPANT_CAP
POSITION_LIMIT
ONE_SIDE_ONLY
ALREADY_CLAIMED
NOT_A_WINNER
```

### 4.5 `backend/markets/mock_chain.py`

Implements the mock chain using PostgreSQL tables:

- `mock_chain_bets`
- `mock_chain_positions`

Important behavior:

- Wallets must be base58-like strings of 32 to 44 characters.
- Sides must be `0` or `1`.
- Amounts must be positive integer base units.
- Deposits must be at least the configured minimum.
- A wallet cannot exceed its maximum total position.
- A wallet cannot participate on both sides.
- New-wallet participant caps are enforced.
- Existing wallets may top up even after the participant cap is reached.
- Deposits are rejected after `funding_closes_at`.
- Deposits require the chain bet to be `open`.
- Locking transitions `open -> locked`.
- Settlement transitions `locked -> settled`.
- Refund transitions `open|locked -> refunded`.
- Winner claims are supported after settlement.
- Refund claims return the wallet's deposited amount.
- Repeated claims return `ALREADY_CLAIMED`.
- Loser claims return `NOT_A_WINNER`.

Deposits lock the chain bet row with `SELECT ... FOR UPDATE`, allowing participant counts and
wallet limits to be checked atomically.

### 4.6 `backend/markets/links.py`

Implements signed links with `itsdangerous.URLSafeSerializer`.

Properties:

- Salt: `bet-link`.
- Payload: bet ID only.
- URL shape:

```text
DEPOSIT_PAGE_URL/b/<signed-token>
```

- Invalid signatures return `BAD_TOKEN`.
- Link expiration is based on:
  - `payouts_completed_at + LINK_GRACE_HOURS`, when payouts are marked complete.
  - `settle_after + 7 days`, when payouts have not been marked complete.

The module also derives the default CORS origin from `DEPOSIT_PAGE_URL`.

### 4.7 `backend/markets/service.py`

Contains market application logic:

- Link resolution and expiry enforcement.
- Market view construction.
- Participant listing.
- Position lookup.
- Display-name validation.
- Payout preview.
- Deposit handling.
- Market opening.
- Durable notification sending.
- Funding close.
- Automatic close when the participant cap is reached.

The service owns per-bet async locks for funding close operations.

Market notification idempotency uses durable `bet_notices` rows with keys such as:

```text
bet:<bet-id>:link
bet:<bet-id>:locked
bet:<bet-id>:refunded
bet:<bet-id>:failed
```

This supplements the gateway's in-memory ten-minute idempotency cache.

## 5. Database migration

### 5.1 Migration file

[`backend/migrations/0005_markets.sql`](backend/migrations/0005_markets.sql)

### 5.2 `bets` extensions

The migration adds:

- `side0_name`
- `side1_name`
- `chain_bet_id`
- `fee_bps`
- `treasury`
- `min_stake_base`
- `max_stake_base`
- `max_participants`
- `funding_closes_at`
- `settle_after`
- `opened_at`
- `locked_at`
- `settled_at`
- `winning_side`
- `total_pool_base`
- `fee_paid_base`
- `failure_reason`
- `refund_reason`
- `payouts_completed_at`

Constraints validate:

- Supported bet statuses.
- Fee basis points from `0` to `1000`.
- Winning side as `0`, `1`, or null.
- Supported refund reasons.

The existing active uniqueness index is replaced with one covering:

```text
game_confirmed, open, locked
```

An index on `bets.status` is also added.

### 5.3 `bet_participants`

Stores:

- Bet ID.
- Wallet.
- Optional display name.
- Creation timestamp.

The primary key is `(bet_id, wallet)`.

### 5.4 `bet_notices`

Stores durable notification state:

- Bet ID.
- Notice kind.
- Idempotency key.
- Status: `pending`, `sent`, or `failed`.
- Attempt count.
- Last error.
- Creation timestamp.
- Sent timestamp.

The primary key is `(bet_id, kind)`.

### 5.5 Mock chain tables

`mock_chain_bets` stores:

- Chain state.
- Serialized parameters.
- Vault balance.
- Paid fee.
- Distributable amount.
- Winning side.
- Treasury balance.

`mock_chain_positions` stores:

- Wallet and side.
- Amount.
- Claimed state.
- Payout amount.
- Closed state.
- Creation and update timestamps.

All new tables have RLS enabled without public policies, matching the existing database security
model.

## 6. Backend configuration

### 6.1 Updated file

[`backend/app/config.py`](backend/app/config.py)

### 6.2 New settings

The `Settings` dataclass now contains:

- `min_stake_base`
- `max_stake_base`
- `max_participants`
- `fee_bps`
- `treasury_pubkey`
- `link_signing_secret`
- `deposit_page_url`
- `deposit_page_origin`
- `link_grace_hours`
- `funding_minutes_before_kickoff`
- `settle_hours_after_kickoff`
- `chain_mode`

### 6.3 Defaults

```text
MIN_STAKE_USD=1
MAX_STAKE_USD=20
MAX_PARTICIPANTS=20
FEE_BPS=500
LINK_GRACE_HOURS=2
FUNDING_MINUTES_BEFORE_KICKOFF=60
SETTLE_HOURS_AFTER_KICKOFF=6
CHAIN_MODE=mock
```

The mock treasury key defaults to:

```text
MockTreasury1111111111111111111111111111111
```

Real mode requires an explicit `TREASURY_PUBKEY`.

### 6.4 Validation

Startup rejects:

- Invalid USD values.
- More than two decimal places.
- Invalid stake ranges.
- Participant caps below two.
- Fees above `1000` basis points.
- Negative timing values.
- Unsupported chain modes.
- Missing or short signing secrets.
- Missing or invalid deposit URLs.
- Realtime mode without a sports API key.

## 7. Repository changes

### 7.1 Updated file

[`backend/app/repository.py`](backend/app/repository.py)

### 7.2 New repository functionality

The repository now provides:

- Full market-column selection.
- Create-or-get market behavior.
- Market lookup by bet ID.
- Whitelisted market updates.
- Participant listing.
- Participant display-name upsert.
- Durable notice attempt tracking.
- Durable notice result tracking.
- Simulated-game creation for admin/live-check workflows.

All SQL remains centralized in the repository layer.

## 8. Confirmation flow changes

### 8.1 Updated file

[`backend/app/drafts.py`](backend/app/drafts.py)

### 8.2 Confirmation behavior

When a user confirms a candidate game and market configuration is enabled:

1. The provider status is rechecked.
2. The kickoff and funding-close times are validated.
3. A market row is created or reused.
4. The mock chain bet is created idempotently.
5. The bet row becomes `open`.
6. A signed link notification is sent through the durable notice system.
7. The draft is deleted.

If the game is no longer scheduled or the funding window has closed, the draft returns to the
team-selection state and the user receives a fixed response.

If opening fails after the row exists:

1. The bet is marked `failed`.
2. A best-effort chain refund is attempted.
3. A durable failure notice is sent.
4. The normal reply path is suppressed to avoid sending a duplicate failure message.

When market settings are absent in directly constructed legacy test settings, the previous
`game_confirmed` behavior remains available.

### 8.3 Lookup filtering

Game lookup now excludes games whose funding window would close before the current time. This
prevents offering games that cannot be opened.

## 9. Public market API

### 9.1 New file

[`backend/app/market_api.py`](backend/app/market_api.py)

The API is a separate FastAPI sub-application mounted under `/markets` with its own CORS middleware.

The configured `DEPOSIT_PAGE_ORIGIN` is the only allowed origin.

### 9.2 Endpoints

#### `GET /markets/{token}`

Returns:

- Market ID.
- Status.
- Away/home team names.
- Kickoff.
- Funding close time.
- Settlement eligibility time.
- Minimum and maximum deposit limits.
- Participant cap.
- Fee basis points and formatted percentage.
- Total and side-specific pool amounts.
- Canonical deposit link.

#### `GET /markets/{token}/participants`

Returns participant wallet/display-name records.

#### `GET /markets/{token}/position?wallet=...`

Returns the wallet's position with:

- Masked wallet.
- Side.
- Amount in base units and formatted USD.
- Claim status.
- Payout in base units and formatted USD.

#### `POST /markets/{token}/participants`

Accepts:

```json
{
  "wallet": "base58-wallet",
  "displayName": "Example"
}
```

Display names are trimmed, limited to 24 characters, and reject URL-like values.

#### `GET /markets/{token}/preview`

Accepts `side` and `amountBase` query parameters and returns projected payout values.

Preview rejects closed markets with:

```text
409 MARKET_NOT_OPEN
```

#### `POST /markets/{token}/deposits`

Accepts:

```json
{
  "wallet": "base58-wallet",
  "side": 0,
  "amountBase": 100
}
```

Deposits are available only in mock mode.

The response contains the updated masked position. A participant-cap deposit can automatically
trigger funding close.

### 9.3 Error status behavior

- Invalid token: `404 BAD_TOKEN`.
- Expired token: `410 LINK_EXPIRED`.
- Closed market preview/deposit: `409 MARKET_NOT_OPEN`.
- Chain validation failures: `409` with the chain error code.
- Invalid request bodies: `400 BAD_REQUEST`.
- Real-mode deposits: `404`.

## 10. Admin endpoints

### 10.1 Updated file

[`backend/app/main.py`](backend/app/main.py)

All admin endpoints require `X-Admin-Secret`.

### 10.2 Market endpoints

#### `POST /admin/bets/{bet_id}/close_funding`

Closes an open market:

- Locks when there are at least two positions and both sides are represented.
- Refunds when there are too few participants.
- Refunds when all participants are on one side.
- Sends durable proposer notifications.

#### `GET /admin/bets/{bet_id}`

Returns:

- Market view.
- Participant list.
- Chain positions.

#### `POST /admin/bets/{bet_id}/mark_payouts_complete`

Mock-only endpoint that records `payouts_completed_at` for settled or refunded markets.

This timestamp controls the post-payout link grace period.

#### `POST /admin/games`

Simulated-provider-only helper that creates a game a configurable number of minutes in the future.
It supports the planned live-check workflow without modifying the schedule file.

## 11. Templates and user messaging

### 11.1 Updated file

[`backend/app/templates.py`](backend/app/templates.py)

Added fixed responses for:

- Games that are no longer available for betting.
- Market-opening failures.

All market link, lock, refund, and failure notifications are generated by deterministic service
logic rather than by the LLM.

The link text includes:

- The matchup.
- Funding close time.
- Forwarding instructions.
- Deposit range.
- Fee percentage.
- The signed link.

## 12. Documentation and environment updates

### 12.1 `.env.example`

[`/.env.example`](.env.example)

Added placeholders for:

- Stake limits.
- Participant cap.
- Fee basis points.
- Chain mode.
- Treasury key.
- Link signing secret.
- Deposit URL and origin.
- Link grace period.
- Funding close timing.
- Settlement timing.

### 12.2 Backend README

[`backend/README.md`](backend/README.md)

Documented:

- Pool market architecture.
- Integer money model.
- State machine.
- Deposit rules.
- Public API endpoints.
- Error responses.
- Link lifetime.
- Admin endpoints.
- Future worker responsibilities.
- New environment variables.
- Migration `0005_markets`.

## 13. Tests added or updated

### 13.1 New tests

[`backend/tests/test_pool_math.py`](backend/tests/test_pool_math.py)

Covers:

- Decimal-to-base conversion.
- Base-to-USD formatting.
- Basis-point formatting.
- Float rejection.
- Fee flooring.
- Distributable calculation.
- Payout calculation.
- Multiplier formatting.
- Empty-loser preview behavior.

[`backend/tests/test_links.py`](backend/tests/test_links.py)

Covers:

- Signed token creation.
- Token parsing.
- Invalid signature rejection.
- Deposit link construction.
- Origin extraction.
- Pre-settlement expiry.
- Post-payout grace expiry.
- Seven-day fallback expiry.

### 13.2 Updated tests

[`backend/tests/test_config_logging.py`](backend/tests/test_config_logging.py)

The configuration fixtures now include the newly required signing secret and deposit URL.

## 14. Verification performed

### 14.1 Passed checks

```text
npm run typecheck
```

Result: passed.

```text
npm test
```

Result:

```text
40 tests passed
0 failed
```

Python syntax compilation of backend source and tests passed.

Python smoke checks passed for:

- Configuration loading.
- Market defaults.
- Signing and parsing links.
- Money conversion.
- Fee calculation.
- Payout calculation.
- Preview calculation.
- Short-signing-secret rejection.

`git diff --check` passed.

The approved `itsdangerous` package installation completed successfully.

### 14.2 Blocked check

The full command:

```text
cd backend && .venv/bin/pytest -q
```

could not collect tests because the existing environment contains an x86_64 compiled
`pydantic_core` extension while the current host requires arm64:

```text
mach-o file, but is an incompatible architecture
have 'x86_64', need 'arm64'
```

This failure occurs while importing FastAPI/Pydantic, before any application or market test runs.
The virtual environment would need to be rebuilt for the current architecture to run the complete
Python suite.

### 14.3 Migration application

The migration file was created but not applied to a remote Supabase/PostgreSQL project because no
database migration tool or usable database connection was available in the execution environment.

The migration is ready to apply in order after migrations `0001` through `0004`.

## 15. Known limitations and follow-up work

The following items remain intentionally outside chunk 4:

- Background worker execution at `funding_closes_at`.
- Automatic settlement after the game result becomes official.
- Real-chain implementation.
- Production wallet transaction verification.
- Claim triggering from the public API.
- Payout completion automation.
- Retry worker for pending proposer notices.
- Live phone integration test.

The mock chain includes settlement and claim primitives for future use, but no public settlement
or claim route was added.

The real chain mode intentionally fails clearly rather than pretending to perform blockchain
operations.

## 16. Changed-file inventory

Modified:

- [`.env.example`](.env.example)
- [`backend/README.md`](backend/README.md)
- [`backend/app/config.py`](backend/app/config.py)
- [`backend/app/drafts.py`](backend/app/drafts.py)
- [`backend/app/main.py`](backend/app/main.py)
- [`backend/app/repository.py`](backend/app/repository.py)
- [`backend/app/templates.py`](backend/app/templates.py)
- [`backend/pyproject.toml`](backend/pyproject.toml)
- [`backend/tests/test_config_logging.py`](backend/tests/test_config_logging.py)

Added:

- [`backend/app/market_api.py`](backend/app/market_api.py)
- [`backend/markets/__init__.py`](backend/markets/__init__.py)
- [`backend/markets/chain.py`](backend/markets/chain.py)
- [`backend/markets/links.py`](backend/markets/links.py)
- [`backend/markets/money.py`](backend/markets/money.py)
- [`backend/markets/mock_chain.py`](backend/markets/mock_chain.py)
- [`backend/markets/pool_math.py`](backend/markets/pool_math.py)
- [`backend/markets/service.py`](backend/markets/service.py)
- [`backend/migrations/0005_markets.sql`](backend/migrations/0005_markets.sql)
- [`backend/tests/test_links.py`](backend/tests/test_links.py)
- [`backend/tests/test_pool_math.py`](backend/tests/test_pool_math.py)

