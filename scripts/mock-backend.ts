// Mock backend: receives POST /inbound from the gateway and echoes back through the gateway's /send.
// Usage: npm run mock-backend   (env: GATEWAY_SHARED_SECRET, GATEWAY_URL, MOCK_BACKEND_PORT)
import http from "node:http";
import { pathToFileURL } from "node:url";
import { createLogger, type Logger } from "../src/logger.ts";
import { secretMatches } from "../src/server.ts";

export interface MockBackendOptions {
  gatewayUrl: string;
  secret: string;
  logger?: Logger;
}

export interface MockBackend {
  server: http.Server;
  received: Array<Record<string, unknown>>;
  sendResults: Array<{ status: number; body: unknown }>;
}

export function createMockBackend(options: MockBackendOptions): MockBackend {
  const logger = options.logger ?? createLogger();
  const received: Array<Record<string, unknown>> = [];
  const sendResults: Array<{ status: number; body: unknown }> = [];

  const server = http.createServer((req, res) => {
    if (req.method !== "POST" || req.url !== "/inbound") {
      res.writeHead(404).end();
      return;
    }
    if (!secretMatches(req.headers["x-gateway-secret"] as string | undefined, options.secret)) {
      res.writeHead(401).end();
      return;
    }
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      let payload: { messageId: string; handle: string; text: string };
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      } catch {
        res.writeHead(400).end();
        return;
      }
      received.push(payload);
      logger.info("mock.inbound", { handle: payload.handle, messageId: payload.messageId, textLength: payload.text.length });
      res.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');

      // Reply through the gateway (after acknowledging the inbound).
      void (async () => {
        try {
          const r = await fetch(`${options.gatewayUrl}/send`, {
            method: "POST",
            headers: { "content-type": "application/json", "x-gateway-secret": options.secret },
            body: JSON.stringify({ to: payload.handle, text: `echo: ${payload.text}`, idempotencyKey: `echo-${payload.messageId}` }),
          });
          const body: unknown = await r.json();
          sendResults.push({ status: r.status, body });
          logger.info("mock.reply", { handle: payload.handle, status: r.status, body: JSON.stringify(body) });
        } catch (err) {
          logger.error("mock.reply_failed", { detail: err instanceof Error ? err.message : String(err) });
        }
      })();
    });
  });
  return { server, received, sendResults };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const secret = process.env.GATEWAY_SHARED_SECRET;
  if (!secret) {
    console.error("Missing required environment variable: GATEWAY_SHARED_SECRET");
    process.exit(2);
  }
  const gatewayUrl = (process.env.GATEWAY_URL ?? `http://localhost:${process.env.PORT ?? "8787"}`).replace(/\/+$/, "");
  const port = Number(process.env.MOCK_BACKEND_PORT ?? "8788");
  const { server } = createMockBackend({ gatewayUrl, secret });
  server.listen(port, () => console.log(`mock backend listening on :${port} (BACKEND_URL should be http://localhost:${port})`));
}
