import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { createServer, secretMatches } from "../src/server.ts";
import { loadConfig, ConfigError } from "../src/config.ts";
import { BET_ID, putBet, setup } from "./helpers.ts";
import { Keypair } from "@solana/web3.js";

async function boot() {
  const s = setup();
  const server = createServer({ chain: s.chain, secret: s.cfg.secret, logger: { debug() {}, info() {}, warn() {}, error() {} } });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const port = (server.address() as AddressInfo).port;
  const call = (method: string, path: string, body?: unknown, secret: string | null = s.cfg.secret) =>
    fetch(`http://127.0.0.1:${port}${path}`, { method, headers: { "content-type": "application/json", ...(secret ? { "x-chain-secret": secret } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
  return { s, call, close: () => new Promise<void>((r) => { server.closeAllConnections(); server.close(() => r()); }) };
}

test("/health needs no secret; everything else does (401 with a JSON error)", async () => {
  const { call, close } = await boot();
  const h = await call("GET", "/health", undefined, null);
  assert.equal(h.status, 200);
  const hb: any = await h.json();
  assert.equal(hb.ok, true);
  assert.equal(hb.cluster, "devnet");
  assert.equal(typeof hb.payer_sol, "number");
  assert.equal(typeof hb.oracle_sol, "number");
  for (const [m, p] of [["GET", `/bets/${BET_ID}`], ["POST", "/bets"], ["POST", `/bets/${BET_ID}/lock`]] as const) {
    const r = await call(m, p, m === "POST" ? {} : undefined, null);
    assert.equal(r.status, 401);
    assert.deepEqual(await r.json(), { ok: false, code: "UNAUTHORIZED", detail: "missing or invalid X-Chain-Secret" });
    assert.equal((await call(m, p, m === "POST" ? {} : undefined, "wrong")).status, 401);
  }
  await close();
});

test("errors are {ok:false, code, detail}; reads and writes route to the service", async () => {
  const { s, call, close } = await boot();
  let r = await call("GET", `/bets/${BET_ID}`);
  assert.equal(r.status, 404);
  assert.deepEqual(await r.json(), { ok: false, code: "NOT_FOUND", detail: "unknown bet" });
  await putBet(s, { state: "open" });
  r = await call("GET", `/bets/${BET_ID}`);
  const b: any = await r.json();
  assert.equal(b.ok, true);
  assert.equal(b.state, "open");
  r = await call("POST", `/bets/${BET_ID}/lock`, {});
  assert.equal(r.status, 200);
  assert.equal(((await r.json()) as any).already, false);
  r = await call("POST", `/bets/${BET_ID}/settle`, { winning_side: "x" });
  assert.equal(r.status, 400);
  assert.equal(((await r.json()) as any).code, "BAD_REQUEST");
  r = await call("POST", `/bets/${BET_ID}/claim`, { wallet: Keypair.generate().publicKey.toBase58() });
  assert.equal(r.status, 409); // bet is open: WRONG_STATE
  assert.equal(((await r.json()) as any).code, "WRONG_STATE");
  r = await call("POST", `/bets/${BET_ID}/positions`, { depositors: [Keypair.generate().publicKey.toBase58()] });
  assert.equal(((await r.json()) as any).positions.length, 0);
  r = await call("POST", `/bets/${BET_ID}/positions`, {}); // the depositor list is required
  assert.equal(r.status, 400);
  assert.equal(((await r.json()) as any).code, "BAD_REQUEST");
  assert.equal((await call("GET", `/bets/${BET_ID}/positions`)).status, 404); // no scan of the program any more
  r = await call("GET", "/nope");
  assert.equal(r.status, 404);
  await close();
});

test("secretMatches is exact and timing safe", () => {
  assert.equal(secretMatches("a", "a"), true);
  assert.equal(secretMatches("a", "b"), false);
  assert.equal(secretMatches(undefined, "a"), false);
});

test("config: names the bad variable and never echoes a value", () => {
  const good = setup().cfg;
  const env: Record<string, string> = {
    SOLANA_RPC_URL: "https://api.devnet.solana.com", PROGRAM_ID: good.programId.toBase58(), TEST_USDC_MINT: good.mint.toBase58(),
    PAYER_SECRET_KEY: Buffer.from(good.payer.secretKey).toString("base64"), ORACLE_SECRET_KEY: Buffer.from(good.oracle.secretKey).toString("base64"),
    TREASURY_PUBKEY: good.treasury.toBase58(), CHAIN_SERVICE_SECRET: "0123456789abcdef0123",
  };
  assert.equal(loadConfig(env).port, 8789);
  const missing = { ...env }; delete missing.PROGRAM_ID;
  assert.throws(() => loadConfig(missing), /PROGRAM_ID/);
  const bad = { ...env, PAYER_SECRET_KEY: "c2VjcmV0LXZhbHVl" };
  assert.throws(() => loadConfig(bad), (e: Error) => e instanceof ConfigError && /PAYER_SECRET_KEY/.test(e.message) && !e.message.includes("c2VjcmV0"));
  assert.throws(() => loadConfig({ ...env, CHAIN_SERVICE_SECRET: "short" }), /at least 16/);
  assert.throws(() => loadConfig({ ...env, PORT: "99999" }), /PORT/);
});
