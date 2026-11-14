"""Every user-facing reply for bet requests. The LLM never writes text the user sees."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from games.models import Game
from markets.money import bps_label, usd_label

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


GAME_UNAVAILABLE = "That game is no longer on the schedule, so it cannot be bet on. Which game did you mean?"


def funding_closed(minutes: int) -> str:
    return (
        f"That game starts too soon to open a bet, because betting closes {minutes} minutes before kickoff. "
        "Which game did you mean?"
    )


MARKET_FAILED = "Sorry, I could not open that bet. Nothing was charged. Please start again in a few minutes."


def _hours(n: int) -> str:
    return "an hour" if n == 1 else f"{n} hours"


def fee_line(fee_bps: int) -> str:
    if fee_bps == 0:
        return "There is no fee."
    return f"A {bps_label(fee_bps)} fee on the losing side covers operating costs."


def market_open(bet, tz: str, grace_hours: int, url: str) -> str:
    """The one text the proposer gets when the market opens. `bet` is a repository.Bet with market fields set."""
    return (
        f"Your bet is open: {bet.away_name} at {bet.home_name}, {when(bet.kickoff_utc, tz)}.\n"
        f"Betting closes {when(bet.funding_closes_at, tz)}.\n"
        "Forward this link to anyone who wants in. Each person picks a side and their own amount "
        f"from {usd_label(bet.min_stake_base)} to {usd_label(bet.max_stake_base)}.\n"
        f"{fee_line(bet.fee_bps)}\n"
        f"The link stays live until {_hours(grace_hours)} after payouts and shows live odds and results.\n"
        f"{url}"
    )


def market_exists(bet, tz: str, grace_hours: int, url: str) -> str:
    """Proposer confirms a game that already has an active market: resend what they need."""
    if bet.status == "open":
        return "You already have a bet open on that game.\n" + market_open(bet, tz, grace_hours, url)
    return f"You already have a bet on that game and betting is closed. Here is the link:\n{url}"


def market_locked(bet, side_totals: tuple[int, int]) -> str:
    return (
        f"Betting is closed for {bet.away_name} at {bet.home_name}. "
        f"The pool is {usd_label(sum(side_totals))}: {usd_label(side_totals[0])} on {bet.away_name} "
        f"and {usd_label(side_totals[1])} on {bet.home_name}. The bet is locked until the game ends."
    )


def market_refunded(bet) -> str:
    return (
        f"Betting is closed for {bet.away_name} at {bet.home_name}. "
        "The bet did not have people on both sides, so everyone is refunded. Nothing was charged."
    )

