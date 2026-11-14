from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN

BASE_PER_USD = 100


def usd_to_base(value: Decimal | str | int) -> int:
    if isinstance(value, float):
        raise TypeError("money must be Decimal, string, or integer")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("USD amount must be finite and nonnegative")
    scaled = (amount * BASE_PER_USD).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if amount * BASE_PER_USD != scaled:
        raise ValueError("USD amount must have at most two decimal places")
    return int(scaled)


def base_to_usd(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("base amount must be a nonnegative integer")
    return f"{value // BASE_PER_USD}.{value % BASE_PER_USD:02d}"


def format_usd(value: int) -> str:
    return f"${base_to_usd(value)}"


def bps_to_percent(bps: int) -> str:
    if isinstance(bps, bool) or not isinstance(bps, int) or not 0 <= bps <= 10000:
        raise ValueError("basis points must be between 0 and 10000")
    whole, remainder = divmod(bps, 100)
    return f"{whole}.{remainder:02d}".rstrip("0").rstrip(".") + "%"
