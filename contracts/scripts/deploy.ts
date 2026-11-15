// npm run deploy   Sync id, build (--arch v0), deploy to devnet with an explicit upgrade authority, verify.
// The same command upgrades an already deployed program in place (see DEVNET.md).
import { execFileSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { ROOT, explorerAddr, explorerTx, guardDevnet, loadKeypair, keyPath, log, makeConnection, readDevnet, rpcUrl, run, sol, withRetry, writeDevnet, writeEnvGenerated } from "./lib/config";

const PATHS = `${process.env.HOME}/.local/share/solana/install/active_release/bin:${process.env.HOME}/.cargo/bin:${process.env.PATH}`;
const sh = (cmd: string, args: string[]) => execFileSync(cmd, args, { cwd: ROOT, env: { ...process.env, PATH: PATHS }, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });

run(async () => {
  const conn = makeConnection();
  await guardDevnet(conn);
  const d = readDevnet();
  const programKp = loadKeypair("program-keypair");
  const upgrade = loadKeypair("upgrade-authority");
  const id = programKp.publicKey.toBase58();

  // 1. program id sync + build with --arch v0
  fs.copyFileSync(keyPath("program-keypair"), path.join(ROOT, "target/deploy/handshake_pool-keypair.json"));
  fs.chmodSync(path.join(ROOT, "target/deploy/handshake_pool-keypair.json"), 0o600);
  sh("anchor", ["keys", "sync"]);
  log("building (anchor build --arch v0) ...");
  sh("anchor", ["build", "--arch", "v0"]);
  const so = path.join(ROOT, "target/deploy/handshake_pool.so");
  const size = fs.statSync(so).size;

  // 2. balance check
  const bal = await withRetry("balance", () => conn.getBalance(upgrade.publicKey));
  const need = (await conn.getMinimumBalanceForRentExemption(size + 45)) + (await conn.getMinimumBalanceForRentExemption(size + 37)) + 5_000_000;
  log(`upgrade authority ${upgrade.publicKey.toBase58()} has ${sol(bal)} SOL, deploy needs about ${sol(need)} SOL (buffer refunded)`);
  const existing = await conn.getAccountInfo(programKp.publicKey);
  if (!existing && bal < need) throw new Error("upgrade authority is underfunded; run npm run fund (or the web faucet) first");

  // 3. deploy (or upgrade in place when it already exists)
  log(existing ? "program exists: upgrading in place" : "deploying ...");
  const out = sh("solana", [
    "program", "deploy", so, "--program-id", keyPath("program-keypair"),
    "--upgrade-authority", keyPath("upgrade-authority"), "--keypair", keyPath("upgrade-authority"),
    "--url", rpcUrl(), "--commitment", "confirmed",
  ]);
  const sig = /Signature:\s*(\S+)/.exec(out)?.[1];
  log(out.trim().split("\n").filter((l) => !/keypair|secret/i.test(l)).join("\n"));

  // 4. verify with solana program show
  const show = sh("solana", ["program", "show", id, "--url", rpcUrl(), "--keypair", keyPath("upgrade-authority")]);
  log("\n" + show.trim());
  const auth = /Authority:\s*(\S+)/.exec(show)?.[1];
  if (auth !== upgrade.publicKey.toBase58()) throw new Error(`upgrade authority on chain is ${auth}, expected ${upgrade.publicKey.toBase58()}`);
  const len = Number(/Data Length:\s*(\d+)/.exec(show)?.[1]);
  if (!(len >= size)) throw new Error(`on chain data length ${len} is smaller than the local binary ${size}`);

  d.programId = id;
  if (sig) d.deploySignature = sig;
  writeDevnet(d);
  writeEnvGenerated();
  log(`\nOK  program ${id}\n    ${explorerAddr(id)}${sig ? "\n    " + explorerTx(sig) : ""}`);
});
