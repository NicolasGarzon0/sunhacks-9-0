use crate::errors::PoolError;
use crate::events::BetLocked;
use crate::state::*;
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct Lock<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut, has_one = authority @ PoolError::Unauthorized,
        seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump
    )]
    pub bet: Account<'info, Bet>,
}

pub fn lock_handler(ctx: Context<Lock>) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    require!(bet.state == BetState::Open, PoolError::WrongState);
    require!(Clock::get()?.unix_timestamp < bet.refund_after, PoolError::PastRefundDeadline);
    require!(
        bet.side_totals[0] > 0 && bet.side_totals[1] > 0 && bet.participant_count >= MIN_PARTICIPANTS,
        PoolError::BothSidesRequired
    );
    bet.state = BetState::Locked;
    emit!(BetLocked { bet: bet.key(), side_totals: bet.side_totals, participant_count: bet.participant_count });
    Ok(())
}
