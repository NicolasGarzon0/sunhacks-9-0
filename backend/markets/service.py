from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.gateway_client import GatewayClient
from app.logging import Logger, mask_handle
from app.repository import Repository

from .chain import BetParams, ChainClient, ChainError
from .links import LinkError, build_link, link_expired, make_token, parse_token
from .money import base_to_usd, bps_to_percent, format_usd
from .pool_math import preview_payout_for_deposit


class MarketError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class MarketService:
    def __init__(
        self,
        repo: Repository,
        chain: ChainClient,
        gateway: GatewayClient,
        logger: Logger,
        settings,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.chain = chain
        self.gateway = gateway
        self.logger = logger
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, bet_id: str) -> asyncio.Lock:
        return self._locks.setdefault(bet_id, asyncio.Lock())

    async def resolve(self, token: str) -> dict:
        try:
            bet_id = parse_token(self.settings.link_signing_secret, token)
        except LinkError as exc:
            raise MarketError(exc.code, "invalid market link", 404) from exc
        market = await self.repo.get_market(bet_id)
        if market is None or market["status"] == "failed":
            raise MarketError("BAD_TOKEN", "invalid market link", 404)
        if link_expired(
            self.now(), market["payouts_completed_at"], market["settle_after"], self.settings.link_grace_hours
        ):
            raise MarketError("LINK_EXPIRED", "market link has expired", 410)
        market["token"] = token
        return market

    def token_for(self, bet_id: str) -> str:
        return make_token(self.settings.link_signing_secret, bet_id)

    def link_for(self, bet_id: str) -> str:
        return build_link(self.settings.deposit_page_url, self.token_for(bet_id))

    async def view(self, market: dict) -> dict:
        bet = await self.chain.get_bet(market["id"]) if market["chain_bet_id"] else None
        positions = await self.chain.list_positions(market["id"]) if bet else []
        side0 = sum(p.amount_base for p in positions if p.side == 0)
        side1 = sum(p.amount_base for p in positions if p.side == 1)
        return {
            "id": market["id"],
            "status": market["status"],
            "game": {
                "league": market["league"],
                "away": market["side0_name"],
                "home": market["side1_name"],
                "kickoff": market["kickoff_utc"].isoformat(),
            },
            "fundingClosesAt": market["funding_closes_at"].isoformat(),
            "settleAfter": market["settle_after"].isoformat(),
            "limits": {
                "minBase": market["min_stake_base"],
                "min": base_to_usd(market["min_stake_base"]),
                "maxBase": market["max_stake_base"],
                "max": base_to_usd(market["max_stake_base"]),
                "maxParticipants": market["max_participants"],
            },
            "feeBps": market["fee_bps"],
            "fee": bps_to_percent(market["fee_bps"]),
            "pool": {"totalBase": side0 + side1, "total": base_to_usd(side0 + side1), "side0Base": side0, "side1Base": side1},
            "link": self.link_for(market["id"]),
        }

    async def participants(self, market: dict) -> list[dict]:
        return await self.repo.list_participants(market["id"])

    async def position(self, market: dict, wallet: str) -> dict | None:
        for position in await self.chain.list_positions(market["id"]):
            if position.wallet == wallet:
                return {
                    "wallet": mask_handle(wallet),
                    "side": position.side,
                    "amountBase": position.amount_base,
                    "amount": base_to_usd(position.amount_base),
                    "claimed": position.claimed,
                    "payoutBase": position.payout_base,
                    "payout": base_to_usd(position.payout_base),
                }
        return None

    async def set_display_name(self, market: dict, wallet: str, display_name: str) -> dict:
        if (
            not display_name.strip()
            or len(display_name.strip()) > 24
            or "http://" in display_name.lower()
            or "https://" in display_name.lower()
            or "www." in display_name.lower()
        ):
            raise MarketError("BAD_REQUEST", "display name must be at most 24 characters and contain no URL", 400)
        return await self.repo.set_participant_name(market["id"], wallet, display_name.strip())

    async def preview(self, market: dict, side: int, amount_base: int) -> dict:
        if market["status"] != "open":
            raise MarketError("MARKET_NOT_OPEN", "market is not open")
        if side not in (0, 1) or amount_base < market["min_stake_base"] or amount_base > market["max_stake_base"]:
            raise MarketError("INVALID_AMOUNT", "deposit is outside the configured limits")
        positions = await self.chain.list_positions(market["id"])
        side0 = sum(p.amount_base for p in positions if p.side == 0)
        side1 = sum(p.amount_base for p in positions if p.side == 1)
        payout = preview_payout_for_deposit(side, amount_base, side0, side1, market["fee_bps"])
        return {"side": side, "amountBase": amount_base, "amount": base_to_usd(amount_base), "payoutBase": payout, "payout": base_to_usd(payout)}

    async def deposit(self, market: dict, wallet: str, side: int, amount_base: int) -> dict:
        if market["status"] != "open":
            raise MarketError("MARKET_NOT_OPEN", "market is not open")
        try:
            position = await self.chain.deposit(market["id"], wallet, side, amount_base, self.now())
        except ChainError as exc:
            raise MarketError(exc.code, exc.detail) from exc
        await self.repo.set_participant_name(market["id"], wallet, "")
        positions = await self.chain.list_positions(market["id"])
        await self.repo.update_market(
            market["id"],
            total_pool_base=sum(p.amount_base for p in positions),
        )
        await self.maybe_close_when_full(market["id"])
        return {
            "wallet": mask_handle(position.wallet),
            "side": position.side,
            "amountBase": position.amount_base,
            "amount": base_to_usd(position.amount_base),
        }

    async def open_market(self, proposer_handle: str, game: dict) -> None:
        now = self.now()
        values = {
            "side0_name": game["away"]["name"],
            "side1_name": game["home"]["name"],
            "fee_bps": self.settings.fee_bps,
            "treasury": self.settings.treasury_pubkey,
            "min_stake_base": self.settings.min_stake_base,
            "max_stake_base": self.settings.max_stake_base,
            "max_participants": self.settings.max_participants,
            "funding_closes_at": game["kickoff_utc"] - timedelta(minutes=self.settings.funding_minutes_before_kickoff),
            "settle_after": game["kickoff_utc"] + timedelta(hours=self.settings.settle_hours_after_kickoff),
        }
        if game["kickoff_utc"] <= now or values["funding_closes_at"] <= now:
            raise MarketError("GAME_NOT_OPEN", "that game is no longer available for betting", 409)
        market = await self.repo.create_or_get_market(proposer_handle, game, values)
        if market["status"] == "locked":
            await self.notify_once(market, "link", self.link_message(market))
            return
        try:
            params = BetParams(
                values["min_stake_base"], values["max_stake_base"], values["max_participants"],
                values["fee_bps"], values["treasury"], values["funding_closes_at"],
            )
            chain_bet = await self.chain.create_bet(market["id"], params)
            await self.repo.update_market(market["id"], status="open", chain_bet_id=chain_bet.bet_id, opened_at=now)
            market["status"] = "open"
            market["chain_bet_id"] = chain_bet.bet_id
            await self.notify_once(market, "link", self.link_message(market))
            return
        except Exception as exc:
            await self.repo.update_market(market["id"], status="failed", failure_reason=str(exc))
            try:
                await self.chain.refund_all(market["id"])
            except Exception:
                self.logger.error("market.refund_failed", betId=market["id"])
            await self.notify_once(market, "failed", "I could not open that market. Please try again.")
            raise MarketError("OPEN_FAILED", "market could not be opened") from exc

    def link_message(self, market: dict) -> str:
        return (
            f"{market['side0_name']} at {market['side1_name']} is open for betting. "
            f"Funding closes {market['funding_closes_at'].isoformat()}. "
            f"Forward this link to anyone who wants in: {self.link_for(market['id'])}. "
            f"Deposits are {format_usd(market['min_stake_base'])} to {format_usd(market['max_stake_base'])}. "
            f"A {bps_to_percent(market['fee_bps'])} fee on the losing side covers operating costs."
        )

    async def notify_once(self, market: dict, kind: str, text: str) -> None:
        key = f"bet:{market['id']}:{kind}"
        if not await self.repo.notice_attempt(market["id"], kind, key):
            return
        result = await self.gateway.send_text(market["proposer_handle"], text, key)
        await self.repo.notice_result(market["id"], kind, result.ok, result.detail)

    async def close_funding(self, bet_id: str) -> None:
        async with self._lock(bet_id):
            market = await self.repo.get_market(bet_id)
            if market is None:
                raise MarketError("NOT_FOUND", "unknown bet", 404)
            if market["status"] not in ("open", "locked", "refunded"):
                return
            if market["status"] != "open":
                return
            positions = await self.chain.list_positions(bet_id)
            sides = {p.side for p in positions}
            if len(positions) >= 2 and sides == {0, 1}:
                await self.chain.lock(bet_id)
                await self.repo.update_market(bet_id, status="locked", locked_at=self.now())
                await self.notify_once(market, "locked", "Funding is closed and this market is locked.")
            else:
                await self.chain.refund_all(bet_id)
                reason = "too_few_participants" if len(positions) < 2 else "one_sided"
                await self.repo.update_market(bet_id, status="refunded", refund_reason=reason)
                await self.notify_once(market, "refunded", "This market was refunded because it did not have action on both sides.")

    async def maybe_close_when_full(self, bet_id: str) -> None:
        market = await self.repo.get_market(bet_id)
        if market is None or market["status"] != "open":
            return
        positions = await self.chain.list_positions(bet_id)
        if len(positions) >= market["max_participants"]:
            await self.close_funding(bet_id)
