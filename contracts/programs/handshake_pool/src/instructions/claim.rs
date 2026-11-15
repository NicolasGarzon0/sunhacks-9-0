use crate::errors::PoolError;
use crate::events::Claimed;
use crate::math;
use crate::state::*;
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{transfer_checked, Mint, TokenAccount, TokenInterface, TransferChecked};

#[derive(Accounts)]
pub struct Claim<'info> {
    /// Anyone. The app signs and pays the transaction fee on behalf of the winner.
    pub signer: Signer<'info>,
    #[account(mut, seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump)]
    pub bet: Account<'info, Bet>,
    #[account(
        mut, seeds = [VAULT_SEED, bet.key().as_ref()], bump = bet.vault_bump,
        constraint = vault.mint == bet.mint @ PoolError::WrongMint
    )]
    pub vault: InterfaceAccount<'info, TokenAccount>,
    #[account(address = bet.mint @ PoolError::WrongMint)]
    pub mint: InterfaceAccount<'info, Mint>,
    #[account(
        mut, close = rent_receiver,
        seeds = [POSITION_SEED, bet.key().as_ref(), position.owner.as_ref()], bump = position.bump,
        constraint = position.bet == bet.key() @ PoolError::Unauthorized
    )]
    pub position: Account<'info, Position>,
    #[account(
        mut,
        constraint = owner_token.owner == position.owner @ PoolError::WrongDestination,
        constraint = owner_token.mint == bet.mint @ PoolError::WrongDestination
    )]
    pub owner_token: InterfaceAccount<'info, TokenAccount>,
    /// CHECK: only receives lamports, and must be bet.payer.
    #[account(mut, address = bet.payer @ PoolError::WrongRentReceiver)]
    pub rent_receiver: UncheckedAccount<'info>,
    pub token_program: Interface<'info, TokenInterface>,
}

pub fn claim_handler(ctx: Context<Claim>) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    let position = &ctx.accounts.position;
    let (payout, refund) = match bet.state {
        BetState::Settled => {
            require!(position.side == bet.winning_side, PoolError::NotAWinner);
            let winning_total = bet.side_totals[bet.winning_side as usize];
            (math::payout(position.amount, bet.distributable, winning_total)?, false)
        }
        BetState::Refunded => (position.amount, true),
        _ => return err!(PoolError::WrongState),
    };

    let seeds: &[&[u8]] = &[BET_SEED, bet.bet_id.as_ref(), &[bet.bump]];
    transfer_checked(
        CpiContext::new_with_signer(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.vault.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.owner_token.to_account_info(),
                authority: bet.to_account_info(),
            },
            &[seeds],
        ),
        payout,
        MINT_DECIMALS,
    )?;
    bet.positions_open = bet.positions_open.checked_sub(1).ok_or(PoolError::MathOverflow)?;
    emit!(Claimed {
        bet: bet.key(),
        wallet: position.owner,
        side: position.side,
        stake: position.amount,
        payout,
        refund,
        positions_open: bet.positions_open,
    });
    Ok(())
}
