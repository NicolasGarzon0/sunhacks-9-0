import assert from "node:assert/strict";
import { test } from "node:test";
import { createOutbound } from "../src/outbound.ts";
import { createServer } from "../src/server.ts";
import { captureLogger, close, listen } from "./helpers.ts";

async function boot(sendImpl?: (text: string) => Promise<{ id: string } | undefined>) {
  const cap = captureLogger();
  const outbound = createOutbound({
    logger: cap.logger,
    openDm: async () => ({ send: sendImpl ?? (async () => ({ id: "m-1" })) }),
  });
  const server = createServer({
    sharedSecret: "s3cret",
    outbound,
    logger: cap.logger,
    health: () => ({ spectrumConnected: true, lastInboundAt: null }),
  });
  const port = await listen(server);
  const post = (body: unknown, secret: string | null = "s3cret", raw = false) =>
    fetch(`http://127.0.0.1:${port}/send`, {
      method: "POST",
      headers: { "content-type": "application/json", ...(secret ? { "x-gateway-secret": secret } : {}) },
      body: raw ? (body as string) : JSON.stringify(body),
    });
  return { server, port, post, cap };
}

const good = { to: "+15551230001", text: "hi", idempotencyKey: "k1" };

test("bad or missing secret -> 401 UNAUTHORIZED, nothing sent", async () => {
  let sends = 0;
  const { server, post } = await boot(async () => (sends++, { id: "x" }));
  try {
    for (const s of ["wrong", null]) {
      const r = await post(good, s);
      assert.equal(r.status, 401);
      assert.equal(((await r.json()) as { code: string }).code, "UNAUTHORIZED");
    }
    assert.equal(sends, 0);
  } finally {
    await close(server);
  }
});

test("bad shapes -> 400 BAD_REQUEST", async () => {
  const { server, post } = await boot();
  try {
    const bodies: unknown[] = [
      {}, { ...good, to: "" }, { ...good, to: 5 }, { ...good, text: "   " },
      { ...good, idempotencyKey: "" }, { to: good.to, text: "x" }, [], "nope",
    ];
    for (const b of bodies) {
      const r = await post(b);
      assert.equal(r.status, 400, JSON.stringify(b));
      assert.equal(((await r.json()) as { code: string }).code, "BAD_REQUEST");
    }
    const r = await post("{not json", "s3cret", true);
    assert.equal(r.status, 400);
  } finally {
    await close(server);
  }
});

test("success and idempotent replay over HTTP", async () => {
  let sends = 0;
  const { server, post } = await boot(async () => ({ id: `m-${++sends}` }));
  try {
    const a = await (await post(good)).json();
    const b = await (await post(good)).json();
    assert.deepEqual(a, { ok: true, messageId: "m-1" });
    assert.deepEqual(b, a);
    assert.equal(sends, 1);
  } finally {
    await close(server);
  }
});

test("NOT_ALLOWED -> 403, RATE_LIMITED -> 429 with Retry-After", async () => {
  const na = await boot(async () => { throw new Error("Target not allowed for this project"); });
  try {
    const r = await na.post(good);
    assert.equal(r.status, 403);
    assert.equal(((await r.json()) as { code: string }).code, "NOT_ALLOWED");
  } finally {
    await close(na.server);
  }
  const rl = await boot(async () => { throw Object.assign(new Error("x"), { name: "RateLimitError", retryAfter: 9 }); });
  try {
    const r = await rl.post(good);
    assert.equal(r.status, 429);
    assert.equal(r.headers.get("retry-after"), "9");
  } finally {
    await close(rl.server);
  }
});

test("GET /health reports status without auth", async () => {
  const { server, port } = await boot();
  try {
    const body = (await (await fetch(`http://127.0.0.1:${port}/health`)).json()) as Record<string, unknown>;
    assert.equal(body.status, "ok");
    assert.equal(body.spectrumConnected, true);
    assert.equal(typeof body.uptimeSeconds, "number");
  } finally {
    await close(server);
  }
});
