import assert from "node:assert/strict";
import { test } from "node:test";
import { Keypair, PublicKey } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Pool, uuidBytes } from "../src/program.ts";
import { BET_ID } from "./helpers.ts";

const PROGRAM = new PublicKey("38PFJW13NmRcfKqCj5DgSkwPgCbP68itZQm9nz7eCFAE");

test("uuidBytes: 16 raw bytes, rejects non uuids", () => {
  assert.equal(uuidBytes(BET_ID).toString("hex"), BET_ID.replace(/-/g, ""));
  assert.equal(uuidBytes(BET_ID).length, 16);
  for (const bad of ["", "nope", BET_ID + "0", BET_ID.replace(/-/g, "")]) assert.throws(() => uuidBytes(bad), /UUID/);
});

test("PDAs use the contract seeds: bet, vault, position", () => {
  const pool = new Pool(PROGRAM);
  const bet = pool.betPda(BET_ID);
  assert.equal(bet.toBase58(), PublicKey.findProgramAddressSync([Buffer.from("bet"), Buffer.from(BET_ID.replace(/-/g, ""), "hex")], PROGRAM)[0].toBase58());
  const vault = pool.vaultPda(bet);
  assert.equal(vault.toBase58(), PublicKey.findProgramAddressSync([Buffer.from("vault"), bet.toBuffer()], PROGRAM)[0].toBase58());
  const owner = Keypair.generate().publicKey;
  assert.equal(pool.posPda(bet, owner).toBase58(), PublicKey.findProgramAddressSync([Buffer.from("position"), bet.toBuffer(), owner.toBuffer()], PROGRAM)[0].toBase58());
});

test("PDA derivation vector (fixed program id, fixed bet id)", () => {
  const pool = new Pool(PROGRAM);
  const bet = pool.betPda(BET_ID);
  assert.equal(bet.toBase58(), "E39hFd9sBGgCB4u8qTHRw5JA8vYVzhhAG4FbKXem651L");
  assert.equal(pool.vaultPda(bet).toBase58(), "GhUKGyxpEehEFMLMRdcNEH2XFcP87TQA8i6BtLY1q2kV");
});

test("instruction accounts and signer flags per instruction", async () => {
  const pool = new Pool(PROGRAM);
  const [payer, oracle, owner, treasury, mint] = Array.from({ length: 5 }, () => Keypair.generate().publicKey);
  const bet = pool.betPda(BET_ID);
  const tAta = getAssociatedTokenAddressSync(mint!, treasury!, true);
  const flags = (ix: { keys: { pubkey: PublicKey; isSigner: boolean; isWritable: boolean }[] }) => ix.keys.map((k: { isSigner: boolean; isWritable: boolean; pubkey: PublicKey }) => `${k.isSigner ? "S" : "-"}${k.isWritable ? "W" : "-"}`).join(" ");

  const create = await pool.createBetIx({ payer: payer!, betUuid: BET_ID, authority: oracle!, treasury: treasury!, mint: mint!, feeBps: 500, minStake: 1n, maxStake: 2n, maxParticipants: 20, fundingClosesAt: 1, refundAfter: 2 });
  assert.equal(flags(create), "SW -W -- -W -- --"); // payer, bet, mint, vault, token program, system program
  assert.equal(create.keys[0]!.pubkey.toBase58(), payer!.toBase58());

  const deposit = await pool.depositIx({ depositor: owner!, rentPayer: payer!, bet, mint: mint!, side: 0, amount: 5n });
  const signers = deposit.keys.filter((k: { isSigner: boolean; pubkey: PublicKey }) => k.isSigner).map((k: { isSigner: boolean; pubkey: PublicKey }) => k.pubkey.toBase58()).sort();
  assert.deepEqual(signers, [owner!.toBase58(), payer!.toBase58()].sort(), "deposit needs the user AND the payer");

  const lock = await pool.lockIx(oracle!, bet);
  assert.equal(flags(lock), "S- -W");
  const refund = await pool.refundAllIx(oracle!, bet);
  assert.equal(flags(refund), "S- -W");

  const settle = await pool.settleIx({ authority: oracle!, bet, mint: mint!, treasuryToken: tAta, winningSide: 1 });
  assert.equal(settle.keys[0]!.isSigner, true);
  assert.equal(settle.keys.filter((k: { isSigner: boolean; pubkey: PublicKey }) => k.isSigner).length, 1);
  assert.equal(settle.keys[4]!.pubkey.toBase58(), tAta.toBase58());

  const claim = await pool.claimIx({ signer: payer!, bet, mint: mint!, owner: owner!, rentReceiver: payer! });
  assert.equal(claim.keys.filter((k: { isSigner: boolean; pubkey: PublicKey }) => k.isSigner).length, 1);
  assert.equal(claim.keys[0]!.pubkey.toBase58(), payer!.toBase58());
  assert.equal(claim.keys[5]!.pubkey.toBase58(), getAssociatedTokenAddressSync(mint!, owner!, true).toBase58());
  assert.equal(claim.keys[6]!.pubkey.toBase58(), payer!.toBase58()); // rent receiver

  const closeLoser = await pool.closeLoserIx({ signer: payer!, bet, owner: owner!, rentReceiver: payer! });
  assert.equal(closeLoser.keys.filter((k: { isSigner: boolean; pubkey: PublicKey }) => k.isSigner).length, 1);

  const closeBet = await pool.closeBetIx({ authority: oracle!, bet, payer: payer!, mint: mint!, treasuryToken: tAta });
  assert.equal(closeBet.keys[0]!.isSigner, true);
  assert.equal(closeBet.keys[2]!.pubkey.toBase58(), payer!.toBase58());
  assert.equal(TOKEN_PROGRAM_ID.toBase58(), closeBet.keys[6]!.pubkey.toBase58());
});
