export type MarketStatus = "open" | "locked" | "settled" | "refunded" | "failed";

export interface Team { name: string; role: "away" | "home" }
export interface MarketView {
  ok: true;
  bet_id: string;
  status: MarketStatus | string;
  league?: string;
  teams: { side0: Team; side1: Team };
  kickoff_utc: string;
  funding_closes_at: string;
  /** the server clock, ISO UTC. Optional only so an older backend still works: the page then falls back to the browser clock. */
  server_time?: string;
  pools: { side0_base: number; side1_base: number; side0_usd: string; side1_usd: string };
  total_pool_base: number;
  total_pool_usd: string;
  participant_count: number;
  limits: {
    min_stake_base: number; min_stake_usd: string; max_stake_base: number; max_stake_usd: string; max_participants: number;
  };
  fee_bps: number;
  fee_line: string;
  multipliers: { side0: string | null; side1: string | null } | null;
  game_pending?: boolean;
  winning_side?: 0 | 1;
  fee_paid_base?: number;
  fee_paid_usd?: string;
  distributable_base?: number;
  distributable_usd?: string;
  final_multiplier?: string | null;
  refund_reason?: string | null;
  failed?: boolean;
}

export interface Participant {
  wallet: string; side: 0 | 1; stake_base: number; stake_usd: string; display_name: string | null;
}
export interface Position {
  ok: true; status: string; wallet: string; display_name: string | null; has_position: boolean; side: 0 | 1 | null;
  stake_base: number | null; stake_usd: string | null; payout_base: number | null; payout_usd: string | null; paid: boolean;
}
export interface Preview {
  ok: true; side: 0 | 1; amount_base: number; amount_usd: string; payout_usd: string; profit_usd: string;
  multiplier: string | null; fee_bps: number; fee_line: string;
}
export interface WalletInfo {
  ok: true; wallet: string; has_token_account: boolean; usdc_base: number; usdc_usd: string;
}
export interface AirdropResult extends WalletInfo {
  minted_base: number; minted_usd: string; signature: string | null; explorer: string | null;
}
export interface DepositTx {
  ok: true; bet_id: string; transaction_b64: string; message_b64: string; blockhash: string;
  last_valid_block_height: number; expires_at: string;
  summary: {
    side: 0 | 1; team: string; amount_usd: string; amount_base: number; your_total_stake_usd: string; fee_bps: number;
    fee_line: string; payout_usd: string; profit_usd: string; multiplier: string | null; text: string;
  };
}
export interface SubmitResult {
  ok: true; signature: string; explorer: string; already: boolean; market: MarketView;
}

export type SubmitBody =
  | { signed_tx_b64: string }
  | { message_b64: string; user_signature_b64: string };
