"""DepositFlow: the public sponsored deposit and airdrop operations behind /markets/{token}/... (CHAIN_MODE=real only).

The backend never signs anything. The chain service builds the one transaction the contract needs, the user signs it
in their wallet, and the chain service verifies the exact bytes before the sponsor co-signs and sends. This class
validates the link and the dollar amounts, applies the airdrop policy, and keeps names and the early close in step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import templates as T
from app.logging import Logger, mask_handle
from app.ratelimit import RateLimiter
from app.repository import Bet

from .chain import ChainError, is_valid_wallet
from .money import format_usd
from .pool_math import implied_multiplier, preview_payout_for_deposit
from .service import MarketError, MarketService, clean_display_name


class DepositFlow:
    def __init__(self, market: MarketService, settings, logger: Logger, now=None) -> None:
        self.m, self.settings, self.logger = market, settings, logger
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.airdrop_ip = RateLimiter(settings.airdrop_per_ip_per_hour, 3600.0)

    @property
    def chain(self):
        return self.m.chain

    @staticmethod
    def _wallet(wallet: object) -> str:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        return wallet  # type: ignore[return-value]

    # --- wallet ---

    async def wallet_view(self, bet: Bet, wallet: object) -> dict:
        w = self._wallet(wallet)
        d = await self.chain.wallet(w)
        return {"ok": True, "wallet": w, "has_token_account": d["has_token_account"], "usdc_base": d["usdc_base"], "usdc_usd": format_usd(d["usdc_base"])}

    # --- airdrop ---

    async def airdrop(self, bet: Bet, wallet: object, ip: str) -> dict:
        s = self.settings
        if not s.airdrop_enabled:
            raise ChainError("AIRDROP_DISABLED", "the test USDC airdrop is not enabled")
        w = self._wallet(wallet)
        if not self.airdrop_ip.hit(ip):
            raise ChainError("RATE_LIMITED", "too many airdrop requests from this address, try again later")
        outcome, amount, _ = await self.m.repo.reserve_airdrop(
            w, bet.id, ip, s.airdrop_max_per_wallet_base, s.airdrop_daily_cap_base, s.airdrop_max_per_wallet_base, self.now()
        )
        if outcome == "daily_cap":
            self.logger.warn("airdrop.daily_cap", betId=bet.id)
            raise ChainError("AIRDROP_LIMIT", "the daily test USDC airdrop limit has been reached, try again tomorrow")
        if outcome == "full":  # already has its whole allowance: nothing to mint, just report
            return {**await self.wallet_view(bet, w), "minted_base": 0, "minted_usd": "0.00", "signature": None, "explorer": None}
        try:
            done = await self.chain.airdrop(w, amount)
        except BaseException:
            await self.m.repo.finish_airdrop(w, bet.id, None, False, amount, self.now())  # release the reservation
            raise
        await self.m.repo.finish_airdrop(w, bet.id, done.get("signature"), True, amount, self.now())
        self.logger.info("airdrop.minted", betId=bet.id, wallet=mask_handle(w), amountBase=amount)
        return {**await self.wallet_view(bet, w), "minted_base": amount, "minted_usd": format_usd(amount),
                "signature": done.get("signature"), "explorer": done.get("explorer")}

    # --- deposit ---

    def _check_amount(self, bet: Bet, amount: int) -> None:
        if amount < (bet.min_stake_base or 0):
            raise ChainError("BELOW_MIN", "amount is below the minimum")
        if amount > (bet.max_stake_base or 0):
            raise ChainError("ABOVE_MAX", "amount is above the maximum")

    async def deposit_tx(self, bet: Bet, wallet: object, side: object, amount_usd: object) -> dict:
        bet = await self.m._require_open(bet)
        w = self._wallet(wallet)
        s = self.m._parse_side(side)
        amount = self.m._parse_amount(amount_usd)
        self._check_amount(bet, amount)
        built = await self.chain.deposit_build(bet.id, w, s, amount)
        cb = await self.chain.get_bet(bet.id)
        existing = next((p.amount_base for p in await self.chain.get_positions(bet.id, [w]) if p.wallet == w and p.side == s), 0)
        fee_bps = bet.fee_bps or 0
        payout = preview_payout_for_deposit(cb.side_totals, s, amount, fee_bps, existing_stake=existing)
        after = list(cb.side_totals)
        after[s] += amount
        team = bet.side0_name if s == 0 else bet.side1_name
        mult = implied_multiplier((after[0], after[1]), s, fee_bps)
        total_stake = existing + amount
        summary = {
            "side": s, "team": team, "amount_base": amount, "amount_usd": format_usd(amount),
            "your_total_stake_base": total_stake, "your_total_stake_usd": format_usd(total_stake),
            "fee_bps": fee_bps, "fee_line": T.fee_line(fee_bps),
            "payout_base": payout, "payout_usd": format_usd(payout), "profit_base": payout - total_stake, "profit_usd": format_usd(payout - total_stake),
            "multiplier": mult,
            "text": (f"You are putting ${format_usd(amount)} on {team}. If {team} wins you would receive about ${format_usd(payout)}"
                     f" in total ({mult}x your stake), based on the pool right now. {T.fee_line(fee_bps)}"),
        }
        return {
            "ok": True, "bet_id": bet.id, "transaction_b64": built["transaction_b64"], "message_b64": built["message_b64"],
            "blockhash": built["blockhash"], "last_valid_block_height": built["last_valid_block_height"],
            "expires_at": built["expires_at"], "summary": summary,
        }

    async def deposit_submit(self, bet: Bet, wallet: object, side: object, amount_usd: object, *, signed_tx_b64: object = None,
                             message_b64: object = None, user_signature_b64: object = None, display_name: object = None) -> dict:
        name = clean_display_name(display_name) if display_name is not None else None  # refuse a bad name before any money moves
        bet = await self.m._require_open(bet)
        w = self._wallet(wallet)
        s = self.m._parse_side(side)
        amount = self.m._parse_amount(amount_usd)
        for label, value in (("signed_tx_b64", signed_tx_b64), ("message_b64", message_b64), ("user_signature_b64", user_signature_b64)):
            if value is not None and not isinstance(value, str):
                raise MarketError("BAD_REQUEST", f"{label} must be a base64 string")
        if signed_tx_b64 is None and (message_b64 is None or user_signature_b64 is None):
            raise MarketError("BAD_REQUEST", "send signed_tx_b64, or message_b64 together with user_signature_b64")
        await self.m.repo.record_participant(bet.id, w)  # before sending: the deposit may land even if the answer is lost
        done = await self.chain.deposit_submit(
            bet.id, w, s, amount, signed_tx_b64=signed_tx_b64, message_b64=message_b64, user_signature_b64=user_signature_b64  # type: ignore[arg-type]
        )
        self.logger.info("market.deposit_sent", betId=bet.id, wallet=mask_handle(w), side=s, amountBase=amount)
        if name is not None:
            await self.m.repo.set_display_name(bet.id, w, name)
        await self.m.maybe_close_when_full(bet.id)
        market = await self.m.market_view(await self.m.repo.get_bet(bet.id))  # type: ignore[arg-type]
        return {"ok": True, "signature": done["signature"], "explorer": done["explorer"], "already": bool(done.get("already")), "market": market}
