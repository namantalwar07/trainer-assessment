"""Per-uploader Excel batches. Uploads join the uploader's open batch; saving it starts a fresh one."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import ExportBatch, User, VideoSession, utcnow


def open_batch(db: Session, user: User) -> ExportBatch:
    b = db.scalar(select(ExportBatch).where(ExportBatch.owner_id == user.id, ExportBatch.status == "open")
                  .order_by(ExportBatch.id.desc()))
    if b:
        return b
    number = (db.scalar(select(func.max(ExportBatch.number)).where(ExportBatch.owner_id == user.id)) or 0) + 1
    b = ExportBatch(owner_id=user.id, number=number)
    db.add(b)
    db.flush()
    return b


def save_and_start_new(db: Session, user: User) -> tuple[ExportBatch, ExportBatch]:
    """Close the current batch (its Excel stays downloadable) and open an empty one."""
    cur = open_batch(db, user)
    cur.status, cur.saved_at = "saved", utcnow()
    db.flush()
    return cur, open_batch(db, user)


def batch_count(db: Session, batch_id: int) -> int:
    return db.scalar(select(func.count()).select_from(VideoSession).where(VideoSession.batch_id == batch_id)) or 0


def backfill(db: Session) -> list[int]:
    """One-off tidy for recordings uploaded before batches and date detection existed.

    Puts each into its uploader's open Excel and replaces an assumed date with the one in the file name.
    Returns the ids of sessions whose date changed (their PDFs need rebuilding).
    """
    from app.filenames import FilenameError, parse_filename
    from app.recdate import date_from_name

    changed: list[int] = []
    for s in db.scalars(select(VideoSession).where(VideoSession.date_source.is_(None))):
        try:
            parse_filename(s.original_filename)
            s.date_source = "filename"
            continue
        except FilenameError:
            pass
        d = date_from_name(s.original_filename)
        if d and d != s.session_date:
            s.session_date, s.date_source = d, "filename"
            changed.append(s.id)
        else:
            s.date_source = "filename" if d else "assumed"
    for s in db.scalars(select(VideoSession).where(VideoSession.batch_id.is_(None))):
        owner = db.get(User, s.uploaded_by)
        if owner:
            s.batch_id = open_batch(db, owner).id
    db.commit()
    return changed
