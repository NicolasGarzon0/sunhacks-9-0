// Sponsored deposits. The payer key signs ONLY after the exact transaction has been verified, and the bytes that were
// verified are the bytes that are signed and sent: a signature authorizes bytes, so nothing is modified after any signature.
import { createHash } from "node:crypto";
import { Message, PublicKey, Transaction, VersionedMessage, VersionedTransaction } from "@solana/web3.js";
import type { ChainService, Json } from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { signEd25519, verifyEd25519 } from "./crypto.ts";
import { ChainError, mapLogs, toChainError } from "./errors.ts";
import type { Logger } from "./logger.ts";
import type { BetData } from "./program.ts";
import { RateLimiter } from "./ratelimit.ts";
import type { Rpc } from "./rpc.ts";

const MAX_BASE = BigInt(Number.MAX_SAFE_INTEGER);
const reject = (reason: string) => new ChainError("TX_REJECTED", reason);
export const explorerTx = (sig: string) => `https://explorer.solana.com/tx/${sig}?cluster=devnet`;

export interface DepositArgs { bet_id: string; wallet: string; side: number; amount_base: number }
export type SubmitArgs = DepositArgs & { signed_tx_b64?: string; message_b64?: string; user_signature_b64?: string };

function shortvecLen(n: number): Buffer {
  const out: number[] = [];
  for (;;) { const b = n & 0x7f; n >>= 7; if (n === 0) { out.push(b); break; } out.push(b | 0x80); }
  return Buffer.from(out);
}

export class DepositService {
  private walletLimit: RateLimiter;
  private betLimit: RateLimiter;
  private done = new Map<string, Promise<string>>(); // idempotency: hash(message + user signature) -> signature

  constructor(private deps: { rpc: Rpc; cfg: ChainConfig; chain: ChainService; logger: Logger; now?: () => number }) {
    const now = deps.now ?? Date.now;
    this.walletLimit = new RateLimiter(deps.cfg.depositRatePerWalletPerMin, 60_000, now);
    this.betLimit = new RateLimiter(deps.cfg.depositRatePerBetPerMin, 60_000, now);
  }
  private get rpc() { return this.deps.rpc; }
  private get cfg() { return this.deps.cfg; }
  private get chain() { return this.deps.chain; }
  private get log() { return this.deps.logger; }

  // ---- shared rules ----

  private parse(a: DepositArgs): { wallet: PublicKey; amount: bigint } {
    let wallet: PublicKey;
    try { wallet = new PublicKey(a.wallet); } catch { throw new ChainError("BAD_REQUEST", "wallet is not a valid public key"); }
    if (a.side !== 0 && a.side !== 1) throw new ChainError("INVALID_PARAMS", "side must be 0 or 1");
    if (!Number.isSafeInteger(a.amount_base) || a.amount_base <= 0) throw new ChainError("INVALID_PARAMS", "amount_base must be a positive integer");
    return { wallet, amount: BigInt(a.amount_base) };
  }

  private limited(a: DepositArgs): void {
    if (!this.walletLimit.hit(`w:${a.wallet}`) || !this.betLimit.hit(`b:${a.bet_id}`)) throw new ChainError("RATE_LIMITED", "too many deposit requests, slow down");
  }

  /** Live chain state against the deposit rules, in the contract's priority order, plus the wallet's token account. */
  async prevalidate(a: DepositArgs): Promise<BetData> {
    const { wallet, amount } = this.parse(a);
    const { bet } = await this.chain.loadBet(a.bet_id); // includes the trust check
    const betPda = this.chain.pool.betPda(a.bet_id);
    if (bet.state !== "open") throw new ChainError("MARKET_NOT_OPEN", `market is ${bet.state}`);
    const now = await this.rpc.getClockUnix();
    if (now + this.cfg.depositCutoffSeconds >= bet.fundingClosesAt) throw new ChainError("FUNDING_CLOSED", "funding is closing or closed");
    if (amount < bet.minStake) throw new ChainError("BELOW_MIN", "amount is below the minimum");
    const pos = await this.rpc.getAccount(this.chain.pool.posPda(betPda, wallet));
    let existing = 0n;
    if (!pos) {
      if (bet.participantCount >= bet.maxParticipants) throw new ChainError("MARKET_FULL", "no room for another participant");
    } else {
      const p = this.chain.pool.decodePosition(pos.data);
      if (p.side !== a.side) throw new ChainError("ALREADY_OTHER_SIDE", "this wallet already holds the other side");
      existing = p.amount;
    }
    if (existing + amount > bet.maxStake) throw new ChainError("ABOVE_MAX", "position would exceed the maximum");
    const ata = this.chain.pool.ata(wallet, this.cfg.mint);
    if (!(await this.rpc.getAccount(ata))) throw new ChainError("NO_TOKEN_ACCOUNT", "the wallet has no USDC token account yet");
    if ((await this.rpc.getTokenBalance(ata)) < amount) throw new ChainError("INSUFFICIENT_USDC", "not enough USDC");
    return bet;
  }

