import { parseLogLevel, type LogLevel } from "./logger.ts";

export interface Config {
  projectId: string;
  projectSecret: string;
  backendUrl: string;
  sharedSecret: string;
  port: number;
  logLevel: LogLevel;
}

export class ConfigError extends Error {}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name]?.trim();
  if (!value) throw new ConfigError(`Missing required environment variable: ${name}`);
  return value;
}

/** Reads and validates env. Throws ConfigError naming the offending variable. Never logs values. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const projectId = required(env, "PROJECT_ID");
  const projectSecret = required(env, "PROJECT_SECRET");
  const backendUrlRaw = required(env, "BACKEND_URL");
  const sharedSecret = required(env, "GATEWAY_SHARED_SECRET");

  let backendUrl: string;
  try {
    backendUrl = new URL(backendUrlRaw).toString().replace(/\/+$/, "");
  } catch {
    throw new ConfigError("BACKEND_URL is not a valid URL");
  }

  const portRaw = env.PORT?.trim();
  const port = portRaw ? Number(portRaw) : 8787;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new ConfigError("PORT must be an integer between 1 and 65535");
  }

  return { projectId, projectSecret, backendUrl, sharedSecret, port, logLevel: parseLogLevel(env.LOG_LEVEL) };
}
