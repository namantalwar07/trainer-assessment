"""End to end: absence gap, conduct events, mentee names, retry feedback and DB migration."""

import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from app.db import Assessment, make_session_factory
from app.gemini.mock import MockEngine
from app.worker import WorkerPool
from tests.test_app_flow import GOOD, PW, create_app, login, upload  # noqa: F401
from tests.test_app_flow import app as app  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs FFmpeg for a real 30 min clip")

_CLIP: dict[str, bytes] = {}


def clip_30min() -> bytes:
    if "c" not in _CLIP:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "c.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1800:size=96x72:rate=1",
                            "-f", "lavfi", "-i", "sine=frequency=300:duration=1800", "-shortest", "-c:v", "libx264",
                            "-preset", "ultrafast", "-c:a", "aac", "-b:a", "16k", str(out)], check=True)
            _CLIP["c"] = out.read_bytes()
    return _CLIP["c"]


class GapEngine(MockEngine):
    """Trainer leaves for 6 minutes and uses a phone; three mentees are named (one twice)."""

    def score(self, prompt, ctx):
        r = super().score(prompt, ctx)
        p = json.loads(r.text)
        p["timeline"] = [
            {"start": "00:00:00", "end": "00:10:00", "state": "teaching", "note": ""},
            {"start": "00:10:00", "end": "00:16:00", "state": "trainer_absent", "note": "left the room"},
            {"start": "00:16:00", "end": "00:19:00", "state": "pause", "note": "waiting"},
            {"start": "00:19:00", "end": "00:20:00", "state": "break", "note": ""},
            {"start": "00:20:00", "end": "00:30:00", "state": "teaching", "note": ""},
        ]
        p["participants"] = [
            {"name": "Anita Sharma", "first_seen": "00:01:00", "source": "introduction", "confidence": "High"},
            {"name": "anita  sharma", "first_seen": "00:21:00", "source": "spoken_in_session", "confidence": "Low"},
            {"name": "Mohd Irfan", "first_seen": "00:02:00", "source": "attendance", "confidence": "Medium"}]
        p["conduct_events"] = [
            {"timestamp": "00:05:00", "type": "phone_use", "duration_seconds": 240, "note": "on a call while class waits"}]
        r.text = json.dumps(p)
        return r


def test_absence_is_flagged_measured_and_routed_to_review(app):  # noqa: F811
    op = login(app, "operator")
    assert "Queued" in upload(op, data=clip_30min()).text
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric,
               engine=GapEngine(app.state.settings)).run_until_empty()
    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        ta = a.time_analysis
        assert ta["total_seconds"] == 1800
        # 6 + 3 + 1 min of consecutive non-teaching time = one 10 min gap, flagged and not counted
        assert len(ta["gaps"]) == 1 and ta["gaps"][0]["flagged"] and ta["gaps"][0]["seconds"] == 600
        assert ta["effective_seconds"] == 1200 and not ta["meets_threshold"]
        assert [p["name"] for p in a.participants] == ["Anita Sharma", "Mohd Irfan"]
        assert a.conduct_summary["count"] == 1
        reasons = " | ".join(a.review_reasons)
        assert "2.5 min or more" in reasons and "below the 60 min" in reasons and "conduct observation" in reasons
        assert a.human_review_required
        assert a.overall_score is not None  # conduct and time never change the competency score
        assert (app.state.settings.data_dir / a.report_path).stat().st_size > 3000
        aid = a.id
    rev = login(app, "reviewer")
    page = rev.get(f"/assessments/{aid}").text
    assert "Effective teaching time" in page and "Flagged" in page and "Anita Sharma" in page and "Phone use" in page
    assert "long pause" in rev.get("/sessions").text


def test_rejected_output_is_retried_with_the_errors_in_the_prompt(app):  # noqa: F811
    seen = []

    class Flaky(MockEngine):
        def score(self, prompt, ctx):
            seen.append(prompt)
            r = super().score(prompt, ctx)
            if len(seen) == 1:
                p = json.loads(r.text)
                p["timeline"] = [{"start": "00:00:00", "end": "00:00:01", "state": "teaching", "note": ""}]
                r.text = json.dumps(p)
            return r

    op = login(app, "operator")
    upload(op, data=clip_30min())
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric,
               engine=Flaky(app.state.settings)).run_until_empty()
    assert len(seen) == 2
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" not in seen[0]
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" in seen[1] and "describes only" in seen[1]


def test_old_database_gets_new_columns_automatically(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE assessments (id INTEGER PRIMARY KEY, job_id VARCHAR(36))")
    sm = make_session_factory(f"sqlite:///{db}")
    cols = {c["name"] for c in inspect(sm.kw["bind"]).get_columns("assessments")}
    assert {"time_analysis", "participants", "conduct_events", "conduct_summary"} <= cols


def test_config_rejects_tolerance_above_flag_threshold():
    from app.config import ConfigError, Settings

    with pytest.raises(ConfigError, match="GAP_TOLERANCE"):
        Settings.from_env({"GAP_TOLERANCE_SECONDS": "300", "GAP_FLAG_SECONDS": "180"})
    assert replace(Settings.from_env({}), min_effective_minutes=45).min_effective_minutes == 45
