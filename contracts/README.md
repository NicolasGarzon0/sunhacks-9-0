# handshake_pool: Anchor escrow for shared pool bets

Program `handshake_pool` holds the money for one pool bet in a vault, lets people deposit on side 0 (away) or
side 1 (home), and pays winners after the oracle settles. It is the on chain version of the backend mock
(`backend/markets/mock_chain.py` and `pool_math.py`). Local only so far: nothing is deployed anywhere.

* Program id (throwaway, from `target/deploy/handshake_pool-keypair.json`, which is gitignored): `HmgmQJP136sLcH1gQDywG7MeUX9xZN6wZMDdzsgf9X1c`
* Token: a 6 decimal classic SPL Token mint (USDC style). The mint is stored on each bet. Amounts are u64 base units.
* Side 0 = away team, side 1 = home team.

## Build and test

All commands run from `contracts/`. On an Apple Silicon Mac whose terminal runs under Rosetta, prefix them with
`arch -arm64` (the native Solana and LiteSVM binaries are arm64).

```sh
npm install
anchor build --arch v0          # IMPORTANT: see "SBPF version" below
cargo test -p handshake_pool    # 8 Rust unit tests for the money math
npx ts-mocha -p ./tsconfig.json -t 1000000 "tests/**/*.test.ts"   # 89 LiteSVM tests
anchor test --validator legacy --skip-build                         # 1 lifecycle test on solana-test-validator
```

`npm test` runs all three. `--validator legacy` is needed because Anchor 1.x defaults to Surfpool, which is not installed.

### SBPF version (read this before deploying)
Anchor 1.2.2 builds SBPF **v3** by default. `solana-test-validator` 3.1.14 rejects such a program ("Program is not deployed").
The build here therefore uses `--arch v0`, which the local validator accepts. Chunk 2 must keep `--arch v0` unless it first
confirms devnet has the SBPF v3 feature active.

### Tests
* `programs/handshake_pool/src/math.rs`: Rust unit tests for fee, distributable and payout (the vectors of `backend/tests/test_pool_math.py`, the $13 vs $13 vector, a 3000 case randomized "winner never gets less than stake" check).
* `tests/*.test.ts`: LiteSVM suite (89 tests): every case in the task list, the vectors end to end, vault accounting on a 12 wallet market, and a 20 participant lifecycle (settled on side 0 and on side 1) that also asserts the sponsor gets every lamport of rent back.
* `tests-validator/smoke.test.ts`: full lifecycle (create, 2 deposits, lock, settle, claim, loser close, close_bet) on a real local `solana-test-validator`.

LiteSVM was chosen for the detailed suite because it can set the clock (funding window cases) and runs 20 participant markets in under a second.

## PDAs

