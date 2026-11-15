import * as ed from "@noble/ed25519";
import { base58Encode, fromBase64, toBase64 } from "../lib/format";
import type { SignResult, WalletProvider } from "./types";

const STORAGE_KEY = "handshake.devwallet.v1";

/**
 * A throwaway devnet wallet: an ed25519 keypair generated in the browser. It holds test money only.
 * The 32 byte seed is kept in localStorage (inside try/catch, with an in memory fallback when storage is blocked).
 */
export class LocalDevWalletProvider implements WalletProvider {
  readonly name = "Local test wallet";
  readonly isTest = true;
  private seed: Uint8Array | null = null;
  private _address: string | null = null;
  private _ready = false;
  private listeners = new Set<() => void>();
  private memory: string | null = null; // fallback when localStorage throws

  constructor(private storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null = safeStorage()) {}

  get address() { return this._address; }
  get ready() { return this._ready; }

  subscribe(fn: () => void) {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }
  private emit() { this.listeners.forEach((l) => l()); }

  private read(): string | null {
    try {
      const v = this.storage?.getItem(STORAGE_KEY) ?? null;
      if (v) return v;
    } catch { /* blocked storage */ }
    return this.memory;
  }
  private write(v: string) {
    this.memory = v;
    try { this.storage?.setItem(STORAGE_KEY, v); } catch { /* keep the in memory copy */ }
  }
  private clear() {
    this.memory = null;
    try { this.storage?.removeItem(STORAGE_KEY); } catch { /* ignore */ }
  }

  async connect(): Promise<void> {
    if (this._ready) return;
    let seed: Uint8Array | null = null;
    const stored = this.read();
    if (stored) {
      try {
        const bytes = fromBase64(stored);
        if (bytes.length === 32) seed = bytes;
      } catch { /* corrupt value: make a new wallet */ }
    }
    if (!seed) {
      seed = ed.utils.randomSecretKey();
      this.write(toBase64(seed));
    }
    this.seed = seed;
    this._address = base58Encode(await ed.getPublicKeyAsync(seed));
    this._ready = true;
    this.emit();
  }

  async signDeposit(_unsignedTxB64: string, messageB64: string): Promise<SignResult> {
    if (!this.seed) throw new Error("wallet is not ready");
    const sig = await ed.signAsync(fromBase64(messageB64), this.seed);
    return { message_b64: messageB64, user_signature_b64: toBase64(sig) };
  }

  /** Forgets the wallet: the next connect() makes a new one. */
  async disconnect(): Promise<void> {
    this.seed = null;
    this._address = null;
    this._ready = false;
    this.clear();
    this.emit();
  }
}

function safeStorage(): Storage | null {
  try { return typeof localStorage === "undefined" ? null : localStorage; } catch { return null; }
}
