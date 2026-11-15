// npm run fund [-- --check]   Estimate SOL needs, airdrop with backoff, or print faucet instructions.
import * as fs from "fs";
import * as path from "path";
import { LAMPORTS, ROOT, guardDevnet, loadKeypair, log, makeConnection, readDevnet, run, sleep, sol, withRetry, Role } from "./lib/config";

const TARGETS: { role: Role; label: string; sol: number }[] = [
  { role: "upgrade-authority", label: "upgrade authority", sol: 4 },
  { role: "payer", label: "payer", sol: 1 },
  { role: "oracle", label: "oracle", sol: 0.1 },
];

run(async () => {
  const check = process.argv.includes("--check");
  const conn = makeConnection();
  await guardDevnet(conn);
  const d = readDevnet();

  const so = path.join(ROOT, "target/deploy/handshake_pool.so");
  const size = fs.statSync(so).size;
  const rent = (n: number) => withRetry("rent", () => conn.getMinimumBalanceForRentExemption(n));
  const programData = await rent(size + 45);
  const buffer = await rent(size + 37);
  const betRent = await rent(236), vaultRent = await rent(165), posRent = await rent(82);
  const perMarket = betRent + vaultRent + 20 * posRent;
  log(`program .so: ${size} bytes`);
  log(`deploy: ProgramData rent ${sol(programData)} SOL (kept) + temporary buffer ${sol(buffer)} SOL (refunded) + fees`);
  log(`50 markets: worst case rent ${sol(50 * perMarket)} SOL locked until closed (all returned), fees ~0.03 SOL,`);
  log(`            user token accounts ~${sol(await rent(165))} SOL each, never returned`);
  log(`targets: ${TARGETS.map((t) => `${t.label} ${t.sol}`).join(", ")}  = ${TARGETS.reduce((a, t) => a + t.sol, 0)} SOL total\n`);

  const missing: { label: string; pub: string; sol: number }[] = [];
  for (const t of TARGETS) {
    const kp = loadKeypair(t.role);
    let bal = await withRetry("balance", () => conn.getBalance(kp.publicKey));
    log(`${t.label.padEnd(18)} ${kp.publicKey.toBase58()}  ${sol(bal)} SOL (target ${t.sol})`);
    if (check || bal >= t.sol * LAMPORTS) { if (bal < t.sol * LAMPORTS) missing.push({ label: t.label, pub: kp.publicKey.toBase58(), sol: t.sol - bal / LAMPORTS }); continue; }
    let stopped = false;
    while (bal < t.sol * LAMPORTS && !stopped) {
      const ask = Math.min(1, t.sol - bal / LAMPORTS);
      let ok = false;
      for (const wait of [0, 5000, 15000, 45000]) {
        if (wait) { log(`  airdrop failed, waiting ${wait / 1000}s`); await sleep(wait); }
        try {
          const sig = await conn.requestAirdrop(kp.publicKey, Math.floor(ask * LAMPORTS));
          for (let i = 0; i < 30; i++) { const s = (await conn.getSignatureStatuses([sig])).value[0]; if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") break; await sleep(1500); }
          ok = true; break;
        } catch (e: any) { /* rate limited or faucet dry: retry with backoff */ }
      }
      if (!ok) { stopped = true; break; }
      await sleep(2000);
      bal = await withRetry("balance", () => conn.getBalance(kp.publicKey));
      log(`  airdropped, ${t.label} now ${sol(bal)} SOL`);
    }
    if (bal < t.sol * LAMPORTS) missing.push({ label: t.label, pub: kp.publicKey.toBase58(), sol: t.sol - bal / LAMPORTS });
  }
  if (missing.length) {
    log("\nThe command line airdrop is rate limited or not enough. Use the web faucet https://faucet.solana.com (network: Devnet):");
    for (const m of missing) log(`  send ${Math.ceil(m.sol * 100) / 100} SOL to ${m.pub}   (${m.label})`);
    log("Then run: npm run fund -- --check");
    process.exit(2);
  }
  log("\nall balances at target");
});
