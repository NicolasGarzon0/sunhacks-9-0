import assert from "node:assert/strict";
import { test } from "node:test";
import { Keypair } from "@solana/web3.js";
import { ChainError } from "../src/errors.ts";
import { BET_ID, USDC, instructionNames, ixAccounts, putBet, putPosition, setup, signersOf } from "./helpers.ts";

const code = async (p: Promise<unknown>) => { try { await p; return "no error"; } catch (e) { return (e as ChainError).code ?? String(e); } };

// ---------- trust check ----------
test("trust check: wrong authority, payer, treasury or mint is UNTRUSTED_BET on every read and write", async () => {
  const other = Keypair.generate().publicKey;
  for (const over of [{ authority: other }, { payer: other }, { treasury: other }, { mint: other }]) {
    const s = setup();
    await putBet(s, { ...over, state: "locked" });
    assert.equal(await code(s.chain.getBet(BET_ID)), "UNTRUSTED_BET", JSON.stringify(Object.keys(over)));
    assert.equal(await code(s.chain.positions(BET_ID)), "UNTRUSTED_BET");
    assert.equal(await code(s.chain.lock(BET_ID)), "UNTRUSTED_BET");
    assert.equal(await code(s.chain.settle(BET_ID, 1)), "UNTRUSTED_BET");
    assert.equal(await code(s.chain.refundAll(BET_ID)), "UNTRUSTED_BET");
    assert.equal(await code(s.chain.claim(BET_ID, other.toBase58())), "UNTRUSTED_BET");
    assert.equal(await code(s.chain.closeLoserPosition(BET_ID, other.toBase58())), "UNTRUSTED_BET");
    assert.equal(await code(s.chain.closeBet(BET_ID)), "UNTRUSTED_BET");
    assert.equal(s.rpc.sent.length, 0, "nothing was sent for an untrusted bet");
  }
});

test("trust check: an account owned by another program is untrusted; a trusted bet reads fine", async () => {
  const s = setup();
  const pda = await putBet(s);
  s.rpc.accounts.get(pda.toBase58())!.owner = Keypair.generate().publicKey;
  assert.equal(await code(s.chain.getBet(BET_ID)), "UNTRUSTED_BET");
  const t = setup();
  await putBet(t);
  const v: any = await t.chain.getBet(BET_ID);
  assert.equal(v.state, "open");
  assert.deepEqual(v.side_totals, [10_000_000, 20_000_000]);
  assert.equal(v.winning_side, null);
  assert.equal(v.refund_after, 1_900_040_000);
});

test("unknown bet is NOT_FOUND; bad uuid is BAD_REQUEST", async () => {
  const s = setup();
  assert.equal(await code(s.chain.getBet(BET_ID)), "NOT_FOUND");
  assert.equal(await code(s.chain.getBet("not-a-uuid")), "BAD_REQUEST");
});

// ---------- create ----------
const params = { bet_id: BET_ID, funding_closes_at: 1_900_000_000, refund_after: 1_900_040_000, fee_bps: 500, min_stake_base: 1_000_000, max_stake_base: 20_000_000, max_participants: 20 };

test("create_bet: payer signs and pays, oracle is the authority, treasury from config; idempotent", async () => {
  const s = setup();
  s.rpc.onSend = () => putBet(s, { state: "open", participantCount: 0, positionsOpen: 0, sideTotals: [0n, 0n] }); // the chain "creates" it
  const r: any = await s.chain.createBet(params);
  assert.equal(r.ok, true);
  assert.equal(r.already, false);
  assert.equal(s.rpc.sent.length, 1);
  const tx = s.rpc.sent[0]!;
  assert.deepEqual(instructionNames(tx, s.cfg.programId), ["create_bet"]);
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()), [s.cfg.payer.publicKey.toBase58()]); // the payer is the only signer
  const again: any = await s.chain.createBet(params);
  assert.equal(again.already, true);
  assert.equal(s.rpc.sent.length, 1, "no second transaction");
});

