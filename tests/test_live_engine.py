"""LiveEngine logic against a fake SDK client (no network, no key)."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from app.gemini.base import TransientError, VideoHandle
from app.gemini.live import LiveEngine, classify, interaction_text, usage_from

H = VideoHandle("files/abc", "https://files/abc", "video/mp4")


class FakeInteractions:
    def __init__(self, statuses, final=None, create_exc=None):
        self.statuses, self.final, self.create_exc, self.calls = list(statuses), final, create_exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.create_exc:
            raise self.create_exc
        return NS(id="i1", status=self.statuses.pop(0), output_text=None, errors=None, usage=None)

    def get(self, id):  # noqa: A002
        st = self.statuses.pop(0)
        if st == "completed":
            return self.final
        return NS(id="i1", status=st, output_text=None, errors=["boom"] if st == "failed" else None, usage=None)


class FakeModels:
    def __init__(self, text="{}", exc=None):
        self.text, self.exc, self.calls = text, exc, []

    def generate_content(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return NS(text=self.text, usage_metadata=NS(prompt_token_count=10, candidates_token_count=5,
                                                    thoughts_token_count=2))


def engine(settings, interactions=None, models=None) -> LiveEngine:
    e = object.__new__(LiveEngine)
    e.settings = replace(settings, poll_interval_seconds=0.0, interaction_timeout_seconds=5.0)
    e.model = "gemini-3.8-flash"
    e.client = NS(interactions=interactions, models=models or FakeModels(), files=NS(delete=lambda name: None))
    return e


FINAL = NS(id="i1", status="completed", output_text='{"ok": true}', errors=None,
           usage=NS(total_input_tokens=450000, total_output_tokens=9000, total_thought_tokens=3000,
                    total_tool_use_tokens=500))


def test_agentic_background_call_shape_and_polling(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fi = FakeInteractions(["in_progress", "in_progress", "completed"], FINAL)
    r = engine(settings, fi).extract(H, "PROMPT", {})
    call = fi.calls[0]
    assert call["model"] == "gemini-3.8-flash" and call["background"] is True
    video, text = call["input"]
    assert video == {"type": "video", "uri": H.uri, "mime_type": "video/mp4", "processing": "agentic"}
    assert text == {"type": "text", "text": "PROMPT"}
    assert r.text == '{"ok": true}' and r.api_path == "interactions"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.thinking_tokens) == (450000, 9000, 3500)


def test_video_resolution_is_passed_when_configured(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fi = FakeInteractions(["in_progress", "completed"], FINAL)
    e = engine(settings, fi)
    e.settings = replace(e.settings, video_resolution="low")
    e.extract(H, "P", {})
    assert fi.calls[0]["input"][0]["resolution"] == "low"


def test_failed_interaction_falls_back_to_generate_content(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fi = FakeInteractions(["in_progress", "failed"])
    models = FakeModels(text="fallback transcript")
    monkeypatch.setattr("google.genai.types.Part.from_uri", lambda **kw: ("part", kw), raising=False)
    r = engine(settings, fi, models).extract(H, "P", {})
    assert r.text == "fallback transcript" and r.api_path == "generate_content-fallback"
    assert models.calls[0]["model"] == "gemini-3.8-flash"


def test_no_fallback_when_disabled(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    e = engine(settings, FakeInteractions(["in_progress", "failed"]))
    e.settings = replace(e.settings, extraction_fallback=False)
    with pytest.raises(RuntimeError, match="failed"):
        e.extract(H, "P", {})


def test_transient_error_is_retriable_not_a_fallback(settings):
    exc = type("E", (Exception,), {"code": 503})("overloaded")
    with pytest.raises(TransientError):
        engine(settings, FakeInteractions([], create_exc=exc)).extract(H, "P", {})


def test_bad_key_is_fatal_and_clear(settings):
    exc = type("E", (Exception,), {"code": 403})("PERMISSION_DENIED")
    with pytest.raises(RuntimeError, match="rejected the key"):
        engine(settings, FakeInteractions([], create_exc=exc)).extract(H, "P", {})


def test_score_stage_uses_json_mode_and_reads_usage(settings):
    models = FakeModels(text='{"a": 1}')
    r = engine(settings, None, models).score("PROMPT", {})
    cfg = models.calls[0]["config"]
    assert cfg["response_mime_type"] == "application/json" and cfg["temperature"] == 0
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.thinking_tokens) == (10, 5, 2)


def test_score_rate_limit_becomes_transient(settings):
    exc = type("E", (Exception,), {"code": 429})("quota")
    with pytest.raises(TransientError):
        engine(settings, None, FakeModels(exc=exc)).score("P", {})


def test_helpers():
    assert interaction_text(NS(output_text="hi", steps=None)) == "hi"
    assert interaction_text(NS(output_text=None, steps=[NS(content=[NS(text="a"), {"text": "b"}])])) == "a\nb"
    assert usage_from(NS(usage=None, usage_metadata=None)).input_tokens == 0
    assert isinstance(classify(Exception("read timed out")), TransientError)
    assert not isinstance(classify(Exception("bad request")), TransientError)


def test_release_never_raises(settings):
    e = engine(settings)
    e.client.files = NS(delete=lambda name: (_ for _ in ()).throw(RuntimeError("gone")))
    e.release(H)  # swallowed: object may already have expired


