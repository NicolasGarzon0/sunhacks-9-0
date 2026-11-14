"""Dev tool: run the real Haiku extraction + deterministic validation. Never sends an iMessage.

Single message:
    .venv/bin/python scripts/try_extract.py "create a bet for asu vs texas tech tomorrow"
    .venv/bin/python scripts/try_extract.py "ASU vs Texas Tech" --pending team

Offline eval (evals/extraction_cases.json), printing expected vs actual:
    .venv/bin/python scripts/try_extract.py --eval

Reads ANTHROPIC_API_KEY, HAIKU_MODEL and LOCAL_TIMEZONE from the repo-root .env.
Uses real API calls (Haiku: fractions of a cent each). Needs no database and sends nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ConfigError, load_settings  # noqa: E402
from app.logging import Logger  # noqa: E402
from app.validation import evaluate  # noqa: E402
from extraction import HaikuExtractor  # noqa: E402

CASES = ROOT / "evals" / "extraction_cases.json"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
FIELDS = ("team", "opponent", "date")


def first_after(today: date, weekday: str) -> date:
    delta = (WEEKDAYS.index(weekday) - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def expected_dates(token, today: date) -> list[str] | None:
    """int = day offset; upcoming_<day> = first one after today; next_<day> = first or second (people read
    'next' both ways); last_<day> = the most recent one before today."""
    if token is None:
        return None
    if isinstance(token, int):
        return [(today + timedelta(days=token)).isoformat()]
    kind, day = token.split("_", 1)
    if kind == "last":
        back = (today.weekday() - WEEKDAYS.index(day)) % 7 or 7
        return [(today - timedelta(days=back)).isoformat()]
    first = first_after(today, day)
    return [first.isoformat()] + ([(first + timedelta(days=7)).isoformat()] if kind == "next" else [])


def matches(field: str, expected, actual, today: date) -> bool:
    if expected == "?":
        return True
    if field == "date":
        exp = expected_dates(expected, today)
        return actual is None if exp is None else actual in exp
    if expected is None or actual is None:
        return expected is None and actual is None
    return " ".join(str(expected).lower().split()) == " ".join(str(actual).lower().split())


def short(v, n=14) -> str:
    s = "-" if v is None else str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def local_today(tz: str) -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).date()


async def run_eval(ex: HaikuExtractor, settings) -> int:
    today = local_today(settings.local_timezone)
    cases = json.loads(CASES.read_text())["cases"]
    print(f"model={settings.haiku_model}  today={today} ({today.strftime('%A')}) {settings.local_timezone}  cases={len(cases)}\n")
    header = (
        f"{'#':>2}  {'message':<42} {'pend':<5} {'class (actual)':<17} {'team':<12} {'opponent':<12} "
        f"{'date':<11} {'validation exp→act':<34} result"
    )
    print(header + "\n" + "-" * len(header))
    passed, details = 0, []
    for i, case in enumerate(cases, 1):
        result = await ex.extract(case["text"], today, settings.local_timezone, case["pending"])
        exp = case["expect"]
        if result is None:
            print(f"{i:>2}  {short(case['text'], 42):<42} EXTRACTION FAILED")
            details.append((i, "extraction returned None"))
            continue
        f = result.fields()
        bad = [k for k in FIELDS if not matches(k, exp[k], f[k], today)]
        cls_ok = exp["classification"] == result.classification
        if not cls_ok:
            bad.insert(0, "classification")
        v_exp = case["validation"]
        v_act = evaluate(f, today).reason if v_exp is not None else None
        v_ok = v_exp in (None, "?") or v_exp == v_act
        ok = cls_ok and not [b for b in bad if b != "classification"] and v_ok
        passed += ok
        val = "-" if v_exp is None else f"{v_exp}→{v_act}"
        print(
            f"{i:>2}  {short(case['text'], 42):<42} {short(case['pending'], 5):<5} {result.classification:<17} "
            f"{short(f['team'], 12):<12} {short(f['opponent'], 12):<12} {short(f['date'], 11):<11} {val:<34} "
            f"{'PASS' if ok else 'FAIL'}"
        )
        if bad:
            def ev(k):
                return exp["classification"] if k == "classification" else exp[k]
            got = {**f, "classification": result.classification}
            details.append((i, ", ".join(f"{k}: expected {ev(k)!r} got {got[k]!r}" for k in bad)))
    print(f"\n{passed}/{len(cases)} passed")
    for i, d in details:
        print(f"  #{i}: {d}")
    return 0 if passed == len(cases) else 1


async def run_single(ex: HaikuExtractor, settings, text: str, pending: str | None) -> int:
    today = local_today(settings.local_timezone)
    result = await ex.extract(text, today, settings.local_timezone, pending)
    print(f"today={today} ({today.strftime('%A')}) {settings.local_timezone}  model={settings.haiku_model}  pending={pending}")
    if result is None:
        print("extraction: FAILED (the user would get the fallback text)")
        return 1
    print(f"classification: {result.classification}")
    for k, v in result.fields().items():
        print(f"  {k:<9} {v}")
    if result.classification != "other":
        o = evaluate(result.fields(), today)
        print(f"validation: {'READY' if o.ready else 'ASK ' + str(o.pending_field)} ({o.reason})")
        print(f"reply would be: {o.reply}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="message text (single mode)")
    ap.add_argument("--pending", choices=["team", "date"], help="simulate a pending draft field")
    ap.add_argument("--eval", action="store_true", help="run evals/extraction_cases.json")
    args = ap.parse_args()
    if not args.eval and args.text is None:
        ap.error("give TEXT, or --eval")
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    ex = HaikuExtractor(settings.anthropic_api_key, settings.haiku_model, Logger("error"))

    async def go() -> int:
        try:
            if args.eval:
                return await run_eval(ex, settings)
            return await run_single(ex, settings, args.text, args.pending)
        finally:
            await ex.aclose()

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