test("create_bet: an existing but untrusted bet is rejected, not returned", async () => {
  const s = setup();
  await putBet(s, { treasury: Keypair.generate().publicKey });
  assert.equal(await code(s.chain.createBet(params)), "UNTRUSTED_BET");
});

test("create_bet: program rejection is mapped (INVALID_PARAMS) and non integers are BAD_REQUEST", async () => {
  const s = setup();
  s.rpc.sendError = Object.assign(new Error("Simulation failed"), { logs: ["Program log: AnchorError thrown in x.rs:1. Error Code: InvalidParams. Error Number: 6011. Error Message: Invalid parameters."] });
  assert.equal(await code(s.chain.createBet(params)), "INVALID_PARAMS");
  assert.equal(await code(s.chain.createBet({ ...params, fee_bps: 1.5 })), "BAD_REQUEST");
});

// ---------- lock / refund_all / settle ----------
test("lock: oracle signs, payer is the fee payer; already locked or settled is already:true; refunded is WRONG_STATE", async () => {
  const s = setup();
  await putBet(s, { state: "open" });
  const r: any = await s.chain.lock(BET_ID);
  assert.equal(r.already, false);
  const tx = s.rpc.sent[0]!;
  assert.deepEqual(instructionNames(tx, s.cfg.programId), ["lock"]);
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()), [s.cfg.payer.publicKey.toBase58(), s.cfg.oracle.publicKey.toBase58()]);
  for (const state of ["locked", "settled"]) {
    const t = setup(); await putBet(t, { state });
    assert.equal(((await t.chain.lock(BET_ID)) as any).already, true, state);
    assert.equal(t.rpc.sent.length, 0);
  }
  const u = setup(); await putBet(u, { state: "refunded" });
  assert.equal(await code(u.chain.lock(BET_ID)), "WRONG_STATE");
});

test("lock: program rejections map (BOTH_SIDES_REQUIRED, PAST_REFUND_DEADLINE)", async () => {
  for (const [name, num, want] of [["BothSidesRequired", 6009, "BOTH_SIDES_REQUIRED"], ["PastRefundDeadline", 6019, "PAST_REFUND_DEADLINE"]] as const) {
    const s = setup(); await putBet(s);
    s.rpc.sendError = Object.assign(new Error("x"), { logs: [`Program log: AnchorError thrown in x.rs:1. Error Code: ${name}. Error Number: ${num}. Error Message: m.`] });
    assert.equal(await code(s.chain.lock(BET_ID)), want);
  }
});

test("settle: ensures the treasury token account, oracle signs; same side again is already:true, other side WRONG_STATE", async () => {
  const s = setup();
  await putBet(s, { state: "locked" });
  await s.chain.settle(BET_ID, 1);
  const tx = s.rpc.sent[0]!;
  const names = instructionNames(tx, s.cfg.programId);
  assert.equal(names.length, 2);
  assert.equal(names[1], "settle");
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()), [s.cfg.payer.publicKey.toBase58(), s.cfg.oracle.publicKey.toBase58()]);
  const settledAcc = ixAccounts(tx, 1);
  assert.equal(settledAcc[4]!.key.toBase58(), s.chain.treasuryAta.toBase58());

  const t = setup(); await putBet(t, { state: "settled", winningSide: 1 });
  assert.equal(((await t.chain.settle(BET_ID, 1)) as any).already, true);
  assert.equal(await code(t.chain.settle(BET_ID, 0)), "WRONG_STATE");
  const u = setup(); await putBet(u, { state: "open" });
  assert.equal(await code(u.chain.settle(BET_ID, 0)), "WRONG_STATE");
  assert.equal(await code(u.chain.settle(BET_ID, 2)), "BAD_REQUEST");
});

