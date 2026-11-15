use crate::errors::PoolError;
use crate::events::LoserClosed;
use crate::state::*;
use anchor_lang::prelude::*;

#[derive(Accounts)]
pub struct CloseLoserPosition<'info> {
    /// Anyone.
    pub signer: Signer<'info>,
    #[account(mut, seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump)]
    pub bet: Account<'info, Bet>,
    #[account(
        mut, close = rent_receiver,
        seeds = [POSITION_SEED, bet.key().as_ref(), position.owner.as_ref()], bump = position.bump,
        constraint = position.bet == bet.key() @ PoolError::Unauthorized
    )]
    pub position: Account<'info, Position>,
    /// CHECK: only receives lamports, and must be bet.payer.
    #[account(mut, address = bet.payer @ PoolError::WrongRentReceiver)]
    pub rent_receiver: UncheckedAccount<'info>,
}

pub fn close_loser_position_handler(ctx: Context<CloseLoserPosition>) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    let position = &ctx.accounts.position;
    require!(
        bet.state == BetState::Settled && position.side != bet.winning_side,
        PoolError::WrongState
    );
    bet.positions_open = bet.positions_open.checked_sub(1).ok_or(PoolError::MathOverflow)?;
    emit!(LoserClosed {
        bet: bet.key(),
        wallet: position.owner,
        side: position.side,
        stake: position.amount,
        positions_open: bet.positions_open,
    });
    Ok(())
}
