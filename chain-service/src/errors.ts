// Service error codes and the mapping from Anchor program errors, simulation logs and RPC failures.
export const CODES = [
  "BELOW_MIN", "ABOVE_MAX", "MARKET_FULL", "ALREADY_OTHER_SIDE", "MARKET_NOT_OPEN", "WRONG_STATE", "NOT_A_WINNER", "NOT_FOUND",
  "FUNDING_CLOSED", "PAST_REFUND_DEADLINE", "REFUND_TOO_EARLY", "BOTH_SIDES_REQUIRED", "UNTRUSTED_BET", "INVALID_PARAMS", "RPC_ERROR",
  "UNAUTHORIZED", "BAD_REQUEST",
  // sponsored deposits and the airdrop (chunk 3B)
  "NO_TOKEN_ACCOUNT", "INSUFFICIENT_USDC", "TX_EXPIRED", "TX_REJECTED", "SUBMIT_FAILED", "RATE_LIMITED", "AIRDROP_DISABLED",
] as const;
export type Code = (typeof CODES)[number];

export class ChainError extends Error {
  constructor(public code: Code, public detail: string) {
    super(`${code}: ${detail}`);
  }
}

/** Anchor error name (from "Error Code: X" in the logs) -> service code. */
export const PROGRAM_ERRORS: Record<string, Code> = {
  BelowMin: "BELOW_MIN",
  AboveMax: "ABOVE_MAX",
  MarketFull: "MARKET_FULL",
  AlreadyOtherSide: "ALREADY_OTHER_SIDE",
  MarketNotOpen: "MARKET_NOT_OPEN",
  WrongState: "WRONG_STATE",
  NotAWinner: "NOT_A_WINNER",
  InvalidSide: "INVALID_PARAMS",
  FundingClosed: "FUNDING_CLOSED",
  BothSidesRequired: "BOTH_SIDES_REQUIRED",
  FeeTooHigh: "INVALID_PARAMS",
  InvalidParams: "INVALID_PARAMS",
  Unauthorized: "UNTRUSTED_BET",
  MathOverflow: "RPC_ERROR",
  WrongMint: "UNTRUSTED_BET",
  WrongTreasury: "UNTRUSTED_BET",
  WrongDestination: "INVALID_PARAMS",
  WrongRentReceiver: "UNTRUSTED_BET",
  PositionsStillOpen: "WRONG_STATE",
  PastRefundDeadline: "PAST_REFUND_DEADLINE",
  RefundTooEarly: "REFUND_TOO_EARLY",
  AccountNotInitialized: "NOT_FOUND",
  AccountNotSigner: "INVALID_PARAMS",
};
const PROGRAM_ERROR_NUMBERS: string[] = [
  "BelowMin", "AboveMax", "MarketFull", "AlreadyOtherSide", "MarketNotOpen", "WrongState", "NotAWinner", "InvalidSide", "FundingClosed",
  "BothSidesRequired", "FeeTooHigh", "InvalidParams", "Unauthorized", "MathOverflow", "WrongMint", "WrongTreasury", "WrongDestination",
  "WrongRentReceiver", "PositionsStillOpen", "PastRefundDeadline", "RefundTooEarly",
]; // 6000 + index, same order as the program's error enum

/** Returns the mapped error when the logs contain an Anchor error, else null. */
export function mapLogs(logs: readonly string[] | undefined | null): ChainError | null {
  if (!logs?.length) return null;
  for (const line of logs) {
    const m = /Error Code: (\w+)\. Error Number: (\d+)\. Error Message: (.*?)\.?$/.exec(line);
    if (m) {
      const name = m[1]!;
      const code = PROGRAM_ERRORS[name] ?? (name.startsWith("Constraint") ? "INVALID_PARAMS" : "RPC_ERROR");
      return new ChainError(code, `${name}: ${m[3]}`);
    }
  }
  for (const line of logs) {
    const m = /custom program error: 0x([0-9a-f]+)/i.exec(line);
    if (m) {
      const n = parseInt(m[1]!, 16);
      const name = n >= 6000 ? PROGRAM_ERROR_NUMBERS[n - 6000] : undefined;
      if (name) return new ChainError(PROGRAM_ERRORS[name]!, name);
    }
  }
  return null;
}

/** Any thrown value -> ChainError. Simulation logs win over message text. */
export function toChainError(e: unknown): ChainError {
  if (e instanceof ChainError) return e;
  const logs: string[] | undefined = (e as { logs?: string[] } | null)?.logs;
  const fromLogs = mapLogs(logs);
  if (fromLogs) return fromLogs;
  const msg = e instanceof Error ? e.message : String(e);
  if (/insufficient (funds|lamports)|no record of a prior credit/i.test(msg)) return new ChainError("RPC_ERROR", "a signing account has too little SOL");
  return new ChainError("RPC_ERROR", msg.replace(/\s+/g, " ").slice(0, 200));
}

export function httpStatus(code: Code): number {
  switch (code) {
    case "UNAUTHORIZED": return 401;
    case "BAD_REQUEST":
    case "TX_REJECTED":
    case "INVALID_PARAMS": return 400;
    case "RATE_LIMITED": return 429;
    case "SUBMIT_FAILED": return 502;
    case "NOT_FOUND": return 404;
    case "RPC_ERROR": return 502;
    default: return 409;
  }
}
