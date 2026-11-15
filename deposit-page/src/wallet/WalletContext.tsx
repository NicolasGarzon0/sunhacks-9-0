import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { errorCopy } from "../lib/errors";
import { WalletError, type WalletProvider } from "./types";

const Ctx = createContext<WalletProvider | null>(null);

export function WalletProviderContext({ provider, children }: { provider: WalletProvider; children: ReactNode }) {
  return <Ctx.Provider value={provider}>{children}</Ctx.Provider>;
}

export interface WalletState {
  provider: WalletProvider;
  address: string | null;
  ready: boolean;
  /** true while a login is in progress (login providers only) */
  connecting: boolean;
  error: string | null;
  /** Log in (login providers) or try connecting again. */
  retry: () => void;
  /** Forget the wallet (local test wallet) or log out (login providers). */
  reset: () => Promise<void>;
}

function messageOf(e: unknown): string {
  if (e instanceof WalletError) return errorCopy(e.code);
  return e instanceof Error ? e.message : "wallet failed";
}

/**
 * Connects the provider on mount and re-renders when its address changes. Components only see this.
 * Login providers (Privy) are not connected on mount: the user presses Log in, which runs `retry`.
 */
export function useWallet(): WalletState {
  const provider = useContext(Ctx);
  if (!provider) throw new Error("useWallet needs a WalletProviderContext");
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const address = useSyncExternalStore(
    (cb) => provider.subscribe(cb), () => provider.address, () => provider.address,
  );
  useEffect(() => {
    if (provider.requiresLogin) return;
    let live = true;
    setError(null);
    provider.connect().catch((e: unknown) => { if (live) setError(messageOf(e)); });
    return () => { live = false; };
  }, [provider, attempt]);

  const login = () => {
    setError(null);
    setConnecting(true);
    provider.connect().catch((e: unknown) => setError(messageOf(e))).finally(() => setConnecting(false));
  };
  return {
    provider, address, ready: provider.ready && address !== null, connecting, error,
    retry: provider.requiresLogin ? login : () => setAttempt((n) => n + 1),
    reset: async () => {
      setError(null);
      try { await provider.disconnect(); } catch (e) { setError(messageOf(e)); }
      setAttempt((n) => n + 1);
    },
  };
}
