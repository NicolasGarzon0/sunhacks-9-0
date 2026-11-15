# chain-service

A small HTTP service between the Python backend and the `handshake_pool` Solana program (devnet). It is the only
component that holds the **payer** and **oracle** keys. The backend (`CHAIN_MODE=real`) talks to it through
`RealChainClient` (`backend/markets/real_chain.py`) and never sees a key.

```
iMessage gateway ─▶ backend (Python, FastAPI, worker) ──HTTP, X-Chain-Secret──▶ chain-service ──RPC──▶ Solana devnet
                                                                                    │ holds PAYER + ORACLE keys
deposit page (3B) ─── user signed deposits (sponsor co-signs) ──────────────────────┘
```

**Deployment:** it must run next to the backend (same private network), reachable only by the backend. It holds
the payer and oracle secret keys, so treat its host like a wallet: env vars only, no key files, no public port, no logs of
secrets (it logs bet id, instruction and signature only).

## Run
```sh
cd chain-service
npm install
npm run sync-idl           # copies contracts/target/idl/handshake_pool.json to idl/ (rerun after every contract build)
npm start                  # PORT, default 8789; aborts at startup unless the RPC is devnet
npm test                   # unit tests, mocked RPC (31)
npm run typecheck
RUN_DEVNET_TESTS=1 npm run test:devnet     # also lock, settle, claim, close_loser_position, close_bet on devnet
```

## Environment
| Variable | Notes |
|---|---|
| `SOLANA_RPC_URL` | default `https://api.devnet.solana.com`. The service checks the devnet genesis hash at startup and refuses anything else. |
| `PROGRAM_ID` | the deployed `handshake_pool` (`contracts/devnet.json`) |
| `TEST_USDC_MINT` | the 6 decimal mint every bet must use |
| `PAYER_SECRET_KEY` | base64 of the 64 byte secret key. Sponsor: fee payer, `bet.payer`, signs claims and loser closes |
| `ORACLE_SECRET_KEY` | base64, `bet.authority`: signs lock, settle, refund_all, close_bet |
| `TREASURY_PUBKEY` | owner of the fee token account (public) |
| `CHAIN_SERVICE_SECRET` | at least 16 characters; every request except `GET /health` needs it in `X-Chain-Secret` |
| `PORT` | default 8789 |
| `LOG_LEVEL` | `debug` \| `info` \| `warn` \| `error` |

Startup fails naming the variable (never its value). `contracts/.keys/env.generated` holds ready to copy values.

## API
All JSON. Success is `{ok:true, ...}`, failure is `{ok:false, code, detail}` with an HTTP status (400 bad input, 401 secret, 404 NOT_FOUND, 409 state errors, 502 RPC_ERROR).

| Endpoint | Does |
|---|---|
| `GET /health` (no secret) | `ok, cluster, program_id, payer, oracle, payer_sol, oracle_sol` |
| `POST /bets` | `create_bet`: `bet_id` (UUID), `funding_closes_at`, `refund_after` (unix seconds), `fee_bps`, `min_stake_base`, `max_stake_base`, `max_participants`. Payer signs and pays, authority = oracle, treasury and mint from env |
| `GET /bets/{id}` | `state, side_totals, participant_count, positions_open, fee_bps, fee_paid_base, distributable_base, winning_side, funding_closes_at, refund_after, min/max_stake_base, max_participants, vault_base, treasury_balance_base` |
| `GET /bets/{id}/positions` | open positions `[{wallet, side, amount_base}]` via `getProgramAccounts` (`dataSize 82` + `memcmp offset 8 = bet`) |
| `POST /bets/{id}/lock`, `/settle {winning_side}`, `/refund_all`, `/close_bet` | oracle signed (the payer is fee payer). `settle` and `close_bet` create the treasury token account first if it is missing |
| `POST /bets/{id}/claim {wallet}` | payer is signer and fee payer, rent goes to `bet.payer`. Returns `payout_base`, `kind` (`winnings` or `refund`), `signature` |
| `POST /bets/{id}/close_loser_position {wallet}` | closes a losing position (rent back to the payer, no tokens move) |

Deposits are not an endpoint: the user signs on the deposit page and the payer co-signs (chunk 3B). `chain-service/scripts/dev_deposit.ts` does the same for tests.

## Error codes
`BELOW_MIN, ABOVE_MAX, MARKET_FULL, ALREADY_OTHER_SIDE, MARKET_NOT_OPEN, WRONG_STATE, NOT_A_WINNER, NOT_FOUND, FUNDING_CLOSED, PAST_REFUND_DEADLINE, REFUND_TOO_EARLY, BOTH_SIDES_REQUIRED, UNTRUSTED_BET, INVALID_PARAMS, RPC_ERROR`, plus `UNAUTHORIZED` (bad secret) and `BAD_REQUEST` (malformed request; the backend maps it to INVALID_PARAMS).
Program errors are parsed from simulation and transaction logs (`Error Code: X`, with the `custom program error: 0x17xx` number as a fallback): `Unauthorized`, `WrongMint`, `WrongTreasury`, `WrongRentReceiver` become `UNTRUSTED_BET`; `PositionsStillOpen` becomes `WRONG_STATE`; `AccountNotInitialized` becomes `NOT_FOUND`; Anchor constraint errors become `INVALID_PARAMS`. Network, 429 and timeouts become `RPC_ERROR` after retries (1, 2, 4, 8 s).

## Trust check
Every read and write loads the Bet account and requires: owner = the program, `authority` = oracle public key, `payer` = payer public key, `treasury` = `TREASURY_PUBKEY`, `mint` = `TEST_USDC_MINT`. Anything else answers `UNTRUSTED_BET` and sends nothing. Anyone can create a Bet account on the public program, so without this check a foreign bet could make the service sign for the wrong accounts.

## Idempotency
A change that already happened returns `ok` with `already:true` instead of an error, so the backend can repeat any call after a crash:
- `POST /bets`: the Bet PDA exists (and is trusted) → returned as is.
- `lock`: already locked or settled. `settle`: already settled with the same side (the other side is `WRONG_STATE`). `refund_all`: already refunded.
- `claim`: no position account means it was already claimed (`already:true`; the backend client raises `ALREADY_CLAIMED`). `close_loser_position`: no position account. `close_bet`: no Bet account.
- A transaction that loses a race (the position vanished while it was in flight) is re-read once and answered `already:true`.

## Keys
Secrets exist only in the process environment. They are never logged, echoed in errors, or returned. `.env.live` (used for live runs) is gitignored. Rotate keys as described in `contracts/DEVNET.md`; a bet remembers the payer and oracle it was created with, so an old key must stay available until its bets are closed, and the trust check will mark bets created with an old key `UNTRUSTED_BET`.

## Snapshots and what changed in the backend
On chain a position account is deleted when it is claimed or closed, and the Bet account is deleted by `close_bet`.
The backend therefore copies the positions into `position_snapshots` when funding closes and serves every locked, settled and refunded market view from there (see `backend/README.md`).

## What 3B adds
The deposit page (user signed deposit transactions with the sponsor co-signature) and the test USDC airdrop endpoint, both built on the code here (`src/program.ts`, `scripts/dev_deposit.ts`, `contracts/scripts/lib/airdrop.ts`).
