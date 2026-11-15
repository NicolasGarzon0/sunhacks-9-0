import assert from "node:assert/strict";
import { test } from "node:test";
import { Keypair } from "@solana/web3.js";
import { ChainError } from "../src/errors.ts";
import { WalletService } from "../src/wallet.ts";
import { instructionNames, ixAccounts, signersOf, setup } from "./helpers.ts";

const quiet = { debug() {}, info() {}, warn() {}, error() {} };
const code = async (p: Promise<unknown>) => { try { await p; return "no error"; } catch (e) { return (e as ChainError).code; } };

function make(over: Parameters<typeof setup>[0] = {}) {
  const s = setup(over);
  return { s, w: new WalletService({ rpc: s.rpc, cfg: s.cfg, chain: s.chain, logger: quiet }) };
}

test("wallet: token account, USDC balance and SOL", async () => {
  const { s, w } = make();
  const user = Keypair.generate().publicKey;
  let r: any = await w.wallet(user.toBase58());
  assert.deepEqual([r.has_token_account, r.usdc_base, r.sol_lamports], [false, 0, 1_000_000_000]);
  const ata = s.pool.ata(user, s.cfg.mint);
  s.rpc.accounts.set(ata.toBase58(), { data: Buffer.alloc(165), lamports: 1, owner: Keypair.generate().publicKey });
  s.rpc.tokens.set(ata.toBase58(), 25_000_000n);
  s.rpc.balances.set(user.toBase58(), 0);
  r = await w.wallet(user.toBase58());
  assert.deepEqual([r.has_token_account, r.usdc_base, r.sol_lamports], [true, 25_000_000, 0]);
  assert.equal(await code(w.wallet("nope")), "BAD_REQUEST");
});

test("airdrop: idempotent token account creation, then mint; payer and mint authority sign; nothing is ever closed", async () => {
  const { s, w } = make();
  const user = Keypair.generate().publicKey;
  const r: any = await w.airdrop(user.toBase58(), 25_000_000);
  assert.equal(r.amount_base, 25_000_000);
  assert.match(r.explorer, /cluster=devnet/);
  const tx = s.rpc.sent[0]!;
  const names = instructionNames(tx, s.cfg.programId);
  assert.equal(names.length, 2);
  assert.deepEqual(signersOf(tx).map((k) => k.toBase58()).sort(), [s.cfg.payer.publicKey.toBase58(), s.cfg.mintAuthority!.publicKey.toBase58()].sort());
  const mint = ixAccounts(tx, 1);
  assert.equal(mint[0]!.key.toBase58(), s.cfg.mint.toBase58()); // mintToChecked: mint, destination, authority
  assert.equal(mint[1]!.key.toBase58(), s.pool.ata(user, s.cfg.mint).toBase58());
  assert.equal(mint[2]!.key.toBase58(), s.cfg.mintAuthority!.publicKey.toBase58());
  for (let i = 0; i < 2; i++) assert.ok(!tx.message.compiledInstructions.some((ci) => ci.data[0] === 9), "no CloseAccount instruction");
});

test("airdrop: per call cap, bad input, disabled without a mint authority", async () => {
  const { s, w } = make({ airdropMaxPerCallBase: 50_000_000n });
  const user = Keypair.generate().publicKey.toBase58();
  assert.equal(await code(w.airdrop(user, 50_000_001)), "INVALID_PARAMS");
  assert.equal(await code(w.airdrop(user, 0)), "INVALID_PARAMS");
  assert.equal(await code(w.airdrop(user, 1.5)), "INVALID_PARAMS");
  assert.equal(await code(w.airdrop(user, "5")), "INVALID_PARAMS");
  assert.equal(await code(w.airdrop("nope", 5)), "BAD_REQUEST");
  assert.equal(await code(w.airdrop(user, 50_000_000)), "no error");
  assert.equal(s.rpc.sent.length, 1);
  const off = make({ mintAuthority: null });
  assert.equal(await code(off.w.airdrop(user, 5_000_000)), "AIRDROP_DISABLED");
  assert.equal(off.s.rpc.sent.length, 0);
});
