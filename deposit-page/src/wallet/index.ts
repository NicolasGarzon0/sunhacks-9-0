import type { WalletMode } from "../config";
import { LocalDevWalletProvider } from "./LocalDevWalletProvider";
import { PrivyWalletProvider } from "./PrivyWalletProvider";
import type { WalletProvider } from "./types";

export type { SignResult, WalletProvider, WalletErrorCode } from "./types";
export { WalletError } from "./types";
export { WalletRoot } from "./WalletRoot";

/** The only place that knows which concrete provider exists. */
export function createWalletProvider(mode: WalletMode): WalletProvider {
  return mode === "privy" ? new PrivyWalletProvider() : new LocalDevWalletProvider();
}
