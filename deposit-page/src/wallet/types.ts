/** What a wallet hands back after the user approves a deposit. Either shape is accepted by deposit_submit. */
export type SignResult =
  | { signed_tx_b64: string }
  | { message_b64: string; user_signature_b64: string };

/** Codes a wallet can fail with. Each one has plain copy in src/lib/errors.ts. */
export type WalletErrorCode = "WALLET" | "WALLET_LOGIN_CANCELLED" | "WALLET_REJECTED" | "WALLET_NETWORK";

export class WalletError extends Error {
  constructor(readonly code: WalletErrorCode, detail = "") {
    super(detail || code);
    this.name = "WalletError";
  }
}

/**
 * The only thing the UI knows about wallets.
 * `isTest` marks a throwaway devnet wallet (the UI then shows the test wallet notice); `disconnect()` forgets the wallet
 * (for the local test wallet that is the "reset" action, for a login wallet it is "log out").
 * `requiresLogin` providers are not connected on page load: the user presses Log in, which calls `connect()`.
 */
export interface WalletProvider {
  readonly name: string;
  readonly address: string | null; // null until ready
  readonly ready: boolean;
  readonly isTest: boolean;
  readonly requiresLogin: boolean;
  connect(): Promise<void>;
  signDeposit(unsignedTxB64: string, messageB64: string): Promise<SignResult>;
  disconnect(): Promise<void>;
  /** Called whenever address / ready change. Returns the unsubscribe function. */
  subscribe(listener: () => void): () => void;
}
