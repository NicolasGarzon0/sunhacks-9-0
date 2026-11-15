// Runs against the real devnet program. Only with RUN_DEVNET_TESTS=1. Needs the same env as the service plus MINT_AUTHORITY_SECRET_KEY.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import { ChainService } from "../src/chain.ts";
import { loadConfig } from "../src/config.ts";
import { createLogger } from "../src/logger.ts";
import { ConnectionRpc, guardDevnet } from "../src/rpc.ts";
import { depositFor, envFromProcess } from "../scripts/dev_deposit.ts";

const run = process.env.RUN_DEVNET_TESTS === "1";

test("devnet: create, deposit x2, lock, settle, claim, close_loser_position, close_bet (and repeats are already:true)", { skip: !run, timeout: 240_000 }, async () => {
  const cfg = loadConfig();
  const rpc = ConnectionRpc.create(cfg.rpcUrl);
  await guardDevnet(rpc);
  const chain = new ChainService({ rpc, cfg, logger: createLogger({ level: "warn" }) });
  const env = envFromProcess();
  const id = randomUUID();
  const now = Math.floor(Date.now() / 1000);

  const created: any = await chain.createBet({ bet_id: id, funding_closes_at: now + 600, refund_after: now + 1200, fee_bps: 500, min_stake_base: 1_000_000, max_stake_base: 20_000_000, max_participants: 20 });
  assert.equal(created.ok, true);
  assert.equal(created.state, "open");
  assert.equal(((await chain.createBet({ bet_id: id, funding_closes_at: now + 600, refund_after: now + 1200, fee_bps: 500, min_stake_base: 1_000_000, max_stake_base: 20_000_000, max_participants: 20 })) as any).already, true);

  const a = await depositFor(env, id, 0, "4"); // loser, side 0
  const b = await depositFor(env, id, 1, "6"); // winner, side 1
  const bet: any = await chain.getBet(id);
  assert.deepEqual(bet.side_totals, [4_000_000, 6_000_000]);
  assert.equal(bet.participant_count, 2);
  const pos: any = await chain.positions(id);
  assert.equal(pos.positions.length, 2);

  assert.equal(((await chain.lock(id)) as any).already, false);
  assert.equal(((await chain.lock(id)) as any).already, true);
  const settled: any = await chain.settle(id, 1);
  assert.equal(settled.fee_paid_base, 200_000); // 5% of the losing 4 USDC
  assert.equal(settled.distributable_base, 9_800_000);
  assert.equal(((await chain.settle(id, 1)) as any).already, true);

  const claim: any = await chain.claim(id, b.wallet);
  assert.equal(claim.payout_base, 9_800_000);
  assert.equal(((await chain.claim(id, b.wallet)) as any).already, true, "a second claim means already claimed");
  assert.equal(((await chain.closeLoserPosition(id, a.wallet)) as any).already, false);
  assert.equal(((await chain.closeLoserPosition(id, a.wallet)) as any).already, true);

  const final: any = await chain.getBet(id);
  assert.equal(final.positions_open, 0);
  assert.equal(final.vault_base, 0);
  assert.equal(((await chain.closeBet(id)) as any).already, false);
  assert.equal(((await chain.closeBet(id)) as any).already, true);
});
