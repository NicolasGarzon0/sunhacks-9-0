// Everything that knows the handshake_pool layout: PDAs, account decoding, instruction builders.
import anchor from "@anchor-lang/core";
import { Connection, Keypair, PublicKey, SystemProgram, type TransactionInstruction } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID, createAssociatedTokenAccountIdempotentInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import idlJson from "../idl/handshake_pool.json" with { type: "json" };
import { ChainError } from "./errors.ts";

const { Program, AnchorProvider, BN } = anchor;

export const POSITION_SIZE = 82; // 8 discriminator + bet 32 + owner 32 + side 1 + amount 8 + bump 1
export const NO_WINNER = 255;
export type BetState = "open" | "locked" | "settled" | "refunded";

export interface BetData {
  betId: Buffer;
  authority: PublicKey;
  payer: PublicKey;
  treasury: PublicKey;
  mint: PublicKey;
  feeBps: number;
  minStake: bigint;
  maxStake: bigint;
  maxParticipants: number;
  participantCount: number;
  positionsOpen: number;
  sideTotals: [bigint, bigint];
  state: BetState;
  winningSide: number | null;
  feePaid: bigint;
  distributable: bigint;
  fundingClosesAt: number;
  refundAfter: number;
  createdAt: number;
}
export interface PositionData { bet: PublicKey; owner: PublicKey; side: number; amount: bigint }

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function uuidBytes(uuid: string): Buffer {
  if (!UUID_RE.test(uuid)) throw new ChainError("BAD_REQUEST", "bet_id must be a UUID");
  return Buffer.from(uuid.replace(/-/g, ""), "hex");
}

const big = (x: { toString(): string }) => BigInt(x.toString());

export class Pool {
  // typed loosely on purpose: the IDL is loaded at runtime
  readonly program: any;
  constructor(public readonly programId: PublicKey, connection?: Connection) {
    const wallet: any = { publicKey: PublicKey.default, signTransaction: async (t: any) => t, signAllTransactions: async (t: any) => t };
    // the connection is never used to send: this Program only builds instructions and decodes accounts
    const conn = connection ?? new Connection("http://127.0.0.1:1");
    this.program = new Program({ ...(idlJson as any), address: programId.toBase58() }, new AnchorProvider(conn, wallet, {}));
  }

  betPda(betUuid: string): PublicKey {
    return PublicKey.findProgramAddressSync([Buffer.from("bet"), uuidBytes(betUuid)], this.programId)[0];
  }
  vaultPda(bet: PublicKey): PublicKey {
    return PublicKey.findProgramAddressSync([Buffer.from("vault"), bet.toBuffer()], this.programId)[0];
  }
  posPda(bet: PublicKey, owner: PublicKey): PublicKey {
    return PublicKey.findProgramAddressSync([Buffer.from("position"), bet.toBuffer(), owner.toBuffer()], this.programId)[0];
  }
  get positionDiscriminator(): Buffer {
    const d = (idlJson as any).accounts.find((a: any) => a.name === "Position").discriminator as number[];
    return Buffer.from(d);
  }

  decodeBet(data: Buffer): BetData {
    const b: any = this.program.coder.accounts.decode("bet", data);
    const winning = Number(b.winningSide);
    return {
      betId: Buffer.from(b.betId),
      authority: b.authority, payer: b.payer, treasury: b.treasury, mint: b.mint,
      feeBps: Number(b.feeBps), minStake: big(b.minStake), maxStake: big(b.maxStake),
      maxParticipants: Number(b.maxParticipants), participantCount: Number(b.participantCount), positionsOpen: Number(b.positionsOpen),
      sideTotals: [big(b.sideTotals[0]), big(b.sideTotals[1])],
      state: Object.keys(b.state)[0] as BetState,
      winningSide: winning === NO_WINNER ? null : winning,
      feePaid: big(b.feePaid), distributable: big(b.distributable),
      fundingClosesAt: Number(b.fundingClosesAt.toString()), refundAfter: Number(b.refundAfter.toString()), createdAt: Number(b.createdAt.toString()),
    };
  }
  decodePosition(data: Buffer): PositionData {
    const p: any = this.program.coder.accounts.decode("position", data);
    return { bet: p.bet, owner: p.owner, side: Number(p.side), amount: big(p.amount) };
  }

