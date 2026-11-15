import type { SignResult, WalletProvider } from "./types";

/**
 * Chunk 5 placeholder. Selected by VITE_WALLET_MODE=privy.
 *
 * To implement: log the user in with Privy (embedded Solana wallet), expose its public key as `address`, and in
 * `signDeposit` sign the `messageB64` bytes with the embedded wallet, returning
 * `{ message_b64, user_signature_b64 }` (a 64 byte ed25519 signature, base64) or `{ signed_tx_b64 }`.
 * The backend verifies the exact bytes, so the message must never be changed before signing.
 */
export class PrivyWalletProvider implements WalletProvider {
  readonly name = "Privy";
  readonly isTest = false;
  readonly address = null;
  readonly ready = false;

  async connect(): Promise<void> { throw new Error("not implemented in chunk 4"); }
  async signDeposit(): Promise<SignResult> { throw new Error("not implemented in chunk 4"); }
  async disconnect(): Promise<void> { /* nothing to forget */ }
  subscribe() { return () => {}; }
}
