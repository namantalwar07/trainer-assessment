"""End to end in mock mode: login, RBAC, upload validation, job processing, PDF, Excel, audit."""

import io
import shutil
import struct
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Assessment, AuditLog, Module, User
from app.security import hash_password
from app.web import create_app

GOOD = "UP_BUSINESSFUNDAMENTALS_TR001_S001_20261104.mp4"
PW = "Correct-Horse-9"


_CLIPS: dict[int, bytes] = {}


def fake_mp4(tag: int = 1) -> bytes:
    """A real 6 s clip when FFmpeg exists (so validation genuinely passes), else a header-only stub."""
    if shutil.which("ffmpeg"):
        if tag not in _CLIPS:
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "c.mp4"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=160x120:rate=10",
                                "-f", "lavfi", "-i", f"sine=frequency={200 + tag * 37}:duration=6", "-shortest",
                                "-c:v", "libx264", "-c:a", "aac", str(out)], check=True)
                _CLIPS[tag] = out.read_bytes()
        return _CLIPS[tag]
    return stub_mp4(tag)


def stub_mp4(tag: int = 1) -> bytes:
    ftyp = b"ftypisom" + struct.pack(">I", 512) + b"isomiso2mp41"
    return struct.pack(">I", 4 + len(ftyp)) + ftyp + bytes([tag]) * 4096


@pytest.fixture
def app(settings):
    app = create_app(replace(settings, embedded_worker=False))
    with app.state.sm() as db:
        for role in ("admin", "operator", "reviewer"):
            db.add(User(email=f"{role}@t.test", role=role, password_hash=hash_password(PW)))
        db.add(Module(code="BUSINESSFUNDAMENTALS", name="BF", learning_outcomes="x", reference_content="ref"))
        db.commit()
    return app


def login(app, role) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"email": f"{role}@t.test", "password": PW})
    assert r.status_code == 303
    return c


def csrf_of(c: TestClient, path="/sessions") -> str:
    import re
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def upload(c, name=GOOD, data=None, language="Hindi"):
    return c.post("/upload", data={"language": language, "csrf": csrf_of(c, "/upload")},
                  files={"files": (name, io.BytesIO(data if data is not None else fake_mp4()), "video/mp4")})


