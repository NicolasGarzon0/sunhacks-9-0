import type { Preview } from "../api/types";
import { errorCopy } from "../lib/errors";

function feePercent(bps: number): string {
  const p = bps / 100;
  return Number.isInteger(p) ? String(p) : p.toFixed(2).replace(/0+$/, "");
}

interface Props {
  team: string | null; preview: Preview | null; loading: boolean; errorCode: string | null; feeBps: number;
  hasInput: boolean;
}

/** A fixed height box, so the layout does not move while the numbers update. */
export function PayoutPreview({ team, preview, loading, errorCode, feeBps, hasInput }: Props) {
  let body: string;
  if (errorCode) body = errorCopy(errorCode);
  else if (!team) body = "Pick a team to see what you could receive.";
  else if (!hasInput) body = "Enter an amount to see what you could receive.";
  else if (preview) {
    body = `If ${team} wins you receive about $${preview.payout_usd}${preview.multiplier ? ` (${preview.multiplier}x)` : ""} after the ${feePercent(preview.fee_bps)}% fee on the losing side.`;
  } else body = `If ${team} wins you receive about … after the ${feePercent(feeBps)}% fee on the losing side.`;
  const showGrid = !errorCode && team !== null && hasInput;
  return (
    <div className={`preview${loading ? " loading" : ""}`} data-testid="preview">
      {showGrid ? (
        <>
          <span className="sr-only">{body}</span>
          <div className="preview-grid" aria-hidden="true">
            <div className="preview-cell">
              <span className="preview-label">Odds</span>
              <span className="preview-odds">{preview?.multiplier ? `${preview.multiplier}x` : "…"}</span>
            </div>
            <div className="preview-cell preview-cell-win">
              <span className="preview-label">You could win</span>
              <span className="preview-win">{preview ? `$${preview.payout_usd}` : "…"}</span>
              <span className="preview-profit">{preview ? `+$${preview.profit_usd} profit` : " "}</span>
            </div>
          </div>
        </>
      ) : <span className="preview-text">{body}</span>}
      <span className="preview-note">The payout moves as others join.</span>
    </div>
  );
}
