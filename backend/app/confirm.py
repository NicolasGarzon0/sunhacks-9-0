"""Deterministic parsing of a game confirmation reply. No model involved."""

from __future__ import annotations

import re
from dataclasses import dataclass

YES = {"yes", "y", "yep", "yeah", "ok", "okay", "yup", "ya", "sure"}
NO = {"no", "n", "nope"}
_CLEAN = re.compile(r"[\s.!]+$")


@dataclass(frozen=True)
class Answer:
    kind: str  # "yes" | "no" | "pick"
    index: int = 0  # 0-based candidate index for yes/pick


def parse_confirmation(text: str, n_candidates: int) -> Answer | None:
    """YES only counts when exactly one game was offered; a number must be 1..n. Anything else -> None."""
    t = _CLEAN.sub("", text.strip().lower())
    if t.startswith("#"):
        t = t[1:].strip()
    if t in NO:
        return Answer("no")
    if t in YES:
        return Answer("yes", 0) if n_candidates == 1 else None
    if t.isdigit():
        k = int(t)
        if 1 <= k <= n_candidates:
            return Answer("pick", k - 1)
    return None
