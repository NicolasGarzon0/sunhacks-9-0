//! Pool money. Mirrors backend/markets/pool_math.py. All u128, all checked.
//!   fee           = floor(losing_pool * fee_bps / 10000)
//!   distributable = total_pool - fee
//!   payout        = floor(stake * distributable / winning_side_total)
use crate::errors::PoolError;
use crate::state::BPS;
use anchor_lang::prelude::*;

fn to_u64(v: u128) -> Result<u64> {
    u64::try_from(v).map_err(|_| error!(PoolError::MathOverflow))
}

pub fn fee(losing_pool: u64, fee_bps: u16) -> Result<u64> {
    let v = (losing_pool as u128)
        .checked_mul(fee_bps as u128)
        .and_then(|x| x.checked_div(BPS))
        .ok_or(PoolError::MathOverflow)?;
    to_u64(v)
}

pub fn distributable(total_pool: u64, fee: u64) -> Result<u64> {
    total_pool.checked_sub(fee).ok_or(error!(PoolError::MathOverflow))
}

pub fn payout(stake: u64, distributable: u64, winning_total: u64) -> Result<u64> {
    let v = (stake as u128)
        .checked_mul(distributable as u128)
        .and_then(|x| x.checked_div(winning_total as u128))
        .ok_or(PoolError::MathOverflow)?;
    to_u64(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    const USDC: u64 = 1_000_000;

    fn settle(totals: [u64; 2], win: usize, bps: u16) -> (u64, u64) {
        let f = fee(totals[1 - win], bps).unwrap();
        (f, distributable(totals[0] + totals[1], f).unwrap())
    }

    #[test]
    fn balanced_pool() {
        let (f, d) = settle([100 * USDC, 100 * USDC], 0, 500);
        assert_eq!((f, d), (5 * USDC, 195 * USDC));
        assert_eq!(payout(100 * USDC, d, 100 * USDC).unwrap(), 195 * USDC);
    }

    #[test]
    fn thirteen_dollars_per_side() {
        let (f, d) = settle([13 * USDC, 13 * USDC], 1, 500);
        assert_eq!((f, d), (650_000, 25_350_000));
        assert_eq!(payout(13 * USDC, d, 13 * USDC).unwrap(), 25_350_000); // 1.95x
    }

    #[test]
    fn lopsided_pool() {
        let (f, d) = settle([300 * USDC, 100 * USDC], 1, 500);
        assert_eq!((f, d), (15 * USDC, 385 * USDC));
        let (f, d) = settle([300 * USDC, 100 * USDC], 0, 500);
        assert_eq!((f, d), (5 * USDC, 395 * USDC));
        assert_eq!(payout(30 * USDC, d, 300 * USDC).unwrap(), 39_500_000);
    }

    #[test]
    fn zero_losing_pool_no_fee() {
        let (f, d) = settle([100 * USDC, 0], 0, 500);
        assert_eq!((f, d), (0, 100 * USDC));
        assert_eq!(payout(40 * USDC, d, 100 * USDC).unwrap(), 40 * USDC);
    }

    #[test]
    fn rounding_dust_stays() {
        let (_, d) = settle([3, 1], 0, 0);
        assert_eq!(d, 4);
        let p = payout(1, d, 3).unwrap();
        assert_eq!(p, 1);
        assert_eq!(d - 3 * p, 1);
    }

    #[test]
    fn fee_floors() {
        assert_eq!(fee(19, 500).unwrap(), 0);
        assert_eq!(fee(20 * USDC, 500).unwrap(), USDC);
        assert_eq!(fee(1000, 1000).unwrap(), 100);
    }

    #[test]
    fn winner_never_below_stake_randomized() {
        // deterministic LCG, no extra dependency
        let mut x: u64 = 7;
        let mut next = |m: u64| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (x >> 33) % m
        };
        for _ in 0..3000 {
            let n = 1 + next(8) as usize;
            let stakes: Vec<u64> = (0..n).map(|_| 1 + next(20 * USDC)).collect();
            let win: u64 = stakes.iter().sum();
            let losing = if next(2) == 0 { 0 } else { 1 + next(160 * USDC) };
            let bps = [0u16, 1, 250, 500, 999, 1000][next(6) as usize];
            let (f, d) = settle([win, losing], 0, bps);
            let paid: Vec<u64> = stakes.iter().map(|s| payout(*s, d, win).unwrap()).collect();
            assert!(paid.iter().zip(&stakes).all(|(p, s)| p >= s));
            assert!(paid.iter().sum::<u64>() <= d);
            assert!(d - paid.iter().sum::<u64>() < n as u64);
            assert!(f as u128 * 10_000 <= losing as u128 * bps as u128);
        }
    }

    #[test]
    fn overflow_is_an_error_not_a_wrap() {
        assert!(distributable(1, 2).is_err());
        assert!(payout(u64::MAX, u64::MAX, 1).is_err()); // quotient does not fit u64
        assert!(payout(1, 1, 0).is_err()); // division by zero is checked
    }
}
