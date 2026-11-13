"""Per-video job: validate -> Files API -> extract (video) -> score (text, retried) -> compute -> persist."""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import audit
from app.chunking import merge_extractions, part_note, slice_signals, split_video
from app.config import Settings
from app.db import (
    Assessment,
    CoachingAction,
    Evidence,
    InvoiceEntry,
    ModelOutputRaw,
    Module,
    ProcessingJob,
    UploadedFile,
    VideoSession,
    utcnow,
)
from app.gemini.base import AssessmentEngine, TokenUsage, TransientError
from app.prompts import PROMPT_VERSION, build_extract_prompt, build_score_prompt
from app.recdate import date_from_metadata
from app.reports.pdf import build_pdf
from app.review import build_review
from app.rubric import Rubric
from app.schema import normalize_payload, parse_timestamp, validate_payload
from app.scoring import compute_scores
from app.signals import long_silences, measure_signals, signals_for_prompt
from app.timeline import analyse, clean_participants, conduct_summary
from app.validation import validate_video

log = logging.getLogger("app.pipeline")
MIME = {"mp4": "video/mp4", "mov": "video/quicktime"}


def parse_model_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError("Top-level JSON value must be an object")
    return data


def with_backoff(fn: Callable, settings: Settings, sleep: Callable[[float], None]):
    for attempt in range(6):  # waits 5, 10, 20, 40, 80 s: rides out a short network or API outage
        try:
            return fn()
        except TransientError:
            if attempt == 5:
                raise
            sleep(settings.backoff_base_seconds * 2**attempt)


def _add_usage(total: dict, stage: str, u: TokenUsage) -> None:
    total[stage] = u.as_dict()
    for k, v in u.as_dict().items():
        total["total"][k] = total["total"].get(k, 0) + v


def estimate_cost_usd(total: dict, settings: Settings) -> float:
    t = total["total"]
    return round(t.get("input", 0) * settings.price_input_per_m_usd / 1e6
                 + (t.get("output", 0) + t.get("thinking", 0)) * settings.price_output_per_m_usd / 1e6, 4)


def _finish(job: ProcessingJob, status: str, error: str | None = None) -> None:
    log.info("job %s %s video=%s%s", job.id[:8], status, job.session.video_id, f" error={error[:200]}" if error else "")
    job.status, job.error, job.finished_at = status, error, utcnow()
    job.session.status = status


