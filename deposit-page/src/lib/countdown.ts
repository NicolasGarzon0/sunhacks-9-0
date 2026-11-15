export const CUTOFF_MS = 30_000; // the server refuses deposits in the last 30 seconds before funding closes

export type FundingPhase = "open" | "closing" | "closed";

/**
 * How far the server clock is ahead of this browser's clock (negative: behind), in ms: server_time minus the local time
 * at fetch. 0 when the server sent no usable time, so the page degrades to the browser clock.
 */
export function clockOffsetMs(serverTime: string | undefined, localAtFetchMs: number): number {
  if (!serverTime) return 0;
  const t = Date.parse(serverTime);
  return Number.isNaN(t) ? 0 : t - localAtFetchMs;
}

export function msLeft(iso: string, now: number): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t - now;
}

export function phase(ms: number): FundingPhase {
  if (ms <= 0) return "closed";
  if (ms < CUTOFF_MS) return "closing";
  return "open";
}

/** 93 s -> "1m 33s", 3700 s -> "1h 01m", 90000 s -> "1d 1h". Fixed width friendly. */
export function formatCountdown(ms: number): string {
  if (ms <= 0) return "0s";
  const total = Math.floor(ms / 1000);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
