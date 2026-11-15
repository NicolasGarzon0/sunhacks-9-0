use crate::errors::PoolError;
use crate::state::*;
use anchor_lang::prelude::*;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

#[derive(Accounts)]
#[instruction(bet_id: [u8; 16])]
pub struct CreateBet<'info> {
    /// Sponsor: pays rent for the Bet and the Vault and is stored as bet.payer.
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(
        init, payer = payer, space = 8 + Bet::INIT_SPACE,
        seeds = [BET_SEED, bet_id.as_ref()], bump
    )]
    pub bet: Account<'info, Bet>,
    pub mint: InterfaceAccount<'info, Mint>,
    #[account(
        init, payer = payer,
        seeds = [VAULT_SEED, bet.key().as_ref()], bump,
        token::mint = mint, token::authority = bet, token::token_program = token_program
    )]
    pub vault: InterfaceAccount<'info, TokenAccount>,
    /// Classic SPL Token only: Token-2022 extensions (transfer fees, hooks) would break the accounting.
    #[account(constraint = token_program.key() == anchor_spl::token::ID @ PoolError::WrongMint)]
    pub token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

#[allow(clippy::too_many_arguments)]
pub fn create_bet_handler(
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
    require!(fee_bps <= MAX_FEE_BPS, PoolError::FeeTooHigh);
    let now = Clock::get()?.unix_timestamp;
    require!(
        min_stake > 0
            && min_stake <= max_stake
            && (MIN_PARTICIPANTS..=MAX_PARTICIPANTS).contains(&max_participants)
            && funding_closes_at > now
            && refund_after > funding_closes_at
            && ctx.accounts.mint.decimals == MINT_DECIMALS
            && authority != Pubkey::default()
            && treasury != Pubkey::default(),
        PoolError::InvalidParams
    );
    let bet = &mut ctx.accounts.bet;
    bet.bet_id = bet_id;
    bet.authority = authority;
    bet.payer = ctx.accounts.payer.key();
    bet.treasury = treasury;
    bet.mint = ctx.accounts.mint.key();
    bet.fee_bps = fee_bps;
    bet.min_stake = min_stake;
    bet.max_stake = max_stake;
    bet.max_participants = max_participants;
    bet.participant_count = 0;
    bet.positions_open = 0;
    bet.side_totals = [0, 0];
    bet.state = BetState::Open;
    bet.winning_side = NO_WINNER;
    bet.fee_paid = 0;
    bet.distributable = 0;
    bet.funding_closes_at = funding_closes_at;
    bet.refund_after = refund_after;
    bet.created_at = now;
    bet.bump = ctx.bumps.bet;
    bet.vault_bump = ctx.bumps.vault;
    Ok(())
}
