import type { MarketView } from "../api/types";
import { formatCountdown, msLeft, phase } from "../lib/countdown";
import { formatKickoff } from "../lib/format";
import { StatusChip } from "./Basics";

export function Matchup({ market, now }: { market: MarketView; now: number }) {
  const away = market.teams.side0.name;
  const home = market.teams.side1.name;
  const ms = msLeft(market.funding_closes_at, now);
  const ph = phase(ms);
  const closing = market.status === "open"
    ? ph === "open" ? formatCountdown(ms) : ph === "closing" ? "Closing…" : "Closed"
    : "Closed";
  return (
    <header className="matchup">
      <div className="matchup-top">
        <StatusChip status={market.status} />
      </div>
      <h1 className="matchup-title">
        <span className="team"><span className="clip" title={away}>{away}</span></span>
        <span className="at">at</span>
        <span className="team"><span className="clip" title={home}>{home}</span></span>
      </h1>
      <p className="kickoff">Kickoff {formatKickoff(market.kickoff_utc)}</p>
      <dl className="stats">
        <div><dt>Total pool</dt><dd>${market.total_pool_usd}</dd></div>
        <div><dt>Players</dt><dd>{market.participant_count} / {market.limits.max_participants}</dd></div>
        <div><dt>Betting closes</dt><dd className={ph === "closing" ? "warn" : ""} data-testid="countdown">{closing}</dd></div>
      </dl>
    </header>
  );
}