| Account | Seeds | Notes |
|---|---|---|
| Bet | `["bet", bet_id]` | `bet_id` is 16 bytes (the backend UUID's raw bytes) |
| Vault | `["vault", bet]` | token account, mint = `bet.mint`, token authority = the Bet PDA |
| Position | `["position", bet, depositor]` | one per wallet per bet |

## Instructions

Signer legend: **S** = must sign. "anyone" = any key, the app normally signs and pays the fee.

### create_bet(bet_id [u8;16], authority, treasury, fee_bps u16, min_stake u64, max_stake u64, max_participants u16, funding_closes_at i64, refund_after i64)
| Account | | |
|---|---|---|
| payer | **S**, mut | sponsor, pays rent for Bet and Vault, stored as `bet.payer` |
| bet | init, PDA | |
| mint | | must have 6 decimals |
| vault | init, PDA | |
| token_program | | must be the classic SPL Token program |
| system_program | | |

Rejects: `fee_bps` > 1000 `FeeTooHigh`; `min_stake` 0 or > `max_stake`, `max_participants` outside 2..=100, `funding_closes_at` not after now, `refund_after` not strictly after `funding_closes_at`, mint decimals != 6, default (all zero) `authority` or `treasury`: `InvalidParams`; Token-2022 program: `WrongMint`.

### deposit(side u8, amount u64): **TWO SIGNERS: the user AND the sponsor**
| Account | | |
|---|---|---|
| depositor | **S** | the user. Authorizes the token transfer. Holds no SOL, never pays anything |
| rent_payer | **S**, mut | the sponsor: must equal `bet.payer` (else `Unauthorized`), pays Position rent. Also the natural fee payer |
| bet | mut | |
| vault | mut | |
| mint | | = `bet.mint` |
| position | init_if_needed, mut | rent paid by `rent_payer` |
| depositor_token | mut | the depositor's token account for the mint |
| token_program, system_program | | |

The transaction must carry both signatures. The deposit page has the user sign and the backend (or a relayer) adds the sponsor signature, or the other way round.
Error priority: `MarketNotOpen`, `FundingClosed` (now >= `funding_closes_at`), `InvalidSide`, `BelowMin`, then for a new position `MarketFull`, for an existing one `AlreadyOtherSide`, then `AboveMax` (position + amount > `max_stake`). Account level errors come first: `WrongMint`, `Unauthorized` (token account not owned by the depositor, or wrong rent payer).
A top up on a full market is allowed. A new position increments `participant_count` and `positions_open`.

### lock()
Accounts: `authority` **S** (= `bet.authority`, else `Unauthorized`), `bet` mut. Open -> Locked. Fails with `PastRefundDeadline` at or after `refund_after`. Needs both `side_totals` > 0 and `participant_count` >= 2, else `BothSidesRequired`. The oracle may lock before or after `funding_closes_at`, but only before `refund_after`.

### settle(winning_side u8)
Accounts: `authority` **S**, `bet` mut, `vault` mut, `mint`, `treasury_token` mut (owner = `bet.treasury` and mint = `bet.mint`, else `WrongTreasury`), `token_program`.
Locked -> Settled. Fails with `PastRefundDeadline` at or after `refund_after` (so the oracle cannot race the emergency refund). `fee = floor(losing_pool * fee_bps / 10000)`, `distributable = total - fee`. A fee above zero moves from the vault to `treasury_token`, signed by the Bet PDA. Stores `fee_paid`, `distributable`, `winning_side`. `treasury_token` is required even when the fee is 0.

### refund_all()
Accounts: `authority` **S**, `bet` mut. Open or Locked -> Refunded. No transfers, no fee.

### emergency_refund()
Accounts: `signer` **S** (anyone, even a key with zero SOL, the app or a user can send it), `bet` mut.
Allowed only when state is Open or Locked (else `WrongState`) and the clock is at or after `refund_after` (else `RefundTooEarly`). Sets state Refunded, `winning_side` stays 255, no fee, no transfers, emits `EmergencyRefunded`. It is the exit when the oracle never locks or settles. After it, `claim` pays every participant their full stake exactly like a normal refund, and rent still goes to `bet.payer`.
Timeline: `created_at` < `funding_closes_at` (deposits stop) < `refund_after` (lock and settle stop, emergency_refund opens).

### claim()
Accounts: `signer` **S** (anyone), `bet` mut, `vault` mut, `mint`, `position` mut (closed to `rent_receiver`), `owner_token` mut (owner = `position.owner` and mint = `bet.mint`, else `WrongDestination`), `rent_receiver` mut (= `bet.payer`, else `WrongRentReceiver`), `token_program`.
Settled: only the winning side (`NotAWinner`), payout `floor(amount * distributable / side_totals[winning_side])` in u128. Refunded: payout = `amount`. Other states: `WrongState`. The Position is closed in the same instruction, so a second claim fails with Anchor's `AccountNotInitialized` (3012). `positions_open` goes down by one.

### close_loser_position()
Accounts: `signer` **S** (anyone), `bet` mut, `position` mut (closed), `rent_receiver` mut (= `bet.payer`). Settled only and `position.side != winning_side`, else `WrongState`. No token transfer.

### close_bet() (stretch goal, built)
Accounts: `authority` **S**, `bet` mut (closed to payer), `payer` mut (= `bet.payer`, else `WrongRentReceiver`), `vault` mut, `mint`, `treasury_token` mut (`WrongTreasury` check), `token_program`.
Settled or Refunded with `positions_open == 0` (`PositionsStillOpen`). Sends the vault's remaining tokens (rounding dust) to the treasury, closes the Vault by CPI `close_account`, closes the Bet. Rent of both goes to `bet.payer`.

## State machine

```
Open --lock--> Locked --settle--> Settled   (lock and settle only before refund_after) --claim / close_loser_position--> (positions close) --close_bet--> gone
 |               |
 +--refund_all (oracle) or emergency_refund (anyone, at or after refund_after)--+--> Refunded --claim--> (positions close) --close_bet--> gone
```
Only these transitions exist (emergency_refund is a second route into Refunded). `deposit` needs Open. Everything else returns `WrongState` (or `MarketNotOpen` for deposit).

## Errors (Anchor custom, start at 6000)
6000 BelowMin, 6001 AboveMax, 6002 MarketFull, 6003 AlreadyOtherSide, 6004 MarketNotOpen, 6005 WrongState, 6006 NotAWinner, 6007 InvalidSide, 6008 FundingClosed, 6009 BothSidesRequired, 6010 FeeTooHigh, 6011 InvalidParams, 6012 Unauthorized, 6013 MathOverflow, 6014 WrongMint, 6015 WrongTreasury, 6016 WrongDestination, 6017 WrongRentReceiver, 6018 PositionsStillOpen, 6019 PastRefundDeadline, 6020 RefundTooEarly.
Also seen in practice: Anchor's `AccountNotInitialized` (3012, claim or close on an already closed position), `ConstraintSeeds` (2006, wrong PDA), `ConstraintDuplicateMutableAccount` (2040, the vault passed as its own destination).

## Events (emitted with `emit!`, read from transaction logs)
| Event | Fields |
|---|---|
| Deposited | bet, wallet, side, amount, position_amount, new_position, side_totals, participant_count |
| BetLocked | bet, side_totals, participant_count |
| BetSettled | bet, winning_side, fee, distributable, side_totals |
| BetRefunded | bet, side_totals, participant_count |
| EmergencyRefunded | bet, caller, previous_state (0 Open, 1 Locked), side_totals, participant_count |
| Claimed | bet, wallet, side, stake, payout, refund, positions_open |
| LoserClosed | bet, wallet, side, stake, positions_open |
| BetClosed | bet, dust_to_treasury |

## Listing all positions of a bet
Position account layout (82 bytes, Borsh, little endian):

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | Anchor discriminator (from the IDL, `accounts[name=Position].discriminator`) |
| 8 | 32 | `bet` |
| 40 | 32 | `owner` |
| 72 | 1 | `side` |
| 73 | 8 | `amount` (u64) |
| 81 | 1 | `bump` |

```ts
connection.getProgramAccounts(programId, {
  filters: [{ dataSize: 82 }, { memcmp: { offset: 8, bytes: betPda.toBase58() } }],
});
```
Closed positions disappear, so this lists only positions not yet claimed or closed. For history use the events. `tests/layout.test.ts` checks the offsets. The Bet account is `8 + Bet::INIT_SPACE` bytes (field order in `programs/handshake_pool/src/state.rs`).

## IDL
Generated by `anchor build` at `target/idl/handshake_pool.json` (types at `target/types/handshake_pool.ts`). `target/` is gitignored, so chunk 2 should copy the IDL to wherever the backend and deposit page read it.

## Toolchain (pinned in `Anchor.toml` [toolchain] and `rust-toolchain.toml`)
| Tool | Version |
|---|---|
| Anchor CLI / anchor-lang / anchor-spl | 1.2.2 (stable 1.x, not 2.0 rc) |
| Agave / Solana CLI | 3.1.14 |
| Platform tools (cargo build-sbf) | v1.57 (Rust 1.95 sbpf), build arch `v0` |
| Host Rust | 1.89.0 (`rust-toolchain.toml`); rustup stable 1.100.0 also installed |
| Node / npm | 22.16.0 / 10.9.2 |
| @anchor-lang/core, litesvm, @solana/web3.js, @solana/spl-token, @solana/kit | 1.2.2, 1.6.2, 1.99.0, 0.4.15, 8.6.0 |
| mocha / ts-mocha / typescript | 9.2.2 / 10.1.0 / 5.9.3 |

`[profile.release]` in `Cargo.toml` has `overflow-checks = true`. All money math is u128 and checked (`src/math.rs`).

## Security review
Checklist, with the result and where it is enforced.

| Check | Result |
|---|---|
| Every account has seeds and bump or owner checks | PASS. Bet, Vault, Position are validated with `seeds` and the stored bump (`bet.bump`, `bet.vault_bump`, `position.bump`). Mint is pinned with `address = bet.mint`. Token accounts are typed `InterfaceAccount<TokenAccount>` (owner is a token program) with mint and owner constraints. `rent_receiver` / `payer` are pinned by `address`/`has_one` to `bet.payer`. Programs are `Interface<TokenInterface>` (only the two real token programs) and `Program<System>`. |
| Every signer is checked | PASS. depositor, rent_payer (and `== bet.payer`), authority, payer are `Signer`. claim and close_loser need any signer by design. Tests cover missing signatures. |
| Authority only instructions check `bet.authority` | PASS. lock, settle, refund_all, close_bet use `has_one = authority @ Unauthorized`. The sponsor key is rejected too. |
| Token accounts checked for mint and owner | PASS. depositor token: owner = depositor, mint = bet.mint. Treasury token: owner = bet.treasury, mint = bet.mint. Destination: owner = position.owner, mint = bet.mint. Vault: PDA and mint. |
| Vault only ever spent by the Bet PDA | PASS. The vault's token authority is the Bet PDA. Spending (settle fee, claim, close_bet dust, close_account) is signed only by `[b"bet", bet_id, bump]` CPI seeds. No instruction lets a caller pick the authority. |
| No way to reinitialise a position | PASS. `init_if_needed` only runs init when the account does not exist. A new position is recognised by an all zero account (`owner == default`); an existing one always has an owner, is checked for `owner == depositor`, `bet == bet.key()` and side, and its bet, owner and bump are never rewritten. Position seeds contain the depositor, so one wallet cannot touch another's position. |
| Rent only goes to `bet.payer` | PASS. Position `close =` and Bet `close =` target an account pinned to `bet.payer`; the Vault `close_account` destination is `bet.payer`. The 20 participant test asserts the sponsor's lamports return exactly. |
| All arithmetic checked | PASS. `checked_add/sub/mul/div` everywhere, u128 for fee, distributable and payout, `u64::try_from` on the way back, `overflow-checks = true` in release. |
| State machine transitions are exactly the specified ones | PASS. Open->Locked, Locked->Settled, Open/Locked->Refunded (by the oracle, or by anyone via emergency_refund after `refund_after`). No other writes to `state`. Covered per instruction in tests. |
| No unchecked remaining accounts | PASS. `ctx.remaining_accounts` is never used. The only `UncheckedAccount`s (`rent_receiver`, `payer`) are lamport sinks pinned by address. |
| A closed position cannot be revived in the same transaction | PASS. Anchor's `close` zeroes and reassigns the account. A tx that claims, refunds lamports to the position address and claims again fails and rolls back (tested), and deposit needs state Open, which is impossible once claims are possible (Settled/Refunded), so a closed position can never be re-created. |

Other points reviewed:
* Token-2022 is refused at `create_bet` (covered by a test; transfer fees and hooks would break the vault accounting). Later instructions then fail on any non classic token program because the vault is owned by the classic program.
* A fake token program cannot be passed: `Interface<TokenInterface>` checks the key.
* Donations to the vault do not change payouts (payouts use `side_totals`); they only enlarge the dust that close_bet sweeps to the treasury.
* Pre funded PDAs (lamports sent to a Position/Bet/Vault address) do not block creation or deposits (tested).
* The same vault passed as source and destination is rejected by Anchor (`ConstraintDuplicateMutableAccount`).
* Residual risks: the oracle key can pick any winning side (trust assumption by design) and can refund instead of settling; the offline oracle case is covered by emergency_refund; the contract has no upgrade authority policy yet (chunk 2).
* emergency_refund is permissionless but harmless: it needs state Open/Locked and `now >= refund_after`, moves no tokens, and the oracle can no longer lock or settle from that moment, so there is no race. Because `refund_after > funding_closes_at` is enforced at creation, deposits are always closed before it opens. `refund_after` is read only from the Bet account, never from instruction arguments after creation.

## Differences between the mock and the contract (for chunk 3 to align the mock)
1. `lock` rejects one sided markets and markets with fewer than 2 participants (`BothSidesRequired`). The mock locks anything. The backend `close_funding` already refunds those, so behavior matches; the mock should reject too.
2. `deposit` is rejected at or after `funding_closes_at` (`FundingClosed`). The mock has no time check.
3. Deposit error order: `MarketNotOpen`, `FundingClosed`, `InvalidSide`, `BelowMin`, `MarketFull` / `AlreadyOtherSide`, `AboveMax`. The mock checks `AlreadyOtherSide` and `AboveMax` before `MarketFull`.
4. `create_bet` is not idempotent: a second call with the same id fails (account already exists). Call get_bet first.
5. Double claim fails with Anchor `AccountNotInitialized` because the position is closed. The mock returns `ALREADY_CLAIMED` from a flag.
6. Claiming a loser position gives `NotAWinner` and the position stays until `close_loser_position` (no mock equivalent): in the mock a loser position never closes.
7. The mock's `get_positions` keeps claimed rows; on chain claimed and closed positions disappear.
8. `settle` has no "winning side holds no money" case, since lock guarantees both sides have money.
9. `close_bet` (dust sweep, closing accounts) has no mock equivalent.
10. `fee_bps`, `min_stake`, `max_stake`, `max_participants` bounds are enforced on chain (fee <= 1000, participants 2..=100, min >= 1, min <= max, decimals 6).
11. **emergency_refund has no mock equivalent.** The backend must pass `refund_after = settle_after + 12 hours` to `create_bet` (make the buffer configurable, for example `REFUND_BUFFER_HOURS=12`), and the worker should call `emergency_refund` for bets still Open or Locked after `refund_after`. The mock should store `refund_after`, reject `lock` and `settle` from that time (`PastRefundDeadline`), and allow a refund by anyone from then on (`RefundTooEarly` before).
12. A second claim of a closed position fails with Anchor `AccountNotInitialized` (code 3012). The backend client must map exactly that to `ALREADY_CLAIMED`.
13. `refund_all` and `settle` need only the oracle's signature; `claim` and `close_loser_position` can be sent by anyone.

## Handoff: chunk 2 (devnet deploy)
* Generate a real program keypair outside the repo, run `anchor keys sync` (updates `declare_id!` and `Anchor.toml`), rebuild with `--arch v0`, deploy with `solana program deploy --url devnet`. The id above is a throwaway.
* Decide the upgrade authority (keep it on a safe key, or make the program immutable after testing).
* Create a 6 decimal test mint on devnet (the program refuses other decimals) and a treasury owner plus its token account for that mint. Fund the oracle (authority) and sponsor (payer) keys with devnet SOL.
* Keys needed: program keypair, oracle (`bet.authority`), sponsor (`bet.payer`, also signs every deposit as `rent_payer`), treasury owner (only its public key is stored; its token account must exist before settle and close_bet). Keep all keypair files out of git (`contracts/.gitignore` already covers `.keys/`, `**/*keypair*.json`, `**/id.json`).
* Copy `target/idl/handshake_pool.json` somewhere the backend and deposit page can read it.

## Handoff: chunk 3 (RealChainClient)
* `create_bet(bet_id=uuid.bytes, authority, treasury owner, fee_bps, min, max, max_participants, funding_closes_at, refund_after)` with `refund_after = settle_after + buffer` (default 12 h), signed by the sponsor. Derive PDAs with the seeds above. Look the Bet up with get_bet first so create stays idempotent.
* `get_bet` / `get_positions`: decode the Bet account; positions via the memcmp filter. `participant_count`, `side_totals`, `state`, `fee_paid`, `distributable`, `winning_side` map directly onto `ChainBet`. `vault_base` = vault token balance. `treasury_balance_base` = balance of the treasury token account.
* `lock`, `settle`, `refund_all`: oracle signed (`lock`/`settle` fail with `PastRefundDeadline` from `refund_after`). `emergency_refund`: any signer, for bets still Open/Locked at `refund_after`; then pay refunds with `claim`. Treasury token account must exist before settle (use an idempotent ATA create).
* The deposit is a user plus sponsor transaction: the deposit page builds it, the user signs, the sponsor co-signs. The user needs an ATA for the mint (the sponsor can create it in the same transaction).
* Payout tasks: one `claim` per winner (or refund) and one `close_loser_position` per loser, any signer, sponsor pays the fee. Treat `AccountNotInitialized` on a claim as "already claimed". After all positions are closed call `close_bet`.
* Map program errors by number (6000..6020) to the backend `ChainError` codes: BelowMin->BELOW_MIN, AboveMax->ABOVE_MAX, MarketFull->MARKET_FULL, AlreadyOtherSide->ALREADY_OTHER_SIDE, MarketNotOpen->MARKET_NOT_OPEN, WrongState->WRONG_STATE, NotAWinner->NOT_A_WINNER, InvalidSide->INVALID_SIDE. The new codes (FundingClosed, BothSidesRequired, WrongMint, WrongTreasury, WrongDestination, WrongRentReceiver, PositionsStillOpen, PastRefundDeadline, RefundTooEarly, Unauthorized, FeeTooHigh, InvalidParams, MathOverflow) need new `ChainError` codes.
