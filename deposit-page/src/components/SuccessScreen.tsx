import type { DepositTx, SubmitResult } from "../api/types";
import { explorerTx } from "../config";
import { Card } from "./Basics";

interface Props { result: SubmitResult; summary: DepositTx["summary"] | null; onView: () => void }

export function SuccessScreen({ result, summary, onView }: Props) {
  return (
    <Card tone="accent">
      <div className="success" data-testid="success">
        <h2>You are in.</h2>
        {summary ? (
          <>
            <p>${summary.amount_usd} on {summary.team}. Your total on this bet: ${summary.your_total_stake_usd}.</p>
            <p className="muted">
              If {summary.team} wins you would receive about ${summary.payout_usd}{summary.multiplier ? ` (${summary.multiplier}x)` : ""}, based on the pool right now.
            </p>
          </>
        ) : <p>Your deposit is confirmed.</p>}
        <p><a href={explorerTx(result.signature)} target="_blank" rel="noopener noreferrer">View the transaction on the explorer</a></p>
        <button type="button" className="btn" onClick={onView}>View the pool</button>
      </div>
    </Card>
  );
}
