export type LogLevel = "debug" | "info" | "warn" | "error";

const ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

/** Keep only the last 4 characters of a handle, e.g. "+15551234567" -> "***4567". */
export function maskHandle(handle: string): string {
  if (handle.length <= 4) return "***";
  return `***${handle.slice(-4)}`;
}

// Phone-like digit runs (optionally with +, spaces, dashes, dots, parens) inside free text such as error messages.
const PHONE_RE = /(?<![\w-])\+?\d[\d\s().-]{5,}\d(?![\w-])/g;
// Email handles inside free text.
const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;

/** Mask phone numbers and emails embedded in arbitrary text (raw SDK error messages, etc.). */
export function maskText(text: string): string {
  return text
    .replace(PHONE_RE, (m) => (m.replace(/\D/g, "").length >= 10 ? maskHandle(m.replace(/[\s().-]/g, "")) : m))
    .replace(EMAIL_RE, (m) => maskHandle(m));
}

const HANDLE_KEYS = new Set(["handle", "to", "sender", "recipient"]);

function sanitize(key: string, value: unknown): unknown {
  if (typeof value === "string") {
    return HANDLE_KEYS.has(key) ? maskHandle(value) : maskText(value);
  }
  return value;
}

export interface Logger {
  debug(event: string, fields?: Record<string, unknown>): void;
  info(event: string, fields?: Record<string, unknown>): void;
  warn(event: string, fields?: Record<string, unknown>): void;
  error(event: string, fields?: Record<string, unknown>): void;
}

export interface LoggerOptions {
  level?: LogLevel;
  write?: (line: string) => void;
}

/**
 * One JSON object per line. Handles are masked to the last 4 chars and phone-like
 * sequences in any string field are masked too. Message text must only ever be passed
 * in a field called `text` at debug level; callers log `textLength` otherwise.
 */
export function createLogger(options: LoggerOptions = {}): Logger {
  const min = ORDER[options.level ?? "info"];
  const write = options.write ?? ((line: string) => process.stdout.write(`${line}\n`));
  const emit = (level: LogLevel, event: string, fields: Record<string, unknown> = {}) => {
    if (ORDER[level] < min) return;
    const entry: Record<string, unknown> = { ts: new Date().toISOString(), level, event };
    for (const [key, value] of Object.entries(fields)) entry[key] = sanitize(key, value);
    write(JSON.stringify(entry));
  };
  return {
    debug: (e, f) => emit("debug", e, f),
    info: (e, f) => emit("info", e, f),
    warn: (e, f) => emit("warn", e, f),
    error: (e, f) => emit("error", e, f),
  };
}

export function parseLogLevel(value: string | undefined): LogLevel {
  return value === "debug" || value === "info" || value === "warn" || value === "error" ? value : "info";
}
