import type { AddressInfo } from "node:net";
import type http from "node:http";
import { createLogger, type Logger } from "../src/logger.ts";
import type { InboundMessage } from "../src/inbound.ts";

export function captureLogger(level: "debug" | "info" | "warn" | "error" = "debug"): { logger: Logger; lines: string[]; events: () => Array<Record<string, unknown>> } {
  const lines: string[] = [];
  const logger = createLogger({ level, write: (l) => lines.push(l) });
  return { logger, lines, events: () => lines.map((l) => JSON.parse(l) as Record<string, unknown>) };
}

export function msg(over: Partial<InboundMessage> & { text?: string } = {}): InboundMessage {
  const { text, ...rest } = over;
  return {
    id: "m1",
    direction: "inbound",
    platform: "imessage",
    timestamp: new Date("2026-01-01T00:00:00.000Z"),
    sender: { id: "+15551234567" },
    content: { type: "text", text: text ?? "hello" },
    ...rest,
  };
}

export async function listen(server: http.Server): Promise<number> {
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  return (server.address() as AddressInfo).port;
}

export const close = (server: http.Server) =>
  new Promise<void>((r) => {
    server.closeAllConnections();
    server.close(() => r());
  });

export const resp = (status: number) => new Response("{}", { status });
