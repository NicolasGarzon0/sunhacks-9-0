import * as fs from "fs";
import * as path from "path";
import { AnchorProvider, BN, EventParser, Program } from "@anchor-lang/core";
import {
  Connection, Keypair, PublicKey, SystemProgram, Transaction, TransactionInstruction,
} from "@solana/web3.js";
import {
  AccountLayout, MINT_SIZE, TOKEN_PROGRAM_ID, createAssociatedTokenAccountIdempotentInstruction,
  createInitializeMint2Instruction, createMintToInstruction, getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { FailedTransactionMetadata, LiteSVM } from "litesvm";
import { getTransactionDecoder } from "@solana/kit";

export const USDC = 1_000_000n;
export const NOW = 1_800_000_000; // fixed test clock
export const HOUR = 3600;
const ROOT = path.resolve(__dirname, "..");
export const IDL = JSON.parse(fs.readFileSync(path.join(ROOT, "target/idl/handshake_pool.json"), "utf8"));
export const PROGRAM_ID = new PublicKey(IDL.address);

export const stateOf = (bet: any): string => Object.keys(bet.state)[0];
export const bn = (x: bigint | number) => new BN(x.toString());
export const big = (x: any): bigint => BigInt(x.toString());

export type Sent = { ok: boolean; logs: string[]; error?: string; code?: string; events: { name: string; data: any }[] };
export type BetRef = { bet: PublicKey; vault: PublicKey; betId: Buffer };
export type CreateOpts = Partial<{
  betId: Buffer; feeBps: number; min: bigint; max: bigint; maxP: number; closesAt: number; refundAfter: number; tokenProgram: PublicKey;
  authority: PublicKey; treasuryOwner: PublicKey; mint: PublicKey;
}>;

export class Env {
  svm = new LiteSVM();
  program: Program;
  payer = Keypair.generate(); // the sponsor: pays rent and fees, stored as bet.payer
  authority = Keypair.generate(); // the oracle
  mintAuth = Keypair.generate();
  mintKp = Keypair.generate();
  mint = this.mintKp.publicKey;
  treasuryOwner = Keypair.generate();
  treasuryAta: PublicKey;
  anyone = Keypair.generate(); // a stranger with zero SOL, used to prove anyone can submit claims
  relayer = Keypair.generate(); // optional separate fee payer so the sponsor's lamports only move by rent
  feePayer: Keypair = this.payer;
  counter = 0;

  constructor() {
    const wallet: any = { publicKey: this.payer.publicKey, signTransaction: async (t: any) => t, signAllTransactions: async (t: any) => t };
    this.program = new Program(IDL, new AnchorProvider(new Connection("http://127.0.0.1:1"), wallet, {}));
    this.svm.addProgramFromFile(PROGRAM_ID.toBase58() as any, path.join(ROOT, "target/deploy/handshake_pool.so"));
    this.setNow(NOW);
    this.svm.airdrop(this.payer.publicKey.toBase58() as any, 1_000n * 1_000_000_000n);
    this.svm.airdrop(this.authority.publicKey.toBase58() as any, 10n * 1_000_000_000n);
    this.svm.airdrop(this.relayer.publicKey.toBase58() as any, 100n * 1_000_000_000n);
    this.makeMint(this.mintKp, 6);
    this.treasuryAta = this.ata(this.treasuryOwner.publicKey);
  }

  // ---- plumbing ----
  setNow(ts: number) {
    const c = this.svm.getClock();
    c.unixTimestamp = BigInt(ts);
    this.svm.setClock(c);
  }
  now(): number { return Number(this.svm.getClock().unixTimestamp); }

  send(ixs: TransactionInstruction[], signers: Keypair[] = [], feePayer: Keypair = this.feePayer): Sent {
    this.svm.expireBlockhash();
    const tx = new Transaction();
    tx.recentBlockhash = this.svm.latestBlockhash();
    tx.feePayer = feePayer.publicKey;
    tx.add(...ixs);
    const all = [feePayer, ...signers.filter((s) => !s.publicKey.equals(feePayer.publicKey))];
    tx.sign(...all);
    const res = this.svm.sendTransaction(getTransactionDecoder().decode(tx.serialize()));
    const failed = res instanceof FailedTransactionMetadata;
    const logs = (failed ? res.meta() : res).logs();
    const events = [...new EventParser(PROGRAM_ID, this.program.coder).parseLogs(logs)].map((e: any) => ({ name: e.name, data: e.data }));
    const out: Sent = { ok: !failed, logs, events };
    if (failed) {
      out.error = res.toString();
      out.code = logs.map((l) => /Error Code: (\w+)/.exec(l)?.[1]).filter(Boolean).pop();
    }
    return out;
  }

  // ---- tokens ----
  makeMint(kp: Keypair, decimals: number) {
    const rent = this.svm.minimumBalanceForRentExemption(BigInt(MINT_SIZE));
    const r = this.send(
      [
        SystemProgram.createAccount({ fromPubkey: this.payer.publicKey, newAccountPubkey: kp.publicKey, lamports: Number(rent), space: MINT_SIZE, programId: TOKEN_PROGRAM_ID }),
        createInitializeMint2Instruction(kp.publicKey, decimals, this.mintAuth.publicKey, null),
      ],
      [kp, this.payer],
    );
    if (!r.ok) throw new Error("makeMint failed " + r.error);
  }
  ata(owner: PublicKey, mint: PublicKey = this.mint): PublicKey {
    const a = getAssociatedTokenAddressSync(mint, owner, true);
    const r = this.send([createAssociatedTokenAccountIdempotentInstruction(this.payer.publicKey, a, owner, mint)], [this.payer]);
    if (!r.ok) throw new Error("ata failed " + r.error);
    return a;
  }
  /** A user wallet with a funded ATA and (by default) ZERO SOL: the sponsor pays for everything. */
  user(usdc: bigint = 100n, mint: PublicKey = this.mint): Keypair {
    const kp = Keypair.generate();
    this.fund(kp.publicKey, usdc, mint);
    return kp;
  }
  fund(owner: PublicKey, usdc: bigint, mint: PublicKey = this.mint): PublicKey {
    const a = this.ata(owner, mint);
    const r = this.send([createMintToInstruction(mint, a, this.mintAuth.publicKey, usdc * USDC)], [this.mintAuth]);
    if (!r.ok) throw new Error("mint failed " + r.error);
    return a;
  }
  tokenBal(account: PublicKey): bigint {
    const a = this.svm.getAccount(account.toBase58() as any);
    if (!a.exists) return 0n;
    return AccountLayout.decode(Buffer.from(a.data)).amount;
  }
  lamports(pk: PublicKey): bigint { return this.svm.getBalance(pk.toBase58() as any) ?? 0n; }
  exists(pk: PublicKey): boolean { return this.svm.getAccount(pk.toBase58() as any).exists; }

  // ---- pdas ----
  betPda(betId: Buffer) { return PublicKey.findProgramAddressSync([Buffer.from("bet"), betId], PROGRAM_ID)[0]; }
  vaultPda(bet: PublicKey) { return PublicKey.findProgramAddressSync([Buffer.from("vault"), bet.toBuffer()], PROGRAM_ID)[0]; }
  posPda(bet: PublicKey, owner: PublicKey) { return PublicKey.findProgramAddressSync([Buffer.from("position"), bet.toBuffer(), owner.toBuffer()], PROGRAM_ID)[0]; }
  newBetId(): Buffer { const b = Buffer.alloc(16); b.writeUInt32BE(++this.counter, 12); b.writeUInt32BE(Math.floor(Math.random() * 2 ** 31), 0); return b; }

  // ---- state readers ----
  bet(b: BetRef) { return this.program.coder.accounts.decode("bet", Buffer.from(this.svm.getAccount(b.bet.toBase58() as any).data as any)) as any; }
  pos(b: BetRef, owner: PublicKey) {
    const a = this.svm.getAccount(this.posPda(b.bet, owner).toBase58() as any);
    return a.exists ? (this.program.coder.accounts.decode("position", Buffer.from(a.data)) as any) : null;
  }

  // ---- instructions ----
  async createBetIx(o: CreateOpts = {}) {
    const betId = o.betId ?? this.newBetId();
    const bet = this.betPda(betId);
    const vault = this.vaultPda(bet);
    const ix = await this.program.methods
      .createBet(
        Array.from(betId), o.authority ?? this.authority.publicKey, o.treasuryOwner ?? this.treasuryOwner.publicKey,
        o.feeBps ?? 500, bn(o.min ?? 1n * USDC), bn(o.max ?? 20n * USDC), o.maxP ?? 20, new BN(o.closesAt ?? NOW + HOUR), new BN(o.refundAfter ?? (o.closesAt ?? NOW + HOUR) + 12 * HOUR),
      )
      .accountsStrict({ payer: this.payer.publicKey, bet, mint: o.mint ?? this.mint, vault, tokenProgram: o.tokenProgram ?? TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId })
      .instruction();
    return { ix, ref: { bet, vault, betId } as BetRef };
  }
  async createBet(o: CreateOpts = {}): Promise<{ ref: BetRef; res: Sent }> {
    const { ix, ref } = await this.createBetIx(o);
    return { ref, res: this.send([ix], [this.payer]) };
  }
  async open(o: CreateOpts = {}): Promise<BetRef> {
    const { ref, res } = await this.createBet(o);
    if (!res.ok) throw new Error("create failed " + res.error + res.logs.join("\n"));
    return ref;
  }

  async deposit(
    b: BetRef, user: Keypair, side: number, usdcBase: bigint,
    o: Partial<{ rentPayer: Keypair; mint: PublicKey; userToken: PublicKey; extraSigners: Keypair[] }> = {},
  ): Promise<Sent> {
    const rentPayer = o.rentPayer ?? this.payer;
    return this.send([await this.depositIx(b, user, side, usdcBase, o)], [user, rentPayer, ...(o.extraSigners ?? [])]);
  }
  async depositIx(
    b: BetRef, user: Keypair, side: number, usdcBase: bigint,
    o: Partial<{ rentPayer: Keypair; mint: PublicKey; userToken: PublicKey }> = {},
  ): Promise<TransactionInstruction> {
    const rentPayer = o.rentPayer ?? this.payer;
    return this.program.methods
      .deposit(side, bn(usdcBase))
      .accountsStrict({
        depositor: user.publicKey, rentPayer: rentPayer.publicKey, bet: b.bet, vault: b.vault, mint: o.mint ?? this.mint,
        position: this.posPda(b.bet, user.publicKey),
        depositorToken: o.userToken ?? getAssociatedTokenAddressSync(this.mint, user.publicKey),
        tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId,
      })
      .instruction();
  }
  async emergencyRefund(b: BetRef, signer: Keypair = this.anyone): Promise<Sent> {
    const ix = await this.program.methods.emergencyRefund().accountsStrict({ signer: signer.publicKey, bet: b.bet }).instruction();
    return this.send([ix], [signer]);
  }
  async lock(b: BetRef, signer: Keypair = this.authority): Promise<Sent> {
    const ix = await this.program.methods.lock().accountsStrict({ authority: signer.publicKey, bet: b.bet }).instruction();
    return this.send([ix], [signer]);
  }
  async refund(b: BetRef, signer: Keypair = this.authority): Promise<Sent> {
    const ix = await this.program.methods.refundAll().accountsStrict({ authority: signer.publicKey, bet: b.bet }).instruction();
    return this.send([ix], [signer]);
  }
  async settle(b: BetRef, side: number, o: Partial<{ signer: Keypair; treasuryToken: PublicKey; mint: PublicKey }> = {}): Promise<Sent> {
    const signer = o.signer ?? this.authority;
    const ix = await this.program.methods
      .settle(side)
      .accountsStrict({
        authority: signer.publicKey, bet: b.bet, vault: b.vault, mint: o.mint ?? this.mint,
        treasuryToken: o.treasuryToken ?? this.treasuryAta, tokenProgram: TOKEN_PROGRAM_ID,
      })
      .instruction();
    return this.send([ix], [signer]);
  }
  async claim(
    b: BetRef, owner: PublicKey,
    o: Partial<{ signer: Keypair; dest: PublicKey; rentReceiver: PublicKey }> = {},
  ): Promise<Sent> {
    return this.send([await this.claimIx(b, owner, o)], [o.signer ?? this.anyone]);
  }
  async claimIx(
    b: BetRef, owner: PublicKey,
    o: Partial<{ signer: Keypair; dest: PublicKey; rentReceiver: PublicKey }> = {},
  ): Promise<TransactionInstruction> {
    const signer = o.signer ?? this.anyone;
    return this.program.methods
      .claim()
      .accountsStrict({
        signer: signer.publicKey, bet: b.bet, vault: b.vault, mint: this.mint, position: this.posPda(b.bet, owner),
        ownerToken: o.dest ?? getAssociatedTokenAddressSync(this.mint, owner), rentReceiver: o.rentReceiver ?? this.payer.publicKey,
        tokenProgram: TOKEN_PROGRAM_ID,
      })
      .instruction();
  }
  async closeLoser(b: BetRef, owner: PublicKey, o: Partial<{ signer: Keypair; rentReceiver: PublicKey }> = {}): Promise<Sent> {
    const signer = o.signer ?? this.anyone;
    const ix = await this.program.methods
      .closeLoserPosition()
      .accountsStrict({ signer: signer.publicKey, bet: b.bet, position: this.posPda(b.bet, owner), rentReceiver: o.rentReceiver ?? this.payer.publicKey })
      .instruction();
    return this.send([ix], [signer]);
  }
  async closeBet(b: BetRef, o: Partial<{ signer: Keypair; payer: PublicKey; treasuryToken: PublicKey }> = {}): Promise<Sent> {
    const signer = o.signer ?? this.authority;
    const ix = await this.program.methods
      .closeBet()
      .accountsStrict({
        authority: signer.publicKey, bet: b.bet, payer: o.payer ?? this.payer.publicKey, vault: b.vault, mint: this.mint,
        treasuryToken: o.treasuryToken ?? this.treasuryAta, tokenProgram: TOKEN_PROGRAM_ID,
      })
      .instruction();
    return this.send([ix], [signer]);
  }

  /** Open market with the common fixture: bet with `deposits` = [userIndex, side, usdc]. Returns users too. */
  async market(deposits: [number, number, bigint][], o: CreateOpts = {}) {
    const ref = await this.open(o);
    const users: Keypair[] = [];
    for (const [i, side, amt] of deposits) {
      users[i] ??= this.user(1000n);
      const r = await this.deposit(ref, users[i], side, amt);
      if (!r.ok) throw new Error(`fixture deposit failed: ${r.code} ${r.error}`);
    }
    return { ref, users };
  }
}

import { expect } from "chai";
export function expectOk(r: Sent) { expect(r.ok, `expected success but got ${r.code}\n${r.logs.join("\n")}`).to.equal(true); }
export function expectErr(r: Sent, code: string) {
  expect(r.ok, "expected failure " + code).to.equal(false);
  expect(r.code, `wrong error (${r.error})\n${r.logs.join("\n")}`).to.equal(code);
}
