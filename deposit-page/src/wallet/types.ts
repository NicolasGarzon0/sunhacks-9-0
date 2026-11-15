/** What a wallet hands back after the user approves a deposit. Either shape is accepted by deposit_submit. */
export type SignResult =
  | { signed_tx_b64: string }
  | { message_b64: string; user_signature_b64: string };

/**
 * The only thing the UI knows about wallets. Chunk 5 adds a Privy implementation without touching any component.
 * `isTest` marks a throwaway devnet wallet (the UI then shows the test wallet notice); `disconnect()` forgets the wallet
 * (for the local test wallet that is the "reset" action).
 */
export interface WalletProvider {
  readonly name: string;
  readonly address: string | null; // null until ready
  readonly ready: boolean;
  readonly isTest: boolean;
  connect(): Promise<void>;
  signDeposit(unsignedTxB64: string, messageB64: string): Promise<SignResult>;
  disconnect(): Promise<void>;
  /** Called whenever address / ready change. Returns the unsubscribe function. */
  subscribe(listener: () => void): () => void;
}
