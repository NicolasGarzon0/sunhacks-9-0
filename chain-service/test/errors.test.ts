import assert from "node:assert/strict";
import { test } from "node:test";
import { ChainError, PROGRAM_ERRORS, httpStatus, mapLogs, toChainError } from "../src/errors.ts";
import { withRetry } from "../src/rpc.ts";

const anchorLog = (name: string, n: number) =>
  `Program log: AnchorError thrown in programs/handshake_pool/src/instructions/deposit.rs:60. Error Code: ${name}. Error Number: ${n}. Error Message: some message.`;

test("every program error maps to the documented service code", () => {
  const expected: Record<string, string> = {
    BelowMin: "BELOW_MIN", AboveMax: "ABOVE_MAX", MarketFull: "MARKET_FULL", AlreadyOtherSide: "ALREADY_OTHER_SIDE",
    MarketNotOpen: "MARKET_NOT_OPEN", WrongState: "WRONG_STATE", NotAWinner: "NOT_A_WINNER", FundingClosed: "FUNDING_CLOSED",
    PastRefundDeadline: "PAST_REFUND_DEADLINE", RefundTooEarly: "REFUND_TOO_EARLY", BothSidesRequired: "BOTH_SIDES_REQUIRED",
    InvalidParams: "INVALID_PARAMS", FeeTooHigh: "INVALID_PARAMS", InvalidSide: "INVALID_PARAMS", AccountNotInitialized: "NOT_FOUND",
    Unauthorized: "UNTRUSTED_BET", WrongMint: "UNTRUSTED_BET", WrongTreasury: "UNTRUSTED_BET", PositionsStillOpen: "WRONG_STATE",
  };
  for (const [name, code] of Object.entries(expected)) {
    assert.equal(mapLogs(["Program log: Instruction: X", anchorLog(name, 6000)])?.code, code, name);
    assert.equal(PROGRAM_ERRORS[name], code);
  }
});

test("Anchor framework errors: constraint errors are INVALID_PARAMS, unknown ones RPC_ERROR", () => {
  assert.equal(mapLogs([anchorLog("ConstraintSeeds", 2006)])?.code, "INVALID_PARAMS");
  assert.equal(mapLogs([anchorLog("SomethingNew", 9999)])?.code, "RPC_ERROR");
});

test("falls back to the custom error number when the message line is missing", () => {
  assert.equal(mapLogs(["Program X failed: custom program error: 0x1770"])?.code, "BELOW_MIN"); // 6000
  assert.equal(mapLogs(["Program X failed: custom program error: 0x1783"])?.code, "PAST_REFUND_DEADLINE"); // 6019
  assert.equal(mapLogs(["Program X failed: custom program error: 0x1784"])?.code, "REFUND_TOO_EARLY"); // 6020
  assert.equal(mapLogs(["Program log: nothing here"]), null);
  assert.equal(mapLogs(undefined), null);
});

test("toChainError reads logs from thrown errors, else wraps the message as RPC_ERROR", () => {
  const e = Object.assign(new Error("Simulation failed"), { logs: [anchorLog("FundingClosed", 6008)] });
  assert.equal(toChainError(e).code, "FUNDING_CLOSED");
  assert.equal(toChainError(new Error("socket hang up")).code, "RPC_ERROR");
  assert.equal(toChainError(new Error("Attempt to debit an account but found no record of a prior credit.")).detail, "a signing account has too little SOL");
  const c = new ChainError("NOT_FOUND", "x");
  assert.equal(toChainError(c), c);
});

test("http status per code", () => {
  assert.equal(httpStatus("UNAUTHORIZED"), 401);
  assert.equal(httpStatus("NOT_FOUND"), 404);
  assert.equal(httpStatus("INVALID_PARAMS"), 400);
  assert.equal(httpStatus("RPC_ERROR"), 502);
  assert.equal(httpStatus("WRONG_STATE"), 409);
});

test("withRetry retries rate limits with backoff, never program rejections", async () => {
  let calls = 0;
  const v = await withRetry("t", async () => { if (++calls < 3) throw new Error("429 Too Many Requests"); return "ok"; }, undefined, [1, 1, 1]);
  assert.equal(v, "ok");
  assert.equal(calls, 3);
  calls = 0;
  await assert.rejects(withRetry("t", async () => { calls++; throw Object.assign(new Error("Simulation failed 503"), { logs: ["x"] }); }, undefined, [1, 1]));
  assert.equal(calls, 1, "a rejection with logs is final");
  calls = 0;
  await assert.rejects(withRetry("t", async () => { calls++; throw new Error("429"); }, undefined, [1, 1]), (e: ChainError) => e.code === "RPC_ERROR");
  assert.equal(calls, 3);
});
