import { useEffect, useMemo, useState } from "react";
import { BetPage } from "./BetPage";
import { Footer } from "./components/Basics";
import { config } from "./config";
import { matchRoute } from "./router";
import { HomeView, InvalidView } from "./views/Views";
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
        <div className="topbar"><a href="/" className="wordmark">handshake<span>.</span></a></div>
        <main className="page">
          {route.name === "bet" ? <BetPage key={route.token} token={route.token} /> : null}
          {route.name === "home" ? (<><HomeView /><Footer /></>) : null}
          {route.name === "notfound" ? (<><InvalidView /><Footer /></>) : null}
        </main>
      </div>
    </WalletRoot>
  );
}
