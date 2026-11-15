import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AddressLookupTableAccount, ComputeBudgetProgram, Keypair, PublicKey, Transaction, TransactionInstruction, TransactionMessage, VersionedTransaction,
} from "@solana/web3.js";
import { createAssociatedTokenAccountInstruction, createCloseAccountInstruction, createTransferCheckedInstruction } from "@solana/spl-token";
import { DepositService } from "../src/deposit.ts";
import { signEd25519, verifyEd25519 } from "../src/crypto.ts";
import { ChainError } from "../src/errors.ts";
import { BET_ID, USDC, putBet, putPosition, setup } from "./helpers.ts";

type S = ReturnType<typeof setup>;

async function env(over: Parameters<typeof setup>[0] = {}, bet: Parameters<typeof putBet>[1] = { state: "open", participantCount: 2, positionsOpen: 2 }) {
  const s = setup(over);
  await putBet(s, bet);
  const svc = new DepositService({ rpc: s.rpc, cfg: s.cfg, chain: s.chain, logger: { debug() {}, info() {}, warn() {}, error() {} } });
  const user = Keypair.generate();
  const ata = s.pool.ata(user.publicKey, s.cfg.mint);
  s.rpc.accounts.set(ata.toBase58(), { data: Buffer.alloc(165), lamports: 2_000_000, owner: Keypair.generate().publicKey });
  s.rpc.tokens.set(ata.toBase58(), 100n * USDC);
  const args = { bet_id: BET_ID, wallet: user.publicKey.toBase58(), side: 1, amount_base: Number(5n * USDC) };
  return { s, svc, user, ata, args };
}

const b64 = (b: Uint8Array) => Buffer.from(b).toString("base64");
const sign = (user: Keypair, message: Uint8Array) => signEd25519(user.secretKey, message);
const code = async (p: Promise<unknown>) => { try { await p; return "no error"; } catch (e) { return `${(e as ChainError).code}`; } };
async function reason(p: Promise<unknown>) { try { await p; return ""; } catch (e) { return (e as ChainError).detail; } }

/** a legacy message with the given instructions, the sponsor (or someone else) as fee payer */
const legacy = (feePayer: PublicKey, ixs: TransactionInstruction[], blockhash: string) => new Transaction({ feePayer, recentBlockhash: blockhash }).add(...ixs).serializeMessage();

async function realIx(e: Awaited<ReturnType<typeof env>>, over: Partial<{ side: number; amount: bigint }> = {}) {
  return e.s.pool.depositIx({ depositor: e.user.publicKey, rentPayer: e.s.cfg.payer.publicKey, bet: e.s.pool.betPda(BET_ID), mint: e.s.cfg.mint, side: over.side ?? e.args.side, amount: over.amount ?? BigInt(e.args.amount_base) });
}
const submitMsg = (e: Awaited<ReturnType<typeof env>>, message: Uint8Array, sig: Uint8Array, args = e.args) => e.svc.submit({ ...args, message_b64: b64(message), user_signature_b64: b64(sig) });

// ---------------- build ----------------

test("build: one legacy deposit transaction, payer is fee payer and rent payer, nothing signed", async () => {
  const e = await env();
  const r: any = await e.svc.build(e.args);
  assert.equal(r.ok, true);
  const tx = Transaction.from(Buffer.from(r.transaction_b64, "base64"));
  assert.equal(tx.instructions.length, 1);
  assert.equal(tx.feePayer!.toBase58(), e.s.cfg.payer.publicKey.toBase58());
  assert.equal(tx.signatures.length, 2);
  assert.ok(tx.signatures.every((x) => x.signature === null), "no signature at build time");
  assert.equal(r.blockhash, e.s.rpc.blockhash);
  assert.equal(typeof r.last_valid_block_height, "number");
  assert.ok(Date.parse(r.expires_at) > Date.now() && Date.parse(r.expires_at) < Date.now() + 61_000);
  assert.equal(r.message_b64, b64(tx.serializeMessage()));
  const keys = tx.instructions[0]!.keys;
  assert.equal(keys.filter((k) => k.isSigner).length, 2);
  assert.equal(e.s.rpc.sent.length, 0);
});

