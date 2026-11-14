import assert from "node:assert/strict";
import { test } from "node:test";
import { maskHandle, maskText } from "../src/logger.ts";

test("maskHandle keeps last 4", () => {
  assert.equal(maskHandle("+15551234567"), "***4567");
  assert.equal(maskHandle("ab"), "***");
});

test("maskText masks phones and emails inside error text", () => {
  const out = maskText("Target not allowed: +1 (555) 123-4567 and bob@example.com");
  assert.ok(!out.includes("555"));
  assert.ok(!out.includes("bob@example.com"));
  assert.ok(out.includes("***4567"));
});

test("maskText leaves message ids (uuid-like) intact", () => {
  const id = "spc-msg-a1b2c3d4-4a93-96c9-90e87f25deac";
  assert.equal(maskText(`sent ${id}`), `sent ${id}`);
  assert.equal(maskText("id spc-msg-e7d95469-a16f-4b36-aa85-609ea85883c2"), "id spc-msg-e7d95469-a16f-4b36-aa85-609ea85883c2");
});
