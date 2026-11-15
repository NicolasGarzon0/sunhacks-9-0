import { useEffect, useMemo, useState } from "react";
import { BetPage } from "./BetPage";
import { config } from "./config";
import { matchRoute } from "./router";
import { InvalidView } from "./views/Views";
import { createWalletProvider, WalletRoot } from "./wallet";

export function App({ pathname }: { pathname?: string }) {
  const [path, setPath] = useState(pathname ?? window.location.pathname);
  useEffect(() => {
    if (pathname !== undefined) return;
    const on = () => setPath(window.location.pathname);
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, [pathname]);
  const provider = useMemo(() => createWalletProvider(config.walletMode), []);
  const route = matchRoute(path);
  return (
    <WalletRoot provider={provider}>
      <div className="shell">
        <header className="topbar"><span className="wordmark">TextBet</span></header>
        <main className="page">
          {route.name === "bet" ? <BetPage key={route.token} token={route.token} /> : null}
          {route.name === "notfound" ? <InvalidView /> : null}
        </main>
      </div>
    </WalletRoot>
  );
}
