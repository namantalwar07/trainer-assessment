"""FastAPI app. Every route is authenticated and role-checked server-side; nothing is served statically."""

from __future__ import annotations

import datetime as dt
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import audit
from app.config import LANGUAGES, Settings
from app.db import (
    Assessment,
    CoachingAction,
    HumanReview,
    InvoiceEntry,
    Module,
    RubricVersion,
    Trainer,
    User,
    VideoSession,
    make_session_factory,
)
from app.ingest import ingest_upload
from app.invoices import TEMPLATE_CSV, InvoiceError, import_csv, link_matches, link_row, parse_date, parse_time
from app.invoices import normalise_trainer_code as TRAINER_CODE
from app.logging_setup import setup_logging
from app.pipeline import rebuild_report, retry_job, review_for
from app.reports.excel import regenerate_workbook, workbook_path
from app.retention import delete_session
from app.review import report_filename
from app.rubric import load_rubric
from app.scoring import human_overall
from app.security import ROLES, hash_password, password_policy_error, verify_password
from app.storage import GCSStorage, make_storage
from app.timeline import CONDUCT_LABELS, STATE_COLORS, STATE_LABELS
from app.timeutil import fmt_timestamp
from app.worker import WorkerPool

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["hms"] = fmt_timestamp
TEMPLATES.env.globals.update(STATE_COLORS=STATE_COLORS, STATE_LABELS=STATE_LABELS, CONDUCT_LABELS=CONDUCT_LABELS)


