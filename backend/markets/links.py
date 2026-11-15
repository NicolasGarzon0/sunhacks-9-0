"""Signed bet link: DEPOSIT_PAGE_URL + /b/ + token. The token carries only the bet id and is multi use.

Valid while the signature checks out, the bet exists, and now is before link_expires_at:
  payouts_completed_at + LINK_GRACE_HOURS when payouts are complete, otherwise settle_after + 7 days (hard ceiling).
Funding closing never invalidates a link. Tokens are never logged, only bet ids.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from itsdangerous import BadSignature, URLSafeSerializer

from app.repository import Bet

SALT = "bet-link"
HARD_CEILING = timedelta(days=7)


class LinkSigner:
    def __init__(self, secret: str, deposit_page_url: str, grace) -> None:
        if len(secret) < 32:
            raise ValueError("LINK_SIGNING_SECRET must be at least 32 characters")
        self._ser = URLSafeSerializer(secret, salt=SALT)
        self.base = deposit_page_url.rstrip("/")
        self.grace = grace if isinstance(grace, timedelta) else timedelta(hours=grace)

    def token(self, bet_id: str) -> str:
        return self._ser.dumps(bet_id)

    def url(self, bet_id: str) -> str:
        return f"{self.base}/b/{self.token(bet_id)}"

    def bet_id(self, token: str) -> str | None:
        """The bet id inside a validly signed token, else None."""
        try:
            value = self._ser.loads(token)
        except BadSignature:
            return None
        return value if isinstance(value, str) else None

    def expires_at(self, bet: Bet) -> datetime | None:
        if bet.payouts_completed_at is not None:
            return bet.payouts_completed_at + self.grace
        if bet.settle_after is not None:
            return bet.settle_after + HARD_CEILING
        return None

    def is_expired(self, bet: Bet, now: datetime) -> bool:
        exp = self.expires_at(bet)
        return exp is not None and now >= exp