test("build refuses what the contract would refuse, with friendly codes", async () => {
  const cases: [string, Awaited<ReturnType<typeof env>> | Promise<Awaited<ReturnType<typeof env>>>, Partial<{ amount_base: number; side: number }>, string][] = [];
  const open = await env();
  assert.equal(await code(open.svc.build({ ...open.args, amount_base: Number(USDC) / 2 })), "BELOW_MIN");
  assert.equal(await code(open.svc.build({ ...open.args, amount_base: Number(21n * USDC) })), "ABOVE_MAX");
  assert.equal(await code(open.svc.build({ ...open.args, side: 2 })), "INVALID_PARAMS");
  assert.equal(await code(open.svc.build({ ...open.args, amount_base: 0 })), "INVALID_PARAMS");
  assert.equal(await code(open.svc.build({ ...open.args, wallet: "nope" })), "BAD_REQUEST");
  void cases;
  const locked = await env({}, { state: "locked" });
  assert.equal(await code(locked.svc.build(locked.args)), "MARKET_NOT_OPEN");
  const closing = await env();
  closing.s.rpc.clock = 1_900_000_000 - 30; // inside the 30 second cutoff
  assert.equal(await code(closing.svc.build(closing.args)), "FUNDING_CLOSED");
  closing.s.rpc.clock = 1_900_000_000 - 31;
  assert.equal(await code(closing.svc.build(closing.args)), "no error");
  const full = await env({}, { state: "open", participantCount: 20 });
  assert.equal(await code(full.svc.build(full.args)), "MARKET_FULL");
  const noAta = await env();
  noAta.s.rpc.accounts.delete(noAta.ata.toBase58());
  assert.equal(await code(noAta.svc.build(noAta.args)), "NO_TOKEN_ACCOUNT");
  const poor = await env();
  poor.s.rpc.tokens.set(poor.ata.toBase58(), 4n * USDC);
  assert.equal(await code(poor.svc.build(poor.args)), "INSUFFICIENT_USDC");
});

test("build: a wallet's existing position decides ALREADY_OTHER_SIDE, top ups and ABOVE_MAX; a full market still allows a top up", async () => {
  const e = await env({}, { state: "open", participantCount: 20 });
  await putPosition(e.s, e.user.publicKey, 1, 18n * USDC);
  assert.equal(await code(e.svc.build({ ...e.args, side: 0 })), "ALREADY_OTHER_SIDE");
  assert.equal(await code(e.svc.build({ ...e.args, amount_base: Number(3n * USDC) })), "ABOVE_MAX"); // 18 + 3 > 20
  assert.equal(await code(e.svc.build({ ...e.args, amount_base: Number(2n * USDC) })), "no error"); // exactly the max, on a full market
});

test("build: an untrusted bet is refused", async () => {
  const e = await env({}, { state: "open", treasury: Keypair.generate().publicKey });
  assert.equal(await code(e.svc.build(e.args)), "UNTRUSTED_BET");
});

// ---------------- submit: success ----------------

test("submit (message + signature shape): payer signs the verified bytes unchanged, then sends once", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const message = Buffer.from(built.message_b64, "base64");
  const r: any = await submitMsg(e, message, sign(e.user, message));
  assert.equal(r.ok, true);
  assert.match(r.explorer, /explorer\.solana\.com\/tx\/.+cluster=devnet/);
  assert.equal(e.s.rpc.sent.length, 1);
  const sent = e.s.rpc.sent[0]!;
  assert.equal(sent.signatures.length, 2);
  assert.ok(Buffer.from(sent.message.serialize()).equals(message), "the sent message is byte for byte the verified message");
  assert.ok(verifyEd25519(e.s.cfg.payer.publicKey.toBytes(), message, sent.signatures[0]!), "payer signature is valid over those bytes");
  assert.ok(verifyEd25519(e.user.publicKey.toBytes(), message, sent.signatures[1]!), "user signature is valid over those bytes");
  assert.equal(e.s.rpc.simulated.length, 1);
  assert.ok(Buffer.from(e.s.rpc.simulated[0]!).subarray(1, 65).every((b) => b === 0), "the payer slot is empty while simulating");
});

test("submit (signed transaction shape): the user fills slot 2, the payer slot stays empty until verified", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const wire = Buffer.from(built.transaction_b64, "base64");
  const message = Buffer.from(built.message_b64, "base64");
  sign(e.user, message).copy(wire, 1 + 64); // user slot = second signer
  const r: any = await e.svc.submit({ ...e.args, signed_tx_b64: b64(wire) });
  assert.equal(r.ok, true);
  assert.equal(e.s.rpc.sent.length, 1);
});

