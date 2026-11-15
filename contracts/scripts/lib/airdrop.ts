// Reusable test USDC airdrop (chunk 3 calls the same function from the backend).
// Idempotent for the token account (creates it only if missing), mints from the mint authority, never closes accounts.
import { Connection, Keypair, PublicKey, Transaction } from "@solana/web3.js";
import { createAssociatedTokenAccountIdempotentInstruction, createMintToCheckedInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { explorerTx, withRetry } from "./config";

export const DEFAULT_AIRDROP_USDC = "25";
export const MAX_AIRDROP_USDC = 100n;
const DECIMALS = 6;

/** "25", 25 or "0.5" -> base units. At most 6 decimals, > 0, <= 100 USDC. Throws otherwise. */
export function parseUsdcAmount(amount: string | number = DEFAULT_AIRDROP_USDC): bigint {
  const s = String(amount).trim();
  if (!/^\d+(\.\d{1,6})?$/.test(s)) throw new Error(`invalid USDC amount "${s}" (use a plain decimal with up to 6 places)`);
  const [w, f = ""] = s.split(".");
  const base = BigInt(w) * 1_000_000n + BigInt(f.padEnd(DECIMALS, "0"));
  if (base <= 0n) throw new Error("amount must be greater than 0");
  if (base > MAX_AIRDROP_USDC * 1_000_000n) throw new Error(`amount exceeds the hard cap of ${MAX_AIRDROP_USDC} USDC per call`);
  return base;
}

export type AirdropArgs = {
  connection: Connection;
  payer: Keypair; // pays fees and the token account rent
  mintAuthority: Keypair;
  mint: PublicKey;
  wallet: string | PublicKey;
  amount?: string | number;
};

export async function airdropUsdc(a: AirdropArgs): Promise<{ signature: string; ata: string; amountBase: bigint; explorer: string }> {
  let owner: PublicKey;
  try { owner = new PublicKey(a.wallet); } catch { throw new Error(`invalid wallet address "${String(a.wallet)}"`); }
  const amountBase = parseUsdcAmount(a.amount);
  const ata = getAssociatedTokenAddressSync(a.mint, owner, true);
  const tx = new Transaction().add(
    createAssociatedTokenAccountIdempotentInstruction(a.payer.publicKey, ata, owner, a.mint),
    createMintToCheckedInstruction(a.mint, ata, a.mintAuthority.publicKey, amountBase, DECIMALS),
  );
  tx.feePayer = a.payer.publicKey;
  const signature = await withRetry("airdrop", async () => {
    const { blockhash } = await a.connection.getLatestBlockhash("confirmed");
    tx.recentBlockhash = blockhash;
    tx.signatures = [];
    tx.sign(a.payer, a.mintAuthority);
    const sig = await a.connection.sendRawTransaction(tx.serialize(), { preflightCommitment: "confirmed" });
    for (let i = 0; i < 60; i++) {
      const s = (await a.connection.getSignatureStatuses([sig])).value[0];
      if (s?.err) throw new Error("airdrop transaction failed: " + JSON.stringify(s.err));
      if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") return sig;
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("airdrop not confirmed in 60s (rate limited?)");
  });
  return { signature, ata: ata.toBase58(), amountBase, explorer: explorerTx(signature) };
}
