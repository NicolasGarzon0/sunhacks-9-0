// npm run topup -- <sol>   Move SOL from the upgrade authority to the payer (devnet only). Prints public info only.
import { Keypair, PublicKey, SystemProgram, Transaction } from "@solana/web3.js";
import { LAMPORTS, explorerTx, guardDevnet, loadKeypair, log, makeConnection, readDevnet, run, sol, withRetry } from "./lib/config";

run(async () => {
  const amount = Number(process.argv.slice(2).find((a) => !a.startsWith("--")) ?? "2");
  if (!(amount > 0 && amount <= 10)) throw new Error("usage: npm run topup -- <sol between 0 and 10>");
  const conn = makeConnection();
  await guardDevnet(conn);
  const from = loadKeypair("upgrade-authority");
  const to = new PublicKey(readDevnet().keys.payer);
  const bal = await conn.getBalance(from.publicKey);
  if (bal < amount * LAMPORTS + 10_000_000) throw new Error(`upgrade authority only has ${sol(bal)} SOL`);
  const tx = new Transaction().add(SystemProgram.transfer({ fromPubkey: from.publicKey, toPubkey: to, lamports: Math.round(amount * LAMPORTS) }));
  tx.feePayer = from.publicKey;
  const sig = await withRetry("topup", async () => {
    tx.recentBlockhash = (await conn.getLatestBlockhash("confirmed")).blockhash;
    tx.signatures = [];
    tx.sign(from);
    return conn.sendRawTransaction(tx.serialize());
  });
  for (let i = 0; i < 60; i++) {
    const s = (await conn.getSignatureStatuses([sig])).value[0];
    if (s?.err) throw new Error("transfer failed: " + JSON.stringify(s.err));
    if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  log(`sent ${amount} SOL  ${from.publicKey.toBase58()} -> ${to.toBase58()}\n  ${explorerTx(sig)}`);
  log(`upgrade authority ${sol(await conn.getBalance(from.publicKey))} SOL, payer ${sol(await conn.getBalance(to))} SOL`);
});