test("submit twice with the same signed transaction is safe: one send, the same signature", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const message = Buffer.from(built.message_b64, "base64");
  const sig = sign(e.user, message);
  const [a, b]: any[] = await Promise.all([submitMsg(e, message, sig), submitMsg(e, message, sig)]); // even concurrently
  const c: any = await submitMsg(e, message, sig);
  assert.equal(e.s.rpc.sent.length, 1);
  assert.equal(a.signature, b.signature);
  assert.equal(c.signature, a.signature);
  assert.equal(c.already, true);
});

test("a failed attempt can be retried; send errors are mapped", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const message = Buffer.from(built.message_b64, "base64");
  const sig = sign(e.user, message);
  e.s.rpc.sendError = new Error("socket hang up");
  assert.equal(await code(submitMsg(e, message, sig)), "SUBMIT_FAILED");
  e.s.rpc.sendError = null;
  assert.equal(((await submitMsg(e, message, sig)) as any).ok, true);
  const f = await env();
  const fb: any = await f.svc.build(f.args);
  const fm = Buffer.from(fb.message_b64, "base64");
  f.s.rpc.sendError = Object.assign(new Error("x"), { logs: ["Program log: AnchorError thrown in x.rs:1. Error Code: MarketFull. Error Number: 6002. Error Message: m."] });
  assert.equal(await code(submitMsg(f, fm, sign(f.user, fm))), "MARKET_FULL");
  const g = await env();
  const gb: any = await g.svc.build(g.args);
  const gm = Buffer.from(gb.message_b64, "base64");
  g.s.rpc.sendError = new Error("Blockhash not found");
  assert.equal(await code(submitMsg(g, gm, sign(g.user, gm))), "TX_EXPIRED");
});

// ---------------- submit: verification matrix (the payer must never sign, nothing may be sent) ----------------

async function expectReject(e: Awaited<ReturnType<typeof env>>, message: Uint8Array, sig: Uint8Array, why: RegExp, args = e.args) {
  const r = await reason(submitMsg(e, message, sig, args));
  assert.match(r, why);
  assert.equal(e.s.rpc.sent.length, 0, "nothing was sent");
  assert.equal(e.s.rpc.simulated.length, 0, "not even simulated");
}

test("tampered amount: a message for another amount than the request is rejected", async () => {
  const e = await env();
  const message = legacy(e.s.cfg.payer.publicKey, [await realIx(e, { amount: 15n * USDC })], e.s.rpc.blockhash); // 15 instead of 5
  await expectReject(e, message, sign(e.user, message), /does not match the requested deposit/);
  const built: any = await e.svc.build(e.args); // honest message, request claims a different amount
  const m2 = Buffer.from(built.message_b64, "base64");
  await expectReject(e, m2, sign(e.user, m2), /does not match the requested deposit/, { ...e.args, amount_base: Number(6n * USDC) });
});

test("tampered side", async () => {
  const e = await env();
  const message = legacy(e.s.cfg.payer.publicKey, [await realIx(e, { side: 0 })], e.s.rpc.blockhash);
  await expectReject(e, message, sign(e.user, message), /does not match the requested deposit/);
});

test("wrong program id", async () => {
  const e = await env();
  const ix = await realIx(e);
  const evil = new TransactionInstruction({ programId: Keypair.generate().publicKey, keys: ix.keys, data: ix.data });
  const message = legacy(e.s.cfg.payer.publicKey, [evil], e.s.rpc.blockhash);
  await expectReject(e, message, sign(e.user, message), /not for the pool program/);
});

test("an instruction of the pool program that is not a deposit", async () => {
  const e = await env();
  const lock = await e.s.pool.lockIx(e.user.publicKey, e.s.pool.betPda(BET_ID));
  const message = legacy(e.s.cfg.payer.publicKey, [lock], e.s.rpc.blockhash);
  await expectReject(e, message, sign(e.user, message), /two signers|not a deposit/);
});

