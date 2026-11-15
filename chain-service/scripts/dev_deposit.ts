// DEV ONLY (until the deposit page exists in chunk 3B).
// npm run dev-deposit -- <bet_id> <side:usdc> [<side:usdc> ...]
//   each side:usdc makes one brand new wallet with ZERO SOL, gives it test USDC, and deposits for it.
// The payer is the fee payer and rent_payer, the user signs: message frozen, user signs first, then the payer.
// Env: SOLANA_RPC_URL, PROGRAM_ID, TEST_USDC_MINT, PAYER_SECRET_KEY, MINT_AUTHORITY_SECRET_KEY. Prints public keys and signatures only.
import { Connection, Keypair, PublicKey, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import { createAssociatedTokenAccountIdempotentInstruction, createMintToCheckedInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Pool } from "../src/program.ts";
import { DEVNET_GENESIS } from "../src/rpc.ts";

export interface DevDepositEnv { conn: Connection; pool: Pool; mint: PublicKey; payer: Keypair; mintAuthority: Keypair }

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** the public devnet RPC rate limits: back off 2, 4, 8, 16 s on 429 and network errors */
async function retry<T>(fn: () => Promise<T>): Promise<T> {
  for (let i = 0; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (i < 4 && /429|too many|rate limit|fetch failed|timed? ?out|ECONNRESET/i.test(String(e))) { await sleep(2000 * 2 ** i); continue; }
      throw e;
    }
  }
}
const key = (b64: string | undefined, name: string) => {
  if (!b64) throw new Error(`Missing required environment variable: ${name}`);
  return Keypair.fromSecretKey(Uint8Array.from(Buffer.from(b64, "base64")));
};

export function envFromProcess(env: NodeJS.ProcessEnv = process.env): DevDepositEnv {
  const programId = new PublicKey(env.PROGRAM_ID ?? (() => { throw new Error("Missing required environment variable: PROGRAM_ID"); })());
  const conn = new Connection(env.SOLANA_RPC_URL || "https://api.devnet.solana.com", { commitment: "confirmed", disableRetryOnRateLimit: true });
  return {
    conn, pool: new Pool(programId), mint: new PublicKey(env.TEST_USDC_MINT ?? (() => { throw new Error("Missing required environment variable: TEST_USDC_MINT"); })()),
    payer: key(env.PAYER_SECRET_KEY, "PAYER_SECRET_KEY"), mintAuthority: key(env.MINT_AUTHORITY_SECRET_KEY, "MINT_AUTHORITY_SECRET_KEY"),
  };
}

async function submit(conn: Connection, tx: VersionedTransaction): Promise<string> {
  for (let attempt = 0; ; attempt++) {
    try {
      const sig = await retry(() => conn.sendRawTransaction(tx.serialize(), { preflightCommitment: "confirmed" }));
      for (let i = 0; i < 90; i++) {
        const s = (await retry(() => conn.getSignatureStatuses([sig]))).value[0];
        if (s?.err) throw new Error("transaction failed on chain: " + JSON.stringify(s.err));
        if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") return sig;
        await sleep(1000);
      }
      throw new Error("not confirmed in 90s");
    } catch (e) {
      if (attempt < 3 && /429|too many|fetch failed|timed? ?out/i.test(String(e))) { await sleep(2000 * 2 ** attempt); continue; }
      throw e;
    }
  }
}

async function frozen(conn: Connection, feePayer: PublicKey, ixs: any[]): Promise<VersionedTransaction> {
  const { blockhash } = await retry(() => conn.getLatestBlockhash("confirmed"));
  return new VersionedTransaction(new TransactionMessage({ payerKey: feePayer, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message());
}

/** Fund (test USDC) and deposit for one wallet. `user` defaults to a fresh zero SOL wallet. */
export async function depositFor(e: DevDepositEnv, betUuid: string, side: number, usdc: string, user: Keypair = Keypair.generate()) {
  const amountBase = BigInt(Math.round(Number(usdc) * 1_000_000));
  const bet = e.pool.betPda(betUuid);
  const ata = getAssociatedTokenAddressSync(e.mint, user.publicKey, true);
  // 1. token account (payer pays) + test USDC (mint authority)
  const fund = await frozen(e.conn, e.payer.publicKey, [
    createAssociatedTokenAccountIdempotentInstruction(e.payer.publicKey, ata, user.publicKey, e.mint),
    createMintToCheckedInstruction(e.mint, ata, e.mintAuthority.publicKey, amountBase, 6),
  ]);
  fund.sign([e.payer, e.mintAuthority]);
  const fundSig = await submit(e.conn, fund);
  // 2. the deposit: payer is fee payer and rent_payer, the user is the depositor
  const ix = await e.pool.depositIx({ depositor: user.publicKey, rentPayer: e.payer.publicKey, bet, mint: e.mint, side, amount: amountBase });
  const tx = await frozen(e.conn, e.payer.publicKey, [ix]);
  tx.sign([user]); // user first
  tx.sign([e.payer]); // then the payer, same frozen message
  const signature = await submit(e.conn, tx);
  return { wallet: user.publicKey.toBase58(), side, amount_base: Number(amountBase), signature, fund_signature: fundSig, user };
}

async function main() {
  const [betId, ...specs] = process.argv.slice(2).filter((a) => !a.startsWith("--"));
  if (!betId || specs.length === 0) throw new Error("usage: npm run dev-deposit -- <bet_id> <side:usdc> [<side:usdc> ...]");
  const e = envFromProcess();
  if ((await e.conn.getGenesisHash()) !== DEVNET_GENESIS) throw new Error("ABORT: the RPC is not devnet");
  for (const spec of specs) {
    const m = /^([01]):(\d+(?:\.\d{1,6})?)$/.exec(spec);
    if (!m) throw new Error(`bad spec "${spec}", expected side:usdc like 1:6.5`);
    const r = await depositFor(e, betId, Number(m[1]), m[2]!);
    const { user: _secret, ...pub } = r; // never print the wallet secret
    console.log(JSON.stringify(pub));
    await sleep(400);
  }
}

if (process.argv[1]?.endsWith("dev_deposit.ts")) {
  main().then(() => process.exit(0), (err) => { console.error("ERROR:", err instanceof Error ? err.message : String(err)); process.exit(1); });
}
