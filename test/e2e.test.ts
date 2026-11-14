import assert from "node:assert/strict";
import { test } from "node:test";
import { createMockBackend } from "../scripts/mock-backend.ts";
import { createInbound } from "../src/inbound.ts";
import { createOutbound } from "../src/outbound.ts";
import { createServer } from "../src/server.ts";
import { captureLogger, close, listen, msg } from "./helpers.ts";

test("local loop: inbound DM -> mock backend -> gateway /send -> outbound (mocked Spectrum)", async () => {
  const cap = captureLogger();
  const SECRET = "loop-secret";
  const delivered: Array<{ to: string; text: string }> = [];

  // Mocked Spectrum: the outbound path opens a DM space and records what gets sent.
  const outbound = createOutbound({
    logger: cap.logger,
    openDm: async (handle) => ({
      send: async (text) => {
        delivered.push({ to: handle, text });
        return { id: `out-${delivered.length}` };
      },
    }),
  });

  const gateway = createServer({
    sharedSecret: SECRET,
    outbound,
    logger: cap.logger,
    health: () => ({ spectrumConnected: true, lastInboundAt: null }),
  });
  const gatewayPort = await listen(gateway);

  const mock = createMockBackend({ gatewayUrl: `http://127.0.0.1:${gatewayPort}`, secret: SECRET, logger: cap.logger });
  const mockPort = await listen(mock.server);

  const inbound = createInbound({
    config: { backendUrl: `http://127.0.0.1:${mockPort}`, sharedSecret: SECRET },
    logger: cap.logger,
    getSpaceKind: (space) => (space as { type: "dm" | "group" }).type,
    onDmContact: (h) => outbound.noteInboundContact(h),
  });

  // Mocked Spectrum inbound stream.
  async function* stream() {
    yield [{ type: "group" }, msg({ id: "g1", text: "ignored group" })] as const;
    yield [{ type: "dm" }, msg({ id: "a1", direction: "outbound", text: "own echo" })] as const;
    yield [{ type: "dm" }, msg({ id: "d1", text: "ping" })] as const;
    yield [{ type: "dm" }, msg({ id: "d1", text: "ping" })] as const; // redelivery
  }
  const pending: Promise<void>[] = [];
  for await (const [space, message] of stream()) pending.push(inbound.handle(space, message));
  await Promise.all(pending);

  // wait for the async echo through /send
  for (let i = 0; i < 50 && mock.sendResults.length === 0; i++) await new Promise((r) => setTimeout(r, 20));

  try {
    assert.equal(mock.received.length, 1, "only the one valid, de-duplicated DM reaches the backend");
    assert.equal(mock.received[0]!.messageId, "d1");
    assert.deepEqual(delivered, [{ to: "+15551234567", text: "echo: ping" }]);
    assert.deepEqual(mock.sendResults[0], { status: 200, body: { ok: true, messageId: "out-1" } });
  } finally {
    await close(mock.server);
    await close(gateway);
  }
});
