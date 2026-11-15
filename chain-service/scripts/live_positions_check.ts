// DEV ONLY. Live devnet check that deposit rule checks, deposit/build and positions never use getProgramAccounts.
//   node --env-file=.env.live --import tsx scripts/live_positions_check.ts
// Uses the PUBLIC devnet RPC (SOLANA_RPC_URL is overridden), creates one throwaway bet, makes one deposit, then reads and builds
// through the real HTTP server and through the backend's RealChainClient. Records every JSON-RPC method the Connection sends and
// fails if getProgramAccounts is among them. Finally refunds, claims and closes the bet so the rent comes back.
// Prints public keys, signatures and method names only, never a secret.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";
import { Keypair } from "@solana/web3.js";
import { ChainService } from "../src/chain.ts";
import { loadConfig } from "../src/config.ts";
import { DepositService } from "../src/deposit.ts";
import { createLogger } from "../src/logger.ts";
import { ConnectionRpc, guardDevnet } from "../src/rpc.ts";
import { createServer } from "../src/server.ts";
import { WalletService } from "../src/wallet.ts";
import { depositFor, envFromProcess } from "./dev_deposit.ts";

const PUBLIC_DEVNET = "https://api.devnet.solana.com";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const cfg = loadConfig({ ...process.env, SOLANA_RPC_URL: PUBLIC_DEVNET });
  const logger = createLogger({ level: "warn" });
  const rpc = ConnectionRpc.create(PUBLIC_DEVNET, logger);
  await guardDevnet(rpc);

  // spy on the lowest level: every JSON-RPC request the Connection makes
  const methods: string[] = [];
  const conn = rpc.conn as unknown as { _rpcRequest: (m: string, a: unknown[]) => Promise<unknown> };
  const original = conn._rpcRequest.bind(conn);
  conn._rpcRequest = (method, args) => { methods.push(method); return original(method, args); };

  const chain = new ChainService({ rpc, cfg, logger });
  const deposits = new DepositService({ rpc, cfg, chain, logger });
  const wallets = new WalletService({ rpc, cfg, chain, logger });
  const server = createServer({ chain, secret: cfg.secret, logger, deposits, wallets });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const call = async (method: string, path: string, body?: unknown) => {
    const res = await fetch(base + path, { method, headers: { "content-type": "application/json", "x-chain-secret": cfg.secret }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: res.status, body: (await res.json()) as any };
  };

  const betId = randomUUID();
  const now = Math.floor(Date.now() / 1000);
  let a: Awaited<ReturnType<typeof depositFor>> | undefined;
  try {
    await chain.createBet({ bet_id: betId, funding_closes_at: now + 900, refund_after: now + 1800, fee_bps: 500, min_stake_base: 1_000_000, max_stake_base: 20_000_000, max_participants: 20 });
    console.log("bet", betId);
    await sleep(500);
    a = await depositFor(envFromProcess({ ...process.env, SOLANA_RPC_URL: PUBLIC_DEVNET }), betId, 0, "2");
    console.log("deposit A", a.wallet, a.signature);
    await sleep(1500);

    const b = Keypair.generate().publicKey.toBase58(); // never deposits: only gets test USDC so the deposit rules can pass
    const junk = Keypair.generate().publicKey.toBase58();
    const air = await wallets.airdrop(b, 5_000_000);
    console.log("airdrop B", b, (air as any).signature);
    await sleep(1500);

    // 1. deposit_tx path: the service's live rule checks + build
    const built = await call("POST", "/deposit/build", { bet_id: betId, wallet: b, side: 1, amount_base: 2_000_000 });
    console.log("deposit/build B", built.status, built.body.ok, "message_b64 length", built.body.message_b64?.length);
    if (built.status !== 200 || !built.body.ok) throw new Error(`deposit/build failed: ${JSON.stringify({ ...built.body, transaction_b64: undefined })}`);
    const other = await call("POST", "/deposit/build", { bet_id: betId, wallet: a.wallet, side: 1, amount_base: 2_000_000 });
    console.log("deposit/build A on the other side ->", other.status, other.body.code, "(expected ALREADY_OTHER_SIDE)");
    if (other.body.code !== "ALREADY_OTHER_SIDE") throw new Error("the existing position was not seen by the rule check");

    // 2. positions: named wallets only
    const pos = await call("POST", `/bets/${betId}/positions`, { depositors: [a.wallet, b, junk] });
    console.log("positions", pos.status, JSON.stringify(pos.body.positions));
    if (pos.body.positions?.length !== 1 || pos.body.positions[0].wallet !== a.wallet || pos.body.positions[0].amount_base !== 2_000_000) throw new Error("unexpected positions");
    const none = await call("POST", `/bets/${betId}/positions`, { depositors: [b, junk] });
    if (none.body.positions?.length !== 0) throw new Error("a wallet that never deposited was listed");
    console.log("positions for wallets that never deposited -> []");

    // 3. the backend's RealChainClient over the same server
    const backend = fileURLToPath(new URL("../../backend", import.meta.url));
    const py = [
      "import asyncio, os, sys", "sys.path.insert(0, '.')",
      "from markets.real_chain import RealChainClient",
      "async def main():",
      "    c = RealChainClient(os.environ['LIVE_URL'], os.environ['CHAIN_SERVICE_SECRET'])",
      "    bet, a, b, j = os.environ['LIVE_BET'], os.environ['LIVE_A'], os.environ['LIVE_B'], os.environ['LIVE_J']",
      "    ps = await c.get_positions(bet, [a, b, j])",
      "    print('python get_positions', [(p.wallet, p.side, p.amount_base) for p in ps])",
      "    d = await c.deposit_build(bet, b, 1, 2_000_000)",
      "    print('python deposit_build ok', bool(d['transaction_b64']), bool(d['message_b64']))",
      "    await c.aclose()",
      "asyncio.run(main())",
    ].join("\n");
    // async on purpose: a blocking child would freeze this process, which is also the HTTP server it talks to
    const { stdout: out } = await promisify(execFile)(`${backend}/.venv/bin/python`, ["-I", "-c", py], {
      cwd: backend, encoding: "utf8", timeout: 120_000,
      env: { ...process.env, PYTHONPATH: backend, LIVE_URL: base, LIVE_BET: betId, LIVE_A: a.wallet, LIVE_B: b, LIVE_J: junk },
    });
    process.stdout.write(out);
    if (!out.includes(`('${a.wallet}', 0, 2000000)`)) throw new Error("the python client did not see the position");
  } finally {
    // tidy up: refund, pay A back, close the bet (rent returns to the payer)
    try {
      if (a) {
        await chain.refundAll(betId); await chain.claim(betId, a.wallet); await chain.closeBet(betId);
        console.log("cleanup: bet refunded, claimed and closed");
      }
    } catch (e) {
      console.log("cleanup failed (the bet stays on devnet):", e instanceof Error ? e.message : String(e));
    }
    server.closeAllConnections(); server.close();
  }

  const used = [...new Set(methods)].sort();
  console.log("JSON-RPC methods used:", used.join(", "));
  console.log("getMultipleAccounts calls:", methods.filter((m) => m === "getMultipleAccounts").length);
  if (methods.includes("getProgramAccounts")) throw new Error("FAIL: getProgramAccounts was called");
  console.log("OK: getProgramAccounts was never called");
}

main().then(() => process.exit(0), (e) => { console.error("ERROR:", e instanceof Error ? e.message : String(e)); process.exit(1); });