for (const [name, extra] of [
  ["compute budget", () => ComputeBudgetProgram.setComputeUnitLimit({ units: 400_000 })],
  ["token transfer", (e: Awaited<ReturnType<typeof env>>) => createTransferCheckedInstruction(e.ata, e.s.cfg.mint, Keypair.generate().publicKey, e.user.publicKey, 1n, 6)],
  ["close account", (e: Awaited<ReturnType<typeof env>>) => createCloseAccountInstruction(e.ata, Keypair.generate().publicKey, e.user.publicKey)],
  ["create associated token account", (e: Awaited<ReturnType<typeof env>>) => createAssociatedTokenAccountInstruction(e.s.cfg.payer.publicKey, Keypair.generate().publicKey, Keypair.generate().publicKey, e.s.cfg.mint)],
] as const) {
  test(`extra instruction: ${name}`, async () => {
    const e = await env();
    for (const order of ["after", "before"]) {
      const ixs = order === "after" ? [await realIx(e), (extra as any)(e)] : [(extra as any)(e), await realIx(e)];
      const message = legacy(e.s.cfg.payer.publicKey, ixs, e.s.rpc.blockhash);
      await expectReject(e, message, sign(e.user, message), /exactly one instruction|signers/);
    }
  });
}

test("missing user signature (signed transaction shape and message shape)", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  assert.match(await reason(e.svc.submit({ ...e.args, signed_tx_b64: built.transaction_b64 })), /missing wallet signature/);
  const message = Buffer.from(built.message_b64, "base64");
  assert.match(await reason(submitMsg(e, message, Buffer.alloc(64))), /signature does not match/);
  assert.match(await reason(submitMsg(e, message, Buffer.alloc(10))), /64 bytes/);
  assert.equal(e.s.rpc.sent.length, 0);
});

test("user signature over a different message", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const message = Buffer.from(built.message_b64, "base64");
  const other = Buffer.from(message); other[other.length - 1] = (other[other.length - 1] ?? 0) ^ 1;
  await expectReject(e, message, sign(e.user, other), /signature does not match/);
  await expectReject(e, message, sign(Keypair.generate(), message), /signature does not match/); // someone else's signature
});

test("a payer signature supplied by the client is refused (the sponsor signs, nobody else)", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const wire = Buffer.from(built.transaction_b64, "base64");
  const message = Buffer.from(built.message_b64, "base64");
  sign(e.user, message).copy(wire, 1 + 64);
  Buffer.alloc(64, 7).copy(wire, 1); // junk in the payer slot
  assert.match(await reason(e.svc.submit({ ...e.args, signed_tx_b64: b64(wire) })), /payer signature must be left empty/);
  assert.equal(e.s.rpc.sent.length, 0);
});

test("wrong fee payer: the user pays instead of the sponsor", async () => {
  const e = await env();
  const message = legacy(e.user.publicKey, [await realIx(e)], e.s.rpc.blockhash);
  await expectReject(e, message, sign(e.user, message), /sponsor must be the fee payer|two signers/);
});

test("extra signer", async () => {
  const e = await env();
  const ix = await realIx(e);
  const extra = Keypair.generate().publicKey;
  const evil = new TransactionInstruction({ programId: ix.programId, data: ix.data, keys: [...ix.keys, { pubkey: extra, isSigner: true, isWritable: false }] });
  const message = legacy(e.s.cfg.payer.publicKey, [evil], e.s.rpc.blockhash);
  await expectReject(e, message, sign(e.user, message), /two signers/);
});

test("versioned transaction with an address lookup table is refused (both shapes)", async () => {
  const e = await env();
  const table = new AddressLookupTableAccount({
    key: Keypair.generate().publicKey,
    state: { deactivationSlot: BigInt("18446744073709551615"), lastExtendedSlot: 0, lastExtendedSlotStartIndex: 0, addresses: [e.s.cfg.mint, e.s.pool.betPda(BET_ID)] },
  });
  const v0 = new TransactionMessage({ payerKey: e.s.cfg.payer.publicKey, recentBlockhash: e.s.rpc.blockhash, instructions: [await realIx(e)] }).compileToV0Message([table]);
  const vtx = new VersionedTransaction(v0);
  vtx.signatures[1] = sign(e.user, v0.serialize());
  assert.match(await reason(e.svc.submit({ ...e.args, signed_tx_b64: b64(vtx.serialize()) })), /only legacy/);
  assert.match(await reason(e.svc.submit({ ...e.args, message_b64: b64(v0.serialize()), user_signature_b64: b64(sign(e.user, v0.serialize())) })), /only legacy/);
  assert.equal(e.s.rpc.sent.length, 0);
});

