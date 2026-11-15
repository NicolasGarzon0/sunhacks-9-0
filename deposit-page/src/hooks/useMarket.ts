import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { ApiError } from "../api/client";
import type { MarketView, Participant, Position } from "../api/types";
import { isReconnecting, nextDelay } from "../lib/polling";

export interface MarketState {
  market: MarketView | null;
  participants: Participant[];
  position: Position | null;
  error: { code: string; http: number } | null; // set only when nothing can be shown (invalid / expired / first load failed)
  failures: number;
  reconnecting: boolean;
  loading: boolean;
  refresh: () => void;
  applyMarket: (m: MarketView) => void;
}

const TERMINAL = new Set([404, 410]);

/** Polls the market (5 s open/locked, 15 s after), pauses while the tab is hidden, backs off on errors. */
export function useMarket(token: string, wallet: string | null): MarketState {
  const [market, setMarket] = useState<MarketView | null>(null);
  const [participants, setParticipants] = useState<Participant[]>([]);
  const [position, setPosition] = useState<Position | null>(null);
  const [error, setError] = useState<MarketState["error"]>(null);
  const [failures, setFailures] = useState(0);
  const [loading, setLoading] = useState(true);
  const walletRef = useRef(wallet);
  const loadRef = useRef<() => void>(() => {});

  useEffect(() => {
    walletRef.current = wallet;
    setPosition(null);
    loadRef.current();
  }, [wallet]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let fails = 0;
    let status: string | undefined;
    let terminal = false;
    let inflight = false;
    setMarket(null); setParticipants([]); setPosition(null); setError(null); setFailures(0); setLoading(true);

    const schedule = () => {
      clearTimeout(timer);
      if (cancelled || terminal || document.hidden) return;
      timer = setTimeout(load, nextDelay(status, fails));
    };

    async function load() {
      if (cancelled || terminal || inflight) return;
      inflight = true;
      clearTimeout(timer);
      try {
        const m = await api.market(token);
        if (cancelled) return;
        status = m.status;
        fails = 0;
        const w = walletRef.current;
        const [parts, pos] = await Promise.all([
          api.participants(token).catch(() => null),
          w ? api.position(token, w).catch(() => null) : Promise.resolve(null),
        ]);
        if (cancelled) return;
        setMarket(m);
        if (parts) setParticipants(parts);
        if (pos) setPosition(pos);
        setError(null); setFailures(0); setLoading(false);
      } catch (e) {
        if (cancelled) return;
        const err = e instanceof ApiError ? e : new ApiError("NETWORK", 0);
        if (TERMINAL.has(err.http)) {
          terminal = true;
          setError({ code: err.code, http: err.http });
          setLoading(false);
        } else {
          fails += 1;
          setFailures(fails);
          setLoading(false);
          // keep showing the last good market; only a first load that never worked shows the error screen
          setError((prev) => prev ?? { code: err.code, http: err.http });
        }
      } finally {
        inflight = false;
        schedule();
      }
    }
    loadRef.current = () => { void load(); };

    const onVisibility = () => {
      if (document.hidden) clearTimeout(timer);
      else void load();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [token]);

  const refresh = useCallback(() => loadRef.current(), []);
  const applyMarket = useCallback((m: MarketView) => { setMarket(m); setError(null); }, []);
  return { market, participants, position, error, failures, reconnecting: isReconnecting(failures) && market !== null, loading, refresh, applyMarket };
}
