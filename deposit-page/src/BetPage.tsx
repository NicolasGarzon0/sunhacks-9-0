import { useEffect, useState } from "react";
import { Footer, LiveRegion, ReconnectBanner, Skeleton } from "./components/Basics";
import { useMarket } from "./hooks/useMarket";
import { useNow } from "./hooks/useNow";
import { statusToView, type ViewName } from "./lib/status";
import { ExpiredView, FailedView, InvalidView, LockedView, NetworkErrorView, RefundedView, SettledView } from "./views/Views";
import { OpenView } from "./views/OpenView";
import { useWallet } from "./wallet/WalletContext";

const ANNOUNCE: Partial<Record<ViewName, string>> = {
  open: "Betting is open.",
  locked: "Betting is now closed. Waiting for the game.",
  settled: "The game is over and the bet is settled.",
  refunded: "This bet was refunded.",
  failed: "This bet did not start.",
  invalid: "This link is not valid.",
  expired: "This link has expired.",
};

export function BetPage({ token }: { token: string }) {
  const wallet = useWallet();
  const m = useMarket(token, wallet.address);
  const now = useNow() + m.clockOffset; // server time: the countdown must not depend on a wrong browser clock
  const [announce, setAnnounce] = useState("");

  const terminal = m.error && (m.error.http === 404 || m.error.http === 410);
  const view: ViewName = terminal
    ? statusToView({ errorHttp: m.error!.http, errorCode: m.error!.code })
    : m.market
      ? statusToView({ status: m.market.status })
      : m.error
        ? "network"
        : "loading";

  useEffect(() => { setAnnounce(ANNOUNCE[view] ?? ""); }, [view]);

  const common = m.market
    ? { market: m.market, participants: m.participants, position: m.position, viewer: wallet.address, now }
    : null;

  let body;
  if (view === "loading") body = <Skeleton />;
  else if (view === "invalid") body = <InvalidView />;
  else if (view === "expired") body = <ExpiredView />;
  else if (view === "network" || !common) body = <NetworkErrorView onRetry={m.refresh} />;
  else if (view === "open") {
    body = (
      <OpenView
        token={token} market={common.market} participants={m.participants} position={m.position} wallet={wallet}
        now={now} applyMarket={m.applyMarket} refresh={m.refresh}
      />
    );
  } else if (view === "locked") body = <LockedView {...common} />;
  else if (view === "settled") body = <SettledView {...common} />;
  else if (view === "refunded") body = <RefundedView {...common} />;
  else body = <FailedView />;

  return (
    <>
      {m.reconnecting ? <ReconnectBanner /> : null}
      <LiveRegion message={announce} />
      {body}
      <Footer />
    </>
  );
}
