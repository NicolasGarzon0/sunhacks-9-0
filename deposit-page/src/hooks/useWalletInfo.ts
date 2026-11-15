import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AirdropResult, WalletInfo } from "../api/types";

/** Balance of the wallet (GET /wallet) and the test USDC airdrop. */
export function useWalletInfo(token: string, address: string | null) {
  const [info, setInfo] = useState<WalletInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [airdropping, setAirdropping] = useState(false);
  const [airdropError, setAirdropError] = useState<string | null>(null);
  const [airdropDone, setAirdropDone] = useState<AirdropResult | null>(null);

  const refresh = useCallback(async () => {
    if (!address) return;
    setLoading(true);
    try {
      setInfo(await api.wallet(token, address));
      setError(null); setUnavailable(false);
    } catch (e) {
      const code = e instanceof ApiError ? e.code : "NETWORK";
      if (code === "NOT_FOUND") setUnavailable(true);
      setError(code);
    } finally {
      setLoading(false);
    }
  }, [token, address]);

  useEffect(() => { setInfo(null); setAirdropDone(null); void refresh(); }, [refresh]);

  const airdrop = useCallback(async () => {
    if (!address || airdropping) return;
    setAirdropping(true);
    setAirdropError(null);
    try {
      const r = await api.airdrop(token, address);
      setAirdropDone(r);
      setInfo({ ok: true, wallet: r.wallet, has_token_account: r.has_token_account, usdc_base: r.usdc_base, usdc_usd: r.usdc_usd });
    } catch (e) {
      setAirdropError(e instanceof ApiError ? e.code : "NETWORK");
    } finally {
      setAirdropping(false);
    }
  }, [token, address, airdropping]);

  return { info, loading, unavailable, error, refresh, airdrop, airdropping, airdropError, airdropDone };
}
