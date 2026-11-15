# Chunk 6 rehearsal report

Date: 2026-11-15 (devnet). Driver: `backend/scripts/rehearsal.py` (see "Rehearsals on devnet" in `backend/README.md`).
The running backend worker, chain-service (`CHAIN_MODE=real`) and the deposit page did all the work. The script only moved
database timestamps and the simulated game, then checked devnet, the database and the page.

**Result: 4 scenarios (5 runs on devnet, plus one dry run), 0 failed checks, 0 production bugs found, no production code changed.**

| Run | Checks | Result |
|---|---|---|
| Scenario 1 on the real market `1eb24624` (Privy wallets) | 28/28 | PASS |
| Scenario 1 dry run on a fresh market `c8b99c1a` (local wallets, same $10/$5) | 28/28 | PASS |
| Scenario 2, one wallet on one side | 14/14 | PASS |
| Scenario 2, two wallets on the same side | 15/15 | PASS |
| Scenario 3, game cancelled before the lock | 15/15 | PASS |
| Scenario 3, game postponed before the lock | 15/15 | PASS |

## Scenario 1: settle with a winner (bet `1eb24624-7f0b-47d3-b2f1-d743591d76a0`, Texas Tech at Arizona State)

Steps the script took (every one printed as `CHANGED`):
`bets.funding_closes_at` 01:30 to one minute ago, so the worker locked it. Then `bets.kickoff_utc` and `sim_games.kickoff_utc`
ten minutes back and the simulated game set to final, Arizona State 27 to Texas Tech 24, so the worker settled it and ran the payouts.
Then `bets.payouts_completed_at` moved back past the 2 hour link grace, so the worker ran `close_bet` and the link went 410.