def run_job(job_id: str, *, sm: sessionmaker, settings: Settings, storage, engine: AssessmentEngine,
            rubric: Rubric, sleep: Callable[[float], None] = time.sleep) -> None:
    db: Session = sm()
    handles: list = []  # (Files API handle, its UploadedFile row) for every part analysed
    try:
        job = db.get(ProcessingJob, job_id)
        sess = job.session
        job.status, job.started_at, job.api_path = "running", utcnow(), engine.api_path
        job.attempts += 1
        sess.status = "running"
        db.commit()
        log.info("job %s start video=%s size=%.0fMB", job.id[:8], sess.video_id, sess.size_bytes / 1e6)

        module = db.query(Module).filter_by(code=sess.module_code).one_or_none()
        has_ref = bool(module and module.reference_content.strip())
        ctx = {
            "video_id": sess.video_id, "trainer_id": sess.trainer.trainer_code,
            "module": (module.name if module and module.name else sess.module_code),
            "language": sess.language, "state": sess.state, "has_reference": has_ref,
            "learning_outcomes": module.learning_outcomes if module else "",
            "reference_content": module.reference_content if module else "", "duration_seconds": 0,
        }
        usage = {"total": {}}

        with storage.local_copy(sess.storage_key) as local_path:
            v = validate_video(Path(local_path), settings)
            sess.validation = v.as_dict()
            if not v.ok:
                new_key = f"quarantine/{Path(sess.storage_key).name}"
                storage.move(sess.storage_key, new_key)
                sess.storage_key = new_key
                _finish(job, "quarantined", "; ".join(v.errors))
                db.commit()
                audit.record(db, None, "quarantine", "session", sess.video_id, {"errors": v.errors},
                             email="system")
                return
            sess.duration_seconds = v.duration_seconds
            ctx["duration_seconds"] = v.duration_seconds
            signals = measure_signals(Path(local_path), v.duration_seconds, settings)  # silence, freezes, jumps
            if sess.date_source == "assumed" and (d := date_from_metadata(signals.get("metadata", {}).get("creation_time"))):
                _adopt_date(db, sess, d)  # no date in the name or form: use the file's own recording date
            ctx["signals_text"] = signals_for_prompt(signals)
            db.commit()

            mime = MIME[sess.storage_key.rsplit(".", 1)[-1]]
            parts_dir = settings.data_dir / "tmp" / f"parts_{job.id[:8]}"
            try:
                chunks = split_video(Path(local_path), parts_dir, v.duration_seconds, settings.chunk_minutes * 60)
                if len(chunks) > 1:
                    log.info("job %s long recording: watching in %d parts", job.id[:8], len(chunks))
                part_texts: list[tuple[str, float]] = []
                extract_path = engine.api_path
                for i, ch in enumerate(chunks, 1):
                    done = db.scalar(select(ModelOutputRaw).where(
                        ModelOutputRaw.job_id == job.id, ModelOutputRaw.stage == "extract",
                        ModelOutputRaw.attempt == i))
                    if done:  # a retried job keeps the parts it already paid for
                        log.info("job %s part %d/%d already analysed: reusing it", job.id[:8], i, len(chunks))
                        part_texts.append((done.text, ch.offset))
                        tu = done.token_usage or {}
                        _add_usage(usage, "extract", TokenUsage(tu.get("input", 0), tu.get("output", 0),
                                                                tu.get("thinking", 0)))
                        continue
                    pctx = dict(ctx)
                    if len(chunks) > 1:
                        end = ch.offset + ch.duration
                        pctx.update(duration_seconds=ch.duration, part_note=part_note(i, len(chunks), ch.offset, end),
                                    signals_text=signals_for_prompt(slice_signals(signals, ch.offset, end)))
                    h = with_backoff(lambda path=ch.path: engine.prepare_video(path, mime), settings, sleep)
                    up = UploadedFile(job_id=job.id, files_api_name=h.name, uri=h.uri, state=h.state)
                    db.add(up)
                    handles.append((h, up))
                    db.commit()
                    ex_i = with_backoff(lambda hh=h, pc=pctx: engine.extract(hh, build_extract_prompt(pc), pc),
                                        settings, sleep)
                    extract_path = ex_i.api_path or engine.api_path
                    _add_usage(usage, "extract", ex_i.usage)
                    db.add(ModelOutputRaw(job_id=job.id, stage="extract", attempt=i, text=ex_i.text,
                                          token_usage=ex_i.usage.as_dict()))
                    job.api_path = extract_path
                    db.commit()
                    part_texts.append((ex_i.text, ch.offset))
            finally:
                shutil.rmtree(parts_dir, ignore_errors=True)
            extract_text = part_texts[0][0] if len(part_texts) == 1 else merge_extractions(part_texts)

        # Stage 2 is text only: schema failures are retried here without re-reading the video.
        payload, notes, errors, retries = None, [], ["no attempt"], 0
        score_usage = TokenUsage()
        for attempt in range(1, settings.max_validation_retries + 2):
            score_prompt = build_score_prompt(ctx, rubric, settings, extract_text, feedback=None if attempt == 1 else errors)
            res = with_backoff(lambda prompt=score_prompt: engine.score(prompt, ctx), settings, sleep)
            score_usage = TokenUsage(score_usage.input_tokens + res.usage.input_tokens,
                                     score_usage.output_tokens + res.usage.output_tokens,
                                     score_usage.thinking_tokens + res.usage.thinking_tokens)
            raw = ModelOutputRaw(job_id=job.id, stage="score", attempt=attempt, text=res.text,
                                 token_usage=res.usage.as_dict())
            db.add(raw)
            try:
                candidate, notes = normalize_payload(parse_model_json(res.text), rubric, has_ref)
                errors = validate_payload(candidate, rubric, settings, v.duration_seconds)
            except (ValueError, KeyError, TypeError) as e:
                candidate, errors = None, [f"Not valid JSON/structure: {str(e)[:200]}"]
            raw.valid, raw.errors = not errors, errors or None
            db.commit()
            if not errors:
                payload = candidate
                break
            retries += 1
            job.validation_retries = retries
            db.commit()
            if attempt <= settings.max_validation_retries:
                sleep(settings.backoff_base_seconds * 2 ** (attempt - 1))
        _add_usage(usage, "score", score_usage)
        job.token_usage = usage
        job.cost_usd = estimate_cost_usd(usage, settings)

        if payload is None:  # failed-jobs queue: raw outputs are kept for inspection
            _finish(job, "failed", f"Output failed validation after {retries} attempts: {errors[:3]}")
            db.commit()
            return

        # Backend identity overrides: never trust ids echoed by the model.
        payload.update(video_id=sess.video_id, trainer_id=sess.trainer.trainer_code,
                       primary_language=sess.language)
        time_analysis = analyse(payload["timeline"], v.duration_seconds, settings,
                                silences=long_silences(signals, settings.silence_min_seconds))
        participants = clean_participants(payload["participants"])
        conduct_sum, conduct_reasons = conduct_summary(payload["conduct_events"], settings)
        invoice = db.get(InvoiceEntry, sess.invoice_entry_id) if sess.invoice_entry_id else None
        review = _review(sess, invoice, time_analysis.as_dict(), payload["integrity"], payload["clock_readings"],
                         signals, payload["observation_note"], settings,
                         mentor_ai=str(payload.get("mentor_name", "")), participants=participants)
        result = compute_scores(payload, rubric, settings, validation_retries=retries, notes=notes,
                                has_reference=has_ref,
                                extra_reasons=time_analysis.review_reasons + conduct_reasons + review["flags"])
        by_id = {c["criterion_id"]: c for c in payload["competencies"]}
        a = Assessment(
            job_id=job.id, session_id=sess.id, overall_score=result.overall_score, band=result.band,
            observed_weight=result.observed_weight, coverage_text=result.coverage_text,
            human_review_required=result.review_required, review_reasons=result.review_reasons,
            scores=[c.as_dict() for c in result.competencies], top_strengths=payload["top_strengths"],
            development_areas=payload["development_areas"], coaching_plan=payload["coaching_plan"],
            critical_flags=payload["critical_flags"], model=engine.model, prompt_version=PROMPT_VERSION,
            rubric_version=rubric.version, api_path=extract_path, token_usage=usage,
            time_analysis=time_analysis.as_dict(), participants=participants,
            conduct_events=payload["conduct_events"], conduct_summary=conduct_sum,
            integrity=payload["integrity"], clock_readings=payload["clock_readings"], forensics=signals,
            observation_note=payload["observation_note"], mentor_name=str(payload.get("mentor_name", ""))[:100])
        for cid, c in by_id.items():
            for e in c["evidence"]:
                a.evidence.append(Evidence(criterion_id=cid, timestamp=e["timestamp"],
                                           seconds=parse_timestamp(e["timestamp"]) or 0,
                                           observation=e["observation"], assessment_note=e["assessment"]))
        db.add(a)
        db.flush()
        for item in payload["coaching_plan"]:
            db.add(CoachingAction(assessment_id=a.id, trainer_id=sess.trainer_id,
                                  competency=item["competency"], action=item["action"],
                                  priority=item["priority"]))
        report = settings.data_dir / "reports" / f"{sess.video_id}.pdf"
        report.parent.mkdir(parents=True, exist_ok=True)
        db.flush()
        build_pdf(a, sess, rubric, report, review, tolerance_text=f"{settings.gap_tolerance_seconds / 60:g} minutes")
        a.report_path = str(report.relative_to(settings.data_dir))
        _finish(job, "completed")
        db.commit()
        if settings.delete_video_after_assessment:  # stored copy only; scores, evidence and PDF are kept
            storage.delete(sess.storage_key)
            sess.video_deleted_at = utcnow()
            db.commit()
        audit.record(db, None, "score", "assessment", sess.video_id,
                     {"overall": result.overall_score, "band": result.band}, email="system")
    except Exception as e:  # noqa: BLE001 - job boundary: record and surface, never crash the worker
        db.rollback()
        job = db.get(ProcessingJob, job_id)
        if job is not None:
            _finish(job, "failed", f"{type(e).__name__}: {str(e)[:500]}")
            db.commit()
    finally:
        for h, up in handles:  # Files API cleanup on every terminal state
            engine.release(h)
            up.deleted_at = utcnow()
        if handles:
            db.commit()
        db.close()


