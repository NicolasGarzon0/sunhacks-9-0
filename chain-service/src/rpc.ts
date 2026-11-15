// The only module that talks to the network. Everything else uses the Rpc interface so tests can inject a fake.
import { Connection, PublicKey, SYSVAR_CLOCK_PUBKEY, VersionedTransaction } from "@solana/web3.js";
import { ChainError } from "./errors.ts";
import type { Logger } from "./logger.ts";

export const DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";

/** getMultipleAccounts accepts at most 100 keys per request */
export const MAX_MULTIPLE_ACCOUNTS = 100;

export interface AccountData { data: Buffer; lamports: number; owner: PublicKey }
export interface TxOutcome { err: unknown | null; logs: string[] }

export interface Rpc {
  getGenesisHash(): Promise<string>;
  getAccount(pk: PublicKey): Promise<AccountData | null>;
  /** one batched read (at most MAX_MULTIPLE_ACCOUNTS keys per call); null for an account that does not exist. Same order as `pks`. */
  getMultipleAccounts(pks: PublicKey[]): Promise<(AccountData | null)[]>;
  getTokenBalance(pk: PublicKey): Promise<bigint>;
  getBalance(pk: PublicKey): Promise<number>;
  getLatestBlockhash(): Promise<string>;
  getLatestBlockhashInfo(): Promise<{ blockhash: string; lastValidBlockHeight: number }>;
  isBlockhashValid(blockhash: string): Promise<boolean>;
  /** unix seconds from the chain's Clock sysvar (the clock the program uses) */
  getClockUnix(): Promise<number>;
  /** simulate a serialized transaction without checking signatures */
  simulate(bytes: Uint8Array): Promise<{ err: unknown | null; logs: string[] }>;
  /** throws an error carrying `.logs` when the program rejects (preflight simulation) */
  sendRaw(bytes: Uint8Array): Promise<string>;
  confirm(signature: string): Promise<TxOutcome>;
}

const TRANSIENT = /429|too many|rate.?limit|fetch failed|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|timed? ?out|\b50[234]\b|socket hang up/i;

export async function withRetry<T>(label: string, fn: () => Promise<T>, logger?: Logger, delays = [1000, 2000, 4000, 8000]): Promise<T> {
  for (let i = 0; ; i++) {
    try {
      return await fn();
    } catch (e) {
      const logs = (e as { logs?: string[] } | null)?.logs;
      const msg = e instanceof Error ? e.message : String(e);
      // a program rejection (it has logs) is a final answer, never retried
      if ((logs && logs.length > 0) || !TRANSIENT.test(msg)) throw e;
      if (i >= delays.length) throw new ChainError("RPC_ERROR", `${label}: RPC keeps rate limiting or failing`);
      logger?.warn("rpc.retry", { label, attempt: i + 1, waitMs: delays[i] });
      await new Promise((r) => setTimeout(r, delays[i]));
    }
  }
}

export class ConnectionRpc implements Rpc {
  constructor(public readonly conn: Connection, private logger?: Logger) {}
  static create(url: string, logger?: Logger) {
    return new ConnectionRpc(new Connection(url, { commitment: "confirmed", disableRetryOnRateLimit: true }), logger);
  }
  private r<T>(label: string, fn: () => Promise<T>) {
    return withRetry(label, fn, this.logger);
  }
  getGenesisHash() { return this.r("genesis", () => this.conn.getGenesisHash()); }
  async getAccount(pk: PublicKey): Promise<AccountData | null> {
    const a = await this.r("getAccount", () => this.conn.getAccountInfo(pk, "confirmed"));
    return a ? { data: Buffer.from(a.data), lamports: a.lamports, owner: a.owner } : null;
  }
  async getMultipleAccounts(pks: PublicKey[]): Promise<(AccountData | null)[]> {
    if (pks.length > MAX_MULTIPLE_ACCOUNTS) throw new ChainError("INVALID_PARAMS", `at most ${MAX_MULTIPLE_ACCOUNTS} accounts per read`);
    if (pks.length === 0) return [];
    const res = await this.r("getMultipleAccounts", () => this.conn.getMultipleAccountsInfo(pks, "confirmed"));
    return res.map((a) => (a ? { data: Buffer.from(a.data), lamports: a.lamports, owner: a.owner } : null));
  }
  async getTokenBalance(pk: PublicKey): Promise<bigint> {
    try {
      return BigInt((await this.r("tokenBalance", () => this.conn.getTokenAccountBalance(pk, "confirmed"))).value.amount);
    } catch (e) {
      if (/could not find account|invalid param/i.test(e instanceof Error ? e.message : String(e))) return 0n;
      throw e;
    }
  }
  getBalance(pk: PublicKey) { return this.r("balance", () => this.conn.getBalance(pk, "confirmed")); }
  async getLatestBlockhash() { return (await this.r("blockhash", () => this.conn.getLatestBlockhash("confirmed"))).blockhash; }
  async getLatestBlockhashInfo() {
    const r = await this.r("blockhash", () => this.conn.getLatestBlockhash("confirmed"));
    return { blockhash: r.blockhash, lastValidBlockHeight: r.lastValidBlockHeight };
  }
  async isBlockhashValid(blockhash: string): Promise<boolean> {
    return (await this.r("isBlockhashValid", () => this.conn.isBlockhashValid(blockhash, { commitment: "confirmed" }))).value;
  }
  async getClockUnix(): Promise<number> {
    const a = await this.r("clock", () => this.conn.getAccountInfo(SYSVAR_CLOCK_PUBKEY, "confirmed"));
    if (!a) throw new ChainError("RPC_ERROR", "clock sysvar missing");
    return Number(Buffer.from(a.data).readBigInt64LE(32));
  }
  async simulate(bytes: Uint8Array) {
    const r = await this.r("simulate", () => this.conn.simulateTransaction(VersionedTransaction.deserialize(bytes), { sigVerify: false, commitment: "confirmed" }));
    return { err: r.value.err, logs: r.value.logs ?? [] };
  }
  async sendRaw(bytes: Uint8Array): Promise<string> {
    return this.r("send", () => this.conn.sendRawTransaction(bytes, { preflightCommitment: "confirmed" }));
  }
  async confirm(signature: string): Promise<TxOutcome> {
    for (let i = 0; i < 90; i++) {
      const s = (await this.r("status", () => this.conn.getSignatureStatuses([signature]))).value[0];
      if (s?.err) {
        const t = await this.r("getTransaction", () => this.conn.getTransaction(signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }));
        return { err: s.err, logs: t?.meta?.logMessages ?? [] };
      }
      if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") return { err: null, logs: [] };
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new ChainError("RPC_ERROR", "transaction not confirmed in 90s");
  }
}

/** Abort unless the RPC is devnet (genesis hash, so custom provider URLs work too). */
export async function guardDevnet(rpc: Rpc): Promise<void> {
  const hash = await rpc.getGenesisHash();
  if (hash !== DEVNET_GENESIS) throw new Error(`ABORT: the RPC is not devnet (genesis ${hash}). This service only runs on devnet.`);
}
