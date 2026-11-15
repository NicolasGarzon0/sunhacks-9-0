import type { WalletMode } from "../config";
import { LocalDevWalletProvider } from "./LocalDevWalletProvider";
import { PrivyWalletProvider } from "./PrivyWalletProvider";
import type { WalletProvider } from "./types";

export type { SignResult, WalletProvider } from "./types";

/** The only place that knows which concrete provider exists. */
export function createWalletProvider(mode: WalletMode): WalletProvider {
  return mode === "privy" ? new PrivyWalletProvider() : new LocalDevWalletProvider();
}
