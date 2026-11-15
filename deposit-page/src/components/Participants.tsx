import type { MarketView, Participant } from "../api/types";
import { shortAddr } from "../lib/format";
import { TeamBadge } from "./Basics";

interface Props { participants: Participant[]; market: MarketView; viewer: string | null }

export function ParticipantsList({ participants, market, viewer }: Props) {
  if (participants.length === 0) return <p className="muted">Nobody has joined yet. Be the first.</p>;
  const winning = market.status === "settled" ? market.winning_side : undefined;
  const refunded = market.status === "refunded";
  return (
    <ul className="people">
      {participants.map((p) => {
        const team = p.side === 0 ? market.teams.side0.name : market.teams.side1.name;
        const you = viewer !== null && p.wallet === viewer;
        const name = p.display_name || shortAddr(p.wallet);
        const outcome = refunded ? "Refunded" : winning === undefined ? null : p.side === winning ? "Won" : "Lost";
        return (
          <li key={p.wallet} className={you ? "you" : ""}>
            <TeamBadge name={team} size="sm" />
            <span className="who">
              <span className="clip" title={name}>{name}</span>
              {you ? <span className="tag">You</span> : null}
            </span>
            <span className="pteam clip" title={team}>{team}</span>
            <span className="pstake">${p.stake_usd}</span>
            {outcome ? <span className={`tag ${outcome.toLowerCase()}`}>{outcome}</span> : null}
          </li>
        );
      })}
    </ul>
  );
}
