export type ViewName = "loading" | "invalid" | "expired" | "network" | "open" | "locked" | "settled" | "refunded" | "failed";

/** Maps a market status (or the failure of the market request) to the view to show. */
export function statusToView(input: { status?: string; errorHttp?: number; errorCode?: string }): ViewName {
  if (input.errorCode || input.errorHttp !== undefined) {
    if (input.errorHttp === 404 || input.errorCode === "BAD_TOKEN") return "invalid";
    if (input.errorHttp === 410 || input.errorCode === "LINK_EXPIRED") return "expired";
    return "network";
  }
  switch (input.status) {
    case "open": return "open";
    case "locked": return "locked";
    case "settled": return "settled";
    case "refunded": return "refunded";
    case "failed": return "failed";
    case undefined: return "loading";
    // game_confirmed, draft... : the page can only wait
    default: return "locked";
  }
}

const REASONS: Record<string, string> = {
  one_sided: "Everyone picked the same team, so there was nobody to win against.",
  too_few_participants: "Not enough people joined this bet.",
  cancelled: "The game was cancelled.",
  postponed: "The game was postponed.",
  tie: "The game ended in a tie.",
  no_result: "We could not get a final result for this game.",
};

export function refundReasonCopy(reason: string | null | undefined): string {
  return (reason && REASONS[reason]) || "This bet could not be settled.";
}
export const REFUND_REASONS = Object.keys(REASONS);
