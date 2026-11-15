use anchor_lang::prelude::*;

#[error_code]
pub enum PoolError {
    #[msg("Deposit is below the minimum stake")]
    BelowMin,
    #[msg("Position would exceed the maximum stake")]
    AboveMax,
    #[msg("The market has no room for another participant")]
    MarketFull,
    #[msg("This wallet already holds the other side")]
    AlreadyOtherSide,
    #[msg("The market is not open")]
    MarketNotOpen,
    #[msg("The market is in the wrong state for this instruction")]
    WrongState,
    #[msg("This position is not on the winning side")]
    NotAWinner,
    #[msg("Side must be 0 or 1")]
    InvalidSide,
    #[msg("Funding has closed")]
    FundingClosed,
    #[msg("Both sides need money and at least 2 participants")]
    BothSidesRequired,
    #[msg("Fee is above the 10% cap")]
    FeeTooHigh,
    #[msg("Invalid parameters")]
    InvalidParams,
    #[msg("Unauthorized")]
    Unauthorized,
    #[msg("Math overflow")]
    MathOverflow,
    #[msg("Wrong mint")]
    WrongMint,
    #[msg("Wrong treasury token account")]
    WrongTreasury,
    #[msg("Wrong destination token account")]
    WrongDestination,
    #[msg("Rent receiver must be the bet payer")]
    WrongRentReceiver,
    #[msg("Positions are still open")]
    PositionsStillOpen,
    #[msg("The refund deadline has passed: lock and settle are closed")]
    PastRefundDeadline,
    #[msg("The refund deadline has not been reached yet")]
    RefundTooEarly,
}
