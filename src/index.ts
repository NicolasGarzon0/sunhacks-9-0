import { Spectrum, type Space } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { ConfigError, loadConfig } from "./config.ts";
import { createInbound, type InboundMessage, type SpaceKind } from "./inbound.ts";
import { createLogger } from "./logger.ts";
import { createOutbound } from "./outbound.ts";
import { createServer } from "./server.ts";

let config;
try {
  config = loadConfig();
} catch (err) {
  if (err instanceof ConfigError) {
    console.error(err.message);
    process.exit(1);
  }
  throw err;
}

const logger = createLogger({ level: config.logLevel });
const startedAt = Date.now();

const app = await Spectrum({
  projectId: config.projectId,
  projectSecret: config.projectSecret,
  providers: [imessage.config()],
});
const im = imessage(app);

// Best-effort: the SDK exposes no connection state. "true" means Spectrum() resolved and the
// inbound loop has not ended or thrown. It is not a live link check.
let spectrumConnected = true;

const outbound = createOutbound({
  logger,
  openDm: async (handle) => {
    const user = await im.user(handle);
    return im.space.create(user);
  },
});

const inbound = createInbound({
  logger,
  config: { backendUrl: config.backendUrl, sharedSecret: config.sharedSecret },
  getSpaceKind: (space, message): SpaceKind =>
    message.platform === "imessage" ? imessage(space as Space).type : "unknown",
  onDmContact: (handle) => outbound.noteInboundContact(handle),
});

const server = createServer({
  sharedSecret: config.sharedSecret,
  outbound,
  logger,
  startedAt,
  health: () => {
    const last = inbound.lastInboundAt();
    return { spectrumConnected, lastInboundAt: last === null ? null : new Date(last).toISOString() };
  },
});
server.listen(config.port, () => logger.info("gateway.listening", { port: config.port }));

let shuttingDown = false;
const shutdown = async (signal: string) => {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info("gateway.shutdown", { signal });
  server.close();
  server.closeAllConnections();
  try {
    await app.stop();
  } catch (err) {
    logger.error("gateway.stop_failed", { detail: err instanceof Error ? err.message : String(err) });
  }
  process.exit(0);
};
process.on("SIGINT", () => void shutdown("SIGINT"));
process.on("SIGTERM", () => void shutdown("SIGTERM"));

try {
  for await (const [space, message] of app.messages) {
    try {
      // Not awaited: handle() never throws and serializes per sender.
      void inbound.handle(space, message as unknown as InboundMessage);
    } catch (err) {
      logger.error("inbound.loop_error", { detail: err instanceof Error ? err.message : String(err) });
    }
  }
  logger.error("gateway.stream_ended", {});
} catch (err) {
  logger.error("gateway.stream_failed", { detail: err instanceof Error ? err.message : String(err) });
}
spectrumConnected = false;
if (!shuttingDown) process.exit(1);
