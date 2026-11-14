import { classifyError, type SendErrorCode } from "./errors.ts";
import type { Logger } from "./logger.ts";

export interface SendRequest {
  to: string;
  text: string;
  idempotencyKey: string;
}

export type SendResult =
  | { ok: true; messageId: string | null }
  | { ok: false; code: SendErrorCode; detail: string; retryAfter?: number };

/** Minimal slice of a Spectrum space that we use. space.send resolves to a Message (with id) or undefined. */
export interface SendableSpace {
  send(text: string): Promise<{ id: string } | undefined>;
}

export interface Limits {
  /** Max sends to one recipient inside perRecipientWindowMs. */
  perRecipientMax: number;
  perRecipientWindowMs: number;
  /** Max sends overall inside globalWindowMs. */
  globalMax: number;
  globalWindowMs: number;
  /** Max messages per rolling 24h (Photon quota is 5000/day; stay well under). */
  globalDailyMax: number;
  /** Max distinct never-contacted recipients per rolling 24h (Photon quota is 50/day/line). */
  newRecipientsPerDay: number;
  idempotencyTtlMs: number;
}

export const DEFAULT_LIMITS: Limits = {
  perRecipientMax: 10,
  perRecipientWindowMs: 60_000,
  globalMax: 60,
  globalWindowMs: 60_000,
  globalDailyMax: 4000,
  newRecipientsPerDay: 45,
  idempotencyTtlMs: 10 * 60_000,
};

const DAY_MS = 24 * 60 * 60_000;

export interface OutboundDeps {
  /** Opens (or creates) a DM space with a handle. Production: imessage(app).user() + space.create(). */
  openDm(handle: string): Promise<SendableSpace>;
  logger: Logger;
  now?: () => number;
  limits?: Partial<Limits>;
}

interface IdemEntry {
  expiresAt: number;
  promise: Promise<SendResult>;
  settled: boolean;
}

export interface Outbound {
  send(req: SendRequest): Promise<SendResult>;
  /** Record that a handle texted us first, so replies to it do not count as a new conversation. */
  noteInboundContact(handle: string): void;
  stats(): { newRecipientsLast24h: number; sendsLast24h: number; idempotencyEntries: number };
}

