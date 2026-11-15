use anchor_lang::prelude::*;

pub const BET_SEED: &[u8] = b"bet";
pub const VAULT_SEED: &[u8] = b"vault";
pub const POSITION_SEED: &[u8] = b"position";
pub const MAX_FEE_BPS: u16 = 1000;
pub const BPS: u128 = 10_000;
pub const MINT_DECIMALS: u8 = 6;
pub const NO_WINNER: u8 = 255;
pub const MIN_PARTICIPANTS: u16 = 2;
pub const MAX_PARTICIPANTS: u16 = 100;

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq, InitSpace, Debug)]
pub enum BetState {
    Open,
    Locked,
    Settled,
    Refunded,
}

#[account]
#[derive(InitSpace)]
pub struct Bet {
    pub bet_id: [u8; 16],
    /// The oracle key: the only key that may lock, settle, refund and close the bet.
    pub authority: Pubkey,
    /// The sponsor that paid rent. Receives all rent back.
    pub payer: Pubkey,
    /// Owner of the token account that must receive the fee.
    pub treasury: Pubkey,
    pub mint: Pubkey,
    pub fee_bps: u16,
    pub min_stake: u64,
    pub max_stake: u64,
    pub max_participants: u16,
    pub participant_count: u16,
    pub positions_open: u16,
    pub side_totals: [u64; 2],
    pub state: BetState,
    pub winning_side: u8,
    pub fee_paid: u64,
    pub distributable: u64,
    pub funding_closes_at: i64,
    /// From this time anyone may call emergency_refund, and lock / settle stop working.
    pub refund_after: i64,
    pub created_at: i64,
    pub bump: u8,
    pub vault_bump: u8,
}

#[account]
#[derive(InitSpace)]
pub struct Position {
    pub bet: Pubkey,
    pub owner: Pubkey,
    pub side: u8,
    pub amount: u64,
    pub bump: u8,
}
