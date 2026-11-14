// Usage: npm run send-dm -- <handle> "text" [--yes]
// Calls the RUNNING gateway's /send over HTTP. Does not open its own Spectrum connection.
import { randomUUID } from "node:crypto";
import { createInterface } from "node:readline/promises";
import { maskHandle } from "../src/logger.ts";

const args = process.argv.slice(2);
const yes = args.includes("--yes");
const positional = args.filter((a) => a !== "--yes");
const [handle, text] = positional;

if (!handle || !text) {
  console.error('Usage: npm run send-dm -- <handle> "text" [--yes]');
  process.exit(2);
}
const secret = process.env.GATEWAY_SHARED_SECRET;
if (!secret) {
  console.error("Missing required environment variable: GATEWAY_SHARED_SECRET");
  process.exit(2);
}
const gatewayUrl = (process.env.GATEWAY_URL ?? `http://localhost:${process.env.PORT ?? "8787"}`).replace(/\/+$/, "");

console.log("Plan:");
console.log(`  POST ${gatewayUrl}/send`);
console.log(`  to:   ${maskHandle(handle)}`);
console.log(`  text: ${JSON.stringify(text)} (${text.length} chars)`);

if (!yes) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const answer = (await rl.question("Send this iMessage? [y/N] ")).trim().toLowerCase();
  rl.close();
  if (answer !== "y" && answer !== "yes") {
    console.log("Aborted.");
    process.exit(1);
  }
}

const res = await fetch(`${gatewayUrl}/send`, {
  method: "POST",
  headers: { "content-type": "application/json", "x-gateway-secret": secret },
  body: JSON.stringify({ to: handle, text, idempotencyKey: randomUUID() }),
  signal: AbortSignal.timeout(30_000),
});
const body = await res.text();
console.log(`HTTP ${res.status}`);
console.log(body);
process.exit(res.ok ? 0 : 1);
