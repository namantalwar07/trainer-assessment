"""Per-video job: validate -> Files API -> extract (video) -> score (text, retried) -> compute -> persist."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app import audit
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
    for attempt in range(4):
        try:
            return fn()
        except TransientError:
            if attempt == 3:
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
    handle = None
    uploaded: UploadedFile | None = None
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
            ctx["signals_text"] = signals_for_prompt(signals)
            db.commit()

            handle = with_backoff(lambda: engine.prepare_video(Path(local_path), MIME[sess.storage_key.rsplit(".", 1)[-1]]),
                                  settings, sleep)
            uploaded = UploadedFile(job_id=job.id, files_api_name=handle.name, uri=handle.uri, state=handle.state)
            db.add(uploaded)
            db.commit()

            extract_prompt = build_extract_prompt(ctx)
            ex = with_backoff(lambda: engine.extract(handle, extract_prompt, ctx), settings, sleep)
            extract_path = ex.api_path or engine.api_path
            _add_usage(usage, "extract", ex.usage)
            job.api_path = extract_path
            db.add(ModelOutputRaw(job_id=job.id, stage="extract", attempt=1, text=ex.text,
                                  token_usage=ex.usage.as_dict()))
            db.commit()

        # Stage 2 is text only: schema failures are retried here without re-reading the video.
        payload, notes, errors, retries = None, [], ["no attempt"], 0
        score_usage = TokenUsage()
        for attempt in range(1, settings.max_validation_retries + 2):
            score_prompt = build_score_prompt(ctx, rubric, settings, ex.text, feedback=None if attempt == 1 else errors)
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
        if handle is not None:  # Files API cleanup on every terminal state
            engine.release(handle)
            if uploaded is not None:
                uploaded.deleted_at = utcnow()
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