test("refund_all: open or locked sends, refunded is already:true, settled is WRONG_STATE", async () => {
  for (const state of ["open", "locked"]) {
    const s = setup(); await putBet(s, { state });
    assert.equal(((await s.chain.refundAll(BET_ID)) as any).already, false);
    assert.deepEqual(instructionNames(s.rpc.sent[0]!, s.cfg.programId), ["refund_all"]);
  }
  const t = setup(); await putBet(t, { state: "refunded" });
  assert.equal(((await t.chain.refundAll(BET_ID)) as any).already, true);
  assert.equal(t.rpc.sent.length, 0);
  const u = setup(); await putBet(u, { state: "settled", winningSide: 0 });
  assert.equal(await code(u.chain.refundAll(BET_ID)), "WRONG_STATE");
});

// ---------- claim / close_loser / close_bet ----------
test("claim: payer is signer and fee payer, rent goes to bet.payer, payout uses the pool math", async () => {
  const s = setup();
  const winner = Keypair.generate().publicKey;
  await putBet(s, { state: "settled", winningSide: 1, sideTotals: [15n * USDC, 15_250_000n], feePaid: 750_000n, distributable: 29_500_000n });
  await putPosition(s, winner, 1, 6n * USDC);
  const r: any = await s.chain.claim(BET_ID, winner.toBase58());
  assert.equal(r.already, false);
  assert.equal(r.payout_base, 11_606_557); // floor(6 * 29.5 / 15.25) in base units
  assert.equal(r.kind, "winnings");
  const tx = s.rpc.sent[0]!;
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()), [s.cfg.payer.publicKey.toBase58()]);
  assert.equal(instructionNames(tx, s.cfg.programId)[1], "claim");
  const acc = ixAccounts(tx, 1);
  assert.equal(acc[6]!.key.toBase58(), s.cfg.payer.publicKey.toBase58()); // rent receiver
  assert.equal(acc[0]!.key.toBase58(), s.cfg.payer.publicKey.toBase58()); // signer
});

test("claim: refunded pays the stake; a loser is NOT_A_WINNER; a missing position is already:true", async () => {
  const s = setup();
  const u = Keypair.generate().publicKey;
  await putBet(s, { state: "refunded" });
  await putPosition(s, u, 0, 7n * USDC);
  const r: any = await s.chain.claim(BET_ID, u.toBase58());
  assert.equal(r.payout_base, 7_000_000);
  assert.equal(r.kind, "refund");
  const t = setup();
  const loser = Keypair.generate().publicKey;
  await putBet(t, { state: "settled", winningSide: 1, distributable: 1n });
  await putPosition(t, loser, 0, 5n * USDC);
  assert.equal(await code(t.chain.claim(BET_ID, loser.toBase58())), "NOT_A_WINNER");
  const gone: any = await t.chain.claim(BET_ID, Keypair.generate().publicKey.toBase58());
  assert.equal(gone.already, true);
  assert.equal(gone.payout_base, null);
  assert.equal(await code(t.chain.claim(BET_ID, "nope")), "BAD_REQUEST");
  const o = setup(); await putBet(o, { state: "locked" });
  assert.equal(await code(o.chain.claim(BET_ID, u.toBase58())), "WRONG_STATE");
});

test("claim race: another claimer closed the position first -> already:true instead of an error", async () => {
  const s = setup();
  const winner = Keypair.generate().publicKey;
  await putBet(s, { state: "refunded" });
  const key = await putPosition(s, winner, 0, 3n * USDC);
  s.rpc.onSend = () => { s.rpc.accounts.delete(key.toBase58()); }; // the other claim lands while ours is in flight
  s.rpc.sendError = Object.assign(new Error("x"), { logs: ["Program log: AnchorError caused by account: position. Error Code: AccountNotInitialized. Error Number: 3012. Error Message: The program expected this account to be already initialized."] });
  const r: any = await s.chain.claim(BET_ID, winner.toBase58());
  assert.equal(r.already, true);
});

