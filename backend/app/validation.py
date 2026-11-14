"""Deterministic validation of a bet request draft. Pure functions: no I/O, no LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import templates as T

MAX_DAYS_AHEAD = 14
EMAIL_RE = re.compile(r"\S+@\S+\.\S+")


@dataclass
class Outcome:
    ready: bool
    fields: dict  # normalized; invalid values cleared
    reply: str
    pending_field: str | None = None
    reason: str = "ok"  # short code for logs (no values)


def looks_like_contact(value: str) -> bool:
    """Phone numbers and emails are never accepted as a team (nothing is parsed from contacts)."""
    return len(re.sub(r"\D", "", value)) >= 7 or bool(EMAIL_RE.search(value))


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def evaluate(fields: dict, today: date) -> Outcome:
    """Team is required; date is optional but must be in [today, today+14] when given."""
    f = {k: fields.get(k) for k in ("team", "opponent", "date")}

    team = f["team"]
    if not team or looks_like_contact(team):
        f["team"] = None
        if f["opponent"] and looks_like_contact(f["opponent"]):
            f["opponent"] = None
        return Outcome(False, f, T.ASK_GAME, pending_field="team", reason="missing_team" if not team else "team_contact")
    if f["opponent"] and looks_like_contact(f["opponent"]):
        f["opponent"] = None

    if f["date"] is not None:
        d = parse_date(f["date"])
        last = today + timedelta(days=MAX_DAYS_AHEAD)
        if d is None or d < today or d > last:
            f["date"] = None
            return Outcome(False, f, T.date_out_of_range(last), pending_field="date", reason="date_out_of_range")
        f["date"] = d.isoformat()

    reply = T.readback(f["team"], f["opponent"], parse_date(f["date"]))
    return Outcome(True, f, reply)
