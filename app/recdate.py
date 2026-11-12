"""Work out the real date of a recording: upload form, then the file name (Zoom or plain), then file metadata."""

from __future__ import annotations

import datetime as dt
import re

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_ZOOM = re.compile(r"GMT(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")  # Zoom names files in UTC
_PLAIN = re.compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_PLAIN_DMY = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[-_.](0[1-9]|1[0-2])[-_.](20\d{2})(?!\d)")


def date_from_name(name: str) -> dt.date | None:
    """Zoom 'GMT20260901-104357_Recording...' is UTC; the meeting day is the Indian date of that moment."""
    if m := _ZOOM.search(name):
        try:
            utc = dt.datetime(*(int(x) for x in m.groups()), tzinfo=dt.UTC)
            return utc.astimezone(IST).date()
        except ValueError:
            pass
    for rx, order in ((_PLAIN, (0, 1, 2)), (_PLAIN_DMY, (2, 1, 0))):
        if m := rx.search(name):
            g = m.groups()
            try:
                return dt.date(int(g[order[0]]), int(g[order[1]]), int(g[order[2]]))
            except ValueError:
                continue
    return None


def date_from_metadata(creation_time: str | None) -> dt.date | None:
    """ffprobe creation_time such as 2026-09-01T10:43:57.000000Z (UTC), shown as the Indian date."""
    if not creation_time:
        return None
    try:
        t = dt.datetime.fromisoformat(creation_time.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    d = t.astimezone(IST).date()
    return d if 2015 <= d.year <= dt.date.today().year + 1 and d <= dt.date.today() else None


DATE_NOTE = {"metadata": " (from file metadata)", "assumed": " (assumed: upload date, not confirmed)"}
