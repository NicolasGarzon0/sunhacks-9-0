# Deposit page API

For the frontend developer. Everything the deposit page calls lives under the backend's market API, mounted at
`/markets/{token}` (CORS allows `DEPOSIT_PAGE_ORIGIN`). `{token}` is the last path segment of the link in the iMessage text
(`DEPOSIT_PAGE_URL/b/{token}`). The wallet is a Solana keypair held by the user; **the user needs no SOL**: the sponsor pays
fees and rent. Amounts are shown in dollars (strings with 2 to 6 decimals) and base units (integers, 6 decimals).

All responses are JSON. Success is `{ "ok": true, ... }`, failure is `{ "ok": false, "code": "...", "detail": "..." }`.

The deposit, wallet and airdrop endpoints exist only when the backend runs against the real chain (`CHAIN_MODE=real`);
otherwise they answer `404 NOT_FOUND`.

## The sequence

```
1. GET  /markets/{token}                      market view (poll every ~3 s the whole time)
2. GET  /markets/{token}/wallet?wallet=PUB    has the wallet a USDC token account? how much USDC?
3. POST /markets/{token}/airdrop              (test network only) get test USDC if the wallet has none
4. POST /markets/{token}/deposit_tx           ask for the transaction + a human readable summary
5.   show the summary, let the user confirm, sign `message_b64` with the wallet
6. POST /markets/{token}/deposit_submit       send the signature; the server verifies, co-signs and sends
7. GET  /markets/{token}                      the new pool, or poll until it shows the deposit
```

A top up on the same side is just another deposit. A wallet can hold only one side.

## GET /markets/{token}

The market. `status` is `open | locked | settled | refunded | failed`. Important fields:

```json
{ "ok": true, "bet_id": "…", "status": "open", "league": "nfl",
  "teams": { "side0": { "name": "Seattle Seahawks", "role": "away" }, "side1": { "name": "Arizona Cardinals", "role": "home" } },
  "kickoff_utc": "2026-11-15T21:05:00Z", "funding_closes_at": "2026-11-15T20:05:00Z",
  "pools": { "side0_base": 12000000, "side1_base": 15250000, "side0_usd": "12.00", "side1_usd": "15.25" },
  "total_pool_usd": "27.25", "participant_count": 4,
  "limits": { "min_stake_usd": "1.00", "max_stake_usd": "20.00", "max_participants": 20 },
  "fee_bps": 500, "fee_line": "A 5% fee on the losing side covers operating costs.",
  "multipliers": { "side0": "2.27", "side1": "1.95" } }
```

`side 0` is the away team, `side 1` the home team. `multipliers` are the payout per 1 staked if that side wins, net of the fee, from the
current pool (null once refunded). Also `GET /markets/{token}/participants` (`[{wallet, side, stake_base, stake_usd, display_name}]`)
and `GET /markets/{token}/position?wallet=PUB`.

## GET /markets/{token}/wallet?wallet=PUB

```json
{ "ok": true, "wallet": "PUB", "has_token_account": true, "usdc_base": 25000000, "usdc_usd": "25.00" }
```

`has_token_account: false` means the wallet has never held the test USDC: airdrop first (the airdrop creates the account).

## POST /markets/{token}/airdrop (test network only)

Request `{ "wallet": "PUB" }`. Gives the wallet test USDC up to the lifetime allowance (default 25.00 USD per wallet).

```json
{ "ok": true, "wallet": "PUB", "has_token_account": true, "usdc_base": 25000000, "usdc_usd": "25.00",
  "minted_base": 25000000, "minted_usd": "25.00", "signature": "5Kc…", "explorer": "https://explorer.solana.com/tx/5Kc…?cluster=devnet" }
```

A repeat request when the wallet already has its whole allowance returns `ok` with `minted_base: 0`, `signature: null` and the current balance.
Errors: `AIRDROP_DISABLED` (409, feature off), `AIRDROP_LIMIT` (409, the daily limit is used up), `RATE_LIMITED` (429), `INVALID_WALLET` (400).

## POST /markets/{token}/deposit_tx

Request `{ "wallet": "PUB", "side": 1, "amount_usd": "5" }`. `amount_usd` is a string or a JSON number, at most 6 decimals.

```json
{ "ok": true, "bet_id": "…",
  "transaction_b64": "AgAAAA…",      // the unsigned transaction: 2 empty signature slots + the message
  "message_b64": "AgACBQ…",          // the bytes to sign
  "blockhash": "EETub…", "last_valid_block_height": 312345678, "expires_at": "2026-11-15T20:01:00Z",
  "summary": {
    "side": 1, "team": "Arizona Cardinals", "amount_usd": "5.00", "amount_base": 5000000,
    "your_total_stake_usd": "5.00", "fee_bps": 500, "fee_line": "A 5% fee on the losing side covers operating costs.",
    "payout_usd": "9.75", "profit_usd": "4.75", "multiplier": "1.95",
    "text": "You are putting $5.00 on Arizona Cardinals. If Arizona Cardinals wins you would receive about $9.75 in total (1.95x your stake), based on the pool right now. A 5% fee on the losing side covers operating costs." } }
```

Show `summary.text` (or build your own from the fields) before asking the user to sign. The payout is a preview that moves as others deposit.
**The transaction expires in about 60 seconds** (`expires_at`).

## POST /markets/{token}/deposit_submit

Two accepted shapes, plus the same `wallet`, `side`, `amount_usd` as in `deposit_tx`, and an optional `display_name` (max 24 characters, no links).

Message plus signature (what most wallets give you after `signMessage`-style signing of the bytes):

