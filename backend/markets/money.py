"""USDC amounts. Everything inside the backend is an int in base units (6 decimals); dollars exist only at the
API / config boundary, parsed with Decimal. Floats are rejected, never rounded."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

DECIMALS = 6
UNIT = 10**DECIMALS


class MoneyError(ValueError):
    pass


def parse_usd(value: object) -> int:
    """Dollars -> base units. Accepts str, int or Decimal (never float). At most 6 decimal places, > 0 not required."""
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError("amount must be a decimal string or integer, not a float")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise MoneyError("amount is not a number") from None
    if not d.is_finite() or d < 0:
        raise MoneyError("amount must be a non negative number")
    base = d * UNIT
    if base != base.to_integral_value():
        raise MoneyError(f"amount has more than {DECIMALS} decimal places")
    return int(base)


def format_usd(base: int) -> str:
    """Base units -> decimal string with at least 2 decimals, e.g. 12_500_000 -> "12.50", 1_234_567 -> "1.234567"."""
    sign = "-" if base < 0 else ""
    whole, frac = divmod(abs(int(base)), UNIT)
    digits = f"{frac:0{DECIMALS}d}".rstrip("0").ljust(2, "0")
    return f"{sign}{whole}.{digits}"


def usd_label(base: int) -> str:
    """For texts: whole dollars without cents ("$20"), otherwise "$12.50"."""
    s = format_usd(base)
    return f"${s[:-3]}" if s.endswith(".00") else f"${s}"


def bps_label(bps: int) -> str:
    """500 -> "5%", 250 -> "2.5%", 125 -> "1.25%"."""
    whole, frac = divmod(int(bps), 100)
    return f"{whole}%" if frac == 0 else f"{whole}.{f'{frac:02d}'.rstrip('0')}%"
