use anchor_lang::prelude::*;

#[event]
pub struct Deposited {
    pub bet: Pubkey,
    pub wallet: Pubkey,
    pub side: u8,
    pub amount: u64,
    pub position_amount: u64,
    pub new_position: bool,
    pub side_totals: [u64; 2],
    pub participant_count: u16,
}

#[event]
pub struct BetLocked {
    pub bet: Pubkey,
    pub side_totals: [u64; 2],
    pub participant_count: u16,
}

#[event]
pub struct BetSettled {
    pub bet: Pubkey,
    pub winning_side: u8,
    pub fee: u64,
    pub distributable: u64,
    pub side_totals: [u64; 2],
}

#[event]
pub struct BetRefunded {
    pub bet: Pubkey,
    pub side_totals: [u64; 2],
    pub participant_count: u16,
}

#[event]
pub struct Claimed {
    pub bet: Pubkey,
    pub wallet: Pubkey,
    pub side: u8,
    pub stake: u64,
    pub payout: u64,
    pub refund: bool,
    pub positions_open: u16,
}

#[event]
pub struct LoserClosed {
    pub bet: Pubkey,
    pub wallet: Pubkey,
    pub side: u8,
    pub stake: u64,
    pub positions_open: u16,
}

#[event]
pub struct BetClosed {
    pub bet: Pubkey,
    pub dust_to_treasury: u64,
}

#[event]
pub struct EmergencyRefunded {
    pub bet: Pubkey,
    pub caller: Pubkey,
    pub previous_state: u8, // 0 = Open, 1 = Locked
    pub side_totals: [u64; 2],
    pub participant_count: u16,
}
