"""SQLAlchemy models. Raw AI outputs, validated assessments and human reviews live in separate tables."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

JsonType = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20))  # operator | reviewer | admin
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Trainer(Base):
    __tablename__ = "trainers"
    id: Mapped[int] = mapped_column(primary_key=True)
    trainer_code: Mapped[str] = mapped_column(String(20), unique=True)  # TR-001
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Module(Base):
    """Ground truth for C01. Without reference_content, C01 is forced to N/A."""

    __tablename__ = "modules"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    learning_outcomes: Mapped[str] = mapped_column(Text, default="")
    reference_content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvoiceEntry(Base):
    """One invoiced mentoring session (a row of the invoice sheet) that a recording is checked against."""

    __tablename__ = "invoice_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_row: Mapped[int] = mapped_column(Integer, unique=True)
    mentor_name: Mapped[str] = mapped_column(String(255), default="")
    trainer_code: Mapped[str] = mapped_column(String(20), default="")  # optional, e.g. TR-001
    session_date: Mapped[dt.date] = mapped_column()
    msme_name: Mapped[str] = mapped_column(String(255), default="")
    start_seconds: Mapped[int] = mapped_column(Integer)  # seconds after midnight, as invoiced
    end_seconds: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VideoSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("trainer_id", "module_code", "session_code", "session_date",
                                       name="uq_session_identity"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[str] = mapped_column(String(20), unique=True, default="")
    trainer_id: Mapped[int] = mapped_column(ForeignKey("trainers.id"))
    module_code: Mapped[str] = mapped_column(String(60))
    state_code: Mapped[str] = mapped_column(String(5))
    state: Mapped[str] = mapped_column(String(100))
    city: Mapped[str] = mapped_column(String(100), default="")
    session_code: Mapped[str] = mapped_column(String(20))
    session_date: Mapped[dt.date] = mapped_column()
    language: Mapped[str] = mapped_column(String(60))
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    video_deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invoice_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the uploader's Excel batch
    date_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # form|filename|metadata|assumed
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    trainer: Mapped[Trainer] = relationship()
    jobs: Mapped[list[ProcessingJob]] = relationship(back_populates="session")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|completed|failed|quarantined
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    validation_retries: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_path: Mapped[str] = mapped_column(String(40), default="")
    token_usage: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[VideoSession] = relationship(back_populates="jobs")


class ExportBatch(Base):
    """One uploader's Excel: all recordings they upload go into their open batch until they save it and start a new one."""

    __tablename__ = "export_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    number: Mapped[int] = mapped_column(Integer, default=1)  # 1, 2, 3 ... per owner
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | saved
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    saved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UploadedFile(Base):
    """Files API objects. Deleted explicitly once the job is terminal."""

    __tablename__ = "uploaded_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id"))
    files_api_name: Mapped[str] = mapped_column(String(255))
    uri: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[str] = mapped_column(String(30), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelOutputRaw(Base):
    __tablename__ = "model_outputs_raw"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id"))
    stage: Mapped[str] = mapped_column(String(20))  # extract | score
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text)
    token_usage: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    errors: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RubricVersion(Base):
    __tablename__ = "rubric_versions"
    version: Mapped[str] = mapped_column(String(40), primary_key=True)
    definition: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Assessment(Base):
    """Final validated, backend-computed result. Never contains model-computed totals."""

    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id"), unique=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str] = mapped_column(String(40))
    observed_weight: Mapped[float] = mapped_column(Float)
    coverage_text: Mapped[str] = mapped_column(String(200))
    human_review_required: Mapped[bool] = mapped_column(Boolean)
    review_reasons: Mapped[list] = mapped_column(JsonType)
    scores: Mapped[list] = mapped_column(JsonType)  # per-competency computed results
    top_strengths: Mapped[list] = mapped_column(JsonType)
    development_areas: Mapped[list] = mapped_column(JsonType)
    coaching_plan: Mapped[list] = mapped_column(JsonType)
    critical_flags: Mapped[list] = mapped_column(JsonType)
    model: Mapped[str] = mapped_column(String(60))
    prompt_version: Mapped[str] = mapped_column(String(60))
    rubric_version: Mapped[str] = mapped_column(String(40))
    api_path: Mapped[str] = mapped_column(String(40))
    token_usage: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    time_analysis: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    participants: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    mentor_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    conduct_events: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    conduct_summary: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    integrity: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    clock_readings: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    forensics: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    observation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[VideoSession] = relationship()
    evidence: Mapped[list[Evidence]] = relationship(back_populates="assessment", cascade="all, delete-orphan")
    reviews: Mapped[list[HumanReview]] = relationship(back_populates="assessment")


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"))
    criterion_id: Mapped[str] = mapped_column(String(5))
    timestamp: Mapped[str] = mapped_column(String(8))
    seconds: Mapped[int] = mapped_column(Integer)
    observation: Mapped[str] = mapped_column(Text)
    assessment_note: Mapped[str] = mapped_column(Text, default="")
    assessment: Mapped[Assessment] = relationship(back_populates="evidence")


class HumanReview(Base):
    __tablename__ = "human_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ratings: Mapped[dict] = mapped_column(JsonType)  # {"C01": 4 | null, ...}
    human_overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str] = mapped_column(String(30))  # accept | adjust | re_record
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assessment: Mapped[Assessment] = relationship(back_populates="reviews")
    reviewer: Mapped[User] = relationship()


class CoachingAction(Base):
    __tablename__ = "coaching_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"))
    trainer_id: Mapped[int] = mapped_column(ForeignKey("trainers.id"))
    competency: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10))
    owner: Mapped[str] = mapped_column(String(255), default="")
    review_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Open")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_email: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(60), index=True)
    object_type: Mapped[str] = mapped_column(String(40), default="")
    object_id: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")


def ensure_columns(engine) -> None:
    """Tiny forward-only migration: add nullable columns introduced after a database was first created."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have and col.nullable and not col.primary_key:
                ddl = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))


def make_session_factory(url: str) -> sessionmaker:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, pool_pre_ping=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(engine)
    ensure_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)
