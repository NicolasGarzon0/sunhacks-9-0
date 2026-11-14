"""Every user-facing reply for bet requests. The LLM never writes text the user sees."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from games.models import Game

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


def when(kickoff_utc: datetime, tz: str) -> str:
    local = kickoff_utc.astimezone(ZoneInfo(tz))
    hour = local.strftime("%I").lstrip("0")
    return f"{day_label(local.date())}, {hour}:{local.strftime('%M %p')} {local.strftime('%Z')}"


def game_line(game: Game, tz: str) -> str:
    return f"{game.away.name} at {game.home.name}, {when(game.kickoff_utc, tz)}"


def offer(games: list[Game], tz: str) -> str:
    if len(games) == 1:
        return f"I found {game_line(games[0], tz)}. Is this the game? Reply YES or NO."
    lines = "\n".join(f"{i}. {game_line(g, tz)}" for i, g in enumerate(games, 1))
    return f"I found these games:\n{lines}\nReply with a number, or NO."


def offer_help(n: int) -> str:
    if n == 1:
        return "Please reply YES if that is the game, or NO if it is not."
    return f"Please reply with a number from 1 to {n} to pick a game, or NO if none of them is right."


def no_games(team: str, d: date | None) -> str:
    window = f"on {day_label(d)}" if d else "in the next 14 days"
    return f"I could not find a game for {clip(team)} {window}. Which team or day should I look for?"


LOOKUP_APOLOGY = "Sorry, I cannot look up games right now. Please try again in a few minutes."
CONFIRM_NO = "OK, which game did you mean? For example: ASU vs Texas Tech."
GAME_STARTED = "That game has already started, so it cannot be bet on. Which game did you mean?"
MARKET_NOT_OPEN = "That game is no longer available for betting. Which game did you mean?"
MARKET_FAILED = "I could not open that market. Please try again."


def confirmed(game: Game, tz: str) -> str:
    return f"Game confirmed: {game_line(game, tz)}. Next I will create your deposit link."
