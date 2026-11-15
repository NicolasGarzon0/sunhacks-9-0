import type { MarketView, Position } from "../api/types";
import { Card } from "./Basics";

export function YourPosition({ position, market }: { position: Position | null; market: MarketView }) {
  if (!position || !position.has_position || position.side === null) return null;
  const team = position.side === 0 ? market.teams.side0.name : market.teams.side1.name;
  const stake = position.stake_usd ?? "0.00";
  let line: string;
  let sub: string;
  if (market.status === "settled") {
    const won = market.winning_side === position.side;
    if (won) {
      line = `You won $${position.payout_usd ?? "…"}`;
      sub = position.paid ? "Paid to your wallet." : "Your payout is being sent. There is nothing to claim.";
    } else {
      line = `${team} did not win this time`;
      sub = `Your $${stake} went to the winners.`;
    }
  } else if (market.status === "refunded") {
    line = position.paid ? `Your $${stake} was returned` : `Your $${stake} is being returned`;
    sub = "Refunds have no fee.";
  } else {
    line = `You are in with $${stake} on ${team}`;
    sub = market.status === "open" ? "" : "Betting is closed. Good luck.";
  }
  return (
    <Card tone="accent">
      <p className="pos-line" data-testid="your-position">{line}</p>
      {sub ? <p className="muted">{sub}</p> : null}
    </Card>
  );
}
