import type { MarketView, Participant, Position } from "../src/api/types";

export const WALLET_B58 = "ignored";

export function market(over: Partial<MarketView> = {}): MarketView {
  return {
    ok: true, bet_id: "b1", status: "open", league: "nfl",
    teams: { side0: { name: "Seattle Seahawks", role: "away" }, side1: { name: "Arizona Cardinals", role: "home" } },
    kickoff_utc: new Date(Date.now() + 3_600_000).toISOString(),
    funding_closes_at: new Date(Date.now() + 1_800_000).toISOString(),
    pools: { side0_base: 12_000_000, side1_base: 15_250_000, side0_usd: "12.00", side1_usd: "15.25" },
    total_pool_base: 27_250_000, total_pool_usd: "27.25", participant_count: 4,
    limits: { min_stake_base: 1_000_000, min_stake_usd: "1.00", max_stake_base: 20_000_000, max_stake_usd: "20.00", max_participants: 20 },
    fee_bps: 500, fee_line: "A 5% fee on the losing side covers operating costs.",
    multipliers: { side0: "2.27", side1: "1.95" },
    ...over,
  };
}

export function people(): Participant[] {
  return [
    { wallet: "Aaaa1111111111111111111111111111111111111111", side: 0, stake_base: 7_000_000, stake_usd: "7.00", display_name: "Dana" },
    { wallet: "Bbbb2222222222222222222222222222222222222222", side: 1, stake_base: 9_000_000, stake_usd: "9.00", display_name: null },
  ];
}

export function position(over: Partial<Position> = {}): Position {
  return {
    ok: true, status: "open", wallet: "w", display_name: null, has_position: false, side: null, stake_base: null, stake_usd: null,
    payout_base: null, payout_usd: null, paid: false, ...over,
  };
}
