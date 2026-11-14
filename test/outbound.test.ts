import assert from "node:assert/strict";
import { test } from "node:test";
import { createOutbound, type OutboundDeps, type SendableSpace } from "../src/outbound.ts";
import { captureLogger } from "./helpers.ts";

function setup(over: Partial<OutboundDeps> = {}, space?: SendableSpace) {
  const cap = captureLogger();
  const sent: Array<{ text: string }> = [];
  const opened: string[] = [];
  const defaultSpace: SendableSpace = {
    send: async (text) => {
      sent.push({ text });
      return { id: `msg-${sent.length}` };
    },
  };
  const outbound = createOutbound({
    logger: cap.logger,
    openDm: async (h) => {
      opened.push(h);
      return space ?? defaultSpace;
    },
    ...over,
  });
  return { outbound, sent, opened, cap };
}

const req = (over: Partial<{ to: string; text: string; idempotencyKey: string }> = {}) => ({
  to: "+15551230001",
  text: "hi",
  idempotencyKey: "k1",
  ...over,
});

test("send ok returns messageId and caches the space per handle", async () => {
  const { outbound, opened } = setup();
  assert.deepEqual(await outbound.send(req()), { ok: true, messageId: "msg-1" });
  await outbound.send(req({ idempotencyKey: "k2" }));
  assert.equal(opened.length, 1);
});

test("send resolving undefined reports ok with messageId null", async () => {
  const { outbound } = setup({}, { send: async () => undefined });
  assert.deepEqual(await outbound.send(req()), { ok: true, messageId: null });
});

test("idempotent replay returns original result and does not send again", async () => {
  const { outbound, sent, cap } = setup();
  const a = await outbound.send(req());
  const b = await outbound.send(req());
  assert.deepEqual(a, b);
  assert.equal(sent.length, 1);
  assert.ok(cap.events().some((e) => e.event === "send.idempotent_replay"));
});

test("concurrent requests with the same key send once", async () => {
  const { outbound, sent } = setup();
  const [a, b] = await Promise.all([outbound.send(req()), outbound.send(req())]);
  assert.deepEqual(a, b);
  assert.equal(sent.length, 1);
});

test("idempotency entry expires after 10 minutes", async () => {
  let t = 0;
  const { outbound, sent } = setup({ now: () => t });
  await outbound.send(req());
  t += 10 * 60_000 + 1;
  await outbound.send(req());
  assert.equal(sent.length, 2);
});

test("failures are cached too (NOT_ALLOWED replayed without re-sending)", async () => {
  let calls = 0;
  const { outbound, cap } = setup({}, {
    send: async () => {
      calls += 1;
      throw Object.assign(new Error("Target not allowed for this project"), { code: "preconditionFailed", grpcCode: 9 });
    },
  });
  const a = await outbound.send(req());
  const b = await outbound.send(req());
  assert.equal(a.ok, false);
  assert.equal(!a.ok && a.code, "NOT_ALLOWED");
  assert.deepEqual(a, b);
  assert.equal(calls, 1);
  const failed = cap.events().find((e) => e.event === "send.failed");
  assert.equal(failed?.errCode, "preconditionFailed");
  assert.equal(failed?.grpcCode, 9);
  assert.ok(cap.events().some((e) => e.event === "send.not_allowed_first_seen"));
});

test("SDK rate-limit error maps to RATE_LIMITED with retryAfter and is NOT cached", async () => {
  let calls = 0;
  const { outbound } = setup({}, {
    send: async () => {
      calls += 1;
      throw Object.assign(new Error("slow"), { name: "RateLimitError", retryAfter: 7 });
    },
  });
  const a = await outbound.send(req());
  assert.deepEqual(a, { ok: false, code: "RATE_LIMITED", detail: "slow", retryAfter: 7 });
  await outbound.send(req());
  assert.equal(calls, 2);
});

test("generic failure maps to SEND_FAILED", async () => {
  const { outbound } = setup({}, { send: async () => { throw new Error("kaboom"); } });
  const r = await outbound.send(req());
  assert.equal(!r.ok && r.code, "SEND_FAILED");
});

test("per-recipient rate limit trips and recovers after the window", async () => {
  let t = 0;
  const { outbound, sent } = setup({ now: () => t, limits: { perRecipientMax: 3, perRecipientWindowMs: 60_000 } });
  for (let i = 0; i < 3; i++) assert.equal((await outbound.send(req({ idempotencyKey: `k${i}` }))).ok, true);
  const blocked = await outbound.send(req({ idempotencyKey: "k-over" }));
  assert.equal(!blocked.ok && blocked.code, "RATE_LIMITED");
  assert.ok(!blocked.ok && (blocked.retryAfter ?? 0) >= 1);
  assert.equal(sent.length, 3);
  // other recipient unaffected
  assert.equal((await outbound.send(req({ to: "+15551230002", idempotencyKey: "other" }))).ok, true);
  // RATE_LIMITED not cached: same key succeeds after window
  t += 60_001;
  assert.equal((await outbound.send(req({ idempotencyKey: "k-over" }))).ok, true);
});

test("global rate limit trips", async () => {
  const { outbound } = setup({ limits: { globalMax: 2 } });
  await outbound.send(req({ to: "a1", idempotencyKey: "1" }));
  await outbound.send(req({ to: "a2", idempotencyKey: "2" }));
  const r = await outbound.send(req({ to: "a3", idempotencyKey: "3" }));
  assert.equal(!r.ok && r.code, "RATE_LIMITED");
});

test("new-conversation daily cap refuses at the cap and exempts inbound contacts and repeat recipients", async () => {
  const { outbound, sent } = setup({ limits: { newRecipientsPerDay: 3, globalMax: 1000, perRecipientMax: 1000 } });
  for (let i = 0; i < 3; i++) assert.equal((await outbound.send(req({ to: `new${i}`, idempotencyKey: `n${i}` }))).ok, true);
  const refused = await outbound.send(req({ to: "new3", idempotencyKey: "n3" }));
  assert.equal(!refused.ok && refused.code, "RATE_LIMITED");
  // existing conversation still allowed
  assert.equal((await outbound.send(req({ to: "new0", idempotencyKey: "again" }))).ok, true);
  // someone who texted us first is a reply, not a new conversation
  outbound.noteInboundContact("texted-first");
  assert.equal((await outbound.send(req({ to: "texted-first", idempotencyKey: "reply" }))).ok, true);
  assert.equal(sent.length, 5);
});

test("failed sends do not consume the new-conversation budget", async () => {
  const { outbound } = setup({ limits: { newRecipientsPerDay: 1, perRecipientMax: 1000, globalMax: 1000 } }, {
    send: async () => { throw new Error("Target not allowed for this project"); },
  });
  for (let i = 0; i < 3; i++) {
    const r = await outbound.send(req({ to: `bad${i}`, idempotencyKey: `b${i}` }));
    assert.equal(!r.ok && r.code, "NOT_ALLOWED");
  }
});
