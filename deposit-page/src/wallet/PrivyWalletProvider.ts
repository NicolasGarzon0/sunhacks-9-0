import * as ed from "@noble/ed25519";
import { base58Decode, fromBase64, toBase64 } from "../lib/format";
import { bytesEqual, parseLegacyTx } from "./solanaTx";
import { WalletError, type SignResult, type WalletProvider } from "./types";

/**
 * What the React side (PrivyRoot) gives the provider. The provider has no Privy imports: it is plain TypeScript,
 * driven through this bridge, so the signing rules can be tested without the SDK.
 */
export interface PrivyBridge {
  sdkReady: boolean;
  authenticated: boolean;
  /** Embedded Solana wallet address, null until the wallet exists. */
  address: string | null;
  /** Opens Privy's login screen (email entry). Result arrives through attach() / onLoginError(). */
  login(): void;
  logout(): Promise<void>;
  /** Signs the serialized transaction and returns the signed transaction bytes. Never sends. */
  signTransaction(tx: Uint8Array): Promise<Uint8Array>;
}

const WALLET_WAIT_MS = 20_000;

interface Deferred { promise: Promise<void>; resolve: () => void; reject: (e: unknown) => void }
function deferred(): Deferred {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej; });
  promise.catch(() => {}); // callers handle it; never an unhandled rejection
  return { promise, resolve, reject };
}

const NETWORK_RE = /network|fetch|timeout|timed out|offline|unreachable|connection|5\d\d\b/i;

function describe(e: unknown): string {
  if (typeof e === "string") return e;
  if (e && typeof e === "object") {
    const o = e as { code?: unknown; message?: unknown };
    return `${typeof o.code === "string" ? o.code : ""} ${typeof o.message === "string" ? o.message : ""}`.trim();
  }
  return "";
}

/** Login errors from Privy: leaving the modal is a cancel, a network problem is a network error. */
export function loginError(e: unknown): WalletError {
  const text = describe(e);
  if (/exited_auth_flow|cancel|closed|exit/i.test(text)) return new WalletError("WALLET_LOGIN_CANCELLED", text);
  if (NETWORK_RE.test(text)) return new WalletError("WALLET_NETWORK", text);
  return new WalletError("WALLET", text);
}

/** Signing errors: a network problem is a network error, anything else means the user (or the wallet) said no. */
export function signError(e: unknown): WalletError {
  if (e instanceof WalletError) return e;
  const text = describe(e);
  return new WalletError(NETWORK_RE.test(text) ? "WALLET_NETWORK" : "WALLET_REJECTED", text);
}

/**
 * Real users: a Privy embedded Solana wallet created at email login. The wallet only SIGNS. It never sends, and the
 * bytes it signs are checked before and after: the message must be exactly the one the service gave us.
 */
export class PrivyWalletProvider implements WalletProvider {
  readonly name = "Wallet";
  readonly isTest = false;
  readonly requiresLogin = true;
  private bridge: PrivyBridge | null = null;
  private listeners = new Set<() => void>();
  private pending: Deferred | null = null;
  private loginOpened = false;
  private walletTimer: ReturnType<typeof setTimeout> | null = null;

  get address(): string | null {
    const b = this.bridge;
    return b && b.authenticated ? b.address : null;
  }
  get ready() { return this.address !== null; }

  subscribe(fn: () => void) {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }
  private emit() { this.listeners.forEach((l) => l()); }

  /** Called by PrivyRoot whenever the SDK state changes. */
  attach(bridge: PrivyBridge | null) {
    this.bridge = bridge;
    this.advance();
    this.emit();
  }

  /** Called by PrivyRoot when the login screen reports an error (including the user closing it). */
  onLoginError(e: unknown) {
    this.settle((d) => d.reject(loginError(e)));
  }

  private settle(f: (d: Deferred) => void) {
    const d = this.pending;
    this.pending = null;
    this.loginOpened = false;
    if (this.walletTimer) { clearTimeout(this.walletTimer); this.walletTimer = null; }
    if (d) f(d);
  }

  private advance() {
    const b = this.bridge;
    if (!this.pending || !b || !b.sdkReady) return;
    if (b.authenticated && b.address) { this.settle((d) => d.resolve()); return; }
    if (b.authenticated) {
      // logged in, the embedded wallet is still being created
      this.walletTimer ??= setTimeout(() => {
        this.settle((d) => d.reject(new WalletError("WALLET_NETWORK", "wallet was not created in time")));
      }, WALLET_WAIT_MS);
      return;
    }
    if (!this.loginOpened) { this.loginOpened = true; b.login(); }
  }

  /** Log in (opens Privy's email screen). Resolves when the wallet address is available. */
  connect(): Promise<void> {
    if (this.address) return Promise.resolve();
    if (this.pending) return this.pending.promise;
    const d = deferred();
    this.pending = d;
    this.advance(); // may settle d right away
    return d.promise;
  }

  async signDeposit(unsignedTxB64: string, messageB64: string): Promise<SignResult> {
    const address = this.address;
    const bridge = this.bridge;
    if (!bridge || !address) throw new WalletError("WALLET", "not logged in");
    const expected = fromBase64(messageB64);
    const unsigned = fromBase64(unsignedTxB64);

    let parsed;
    try { parsed = parseLegacyTx(unsigned); } catch (e) { throw new WalletError("WALLET", `bad transaction: ${describe(e)}`); }
    if (!bytesEqual(parsed.message, expected)) throw new WalletError("WALLET", "transaction does not match the message");

    let signedBytes: Uint8Array;
    try { signedBytes = await bridge.signTransaction(unsigned); } catch (e) { throw signError(e); }

    // The wallet must only have added a signature. Same message byte for byte: same fee payer, blockhash, instructions.
    let signed;
    try { signed = parseLegacyTx(signedBytes); } catch (e) { throw new WalletError("WALLET", `bad signed transaction: ${describe(e)}`); }
    if (!bytesEqual(signed.message, expected)) throw new WalletError("WALLET", "the wallet changed the transaction");

    const pub = base58Decode(address);
    const idx = signed.accountKeys.findIndex((k) => bytesEqual(k, pub));
    if (idx < 0 || idx >= signed.numRequiredSignatures) throw new WalletError("WALLET", "the wallet did not sign as the depositor");
    const sig = signed.signatures[idx];
    if (!(await ed.verifyAsync(sig, expected, pub))) throw new WalletError("WALLET", "the signature does not match the message");
    return { message_b64: messageB64, user_signature_b64: toBase64(sig) };
  }

  /** Log out. The embedded wallet stays with the account: logging in again brings it back. */
  async disconnect(): Promise<void> {
    this.settle((d) => d.resolve());
    await this.bridge?.logout();
  }
}