| Item | Expected | Actual | Result |
|---|---|---|---|
| Lock transaction | confirmed | [282g7CfhAin3...](https://explorer.solana.com/tx/282g7CfhAin3HPAY6mef3KFZiPiMi46ELxQUd4QMENmp1C6YpqqY16MsnJhA3VZMpnpTKNMt7jDJ4TAaomdnGvyv?cluster=devnet), chain `locked`, 2 snapshots `held` | PASS |
| Page while locked | locked view | "Betting is closed. Waiting for the game." | PASS |
| Settle transaction | confirmed, winning side 1 | [3ecJcN4nnjeQ...](https://explorer.solana.com/tx/3ecJcN4nnjeQkaEqkCWbr1SpRGd2mRukuFNnVcTgf7EcnUdDSWdAHusp4to7cPyd7ws2qvisuMqcD6Ga2oXF3QHy?cluster=devnet) | PASS |
| Winner payout | 14.50 USDC = floor(5000000 * (15000000 - 500000) / 5000000) | `payout_tasks.amount_base` 14.50 | PASS |
| Wallet 5Muq...sPKu balance | +14.50 | 20.00 to 34.50 | PASS |
| Claim transaction (winner) | confirmed | [PpJtyPTNeUEr...](https://explorer.solana.com/tx/PpJtyPTNeUErGpqNosWtd8xXoVTRGQqTEL9EhmjHo3a3ppqHZaRwaTHSi9M6NHAA8a6HZgk57PMLAYEEt1E6bVF?cluster=devnet) | PASS |
| Treasury fee received | 0.50 USDC | +0.50 | PASS |
| Loser 2cPA...GtW4 | position closed, balance unchanged | `close_loser` done, balance 15.00 to 15.00, Position account gone on devnet, close tx [1PyZnv6N5ga2...](https://explorer.solana.com/tx/1PyZnv6N5ga2nrTg96efdDQ9xDMUYp72sPVbmv4M3RiCbK52iRQQ97k9MnMCsQh3zN48oY4WJt4UsPV9WuFSYcq?cluster=devnet) | PASS |
| Vault before `close_bet` | dust only | 0 base units (14,500,000 pays out exactly) | PASS |
| `payout_tasks` | 1 win + 1 close_loser, done | `5Muq win 14.50 done tx PpJty...`, `2cPA close_loser 0.00 done` | PASS |
| `position_snapshots` | winner paid, loser closed | `5Muq side 1 stake 5.00 payout 14.50 paid`, `2cPA side 0 stake 10.00 payout 0.00 closed_loser` | PASS |
| `bet_notices` | link, locked, result sent to the proposer | all three `sent`. Result text: "Final score: Texas Tech 24, Arizona State 27. Arizona State wins. The pool was $15. The fee was $0.50, 5% of the losing side. Payouts are being sent now. The link stays live for 2 hours after payouts finish." | PASS |
| Page settled view | winner shown | "Arizona State won." | PASS |
| `close_bet` after the grace | worker runs it, account gone | `chain_closed_at` set, Bet account `NOT_FOUND`, tx [ynnP2rTosxYa...](https://explorer.solana.com/tx/ynnP2rTosxYaX7fdjujSMM3oUyC89LsVZm1K7dhrx4XtzFxwBi1qD88wEuXzLMCS4TMyjHKZ1Td3aQX9S1JJL79?cluster=devnet) | PASS |
| Link after the grace | 410 `LINK_EXPIRED`, page "This bet is over" | both | PASS |
| Treasury after `close_bet` | fee + dust | 0.50 (dust 0) | PASS |

Screenshots of the locked, settled and expired pages are in `deposit-page/screenshots/rehearsal-settle-*.png` (gitignored).

## Scenario 2: refund, one side empty

New market through `MarketService.open_market` (what the text flow's YES calls), deposits through the page's sponsored flow with a local wallet,
then `bets.funding_closes_at` moved to the past. The worker refunded with no fee, the vault was empty, the treasury did not move, every
`payout_tasks` row was a `refund` for the full stake, every snapshot ended `refunded`, and each wallet went 25.00 to 20.00 to 25.00.
The page showed the refunded view, then 410 and "This bet is over" after `close_bet`.

| Variant | Refund reason | Page words | Notice | RefundAll / CloseBet |
|---|---|---|---|---|
| 1 wallet, side 0, $5 | `too_few_participants` | "Not enough people joined this bet." | `refunded` sent | [RefundAll](https://explorer.solana.com/tx/2P6pAdNjXMjpE8fXevCbf5GJZQADumMEF9rT2hcGYptCkBktYDaMzegZHcLt9HBJXLp1iCCuk1EnZBcmqEUUvLQL?cluster=devnet), [CloseBet](https://explorer.solana.com/tx/219mtzm8v18sArjSBNrkBM9GgxaX2wRc1t8hVWTfFLEDVzndV9CZjZQ6ViUdA4S4rBABHZC4vQaR9rJc1t31nHD7?cluster=devnet) |
| 2 wallets, both side 0, $5 each | `one_sided` | "Everyone picked the same team, so there was nobody to win against." | `refunded` sent | [RefundAll](https://explorer.solana.com/tx/s3RFpY2UL5v2TTJNWLgirft4PWaHR8LjuEGwSbLu6wMmdw2Nc3NLCUiZBp8dNbu8QHaymx7SfaYZkAXsj48NJVm?cluster=devnet), [CloseBet](https://explorer.solana.com/tx/3rMyzizhonZENzSuQ4inx16P72KtkqKFxogb4jR4Nst1WGiDFmuVc9kW6mFUheg2MoFzNJq2BDrJA7S89TP5aa8j?cluster=devnet) |

## Scenario 3: cancelled or postponed game

Two wallets on opposite sides ($5 and $3), so the game status is the only possible reason. The script set the simulated game to
`cancelled` (or `postponed`) while funding was still open and did not move any time. The worker refunded on its next tick.

| Game status | Refund reason | Page words | Notice | RefundAll / CloseBet |
|---|---|---|---|---|
| cancelled | `cancelled` | "The game was cancelled." | `refund_result` sent | [RefundAll](https://explorer.solana.com/tx/P3i6tcDqmMNGvrFu9qP9Fv9b3fvsRpvq4cL2qzzPyLzVbnBxV4eLg8YGNHsUGCB5x5ZL9A9bafAnkwgEmLiygNU?cluster=devnet), [CloseBet](https://explorer.solana.com/tx/SV8b7JAqTUyVijiUDJHgQyo7deru8mmnNiXExMTXB4ySzqWhjGHs9DQRcLQ845eJ2M7BuM5iRbYz3kYiYcewwCJ?cluster=devnet) |
| postponed | `postponed` | "The game was postponed." | `refund_result` sent | [RefundAll](https://explorer.solana.com/tx/363FhTP2VU6NBShP8FvguDtHRjViXnu6Yx9M33AZQvEdYfyMSBuPTwa4BeXdjG8JgSdzZPiMVUdtakPbK6NhRVR8?cluster=devnet), [CloseBet](https://explorer.solana.com/tx/WDyupW5nN61m6DnmCF7NX4Mnc5jXnzkMNVifqZYmUiAJzFVJ7v7EawLeKvCh29RVc7gqC5Y82UFsPuCcmbVzCJz?cluster=devnet) |

## Bugs found

None. Every stage behaved as documented, and `GET /admin/worker/status` ended with 0 permanent notice failures, 0 failed payout tasks
and every rehearsal Bet account closed (rent back to the payer: payer SOL 2.901 before, 2.902 after).

## What surprised me (none of it is a defect)

1. **One side empty has two reasons.** A single wallet gives `too_few_participants`, because the worker checks the participant count
   first. `one_sided` needs two or more wallets on the same side. Both texts and page words are fine, but the brief's "single deposit,
   one side" lands on `too_few_participants`.
2. **The two read confirmation was not exercised.** The simulated provider defaults `SETTLE_CONFIRM_WAIT_MINUTES` to 0, so the first final
   read already settles (`result_first_seen_at` equals `final_confirmed_at`). The restart that does exercise it is at the end of this
   report. The rule itself is covered by `tests/test_worker.py`.
3. **The contract's `lock` ignores `funding_closes_at`.** It only requires being before `refund_after`, so moving the database deadline was
   enough for the oracle to lock while the on chain deadline was still days away. That is why the rehearsal works without faking
   anything, but it also means the database deadline, not the chain, decides when the oracle locks. Normally they are equal.
4. **The link grace was moved, not waited.** The running process uses `LINK_GRACE_HOURS` (2 h), so `payouts_completed_at` was moved back
   past it. This covers the 410, the page, and the worker's `close_bet`, but not the real countdown.
5. **The airdrop limit bites rehearsals.** `AIRDROP_PER_IP_PER_HOUR` (5) was hit after about 6 fresh wallets. The driver now reuses two
   local wallets (`rehearsal-pool-0/1`) and only asks for an airdrop when a wallet holds less than the stake. A failed run before that
   change left three empty markets open. `cleanup` refunded and closed them, which also showed that the worker handles markets with no
   deposits (no positions, no tasks, `refund_result` still texted to the proposer).
6. **Text volume.** Each market texts the live test number two or three times. This rehearsal sent about 25 texts in total.
7. **Speed.** From the final result to payouts complete took one worker tick (about 4 seconds on devnet). The slowest stage was
   the page checker and the public RPC rate limit, not the worker.

## Tests

| Suite | Before | After |
|---|---|---|
| backend (`pytest`) | 480 passed, 1 skipped | 491 passed, 1 skipped (+11 for the script's guard and money helpers) |
| chain-service (`npm test`) | 69 run, 67 passed, 2 skipped | unchanged |
| deposit-page (`vitest`) | 103 passed | unchanged |

## To rehearse the two read rule (needs a restart, so I did not do it)

Stop the backend, start it with a process env override (the `.env` file is not touched), then rerun scenario 1 on a fresh market:

```sh
cd backend && SETTLE_CONFIRM_WAIT_MINUTES=1 .venv/bin/python -m app
# in another terminal:
cd backend && SETTLE_CONFIRM_WAIT_MINUTES=1 .venv/bin/python scripts/rehearsal.py settle --fresh
```

Expect `result_first_seen_at` and `final_confirmed_at` about a minute apart, and the market to settle only on the second read.
