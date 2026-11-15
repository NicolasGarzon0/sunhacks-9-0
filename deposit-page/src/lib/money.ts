export interface Limits { minBase: number; maxBase: number }

export type ParsedAmount =
  | { ok: true; base: number; text: string }
  | { ok: false; reason: "empty" | "format" | "below_min" | "above_max" };

/** Parses a dollar amount string (up to 6 decimals) into integer base units (6 decimals). Never uses floats. */
export function parseAmount(input: string, limits: Limits): ParsedAmount {
  const s = input.trim().replace(/^\$/, "").trim();
  if (s === "") return { ok: false, reason: "empty" };
  const m = /^(\d{1,9})(?:\.(\d{1,6}))?$/.exec(s);
  if (!m) return { ok: false, reason: "format" };
  const base = Number(m[1]) * 1_000_000 + Number((m[2] ?? "").padEnd(6, "0"));
  if (base < limits.minBase) return { ok: false, reason: "below_min" };
  if (base > limits.maxBase) return { ok: false, reason: "above_max" };
  return { ok: true, base, text: s };
}

/** "5000000" base units -> "5.00" (2 to 6 decimals, trailing zeros trimmed down to 2). */
export function formatBase(base: number): string {
  const whole = Math.floor(base / 1_000_000);
  let frac = String(base % 1_000_000).padStart(6, "0").replace(/0+$/, "");
  if (frac.length < 2) frac = frac.padEnd(2, "0");
  return `${whole}.${frac}`;
}

/** Whole dollars for chips: "$5". */
export function dollars(usd: string): string {
  return `$${usd}`;
}

/** Share of the pool on a side, as a whole percent. */
export function sharePercent(side: number, total: number): number {
  return total > 0 ? Math.round((side / total) * 100) : 0;
}
