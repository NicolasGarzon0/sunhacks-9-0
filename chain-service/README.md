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
| `MINT_AUTHORITY_SECRET_KEY` | optional, base64. The test USDC minter. Without it `POST /airdrop` answers `AIRDROP_DISABLED` |
| `AIRDROP_MAX_PER_CALL_BASE` | hard cap per airdrop call, default 100000000 (100 USDC). The per wallet and daily policy lives in the backend |
| `DEPOSIT_CUTOFF_SECONDS` | default 30: a deposit must be built at least this long before `funding_closes_at` (chain clock) |
| `DEPOSIT_BUILD_TTL_SECONDS` | default 60: `expires_at` of a built transaction (the blockhash itself lasts about as long) |
| `DEPOSIT_RATE_PER_WALLET_PER_MIN` / `DEPOSIT_RATE_PER_BET_PER_MIN` | defaults 10 and 60. In memory, per process |

Startup fails naming the variable (never its value). `contracts/.keys/env.generated` holds ready to copy values.

## API
All JSON. Success is `{ok:true, ...}`, failure is `{ok:false, code, detail}` with an HTTP status (400 bad input, 401 secret, 404 NOT_FOUND, 409 state errors, 502 RPC_ERROR).

| Endpoint | Does |
|---|---|
| `GET /health` (no secret) | `ok, cluster, program_id, payer, oracle, payer_sol, oracle_sol` |
| `POST /bets` | `create_bet`: `bet_id` (UUID), `funding_closes_at`, `refund_after` (unix seconds), `fee_bps`, `min_stake_base`, `max_stake_base`, `max_participants`. Payer signs and pays, authority = oracle, treasury and mint from env |
| `GET /bets/{id}` | `state, side_totals, participant_count, positions_open, fee_bps, fee_paid_base, distributable_base, winning_side, funding_closes_at, refund_after, min/max_stake_base, max_participants, vault_base, treasury_balance_base` |
| `POST /bets/{id}/positions` | body `{"depositors": ["PUB", ...]}` (at most 1000, deduplicated). Returns the open positions `[{wallet, side, amount_base}]` of those wallets only: each Position PDA is derived from `["position", bet, depositor]` and read with `getMultipleAccounts` in batches of 100. **The service never calls `getProgramAccounts`** (public RPCs rate limit or disable it). A wallet with no open position (never deposited, claimed, closed) is absent. The backend passes its known participants (`bet_participants`). `GET` is no longer served |
| `POST /bets/{id}/lock`, `/settle {winning_side}`, `/refund_all`, `/close_bet` | oracle signed (the payer is fee payer). `settle` and `close_bet` create the treasury token account first if it is missing |
| `POST /bets/{id}/claim {wallet}` | payer is signer and fee payer, rent goes to `bet.payer`. Returns `payout_base`, `kind` (`winnings` or `refund`), `signature` |
| `POST /bets/{id}/close_loser_position {wallet}` | closes a losing position (rent back to the payer, no tokens move) |

| `GET /wallet/{pubkey}` | `has_token_account, usdc_base, sol_lamports` |
| `POST /airdrop {wallet, amount_base}` | creates the wallet's USDC token account if missing (payer pays), then mints. At most `AIRDROP_MAX_PER_CALL_BASE` per call, never closes an account. Who may ask and how often is the backend's policy |
| `POST /deposit/build {bet_id, wallet, side, amount_base}` | checks the live deposit rules, then returns ONE unsigned legacy transaction (`transaction_b64`, `message_b64`, `blockhash`, `last_valid_block_height`, `expires_at`). Signs nothing |
| `POST /deposit/submit {bet_id, wallet, side, amount_base, signed_tx_b64 \| message_b64 + user_signature_b64}` | verifies, then co-signs as the sponsor and sends. Returns `signature`, `explorer`, `already` |

`chain-service/scripts/dev_deposit.ts` still makes deposits directly for tests, and `scripts/deposit_client.ts` plays the deposit page.

## Sponsored deposits: what is verified before the payer signs
A signature authorizes bytes, so the payer key signs **only the exact bytes that were verified**, and the message is never modified after any signature exists. `POST /deposit/submit` requires, in order (each failure is `TX_REJECTED` with a safe reason, and nothing is signed or sent):
1. a **legacy** transaction (versioned messages and address lookup tables are rejected);
2. exactly **two required signers**: the payer (fee payer) and the wallet, nobody else; the client may not supply the payer's signature;
3. **exactly one instruction**, for `PROGRAM_ID`, whose discriminator is `deposit` (so no compute budget, token program, associated token program, close or create instruction can ride along);
4. the submitted message bytes are **byte for byte equal to the message the service rebuilds** from `bet_id`, `wallet`, `side`, `amount_base` and the submitted blockhash (this catches any tampered amount, side or account);
5. the wallet's **ed25519 signature verifies** over those bytes;
6. (an identical submit that already succeeded returns the same signature, see below);
7. the **blockhash is still valid** (`TX_EXPIRED` otherwise);
8. the **live rules** again: bet state Open, now at least `DEPOSIT_CUTOFF_SECONDS` before `funding_closes_at` (chain clock), side, min, max, the wallet's existing side, the participant cap for new wallets, the token account exists and holds the USDC (`MARKET_NOT_OPEN`, `FUNDING_CLOSED`, `BELOW_MIN`, `ABOVE_MAX`, `MARKET_FULL`, `ALREADY_OTHER_SIDE`, `NO_TOKEN_ACCOUNT`, `INSUFFICIENT_USDC`, `UNTRUSTED_BET`);
9. a **simulation** with the sponsor's slot empty; program errors are mapped to the same friendly codes, anything else is `TX_REJECTED`.

Only then the payer signs the verified message with ed25519 and the service sends and confirms it (`SUBMIT_FAILED` when sending fails, `TX_EXPIRED` when the blockhash expired meanwhile). Logs carry bet id, the first 6 characters of the wallet, instruction and signature only.

**Idempotency and rate limits** are in memory and therefore per process: the same signed transaction (same message and wallet signature) submitted twice sends once and returns the same signature (`already:true`), concurrent duplicates included; after a restart a resubmit goes through verification again and fails with a normal code if the deposit already happened. Build and submit are limited per wallet and per bet per minute (`RATE_LIMITED`, HTTP 429).

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

## Reference client
`npm run deposit-client -- <airdrop|wallet|view|deposit> --link <market link> --wallet-file <name>` plays the deposit page with a local keypair (zero SOL, stored under the gitignored `.dev-wallets/`): airdrop, wallet check, `deposit_tx`, local signing, `deposit_submit` in either shape (`--shape message|signed`), and `--tamper amount|side` to prove the service refuses altered bytes. The real wallet login (Privy) is chunk 5.
