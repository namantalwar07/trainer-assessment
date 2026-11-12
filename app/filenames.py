"""Parse STATE_MODULE_TRAINERID_SESSIONID_DATE.mp4. The name is metadata only, never a path."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

STATE_NAMES = {
    "UP": "Uttar Pradesh", "MP": "Madhya Pradesh", "BR": "Bihar", "RJ": "Rajasthan",
    "MH": "Maharashtra", "GJ": "Gujarat", "WB": "West Bengal", "TN": "Tamil Nadu",
    "KA": "Karnataka", "TG": "Telangana", "AP": "Andhra Pradesh", "OD": "Odisha",
    "JH": "Jharkhand", "CG": "Chhattisgarh", "HR": "Haryana", "PB": "Punjab",
    "DL": "Delhi", "UK": "Uttarakhand", "AS": "Assam", "KL": "Kerala",
}

_PATTERN = re.compile(
    r"^(?P<state>[A-Za-z]{2,3})_(?P<module>[A-Za-z0-9]{1,40})_TR(?P<trainer>\d{1,6})"
    r"_S(?P<session>\d{1,6})_(?P<date>\d{8})\.(?P<ext>mp4|mov)$",
    re.IGNORECASE,
)


class FilenameError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedName:
    state_code: str
    state: str
    module_code: str
    trainer_code: str  # TR-001
    session_code: str  # S001
    session_date: dt.date
    ext: str  # mp4 | mov
    auto_session: bool = False  # session code was not given; the uploader picks the next free one
    date_source: str = "filename"  # form | filename | assumed (file metadata may upgrade 'assumed' later)


def parse_filename(name: str) -> ParsedName:
    if not name or len(name) > 255 or any(ch in name for ch in ("/", "\\", "\x00")) or ".." in name:
        raise FilenameError("Filename contains path characters or is empty/too long")
    m = _PATTERN.match(name)
    if not m:
        raise FilenameError(
            "Filename must follow STATE_MODULE_TRAINERID_SESSIONID_DATE.mp4, e.g. "
            "UP_BUSINESSFUNDAMENTALS_TR001_S001_20261104.mp4"
        )
    try:
        date = dt.datetime.strptime(m["date"], "%Y%m%d").date()
    except ValueError as e:
        raise FilenameError(f"Invalid date {m['date']!r} (expected YYYYMMDD)") from e
    code = m["state"].upper()
    return ParsedName(
        state_code=code,
        state=STATE_NAMES.get(code, code),
        module_code=m["module"].upper(),
        trainer_code=f"TR-{int(m['trainer']):03d}",
        session_code=f"S{int(m['session']):03d}",
        session_date=date,
        ext=m["ext"].lower(),
    )


_FALLBACK_EXT = re.compile(r"\.(mp4|mov)$", re.IGNORECASE)


def parse_or_default(name: str, trainer: str = "", module: str = "", date: str = "") -> ParsedName:
    """Accept any file name. Details given on the upload form win; then a name in the standard format; then defaults
    (trainer TR-000 = unassigned, module GENERAL, today's date). The name is still never used as a path."""
    if not name or len(name) > 255 or any(ch in name for ch in ("/", "\\", "\x00")) or ".." in name:
        raise FilenameError("Filename contains path characters or is empty/too long")
    ext_m = _FALLBACK_EXT.search(name)
    if not ext_m:
        raise FilenameError("Only .mp4 or .mov files are accepted")
    try:
        base = parse_filename(name)
    except FilenameError:
        base = None
    trainer_code = base.trainer_code if base else "TR-000"
    if trainer.strip():
        digits = re.sub(r"\D", "", trainer)
        if not digits or int(digits) > 999999:
            raise FilenameError("Trainer ID must be a number, e.g. 12 or TR-012")
        trainer_code = f"TR-{int(digits):03d}"
    module_code = base.module_code if base else "GENERAL"
    if module.strip():
        module_code = re.sub(r"[^A-Za-z0-9]", "", module).upper()[:40]
        if not module_code:
            raise FilenameError("Module code may only contain letters and digits")
    when, source = (base.session_date, "filename") if base else (None, "")
    if when is None:
        from app.recdate import date_from_name
        when = date_from_name(name)
        source = "filename" if when else "assumed"
        when = when or dt.date.today()
    if date.strip():
        try:
            when, source = dt.date.fromisoformat(date.strip()), "form"
        except ValueError as e:
            raise FilenameError("Session date must look like 2026-11-07") from e
    return ParsedName(
        state_code=base.state_code if base else "NA", state=base.state if base else "Not specified",
        module_code=module_code, trainer_code=trainer_code, session_code=base.session_code if base else "S001",
        session_date=when, ext=ext_m[1].lower(), auto_session=base is None,
        date_source=source)
