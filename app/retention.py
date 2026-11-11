"""Deletion and retention enforcement on our side (Google's 48h Files expiry is not our policy)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import (
    Assessment,
    CoachingAction,
    Evidence,
    HumanReview,
    ModelOutputRaw,
    ProcessingJob,
    UploadedFile,
    VideoSession,
    utcnow,
)


def _remove_video(session: VideoSession, storage) -> None:
    try:
        storage.delete(session.storage_key)
    except FileNotFoundError:
        pass
    session.video_deleted_at = utcnow()


def delete_session(db: Session, storage, settings: Settings, session: VideoSession) -> None:
    """Admin hard-delete: video, report, scores, evidence and reviews. The audit log keeps the event."""
    if session.video_deleted_at is None:
        _remove_video(session, storage)
    a_ids = list(db.scalars(select(Assessment.id).where(Assessment.session_id == session.id)))
    job_ids = list(db.scalars(select(ProcessingJob.id).where(ProcessingJob.session_id == session.id)))
    for a in db.scalars(select(Assessment).where(Assessment.session_id == session.id)):
        if a.report_path:
            (settings.data_dir / a.report_path).unlink(missing_ok=True)
    if a_ids:
        for model in (Evidence, HumanReview, CoachingAction):
            db.execute(delete(model).where(model.assessment_id.in_(a_ids)))
        db.execute(delete(Assessment).where(Assessment.id.in_(a_ids)))
    if job_ids:
        db.execute(delete(ModelOutputRaw).where(ModelOutputRaw.job_id.in_(job_ids)))
        db.execute(delete(UploadedFile).where(UploadedFile.job_id.in_(job_ids)))
        db.execute(delete(ProcessingJob).where(ProcessingJob.id.in_(job_ids)))
    db.delete(session)
    db.commit()


def purge_expired_videos(db: Session, storage, settings: Settings) -> int:
    """Delete video files past RETENTION_DAYS but keep scores and evidence. Returns count removed."""
    cutoff = utcnow() - dt.timedelta(days=settings.retention_days)
    n = 0
    for s in db.scalars(select(VideoSession).where(VideoSession.video_deleted_at.is_(None),
                                                   VideoSession.created_at < cutoff,
                                                   VideoSession.status.in_(("completed", "failed", "quarantined")))):
        _remove_video(s, storage)
        n += 1
    db.commit()
    return n
