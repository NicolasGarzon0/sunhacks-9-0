import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import type { WalletProvider } from "./types";

const Ctx = createContext<WalletProvider | null>(null);

export function WalletProviderContext({ provider, children }: { provider: WalletProvider; children: ReactNode }) {
  return <Ctx.Provider value={provider}>{children}</Ctx.Provider>;
}

export interface WalletState {
  provider: WalletProvider;
  address: string | null;
  ready: boolean;
  error: string | null;
  retry: () => void;
  reset: () => Promise<void>;
}

/** Connects the provider on mount and re-renders when its address changes. Components only see this. */
export function useWallet(): WalletState {
  const provider = useContext(Ctx);
  if (!provider) throw new Error("useWallet needs a WalletProviderContext");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const address = useSyncExternalStore(
    (cb) => provider.subscribe(cb), () => provider.address, () => provider.address,
  );
  useEffect(() => {
    let live = true;
    setError(null);
    provider.connect().catch((e: unknown) => { if (live) setError(e instanceof Error ? e.message : "wallet failed"); });
    return () => { live = false; };
  }, [provider, attempt]);
  return {
    provider, address, ready: provider.ready && address !== null, error,
    retry: () => setAttempt((n) => n + 1),
    reset: async () => { await provider.disconnect(); setAttempt((n) => n + 1); },
  };
}
