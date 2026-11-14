"""One JSON object per line, same shape and masking rules as the gateway's src/logger.ts.

- Fields named handle/to/sender/recipient are masked to the last 4 chars ("***4567").
- Phone-like runs (10+ digits) and emails inside any other string field are masked too.
- Message text goes only in a field called `text`, and only at debug level. Log `textLength` otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable

ORDER = {"debug": 10, "info": 20, "warn": 30, "error": 40}
HANDLE_KEYS = frozenset({"handle", "to", "sender", "recipient"})

# Same patterns as src/logger.ts.
PHONE_RE = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{5,}\d(?![\w-])")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def mask_handle(handle: str) -> str:
    if len(handle) <= 4:
        return "***"
    return f"***{handle[-4:]}"


def _mask_phone(m: re.Match[str]) -> str:
    s = m.group(0)
    if len(re.sub(r"\D", "", s)) >= 10:
        return mask_handle(re.sub(r"[\s().-]", "", s))
    return s


def mask_text(text: str) -> str:
    text = PHONE_RE.sub(_mask_phone, text)
    return EMAIL_RE.sub(lambda m: mask_handle(m.group(0)), text)


def _sanitize(key: str, value: Any) -> Any:
    if isinstance(value, str):
        return mask_handle(value) if key in HANDLE_KEYS else mask_text(value)
    return value


class Logger:
    def __init__(self, level: str = "info", write: Callable[[str], None] | None = None) -> None:
        self.min = ORDER.get(level, ORDER["info"])
        self._write = write or (lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()))

    def _emit(self, level: str, event: str, fields: dict[str, Any]) -> None:
        if ORDER[level] < self.min:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": level,
            "event": event,
        }
        for k, v in fields.items():
            entry[k] = _sanitize(k, v)
        try:
            self._write(json.dumps(entry, default=str))
        except Exception:  # logging must never crash the caller
            pass

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._emit("warn", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)


def err_detail(err: BaseException) -> str:
    """Exception summary safe to log (masked by the logger's free-text masking)."""
    msg = str(err)
    return f"{type(err).__name__}: {msg}" if msg else type(err).__name__