  /** The only message this service will ever sign: ONE legacy deposit instruction, payer as fee payer and rent payer. */
  private async expected(a: DepositArgs, blockhash: string): Promise<{ tx: Transaction; message: Buffer }> {
    const { wallet, amount } = this.parse(a);
    const ix = await this.chain.pool.depositIx({
      depositor: wallet, rentPayer: this.cfg.payer.publicKey, bet: this.chain.pool.betPda(a.bet_id), mint: this.cfg.mint, side: a.side, amount,
    });
    const tx = new Transaction({ feePayer: this.cfg.payer.publicKey, recentBlockhash: blockhash });
    tx.add(ix);
    return { tx, message: tx.serializeMessage() };
  }

  // ---- build ----

  async build(a: DepositArgs): Promise<Json> {
    this.limited(a);
    await this.prevalidate(a);
    const { blockhash, lastValidBlockHeight } = await this.rpc.getLatestBlockhashInfo();
    const { tx, message } = await this.expected(a, blockhash);
    const unsigned = tx.serialize({ requireAllSignatures: false, verifySignatures: false }); // two empty signature slots
    const expires = Date.now() + this.cfg.depositBuildTtlSeconds * 1000;
    this.log.info("deposit.build", { betId: a.bet_id, wallet: a.wallet.slice(0, 6), instruction: "deposit" });
    return {
      ok: true, transaction_b64: unsigned.toString("base64"), message_b64: message.toString("base64"), blockhash,
      last_valid_block_height: lastValidBlockHeight, expires_at: new Date(expires).toISOString(), expires_at_unix: Math.floor(expires / 1000),
    };
  }

  // ---- submit ----

  private extract(a: SubmitArgs, wallet: PublicKey): { message: Buffer; userSig: Buffer } {
    const payer = this.cfg.payer.publicKey;
    if (a.signed_tx_b64 !== undefined) {
      const wire = Buffer.from(a.signed_tx_b64, "base64");
      let vtx: VersionedTransaction;
      try { vtx = VersionedTransaction.deserialize(wire); } catch { throw reject("the transaction could not be read"); }
      if (vtx.version !== "legacy") throw reject("only legacy transactions are accepted");
      const required = vtx.message.header.numRequiredSignatures;
      if (vtx.signatures.length !== required || required !== 2) throw reject("exactly two signature slots are required");
      const count = wire[0]!;
      const message = wire.subarray(1 + 64 * count);
      const keys = vtx.message.staticAccountKeys;
      const payerSig = vtx.signatures[keys.findIndex((k) => k.equals(payer))];
      const userIdx = keys.slice(0, required).findIndex((k) => k.equals(wallet));
      if (userIdx < 0) throw reject("the wallet is not a signer");
      if (payerSig && payerSig.some((b) => b !== 0)) throw reject("the payer signature must be left empty");
      const userSig = Buffer.from(vtx.signatures[userIdx]!);
      if (userSig.every((b) => b === 0)) throw reject("missing wallet signature");
      return { message: Buffer.from(message), userSig };
    }
    if (a.message_b64 !== undefined && a.user_signature_b64 !== undefined) {
      const message = Buffer.from(a.message_b64, "base64");
      let version: "legacy" | number;
      try { version = VersionedMessage.deserializeMessageVersion(message); } catch { throw reject("the message could not be read"); }
      if (version !== "legacy") throw reject("only legacy transactions are accepted");
      const userSig = Buffer.from(a.user_signature_b64, "base64");
      if (userSig.length !== 64) throw reject("the wallet signature must be 64 bytes");
      return { message, userSig };
    }
    throw new ChainError("BAD_REQUEST", "send signed_tx_b64, or message_b64 with user_signature_b64");
  }

