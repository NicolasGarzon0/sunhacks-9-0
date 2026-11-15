import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { DepositTx, SubmitResult } from "../api/types";
import type { WalletProvider } from "../wallet";

export type DepositStep = "idle" | "preparing" | "signing" | "sending" | "confirmed" | "error";

export interface DepositInput { side: 0 | 1; amountUsd: string; displayName?: string }
export interface DepositState {
  step: DepositStep;
  error: { code: string; detail: string } | null;
  summary: DepositTx["summary"] | null;
  result: SubmitResult | null;
  retried: boolean; // the page rebuilt the transaction after TX_EXPIRED
}
const IDLE: DepositState = { step: "idle", error: null, summary: null, result: null, retried: false };

/**
 * deposit_tx -> wallet signs -> deposit_submit. On TX_EXPIRED the page rebuilds and signs again once, then shows the error.
 * The message is passed to the wallet exactly as received; nothing is changed after it is signed.
 */
export function useDeposit(token: string, provider: WalletProvider, address: string | null) {
  const [state, setState] = useState<DepositState>(IDLE);
  const busy = useRef(false);

  const reset = useCallback(() => setState(IDLE), []);

  const run = useCallback(async (input: DepositInput): Promise<SubmitResult | null> => {
    if (busy.current || !address) return null;
    busy.current = true;
    let retried = false;
    let summary: DepositTx["summary"] | null = null;
    const set = (patch: Partial<DepositState>) => setState((s) => ({ ...s, ...patch }));
    setState({ ...IDLE, step: "preparing" });
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        set({ step: "preparing", retried });
        const tx = await api.depositTx(token, address, input.side, input.amountUsd);
        summary = tx.summary;
        set({ step: "signing", summary });
        let signed;
        try {
          signed = await provider.signDeposit(tx.transaction_b64, tx.message_b64);
        } catch (e) {
          set({ step: "error", error: { code: "WALLET", detail: e instanceof Error ? e.message : "" }, summary });
          return null;
        }
        set({ step: "sending" });
        try {
          const result = await api.depositSubmit(token, address, input.side, input.amountUsd, signed, input.displayName || undefined);
          set({ step: "confirmed", result, summary, error: null });
          return result;
        } catch (e) {
          if (e instanceof ApiError && e.code === "TX_EXPIRED" && attempt === 0) {
            retried = true; // rebuild and sign again, once
            continue;
          }
          throw e;
        }
      }
      return null;
    } catch (e) {
      const code = e instanceof ApiError ? e.code : "INTERNAL";
      set({ step: "error", error: { code, detail: e instanceof ApiError ? e.detail : "" }, summary });
      return null;
    } finally {
      busy.current = false;
    }
  }, [token, provider, address]);

  return { state, run, reset };
}
