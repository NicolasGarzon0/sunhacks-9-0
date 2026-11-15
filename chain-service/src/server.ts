import { createHash, timingSafeEqual } from "node:crypto";
import http from "node:http";
import type { ChainService } from "./chain.ts";
import type { DepositService, SubmitArgs } from "./deposit.ts";
import type { WalletService } from "./wallet.ts";
import { ChainError, httpStatus, toChainError } from "./errors.ts";
import type { Logger } from "./logger.ts";

const MAX_BODY = 64 * 1024; // a positions request may name up to 1000 wallets
const sha = (s: string) => createHash("sha256").update(s).digest();
export const secretMatches = (given: string | undefined, expected: string): boolean => given !== undefined && timingSafeEqual(sha(given), sha(expected));

function reply(res: http.ServerResponse, status: number, body: unknown) {
  const json = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(json) });
  res.end(json);
}
const failBody = (e: ChainError) => ({ ok: false as const, code: e.code, detail: e.detail });

function readBody(req: http.IncomingMessage): Promise<string | null> {
  return new Promise((resolve) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY) { resolve(null); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", () => resolve(null));
  });
}

function intField(body: Record<string, unknown>, name: string): number {
  const v = body[name];
  if (typeof v !== "number" || !Number.isSafeInteger(v)) throw new ChainError("BAD_REQUEST", `${name} must be an integer`);
  return v;
}

export interface ServerDeps { chain: ChainService; secret: string; logger: Logger; deposits?: DepositService; wallets?: WalletService }

export function createServer(deps: ServerDeps): http.Server {
  const { chain, logger } = deps;
  return http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://localhost");
      const path = url.pathname.replace(/\/+$/, "") || "/";
      if (req.method === "GET" && path === "/health") {
        try {
          return reply(res, 200, await chain.health());
        } catch (e) {
          return reply(res, 200, { ok: false, code: "RPC_ERROR", detail: toChainError(e).detail });
        }
      }
      if (!secretMatches(req.headers["x-chain-secret"] as string | undefined, deps.secret)) {
        logger.warn("request.unauthorized", { path });
        return reply(res, 401, failBody(new ChainError("UNAUTHORIZED", "missing or invalid X-Chain-Secret")));
      }

      let body: Record<string, unknown> = {};
      if (req.method === "POST") {
        const raw = await readBody(req);
        if (raw === null) return reply(res, 400, failBody(new ChainError("BAD_REQUEST", "body unreadable or too large")));
        try {
          const parsed = raw.trim() === "" ? {} : JSON.parse(raw);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
          body = parsed as Record<string, unknown>;
        } catch {
          return reply(res, 400, failBody(new ChainError("BAD_REQUEST", "body must be a JSON object")));
        }
      }

      const send = async (fn: () => Promise<unknown>) => {
        try {
          return reply(res, 200, await fn());
        } catch (e) {
          const err = toChainError(e);
          return reply(res, httpStatus(err.code), failBody(err));
        }
      };

      if (deps.wallets && req.method === "GET" && path.startsWith("/wallet/")) {
        const w = deps.wallets;
        return send(() => w.wallet(decodeURIComponent(path.slice("/wallet/".length))));
      }
      if (deps.wallets && req.method === "POST" && path === "/airdrop") {
        const w = deps.wallets;
        return send(() => w.airdrop(String(body.wallet ?? ""), body.amount_base));
      }
      if (deps.deposits && req.method === "POST" && (path === "/deposit/build" || path === "/deposit/submit")) {
        const d = deps.deposits;
        const base = () => {
          if (typeof body.bet_id !== "string" || typeof body.wallet !== "string") throw new ChainError("BAD_REQUEST", "bet_id and wallet are required");
          return { bet_id: body.bet_id, wallet: body.wallet, side: intField(body, "side"), amount_base: intField(body, "amount_base") };
        };
        if (path === "/deposit/build") return send(() => d.build(base()));
        return send(() => {
          const str = (k: string) => (typeof body[k] === "string" ? (body[k] as string) : undefined);
          const a: SubmitArgs = { ...base(), signed_tx_b64: str("signed_tx_b64"), message_b64: str("message_b64"), user_signature_b64: str("user_signature_b64") };
          return d.submit(a);
        });
      }

      if (req.method === "POST" && path === "/bets") {
        return send(() => {
          const id = body.bet_id;
          if (typeof id !== "string") throw new ChainError("BAD_REQUEST", "bet_id must be a UUID string");
          return chain.createBet({
            bet_id: id, funding_closes_at: intField(body, "funding_closes_at"), refund_after: intField(body, "refund_after"),
            fee_bps: intField(body, "fee_bps"), min_stake_base: intField(body, "min_stake_base"), max_stake_base: intField(body, "max_stake_base"),
            max_participants: intField(body, "max_participants"),
          });
        });
      }
      const m = /^\/bets\/([0-9a-fA-F-]{36})(?:\/([a-z_]+))?$/.exec(path);
      if (m) {
        const id = m[1]!, action = m[2];
        if (req.method === "GET" && !action) return send(async () => ({ ok: true, ...(await chain.getBet(id)) }));
        if (req.method === "POST") {
          switch (action) {
            case "positions": return send(() => chain.positions(id, body.depositors));
            case "lock": return send(() => chain.lock(id));
            case "settle": return send(() => chain.settle(id, intField(body, "winning_side")));
            case "refund_all": return send(() => chain.refundAll(id));
            case "claim": return send(() => chain.claim(id, body.wallet));
            case "close_loser_position": return send(() => chain.closeLoserPosition(id, body.wallet));
            case "close_bet": return send(() => chain.closeBet(id));
          }
        }
      }
      return reply(res, 404, failBody(new ChainError("BAD_REQUEST", "not found")));
    } catch (e) {
      logger.error("server.error", { detail: e instanceof Error ? e.message : String(e) });
      if (!res.headersSent) reply(res, 500, failBody(new ChainError("RPC_ERROR", "internal error")));
    }
  });
}