```json
{ "wallet": "PUB", "side": 1, "amount_usd": "5", "message_b64": "AgACBQ…", "user_signature_b64": "<64 byte ed25519 signature, base64>", "display_name": "Dana" }
```

Signed transaction (the `transaction_b64` from `deposit_tx` with the **wallet's slot (the second one)** filled in; leave the sponsor's slot empty):

```json
{ "wallet": "PUB", "side": 1, "amount_usd": "5", "signed_tx_b64": "AgAAAA…" }
```

Never change a single byte of the message after asking for it: the server rebuilds the exact transaction from `side` and `amount_usd` and
refuses anything that differs. Success:

```json
{ "ok": true, "signature": "3x9…", "explorer": "https://explorer.solana.com/tx/3x9…?cluster=devnet", "already": false,
  "market": { "...": "the updated market view, same shape as GET /markets/{token}" } }
```

Submitting the same signed deposit twice is safe: the second answer has `already: true` and the same signature. When the deposit fills
the last seat the market locks right away and `market.status` is already `locked`.

## Errors and suggested messages

| code | HTTP | where | meaning | suggested message |
|---|---|---|---|---|
| `BAD_TOKEN` | 404 | all | the link is not valid | "This link is not valid." |
| `LINK_EXPIRED` | 410 | all | the link expired (payouts finished + grace) | "This bet is over and the link has expired." |
| `BAD_REQUEST` | 400 | all | malformed body or amount (more than 6 decimals, negative, not a number) | "Enter a valid dollar amount." |
| `INVALID_WALLET` | 400 | all | not a valid public key | "That wallet address is not valid." |
| `INVALID_SIDE` | 400 | tx, submit | side is not 0 or 1 | "Pick a team." |
| `BAD_DISPLAY_NAME` | 400 | submit | name too long, empty or has a link | "Use a short plain name (up to 24 characters, no links)." |
| `INVALID_PARAMS` | 400 | tx, submit | the server refused the numbers | "Check the amount and try again." |
| `TX_REJECTED` | 400 | submit | the signed bytes are not the deposit that was asked for, or the wallet signature is wrong | "We could not verify that transaction. Please start the deposit again." |
| `MARKET_NOT_OPEN` | 409 | tx, submit | locked, settled, refunded or failed (`status` is in the body) | "Betting is closed for this game." |
| `FUNDING_CLOSED` | 409 | tx, submit | within 30 seconds of the deadline or past it | "Betting just closed." |
| `BELOW_MIN` | 409 | tx, submit | under the minimum stake (`limits.min_stake_usd`) | "The minimum is $X." |
| `ABOVE_MAX` | 409 | tx, submit | a wallet's total on a market is capped (`limits.max_stake_usd`) | "The most one wallet can put in is $X." |
| `MARKET_FULL` | 409 | tx, submit | all seats are taken (top ups by existing participants still work) | "This bet is full." |
| `ALREADY_OTHER_SIDE` | 409 | tx, submit | the wallet already holds the other team | "You already picked the other team in this bet." |
| `NO_TOKEN_ACCOUNT` | 409 | tx, submit | the wallet never held the test USDC | "Get some test USDC first." (call the airdrop) |
| `INSUFFICIENT_USDC` | 409 | tx, submit | balance below the amount | "You do not have enough USDC." |
| `TX_EXPIRED` | 409 | submit | the transaction was built too long ago | "That took too long. Please confirm again." (rebuild, below) |
| `AIRDROP_DISABLED` / `AIRDROP_LIMIT` | 409 | airdrop | see above | "Test USDC is not available right now." |
| `RATE_LIMITED` | 429 | all | too many requests (per address and per wallet, per minute) | "Too many tries. Wait a moment." |
| `RPC_ERROR`, `SUBMIT_FAILED`, `UNTRUSTED_BET` | 502 | tx, submit | the chain or its service failed | "Something went wrong on our side. Please try again." |

## Blockhash expiry: rebuild and sign again

A deposit transaction is valid for about a minute. If the user takes longer, or the network is slow, `deposit_submit` answers
`TX_EXPIRED` (409). Nothing was sent and nothing was charged. Call `deposit_tx` again (same wallet, side, amount), have the user
sign the new `message_b64`, and submit again. Do not resubmit the old bytes. Retrying a **submit that timed out without an answer** with
the very same bytes is safe (same signature back) as long as it is within the minute.

## Showing the views

Keep polling `GET /markets/{token}` (every ~3 s while the page is open) and switch on `status`:

- **open**: the deposit form. Show both pools, the multipliers, `funding_closes_at` as a countdown and `participant_count` of `limits.max_participants`.
- **locked**: no more deposits. Betting closed, waiting for the game. Show the final pools and each wallet's stake from `/participants` (and the wallet's own stake from `/position?wallet=`).
- **settled**: `winning_side`, `fee_paid_usd`, `distributable_usd`, `final_multiplier`. For the wallet, `/position?wallet=` gives `side`, `stake_usd`, `payout_usd` and `paid` (true once the payout was sent). A winner's payout arrives by itself, there is nothing to claim. Losers show `payout_usd: "0.00"`.
- **refunded**: everyone gets their stake back with no fee, `refund_reason` says why (`one_sided`, `too_few_participants`, `cancelled`, `tie`, `postponed`, `no_result`). `/position` shows `payout_usd` equal to the stake and `paid` once it was sent.
- **failed**: the market could not be opened. Show a generic "this bet did not start" message.

These views keep working after the on chain positions are closed: they are served from a database snapshot taken when betting closed.
The link stops working (`410 LINK_EXPIRED`) a while after the payouts finish.
