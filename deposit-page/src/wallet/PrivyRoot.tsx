import { PrivyProvider, useLogin, useLogout, usePrivy } from "@privy-io/react-auth";
import { useSignTransaction, useWallets } from "@privy-io/react-auth/solana";
import { useEffect, type ReactNode } from "react";
import type { PrivyWalletProvider } from "./PrivyWalletProvider";
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

/** Moves Privy hook state into the plain provider object. Renders nothing. */
function Bridge({ provider }: { provider: PrivyWalletProvider }) {
  const { ready, authenticated } = usePrivy();
  const { login } = useLogin({ onError: (e) => provider.onLoginError(e) });
  const { logout } = useLogout();
  const { wallets } = useWallets();
  const { signTransaction } = useSignTransaction();
  const wallet = wallets[0] ?? null;
  const address = wallet ? wallet.address : null;

  useEffect(() => {
    provider.attach({
      sdkReady: ready,
      authenticated,
      address,
      login: () => login({ loginMethods: ["email"] }), // default to email; the user can still pick SMS if they close and reopen
      logout: () => logout(),
      signTransaction: async (tx) => {
        if (!wallet) throw new Error("no wallet");
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
