// npm run keys [-- --force]   Generate the role keypairs once. Existing keys are never overwritten silently.
import * as fs from "fs";
import * as path from "path";
import { Keypair } from "@solana/web3.js";
import { DEFAULT_RPC, DEVNET_JSON, KEYS_DIR, ROLES, ROOT, DevnetJson, keyPath, log, loadKeypair, run, writeDevnet, writeEnvGenerated } from "./lib/config";

run(async () => {
  const force = process.argv.includes("--force");
  fs.mkdirSync(KEYS_DIR, { recursive: true, mode: 0o700 });
  fs.chmodSync(KEYS_DIR, 0o700);
  const backup = path.join(KEYS_DIR, `backup-${Date.now()}`);
  for (const role of ROLES) {
    const p = keyPath(role);
    if (fs.existsSync(p)) {
      if (!force) { log(`exists   ${role.padEnd(18)} (kept)`); continue; }
      fs.mkdirSync(backup, { recursive: true, mode: 0o700 });
      fs.renameSync(p, path.join(backup, `${role}.json`));
      log(`backup   ${role} -> ${path.relative(ROOT, backup)}/`);
    }
    fs.writeFileSync(p, JSON.stringify(Array.from(Keypair.generate().secretKey)), { mode: 0o600 });
    fs.chmodSync(p, 0o600);
    log(`created  ${role}`);
  }
  const pub = (r: (typeof ROLES)[number]) => loadKeypair(r).publicKey.toBase58();
  const prev: Partial<DevnetJson> = fs.existsSync(DEVNET_JSON) ? JSON.parse(fs.readFileSync(DEVNET_JSON, "utf8")) : {};
  const keys = { payer: pub("payer"), oracle: pub("oracle"), treasury: pub("treasury"), mintAuthority: pub("mint-authority"), upgradeAuthority: pub("upgrade-authority"), program: pub("program-keypair") };
  const programChanged = prev.keys?.program && prev.keys.program !== keys.program;
  writeDevnet({
    cluster: "devnet", rpcDefault: DEFAULT_RPC, decimals: 6, keys,
    // a new program key means a new program: forget the old deployment record
    programId: programChanged ? undefined : prev.programId,
    deploySignature: programChanged ? undefined : prev.deploySignature,
    testUsdcMint: prev.testUsdcMint,
  });
  writeEnvGenerated();
  log("\npublic keys (devnet.json):");
  for (const [k, v] of Object.entries(keys)) log(`  ${k.padEnd(17)} ${v}`);
  log("\nsecrets: contracts/.keys/ (mode 600, gitignored); env values: .keys/env.generated (not printed)");
});