def test_anonymous_is_redirected_everywhere(app):
    c = TestClient(app, follow_redirects=False)
    for path in ("/sessions", "/upload", "/assessments/1", "/assessments/1/report.pdf", "/sessions/1/video",
                 "/exports/master.xlsx", "/admin/users", "/admin/modules"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_wrong_password_and_lockout(app):
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        assert c.post("/login", data={"email": "admin@t.test", "password": "nope"}).status_code == 401
    assert c.post("/login", data={"email": "admin@t.test", "password": PW}).status_code == 429


def test_role_enforcement(app):
    op, rev = login(app, "operator"), login(app, "reviewer")
    assert op.get("/upload").status_code == 200
    assert rev.get("/upload").status_code == 403
    assert op.get("/exports/master.xlsx").status_code == 403
    assert op.get("/assessments/1").status_code == 404  # nothing uploaded yet; results are never role-blocked for own data
    assert op.get("/admin/users").status_code == 403
    assert rev.get("/admin/modules").status_code == 403


def test_csrf_required_on_post(app):
    op = login(app, "operator")
    r = op.post("/upload", data={"language": "Hindi"},
                files={"files": (GOOD, io.BytesIO(fake_mp4()), "video/mp4")})
    assert r.status_code == 403


def test_upload_rejections(app):
    op = login(app, "operator")
    assert "path characters" in upload(op, "../../etc/UP_X_TR001_S001_20261104.mp4").text
    assert "Only .mp4 or .mov" in upload(op, "notes.txt").text
    assert "not a real MP4" in upload(op, GOOD, b"MZ\x90\x00" + b"x" * 200).text


def test_any_file_name_is_accepted_and_sessions_are_numbered(app):
    from app.filenames import parse_or_default
    p = parse_or_default("aatika p1.MP4")
    assert (p.trainer_code, p.module_code, p.ext, p.auto_session) == ("TR-000", "GENERAL", "mp4", True)
    q = parse_or_default("anything.mov", trainer="12", module="Business Fundamentals", date="2026-11-07")
    assert (q.trainer_code, q.module_code, str(q.session_date)) == ("TR-012", "BUSINESSFUNDAMENTALS", "2026-11-07")
    op = login(app, "operator")
    assert "Queued" in upload(op, "aatika p1.mp4").text
    assert "Queued" in upload(op, "second recording.mp4", fake_mp4() + b"different").text  # same defaults: next session number


def test_duplicate_upload_rejected(app):
    op = login(app, "operator")
    assert "Queued" in upload(op).text
    assert "Duplicate" in upload(op).text


def test_size_limit(app, settings):
    small = create_app(replace(settings, embedded_worker=False, max_upload_bytes=1000))  # same DB as `app`
    assert "exceeds" in upload(login(small, "operator")).text


def test_full_pipeline_pdf_excel_and_audit(app):
    op, rev, admin = login(app, "operator"), login(app, "reviewer"), login(app, "admin")
    assert "Queued" in upload(op).text
    app.state.pool = None
    from app.worker import WorkerPool
    pool = WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric)
    pool.run_until_empty()

    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        assert a and a.overall_score and a.model == "gemini-3.8-flash" and a.api_path == "mock"
        assert a.prompt_version == "master_v3+extract_v3" and a.rubric_version == "rubric_v1"
        assert len(a.scores) == 10 and a.evidence
        assert a.time_analysis["total_seconds"] > 0 and a.time_analysis["effective_seconds"] > 0
        assert a.participants and a.participants[0]["name"].startswith("Dummy Mentee")
        job = a.session.jobs[0]
        assert job.status == "completed" and job.token_usage["total"]["input"] > 0
        assert pool.engine.deleted, "Files API object must be released"
        aid, sid = a.id, a.session_id

    pdf = rev.get(f"/assessments/{aid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert op.get(f"/assessments/{aid}/report.pdf").status_code == 200  # the uploader may open their own result
    with app.state.sm() as db:
        db.add(User(email="operator2@t.test", role="operator", password_hash=hash_password(PW)))
        db.commit()
    op2 = TestClient(app, follow_redirects=False)
    assert op2.post("/login", data={"email": "operator2@t.test", "password": PW}).status_code == 303
    assert op2.get(f"/assessments/{aid}/report.pdf").status_code == 403  # but not someone else's
    assert op2.get(f"/assessments/{aid}").status_code == 403
    assert rev.get(f"/assessments/{aid}").status_code == 200
    assert rev.get(f"/sessions/{sid}/video").status_code == 200

    x = rev.get("/exports/master.xlsx")
    assert x.status_code == 200
    try:
        import pyexpat  # noqa: F401
    except ImportError:  # some Homebrew Pythons ship a broken expat: write path (lxml) still works
        import zipfile
        names = zipfile.ZipFile(io.BytesIO(x.content)).namelist()
        assert len([n for n in names if n.startswith("xl/worksheets/sheet")]) == 10
    else:
        wb = openpyxl.load_workbook(io.BytesIO(x.content))
        assert wb.sheetnames == ["Master Assessment", "Session Review", "Competency Scores", "Evidence Register",
                                 "Trainer Summary", "Language and Regional", "Time and Conduct",
                                 "Participants", "Coaching Tracker", "Management Dashboard"]
        assert wb["Master Assessment"].max_row == 2
        assert wb["Competency Scores"].max_row == 11  # header + 10 competencies
        summary = [c.value for c in wb["Trainer Summary"][2]]
        assert "Insufficient sessions" in str(summary[-1])  # one session: never aggregated or ranked

    token = csrf_of(rev, f"/assessments/{aid}")
    r = rev.post(f"/assessments/{aid}/review", data={"csrf": token, "decision": "adjust", "notes": "ok",
                                                      **{f"rating_C{i:02d}": "4" for i in range(1, 11)}})
    assert r.status_code == 303
    assert "adjust" in rev.get(f"/assessments/{aid}").text

    actions = {row[0] for row in app.state.sm().execute(select(AuditLog.action))}
    assert {"login", "upload", "score", "view_assessment", "download_report", "view_video",
            "export_consolidated", "human_review"} <= actions

    token = csrf_of(admin)
    assert admin.post(f"/sessions/{sid}/delete", data={"csrf": token}).status_code == 303
    with app.state.sm() as db:
        assert db.scalar(select(Assessment)) is None
        assert "delete" in {r[0] for r in db.execute(select(AuditLog.action))}


def test_c01_na_and_review_when_module_has_no_reference(app):
    with app.state.sm() as db:
        db.execute(Module.__table__.update().values(reference_content=""))
        db.commit()
    op = login(app, "operator")
    upload(op)
    from app.worker import WorkerPool
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric).run_until_empty()
    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        c01 = next(c for c in a.scores if c["criterion_id"] == "C01")
        assert c01["na"] and a.observed_weight == 80
        assert a.human_review_required and any("C01" in r for r in a.review_reasons)


