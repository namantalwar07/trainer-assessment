"""Offline engine: deterministic fixture responses, no key, no network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.config import ROOT, Settings
from app.gemini.base import AssessmentEngine, StageResult, TokenUsage, VideoHandle
from app.timeutil import fmt_timestamp, parse_timestamp

FIXTURES = ROOT / "tests" / "fixtures"


_fmt = fmt_timestamp


def _demo_timeline(d: int, digest: bytes) -> list[dict]:
    """Dummy activity timeline scaled to the video; roughly 1 in 3 videos gets a 6 min absence (demo flag)."""
    gap = min(360, int(d * 0.08)) if digest[10] % 3 == 0 else 0
    g0 = int(d * 0.30)
    bounds = sorted({0, d, int(d * 0.45), int(d * 0.55), g0, g0 + gap})
    spans: list[dict] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        mid = (a + b) / 2
        if gap and g0 <= mid < g0 + gap:
            state, note = "trainer_absent", "DUMMY: trainer left the room"
        elif d * 0.45 <= mid < d * 0.55:
            state, note = "participant_activity", "DUMMY: group exercise"
        else:
            state, note = "teaching", ""
        if spans and spans[-1]["state"] == state:
            spans[-1]["end"] = _fmt(b)
        else:
            span = {"start": _fmt(a), "end": _fmt(b), "state": state, "note": note}
            if state == "trainer_absent":
                span.update(before="DUMMY: \u201cLet me check something, one minute.\u201d",
                            after="DUMMY: \u201cSorry, I am back. Where were we?\u201d")
            spans.append(span)
    return spans


class MockEngine(AssessmentEngine):
    api_path = "mock"

    def __init__(self, settings: Settings, fixtures: Path = FIXTURES):
        self.model = settings.model
        self.fixtures = fixtures
        self.deleted: list[str] = []

    def prepare_video(self, path: Path, mime_type: str) -> VideoHandle:
        digest = hashlib.sha256(path.name.encode()).hexdigest()[:12]
        return VideoHandle(name=f"files/mock-{digest}", uri=f"mock://files/{digest}", mime_type=mime_type)

    def extract(self, handle: VideoHandle, prompt: str, ctx: dict) -> StageResult:
        text = (self.fixtures / "mock_extraction.json").read_text()
        return StageResult(text, TokenUsage(int(ctx["duration_seconds"] * 100), 6000, 2000), "mock")

    def score(self, prompt: str, ctx: dict) -> StageResult:
        data = json.loads((self.fixtures / "mock_scoring.json").read_text())
        data.pop("_note", None)
        digest = hashlib.sha256(ctx["video_id"].encode()).digest()
        duration = int(ctx["duration_seconds"])
        limit = max(duration - 1, 0)

        def clamp_ts(ts: str) -> str:
            return _fmt(min(parse_timestamp(ts) or 0, limit))

        data.update(video_id=ctx["video_id"], trainer_id=ctx["trainer_id"],
                    primary_language=ctx["language"], duration_minutes=max(1, round(duration / 60)))
        for i, c in enumerate(data["competencies"]):
            c["rating"] = min(5, max(1, c["rating"] + (digest[i] % 3) - 1))  # deterministic spread
            for e in c["evidence"]:
                e["timestamp"] = clamp_ts(e["timestamp"])
            if c["criterion_id"] == "C01" and not ctx["has_reference"]:
                c.update(rating=None, na=True, evidence=[])
        data["timeline"] = _demo_timeline(duration, digest)
        start_clock = 14 * 3600 + 15 * 60 + digest[12] % 40 * 60
        data["clock_readings"] = [
            {"video_time": _fmt(t), "clock_time": f"{(start_clock + t) // 3600 % 24:02d}:{(start_clock + t) % 3600 // 60:02d}"}
            for t in sorted({0, duration // 2, duration})]
        data["integrity"] = {
            "clock_visible": True, "same_mentor_throughout": True, "same_participant_throughout": True,
            "identity_summary": "DUMMY: the same mentor and the same participant appear throughout the recording.",
            "edit_signs": [], "freezes": []}
        data["observation_note"] = ""
        data["mentor_name"] = "Dummy Mentor"
        data["participants"] = [
            {"name": n, "first_seen": _fmt(int(duration * f)), "source": src, "confidence": "High"}
            for n, f, src in (("Dummy Mentee One", 0.01, "introduction"), ("Dummy Mentee Two", 0.02, "introduction"),
                              ("Dummy Mentee Three", 0.15, "spoken_in_session"))]
        data["conduct_events"] = ([{"timestamp": _fmt(int(duration * 0.6)), "type": "phone_use",
                                    "duration_seconds": min(40, duration // 10), "note": "DUMMY: looks at phone"}]
                                  if digest[11] % 5 == 0 else [])
        return StageResult(json.dumps(data), TokenUsage(9000, 2500, 1500), "mock")

    def release(self, handle: VideoHandle) -> None:
        self.deleted.append(handle.name)
