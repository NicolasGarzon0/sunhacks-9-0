import { config } from "../config";
import type {
  AirdropResult, DepositTx, MarketView, Participant, Position, Preview, SubmitBody, SubmitResult, WalletInfo,
} from "./types";

export class ApiError extends Error {
  constructor(public code: string, public http: number, public detail: string = "", public status?: string) {
    super(`${code}${detail ? `: ${detail}` : ""}`);
    this.name = "ApiError";
  }
}

const TIMEOUT_MS = 12_000;
// Sending a deposit waits for the chain to confirm (devnet can take 20 s or more); the server makes a resend of the same bytes safe.
const SLOW_TIMEOUT_MS = 75_000;

async function request<T>(path: string, init?: RequestInit & { json?: unknown; timeoutMs?: number }): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), init?.timeoutMs ?? TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`${config.apiBase}${path}`, {
      method: init?.json !== undefined ? "POST" : "GET",
      headers: init?.json !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: init?.json !== undefined ? JSON.stringify(init.json) : undefined,
      signal: ctrl.signal,
    });
  } catch {
    throw new ApiError("NETWORK", 0, "could not reach the server");
  } finally {
    clearTimeout(timer);
  }
  let body: { ok?: boolean; code?: string; detail?: string; status?: string } | null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok || !body || body.ok === false) {
    throw new ApiError(body?.code || (res.status >= 500 ? "RPC_ERROR" : "INTERNAL"), res.status, body?.detail || "", body?.status);
  }
  return body as T;
}

const enc = encodeURIComponent;

export const api = {
  market: (token: string) => request<MarketView>(`/${enc(token)}`),
  participants: async (token: string) =>
    (await request<{ participants: Participant[] }>(`/${enc(token)}/participants`)).participants,
  position: (token: string, wallet: string) => request<Position>(`/${enc(token)}/position?wallet=${enc(wallet)}`),
  preview: (token: string, side: number, amountUsd: string) =>
    request<Preview>(`/${enc(token)}/preview?side=${side}&amount_usd=${enc(amountUsd)}`),
  wallet: (token: string, wallet: string) => request<WalletInfo>(`/${enc(token)}/wallet?wallet=${enc(wallet)}`),
  airdrop: (token: string, wallet: string) => request<AirdropResult>(`/${enc(token)}/airdrop`, { json: { wallet }, timeoutMs: SLOW_TIMEOUT_MS }),
  depositTx: (token: string, wallet: string, side: number, amountUsd: string) =>
    request<DepositTx>(`/${enc(token)}/deposit_tx`, { json: { wallet, side, amount_usd: amountUsd }, timeoutMs: 30_000 }),
  depositSubmit: (
    token: string, wallet: string, side: number, amountUsd: string, signed: SubmitBody, displayName?: string,
  ) =>
    request<SubmitResult>(`/${enc(token)}/deposit_submit`, {
      json: { wallet, side, amount_usd: amountUsd, ...signed, ...(displayName ? { display_name: displayName } : {}) },
      timeoutMs: SLOW_TIMEOUT_MS,
    }),
};
export type Api = typeof api;
