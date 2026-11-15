import { PublicKey, SystemProgram } from "@solana/web3.js";
import { createMintToCheckedInstruction } from "@solana/spl-token";
import type { ChainService, Json } from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { ChainError } from "./errors.ts";
import type { Logger } from "./logger.ts";
import type { Rpc } from "./rpc.ts";
import { explorerTx } from "./deposit.ts";

export class WalletService {
  constructor(private deps: { rpc: Rpc; cfg: ChainConfig; chain: ChainService; logger: Logger }) {}

  private pubkey(s: string): PublicKey {
    try { return new PublicKey(s); } catch { throw new ChainError("BAD_REQUEST", "wallet is not a valid public key"); }
  }

  async wallet(addr: string): Promise<Json> {
    const owner = this.pubkey(addr);
    const ata = this.deps.chain.pool.ata(owner, this.deps.cfg.mint);
    const [acc, sol] = await Promise.all([this.deps.rpc.getAccount(ata), this.deps.rpc.getBalance(owner)]);
    const usdc = acc ? await this.deps.rpc.getTokenBalance(ata) : 0n;
    return { ok: true, wallet: owner.toBase58(), has_token_account: acc !== null, usdc_base: Number(usdc), sol_lamports: sol };
  }

  /** Creates the token account if missing (the payer pays), then mints. Policy (who may ask, how often) lives in the backend. */
  async airdrop(addr: string, amountBase: unknown): Promise<Json> {
    const { cfg, chain, logger } = this.deps;
    if (!cfg.mintAuthority) throw new ChainError("AIRDROP_DISABLED", "this service has no mint authority configured");
    const owner = this.pubkey(addr);
    if (typeof amountBase !== "number" || !Number.isSafeInteger(amountBase) || amountBase <= 0) throw new ChainError("INVALID_PARAMS", "amount_base must be a positive integer");
    if (BigInt(amountBase) > cfg.airdropMaxPerCallBase) throw new ChainError("INVALID_PARAMS", `amount_base is above the per call cap of ${cfg.airdropMaxPerCallBase}`);
    const ata = chain.pool.ata(owner, cfg.mint);
    const ixs = [
      chain.pool.ataIx(cfg.payer.publicKey, owner, cfg.mint), // idempotent: never fails when it exists, never closes anything
      createMintToCheckedInstruction(cfg.mint, ata, cfg.mintAuthority.publicKey, BigInt(amountBase), 6),
    ];
    const signature = await chain.send("airdrop", "airdrop", ixs, [cfg.mintAuthority]);
    logger.info("airdrop.sent", { wallet: addr.slice(0, 6), amountBase, signature });
    return { ok: true, wallet: owner.toBase58(), token_account: ata.toBase58(), amount_base: amountBase, signature, explorer: explorerTx(signature) };
  }
}
void SystemProgram;
