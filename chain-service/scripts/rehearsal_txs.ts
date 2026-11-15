// DEV ONLY, read only, no keys. Used by backend/scripts/rehearsal.py to list a bet's transactions for the report.
//   npx tsx scripts/rehearsal_txs.ts <bet_id> [wallet ...]
// Prints ONE line of JSON: {bet_pda, txs: [{signature, instruction, block_time, ok, link}], positions: {wallet: bool}}.
// `txs` are every transaction that touched the Bet account, oldest first (create_bet, deposits, lock, settle, claims, loser
// closes, close_bet), the instruction name read from the program's own log line. `positions` says whether each wallet's
// Position account still exists on chain. Aborts unless the RPC is devnet (genesis hash).
import { readFileSync } from "node:fs";
import path from "node:path";
import { Connection, PublicKey } from "@solana/web3.js";
import { Pool } from "../src/program.ts";
import { DEVNET_GENESIS } from "../src/rpc.ts";

const devnet = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../contracts/devnet.json"), "utf8"));
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** the public devnet RPC rate limits: back off 2, 4, 8, 16 s */
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

async function main() {
  const [betId, ...wallets] = process.argv.slice(2);
  if (!betId) throw new Error("usage: rehearsal_txs.ts <bet_id> [wallet ...]");
  const conn = new Connection(process.env.SOLANA_RPC_URL || devnet.rpcDefault, { commitment: "confirmed", disableRetryOnRateLimit: true });
  const genesis = await retry(() => conn.getGenesisHash());
  if (genesis !== DEVNET_GENESIS) throw new Error(`ABORT: the RPC is not devnet (genesis ${genesis})`);
  const programId = new PublicKey(devnet.programId);
  const pool = new Pool(programId);
  const pda = pool.betPda(betId);

  const sigs = (await retry(() => conn.getSignaturesForAddress(pda, { limit: 100 }, "confirmed"))).reverse();
  const txs = [];
  for (const s of sigs) {
    const t = await retry(() => conn.getTransaction(s.signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }));
    let instruction = "unknown";
    let top = false;
    for (const line of t?.meta?.logMessages ?? []) {
      if (line === `Program ${programId.toBase58()} invoke [1]`) top = true;
      else if (top && line.startsWith("Program log: Instruction: ")) { instruction = line.slice("Program log: Instruction: ".length); break; }
    }
    txs.push({
      signature: s.signature, instruction, block_time: s.blockTime ?? null, ok: s.err === null,
      link: `https://explorer.solana.com/tx/${s.signature}?cluster=devnet`,
    });
  }
  const positions: Record<string, boolean> = {};
  for (const w of wallets) {
    const acc = await retry(() => conn.getAccountInfo(pool.posPda(pda, new PublicKey(w)), "confirmed"));
    positions[w] = acc !== null;
  }
  console.log(JSON.stringify({ bet_pda: pda.toBase58(), txs, positions }));
}

main().catch((e) => { console.error("ERROR:", e instanceof Error ? e.message : String(e)); process.exit(1); });
