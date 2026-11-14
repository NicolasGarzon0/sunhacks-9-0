import { createHash, timingSafeEqual } from "node:crypto";
import http from "node:http";
import type { Logger } from "./logger.ts";
import type { Outbound, SendResult } from "./outbound.ts";

export interface ServerDeps {
  sharedSecret: string;
  outbound: Outbound;
  logger: Logger;
  health(): { spectrumConnected: boolean; lastInboundAt: string | null };
  startedAt?: number;
}

const MAX_BODY = 16 * 1024;
const MAX_TEXT = 5000;

const sha = (s: string) => createHash("sha256").update(s).digest();
export const secretMatches = (given: string | undefined, expected: string): boolean =>
  given !== undefined && timingSafeEqual(sha(given), sha(expected));

function reply(res: http.ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) {
  const json = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(json), ...headers });
  res.end(json);
}

const fail = (code: string, detail: string) => ({ ok: false as const, code, detail });

function statusFor(result: SendResult): number {
  if (result.ok) return 200;
  switch (result.code) {
    case "NOT_ALLOWED":
      return 403;
    case "RATE_LIMITED":
      return 429;
    default:
      return 502;
  }
}

function readBody(req: http.IncomingMessage): Promise<string | null> {
  return new Promise((resolve) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) {
        resolve(null);
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", () => resolve(null));
  });
}

export function parseSendBody(raw: string): { ok: true; value: { to: string; text: string; idempotencyKey: string } } | { ok: false; detail: string } {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { ok: false, detail: "body must be valid JSON" };
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) return { ok: false, detail: "body must be a JSON object" };
  const { to, text, idempotencyKey } = data as Record<string, unknown>;
  if (typeof to !== "string" || to.trim() === "" || to.length > 128 || /\s/.test(to)) {
    return { ok: false, detail: "`to` must be a non-empty string handle (no whitespace, max 128 chars)" };
  }
  if (typeof text !== "string" || text.trim() === "" || text.length > MAX_TEXT) {
    return { ok: false, detail: `\`text\` must be a non-empty string (max ${MAX_TEXT} chars)` };
  }
  if (typeof idempotencyKey !== "string" || idempotencyKey === "" || idempotencyKey.length > 200) {
    return { ok: false, detail: "`idempotencyKey` must be a non-empty string (max 200 chars)" };
  }
  return { ok: true, value: { to, text, idempotencyKey } };
}

export function createServer(deps: ServerDeps): http.Server {
  const startedAt = deps.startedAt ?? Date.now();

  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://localhost");

      if (req.method === "GET" && url.pathname === "/health") {
        const h = deps.health();
        return reply(res, 200, {
          status: "ok",
          spectrumConnected: h.spectrumConnected,
          lastInboundAt: h.lastInboundAt,
          uptimeSeconds: Math.round((Date.now() - startedAt) / 1000),
          ...deps.outbound.stats(),
        });
      }

      if (req.method === "POST" && url.pathname === "/send") {
        if (!secretMatches(req.headers["x-gateway-secret"] as string | undefined, deps.sharedSecret)) {
          deps.logger.warn("send.unauthorized", {});
          return reply(res, 401, fail("UNAUTHORIZED", "missing or invalid X-Gateway-Secret"));
        }
        const raw = await readBody(req);
        if (raw === null) return reply(res, 400, fail("BAD_REQUEST", "body unreadable or too large"));
        const parsed = parseSendBody(raw);
        if (!parsed.ok) return reply(res, 400, fail("BAD_REQUEST", parsed.detail));

        const result = await deps.outbound.send(parsed.value);
        const headers: Record<string, string> = {};
        if (!result.ok && result.code === "RATE_LIMITED" && result.retryAfter !== undefined) {
          headers["retry-after"] = String(result.retryAfter);
        }
        return reply(res, statusFor(result), result, headers);
      }

      return reply(res, 404, fail("BAD_REQUEST", "not found"));
    } catch (err) {
      deps.logger.error("server.error", { detail: err instanceof Error ? err.message : String(err) });
      if (!res.headersSent) reply(res, 500, fail("SEND_FAILED", "internal error"));
    }
  });
}
