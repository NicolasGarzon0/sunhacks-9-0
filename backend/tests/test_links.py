"""Signed bet links: signature, unknown bet, and the lifetime rules (no database needed for the pure part)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.repository import Bet
from markets.links import LinkSigner

SECRET = "test-signing-secret-0123456789abcdef"
KICKOFF = datetime(2026, 11, 15, 21, 5, tzinfo=timezone.utc)
SETTLE = KICKOFF + timedelta(hours=6)


def bet(**kw) -> Bet:
    base = dict(
        id="4cd57c06-fc4a-406d-9bdb-ae48507af330", proposer_handle="+15550001111", game_id="g", league="nfl",
        home_id="h", home_name="H", away_id="a", away_name="A", kickoff_utc=KICKOFF, status="open",
        created_at=KICKOFF, funding_closes_at=KICKOFF - timedelta(hours=1), settle_after=SETTLE,
    )
    return Bet(**{**base, **kw})


@pytest.fixture
def signer() -> LinkSigner:
    return LinkSigner(SECRET, "https://deposit.test/", 2)


def test_valid_token_round_trip_and_url(signer):
    b = bet()
    t = signer.token(b.id)
    assert signer.bet_id(t) == b.id
    assert signer.url(b.id) == f"https://deposit.test/b/{t}"
    assert signer.token(b.id) == t  # multi use: the same token every time


def test_tampered_and_foreign_tokens_are_rejected(signer):
    t = signer.token("4cd57c06-fc4a-406d-9bdb-ae48507af330")
    flipped = t[:-1] + ("A" if t[-1] != "A" else "B")
    assert signer.bet_id(flipped) is None
    assert signer.bet_id("garbage") is None and signer.bet_id("") is None
    other = LinkSigner("another-signing-secret-0123456789abcd", "https://x.test", 2)
    assert signer.bet_id(other.token("4cd57c06-fc4a-406d-9bdb-ae48507af330")) is None


def test_short_secret_is_refused():
    with pytest.raises(ValueError):
        LinkSigner("short", "https://x.test", 2)


def test_link_is_not_invalid_because_funding_closed_or_status(signer):
    after_close = bet().funding_closes_at + timedelta(minutes=5)
    for status in ("open", "locked", "settled", "refunded", "failed"):
        assert not signer.is_expired(bet(status=status), after_close)


def test_grace_window_after_payouts(signer):
    done = SETTLE + timedelta(hours=1)
    b = bet(status="settled", payouts_completed_at=done)
    assert signer.expires_at(b) == done + timedelta(hours=2)
    assert not signer.is_expired(b, done + timedelta(hours=1, minutes=59))
    assert signer.is_expired(b, done + timedelta(hours=2))
    assert signer.is_expired(b, done + timedelta(hours=2, minutes=1))


def test_hard_ceiling_when_payouts_never_complete(signer):
    b = bet(status="locked")
    assert signer.expires_at(b) == SETTLE + timedelta(days=7)
    assert not signer.is_expired(b, SETTLE + timedelta(days=7) - timedelta(seconds=1))
    assert signer.is_expired(b, SETTLE + timedelta(days=7))
