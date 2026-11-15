"""An in memory stand-in for chain-service's HTTP API, with the contract's rules (programs/handshake_pool).

RealChainClient talks to it over httpx.MockTransport. `deposit` plays the user's signed deposit transaction (the real
service has no deposit endpoint: users deposit from the deposit page or the dev script), using the contract's error order.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

from markets.chain import ChainError
from markets.pool_math import compute_fee, compute_payout

SECRET = "chain-secret"


class Fail(Exception):
    def __init__(self, code: str, detail: str = "", status: int = 409) -> None:
        super().__init__(code)
        self.code, self.detail, self.status = code, detail or code, status


class FakeChainService:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.bets: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.payer_sol = 2.0
        self.oracle_sol = 0.5
        self.broken: dict[str, int] = {}  # "METHOD path-suffix" -> remaining failures with HTTP 503

    # ---- the user's deposit transaction (not an HTTP endpoint) ----
    def deposit(self, bet_id: str, wallet: str, side: int, amount: int) -> None:
        b = self._bet(bet_id)
        p = b["params"]
        if b["state"] != "open":
            raise ChainError("MARKET_NOT_OPEN", "market is " + b["state"])
        if self.clock() >= p["funding_closes_at"]:
            raise ChainError("FUNDING_CLOSED", "funding has closed")
        if side not in (0, 1):
            raise ChainError("INVALID_SIDE", "side must be 0 or 1")
        if amount < p["min_stake_base"]:
            raise ChainError("BELOW_MIN", "below minimum")
        mine = b["positions"].get(wallet)
        if mine is None and len(b["positions"]) >= p["max_participants"]:
            raise ChainError("MARKET_FULL", "full")
        if mine is not None and mine["side"] != side:
            raise ChainError("ALREADY_OTHER_SIDE", "other side")
        if (mine["amount"] if mine else 0) + amount > p["max_stake_base"]:
            raise ChainError("ABOVE_MAX", "above max")
        if mine is None:
            b["positions"][wallet] = {"side": side, "amount": 0, "open": True}
        b["positions"][wallet]["amount"] += amount
        b["vault"] += amount

    # ---- http ----
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/health":
            return httpx.Response(200, json={"ok": True, "cluster": "devnet", "program_id": "P", "payer_sol": self.payer_sol, "oracle_sol": self.oracle_sol})
        if request.headers.get("x-chain-secret") != SECRET:
            return httpx.Response(401, json={"ok": False, "code": "UNAUTHORIZED", "detail": "bad secret"})
        key = f"{request.method} {path.rsplit('/', 1)[-1]}"
        if self.broken.get(key, 0) > 0:
            self.broken[key] -= 1
            return httpx.Response(503, text="busy")
        body = json.loads(request.content) if request.content else {}
        try:
            data = self._route(request.method, path, body)
        except Fail as e:
            return httpx.Response(e.status, json={"ok": False, "code": e.code, "detail": e.detail})
        return httpx.Response(200, json={"ok": True, **data})

    def _bet(self, bet_id: str) -> dict:
        b = self.bets.get(bet_id)
        if b is None or b["closed"]:
            raise Fail("NOT_FOUND", "unknown bet", 404)
        return b

    def _view(self, b: dict, extra: dict | None = None) -> dict:
        totals = [0, 0]
        for p in b["positions"].values():
            totals[p["side"]] += p["amount"]
        return {
            "state": b["state"], "side_totals": totals, "participant_count": len(b["positions"]),
            "positions_open": sum(1 for p in b["positions"].values() if p["open"]), "fee_bps": b["params"]["fee_bps"],
            "fee_paid_base": b["fee_paid"], "distributable_base": b["distributable"], "winning_side": b["winning"],
            "funding_closes_at": int(b["params"]["funding_closes_at"].timestamp()), "refund_after": int(b["params"]["refund_after"].timestamp()),
            "vault_base": b["vault"], "treasury_balance_base": b["treasury"], **(extra or {}),
        }

    def _route(self, method: str, path: str, body: dict) -> dict:
        if method == "POST" and path == "/bets":
            return self._create(body)
        m = re.fullmatch(r"/bets/([0-9a-f-]{36})(?:/(\w+))?", path)
        if not m:
            raise Fail("BAD_REQUEST", "not found", 404)
        bet_id, action = m.group(1), m.group(2)
        if method == "GET" and action is None:
            return self._view(self._bet(bet_id))
        if method == "GET" and action == "positions":
            b = self._bet(bet_id)
            return {"positions": [{"wallet": w, "side": p["side"], "amount_base": p["amount"]} for w, p in sorted(b["positions"].items()) if p["open"]]}
        fn = getattr(self, f"_{action}", None)
        if method != "POST" or fn is None:
            raise Fail("BAD_REQUEST", "not found", 404)
        return fn(bet_id, body)

    def _create(self, body: dict) -> dict:
        bet_id = body["bet_id"]
        if bet_id in self.bets and not self.bets[bet_id]["closed"]:
            return {"already": True, **self._view(self.bets[bet_id])}
        now = self.clock().timestamp()
        if (body["fee_bps"] > 1000 or body["min_stake_base"] <= 0 or body["min_stake_base"] > body["max_stake_base"]
                or not 2 <= body["max_participants"] <= 100 or body["funding_closes_at"] <= now
                or body["refund_after"] <= body["funding_closes_at"]):
            raise Fail("INVALID_PARAMS", "InvalidParams", 400)
        params = {**body, "funding_closes_at": datetime.fromtimestamp(body["funding_closes_at"], self.clock().tzinfo),
                  "refund_after": datetime.fromtimestamp(body["refund_after"], self.clock().tzinfo)}
        self.bets[bet_id] = {"params": params, "state": "open", "positions": {}, "vault": 0, "fee_paid": 0, "distributable": 0,
                             "winning": None, "treasury": 0, "closed": False}
        return {"already": False, **self._view(self.bets[bet_id])}

    def _past_deadline(self, b: dict) -> bool:
        return self.clock() >= b["params"]["refund_after"]

    def _lock(self, bet_id: str, body: dict) -> dict:
        b = self._bet(bet_id)
        if b["state"] in ("locked", "settled"):
            return {"already": True, **self._view(b)}
        if b["state"] != "open":
            raise Fail("WRONG_STATE", "bet is " + b["state"])
        if self._past_deadline(b):
            raise Fail("PAST_REFUND_DEADLINE", "past")
        v = self._view(b)
        if v["side_totals"][0] == 0 or v["side_totals"][1] == 0 or v["participant_count"] < 2:
            raise Fail("BOTH_SIDES_REQUIRED", "both sides")
        b["state"] = "locked"
        return {"already": False, **self._view(b)}

    def _settle(self, bet_id: str, body: dict) -> dict:
        side = body["winning_side"]
        b = self._bet(bet_id)
        if b["state"] == "settled":
            if b["winning"] == side:
                return {"already": True, **self._view(b)}
            raise Fail("WRONG_STATE", "settled with the other side")
        if b["state"] != "locked":
            raise Fail("WRONG_STATE", "bet is " + b["state"])
        if self._past_deadline(b):
            raise Fail("PAST_REFUND_DEADLINE", "past")
        if side not in (0, 1):
            raise Fail("BAD_REQUEST", "winning_side", 400)
        totals = self._view(b)["side_totals"]
        fee = compute_fee(totals[1 - side], b["params"]["fee_bps"])
        b.update(state="settled", winning=side, fee_paid=fee, distributable=totals[0] + totals[1] - fee)
        b["vault"] -= fee
        b["treasury"] += fee
        return {"already": False, **self._view(b)}

    def _refund_all(self, bet_id: str, body: dict) -> dict:
        b = self._bet(bet_id)
        if b["state"] == "refunded":
            return {"already": True, **self._view(b)}
        if b["state"] == "settled":
            raise Fail("WRONG_STATE", "settled")
        b["state"] = "refunded"
        return {"already": False, **self._view(b)}

    def _claim(self, bet_id: str, body: dict) -> dict:
        b = self._bet(bet_id)
        if b["state"] not in ("settled", "refunded"):
            raise Fail("WRONG_STATE", "bet is " + b["state"])
        pos = b["positions"].get(body["wallet"])
        if pos is None or not pos["open"]:
            return {"already": True, "payout_base": None, "kind": None}
        if b["state"] == "refunded":
            payout, kind = pos["amount"], "refund"
        else:
            if pos["side"] != b["winning"]:
                raise Fail("NOT_A_WINNER", "loser")
            win_total = self._view(b)["side_totals"][b["winning"]]
            payout, kind = compute_payout(pos["amount"], b["distributable"], win_total), "winnings"
        pos["open"] = False
        b["vault"] -= payout
        return {"already": False, "payout_base": payout, "kind": kind, "signature": "sig"}

    def _close_loser_position(self, bet_id: str, body: dict) -> dict:
        b = self._bet(bet_id)
        pos = b["positions"].get(body["wallet"])
        if pos is None or not pos["open"]:
            return {"already": True}
        if b["state"] != "settled" or pos["side"] == b["winning"]:
            raise Fail("WRONG_STATE", "cannot close this position")
        pos["open"] = False
        return {"already": False, "signature": "sig"}

    def _close_bet(self, bet_id: str, body: dict) -> dict:
        b = self.bets.get(bet_id)
        if b is None or b["closed"]:
            return {"already": True}
        if b["state"] not in ("settled", "refunded"):
            raise Fail("WRONG_STATE", "bet is " + b["state"])
        if any(p["open"] for p in b["positions"].values()):
            raise Fail("WRONG_STATE", "positions are still open")
        b["treasury"] += b["vault"]
        b["vault"] = 0
        b["closed"] = True
        return {"already": False, "signature": "sig"}
