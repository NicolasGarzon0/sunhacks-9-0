import assert from "node:assert/strict";
import { test } from "node:test";
import { classifyError } from "../src/errors.ts";

const err = (name: string, message: string, extra: Record<string, unknown> = {}) =>
  Object.assign(new Error(message), { name, ...extra });

test("Target not allowed message maps to NOT_ALLOWED (any class, keeps code/grpcCode)", () => {
  const r = classifyError(err("ValidationError", "Target not allowed for this project", { code: "preconditionFailed", grpcCode: 9 }));
  assert.equal(r.code, "NOT_ALLOWED");
  assert.equal(r.errCode, "preconditionFailed");
  assert.equal(r.grpcCode, 9);
});

test("Target not allowed wins over a rate-limit shaped error", () => {
  assert.equal(classifyError(err("RateLimitError", "Target not allowed for this project")).code, "NOT_ALLOWED");
});

test("RateLimitError by name, with retryAfter", () => {
  const r = classifyError(err("RateLimitError", "slow down", { retryAfter: 12 }));
  assert.deepEqual([r.code, r.retryAfter], ["RATE_LIMITED", 12]);
});

test("RESOURCE_EXHAUSTED grpcCode 8 maps to RATE_LIMITED", () => {
  assert.equal(classifyError(err("Error", "x", { grpcCode: 8 })).code, "RATE_LIMITED");
});

test("quota ErrorCode strings map to RATE_LIMITED", () => {
  for (const code of ["dailyLimitExceeded", "recipientLimitExceeded", "newContactThrottled", "burstRateExceeded"]) {
    assert.equal(classifyError(err("IMessageError", "x", { code })).code, "RATE_LIMITED", code);
  }
});

test("everything else maps to SEND_FAILED", () => {
  assert.equal(classifyError(err("ConnectionError", "unreachable", { grpcCode: 14 })).code, "SEND_FAILED");
  assert.equal(classifyError(err("AuthenticationError", "bad token", { code: "unauthenticated" })).code, "SEND_FAILED");
  assert.equal(classifyError("boom").code, "SEND_FAILED");
  assert.equal(classifyError(undefined).code, "SEND_FAILED");
});
