import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import * as ed from "@noble/ed25519";
import { CUTOFF_MS, clockOffsetMs, formatCountdown, msLeft, phase } from "../src/lib/countdown";
import { DOCUMENTED_CODES, ERROR_COPY, UNKNOWN_COPY, errorCopy } from "../src/lib/errors";
import { base58Decode, base58Encode, formatKickoff, fromBase64, hueFor, initials, shortAddr, toBase64 } from "../src/lib/format";
import { formatBase, parseAmount, sharePercent } from "../src/lib/money";
import { MAX_BACKOFF_MS, RECONNECT_AFTER, isReconnecting, nextDelay } from "../src/lib/polling";
import { REFUND_REASONS, refundReasonCopy, statusToView } from "../src/lib/status";
import { matchRoute } from "../src/router";
import { LocalDevWalletProvider } from "../src/wallet/LocalDevWalletProvider";
import { createWalletProvider } from "../src/wallet";

const L = { minBase: 1_000_000, maxBase: 20_000_000 };

describe("parseAmount", () => {
  it.each([
    ["1", 1_000_000], ["5", 5_000_000], ["20", 20_000_000], ["5.5", 5_500_000], ["$7.25", 7_250_000], [" 12.000001 ", 12_000_001],
  ])("accepts %s", (s, base) => {
    expect(parseAmount(s, L)).toMatchObject({ ok: true, base });
  });
  it("reports empty, format, below min and above max", () => {
    expect(parseAmount("", L)).toEqual({ ok: false, reason: "empty" });
    for (const bad of ["abc", "-1", "1.0000001", "1,5", "5.", ".5", "1e3", "0x10"]) expect(parseAmount(bad, L)).toEqual({ ok: false, reason: "format" });
    expect(parseAmount("0.99", L)).toEqual({ ok: false, reason: "below_min" });
    expect(parseAmount("0", L)).toEqual({ ok: false, reason: "below_min" });
    expect(parseAmount("20.01", L)).toEqual({ ok: false, reason: "above_max" });
  });
  it("uses the remaining room when the wallet already holds a stake", () => {
    expect(parseAmount("5", { minBase: 1_000_000, maxBase: 4_000_000 })).toEqual({ ok: false, reason: "above_max" });
  });
  it("formats base units", () => {
    expect(formatBase(5_000_000)).toBe("5.00");
    expect(formatBase(5_500_000)).toBe("5.50");
    expect(formatBase(1_234_567)).toBe("1.234567");
    expect(sharePercent(1, 4)).toBe(25);
    expect(sharePercent(0, 0)).toBe(0);
  });
});

describe("countdown", () => {
  it("formats", () => {
    expect(formatCountdown(0)).toBe("0s");
    expect(formatCountdown(45_000)).toBe("45s");
    expect(formatCountdown(93_000)).toBe("1m 33s");
    expect(formatCountdown(3_700_000)).toBe("1h 01m");
    expect(formatCountdown(90_000_000)).toBe("1d 1h");
  });
  it("has phases around the 30 second cutoff", () => {
    expect(phase(CUTOFF_MS + 1)).toBe("open");
    expect(phase(CUTOFF_MS - 1)).toBe("closing");
    expect(phase(0)).toBe("closed");
    expect(phase(-5)).toBe("closed");
  });
  it("computes ms left", () => {
    expect(msLeft("2026-01-01T00:01:00Z", Date.parse("2026-01-01T00:00:00Z"))).toBe(60_000);
    expect(msLeft("garbage", 1)).toBe(0);
  });
  it("offsets the clock by server_time minus the local time at fetch", () => {
    const local = Date.parse("2026-01-01T00:00:00Z");
    expect(clockOffsetMs("2026-01-01T00:10:00.000Z", local)).toBe(600_000); // the server is 10 minutes ahead
    expect(clockOffsetMs("2025-12-31T23:59:30.250Z", local)).toBe(-29_750); // behind, with milliseconds
    expect(clockOffsetMs("2026-01-01T00:00:00.123456Z", local)).toBe(123); // microseconds from an older server are fine
    expect(clockOffsetMs(undefined, local)).toBe(0); // an older backend: fall back to the browser clock
    expect(clockOffsetMs("garbage", local)).toBe(0);
    // a browser one hour behind still counts down the server's 90 s, not the browser's 1 h 01 m
    const server = Date.parse("2026-01-01T00:10:00Z");
    const off = clockOffsetMs("2026-01-01T00:10:00Z", server - 3_600_000);
    expect(msLeft("2026-01-01T00:11:30Z", server - 3_600_000 + off)).toBe(90_000);
  });
});