def retry_job(db: Session, session: VideoSession) -> bool:
    """Requeue a failed job. Each run uploads a fresh Files object, so 48h expiry is never a problem."""
    job = max(session.jobs, key=lambda j: j.created_at)
    if job.status != "failed":
        return False
    job.status, job.error, job.finished_at, session.status = "queued", None, None, "queued"
    db.commit()
    return True


def reassess(db: Session, session: VideoSession) -> bool:
    """Queue a fresh assessment of an already-assessed recording (new job; the old assessment is kept as history)."""
    import uuid

    from app.db import ProcessingJob
    if any(j.status in ("queued", "running") for j in session.jobs):
        return False
    db.add(ProcessingJob(id=str(uuid.uuid4()), session_id=session.id, status="queued"))
    session.status = "queued"
    db.commit()
    return True


def _adopt_date(db: Session, sess: VideoSession, d) -> None:
    """Replace the assumed upload date with the file's recording date, unless that would duplicate a session."""
    old = sess.session_date
    try:
        with db.begin_nested():  # savepoint: a clash must not undo the rest of the job
            sess.session_date, sess.date_source = d, "metadata"
            db.flush()
    except IntegrityError:
        sess.session_date, sess.date_source = old, "assumed"


def _review(sess, invoice, ta, integrity, clock_readings, signals, note, settings, mentor_ai="",
            participants=None) -> dict:
    return build_review(session=sess, trainer_name=sess.trainer.name, invoice=invoice, ta=ta, integrity=integrity,
                        clock_readings=clock_readings, forensics=signals, observation_note=note, settings=settings,
                        mentor_ai=mentor_ai, mentees=[x["name"] for x in participants or []])


