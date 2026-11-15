import assert from "node:assert/strict";
import { test } from "node:test";
import { ConfigError, loadConfig } from "../src/config.ts";

const base = { PROJECT_ID: "p", PROJECT_SECRET: "s", BACKEND_URL: "http://localhost:9/", GATEWAY_SHARED_SECRET: "g" };

test("loads and normalizes", () => {
  const c = loadConfig(base);
  assert.equal(c.backendUrl, "http://localhost:9");
  assert.equal(c.port, 8787);
});

test("fails fast naming the missing variable", () => {
  for (const name of Object.keys(base)) {
    const env: Record<string, string> = { ...base };
    delete env[name];
    assert.throws(() => loadConfig(env), (e: unknown) => e instanceof ConfigError && e.message.includes(name));
  }
});

test("rejects bad PORT and BACKEND_URL", () => {
  assert.throws(() => loadConfig({ ...base, PORT: "abc" }), /PORT/);
  assert.throws(() => loadConfig({ ...base, BACKEND_URL: "not a url" }), /BACKEND_URL/);
});

test("BACKEND_URL without a scheme (Render private host:port) gets http://", () => {
  const cfg = loadConfig({ PROJECT_ID: "p", PROJECT_SECRET: "s", BACKEND_URL: "backend:10000", GATEWAY_SHARED_SECRET: "g" });
  assert.equal(cfg.backendUrl, "http://backend:10000");
});
