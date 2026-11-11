"""Invoice sheet import (CSV) and matching invoice rows to uploaded recordings."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import InvoiceEntry, Trainer, VideoSession

_HEADERS = {
    "invoice_row": {"invoice_row", "row", "invoice row", "row no", "row number", "sr no", "s no", "sno", "sl no"},
    "mentor": {"mentor", "mentor_name", "mentor name", "trainer", "trainer_name", "trainer name", "name of mentor"},
    "trainer_id": {"trainer_id", "trainer id", "mentor_id", "mentor id", "trainer code"},
    "date": {"date", "session_date", "session date", "date of session"},
    "msme": {"msme", "msme_name", "msme name", "mentee", "mentee name", "participant", "business", "enterprise"},
    "start": {"start", "invoiced_start", "invoiced start", "from", "start time", "start_time"},
    "end": {"end", "invoiced_end", "invoiced end", "to", "end time", "end_time"},
    "time": {"time", "invoiced time", "invoiced_time", "timing", "session time", "slot"},
}
_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d.%m.%Y", "%d %b, %Y")
_TIME = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([APap]\.?[Mm]\.?)?\s*$")
TEMPLATE_CSV = ("invoice_row,mentor,trainer_id,date,msme,invoiced_start,invoiced_end\n"
                "22,Shrishti Pathak,TR-001,13 Aug 2026,Shyam / Shri Krishna,1:56 PM,3:26 PM\n")


class InvoiceError(ValueError):
    pass


def parse_time(text: str, default_meridiem: str | None = None) -> tuple[int, str | None]:
    """'1:56 PM' / '13:56' / '1.56pm' -> (seconds after midnight, meridiem seen or None)."""
    m = _TIME.match(text or "")
    if not m:
        raise InvoiceError(f"Cannot read time {text!r} (use e.g. 1:56 PM or 13:56)")
    h, mi, mer = int(m[1]), int(m[2] or 0), (m[3] or "").replace(".", "").upper() or None
    if mi > 59 or h > 24:
        raise InvoiceError(f"Invalid time {text!r}")
    use = mer or default_meridiem
    if use == "PM" and h < 12:
        h += 12
    elif use == "AM" and h == 12:
        h = 0
    return h * 3600 + mi * 60, mer


def parse_time_range(text: str) -> tuple[int, int]:
    """'1:56-3:26 PM', '13:56 to 15:26', '1:56 PM - 3:26 PM' -> (start_s, end_s)."""
    parts = re.split(r"\s*(?:-|–|—|\bto\b)\s*", (text or "").strip(), maxsplit=1)
    if len(parts) != 2:
        raise InvoiceError(f"Cannot read time range {text!r} (use e.g. 1:56-3:26 PM)")
    end_s, end_mer = parse_time(parts[1])
    start_s, _ = parse_time(parts[0], end_mer)
    if end_mer == "PM" and start_s > end_s and start_s - 12 * 3600 >= 0 and parts[0].strip()[-2:].upper() != "PM":
        start_s -= 12 * 3600  # "11:30-12:30 PM": the start is in the morning
    if end_s <= start_s:
        raise InvoiceError(f"End time must be after start time in {text!r}")
    return start_s, end_s


def parse_date(text: str) -> dt.date:
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    raise InvoiceError(f"Cannot read date {text!r} (use e.g. 13 Aug 2026 or 2026-08-13)")


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in fieldnames:
        key = re.sub(r"\s+", " ", (raw or "").strip().lower())
        for canon, names in _HEADERS.items():
            if key in names and canon not in out:
                out[canon] = raw
    return out


def import_csv(db: Session, text: str) -> ImportResult:
    res = ImportResult()
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    cols = _map_headers(reader.fieldnames or [])
    missing = [c for c in ("invoice_row", "date") if c not in cols]
    if "time" not in cols and not ("start" in cols and "end" in cols):
        missing.append("invoiced_start + invoiced_end (or one 'invoiced time' column)")
    if "mentor" not in cols and "trainer_id" not in cols:
        missing.append("mentor (or trainer_id)")
    if missing:
        res.errors.append("Missing column(s): " + ", ".join(missing))
        return res
    for n, row in enumerate(reader, start=2):
        g = lambda k, row=row: (row.get(cols[k], "") if k in cols else "").strip()  # noqa: E731
        if not any((v or "").strip() for v in row.values()):
            continue
        try:
            number = int(re.sub(r"\D", "", g("invoice_row")) or "x")
            start_s, end_s = (parse_time_range(g("time")) if "time" in cols and g("time")
                              else (parse_time(g("start"))[0], parse_time(g("end"), parse_time(g("start"))[1])[0]))
            if end_s <= start_s:
                raise InvoiceError("End time must be after start time")
            entry = db.scalar(select(InvoiceEntry).where(InvoiceEntry.invoice_row == number))
            new = entry is None
            entry = entry or InvoiceEntry(invoice_row=number)
            entry.mentor_name, entry.msme_name = g("mentor")[:255], g("msme")[:255]
            entry.trainer_code = normalise_trainer_code(g("trainer_id"))
            entry.session_date, entry.start_seconds, entry.end_seconds = parse_date(g("date")), start_s, end_s
            db.add(entry)
            res.created += new
            res.updated += not new
        except (InvoiceError, ValueError) as e:
            res.errors.append(f"Line {n}: {e}")
    db.commit()
    return res


def normalise_trainer_code(text: str) -> str:
    m = re.search(r"(\d+)", text or "")
    return f"TR-{int(m[1]):03d}" if m else ""


def link_matches(db: Session) -> int:
    """Link unlinked recordings to the single invoice row with the same date and mentor. Returns links made."""
    made = 0
    entries = list(db.scalars(select(InvoiceEntry)))
    taken = {s.invoice_entry_id for s in db.scalars(select(VideoSession)) if s.invoice_entry_id}
    for sess in db.scalars(select(VideoSession).where(VideoSession.invoice_entry_id.is_(None))):
        t: Trainer = sess.trainer
        cands = [e for e in entries if e.session_date == sess.session_date and e.id not in taken
                 and ((e.trainer_code and e.trainer_code == t.trainer_code)
                      or (e.mentor_name and t.name and e.mentor_name.casefold() == t.name.casefold()))]
        if len(cands) == 1:
            sess.invoice_entry_id = cands[0].id
            taken.add(cands[0].id)
            if not t.name and cands[0].mentor_name:
                t.name = cands[0].mentor_name
            made += 1
    db.commit()
    return made


def link_row(db: Session, sess: VideoSession, invoice_row: int) -> InvoiceEntry:
    entry = db.scalar(select(InvoiceEntry).where(InvoiceEntry.invoice_row == invoice_row))
    if not entry:
        raise InvoiceError(f"Invoice row {invoice_row} has not been imported yet")
    other = db.scalar(select(VideoSession).where(VideoSession.invoice_entry_id == entry.id, VideoSession.id != sess.id))
    if other:
        raise InvoiceError(f"Invoice row {invoice_row} is already linked to {other.video_id}")
    sess.invoice_entry_id = entry.id
    if not sess.trainer.name and entry.mentor_name:
        sess.trainer.name = entry.mentor_name
    db.commit()
    return entry