describe("error copy", () => {
  const doc = readFileSync(resolve(__dirname, "../../docs/deposit-page-api.md"), "utf8");
  const table = doc.slice(doc.indexOf("## Errors and suggested messages"), doc.indexOf("## Blockhash expiry"));
  const documented = new Set<string>();
  for (const row of table.split("\n").filter((l) => l.startsWith("| `"))) {
    for (const m of row.split("|")[1].matchAll(/`([A-Z_]+)`/g)) documented.add(m[1]);
  }
  it("covers every code in the API doc", () => {
    expect(documented.size).toBeGreaterThan(15);
    for (const code of documented) {
      expect(DOCUMENTED_CODES as readonly string[], code).toContain(code);
      expect(typeof ERROR_COPY[code as keyof typeof ERROR_COPY], code).toBe("function");
      expect(errorCopy(code).length).toBeGreaterThan(5);
      expect(errorCopy(code)).not.toBe(UNKNOWN_COPY);
    }
  });
  it("has copy for the codes the prompt lists, never shows a raw code, and falls back for unknown", () => {
    for (const code of ["BELOW_MIN", "ABOVE_MAX", "MARKET_FULL", "ALREADY_OTHER_SIDE", "MARKET_NOT_OPEN", "FUNDING_CLOSED", "NO_TOKEN_ACCOUNT",
      "INSUFFICIENT_USDC", "TX_EXPIRED", "TX_REJECTED", "SUBMIT_FAILED", "AIRDROP_LIMIT", "AIRDROP_DISABLED", "RATE_LIMITED"]) {
      expect(errorCopy(code)).not.toContain(code);
      expect(errorCopy(code)).not.toBe(UNKNOWN_COPY);
    }
    expect(errorCopy("SOMETHING_NEW")).toBe(UNKNOWN_COPY);
    expect(errorCopy("BELOW_MIN", { minUsd: "2.00" })).toContain("$2.00");
    expect(errorCopy("ABOVE_MAX", { maxUsd: "20.00" })).toContain("$20.00");
  });
});

describe("status to view", () => {
  it("maps statuses and failures", () => {
    expect(statusToView({})).toBe("loading");
    for (const s of ["open", "locked", "settled", "refunded", "failed"]) expect(statusToView({ status: s })).toBe(s);
    expect(statusToView({ status: "game_confirmed" })).toBe("locked");
    expect(statusToView({ errorHttp: 404, errorCode: "BAD_TOKEN" })).toBe("invalid");
    expect(statusToView({ errorHttp: 410, errorCode: "LINK_EXPIRED" })).toBe("expired");
    expect(statusToView({ errorHttp: 0, errorCode: "NETWORK" })).toBe("network");
    expect(statusToView({ errorHttp: 502, errorCode: "RPC_ERROR" })).toBe("network");
  });
  it("explains every refund reason in plain words", () => {
    expect(REFUND_REASONS.sort()).toEqual(["cancelled", "no_result", "one_sided", "postponed", "tie", "too_few_participants"]);
    for (const r of REFUND_REASONS) expect(refundReasonCopy(r)).not.toContain("_");
    expect(refundReasonCopy("weird")).toContain("could not be settled");
  });
});

describe("polling", () => {
  it("uses 5 s while open or locked and 15 s afterwards", () => {
    expect(nextDelay("open", 0)).toBe(5000);
    expect(nextDelay("locked", 0)).toBe(5000);
    for (const s of ["settled", "refunded", "failed"]) expect(nextDelay(s, 0)).toBe(15000);
  });
  it("backs off on errors with a cap", () => {
    expect(nextDelay("open", 1)).toBe(10_000);
    expect(nextDelay("open", 2)).toBe(20_000);
    expect(nextDelay("open", 10)).toBe(MAX_BACKOFF_MS);
    expect(nextDelay("settled", 5)).toBe(MAX_BACKOFF_MS);
  });
  it("shows the reconnecting banner after repeated failures", () => {
    expect(isReconnecting(RECONNECT_AFTER - 1)).toBe(false);
    expect(isReconnecting(RECONNECT_AFTER)).toBe(true);
  });
});