def test_invalid_model_output_retries_then_fails_with_raw_stored(app):
    from app.db import ModelOutputRaw, ProcessingJob
    from app.gemini.mock import MockEngine
    from app.worker import WorkerPool

    class Broken(MockEngine):
        def score(self, prompt, ctx):
            r = super().score(prompt, ctx)
            r.text = '{"competencies": []}'
            return r

    op = login(app, "operator")
    upload(op)
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric,
               engine=Broken(app.state.settings)).run_until_empty()
    with app.state.sm() as db:
        job = db.scalar(select(ProcessingJob))
        raws = list(db.scalars(select(ModelOutputRaw).where(ModelOutputRaw.stage == "score")))
        assert job.status == "failed" and len(raws) == 4  # 1 try + 3 retries
        assert all(r.valid is False for r in raws)
        assert db.scalar(select(Assessment)) is None


def test_recovers_after_one_bad_output_and_flags_review(app):
    from app.db import ProcessingJob
    from app.gemini.mock import MockEngine
    from app.worker import WorkerPool

    class FlakyOnce(MockEngine):
        calls = 0

        def score(self, prompt, ctx):
            r = super().score(prompt, ctx)
            FlakyOnce.calls += 1
            if FlakyOnce.calls == 1:
                r.text = "not json at all"
            return r

    op = login(app, "operator")
    upload(op)
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric,
               engine=FlakyOnce(app.state.settings)).run_until_empty()
    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        assert db.scalar(select(ProcessingJob)).validation_retries == 1
        assert a.human_review_required and any("validation retr" in r for r in a.review_reasons)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs FFmpeg")
def test_file_that_is_not_a_playable_video_is_quarantined(app):
    from app.db import ProcessingJob
    from app.worker import WorkerPool

    op = login(app, "operator")
    assert "Queued" in upload(op, data=stub_mp4(9)).text  # right header, but not decodable
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric).run_until_empty()
    with app.state.sm() as db:
        job = db.scalar(select(ProcessingJob))
        assert job.status == "quarantined" and job.error
        assert "/" not in job.error and "moov" not in job.error  # friendly text, no server paths
        assert db.scalar(select(Assessment)) is None
        assert job.session.storage_key.startswith("quarantine/")


