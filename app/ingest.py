"""Upload handling: filename parse, magic bytes, size limit, hashing, server-side storage key."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import shutil
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.config import Settings
from app.db import InvoiceEntry, ProcessingJob, Trainer, User, VideoSession
from app.filenames import FilenameError, parse_or_default
from app.invoices import link_matches
from app.validation import check_magic

_CHUNK = 1024 * 1024
log = logging.getLogger("app.ingest")


@dataclass
class IngestResult:
    filename: str
    ok: bool
    message: str
    video_id: str | None = None


def _get_or_create_trainer(db: Session, code: str) -> Trainer:
    t = db.scalar(select(Trainer).where(Trainer.trainer_code == code))
    if t:
        return t
    try:
        with db.begin_nested():  # savepoint: a concurrent upload may create the same trainer first
            t = Trainer(trainer_code=code)
            db.add(t)
            db.flush()
    except IntegrityError:
        t = db.scalar(select(Trainer).where(Trainer.trainer_code == code))
    return t


def ingest_upload(db: Session, settings: Settings, storage, user: User, filename: str, fileobj: BinaryIO,
                  language: str, city: str = "", ip: str = "", invoice_row: int | None = None,
                  trainer: str = "", module: str = "", session_date: str = "") -> IngestResult:
    try:
        return _ingest(db, settings, storage, user, filename, fileobj, language, city, ip, invoice_row,
                       (trainer, module, session_date))
    except Exception:  # noqa: BLE001 - never surface a bare 500 to the uploader
        db.rollback()
        log.exception("ingest failed for %s", filename[:100])
        audit.record(db, user, "upload_error", "file", filename[:100], ip=ip)
        return IngestResult(filename, False, "Server error while saving this file. Please retry; if it persists contact an admin.")


def _ingest(db: Session, settings: Settings, storage, user: User, filename: str, fileobj: BinaryIO,
            language: str, city: str, ip: str, invoice_row: int | None,
            meta: tuple[str, str, str] = ("", "", "")) -> IngestResult:
    def reject(msg: str) -> IngestResult:
        audit.record(db, user, "upload_rejected", "file", filename[:100], {"reason": msg}, ip)
        return IngestResult(filename, False, msg)

    try:
        parsed = parse_or_default(filename, *meta)
    except FilenameError as e:
        return reject(str(e))

    entry = None
    if invoice_row is not None:
        entry = db.scalar(select(InvoiceEntry).where(InvoiceEntry.invoice_row == invoice_row))
        if not entry:
            return reject(f"Invoice row {invoice_row} has not been imported yet. Import the invoice sheet first "
                          "or leave the invoice row blank.")
        if db.scalar(select(VideoSession.id).where(VideoSession.invoice_entry_id == entry.id)):
            return reject(f"Invoice row {invoice_row} already has a recording linked to it.")

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(settings.data_dir).free / 1024**3
    if settings.storage_backend == "local" and free_gb < settings.min_free_disk_gb:
        return reject(f"Server storage is nearly full ({free_gb:.1f} GB free). Ask an admin to free space "
                      "(delete assessed videos or enable DELETE_VIDEO_AFTER_ASSESSMENT).")

    head = fileobj.read(64)
    if err := check_magic(head):
        return reject(err)

    tmp_dir = settings.data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}.part"  # server-generated; user input never touches the path
    sha, size = hashlib.sha256(head), len(head)
    try:
        with open(tmp, "wb") as out:
            out.write(head)
            while chunk := fileobj.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    tmp.unlink(missing_ok=True)
                    return reject(f"File exceeds the {settings.max_upload_bytes // 1024**2} MB limit")
                sha.update(chunk)
                out.write(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    digest = sha.hexdigest()

    if db.scalar(select(VideoSession.id).where(VideoSession.sha256 == digest)):
        tmp.unlink(missing_ok=True)
        return reject("Duplicate upload: this exact file has already been submitted")

    trainer = _get_or_create_trainer(db, parsed.trainer_code)
    if parsed.auto_session:  # no session number given: take the next free one for this trainer, module and date
        used = set(db.scalars(select(VideoSession.session_code).where(
            VideoSession.trainer_id == trainer.id, VideoSession.module_code == parsed.module_code,
            VideoSession.session_date == parsed.session_date)))
        n = 1
        while f"S{n:03d}" in used:
            n += 1
        parsed = dataclasses.replace(parsed, session_code=f"S{n:03d}")
    if db.scalar(select(VideoSession.id).where(
            VideoSession.trainer_id == trainer.id, VideoSession.module_code == parsed.module_code,
            VideoSession.session_code == parsed.session_code, VideoSession.session_date == parsed.session_date)):
        tmp.unlink(missing_ok=True)
        db.rollback()
        return reject("Duplicate session: this trainer/module/session/date was already uploaded")

    key = f"videos/{uuid.uuid4().hex}.{parsed.ext}"
    storage.put(tmp, key)
    session = VideoSession(
        trainer_id=trainer.id, module_code=parsed.module_code, state_code=parsed.state_code,
        state=parsed.state, city=city.strip()[:100], session_code=parsed.session_code,
        session_date=parsed.session_date, language=language, original_filename=filename,
        storage_key=key, sha256=digest, size_bytes=size, uploaded_by=user.id, status="queued",
        invoice_entry_id=entry.id if entry else None)
    db.add(session)
    try:
        db.flush()
        session.video_id = f"VID-{session.id:04d}"
        db.add(ProcessingJob(id=str(uuid.uuid4()), session_id=session.id, status="queued"))
        if entry and not trainer.name and entry.mentor_name:
            trainer.name = entry.mentor_name
        db.commit()
        if not entry:
            link_matches(db)  # auto-link when exactly one invoice row has this trainer and date
    except IntegrityError:  # lost a race against an identical concurrent upload
        db.rollback()
        storage.delete(key)
        return reject("Duplicate: this recording or session was already uploaded")
    audit.record(db, user, "upload", "session", session.video_id,
                 {"filename": filename, "size_bytes": size, "sha256": digest}, ip)
    return IngestResult(filename, True, "Queued for assessment", session.video_id)
