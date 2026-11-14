import type { Logger } from "./logger.ts";

/** Minimal slice of a Spectrum message that the gateway needs. */
export interface InboundMessage {
  id: string;
  direction: string;
  platform: string;
  timestamp: Date;
  sender?: { id: string } | undefined;
  content: { type: string; text?: string };
}

export type SpaceKind = "dm" | "group" | "unknown";

export interface InboundConfig {
  backendUrl: string;
  sharedSecret: string;
}

export interface InboundDeps {
  config: InboundConfig;
  logger: Logger;
  /** Production: platform === "imessage" ? imessage(space).type : "unknown". */
  getSpaceKind(space: unknown, message: InboundMessage): SpaceKind;
  /** Called for each valid DM sender so replies do not count as new conversations. */
  onDmContact?(handle: string): void;
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  dedupTtlMs?: number;
  /** Total retry budget when the backend is down. */
  retryBudgetMs?: number;
  attemptTimeoutMs?: number;
  initialBackoffMs?: number;
}

export interface Inbound {
  /** Never throws. Resolves once the message is forwarded, dropped, or the retry budget is spent. */
  handle(space: unknown, message: InboundMessage): Promise<void>;
  lastInboundAt(): number | null;
}

export function createInbound(deps: InboundDeps): Inbound {
  const { logger, config } = deps;
  const doFetch = deps.fetch ?? fetch;
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const now = deps.now ?? Date.now;
  const dedupTtl = deps.dedupTtlMs ?? 60 * 60_000;
  const retryBudget = deps.retryBudgetMs ?? 30_000;
  const attemptTimeout = deps.attemptTimeoutMs ?? 5_000;
  const initialBackoff = deps.initialBackoffMs ?? 500;

  // message.id -> expiry. In memory only: a restart resets it.
  const seen = new Map<string, number>();
  // Per-handle serialization so a backend outage does not reorder one user's messages.
  const tails = new Map<string, Promise<void>>();
  let lastInbound: number | null = null;

  const sweepSeen = (t: number) => {
    if (seen.size < 500) return;
    for (const [id, exp] of seen) if (exp <= t) seen.delete(id);
  };

  const drop = (reason: string, fields: Record<string, unknown> = {}, level: "debug" | "info" | "warn" = "info") => {
    logger[level]("inbound.dropped", { reason, ...fields });
  };

  const forward = async (payload: Record<string, unknown>, handle: string, messageId: string): Promise<void> => {
    const started = now();
    let delay = initialBackoff;
    let attempt = 0;
    for (;;) {
      attempt += 1;
      let retryable = true;
      let failure: string;
      try {
        const res = await doFetch(`${config.backendUrl}/inbound`, {
          method: "POST",
          headers: { "content-type": "application/json", "x-gateway-secret": config.sharedSecret },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(attemptTimeout),
        });
        if (res.ok) {
          logger.info("inbound.forwarded", { handle, messageId, attempts: attempt, status: res.status });
          return;
        }
        failure = `backend responded ${res.status}`;
        // 4xx (other than 408/429) will not succeed on retry.
        retryable = res.status >= 500 || res.status === 408 || res.status === 429;
        await res.body?.cancel().catch(() => undefined);
      } catch (err) {
        failure = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
      }

      if (!retryable) {
        drop("backend_rejected", { handle, messageId, attempts: attempt, detail: failure }, "warn");
        return;
      }
      if (now() - started + delay > retryBudget) {
        drop("backend_unreachable", { handle, messageId, attempts: attempt, detail: failure }, "warn");
        return;
      }
      logger.warn("inbound.retry", { handle, messageId, attempt, nextDelayMs: delay, detail: failure });
      await sleep(delay);
      delay = Math.min(delay * 2, 8_000);
    }
  };

  const process_ = async (space: unknown, message: InboundMessage): Promise<void> => {
    if (message.direction === "outbound") return drop("outbound", { messageId: message.id }, "debug");
    if (message.content.type !== "text") {
      return drop("non_text", { messageId: message.id, contentType: message.content.type }, "debug");
    }
    const handle = message.sender?.id;
    if (!handle) return drop("no_sender", { messageId: message.id }, "warn");

    const kind = deps.getSpaceKind(space, message);
    if (kind !== "dm") return drop(kind === "group" ? "group" : "not_dm", { messageId: message.id, handle });

    const text = message.content.text;
    if (typeof text !== "string") return drop("no_text", { messageId: message.id, handle }, "warn");

    const t = now();
    sweepSeen(t);
    const exp = seen.get(message.id);
    if (exp !== undefined && exp > t) return drop("duplicate", { messageId: message.id, handle });
    seen.set(message.id, t + dedupTtl);

    lastInbound = t;
    deps.onDmContact?.(handle);
    logger.info("inbound.received", { handle, messageId: message.id, textLength: text.length });
    logger.debug("inbound.text", { handle, messageId: message.id, text });

    await forward(
      { messageId: message.id, handle, text, receivedAt: message.timestamp.toISOString() },
      handle,
      message.id,
    );
  };

  return {
    handle(space, message) {
      const key = message.sender?.id ?? "";
      const prev = tails.get(key) ?? Promise.resolve();
      const run = prev
        .then(() => process_(space, message))
        .catch((err) => {
          logger.error("inbound.error", { messageId: message.id, detail: err instanceof Error ? err.message : String(err) });
        });
      tails.set(key, run);
      void run.then(() => {
        if (tails.get(key) === run) tails.delete(key);
      });
      return run;
    },
    lastInboundAt: () => lastInbound,
  };
}
