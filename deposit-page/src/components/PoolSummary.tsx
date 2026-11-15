import type { MarketView } from "../api/types";
import { sharePercent } from "../lib/money";

export function PoolSummary({ market }: { market: MarketView }) {
  const total = market.pools.side0_base + market.pools.side1_base;
  const rows = [
    { side: 0 as const, team: market.teams.side0, pool: market.pools.side0_usd, base: market.pools.side0_base, mult: market.multipliers?.side0 },
    { side: 1 as const, team: market.teams.side1, pool: market.pools.side1_usd, base: market.pools.side1_base, mult: market.multipliers?.side1 },
  ];
  return (
    <div className="poolsum">
      {rows.map((r) => (
        <div key={r.side} className={`poolrow${market.status === "settled" && market.winning_side === r.side ? " winner" : ""}`}>
          <span className="clip pname" title={r.team.name}>{r.team.name}</span>
          <span className="pval">${r.pool}</span>
          <span className="pshare">{sharePercent(r.base, total)}%</span>
          {r.mult ? <span className="pmult">{r.mult}x</span> : null}
          <span className="side-bar" aria-hidden="true"><span style={{ width: `${sharePercent(r.base, total)}%` }} /></span>
        </div>
      ))}
    </div>
  );
}
