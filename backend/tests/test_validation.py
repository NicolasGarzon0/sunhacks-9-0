from __future__ import annotations

from datetime import date

from app import templates as T
from app.validation import evaluate, looks_like_contact

TODAY = date(2026, 11, 14)


def ev(**fields):
    return evaluate({"team": "ASU", "opponent": "Texas Tech", "date": None, **fields}, TODAY)


def test_ready_without_date():
    o = ev()
    assert o.ready and o.reply == ""  # the flow replaces this with the lookup result


def test_ready_with_date_and_unknown_opponent():
    o = ev(opponent=None, date="2026-11-15")
    assert o.ready and o.fields == {"team": "ASU", "opponent": None, "date": "2026-11-15"}


def test_team_required():
    o = ev(team=None)
    assert not o.ready and o.pending_field == "team" and o.reply == T.ASK_GAME


def test_date_window_edges():
    assert ev(date="2026-11-14").ready and ev(date="2026-11-28").ready
    for bad in ("2026-11-13", "2026-11-29", "garbage"):
        o = ev(date=bad)
        assert o.reason == "date_out_of_range" and o.pending_field == "date" and o.fields["date"] is None
        assert o.reply == "I can only look up games from today through Saturday Nov 28. What day is the game?"


def test_contacts_never_accepted():
    assert looks_like_contact("480-555-0199") and looks_like_contact("+14805550123")
    assert looks_like_contact("bob@example.com")
    assert not looks_like_contact("ASU") and not looks_like_contact("49ers") and not looks_like_contact("76ers")
    o = ev(team="+14805550123")
    assert o.reason == "team_contact" and o.fields["team"] is None
    o = ev(opponent="480 555 0199")
    assert o.ready and o.fields["opponent"] is None


def test_stake_side_friend_are_dropped():
    o = evaluate({"team": "ASU", "stake_usd": 5, "side": "ASU", "friend": "Sam"}, TODAY)
    assert set(o.fields) == {"team", "opponent", "date"}


def test_long_values_are_clipped():
    assert T.clip("A" * 100) == "A" * 39 + "…"
    assert "A" * 41 not in T.no_games("A" * 100, None)
