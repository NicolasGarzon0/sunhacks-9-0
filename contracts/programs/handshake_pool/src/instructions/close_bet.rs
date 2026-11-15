use crate::errors::PoolError;
use crate::events::BetClosed;
use crate::state::*;
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{
    close_account, transfer_checked, CloseAccount, Mint, TokenAccount, TokenInterface, TransferChecked,
};

#[derive(Accounts)]
pub struct CloseBet<'info> {
    pub authority: Signer<'info>,
    #[account(
        mut, close = payer,
        has_one = authority @ PoolError::Unauthorized,
        has_one = payer @ PoolError::WrongRentReceiver,
        seeds = [BET_SEED, bet.bet_id.as_ref()], bump = bet.bump
    )]
    pub bet: Account<'info, Bet>,
    /// CHECK: only receives lamports; pinned to bet.payer by has_one.
    #[account(mut)]
    pub payer: UncheckedAccount<'info>,
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

pub fn close_bet_handler(ctx: Context<CloseBet>) -> Result<()> {
    let bet = &ctx.accounts.bet;
    require!(matches!(bet.state, BetState::Settled | BetState::Refunded), PoolError::WrongState);
    require!(bet.positions_open == 0, PoolError::PositionsStillOpen);

    let seeds: &[&[u8]] = &[BET_SEED, bet.bet_id.as_ref(), &[bet.bump]];
    let dust = ctx.accounts.vault.amount;
    if dust > 0 {
        transfer_checked(
            CpiContext::new_with_signer(
                ctx.accounts.token_program.key(),
                TransferChecked {
                    from: ctx.accounts.vault.to_account_info(),
                    mint: ctx.accounts.mint.to_account_info(),
                    to: ctx.accounts.treasury_token.to_account_info(),
                    authority: ctx.accounts.bet.to_account_info(),
                },
                &[seeds],
            ),
            dust,
            MINT_DECIMALS,
        )?;
    }
    close_account(CpiContext::new_with_signer(
        ctx.accounts.token_program.key(),
        CloseAccount {
            account: ctx.accounts.vault.to_account_info(),
            destination: ctx.accounts.payer.to_account_info(),
            authority: ctx.accounts.bet.to_account_info(),
        },
        &[seeds],
    ))?;
    emit!(BetClosed { bet: bet.key(), dust_to_treasury: dust });
    Ok(())
}
