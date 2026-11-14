import assert from "node:assert/strict";
import { test } from "node:test";
import { createInbound, type InboundDeps, type SpaceKind } from "../src/inbound.ts";
import { captureLogger, msg, resp } from "./helpers.ts";

function setup(over: Partial<InboundDeps> = {}, kind: SpaceKind = "dm") {
  const cap = captureLogger();
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetchOk: typeof fetch = async (url, init) => {
    calls.push({ url: String(url), init: init as RequestInit });
    return resp(200);
  };
  const inbound = createInbound({
    config: { backendUrl: "http://backend", sharedSecret: "s3cret" },
    logger: cap.logger,
    getSpaceKind: () => kind,
    fetch: fetchOk,
    sleep: async () => undefined,
    ...over,
  });
  return { inbound, calls, cap };
}

const dropReasons = (cap: ReturnType<typeof captureLogger>) =>
  cap.events().filter((e) => e.event === "inbound.dropped").map((e) => e.reason);

test("valid DM is forwarded with payload and secret header", async () => {
  const { inbound, calls } = setup();
  await inbound.handle({}, msg());
  assert.equal(calls.length, 1);
  assert.equal(calls[0]!.url, "http://backend/inbound");
  const headers = calls[0]!.init.headers as Record<string, string>;
  assert.equal(headers["x-gateway-secret"], "s3cret");
  assert.deepEqual(JSON.parse(calls[0]!.init.body as string), {
    messageId: "m1",
    handle: "+15551234567",
    text: "hello",
    receivedAt: "2026-01-01T00:00:00.000Z",
  });
});

test("group message is dropped", async () => {
  const { inbound, calls, cap } = setup({}, "group");
  await inbound.handle({}, msg());
  assert.equal(calls.length, 0);
  assert.deepEqual(dropReasons(cap), ["group"]);
});

test("outbound message is dropped", async () => {
  const { inbound, calls, cap } = setup();
  await inbound.handle({}, msg({ direction: "outbound" }));
  assert.equal(calls.length, 0);
  assert.deepEqual(dropReasons(cap), ["outbound"]);
});

test("undefined sender is dropped and logged", async () => {
  const { inbound, calls, cap } = setup();
  await inbound.handle({}, msg({ sender: undefined }));
  assert.equal(calls.length, 0);
  assert.deepEqual(dropReasons(cap), ["no_sender"]);
});

test("non-text content is dropped", async () => {
  const { inbound, calls, cap } = setup();
  for (const type of ["attachment", "reaction", "read", "addMember", "rename"]) {
    await inbound.handle({}, msg({ id: `m-${type}`, content: { type } }));
  }
  assert.equal(calls.length, 0);
  assert.equal(dropReasons(cap).length, 5);
});

test("dedup: same message.id forwarded once; expires after TTL", async () => {
  let t = 1_000;
  const { inbound, calls, cap } = setup({ now: () => t, dedupTtlMs: 60_000 });
  await inbound.handle({}, msg());
  await inbound.handle({}, msg());
  assert.equal(calls.length, 1);
  assert.deepEqual(dropReasons(cap), ["duplicate"]);
  t += 61_000;
  await inbound.handle({}, msg());
  assert.equal(calls.length, 2);
});

test("backend down: retries with backoff, then drops, never throws", async () => {
  let t = 0;
  let attempts = 0;
  const sleeps: number[] = [];
  const { inbound, cap } = setup({
    now: () => t,
    sleep: async (ms) => {
      sleeps.push(ms);
      t += ms;
    },
    fetch: async () => {
      attempts += 1;
      throw new TypeError("fetch failed");
    },
  });
  await assert.doesNotReject(inbound.handle({}, msg()));
  assert.ok(attempts > 2, `expected several attempts, got ${attempts}`);
  assert.deepEqual(sleeps.slice(0, 3), [500, 1000, 2000]);
  assert.ok(t <= 30_000);
  assert.deepEqual(dropReasons(cap), ["backend_unreachable"]);
});

test("backend recovers after failures: forwarded", async () => {
  let n = 0;
  const { inbound, cap } = setup({
    fetch: async () => (++n < 3 ? resp(503) : resp(200)),
  });
  await inbound.handle({}, msg());
  assert.equal(n, 3);
  assert.ok(cap.events().some((e) => e.event === "inbound.forwarded" && e.attempts === 3));
});

test("backend 4xx is not retried", async () => {
  let n = 0;
  const { inbound, cap } = setup({ fetch: async () => (n++, resp(401)) });
  await inbound.handle({}, msg());
  assert.equal(n, 1);
  assert.deepEqual(dropReasons(cap), ["backend_rejected"]);
});

test("logs mask handles and omit text below debug level", async () => {
  const cap = captureLogger("info");
  const inbound = createInbound({
    config: { backendUrl: "http://b", sharedSecret: "x" },
    logger: cap.logger,
    getSpaceKind: () => "dm",
    fetch: async () => resp(200),
  });
  await inbound.handle({}, msg({ text: "super private words" }));
  const all = cap.lines.join("\n");
  assert.ok(!all.includes("+15551234567"));
  assert.ok(all.includes("***4567"));
  assert.ok(!all.includes("super private words"));
});