  /** structure rules, each with a safe reason: legacy message, the two signers, exactly one pool deposit instruction */
  private checkStructure(messageBytes: Buffer, wallet: PublicKey): string {
    let msg: Message;
    try { msg = Message.from(messageBytes); } catch { throw reject("the message could not be read"); }
    const keys = msg.accountKeys;
    if (msg.header.numRequiredSignatures !== 2) throw reject("exactly two signers (the sponsor and the wallet) are required");
    if (!keys[0]!.equals(this.cfg.payer.publicKey)) throw reject("the sponsor must be the fee payer");
    if (!keys[1]!.equals(wallet)) throw reject("the second signer must be the wallet");
    if (msg.instructions.length !== 1) throw reject("exactly one instruction is allowed");
    const tx = Transaction.populate(msg);
    const ix = tx.instructions[0]!;
    if (!ix.programId.equals(this.cfg.programId)) throw reject("the instruction is not for the pool program");
    const disc = this.depositDiscriminator();
    if (!Buffer.from(ix.data).subarray(0, 8).equals(disc)) throw reject("the instruction is not a deposit");
    return msg.recentBlockhash;
  }

  private depositDiscriminator(): Buffer {
    const d = (this.chain.pool.program.idl.instructions as { name: string; discriminator: number[] }[]).find((i) => i.name === "deposit")!.discriminator;
    return Buffer.from(d);
  }

  async submit(a: SubmitArgs): Promise<Json> {
    this.limited(a);
    const { wallet, amount } = this.parse(a);
    const { message, userSig } = this.extract(a, wallet);
    const blockhash = this.checkStructure(message, wallet);

    // the submitted bytes must be EXACTLY the message this service would build itself (catches any tampering)
    const rebuilt = (await this.expected(a, blockhash)).message;
    if (!rebuilt.equals(message)) throw reject("the transaction does not match the requested deposit");
    if (!verifyEd25519(wallet.toBytes(), message, userSig)) throw reject("the wallet signature does not match the message");

    const key = createHash("sha256").update(message).update(userSig).digest("hex");
    const cached = this.done.get(key);
    if (cached) {
      const signature = await cached;
      return { ok: true, already: true, signature, explorer: explorerTx(signature) };
    }
    const attempt = this.finish(a, wallet, amount, message, userSig, blockhash);
    this.done.set(key, attempt);
    if (this.done.size > 1000) this.done.delete(this.done.keys().next().value as string);
    try {
      const signature = await attempt;
      return { ok: true, already: false, signature, explorer: explorerTx(signature) };
    } catch (e) {
      this.done.delete(key); // a failed attempt may be retried
      throw e;
    }
  }

  private async finish(a: DepositArgs, wallet: PublicKey, amount: bigint, message: Buffer, userSig: Buffer, blockhash: string): Promise<string> {
    if (!(await this.rpc.isBlockhashValid(blockhash))) throw new ChainError("TX_EXPIRED", "the transaction expired: build and sign it again");
    await this.prevalidate(a); // the market may have changed since the build

    // the sponsor's slot is empty while simulating: simulation never needs (or gets) the payer signature
    const slots = (payerSig: Uint8Array) => Buffer.concat([shortvecLen(2), payerSig, userSig, message]);
    const sim = await this.rpc.simulate(slots(Buffer.alloc(64)));
    if (sim.err) {
      const mapped = mapLogs(sim.logs);
      if (mapped) throw mapped;
      throw reject("the program would reject this deposit");
    }

    // verified: sign the very same bytes with the payer and send. Nothing about the message changes from here on.
    const payerSig = signEd25519(this.cfg.payer.secretKey, message);
    const wire = slots(payerSig);
    let signature: string;
    try {
      signature = await this.rpc.sendRaw(wire);
      const out = await this.rpc.confirm(signature);
      if (out.err) throw Object.assign(new Error(JSON.stringify(out.err)), { logs: out.logs });
    } catch (e) {
      const err = toChainError(e);
      this.log.warn("deposit.failed", { betId: a.bet_id, wallet: a.wallet.slice(0, 6), code: err.code, detail: err.detail });
      if (err.code === "RPC_ERROR") throw new ChainError(/blockhash/i.test(err.detail) ? "TX_EXPIRED" : "SUBMIT_FAILED", /blockhash/i.test(err.detail) ? "the transaction expired: build and sign it again" : "the transaction could not be sent");
      throw err;
    }
    this.log.info("deposit.sent", { betId: a.bet_id, wallet: a.wallet.slice(0, 6), instruction: "deposit", signature });
    void amount;
    return signature;
  }
}
