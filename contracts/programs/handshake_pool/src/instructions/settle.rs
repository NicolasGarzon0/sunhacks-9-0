use crate::errors::PoolError;
use crate::events::BetSettled;
use crate::math;
use crate::state::*;
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{transfer_checked, Mint, TokenAccount, TokenInterface, TransferChecked};

#[derive(Accounts)]
pub struct Settle<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut, has_one = authority @ PoolError::Unauthorized,
        seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump
    )]
    pub bet: Account<'info, Bet>,
    #[account(
        mut, seeds = [VAULT_SEED, bet.key().as_ref()], bump = bet.vault_bump,
        constraint = vault.mint == bet.mint @ PoolError::WrongMint
    )]
    pub vault: InterfaceAccount<'info, TokenAccount>,
    #[account(address = bet.mint @ PoolError::WrongMint)]
    pub mint: InterfaceAccount<'info, Mint>,
    #[account(
        mut,
        constraint = treasury_token.owner == bet.treasury @ PoolError::WrongTreasury,
        constraint = treasury_token.mint == bet.mint @ PoolError::WrongTreasury
    )]
    pub treasury_token: InterfaceAccount<'info, TokenAccount>,
    pub token_program: Interface<'info, TokenInterface>,
}

pub fn settle_handler(ctx: Context<Settle>, winning_side: u8) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    require!(bet.state == BetState::Locked, PoolError::WrongState);
    require!(Clock::get()?.unix_timestamp < bet.refund_after, PoolError::PastRefundDeadline);
    require!(winning_side <= 1, PoolError::InvalidSide);

    let losing_pool = bet.side_totals[1 - winning_side as usize];
    let total = bet.side_totals[0].checked_add(bet.side_totals[1]).ok_or(PoolError::MathOverflow)?;
    let fee = math::fee(losing_pool, bet.fee_bps)?;
    let distributable = math::distributable(total, fee)?;

    if fee > 0 {
        let seeds: &[&[u8]] = &[BET_SEED, bet.bet_id.as_ref(), &[bet.bump]];
        transfer_checked(
            CpiContext::new_with_signer(
                ctx.accounts.token_program.key(),
                TransferChecked {
                    from: ctx.accounts.vault.to_account_info(),
                    mint: ctx.accounts.mint.to_account_info(),
                    to: ctx.accounts.treasury_token.to_account_info(),
                    authority: bet.to_account_info(),
                },
                &[seeds],
            ),
            fee,
            MINT_DECIMALS,
        )?;
    }
    bet.state = BetState::Settled;
    bet.winning_side = winning_side;
    bet.fee_paid = fee;
    bet.distributable = distributable;
    emit!(BetSettled { bet: bet.key(), winning_side, fee, distributable, side_totals: bet.side_totals });
    Ok(())
}