def review_for(db: Session, settings: Settings, a: Assessment) -> dict:
    """Review content for an existing assessment (used by the PDF, the web page and the Excel export)."""
    sess = a.session
    invoice = db.get(InvoiceEntry, sess.invoice_entry_id) if sess.invoice_entry_id else None
    return _review(sess, invoice, a.time_analysis, a.integrity, a.clock_readings, a.forensics, a.observation_note,
                   settings, mentor_ai=a.mentor_name or "", participants=a.participants)


_FLAG_PREFIXES = ("Integrity:", "Invoice check:")


def rebuild_report(db: Session, settings: Settings, rubric: Rubric, assessment: Assessment) -> None:
    """Regenerate the PDF from stored data (for example after an invoice row is linked). No model call."""
    sess = assessment.session
    review = review_for(db, settings, assessment)
    kept = [r for r in (assessment.review_reasons or []) if not r.startswith(_FLAG_PREFIXES)]
    assessment.review_reasons = kept + review["flags"]
    assessment.human_review_required = bool(assessment.review_reasons)
    report = settings.data_dir / "reports" / f"{sess.video_id}.pdf"
    report.parent.mkdir(parents=True, exist_ok=True)
    build_pdf(assessment, sess, rubric, report, review, tolerance_text=f"{settings.gap_tolerance_seconds / 60:g} minutes")
    assessment.report_path = str(report.relative_to(settings.data_dir))
    db.commit()