class NotAuthenticated(Exception):
    pass


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings)
    sm = make_session_factory(settings.db_url)
    rubric = load_rubric()
    storage = make_storage(settings)
    with sm() as db:
        if not db.get(RubricVersion, rubric.version):
            db.add(RubricVersion(version=rubric.version, definition=rubric.raw))
            db.commit()
    pool = WorkerPool(sm, settings, storage, rubric, engine=engine) if settings.embedded_worker else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if pool:
            pool.start()
        yield
        if pool:
            pool.shutdown()

    app = FastAPI(title="Trainer Assessment", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="strict",
                       https_only=settings.cookie_secure, max_age=8 * 3600)
    app.state.settings, app.state.sm, app.state.rubric = settings, sm, rubric
    app.state.storage, app.state.pool = storage, pool

    # ---------- plumbing ----------
    def get_db(request: Request):
        db = request.app.state.sm()
        try:
            yield db
        finally:
            db.close()

    def current_user(request: Request, db: Session = Depends(get_db)) -> User:
        uid = request.session.get("uid")
        user = db.get(User, uid) if uid else None
        if not user or not user.active:
            raise NotAuthenticated
        return user

    def require(*roles: str):
        def dep(user: User = Depends(current_user)) -> User:
            if user.role != "admin" and user.role not in roles:
                raise HTTPException(403, "Your role does not allow this action")
            return user
        return dep

    operator_up = require("operator")
    reviewer_up = require("reviewer")
    admin_only = require()

    def csrf(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        return request.session["csrf"]

    def check_csrf(request: Request, token: str) -> None:
        if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(403, "Invalid or missing CSRF token")

    def render(request: Request, name: str, user: User | None = None, status: int = 200, **ctx):
        ctx.update(user=user, csrf=csrf(request), mock=settings.mock_gemini, settings=settings, rubric=rubric)
        return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status)

    def ip(request: Request) -> str:
        return request.client.host if request.client else ""

    @app.exception_handler(NotAuthenticated)
    async def _unauth(request: Request, _):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "message": "Session expired. Please sign in again."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_err(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "message": str(exc.detail)}, status_code=exc.status_code)
        return render(request, "error.html", status=exc.status_code, code=exc.status_code, message=exc.detail)

    # ---------- auth ----------
    @app.get("/login")
    def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        email = email.strip().lower()
        if audit.recent_failed_logins(db, email) >= 5:
            raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
        user = db.scalar(select(User).where(User.email == email))
        if not user or not user.active or not verify_password(password, user.password_hash):
            audit.record(db, None, "login_failed", "user", email, ip=ip(request), email=email)
            return render(request, "login.html", status=401, error="Invalid email or password")
        request.session.clear()
        request.session["uid"] = user.id
        audit.record(db, user, "login", "user", user.id, ip=ip(request))
        return RedirectResponse("/sessions", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("", alias="csrf"), user: User = Depends(current_user),
               db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        audit.record(db, user, "logout", "user", user.id, ip=ip(request))
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz(db: Session = Depends(get_db)):
        db.execute(select(1))
        return {"status": "ok", "mode": "mock" if settings.mock_gemini else "live"}

    @app.get("/")
    def home():
        return RedirectResponse("/sessions", status_code=303)

    # ---------- sessions & upload ----------
    @app.get("/sessions")
    def sessions_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
        rows = []
        for s in db.scalars(select(VideoSession).order_by(VideoSession.id.desc()).limit(500)):
            job = max(s.jobs, key=lambda j: j.created_at) if s.jobs else None
            a = db.scalar(select(Assessment).where(Assessment.session_id == s.id).order_by(Assessment.id.desc()))
            rows.append({"s": s, "job": job, "a": a})
        busy = any(r["s"].status in ("queued", "running") for r in rows)
        stats = {
            "total": len(rows),
            "done": sum(r["s"].status == "completed" for r in rows),
            "active": sum(r["s"].status in ("queued", "running") for r in rows),
            "problem": sum(r["s"].status in ("failed", "quarantined") for r in rows),
            "review": sum(bool(r["a"] and r["a"].human_review_required) for r in rows),
        }
        scored = [r["a"].overall_score for r in rows if r["a"] and r["a"].overall_score is not None]
        stats["avg"] = round(sum(scored) / len(scored), 1) if scored else None
        return render(request, "sessions.html", user, rows=rows, busy=busy, stats=stats,
                      can_score=user.role in ("reviewer", "admin"))

    @app.get("/upload")
    def upload_page(request: Request, user: User = Depends(operator_up), db: Session = Depends(get_db)):
        no_ref = [m.code for m in db.scalars(select(Module)) if not m.reference_content.strip()]
        return render(request, "upload.html", user, languages=LANGUAGES, no_ref=no_ref,
                      max_mb=settings.max_upload_bytes // 1024**2)

    @app.post("/upload")
    def upload(request: Request, files: list[UploadFile] = File(...), language: str = Form(...),
               city: str = Form(""), csrf_token: str = Form("", alias="csrf"),
               user: User = Depends(operator_up), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        if language not in LANGUAGES:
            raise HTTPException(400, "Unknown language")
        results = [ingest_upload(db, settings, storage, user, f.filename or "", f.file, language, city, ip(request))
                   for f in files]
        return render(request, "upload_result.html", user, results=results)

    @app.post("/api/upload")
    def api_upload(request: Request, file: UploadFile = File(...), language: str = Form(...),
                   city: str = Form(""), invoice_row: str = Form(""), trainer: str = Form(""), module: str = Form(""),
                   session_date: str = Form(""), user: User = Depends(operator_up),
                   db: Session = Depends(get_db)):
        """One file per request so the browser can show per-file progress and retry independently."""
        check_csrf(request, request.headers.get("x-csrf-token", ""))
        if language not in LANGUAGES:
            raise HTTPException(400, "Unknown language")
        row = int(invoice_row) if invoice_row.strip().isdigit() else None
        if invoice_row.strip() and row is None:
            raise HTTPException(400, "Invoice row must be a number")
        r = ingest_upload(db, settings, storage, user, file.filename or "", file.file, language, city, ip(request),
                          invoice_row=row, trainer=trainer, module=module, session_date=session_date)
        return JSONResponse({"ok": r.ok, "message": r.message, "video_id": r.video_id},
                            status_code=200 if r.ok else 422)

    # ---------- invoices ----------
    @app.get("/invoices")
    def invoices_page(request: Request, user: User = Depends(operator_up), db: Session = Depends(get_db),
                      msg: str = "", err: str = ""):
        entries = db.scalars(select(InvoiceEntry).order_by(InvoiceEntry.invoice_row)).all()
        linked = {s.invoice_entry_id: s for s in db.scalars(select(VideoSession)) if s.invoice_entry_id}
        unlinked = [s for s in db.scalars(select(VideoSession).order_by(VideoSession.id.desc()))
                    if not s.invoice_entry_id]
        return render(request, "invoices.html", user, entries=entries, linked=linked, unlinked=unlinked,
                      msg=msg, err=err)

    @app.get("/invoices/template.csv")
    def invoice_template(user: User = Depends(operator_up)):
        return PlainTextResponse(TEMPLATE_CSV, headers={"Content-Disposition": "attachment; filename=invoice_template.csv"})

    @app.post("/invoices/import")
    def invoices_import(request: Request, file: UploadFile = File(...), csrf_token: str = Form("", alias="csrf"),
                        user: User = Depends(operator_up), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        raw = file.file.read(2_000_001)
        if len(raw) > 2_000_000:
            raise HTTPException(400, "CSV is larger than 2 MB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        res = import_csv(db, text)
        made = link_matches(db)
        audit.record(db, user, "invoice_import", "invoice", file.filename or "", {"created": res.created,
                     "updated": res.updated, "errors": len(res.errors), "linked": made}, ip(request))
        note = f"{res.created} added, {res.updated} updated, {made} recording(s) linked automatically."
        if res.errors:
            return render(request, "invoices.html", user, entries=db.scalars(select(InvoiceEntry).order_by(
                InvoiceEntry.invoice_row)).all(), linked={}, unlinked=[], msg=note, err="\n".join(res.errors[:30]))
        return RedirectResponse(f"/invoices?msg={note}", status_code=303)

    @app.post("/invoices/add")
    def invoices_add(request: Request, invoice_row: int = Form(...), mentor: str = Form(""), trainer_id: str = Form(""),
                     date: str = Form(...), msme: str = Form(""), start: str = Form(...), end: str = Form(...),
                     csrf_token: str = Form("", alias="csrf"), user: User = Depends(operator_up),
                     db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        try:
            s_sec, mer = parse_time(start)
            e_sec, _ = parse_time(end, mer)
            if e_sec <= s_sec:
                raise InvoiceError("End time must be after start time")
            entry = db.scalar(select(InvoiceEntry).where(InvoiceEntry.invoice_row == invoice_row)) or InvoiceEntry(
                invoice_row=invoice_row)
            entry.mentor_name, entry.msme_name = mentor.strip()[:255], msme.strip()[:255]
            entry.trainer_code = TRAINER_CODE(trainer_id)
            entry.session_date, entry.start_seconds, entry.end_seconds = parse_date(date), s_sec, e_sec
            db.add(entry)
            db.commit()
        except InvoiceError as e:
            raise HTTPException(400, str(e)) from e
        link_matches(db)
        audit.record(db, user, "invoice_add", "invoice", invoice_row, ip=ip(request))
        return RedirectResponse(f"/invoices?msg=Invoice row {invoice_row} saved", status_code=303)

    @app.post("/sessions/{sid}/invoice")
    def session_link_invoice(sid: int, request: Request, invoice_row: int = Form(...),
                             csrf_token: str = Form("", alias="csrf"), user: User = Depends(operator_up),
                             db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        s = db.get(VideoSession, sid) or _404()
        try:
            link_row(db, s, invoice_row)
        except InvoiceError as e:
            return RedirectResponse(f"/invoices?err={e}", status_code=303)
        a = db.scalar(select(Assessment).where(Assessment.session_id == s.id).order_by(Assessment.id.desc()))
        if a:
            rebuild_report(db, settings, rubric, a)
        audit.record(db, user, "invoice_link", "session", s.video_id, {"invoice_row": invoice_row}, ip(request))
        return RedirectResponse(f"/invoices?msg={s.video_id} linked to invoice row {invoice_row}", status_code=303)

    @app.post("/sessions/{sid}/retry")
    def retry(sid: int, request: Request, csrf_token: str = Form("", alias="csrf"),
              user: User = Depends(operator_up), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        s = db.get(VideoSession, sid) or _404()
        if retry_job(db, s):
            audit.record(db, user, "retry", "session", s.video_id, ip=ip(request))
        return RedirectResponse("/sessions", status_code=303)

    @app.post("/sessions/{sid}/delete")
    def delete(sid: int, request: Request, csrf_token: str = Form("", alias="csrf"),
               user: User = Depends(admin_only), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        s = db.get(VideoSession, sid) or _404()
        if s.status == "running":
            raise HTTPException(409, "Job is running; try again when it finishes")
        vid = s.video_id
        delete_session(db, storage, settings, s)
        audit.record(db, user, "delete", "session", vid, ip=ip(request))
        return RedirectResponse("/sessions", status_code=303)

    @app.get("/sessions/{sid}/video")
    def video(sid: int, request: Request, user: User = Depends(reviewer_up), db: Session = Depends(get_db)):
        s = db.get(VideoSession, sid) or _404()
        if s.video_deleted_at:
            raise HTTPException(410, "Video removed under the retention policy")
        audit.record(db, user, "view_video", "session", s.video_id, ip=ip(request))
        if isinstance(storage, GCSStorage):
            return RedirectResponse(storage.signed_url(s.storage_key, minutes=10), status_code=303)
        return FileResponse(storage.path(s.storage_key), media_type="video/mp4",
                            headers={"Cache-Control": "private, no-store"})

    # ---------- assessments ----------
    @app.get("/assessments/{aid}")
    def assessment_page(aid: int, request: Request, user: User = Depends(reviewer_up),
                        db: Session = Depends(get_db)):
        a = db.get(Assessment, aid) or _404()
        audit.record(db, user, "view_assessment", "assessment", a.session.video_id, ip=ip(request))
        ev: dict[str, list] = {}
        for e in sorted(a.evidence, key=lambda x: x.seconds):
            ev.setdefault(e.criterion_id, []).append(e)
        job = next((j for j in a.session.jobs if j.id == a.job_id), None)
        coaching = db.scalars(select(CoachingAction).where(CoachingAction.assessment_id == a.id)
                              .order_by(CoachingAction.id)).all()
        return render(request, "assessment.html", user, a=a, ev=ev, job=job, coaching=coaching,
                      review=review_for(db, settings, a),
                      reviews=sorted(a.reviews, key=lambda r: r.created_at, reverse=True))

    @app.post("/assessments/{aid}/review")
    async def review(aid: int, request: Request, user: User = Depends(reviewer_up),
                     db: Session = Depends(get_db)):
        form = await request.form()
        check_csrf(request, str(form.get("csrf", "")))
        a = db.get(Assessment, aid) or _404()
        ratings: dict[str, int | None] = {}
        lo = 0 if settings.zero_anchor else 1
        for cid in rubric.ids:
            raw = str(form.get(f"rating_{cid}", "")).strip()
            if raw in ("", "na"):
                ratings[cid] = None
            elif raw.isdigit() and lo <= int(raw) <= 5:
                ratings[cid] = int(raw)
            else:
                raise HTTPException(400, f"Invalid rating for {cid}")
        decision = str(form.get("decision", ""))
        if decision not in ("accept", "adjust", "re_record"):
            raise HTTPException(400, "Invalid decision")
        db.add(HumanReview(assessment_id=a.id, reviewer_id=user.id, ratings=ratings,
                           human_overall=human_overall(ratings, rubric), decision=decision,
                           notes=str(form.get("notes", ""))[:4000]))
        db.commit()
        audit.record(db, user, "human_review", "assessment", a.session.video_id, {"decision": decision}, ip(request))
        return RedirectResponse(f"/assessments/{aid}", status_code=303)

    @app.get("/assessments/{aid}/report.pdf")
    def report(aid: int, request: Request, user: User = Depends(reviewer_up), db: Session = Depends(get_db)):
        a = db.get(Assessment, aid) or _404()
        path = settings.data_dir / (a.report_path or "")
        if not a.report_path or not path.is_file():
            raise HTTPException(404, "Report not available")
        audit.record(db, user, "download_report", "assessment", a.session.video_id, ip=ip(request))
        return FileResponse(path, media_type="application/pdf", filename=report_filename(a.session.video_id, review_for(db, settings, a)["mentor"],
                                                  [x["name"] for x in a.participants or []]),
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/exports/master.xlsx")
    def export_workbook(request: Request, user: User = Depends(reviewer_up), db: Session = Depends(get_db)):
        regenerate_workbook(sm, settings, rubric)
        audit.record(db, user, "export_workbook", "export", "master.xlsx", ip=ip(request))
        return FileResponse(
            workbook_path(settings), filename="trainer_assessment_master.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Cache-Control": "private, no-store"})

    @app.post("/coaching/{cid}")
    def coaching_update(cid: int, request: Request, owner: str = Form(""), review_date: str = Form(""),
                        status: str = Form("Open"), csrf_token: str = Form("", alias="csrf"),
                        user: User = Depends(reviewer_up), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        c = db.get(CoachingAction, cid) or _404()
        if status not in ("Open", "In progress", "Done"):
            raise HTTPException(400, "Invalid status")
        try:
            c.review_date = dt.date.fromisoformat(review_date) if review_date else None
        except ValueError as e:
            raise HTTPException(400, "Review date must be YYYY-MM-DD") from e
        c.owner, c.status = owner.strip()[:255], status
        db.commit()
        audit.record(db, user, "coaching_update", "coaching", cid, {"status": status}, ip(request))
        return RedirectResponse(f"/assessments/{c.assessment_id}#coaching", status_code=303)

    @app.get("/admin/trainers")
    def trainers_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
        counts = dict(db.execute(select(VideoSession.trainer_id, func.count()).group_by(VideoSession.trainer_id)).all())
        return render(request, "trainers.html", user, counts=counts,
                      trainers=db.scalars(select(Trainer).order_by(Trainer.trainer_code)).all())

    @app.post("/admin/trainers/{tid}")
    def trainers_save(tid: int, request: Request, name: str = Form(""), csrf_token: str = Form("", alias="csrf"),
                      user: User = Depends(admin_only), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        t = db.get(Trainer, tid) or _404()
        t.name = name.strip()[:255]
        db.commit()
        audit.record(db, user, "trainer_update", "trainer", t.trainer_code, ip=ip(request))
        return RedirectResponse("/admin/trainers", status_code=303)

    # ---------- admin ----------
    @app.get("/admin/modules")
    def modules_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
        return render(request, "modules.html", user, modules=db.scalars(select(Module).order_by(Module.code)).all())

    @app.post("/admin/modules")
    def modules_save(request: Request, code: str = Form(...), name: str = Form(""),
                     learning_outcomes: str = Form(""), reference_content: str = Form(""),
                     csrf_token: str = Form("", alias="csrf"), user: User = Depends(admin_only),
                     db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        code = code.strip().upper()
        if not code.isalnum():
            raise HTTPException(400, "Module code must be letters/digits only, as used in filenames")
        m = db.scalar(select(Module).where(Module.code == code)) or Module(code=code)
        m.name, m.learning_outcomes, m.reference_content = name.strip(), learning_outcomes, reference_content
        m.updated_at = dt.datetime.now(dt.UTC)
        db.add(m)
        db.commit()
        audit.record(db, user, "module_update", "module", code, ip=ip(request))
        return RedirectResponse("/admin/modules", status_code=303)

    @app.get("/admin/users")
    def users_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
        return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.email)).all(),
                      roles=ROLES)

    @app.post("/admin/users")
    def users_create(request: Request, email: str = Form(...), name: str = Form(""), role: str = Form(...),
                     password: str = Form(...), csrf_token: str = Form("", alias="csrf"),
                     user: User = Depends(admin_only), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        email = email.strip().lower()
        if role not in ROLES:
            raise HTTPException(400, "Unknown role")
        if err := password_policy_error(password):
            raise HTTPException(400, err)
        if db.scalar(select(User).where(User.email == email)):
            raise HTTPException(409, "A user with this email already exists")
        db.add(User(email=email, name=name.strip(), role=role, password_hash=hash_password(password)))
        db.commit()
        audit.record(db, user, "user_create", "user", email, {"role": role}, ip(request))
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{uid}/toggle")
    def users_toggle(uid: int, request: Request, csrf_token: str = Form("", alias="csrf"),
                     user: User = Depends(admin_only), db: Session = Depends(get_db)):
        check_csrf(request, csrf_token)
        target = db.get(User, uid) or _404()
        if target.id == user.id:
            raise HTTPException(400, "You cannot deactivate yourself")
        target.active = not target.active
        db.commit()
        audit.record(db, user, "user_toggle", "user", target.email, {"active": target.active}, ip(request))
        return RedirectResponse("/admin/users", status_code=303)

    return app


def _404():
    raise HTTPException(404, "Not found")


def get_app() -> FastAPI:  # uvicorn --factory app.web:get_app
    return create_app()
