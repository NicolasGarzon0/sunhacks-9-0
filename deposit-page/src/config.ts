// Public configuration only. Nothing secret ever goes in the frontend.
export type WalletMode = "local" | "privy";

function apiBase(raw: string | undefined): string {
  const base = (raw || "http://127.0.0.1:8788").replace(/\/+$/, "");
  return base.endsWith("/markets") ? base : `${base}/markets`;
}

export const config = {
  apiBase: apiBase(import.meta.env.VITE_API_BASE_URL),
  walletMode: (import.meta.env.VITE_WALLET_MODE === "privy" ? "privy" : "local") as WalletMode,
  // Public ids only. There is no Privy app secret and the page never uses one.
  privyAppId: (import.meta.env.VITE_PRIVY_APP_ID as string | undefined) || "",
  privyClientId: (import.meta.env.VITE_PRIVY_CLIENT_ID as string | undefined) || undefined,
  cluster: import.meta.env.VITE_EXPLORER_CLUSTER || "devnet",
};

export function explorerTx(signature: string, cluster = config.cluster): string {
  return `https://explorer.solana.com/tx/${encodeURIComponent(signature)}?cluster=${encodeURIComponent(cluster)}`;
}
