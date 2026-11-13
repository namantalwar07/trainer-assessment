"""Long recordings are watched in parts and merged back onto the full-recording clock."""

import json
import shutil
import subprocess
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.chunking import merge_extractions, plan_parts, shift, slice_signals, split_video
from app.db import Assessment, ModelOutputRaw, UploadedFile

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")


def test_plan_parts():
    assert plan_parts(3600, 3900) == 1 and plan_parts(4400, 3900) == 1  # slightly long: not worth splitting
    assert plan_parts(7770, 3900) == 2 and plan_parts(10000, 3900) == 3 and plan_parts(9999, 0) == 1


def test_shift_moves_timestamps_but_not_the_meeting_clock():
    d = {"start": "00:10:00", "note": "froze at 00:41:50 for a minute", "clock_time": "12:30", "n": 3,
         "list": [{"timestamp": "01:00:05"}]}
    out = shift(d, 3900)
    assert out["start"] == "01:15:00" and "01:46:50" in out["note"] and out["clock_time"] == "12:30"
    assert out["list"][0]["timestamp"] == "02:05:05" and out["n"] == 3


def test_merge_combines_parts_on_one_clock():
    a = {"session_summary": "Part one.", "activity_timeline": [{"start": "00:00:00", "end": "00:30:00"}],
         "integrity": {"clock_continuous": True, "freezes": [{"timestamp": "00:10:00"}]}, "mentor_name": "Tanushree"}
    b = {"session_summary": "Part two.", "activity_timeline": [{"start": "00:00:00", "end": "00:20:00"}],
         "integrity": {"clock_continuous": False, "freezes": []}, "mentor_name": ""}
    m = json.loads(merge_extractions([(json.dumps(a), 0), ("```json\n" + json.dumps(b) + "\n```", 3900)]))
    assert [x["start"] for x in m["activity_timeline"]] == ["00:00:00", "01:05:00"]
    assert m["activity_timeline"][1]["end"] == "01:25:00"
    assert m["integrity"]["clock_continuous"] is False and len(m["integrity"]["freezes"]) == 1
    assert m["session_summary"] == "Part one. Part two." and m["mentor_name"] == "Tanushree"


def test_signals_are_sliced_to_each_part():
    sig = {"silences": [{"start": 100, "end": 4000}, {"start": 5000, "end": 5100}], "freezes": [],
           "pts_jumps": [{"at": 4500, "jump_seconds": 6}], "metadata": {}, "notes": []}
    p2 = slice_signals(sig, 3900, 7770)
    assert p2["silences"] == [{"start": 0, "end": 100}, {"start": 1100, "end": 1200}]
    assert p2["pts_jumps"][0]["at"] == 600
    assert slice_signals(sig, 0, 3900)["silences"] == [{"start": 100, "end": 3900}]


def _clip(path, seconds, gop=10):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
                    "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}", "-shortest", "-c:v", "libx264",
                    "-g", str(gop), "-c:a", "aac", str(path)], check=True)


@needs_ffmpeg
def test_split_video_parts_add_up_to_the_whole(tmp_path):
    src = tmp_path / "long.mp4"
    _clip(src, 30)
    parts = split_video(src, tmp_path / "parts", 30.0, 10.0)
    assert len(parts) == 3 and parts[0].offset == 0
    assert abs(sum(p.duration for p in parts) - 30) < 1.5
    assert all(parts[i + 1].offset == pytest.approx(parts[i].offset + parts[i].duration) for i in range(2))
    assert split_video(src, tmp_path / "none", 30.0, 600.0)[0].path == src  # short enough: untouched


@needs_ffmpeg
def test_pipeline_watches_a_long_video_in_parts_and_cleans_up(tmp_path, settings):
    from fastapi.testclient import TestClient  # noqa: F401

    from app.db import Module, User
    from app.security import hash_password
    from app.web import create_app
    from tests.test_app_flow import GOOD, csrf_of, login
    app = create_app(replace(settings, embedded_worker=False, chunk_minutes=0.1))  # 6 s parts
    with app.state.sm() as db:
        db.add(User(email="operator@t.test", role="operator", password_hash=hash_password("Correct-Horse-9")))
        db.add(Module(code="BUSINESSFUNDAMENTALS", name="BF", learning_outcomes="x", reference_content="ref"))
        db.commit()
    clip = tmp_path / "c.mp4"
    _clip(clip, 20)
    op = login(app, "operator")
    r = op.post("/upload", data={"language": "Hindi", "csrf": csrf_of(op, "/upload")},
                files={"files": (GOOD, clip.read_bytes(), "video/mp4")})
    assert "Queued" in r.text
    from app.worker import WorkerPool
    pool = WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric)
    pool.run_until_empty()
    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        assert a and a.overall_score and a.session.jobs[0].status == "completed"
        extracts = db.scalars(select(ModelOutputRaw).where(ModelOutputRaw.stage == "extract")).all()
        assert len(extracts) >= 3, "one extraction per part"
        assert all(u.deleted_at for u in db.scalars(select(UploadedFile))), "every part's Files object released"
    assert not list((settings.data_dir / "tmp").glob("parts_*")), "part files removed"


@needs_ffmpeg
def test_retried_job_reuses_parts_already_analysed(tmp_path, settings):
    from fastapi.testclient import TestClient  # noqa: F401

    from app.db import Module, ProcessingJob, User
    from app.pipeline import retry_job
    from app.security import hash_password
    from app.web import create_app
    from app.worker import WorkerPool
    from tests.test_app_flow import GOOD, csrf_of, login
    app = create_app(replace(settings, embedded_worker=False, chunk_minutes=0.1))
    with app.state.sm() as db:
        db.add(User(email="operator@t.test", role="operator", password_hash=hash_password("Correct-Horse-9")))
        db.add(Module(code="BUSINESSFUNDAMENTALS", name="BF", learning_outcomes="x", reference_content="ref"))
        db.commit()
    clip = tmp_path / "c.mp4"
    _clip(clip, 20)
    op = login(app, "operator")
    op.post("/upload", data={"language": "Hindi", "csrf": csrf_of(op, "/upload")},
            files={"files": (GOOD, clip.read_bytes(), "video/mp4")})
    pool = WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric)
    calls = {"n": 0}
    real = pool.engine.extract

    def flaky(handle, prompt, ctx):
        calls["n"] += 1
        if calls["n"] == 3:  # the network drops on the third part
            raise RuntimeError("[Errno 65] No route to host")
        return real(handle, prompt, ctx)

    pool.engine.extract = flaky
    pool.run_until_empty()
    with app.state.sm() as db:
        job = db.scalar(select(ProcessingJob))
        assert job.status == "failed" and "No route" in job.error
        assert len(db.scalars(select(ModelOutputRaw).where(ModelOutputRaw.stage == "extract")).all()) == 2
        assert retry_job(db, job.session)
    pool.engine.extract = real
    calls["n"] = 0
    pool.run_until_empty()
    with app.state.sm() as db:
        assert db.scalar(select(ProcessingJob)).status == "completed" and db.scalar(select(Assessment))
