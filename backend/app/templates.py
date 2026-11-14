"""Every user-facing reply for bet requests. The LLM never writes text the user sees."""

from __future__ import annotations

from datetime import date

EXAMPLE = "create a bet for ASU vs Texas Tech"

FALLBACK = f"I could not read that, try: {EXAMPLE}"
HELP = f"To start a bet, text something like: {EXAMPLE}"
TOO_LONG = f"That message is too long for me. Keep it short, like: {EXAMPLE}"

ASK_GAME = "Which game is the bet on? For example: ASU vs Texas Tech."
ASK_DATE = "What day is the game?"

REPLACE_ASK = "You already have a bet request waiting for its game. Replace it with this new one? Reply YES or NO."
REPLACE_UNCLEAR = "Reply YES to replace your bet request in progress with the new one, or NO to keep it."
REPLACE_KEPT_PREFIX = "OK, keeping your bet request in progress. "

MAX_INTERPOLATED = 40  # cap model-extracted strings placed into templates


def clip(value: str) -> str:
    value = " ".join(value.split())
    return value if len(value) <= MAX_INTERPOLATED else value[: MAX_INTERPOLATED - 1] + "…"


def day_label(d: date) -> str:
    return f"{d.strftime('%A %b')} {d.day}"


def question(field: str) -> str:
    return {"team": ASK_GAME, "date": ASK_DATE}[field]


def date_out_of_range(last: date) -> str:
    return f"I can only look up games from today through {day_label(last)}. What day is the game?"


def readback(team: str, opponent: str | None, d: date | None) -> str:
    opp = clip(opponent) if opponent else "the other team"
    when = f", on {day_label(d)}" if d else ""
    return f"Got it: a bet on {clip(team)} vs {opp}{when}. Next I will look up the game."
