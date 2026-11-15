
export interface ErrorContext { minUsd?: string; maxUsd?: string }

/** Every code in the table of docs/deposit-page-api.md, plus the page's own NETWORK / INTERNAL / NOT_FOUND. */
export const DOCUMENTED_CODES = [
  "BAD_TOKEN", "LINK_EXPIRED", "BAD_REQUEST", "INVALID_WALLET", "INVALID_SIDE", "BAD_DISPLAY_NAME", "INVALID_PARAMS",
  "TX_REJECTED", "MARKET_NOT_OPEN", "FUNDING_CLOSED", "BELOW_MIN", "ABOVE_MAX", "MARKET_FULL", "ALREADY_OTHER_SIDE",
  "NO_TOKEN_ACCOUNT", "INSUFFICIENT_USDC", "TX_EXPIRED", "AIRDROP_DISABLED", "AIRDROP_LIMIT", "RATE_LIMITED",
  "RPC_ERROR", "SUBMIT_FAILED", "UNTRUSTED_BET",
] as const;

export const PAGE_CODES = ["NETWORK", "INTERNAL", "NOT_FOUND", "WALLET"] as const;
export type KnownCode = (typeof DOCUMENTED_CODES)[number] | (typeof PAGE_CODES)[number];

const SERVER = "Something went wrong on our side. Please try again.";

export const ERROR_COPY: Record<KnownCode, (c: ErrorContext) => string> = {
  BAD_TOKEN: () => "This link is not valid.",
  LINK_EXPIRED: () => "This bet is over and the link has expired.",
  BAD_REQUEST: () => "Enter a valid dollar amount.",
  INVALID_WALLET: () => "That wallet address is not valid.",
  INVALID_SIDE: () => "Pick a team.",
  BAD_DISPLAY_NAME: () => "Use a short plain name (up to 24 characters, no links).",
  INVALID_PARAMS: () => "Check the amount and try again.",
  TX_REJECTED: () => "We could not verify that transaction. Please start the deposit again.",
  MARKET_NOT_OPEN: () => "Betting is closed for this game.",
  FUNDING_CLOSED: () => "Betting just closed.",
  BELOW_MIN: (c) => `The minimum is $${c.minUsd ?? "1.00"}.`,
  ABOVE_MAX: (c) => `The most one wallet can put in is $${c.maxUsd ?? "20.00"}.`,
  MARKET_FULL: () => "This bet is full.",
  ALREADY_OTHER_SIDE: () => "You already picked the other team in this bet.",
  NO_TOKEN_ACCOUNT: () => "Get some test USDC first.",
  INSUFFICIENT_USDC: () => "You do not have enough USDC.",
  TX_EXPIRED: () => "That took too long. Please confirm again.",
  AIRDROP_DISABLED: () => "Test USDC is not available right now.",
  AIRDROP_LIMIT: () => "Test USDC is not available right now.",
  RATE_LIMITED: () => "Too many tries. Wait a moment.",
  RPC_ERROR: () => SERVER,
  SUBMIT_FAILED: () => SERVER,
  UNTRUSTED_BET: () => SERVER,
  NETWORK: () => "We could not reach the server. Check your connection and try again.",
  INTERNAL: () => SERVER,
  NOT_FOUND: () => "Deposits are not available on this server right now.",
  WALLET: () => "We could not sign with your wallet. Please try again.",
};

export const UNKNOWN_COPY = "Something unexpected happened. Please try again.";

export function errorCopy(code: string, ctx: ErrorContext = {}): string {
  const fn = (ERROR_COPY as Record<string, ((c: ErrorContext) => string) | undefined>)[code];
  return fn ? fn(ctx) : UNKNOWN_COPY;
}

/** Codes that make a deposit impossible right now (the form should say so, not just flash an error). */
export const BLOCKING_CODES: ReadonlySet<string> = new Set([
  "MARKET_NOT_OPEN", "FUNDING_CLOSED", "MARKET_FULL", "ALREADY_OTHER_SIDE", "NO_TOKEN_ACCOUNT", "INSUFFICIENT_USDC",
  "ABOVE_MAX", "BELOW_MIN",
]);
