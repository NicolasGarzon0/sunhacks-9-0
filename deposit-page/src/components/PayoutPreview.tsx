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
  return (
    <p className={`preview${loading ? " loading" : ""}`} data-testid="preview">
      {body}
      <span className="preview-note">The payout moves as others join.</span>
    </p>
  );
}
