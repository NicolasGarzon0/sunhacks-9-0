export const FAST_MS = 5_000; // open or locked
export const SLOW_MS = 15_000; // settled, refunded, failed
export const MAX_BACKOFF_MS = 60_000;
export const RECONNECT_AFTER = 3;

export function baseInterval(status: string | undefined): number {
  return status === "open" || status === "locked" ? FAST_MS : SLOW_MS;
}

/** Delay before the next market refresh: the base interval, doubled for every consecutive failure, capped. */
export function nextDelay(status: string | undefined, failures: number): number {
  const base = baseInterval(status);
  if (failures <= 0) return base;
  return Math.min(base * 2 ** failures, MAX_BACKOFF_MS);
}

export function isReconnecting(failures: number): boolean {
  return failures >= RECONNECT_AFTER;
}
