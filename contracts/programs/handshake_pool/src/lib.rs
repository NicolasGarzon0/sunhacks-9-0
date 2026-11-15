pub mod errors;
pub mod events;
pub mod instructions;
pub mod math;
pub mod state;

use anchor_lang::prelude::*;

pub use instructions::*;
pub use state::*;

declare_id!("HmgmQJP136sLcH1gQDywG7MeUX9xZN6wZMDdzsgf9X1c");

#[program]
pub mod handshake_pool {
    use super::*;

    #[allow(clippy::too_many_arguments)]
    pub fn create_bet(
        ctx: Context<CreateBet>,
        bet_id: [u8; 16],
        authority: Pubkey,
        treasury: Pubkey,
        fee_bps: u16,
        min_stake: u64,
        max_stake: u64,
        max_participants: u16,
        funding_closes_at: i64,
        refund_after: i64,
    ) -> Result<()> {
        instructions::create_bet::create_bet_handler(
            ctx, bet_id, authority, treasury, fee_bps, min_stake, max_stake, max_participants, funding_closes_at, refund_after,
        )
    }

    pub fn deposit(ctx: Context<Deposit>, side: u8, amount: u64) -> Result<()> {
        instructions::deposit::deposit_handler(ctx, side, amount)
    }

    pub fn lock(ctx: Context<Lock>) -> Result<()> {
        instructions::lock::lock_handler(ctx)
    }

    pub fn settle(ctx: Context<Settle>, winning_side: u8) -> Result<()> {
        instructions::settle::settle_handler(ctx, winning_side)
    }

    pub fn refund_all(ctx: Context<RefundAll>) -> Result<()> {
        instructions::refund_all::refund_all_handler(ctx)
    }

    pub fn emergency_refund(ctx: Context<EmergencyRefund>) -> Result<()> {
        instructions::emergency_refund::emergency_refund_handler(ctx)
    }

    pub fn claim(ctx: Context<Claim>) -> Result<()> {
        instructions::claim::claim_handler(ctx)
    }

    pub fn close_loser_position(ctx: Context<CloseLoserPosition>) -> Result<()> {
        instructions::close_loser_position::close_loser_position_handler(ctx)
    }

    pub fn close_bet(ctx: Context<CloseBet>) -> Result<()> {
        instructions::close_bet::close_bet_handler(ctx)
    }
}
