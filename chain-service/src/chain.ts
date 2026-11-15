// ChainService: the operations behind the HTTP API. Idempotent, trust checked, errors mapped.
import { PublicKey, TransactionMessage, VersionedTransaction, type Keypair, type TransactionInstruction } from "@solana/web3.js";
import type { ChainConfig } from "./config.ts";
import { ChainError, mapLogs, toChainError } from "./errors.ts";
import type { Logger } from "./logger.ts";
import { POSITION_SIZE, Pool, uuidBytes, type BetData, type PositionData } from "./program.ts";
import { MAX_MULTIPLE_ACCOUNTS, type Rpc } from "./rpc.ts";

const BPS = 10_000n;
/** upper bound on the wallets one positions request may name (a bet holds at most 100 participants; the rest is slack for wallets that never deposited) */
export const MAX_DEPOSITORS = 1000;
const MAX_JS = BigInt(Number.MAX_SAFE_INTEGER);

export type Json = Record<string, unknown>;

function num(v: bigint): number {
  if (v > MAX_JS) throw new ChainError("RPC_ERROR", "amount does not fit a JSON number");
  return Number(v);
}

export function payoutFor(stake: bigint, distributable: bigint, winningTotal: bigint): bigint {
  if (winningTotal === 0n) return 0n;
  return (stake * distributable) / winningTotal;
}

export interface CreateParams {
  bet_id: string;
  funding_closes_at: number;
  refund_after: number;
  fee_bps: number;
  min_stake_base: number;
  max_stake_base: number;
  max_participants: number;
}

export class ChainService {
  readonly pool: Pool;
  readonly treasuryAta: PublicKey;

  constructor(private deps: { rpc: Rpc; cfg: ChainConfig; logger: Logger }) {
    this.pool = new Pool(deps.cfg.programId);
    this.treasuryAta = this.pool.ata(deps.cfg.treasury, deps.cfg.mint);
  }
  private get rpc() { return this.deps.rpc; }
  private get cfg() { return this.deps.cfg; }
  private get log() { return this.deps.logger; }

  // ---- reads ----

  async health(): Promise<Json> {
    const [payer, oracle] = await Promise.all([this.rpc.getBalance(this.cfg.payer.publicKey), this.rpc.getBalance(this.cfg.oracle.publicKey)]);
    return {
      ok: true, cluster: "devnet", program_id: this.cfg.programId.toBase58(),
      payer: this.cfg.payer.publicKey.toBase58(), oracle: this.cfg.oracle.publicKey.toBase58(),
      payer_sol: payer / 1e9, oracle_sol: oracle / 1e9,
    };
  }

  /** Reads the Bet and enforces the trust check. NOT_FOUND when the account does not exist. */
  async loadBet(betId: string): Promise<{ pda: PublicKey; bet: BetData }> {
    const pda = this.pool.betPda(betId);
    const acc = await this.rpc.getAccount(pda);
    if (!acc) throw new ChainError("NOT_FOUND", "unknown bet");
    return { pda, bet: this.trusted(acc.owner, acc.data) };
  }

  private trusted(owner: PublicKey, data: Buffer): BetData {
    if (!owner.equals(this.cfg.programId)) throw new ChainError("UNTRUSTED_BET", "account is not owned by the pool program");
    let bet: BetData;
    try {
      bet = this.pool.decodeBet(data);
    } catch {
      throw new ChainError("UNTRUSTED_BET", "account is not a Bet");
    }
    const bad =
      !bet.authority.equals(this.cfg.oracle.publicKey) ? "authority"
      : !bet.payer.equals(this.cfg.payer.publicKey) ? "payer"
      : !bet.treasury.equals(this.cfg.treasury) ? "treasury"
      : !bet.mint.equals(this.cfg.mint) ? "mint" : null;
    if (bad) throw new ChainError("UNTRUSTED_BET", `bet ${bad} does not match this service's configuration`);
    return bet;
  }

  private view(bet: BetData, extra: Json = {}): Json {
    return {
      state: bet.state,
      side_totals: [num(bet.sideTotals[0]), num(bet.sideTotals[1])],
      participant_count: bet.participantCount,
      positions_open: bet.positionsOpen,
      fee_bps: bet.feeBps,
      fee_paid_base: num(bet.feePaid),
      distributable_base: num(bet.distributable),
      winning_side: bet.winningSide,
      funding_closes_at: bet.fundingClosesAt,
      refund_after: bet.refundAfter,
      min_stake_base: num(bet.minStake),
      max_stake_base: num(bet.maxStake),
      max_participants: bet.maxParticipants,
      ...extra,
    };
  }