test("rebuilt message mismatch: one account swapped for another", async () => {
  const e = await env();
  const ix = await realIx(e);
  const keys = ix.keys.map((k, i) => (i === 6 ? { ...k, pubkey: Keypair.generate().publicKey } : k)); // the depositor token account
  const message = legacy(e.s.cfg.payer.publicKey, [new TransactionInstruction({ programId: ix.programId, data: ix.data, keys })], e.s.rpc.blockhash);
  await expectReject(e, message, sign(e.user, message), /does not match the requested deposit/);
});

test("garbage input", async () => {
  const e = await env();
  assert.equal(await code(e.svc.submit({ ...e.args })), "BAD_REQUEST");
  assert.equal(await code(e.svc.submit({ ...e.args, signed_tx_b64: "AAAA" })), "TX_REJECTED");
  assert.equal(await code(e.svc.submit({ ...e.args, message_b64: "AAAA", user_signature_b64: b64(Buffer.alloc(64)) })), "TX_REJECTED");
  assert.equal(e.s.rpc.sent.length, 0);
});

// ---------------- submit: live checks ----------------

test("expired blockhash is TX_EXPIRED and nothing is signed or sent", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const message = Buffer.from(built.message_b64, "base64");
  e.s.rpc.validBlockhashes.clear();
  assert.equal(await code(submitMsg(e, message, sign(e.user, message))), "TX_EXPIRED");
  assert.equal(e.s.rpc.sent.length, 0);
});

test("the market is re-checked at submit: not open, funding cutoff, limits", async () => {
  for (const [what, mutate, want] of [
    ["locked", async (e: any) => { await putBet(e.s, { state: "locked" }); }, "MARKET_NOT_OPEN"],
    ["cutoff", async (e: any) => { e.s.rpc.clock = 1_900_000_000 - 10; }, "FUNDING_CLOSED"],
    ["became full", async (e: any) => { await putBet(e.s, { state: "open", participantCount: 20 }); }, "MARKET_FULL"],
    ["other side", async (e: any) => { await putPosition(e.s, e.user.publicKey, 0, 5n * USDC); }, "ALREADY_OTHER_SIDE"],
    ["above max", async (e: any) => { await putPosition(e.s, e.user.publicKey, 1, 17n * USDC); }, "ABOVE_MAX"],
    ["spent the usdc", async (e: any) => { e.s.rpc.tokens.set(e.ata.toBase58(), 1n); }, "INSUFFICIENT_USDC"],
    ["token account closed", async (e: any) => { e.s.rpc.accounts.delete(e.ata.toBase58()); }, "NO_TOKEN_ACCOUNT"],
  ] as const) {
    const e = await env();
    const built: any = await e.svc.build(e.args);
    const message = Buffer.from(built.message_b64, "base64");
    const sig = sign(e.user, message);
    await mutate(e);
    assert.equal(await code(submitMsg(e, message, sig)), want, what);
    assert.equal(e.s.rpc.sent.length, 0, what);
  }
});

test("simulation failures are mapped and nothing is sent", async () => {
  const e = await env();
  const built: any = await e.svc.build(e.args);
  const message = Buffer.from(built.message_b64, "base64");
  const sig = sign(e.user, message);
  e.s.rpc.simulation = { err: { InstructionError: [0, { Custom: 6000 }] }, logs: ["Program log: AnchorError thrown in x.rs:1. Error Code: BelowMin. Error Number: 6000. Error Message: m."] };
  assert.equal(await code(submitMsg(e, message, sig)), "BELOW_MIN");
  e.s.rpc.simulation = { err: "weird", logs: [] };
  assert.equal(await code(submitMsg(e, message, sig)), "TX_REJECTED");
  assert.equal(e.s.rpc.sent.length, 0);
});

// ---------------- rate limits ----------------

test("rate limit per wallet and per bet", async () => {
  const e = await env({ depositRatePerWalletPerMin: 3, depositRatePerBetPerMin: 100 });
  for (let i = 0; i < 3; i++) assert.equal(await code(e.svc.build(e.args)), "no error");
  assert.equal(await code(e.svc.build(e.args)), "RATE_LIMITED");
  const other = { ...e.args, wallet: Keypair.generate().publicKey.toBase58() };
  assert.notEqual(await code(e.svc.build(other)), "RATE_LIMITED"); // another wallet is unaffected
  const f = await env({ depositRatePerWalletPerMin: 100, depositRatePerBetPerMin: 2 });
  await f.svc.build(f.args); await f.svc.build(f.args);
  assert.equal(await code(f.svc.build({ ...f.args, wallet: Keypair.generate().publicKey.toBase58() })), "RATE_LIMITED");
});
