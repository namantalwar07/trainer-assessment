"""Append-only audit trail: who did what to which object, and when."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import AuditLog, User, utcnow


def record(db: Session, user: User | None, action: str, object_type: str = "", object_id: object = "",
           detail: dict | None = None, ip: str = "", email: str = "") -> None:
    db.add(AuditLog(
        user_id=user.id if user else None,
        user_email=user.email if user else email,
        action=action, object_type=object_type, object_id=str(object_id),
        detail=detail, ip=ip,
    ))
    db.commit()


def recent_failed_logins(db: Session, email: str, minutes: int = 15) -> int:
    import datetime as dt

    since = utcnow() - dt.timedelta(minutes=minutes)
    return db.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "login_failed", AuditLog.user_email == email, AuditLog.ts >= since)
    ) or 0
