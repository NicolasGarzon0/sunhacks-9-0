# Devnet setup for handshake_pool

Everything here is devnet only. Every script aborts unless the RPC's genesis hash is devnet's. Default RPC is
`https://api.devnet.solana.com`; override with `SOLANA_RPC_URL`. On Apple Silicon under Rosetta prefix commands with `arch -arm64`.

Public addresses are in `devnet.json` (committed): program, test USDC mint, and the public key of every role.
Secrets live only in `contracts/.keys/` (directory 700, files 600, gitignored), never printed by any script.

## Keys

| File in `.keys/` | Role | Notes |
|---|---|---|
| `payer.json` | sponsor | pays fees and rent, stored as `bet.payer`, gets every rent lamport back, co-signs every deposit as `rent_payer` |
| `oracle.json` | `bet.authority` | the only key that may lock, settle, refund_all, close_bet |
| `treasury.json` | fee owner | only its public key matters to the backend; the fee token account is its associated token account |
| `mint-authority.json` | test USDC minter | mints test USDC (the mint has no freeze authority) |
| `upgrade-authority.json` | dev deployer | pays deploys and is the program's upgrade authority |
| `program-keypair.json` | program address | signs only the first deploy |
| `env.generated` | generated | base64 secrets and public values for the backend env (never print or commit) |

`npm run keys` creates missing keys and keeps existing ones. `npm run keys -- --force` moves existing keys to
`.keys/backup-<timestamp>/` first and creates new ones; a new program key means a new program id (redeploy).

## Fund
`npm run fund` estimates the SOL needed and tries the command line airdrop with backoff. If it is rate limited it prints
each public key and amount: use https://faucet.solana.com (network Devnet), then `npm run fund -- --check`.
Needs: upgrade authority about 4 SOL (deploy locks about 1.61 SOL of rent plus a 1.61 SOL buffer that is refunded),
payer 1 to 2 SOL (50 markets lock at most about 1.2 SOL of rent, all returned; each new user token account costs about 0.0015 SOL, never returned), oracle 0.1 SOL.
Other fallbacks: the faucets of Helius, QuickNode or Triton with a free devnet account, or `solana airdrop` from another network location.

## Deploy, upgrade
```sh
npm run deploy          # keys sync, anchor build --arch v0, deploy, solana program show check, records devnet.json
```
First run deploys; later runs upgrade in place (same command). Always `--arch v0` (the default SBPF v3 is not accepted by the local validator).
Equivalent manual upgrade:
```sh
anchor build --arch v0
solana program deploy target/deploy/handshake_pool.so --program-id .keys/program-keypair.json \
  --upgrade-authority .keys/upgrade-authority.json --keypair .keys/upgrade-authority.json --url https://api.devnet.solana.com
```
If the new binary is bigger than the account (317056 bytes now): `solana program extend <PROGRAM_ID> <extra bytes> --keypair .keys/upgrade-authority.json --url ...` first.
Check: `solana program show <PROGRAM_ID> --url https://api.devnet.solana.com --keypair .keys/upgrade-authority.json`.
After a change that alters instructions or accounts, rebuild the IDL (`target/idl/handshake_pool.json`) for the backend.

## Test USDC
`npm run mint:create` creates the 6 decimal classic mint (authority = mint_authority, no freeze authority) once, mints 10,000 USDC to the payer, records the mint in `devnet.json`. Safe to rerun.

## Airdrop USDC
```sh
npm run airdrop -- <wallet> [amount]     # default 25, hard cap 100 per call
```
Creates the wallet's associated token account (paid by the payer) if missing, mints the amount, prints the signature and an explorer link. It never closes accounts.
The reusable function is `airdropUsdc()` in `scripts/lib/airdrop.ts` (chunk 3 calls the same logic).

## Smoke test
`npm run smoke` (about 5 minutes, spends about 0.013 SOL on fees and user token accounts): market A (settled, 4 zero-SOL users, one top up, claims by a stranger, loser closes, rent and fee checks), a deposit without the payer signature failing on chain, market B (refund_all), market C (emergency_refund with short deadlines), then close_bet on all three. Exits non-zero on any failed check.
Runs only need the payer to hold at least 0.2 SOL.

## Rotate a key
- payer, oracle, treasury: affect only new bets. Existing bets keep the stored keys, so keep the old key until its bets are closed. Run `npm run keys -- --force`, then fund the new key (and re-run `mint:create` only if mint-authority changed).
- mint authority: `spl-token authorize <MINT> mint <NEW_PUBKEY> --owner .keys/mint-authority.json --url devnet`, then replace the key file.
- upgrade authority: `solana program set-upgrade-authority <PROGRAM_ID> --new-upgrade-authority <NEW_PUBKEY> --keypair .keys/upgrade-authority.json --url devnet`, then replace the key file.
- program key: a new program id, which means a fresh deploy (old bets stay on the old program).
`env.generated` is rewritten by deploy and `mint:create`; run `npm run keys` to refresh it after a rotation.

## Environment variables for chunk 3 (backend)
Secrets are base64 of the 64 byte secret key, set as env values on the host, never as files. `.keys/env.generated` has them all.
```
SOLANA_RPC_URL=https://api.devnet.solana.com
PROGRAM_ID=<devnet.json programId>
TEST_USDC_MINT=<devnet.json testUsdcMint>
PAYER_SECRET_KEY=<base64>
ORACLE_SECRET_KEY=<base64>
TREASURY_PUBKEY=<devnet.json keys.treasury>
MINT_AUTHORITY_SECRET_KEY=<base64>
```
`.env.example` has commented placeholders. The public devnet RPC rate limits; a free provider URL in `SOLANA_RPC_URL` is fine.

## Chunk 3 notes
- `bet_id` for the program is the 16 raw bytes of the backend UUID. `refund_after` = `settle_after` + 12 h (configurable).
- Deposit transaction: payer is fee payer and `rent_payer`, the user is `depositor`. Build once, freeze the message, user signs first, payer signs second (see `deposit()` in `scripts/smoke.ts`).
- Create the treasury token account before the first settle (the smoke test shows the idempotent ATA instruction).