export function createOutbound(deps: OutboundDeps): Outbound {
  const now = deps.now ?? Date.now;
  const limits: Limits = { ...DEFAULT_LIMITS, ...deps.limits };
  const { logger } = deps;

  const spaces = new Map<string, SendableSpace>();
  const idem = new Map<string, IdemEntry>();
  const perRecipient = new Map<string, number[]>();
  const globalSends: number[] = []; // timestamps, pruned to 24h
  const contacted = new Map<string, number>(); // handles WE opened a conversation with -> time, pruned to 24h (counts toward quota)
  const inboundContacts = new Map<string, number>(); // handles that texted us first -> time (replies are exempt from quota)
  let notAllowedLogged = false;

  const prune = (arr: number[], windowMs: number, t: number) => {
    while (arr.length > 0 && t - (arr[0] as number) >= windowMs) arr.shift();
  };

  const pruneContacted = (t: number) => {
    for (const [h, ts] of contacted) if (t - ts >= DAY_MS) contacted.delete(h);
    for (const [h, ts] of inboundContacts) if (t - ts >= DAY_MS) inboundContacts.delete(h);
  };

  const sweepIdem = (t: number) => {
    for (const [k, v] of idem) if (v.settled && v.expiresAt <= t) idem.delete(k);
  };

  /** Returns a RATE_LIMITED result if any limit would be exceeded; does not consume budget. */
  const checkLimits = (to: string, t: number): SendResult | undefined => {
    const limited = (detail: string, retryAfterMs: number): SendResult => ({
      ok: false,
      code: "RATE_LIMITED",
      detail,
      retryAfter: Math.max(1, Math.ceil(retryAfterMs / 1000)),
    });

    pruneContacted(t);
    prune(globalSends, DAY_MS, t);

    const rec = perRecipient.get(to) ?? [];
    prune(rec, limits.perRecipientWindowMs, t);
    if (rec.length === 0) perRecipient.delete(to);
    else perRecipient.set(to, rec);
    if (rec.length >= limits.perRecipientMax) {
      return limited("per-recipient rate limit", limits.perRecipientWindowMs - (t - (rec[0] as number)));
    }

    const recentGlobal = globalSends.filter((ts) => t - ts < limits.globalWindowMs);
    if (recentGlobal.length >= limits.globalMax) {
      return limited("global rate limit", limits.globalWindowMs - (t - (recentGlobal[0] as number)));
    }
    if (globalSends.length >= limits.globalDailyMax) {
      return limited("global daily limit", DAY_MS - (t - (globalSends[0] as number)));
    }
    if (!contacted.has(to) && !inboundContacts.has(to) && contacted.size >= limits.newRecipientsPerDay) {
      const oldest = Math.min(...contacted.values());
      return limited("new-conversation daily limit", DAY_MS - (t - oldest));
    }
    return undefined;
  };

  const doSend = async (req: SendRequest): Promise<SendResult> => {
    const t = now();
    const blocked = checkLimits(req.to, t);
    if (blocked) return blocked;

    // Consume rate budget at attempt time.
    const rec = perRecipient.get(req.to) ?? [];
    rec.push(t);
    perRecipient.set(req.to, rec);
    globalSends.push(t);

    try {
      let space = spaces.get(req.to);
      if (!space) {
        space = await deps.openDm(req.to);
        spaces.set(req.to, space);
      }
      const sent = await space.send(req.text);
      // Only successful sends count as a new conversation.
      if (!contacted.has(req.to) && !inboundContacts.has(req.to)) contacted.set(req.to, now());
      return { ok: true, messageId: sent?.id ?? null };
    } catch (err) {
      const c = classifyError(err);
      if (c.code === "SEND_FAILED") spaces.delete(req.to); // drop possibly stale cached space
      if (c.code === "NOT_ALLOWED" && !notAllowedLogged) {
        notAllowedLogged = true;
        logger.warn("send.not_allowed_first_seen", {
          errName: err instanceof Error ? err.name : typeof err,
          errCode: c.errCode,
          grpcCode: c.grpcCode,
          detail: c.detail,
        });
      }
      logger.warn("send.failed", {
        to: req.to,
        mappedCode: c.code,
        errCode: c.errCode,
        grpcCode: c.grpcCode,
        retryAfter: c.retryAfter,
        detail: c.detail,
      });
      return {
        ok: false,
        code: c.code,
        detail: c.detail,
        ...(c.retryAfter !== undefined ? { retryAfter: c.retryAfter } : {}),
      };
    }
  };

  return {
    async send(req) {
      const t = now();
      sweepIdem(t);

      const existing = idem.get(req.idempotencyKey);
      if (existing && (!existing.settled || existing.expiresAt > t)) {
        logger.info("send.idempotent_replay", { to: req.to, idempotencyKey: req.idempotencyKey });
        return existing.promise;
      }

      logger.info("send.requested", { to: req.to, textLength: req.text.length, idempotencyKey: req.idempotencyKey });

      const entry: IdemEntry = { expiresAt: Number.POSITIVE_INFINITY, settled: false, promise: undefined as never };
      entry.promise = doSend(req).then((result) => {
        entry.settled = true;
        if (!result.ok && result.code === "RATE_LIMITED") {
          idem.delete(req.idempotencyKey); // never cache rate-limit results
        } else {
          entry.expiresAt = now() + limits.idempotencyTtlMs;
        }
        if (result.ok) logger.info("send.ok", { to: req.to, messageId: result.messageId });
        return result;
      });
      idem.set(req.idempotencyKey, entry);
      return entry.promise;
    },

    noteInboundContact(handle) {
      inboundContacts.set(handle, now());
    },

    stats() {
      const t = now();
      pruneContacted(t);
      prune(globalSends, DAY_MS, t);
      return { newRecipientsLast24h: contacted.size, sendsLast24h: globalSends.length, idempotencyEntries: idem.size };
    },
  };
}
