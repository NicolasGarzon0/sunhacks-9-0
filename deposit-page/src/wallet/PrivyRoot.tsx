import { PrivyProvider, useLogin, useLogout, usePrivy } from "@privy-io/react-auth";
import { useSignTransaction, useWallets } from "@privy-io/react-auth/solana";
import { useEffect, type ReactNode } from "react";
import type { PrivyWalletProvider } from "./PrivyWalletProvider";
import { WalletError } from "./types";
import { WalletProviderContext } from "./WalletContext";

/** Privy settings. Login by email or SMS (SMS is US and Canada only), one embedded Solana wallet, no wallet pop ups of Privy's own. */
export function privyConfig() {
  return {
    loginMethods: ["email", "sms"] as ("email" | "sms")[],
    embeddedWallets: {
      solana: { createOnLogin: "users-without-wallets" as const },
      showWalletUIs: false,
    },
    externalWallets: { disableAllExternalWallets: true },
    appearance: { walletChainType: "solana-only" as const },
  };
}

/**
 * The embedded Solana wallet address from the Privy user record. The SDK's useWallets list is event driven and can lag
 * behind the user (or stay empty after a session restore), while the user record is reactive and has the address as soon
 * as the wallet exists. A wallet the user can sign with has walletClientType "privy".
 */
export function linkedSolanaAddress(user: unknown): string | null {
  const accounts = (user as { linkedAccounts?: unknown } | null | undefined)?.linkedAccounts;
  if (!Array.isArray(accounts)) return null;
  for (const a of accounts as Record<string, unknown>[]) {
    if (a && a.type === "wallet" && a.chainType === "solana" && a.walletClientType === "privy" && a.userCanSign !== false && typeof a.address === "string") {
      return a.address;
    }
  }
  return null;
}

/** Moves Privy hook state into the plain provider object. Renders nothing. */
function Bridge({ provider }: { provider: PrivyWalletProvider }) {
  const { ready, authenticated, user } = usePrivy();
  const { login } = useLogin({ onError: (e) => provider.onLoginError(e) });
  const { logout } = useLogout();
  const { wallets } = useWallets();
  const { signTransaction } = useSignTransaction();
  // Address: the wallet list when it has one, else the user record. Signing needs the list's wallet object (found by address).
  const address = wallets[0]?.address ?? linkedSolanaAddress(user);
  const wallet = wallets.find((w) => w.address === address) ?? null;

  useEffect(() => {
    provider.attach({
      sdkReady: ready,
      authenticated,
      address,
      login: () => login({ loginMethods: ["email"] }), // default to email; the user can still pick SMS if they close and reopen
      logout: () => logout(),
      signTransaction: async (tx) => {
        if (!wallet) throw new WalletError("WALLET", "the wallet is not ready yet, try again in a moment");
        // Sign only. chain is devnet, Privy's own confirmation screen is off, nothing is sent.
        const out = await signTransaction({ transaction: tx, wallet, chain: "solana:devnet", options: { uiOptions: { showWalletUIs: false } } });
        return out.signedTransaction;
      },
    });
  }, [provider, ready, authenticated, address, wallet, login, logout, signTransaction]);

  useEffect(() => () => provider.attach(null), [provider]);
  return null;
}

export default function PrivyRoot({ provider, appId, clientId, children }: {
  provider: PrivyWalletProvider; appId: string; clientId?: string; children: ReactNode;
}) {
  return (
    <PrivyProvider appId={appId} {...(clientId ? { clientId } : {})} config={privyConfig()}>
      <Bridge provider={provider} />
      <WalletProviderContext provider={provider}>{children}</WalletProviderContext>
    </PrivyProvider>
  );
}
