import { Keypair, PublicKey } from "@solana/web3.js";
import { parseLogLevel, type LogLevel } from "./logger.ts";

export interface ChainConfig {
  rpcUrl: string;
  programId: PublicKey;
  mint: PublicKey;
  payer: Keypair;
  oracle: Keypair;
  treasury: PublicKey;
  secret: string;
  port: number;
  logLevel: LogLevel;
}

export class ConfigError extends Error {}

const DEFAULT_RPC = "https://api.devnet.solana.com";

function required(env: NodeJS.ProcessEnv, name: string): string {
  const v = env[name]?.trim();
  if (!v) throw new ConfigError(`Missing required environment variable: ${name}`);
  return v;
}
function pubkey(env: NodeJS.ProcessEnv, name: string): PublicKey {
  try {
    return new PublicKey(required(env, name));
  } catch (e) {
    if (e instanceof ConfigError) throw e;
    throw new ConfigError(`${name} is not a valid public key`);
  }
}
function keypair(env: NodeJS.ProcessEnv, name: string): Keypair {
  const raw = required(env, name);
  try {
    const bytes = Buffer.from(raw, "base64");
    if (bytes.length !== 64) throw new Error("length");
    return Keypair.fromSecretKey(Uint8Array.from(bytes));
  } catch {
    // never echo the value
    throw new ConfigError(`${name} must be the base64 of a 64 byte secret key`);
  }
}

/** Reads and validates env. Names the offending variable, never prints a value. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): ChainConfig {
  const secret = required(env, "CHAIN_SERVICE_SECRET");
  if (secret.length < 16) throw new ConfigError("CHAIN_SERVICE_SECRET must be at least 16 characters");
  const portRaw = env.PORT?.trim();
  const port = portRaw ? Number(portRaw) : 8789;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new ConfigError("PORT must be an integer between 1 and 65535");
  return {
    rpcUrl: env.SOLANA_RPC_URL?.trim() || DEFAULT_RPC,
    programId: pubkey(env, "PROGRAM_ID"),
    mint: pubkey(env, "TEST_USDC_MINT"),
    payer: keypair(env, "PAYER_SECRET_KEY"),
    oracle: keypair(env, "ORACLE_SECRET_KEY"),
    treasury: pubkey(env, "TREASURY_PUBKEY"),
    secret,
    port,
    logLevel: parseLogLevel(env.LOG_LEVEL),
  };
}
