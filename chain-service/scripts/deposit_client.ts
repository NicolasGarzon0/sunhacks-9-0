// Reference client: plays the part of the deposit page, with a locally generated keypair (the real wallet login is chunk 5).
//   npm run deposit-client -- <airdrop|wallet|view|deposit> --link <market link or token> [--wallet-file name-or-path]
//        [--api http://127.0.0.1:8788] [--side 0|1 --usd 5] [--shape message|signed] [--tamper amount|side] [--name "Dana"]
// Prints ONE line of JSON: {ok, status, body} with public data only. The keypair holds no SOL and is never printed.
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { Keypair } from "@solana/web3.js";
import { signEd25519 } from "../src/crypto.ts";

export const DEFAULT_API = "http://127.0.0.1:8788";
const WALLET_DIR = path.resolve(import.meta.dirname, "../.dev-wallets");

export function tokenFrom(link: string): string {
  const t = link.trim().replace(/\/+$/, "");
  return t.includes("/") ? t.slice(t.lastIndexOf("/") + 1) : t;
}

/** A keypair file under chain-service/.dev-wallets (gitignored, mode 600). Created when missing. */
export function loadWallet(fileOrName: string): Keypair {
  const file = fileOrName.includes("/") ? fileOrName : path.join(WALLET_DIR, `${fileOrName}.json`);
  if (existsSync(file)) return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(readFileSync(file, "utf8"))));
  mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const kp = Keypair.generate();
  writeFileSync(file, JSON.stringify(Array.from(kp.secretKey)), { mode: 0o600 });
  chmodSync(file, 0o600);
  return kp;
}

export interface Result { ok: boolean; status: number; body: any }

export class DepositClient {
  constructor(public api: string, public token: string, public kp: Keypair) {}

  private async call(method: "GET" | "POST", suffix: string, body?: unknown, query = ""): Promise<Result> {
    const res = await fetch(`${this.api}/markets/${this.token}${suffix}${query}`, {
      method, headers: body ? { "content-type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined,
    });
    const json = (await res.json().catch(() => ({}))) as any;
    return { ok: res.ok && json.ok !== false, status: res.status, body: json };
  }

  get wallet() { return this.kp.publicKey.toBase58(); }
  walletInfo() { return this.call("GET", "/wallet", undefined, `?wallet=${this.wallet}`); }
  airdrop() { return this.call("POST", "/airdrop", { wallet: this.wallet }); }
  view() { return this.call("GET", ""); }
  depositTx(side: number, usd: string | number) { return this.call("POST", "/deposit_tx", { wallet: this.wallet, side, amount_usd: usd }); }

  /** Flip the amount or the side inside the message the page was given, then sign the altered bytes (what a malicious page would do). */
  private tamper(message: Buffer, what: "amount" | "side", side: number, usd: string | number): Buffer {
    const out: Buffer = Buffer.from(message);
    const amount = BigInt(Math.round(Number(usd) * 1_000_000));
    const data = Buffer.alloc(9);
    data.writeUInt8(side, 0);
    data.writeBigUInt64LE(amount, 1);
    const at = out.indexOf(data);
    if (at < 0) throw new Error("could not find the instruction data to tamper with");
    if (what === "amount") out.writeBigUInt64LE(amount * 10n, at + 1);
    else out.writeUInt8(side === 0 ? 1 : 0, at);
    return out;
  }

  /** deposit_tx, sign locally, deposit_submit. Returns the submit result (or the deposit_tx error). */
  async deposit(o: { side: number; usd: string | number; shape?: "message" | "signed"; tamper?: "amount" | "side"; name?: string }): Promise<Result> {
    const built = await this.depositTx(o.side, o.usd);
    if (!built.ok) return built;
    let message: Buffer = Buffer.from(built.body.message_b64, "base64");
    if (o.tamper) message = this.tamper(message, o.tamper, o.side, o.usd);
    const sig = signEd25519(this.kp.secretKey, message);
    const body: Record<string, unknown> = { wallet: this.wallet, side: o.side, amount_usd: o.usd, ...(o.name ? { display_name: o.name } : {}) };
    if ((o.shape ?? "message") === "message") {
      body.message_b64 = message.toString("base64");
      body.user_signature_b64 = sig.toString("base64");
    } else {
      const wire = Buffer.from(built.body.transaction_b64, "base64"); // [2][payer slot][user slot][message]
      if (o.tamper) { const tampered = Buffer.concat([wire.subarray(0, 1 + 128), message]); sig.copy(tampered, 1 + 64); body.signed_tx_b64 = tampered.toString("base64"); }
      else { sig.copy(wire, 1 + 64); body.signed_tx_b64 = wire.toString("base64"); }
    }
    const res = await this.call("POST", "/deposit_submit", body);
    return { ...res, body: { ...res.body, summary: built.body.summary } };
  }
}

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main() {
  const [cmd] = process.argv.slice(2);
  const link = arg("link");
  const file = arg("wallet-file");
  if (!cmd || !link || !file) throw new Error("usage: npm run deposit-client -- <airdrop|wallet|view|deposit> --link <url|token> --wallet-file <name> [--side 0|1 --usd 5 --shape message|signed --tamper amount|side --name Dana --api URL]");
  const c = new DepositClient(arg("api") ?? DEFAULT_API, tokenFrom(link), loadWallet(file));
  let r: Result;
  switch (cmd) {
    case "airdrop": r = await c.airdrop(); break;
    case "wallet": r = await c.walletInfo(); break;
    case "view": r = await c.view(); break;
    case "deposit": {
      const side = Number(arg("side"));
      const usd = arg("usd");
      if (!usd || (side !== 0 && side !== 1)) throw new Error("deposit needs --side 0|1 and --usd <dollars>");
      r = await c.deposit({ side, usd, shape: (arg("shape") as "message" | "signed" | undefined) ?? "message", tamper: arg("tamper") as "amount" | "side" | undefined, name: arg("name") });
      break;
    }
    default: throw new Error(`unknown command ${cmd}`);
  }
  console.log(JSON.stringify({ wallet: c.wallet, ...r }));
  process.exit(r.ok ? 0 : 3);
}

if (process.argv[1]?.endsWith("deposit_client.ts")) {
  main().catch((e) => { console.error("ERROR:", e instanceof Error ? e.message : String(e)); process.exit(1); });
}
