use crate::errors::PoolError;
use crate::events::EmergencyRefunded;
use crate::state::*;
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct EmergencyRefund<'info> {
    /// Anyone. Needs no authority: this is the exit when the oracle never settles.
    pub signer: Signer<'info>,
    #[account(mut, seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump)]
    pub bet: Account<'info, Bet>,
}

pub fn emergency_refund_handler(ctx: Context<EmergencyRefund>) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    let previous_state = match bet.state {
        BetState::Open => 0u8,
        BetState::Locked => 1u8,
        _ => return err!(PoolError::WrongState),
    };
    require!(Clock::get()?.unix_timestamp >= bet.refund_after, PoolError::RefundTooEarly);
    bet.state = BetState::Refunded;
    bet.winning_side = NO_WINNER;
    emit!(EmergencyRefunded {
        bet: bet.key(),
        caller: ctx.accounts.signer.key(),
        previous_state,
        side_totals: bet.side_totals,
        participant_count: bet.participant_count,
    });
    Ok(())
}
