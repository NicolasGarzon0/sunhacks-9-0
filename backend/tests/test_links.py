from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from markets.links import LinkError, build_link, link_expired, make_token, origin, parse_token


SECRET = "x" * 32
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def test_signed_link_contains_only_the_bet_id() -> None:
    token = make_token(SECRET, "bet-123")
    assert parse_token(SECRET, token) == "bet-123"
    assert build_link("https://deposit.example/", token).startswith("https://deposit.example/b/")
    with pytest.raises(LinkError):
        parse_token(SECRET, token + "bad")


def test_link_expiry_grace_rules() -> None:
    settle_after = NOW + timedelta(hours=1)
    assert not link_expired(NOW, None, settle_after, 2)
    assert link_expired(NOW + timedelta(days=8), None, settle_after, 2)
    completed = NOW
    assert not link_expired(NOW + timedelta(hours=1), completed, settle_after, 2)
    assert link_expired(NOW + timedelta(hours=2), completed, settle_after, 2)
    assert origin("https://deposit.example/path") == "https://deposit.example"