def test_video_is_deleted_after_assessment_when_configured(settings):
    from app.worker import WorkerPool

    cfg = replace(settings, embedded_worker=False, delete_video_after_assessment=True)
    app = create_app(cfg)
    with app.state.sm() as db:
        db.add(User(email="operator@t.test", role="operator", password_hash=hash_password(PW)))
        db.commit()
    op = login(app, "operator")
    assert "Queued" in upload(op).text
    WorkerPool(app.state.sm, cfg, app.state.storage, app.state.rubric).run_until_empty()
    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        assert a and a.session.video_deleted_at is not None
        assert not list((cfg.data_dir / "videos").rglob("*.mp4"))
        assert (cfg.data_dir / a.report_path).is_file()


def test_upload_refused_when_disk_nearly_full(settings):
    full = create_app(replace(settings, embedded_worker=False, min_free_disk_gb=10**9))
    with full.state.sm() as db:
        db.add(User(email="operator@t.test", role="operator", password_hash=hash_password(PW)))
        db.commit()
    assert "nearly full" in upload(login(full, "operator")).text


def test_each_uploader_has_own_excel_and_admin_sees_all(app):
    op, admin = login(app, "operator"), login(app, "admin")
    assert "Queued" in upload(op, "first.mp4", fake_mp4(1)).text
    assert "Queued" in upload(op, "second.mp4", fake_mp4(2)).text
    page = op.get("/exports")
    assert page.status_code == 200 and "2 recordings so far" in page.text
    # the uploader only sees their own recordings and cannot use the consolidated export
    assert op.get("/exports/consolidated.xlsx").status_code == 403
    assert "first.mp4" in op.get("/sessions").text
    # save and start fresh
    r = op.post("/exports/save-new", data={"csrf": csrf_of(op, "/exports")})
    assert r.status_code == 303 and "Saved+as+Excel+1" in r.headers["location"]
    page = op.get("/exports").text
    assert "0 recordings so far" in page and "Excel 2" in page and "Saved Excels" in page
    assert op.post("/exports/save-new", data={"csrf": csrf_of(op, "/exports")}).headers["location"].startswith(
        "/exports?msg=Nothing")  # an empty batch is not saved
    assert "Queued" in upload(op, "third.mp4", fake_mp4(3)).text
    assert "1 recording so far" in op.get("/exports").text
    # the saved batch still downloads, with only its own two recordings
    with app.state.sm() as db:
        from app.db import ExportBatch
        saved = db.scalar(select(ExportBatch).where(ExportBatch.status == "saved"))
    x = op.get(f"/exports/batch/{saved.id}.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    # admin sees everyone's recordings and can download the consolidated workbook with filters
    assert admin.get("/exports/consolidated.xlsx").status_code == 200
    assert admin.get("/exports/consolidated.xlsx?date_from=2999-01-01").status_code == 200
    assert admin.get("/exports/consolidated.xlsx?date_from=bad").status_code == 400
    assert "Saved Excels" in admin.get("/exports").text


def test_recording_date_from_name_and_metadata():
    import datetime as dt

    from app.filenames import parse_or_default
    from app.recdate import date_from_metadata, date_from_name
    assert date_from_name("GMT20260901-104357_Recording_as_1920x1020.mp4") == dt.date(2026, 9, 1)
    assert date_from_name("GMT20260901-210000_Recording.mp4") == dt.date(2026, 9, 2)  # 21:00 UTC is already 2 Sep in India
    assert date_from_name("Session 14-09-2026 final.mp4") == dt.date(2026, 9, 14)
    assert date_from_name("aatika p1.mp4") is None
    assert date_from_metadata("2026-09-01T10:43:57.000000Z") == dt.date(2026, 9, 1)
    assert date_from_metadata("1970-01-01T00:00:00Z") is None
    p = parse_or_default("GMT20260901-104357_Recording_as_1920x1020.mp4")
    assert (p.session_date, p.date_source) == (dt.date(2026, 9, 1), "filename")
    assert parse_or_default("x.mp4").date_source == "assumed"
    assert parse_or_default("x.mp4", date="2026-01-05").date_source == "form"
