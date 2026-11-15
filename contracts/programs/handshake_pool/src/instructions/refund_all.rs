use crate::errors::PoolError;
use crate::events::BetRefunded;
use crate::state::*;
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct RefundAll<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut, has_one = authority @ PoolError::Unauthorized,
        seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump
    )]
    pub bet: Account<'info, Bet>,
}

pub fn refund_all_handler(ctx: Context<RefundAll>) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    require!(matches!(bet.state, BetState::Open | BetState::Locked), PoolError::WrongState);
    bet.state = BetState::Refunded;
    emit!(BetRefunded { bet: bet.key(), side_totals: bet.side_totals, participant_count: bet.participant_count });
    Ok(())
}