test("close_loser_position: loser closes with the payer as signer; winner is WRONG_STATE; missing is already:true", async () => {
  const s = setup();
  const loser = Keypair.generate().publicKey, winner = Keypair.generate().publicKey;
  await putBet(s, { state: "settled", winningSide: 1 });
  await putPosition(s, loser, 0, 5n * USDC);
  await putPosition(s, winner, 1, 5n * USDC);
  const r: any = await s.chain.closeLoserPosition(BET_ID, loser.toBase58());
  assert.equal(r.already, false);
  const tx = s.rpc.sent[0]!;
  assert.deepEqual(instructionNames(tx, s.cfg.programId), ["close_loser_position"]);
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()), [s.cfg.payer.publicKey.toBase58()]);
  assert.equal(await code(s.chain.closeLoserPosition(BET_ID, winner.toBase58())), "WRONG_STATE");
  assert.equal(((await s.chain.closeLoserPosition(BET_ID, Keypair.generate().publicKey.toBase58())) as any).already, true);
  const t = setup(); const u = Keypair.generate().publicKey;
  await putBet(t, { state: "locked" }); await putPosition(t, u, 0, 1n);
  assert.equal(await code(t.chain.closeLoserPosition(BET_ID, u.toBase58())), "WRONG_STATE");
});

test("close_bet: needs settled or refunded and no open positions; oracle signs; missing bet is already:true", async () => {
  const s = setup();
  await putBet(s, { state: "settled", winningSide: 1, positionsOpen: 1 });
  assert.equal(await code(s.chain.closeBet(BET_ID)), "WRONG_STATE");
  const t = setup(); await putBet(t, { state: "locked", positionsOpen: 0 });
  assert.equal(await code(t.chain.closeBet(BET_ID)), "WRONG_STATE");
  const u = setup(); await putBet(u, { state: "refunded", positionsOpen: 0 });
  const r: any = await u.chain.closeBet(BET_ID);
  assert.equal(r.already, false);
  const tx = u.rpc.sent[0]!;
  assert.equal(instructionNames(tx, u.cfg.programId)[1], "close_bet");
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()), [u.cfg.payer.publicKey.toBase58(), u.cfg.oracle.publicKey.toBase58()]);
  const none = setup();
  assert.equal(((await none.chain.closeBet(BET_ID)) as any).already, true);
});

// ---------- positions ----------
test("positions: lists only this bet's positions via the dataSize + memcmp(offset 8) filter", async () => {
  const s = setup();
  const a = Keypair.generate().publicKey, b = Keypair.generate().publicKey;
  await putBet(s); await putBet(s, {}, "11111111-2222-4333-8444-555555555555");
  await putPosition(s, a, 0, 5n * USDC); await putPosition(s, b, 1, 6n * USDC);
  await putPosition(s, Keypair.generate().publicKey, 1, 9n * USDC, "11111111-2222-4333-8444-555555555555");
  const r: any = await s.chain.positions(BET_ID);
  assert.equal(r.positions.length, 2);
  const byWallet = Object.fromEntries(r.positions.map((p: any) => [p.wallet, p]));
  assert.deepEqual(byWallet[a.toBase58()], { wallet: a.toBase58(), side: 0, amount_base: 5_000_000 });
  assert.deepEqual(byWallet[b.toBase58()], { wallet: b.toBase58(), side: 1, amount_base: 6_000_000 });
});

test("logs carry bet id, instruction and signature, never key material", async () => {
  const s = setup();
  await putBet(s, { state: "open" });
  await s.chain.lock(BET_ID);
  const text = s.lines.join("\n");
  assert.match(text, /"event":"tx.sent"/);
  assert.match(text, new RegExp(BET_ID));
  assert.ok(!text.includes(Buffer.from(s.cfg.payer.secretKey).toString("base64")));
  assert.ok(!text.includes(JSON.stringify(Array.from(s.cfg.oracle.secretKey))));
});