  // ---- instruction builders (accounts strict: every account is explicit) ----
  createBetIx(a: { payer: PublicKey; betUuid: string; authority: PublicKey; treasury: PublicKey; mint: PublicKey; feeBps: number; minStake: bigint; maxStake: bigint; maxParticipants: number; fundingClosesAt: number; refundAfter: number }): Promise<TransactionInstruction> {
    const bet = this.betPda(a.betUuid);
    return this.program.methods
      .createBet(Array.from(uuidBytes(a.betUuid)), a.authority, a.treasury, a.feeBps, new BN(a.minStake.toString()), new BN(a.maxStake.toString()), a.maxParticipants, new BN(a.fundingClosesAt), new BN(a.refundAfter))
      .accountsStrict({ payer: a.payer, bet, mint: a.mint, vault: this.vaultPda(bet), tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId })
      .instruction();
  }
  depositIx(a: { depositor: PublicKey; rentPayer: PublicKey; bet: PublicKey; mint: PublicKey; side: number; amount: bigint }): Promise<TransactionInstruction> {
    return this.program.methods
      .deposit(a.side, new BN(a.amount.toString()))
      .accountsStrict({
        depositor: a.depositor, rentPayer: a.rentPayer, bet: a.bet, vault: this.vaultPda(a.bet), mint: a.mint,
        position: this.posPda(a.bet, a.depositor), depositorToken: getAssociatedTokenAddressSync(a.mint, a.depositor, true),
        tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId,
      })
      .instruction();
  }
  lockIx(authority: PublicKey, bet: PublicKey) {
    return this.program.methods.lock().accountsStrict({ authority, bet }).instruction();
  }
  refundAllIx(authority: PublicKey, bet: PublicKey) {
    return this.program.methods.refundAll().accountsStrict({ authority, bet }).instruction();
  }
  settleIx(a: { authority: PublicKey; bet: PublicKey; mint: PublicKey; treasuryToken: PublicKey; winningSide: number }) {
    return this.program.methods
      .settle(a.winningSide)
      .accountsStrict({ authority: a.authority, bet: a.bet, vault: this.vaultPda(a.bet), mint: a.mint, treasuryToken: a.treasuryToken, tokenProgram: TOKEN_PROGRAM_ID })
      .instruction();
  }
  claimIx(a: { signer: PublicKey; bet: PublicKey; mint: PublicKey; owner: PublicKey; rentReceiver: PublicKey }) {
    return this.program.methods
      .claim()
      .accountsStrict({
        signer: a.signer, bet: a.bet, vault: this.vaultPda(a.bet), mint: a.mint, position: this.posPda(a.bet, a.owner),
        ownerToken: getAssociatedTokenAddressSync(a.mint, a.owner, true), rentReceiver: a.rentReceiver, tokenProgram: TOKEN_PROGRAM_ID,
      })
      .instruction();
  }
  closeLoserIx(a: { signer: PublicKey; bet: PublicKey; owner: PublicKey; rentReceiver: PublicKey }) {
    return this.program.methods
      .closeLoserPosition()
      .accountsStrict({ signer: a.signer, bet: a.bet, position: this.posPda(a.bet, a.owner), rentReceiver: a.rentReceiver })
      .instruction();
  }
  closeBetIx(a: { authority: PublicKey; bet: PublicKey; payer: PublicKey; mint: PublicKey; treasuryToken: PublicKey }) {
    return this.program.methods
      .closeBet()
      .accountsStrict({ authority: a.authority, bet: a.bet, payer: a.payer, vault: this.vaultPda(a.bet), mint: a.mint, treasuryToken: a.treasuryToken, tokenProgram: TOKEN_PROGRAM_ID })
      .instruction();
  }
  ataIx(payer: PublicKey, owner: PublicKey, mint: PublicKey): TransactionInstruction {
    return createAssociatedTokenAccountIdempotentInstruction(payer, getAssociatedTokenAddressSync(mint, owner, true), owner, mint);
  }
  ata(owner: PublicKey, mint: PublicKey): PublicKey {
    return getAssociatedTokenAddressSync(mint, owner, true);
  }
}

export { Keypair };