  async getBet(betId: string): Promise<Json> {
    const { pda, bet } = await this.loadBet(betId);
    const [vault, treasury] = await Promise.all([this.rpc.getTokenBalance(this.pool.vaultPda(pda)), this.rpc.getTokenBalance(this.treasuryAta)]);
    return this.view(bet, { vault_base: num(vault), treasury_balance_base: num(treasury) });
  }

  /**
   * The open positions of `depositors` in this bet. Never scans the program (no program-wide account query): each Position PDA is
   * derived from ["position", bet, depositor] and read with getMultipleAccounts in batches. A depositor with no open
   * position (never deposited, claimed, or closed) is simply absent from the answer.
   */
  private async readPositions(pda: PublicKey, depositors: PublicKey[]): Promise<{ pubkey: PublicKey; pos: PositionData }[]> {
    const keys = depositors.map((d) => ({ depositor: d, pda: this.pool.posPda(pda, d) }));
    const disc = this.pool.positionDiscriminator;
    const out: { pubkey: PublicKey; pos: PositionData }[] = [];
    for (let i = 0; i < keys.length; i += MAX_MULTIPLE_ACCOUNTS) {
      const batch = keys.slice(i, i + MAX_MULTIPLE_ACCOUNTS);
      const accounts = await this.rpc.getMultipleAccounts(batch.map((k) => k.pda));
      batch.forEach((k, j) => {
        const acc = accounts[j];
        if (!acc || !acc.owner.equals(this.cfg.programId) || acc.data.length !== POSITION_SIZE || !acc.data.subarray(0, 8).equals(disc)) return;
        const pos = this.pool.decodePosition(acc.data);
        if (!pos.bet.equals(pda) || !pos.owner.equals(k.depositor)) return; // cannot happen for a PDA this program made; never trust it blindly
        out.push({ pubkey: k.pda, pos });
      });
    }
    return out.sort((a, b) => a.pos.owner.toBase58().localeCompare(b.pos.owner.toBase58()));
  }

  /** `depositors` comes from the caller (the backend's participants table): a list of base58 wallets, deduplicated here. */
  async positions(betId: string, depositors: unknown): Promise<Json> {
    if (!Array.isArray(depositors)) throw new ChainError("BAD_REQUEST", "depositors must be an array of wallet public keys");
    if (depositors.length > MAX_DEPOSITORS) throw new ChainError("BAD_REQUEST", `at most ${MAX_DEPOSITORS} depositors per request`);
    const wallets = new Map<string, PublicKey>();
    for (const d of depositors) {
      let pk: PublicKey;
      try {
        if (typeof d !== "string") throw new Error("not a string");
        pk = new PublicKey(d);
      } catch {
        throw new ChainError("BAD_REQUEST", "depositors must be valid wallet public keys");
      }
      wallets.set(pk.toBase58(), pk);
    }
    const { pda } = await this.loadBet(betId);
    const rows = await this.readPositions(pda, [...wallets.values()]);
    return { ok: true, positions: rows.map((r) => ({ wallet: r.pos.owner.toBase58(), side: r.pos.side, amount_base: num(r.pos.amount) })) };
  }

  // ---- transaction plumbing ----

