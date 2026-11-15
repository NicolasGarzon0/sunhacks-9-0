use crate::errors::PoolError;
use crate::events::Deposited;
use crate::state::*;
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{transfer_checked, Mint, TokenAccount, TokenInterface, TransferChecked};

#[derive(Accounts)]
pub struct Deposit<'info> {
    /// The user. Signs and authorizes the token transfer.
    pub depositor: Signer<'info>,
    /// The sponsor. Second signer: pays Position rent and must be bet.payer.
    #[account(mut, address = bet.payer @ PoolError::Unauthorized)]
    pub rent_payer: Signer<'info>,
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
        init_if_needed, payer = rent_payer, space = 8 + Position::INIT_SPACE,
        seeds = [POSITION_SEED, bet.key().as_ref(), depositor.key().as_ref()], bump
    )]
    pub position: Account<'info, Position>,
    #[account(
        mut,
        constraint = depositor_token.mint == bet.mint @ PoolError::WrongMint,
        constraint = depositor_token.owner == depositor.key() @ PoolError::Unauthorized
    )]
    pub depositor_token: InterfaceAccount<'info, TokenAccount>,
    pub token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

pub fn deposit_handler(ctx: Context<Deposit>, side: u8, amount: u64) -> Result<()> {
    let bet = &mut ctx.accounts.bet;
    let position = &mut ctx.accounts.position;
    let depositor = ctx.accounts.depositor.key();

    // Error priority is part of the spec: state, funding window, side, min, then cap / side match / max.
    require!(bet.state == BetState::Open, PoolError::MarketNotOpen);
    require!(Clock::get()?.unix_timestamp < bet.funding_closes_at, PoolError::FundingClosed);
    require!(side <= 1, PoolError::InvalidSide);
    require!(amount >= bet.min_stake, PoolError::BelowMin);

    // A freshly created Position is all zeros. An existing one always has a non default owner, so it can never be
    // re-initialised or reassigned: the new-position branch is unreachable for it.
    let is_new = position.owner == Pubkey::default();
    if is_new {
        require!(position.amount == 0 && position.bet == Pubkey::default(), PoolError::Unauthorized);
        require!(bet.participant_count < bet.max_participants, PoolError::MarketFull);
    } else {
        require!(position.owner == depositor && position.bet == bet.key(), PoolError::Unauthorized);
        require!(position.side == side, PoolError::AlreadyOtherSide);
    }
    let new_amount = position.amount.checked_add(amount).ok_or(PoolError::MathOverflow)?;
    require!(new_amount <= bet.max_stake, PoolError::AboveMax);

    transfer_checked(
        CpiContext::new(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.depositor_token.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.vault.to_account_info(),
                authority: ctx.accounts.depositor.to_account_info(),
            },
        ),
        amount,
        MINT_DECIMALS,
    )?;

    let idx = side as usize;
    bet.side_totals[idx] = bet.side_totals[idx].checked_add(amount).ok_or(PoolError::MathOverflow)?;
    if is_new {
        position.bet = bet.key();
        position.owner = depositor;
        position.side = side;
        position.bump = ctx.bumps.position;
        bet.participant_count = bet.participant_count.checked_add(1).ok_or(PoolError::MathOverflow)?;
        bet.positions_open = bet.positions_open.checked_add(1).ok_or(PoolError::MathOverflow)?;
    }
    position.amount = new_amount;

    emit!(Deposited {
        bet: bet.key(),
        wallet: depositor,
        side,
        amount,
        position_amount: new_amount,
        new_position: is_new,
        side_totals: bet.side_totals,
        participant_count: bet.participant_count,
    });
    Ok(())
}
