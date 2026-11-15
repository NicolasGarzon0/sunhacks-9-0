import { useEffect, useState } from "react";

/** The current time, refreshed every `everyMs` (default 1 s). Paused while the tab is hidden. */
export function useNow(everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const id = setInterval(() => { if (!document.hidden) tick(); }, everyMs);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", tick); };
  }, [everyMs]);
  return now;
}
