import anchor from "@anchor-lang/core";
import { Keypair, PublicKey, VersionedTransaction, type GetProgramAccountsFilter } from "@solana/web3.js";
import { ChainService } from "../src/chain.ts";
import type { ChainConfig } from "../src/config.ts";
import { createLogger } from "../src/logger.ts";
import { POSITION_SIZE, Pool } from "../src/program.ts";
import type { AccountData, Rpc, TxOutcome } from "../src/rpc.ts";
import idl from "../idl/handshake_pool.json" with { type: "json" };

const { BN } = anchor;

export const BET_ID = "0b6f3c2e-5d1a-4c7e-9a41-2f8d6e7b1a90";
export const USDC = 1_000_000n;

export function testConfig(over: Partial<ChainConfig> = {}): ChainConfig {
  return {
    rpcUrl: "http://fake", programId: Keypair.generate().publicKey, mint: Keypair.generate().publicKey,
    payer: Keypair.generate(), oracle: Keypair.generate(), treasury: Keypair.generate().publicKey,
    secret: "test-secret-0123456789", port: 0, logLevel: "debug", ...over,
  };
}

export class FakeRpc implements Rpc {
  accounts = new Map<string, AccountData>();
  tokens = new Map<string, bigint>();
  sent: VersionedTransaction[] = [];
  sendError: (Error & { logs?: string[] }) | null = null;
  onSend: ((tx: VersionedTransaction) => unknown) | null = null;
  confirmResult: TxOutcome = { err: null, logs: [] };
  balances = new Map<string, number>();
  constructor(public programId: PublicKey) {}
  async getGenesisHash() { return "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"; }
  async getAccount(pk: PublicKey) { return this.accounts.get(pk.toBase58()) ?? null; }
  async getProgramAccounts(programId: PublicKey, filters: GetProgramAccountsFilter[]) {
    return [...this.accounts.entries()]
      .filter(([, a]) => a.owner.equals(programId))
      .filter(([, a]) => filters.every((f: any) => {
        if (f.dataSize !== undefined) return a.data.length === f.dataSize;
        const want = new PublicKey(f.memcmp.bytes).toBuffer();
        return a.data.subarray(f.memcmp.offset, f.memcmp.offset + want.length).equals(want);
      }))
      .map(([k, a]) => ({ pubkey: new PublicKey(k), data: a.data }));
  }
  async getTokenBalance(pk: PublicKey) { return this.tokens.get(pk.toBase58()) ?? 0n; }
  async getBalance(pk: PublicKey) { return this.balances.get(pk.toBase58()) ?? 1_000_000_000; }
  async getLatestBlockhash() { return "11111111111111111111111111111111"; }
  async sendRaw(bytes: Uint8Array) {
    const tx = VersionedTransaction.deserialize(bytes);
    await this.onSend?.(tx); // lets a test change the chain "during" the send (a race, or the effect of the tx)
    if (this.sendError) throw this.sendError;
    this.sent.push(tx);
    return "sig" + this.sent.length;
  }
  async confirm(_sig: string) { return this.confirmResult; }
}

export function setup(over: Partial<ChainConfig> = {}) {
  const cfg = testConfig(over);
  const rpc = new FakeRpc(cfg.programId);
  const lines: string[] = [];
  const logger = createLogger({ level: "debug", write: (l) => lines.push(l) });
  const chain = new ChainService({ rpc, cfg, logger });
  return { cfg, rpc, chain, pool: chain.pool, lines };
}

export type BetOver = Partial<{
  authority: PublicKey; payer: PublicKey; treasury: PublicKey; mint: PublicKey; state: string; winningSide: number;
  sideTotals: [bigint, bigint]; participantCount: number; positionsOpen: number; feeBps: number; feePaid: bigint; distributable: bigint;
}>;

export async function putBet(s: ReturnType<typeof setup>, over: BetOver = {}, betId = BET_ID) {
  const pda = s.pool.betPda(betId);
  const st = over.state ?? "open";
  const data = await s.pool.program.coder.accounts.encode("bet", {
    betId: Array.from(Buffer.from(betId.replace(/-/g, ""), "hex")),
    authority: over.authority ?? s.cfg.oracle.publicKey, payer: over.payer ?? s.cfg.payer.publicKey,
    treasury: over.treasury ?? s.cfg.treasury, mint: over.mint ?? s.cfg.mint,
    feeBps: over.feeBps ?? 500, minStake: new BN(1_000_000), maxStake: new BN(20_000_000), maxParticipants: 20,
    participantCount: over.participantCount ?? 2, positionsOpen: over.positionsOpen ?? 2,
    sideTotals: (over.sideTotals ?? [10_000_000n, 20_000_000n]).map((x) => new BN(x.toString())),
    state: { [st]: {} }, winningSide: over.winningSide ?? 255,
    feePaid: new BN((over.feePaid ?? 0n).toString()), distributable: new BN((over.distributable ?? 0n).toString()),
    fundingClosesAt: new BN(1_900_000_000), refundAfter: new BN(1_900_040_000), createdAt: new BN(1_899_990_000), bump: 255, vaultBump: 254,
  });
  s.rpc.accounts.set(pda.toBase58(), { data: Buffer.from(data), lamports: 3_000_000, owner: s.cfg.programId });
  return pda;
}

export async function putPosition(s: ReturnType<typeof setup>, owner: PublicKey, side: number, amount: bigint, betId = BET_ID) {
  const bet = s.pool.betPda(betId);
  const key = s.pool.posPda(bet, owner);
  const data = await s.pool.program.coder.accounts.encode("position", { bet, owner, side, amount: new BN(amount.toString()), bump: 253 });
  s.rpc.accounts.set(key.toBase58(), { data: Buffer.from(data), lamports: 1_500_000, owner: s.cfg.programId });
  return key;
}

/** instruction names (from the IDL discriminators) of a sent transaction, in order */
export function instructionNames(tx: VersionedTransaction, programId: PublicKey): string[] {
  const keys = tx.message.staticAccountKeys;
  const out: string[] = [];
  for (const ci of tx.message.compiledInstructions) {
    const pid = keys[ci.programIdIndex]!;
    if (!pid.equals(programId)) { out.push(pid.toBase58()); continue; }
    const disc = [...ci.data.subarray(0, 8)];
    const found = (idl as any).instructions.find((i: any) => JSON.stringify(i.discriminator) === JSON.stringify(disc));
    out.push(found?.name ?? "?");
  }
  return out;
}

/** accounts of the instruction at `index`, with signer/writable flags resolved from the message header */
export function ixAccounts(tx: VersionedTransaction, index: number) {
  const m = tx.message;
  const keys = m.staticAccountKeys;
  const h = m.header;
  const isSigner = (i: number) => i < h.numRequiredSignatures;
  const isWritable = (i: number) =>
    i < h.numRequiredSignatures - h.numReadonlySignedAccounts ||
    (i >= h.numRequiredSignatures && i < keys.length - h.numReadonlyUnsignedAccounts);
  return m.compiledInstructions[index]!.accountKeyIndexes.map((i) => ({ key: keys[i]!, signer: isSigner(i), writable: isWritable(i) }));
}

export const signersOf = (tx: VersionedTransaction) => tx.message.staticAccountKeys.slice(0, tx.message.header.numRequiredSignatures);
