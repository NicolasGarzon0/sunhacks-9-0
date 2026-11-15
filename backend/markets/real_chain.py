"""RealChainClient: the ChainClient interface over HTTP to chain-service (which holds the payer and oracle keys).

The backend never holds a key. Every endpoint of the service is idempotent (an already done change returns
already:true), so every call may be retried on transport trouble, 429, 5xx and RPC_ERROR. Business errors
(WRONG_STATE, NOT_A_WINNER, ...) are final answers and are never retried. The secret is only ever sent in a header.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx

from .chain import ERROR_CODES, BetParams, ChainBet, ChainError, Claim, Position, is_valid_wallet

RETRY_STATUS = {429, 502, 503, 504}
CODE_MAP = {"BAD_REQUEST": "INVALID_PARAMS", "UNAUTHORIZED": "RPC_ERROR"}  # service codes the backend has no twin for


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _chain_bet(bet_id: str, d: dict) -> ChainBet:
    totals = d["side_totals"]
    return ChainBet(
        bet_id=bet_id, state=d["state"], side_totals=(int(totals[0]), int(totals[1])),
        participant_count=int(d["participant_count"]), fee_paid_base=int(d["fee_paid_base"]),
        distributable_base=int(d["distributable_base"]), winning_side=d["winning_side"],
        vault_base=int(d.get("vault_base", 0)), treasury_balance_base=int(d.get("treasury_balance_base", 0)),
        positions_open=int(d["positions_open"]),
    )


class RealChainClient:
    mode = "real"

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retries: int = 3,
        backoff: float = 0.5,
        read_timeout: float = 10.0,
        write_timeout: float = 100.0,  # a transaction can take up to ~90s to confirm
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._base = "" if client is None else base_url.rstrip("/")
        self._secret = secret
        self._sleep, self._retries, self._backoff = sleep, retries, backoff
        self._read_timeout, self._write_timeout = read_timeout, write_timeout

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- transport ---

    async def _call(self, method: str, path: str, body: dict | None = None, *, write: bool = False,
                    retry_transport_only: bool = False) -> dict:
        timeout = self._write_timeout if write else self._read_timeout
        last: ChainError | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                resp = await self._client.request(
                    method, self._base + path, json=body, headers={"X-Chain-Secret": self._secret}, timeout=timeout
                )
            except httpx.HTTPError as e:  # timeouts, connection errors
                last = ChainError("RPC_ERROR", f"chain service unreachable ({type(e).__name__})")
                if retry_transport_only and not isinstance(e, httpx.ConnectError):
                    break  # the request may have reached the service: a mint is not repeated
                continue
            try:
                data: Any = resp.json()
            except ValueError:
                data = None
            if resp.status_code == 401:
                raise ChainError("RPC_ERROR", "chain service rejected the shared secret (check CHAIN_SERVICE_SECRET)")
            if isinstance(data, dict) and (data.get("ok") is False or resp.status_code >= 400) and resp.status_code != 401:
                raw = str(data.get("code", "RPC_ERROR"))
                code = CODE_MAP.get(raw, raw)
                err = ChainError(code if code in ERROR_CODES else "RPC_ERROR", str(data.get("detail") or raw))
                if err.code != "RPC_ERROR":
                    raise err  # a business answer (including RATE_LIMITED) is final
                if retry_transport_only:
                    raise err  # a mint is not repeated after the service already answered
                last = err  # the chain was busy or failing: safe to repeat, every call is idempotent
                continue
            if resp.status_code in RETRY_STATUS and not retry_transport_only:
                last = ChainError("RPC_ERROR", f"chain service returned HTTP {resp.status_code}")
                continue
            if not isinstance(data, dict):
                raise ChainError("RPC_ERROR", f"chain service sent an unreadable response (HTTP {resp.status_code})")
            return data
        assert last is not None
        raise last

    # --- ChainClient ---

    async def create_bet(self, bet_id: str, params: BetParams) -> ChainBet:
        if params.funding_closes_at is None or params.refund_after is None:
            raise ChainError("INVALID_PARAMS", "funding_closes_at and refund_after are required on chain")
        d = await self._call("POST", "/bets", {
            "bet_id": bet_id, "funding_closes_at": _ts(params.funding_closes_at), "refund_after": _ts(params.refund_after),
            "fee_bps": params.fee_bps, "min_stake_base": params.min_stake_base, "max_stake_base": params.max_stake_base,
            "max_participants": params.max_participants,
        }, write=True)
        return _chain_bet(bet_id, d)

    async def get_bet(self, bet_id: str) -> ChainBet:
        return _chain_bet(bet_id, await self._call("GET", f"/bets/{bet_id}"))

    async def get_positions(self, bet_id: str) -> list[Position]:
        d = await self._call("GET", f"/bets/{bet_id}/positions")
        return [
            Position(bet_id, p["wallet"], int(p["side"]), int(p["amount_base"]), claimed=False, payout_base=0, closed=False)
            for p in d["positions"]
        ]

    async def lock(self, bet_id: str) -> ChainBet:
        return _chain_bet(bet_id, await self._call("POST", f"/bets/{bet_id}/lock", {}, write=True))

    async def settle(self, bet_id: str, winning_side: int) -> ChainBet:
        if isinstance(winning_side, bool) or winning_side not in (0, 1):
            raise ChainError("INVALID_SIDE", "winning side must be 0 or 1")
        return _chain_bet(bet_id, await self._call("POST", f"/bets/{bet_id}/settle", {"winning_side": winning_side}, write=True))

    async def refund_all(self, bet_id: str) -> ChainBet:
        return _chain_bet(bet_id, await self._call("POST", f"/bets/{bet_id}/refund_all", {}, write=True))

    async def claim(self, bet_id: str, wallet: str) -> Claim:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        d = await self._call("POST", f"/bets/{bet_id}/claim", {"wallet": wallet}, write=True)
        if d.get("already"):
            raise ChainError("ALREADY_CLAIMED", "no open position for this wallet")
        return Claim(bet_id, wallet, int(d["payout_base"]), str(d["kind"]), d.get("signature"))

    async def close_loser_position(self, bet_id: str, wallet: str) -> None:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        await self._call("POST", f"/bets/{bet_id}/close_loser_position", {"wallet": wallet}, write=True)

    async def close_bet(self, bet_id: str) -> None:
        await self._call("POST", f"/bets/{bet_id}/close_bet", {}, write=True)

    # --- sponsored deposits and the airdrop (chunk 3B). Only the real chain has them. ---

    async def wallet(self, wallet: str) -> dict:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        d = await self._call("GET", f"/wallet/{wallet}")
        return {"has_token_account": bool(d["has_token_account"]), "usdc_base": int(d["usdc_base"]), "sol_lamports": int(d["sol_lamports"])}

    async def airdrop(self, wallet: str, amount_base: int) -> dict:
        """Mints test USDC. A mint is not idempotent, so only a refused connection (nothing was sent) is retried."""
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        return await self._call("POST", "/airdrop", {"wallet": wallet, "amount_base": amount_base}, write=True, retry_transport_only=True)

    async def deposit_build(self, bet_id: str, wallet: str, side: int, amount_base: int) -> dict:
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        return await self._call("POST", "/deposit/build", {"bet_id": bet_id, "wallet": wallet, "side": side, "amount_base": amount_base}, write=True)

    async def deposit_submit(self, bet_id: str, wallet: str, side: int, amount_base: int, *, signed_tx_b64: str | None = None,
                             message_b64: str | None = None, user_signature_b64: str | None = None) -> dict:
        """Safe to repeat: the service answers the same signature for the same signed bytes."""
        if not is_valid_wallet(wallet):
            raise ChainError("INVALID_WALLET", "wallet is not a valid public key")
        body: dict = {"bet_id": bet_id, "wallet": wallet, "side": side, "amount_base": amount_base}
        if signed_tx_b64 is not None:
            body["signed_tx_b64"] = signed_tx_b64
        else:
            body["message_b64"], body["user_signature_b64"] = message_b64, user_signature_b64
        return await self._call("POST", "/deposit/submit", body, write=True)

    async def health(self) -> dict | None:
        d = await self._call("GET", "/health")
        return {"payer_sol": d.get("payer_sol"), "oracle_sol": d.get("oracle_sol"), "program_id": d.get("program_id")}