describe("format and router", () => {
  it("encodes base58 like Solana", () => {
    expect(base58Encode(new Uint8Array(32))).toBe("11111111111111111111111111111111");
    expect(base58Encode(new TextEncoder().encode("Hello World!"))).toBe("2NEpo7TZRRrLZSi2U");
    expect(new TextDecoder().decode(base58Decode("2NEpo7TZRRrLZSi2U"))).toBe("Hello World!");
    expect(base58Decode("11111111111111111111111111111111").length).toBe(32);
  });
  it("round trips base64", () => {
    const b = new Uint8Array([0, 1, 2, 250, 255]);
    expect(Array.from(fromBase64(toBase64(b)))).toEqual(Array.from(b));
  });
  it("shortens, badges and formats", () => {
    expect(shortAddr("CQ8CX6unDrykKKVnH7CdSTFYnXdXJik62AWFN6pwaGno")).toBe("CQ8C…aGno");
    expect(initials("Arizona Cardinals")).toBe("C");
    expect(initials("")).toBe("?");
    expect(hueFor("Seahawks")).toBe(hueFor("Seahawks"));
    expect(hueFor("Seahawks")).toBeLessThan(360);
    expect(formatKickoff("2026-11-15T21:05:00Z", "en-US")).toMatch(/Nov 15|Nov 16/);
    expect(formatKickoff("nope")).toBe("");
  });
  it("matches routes", () => {
    expect(matchRoute("/")).toEqual({ name: "home" });
    expect(matchRoute("/b/abc.def")).toEqual({ name: "bet", token: "abc.def" });
    expect(matchRoute("/b/abc/")).toEqual({ name: "bet", token: "abc" });
    expect(matchRoute("/b/")).toEqual({ name: "notfound" });
    expect(matchRoute("/x")).toEqual({ name: "notfound" });
  });
});

describe("LocalDevWalletProvider", () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
  };
  it("signs the message and the signature verifies", async () => {
    const w = new LocalDevWalletProvider(mem());
    expect(w.ready).toBe(false);
    await w.connect();
    expect(w.ready && w.address).toBeTruthy();
    const msg = new Uint8Array([1, 2, 3, 4, 5]);
    const res = await w.signDeposit("tx", toBase64(msg));
    expect(res).toMatchObject({ message_b64: toBase64(msg) });
    const sig = fromBase64((res as { user_signature_b64: string }).user_signature_b64);
    expect(sig.length).toBe(64);
    expect(await ed.verifyAsync(sig, msg, base58Decode(w.address as string))).toBe(true);
  });
  it("persists the key between instances and resets on disconnect", async () => {
    const store = mem();
    const a = new LocalDevWalletProvider(store); await a.connect();
    const b = new LocalDevWalletProvider(store); await b.connect();
    expect(b.address).toBe(a.address);
    await a.disconnect();
    expect(a.address).toBeNull();
    const c = new LocalDevWalletProvider(store); await c.connect();
    expect(c.address).not.toBe(b.address);
  });
  it("falls back to memory when localStorage throws", async () => {
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => { throw new Error("blocked"); } };
    const w = new LocalDevWalletProvider(broken);
    await w.connect();
    expect(w.address).toBeTruthy();
    await w.disconnect();
    await w.connect();
    expect(w.address).toBeTruthy();
  });
  it("notifies subscribers", async () => {
    const w = new LocalDevWalletProvider(mem());
    let n = 0;
    const off = w.subscribe(() => n++);
    await w.connect(); off(); await w.disconnect();
    expect(n).toBe(1);
  });
  it("selects the provider from the mode", () => {
    const local = createWalletProvider("local");
    expect(local.isTest).toBe(true);
    expect(local.requiresLogin).toBe(false);
    const privy = createWalletProvider("privy");
    expect(privy.isTest).toBe(false);
    expect(privy.requiresLogin).toBe(true);
    expect(privy.address).toBeNull();
  });
});