  async send(betId: string, instruction: string, ixs: TransactionInstruction[], extraSigners: Keypair[]): Promise<string> {
    const payer = this.cfg.payer;
    const msg = new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: await this.rpc.getLatestBlockhash(), instructions: ixs }).compileToV0Message();
    const tx = new VersionedTransaction(msg);
    tx.sign([payer, ...extraSigners.filter((s) => !s.publicKey.equals(payer.publicKey))]);
    let signature: string;
    try {
      signature = await this.rpc.sendRaw(tx.serialize());
      const out = await this.rpc.confirm(signature);
      if (out.err) throw Object.assign(new Error(JSON.stringify(out.err)), { logs: out.logs });
    } catch (e) {
      const err = toChainError(e);
      this.log.warn("tx.failed", { betId, instruction, code: err.code, detail: err.detail });
      throw err;
    }
    this.log.info("tx.sent", { betId, instruction, signature });
    return signature;
  }

  /** run a state changing call; if it races with another writer, re-read once and return the settled answer */
  private async guarded<T>(betId: string, instruction: string, run: () => Promise<T>, recheck: () => Promise<T | null>): Promise<T> {
    try {
      return await run();
    } catch (e) {
      const err = toChainError(e);
      if (err.code === "WRONG_STATE" || err.code === "NOT_FOUND" || err.code === "MARKET_NOT_OPEN") {
        const again = await recheck();
        if (again) return again;
      }
      throw err;
    }
  }

  // ---- writes ----

  async createBet(p: CreateParams): Promise<Json> {
    const pda = this.pool.betPda(p.bet_id); // validates the UUID
    const existing = await this.rpc.getAccount(pda);
    if (existing) return { ok: true, already: true, ...this.view(this.trusted(existing.owner, existing.data)) };
    for (const [k, v] of Object.entries(p)) if (k !== "bet_id" && !Number.isSafeInteger(v)) throw new ChainError("BAD_REQUEST", `${k} must be an integer`);
    const ix = await this.pool.createBetIx({
      payer: this.cfg.payer.publicKey, betUuid: p.bet_id, authority: this.cfg.oracle.publicKey, treasury: this.cfg.treasury, mint: this.cfg.mint,
      feeBps: p.fee_bps, minStake: BigInt(p.min_stake_base), maxStake: BigInt(p.max_stake_base), maxParticipants: p.max_participants,
      fundingClosesAt: p.funding_closes_at, refundAfter: p.refund_after,
    });
    const signature = await this.guarded(p.bet_id, "create_bet", () => this.send(p.bet_id, "create_bet", [ix], []), async () => (await this.rpc.getAccount(pda)) ? "exists" : null);
    const { bet } = await this.loadBet(p.bet_id);
    return { ok: true, already: signature === "exists", signature: signature === "exists" ? null : signature, ...this.view(bet) };
  }

  async lock(betId: string): Promise<Json> {
    const { pda, bet } = await this.loadBet(betId);
    if (bet.state === "locked" || bet.state === "settled") return { ok: true, already: true, ...this.view(bet) };
    if (bet.state !== "open") throw new ChainError("WRONG_STATE", `bet is ${bet.state}`);
    const ix = await this.pool.lockIx(this.cfg.oracle.publicKey, pda);
    const signature = await this.send(betId, "lock", [ix], [this.cfg.oracle]);
    return { ok: true, already: false, signature, ...this.view((await this.loadBet(betId)).bet) };
  }

  async settle(betId: string, winningSide: number): Promise<Json> {
    if (winningSide !== 0 && winningSide !== 1) throw new ChainError("BAD_REQUEST", "winning_side must be 0 or 1");
    const { pda, bet } = await this.loadBet(betId);
    if (bet.state === "settled") {
      if (bet.winningSide === winningSide) return { ok: true, already: true, ...this.view(bet) };
      throw new ChainError("WRONG_STATE", `bet is already settled with winning side ${bet.winningSide}`);
    }
    if (bet.state !== "locked") throw new ChainError("WRONG_STATE", `bet is ${bet.state}`);
    const ixs = [
      this.pool.ataIx(this.cfg.payer.publicKey, this.cfg.treasury, this.cfg.mint), // the fee destination must exist
      await this.pool.settleIx({ authority: this.cfg.oracle.publicKey, bet: pda, mint: this.cfg.mint, treasuryToken: this.treasuryAta, winningSide }),
    ];
    const signature = await this.send(betId, "settle", ixs, [this.cfg.oracle]);
    return { ok: true, already: false, signature, ...this.view((await this.loadBet(betId)).bet) };
  }

  async refundAll(betId: string): Promise<Json> {
    const { pda, bet } = await this.loadBet(betId);
    if (bet.state === "refunded") return { ok: true, already: true, ...this.view(bet) };
    if (bet.state === "settled") throw new ChainError("WRONG_STATE", "bet is settled");
    const ix = await this.pool.refundAllIx(this.cfg.oracle.publicKey, pda);
    const signature = await this.send(betId, "refund_all", [ix], [this.cfg.oracle]);
    return { ok: true, already: false, signature, ...this.view((await this.loadBet(betId)).bet) };
  }

  private parseWallet(wallet: unknown): PublicKey {
    try {
      if (typeof wallet !== "string") throw new Error("type");
      return new PublicKey(wallet);
    } catch {
      throw new ChainError("BAD_REQUEST", "wallet is not a valid public key");
    }
  }

  async claim(betId: string, walletRaw: unknown): Promise<Json> {
    const wallet = this.parseWallet(walletRaw);
    const { pda, bet } = await this.loadBet(betId);
    if (bet.state !== "settled" && bet.state !== "refunded") throw new ChainError("WRONG_STATE", `bet is ${bet.state}`);
    const posKey = this.pool.posPda(pda, wallet);
    const acc = await this.rpc.getAccount(posKey);
    if (!acc) return { ok: true, already: true, payout_base: null, kind: null };
    const pos = this.pool.decodePosition(acc.data);
    let payout: bigint, kind: string;
    if (bet.state === "refunded") {
      payout = pos.amount; kind = "refund";
    } else {
      if (pos.side !== bet.winningSide) throw new ChainError("NOT_A_WINNER", "this position is on the losing side");
      payout = payoutFor(pos.amount, bet.distributable, bet.sideTotals[bet.winningSide as 0 | 1]);
      kind = "winnings";
    }
    const ixs = [
      this.pool.ataIx(this.cfg.payer.publicKey, wallet, this.cfg.mint),
      await this.pool.claimIx({ signer: this.cfg.payer.publicKey, bet: pda, mint: this.cfg.mint, owner: wallet, rentReceiver: bet.payer }),
    ];
    const signature = await this.guarded(
      betId, "claim", () => this.send(betId, "claim", ixs, []),
      async () => ((await this.rpc.getAccount(posKey)) ? null : "gone"),
    );
    if (signature === "gone") return { ok: true, already: true, payout_base: null, kind: null };
    return { ok: true, already: false, signature, payout_base: num(payout), kind };
  }

  async closeLoserPosition(betId: string, walletRaw: unknown): Promise<Json> {
    const wallet = this.parseWallet(walletRaw);
    const { pda, bet } = await this.loadBet(betId);
    const posKey = this.pool.posPda(pda, wallet);
    const acc = await this.rpc.getAccount(posKey);
    if (!acc) return { ok: true, already: true };
    if (bet.state !== "settled") throw new ChainError("WRONG_STATE", `bet is ${bet.state}`);
    const pos = this.pool.decodePosition(acc.data);
    if (pos.side === bet.winningSide) throw new ChainError("WRONG_STATE", "this is a winning position: claim it instead");
    const ix = await this.pool.closeLoserIx({ signer: this.cfg.payer.publicKey, bet: pda, owner: wallet, rentReceiver: bet.payer });
    const signature = await this.guarded(
      betId, "close_loser_position", () => this.send(betId, "close_loser_position", [ix], []),
      async () => ((await this.rpc.getAccount(posKey)) ? null : "gone"),
    );
    if (signature === "gone") return { ok: true, already: true };
    return { ok: true, already: false, signature };
  }

  async closeBet(betId: string): Promise<Json> {
    const pda = this.pool.betPda(betId);
    const acc = await this.rpc.getAccount(pda);
    if (!acc) return { ok: true, already: true };
    const bet = this.trusted(acc.owner, acc.data);
    if (bet.state !== "settled" && bet.state !== "refunded") throw new ChainError("WRONG_STATE", `bet is ${bet.state}`);
    if (bet.positionsOpen > 0) throw new ChainError("WRONG_STATE", `${bet.positionsOpen} positions are still open`);
    const ixs = [
      this.pool.ataIx(this.cfg.payer.publicKey, this.cfg.treasury, this.cfg.mint),
      await this.pool.closeBetIx({ authority: this.cfg.oracle.publicKey, bet: pda, payer: bet.payer, mint: this.cfg.mint, treasuryToken: this.treasuryAta }),
    ];
    const signature = await this.guarded(
      betId, "close_bet", () => this.send(betId, "close_bet", ixs, [this.cfg.oracle]),
      async () => ((await this.rpc.getAccount(pda)) ? null : "gone"),
    );
    if (signature === "gone") return { ok: true, already: true };
    return { ok: true, already: false, signature };
  }
}

export { mapLogs, uuidBytes };
