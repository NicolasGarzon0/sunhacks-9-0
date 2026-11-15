import { lazy, Suspense, type ReactNode } from "react";
import { config } from "../config";
import { PrivyWalletProvider } from "./PrivyWalletProvider";
import type { WalletProvider } from "./types";
import { WalletProviderContext } from "./WalletContext";

// Loaded only in privy mode, so the local test wallet build never pulls the Privy SDK in.
const PrivyRoot = lazy(() => import("./PrivyRoot"));

/** Wraps the app: the Privy SDK provider in privy mode, a plain context otherwise. */
export function WalletRoot({ provider, appId = config.privyAppId, clientId = config.privyClientId, children }: {
  provider: WalletProvider; appId?: string; clientId?: string; children: ReactNode;
}) {
  if (provider instanceof PrivyWalletProvider) {
    if (!appId) {
      return <p className="field-error" role="alert">Wallet login is not set up. Set VITE_PRIVY_APP_ID and reload.</p>;
    }
    return (
      <Suspense fallback={null}>
        <PrivyRoot provider={provider} appId={appId} clientId={clientId}>{children}</PrivyRoot>
      </Suspense>
    );
  }
  return <WalletProviderContext provider={provider}>{children}</WalletProviderContext>;
}
