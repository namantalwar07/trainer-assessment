"""Small time helpers shared by schema validation, the mock engine and the timeline engine."""

from __future__ import annotations

import re

_TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")


def parse_timestamp(ts: str) -> int | None:
    """'HH:MM:SS' -> seconds, or None if malformed."""
    m = _TS.match(ts or "")
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s if mi < 60 and s < 60 else None


def fmt_timestamp(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_clock(text: str) -> int | None:
    """Wall-clock 'H:MM' or 'HH:MM[:SS]' (24-hour) -> seconds after midnight, or None."""
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", (text or "").strip())
    if not m:
        return None
    h, mi, sec = int(m[1]), int(m[2]), int(m[3] or 0)
    return h * 3600 + mi * 60 + sec if h < 24 and mi < 60 and sec < 60 else None
