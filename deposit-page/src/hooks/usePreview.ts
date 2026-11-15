import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Preview } from "../api/types";

export const PREVIEW_DEBOUNCE_MS = 350;

/** Debounced call to the preview endpoint. `amountText` null means "nothing valid to preview". */
export function usePreview(token: string, side: 0 | 1 | null, amountText: string | null, refreshKey: unknown) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (side === null || amountText === null) {
      setPreview(null); setError(null); setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const p = await api.preview(token, side, amountText);
        if (!cancelled) { setPreview(p); setError(null); }
      } catch (e) {
        if (!cancelled) { setPreview(null); setError(e instanceof ApiError ? e.code : "NETWORK"); }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => { cancelled = true; clearTimeout(t); };
  }, [token, side, amountText, refreshKey]);

  return { preview, error, loading };
}
