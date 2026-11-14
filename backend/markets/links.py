from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from itsdangerous import BadSignature, URLSafeSerializer


class LinkError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt="bet-link")


def make_token(secret: str, bet_id: str) -> str:
    return _serializer(secret).dumps(bet_id)


def parse_token(secret: str, token: str) -> str:
    try:
        bet_id = _serializer(secret).loads(token)
    except BadSignature as exc:
        raise LinkError("BAD_TOKEN") from exc
    if not isinstance(bet_id, str) or not bet_id:
        raise LinkError("BAD_TOKEN")
    return bet_id


def build_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/b/{token}"


def origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("DEPOSIT_PAGE_URL must be an http(s) URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def link_expired(
    now: datetime,
    payouts_completed_at: datetime | None,
    settle_after: datetime,
    grace_hours: int,
) -> bool:
    now = now.astimezone(timezone.utc)
    if payouts_completed_at is not None:
        return now >= payouts_completed_at.astimezone(timezone.utc) + timedelta(hours=grace_hours)
    return now >= settle_after.astimezone(timezone.utc) + timedelta(days=7)
