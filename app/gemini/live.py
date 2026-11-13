"""Live engine on gemini-3.8-flash.

Stage 1 (video) uses the Interactions API with agentic processing and background=True, as documented on
4 Nov 2026. Field names below (status, output_text, usage.total_*) were checked against google-genai
2.34 types; the live behaviour itself still needs `make doctor` / spikes/test_interactions.py with a key.
If the Interactions call fails for a non-transient reason and EXTRACTION_FALLBACK=true, Stage 1 falls back
to a plain generate_content call on the same uploaded file; the path actually used is recorded per job.
Stage 2 is a text-only generate_content call with JSON output.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.config import Settings
from app.gemini.base import (
    AssessmentEngine,
    StageResult,
    TokenUsage,
    TransientError,
    VideoHandle,
)

log = logging.getLogger("app.gemini")

_FAILED = {"failed", "cancelled", "incomplete", "budget_exceeded", "requires_action"}
_TRANSIENT_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}


def _num(obj: object, *names: str) -> int:
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return int(v)
    return 0


def usage_from(obj: object) -> TokenUsage:
    """Works for Interactions (`usage.total_*`) and generate_content (`usage_metadata.*_token_count`)."""
    u = getattr(obj, "usage", None) or getattr(obj, "usage_metadata", None)
    if u is None:
        return TokenUsage()
    return TokenUsage(
        _num(u, "total_input_tokens", "prompt_token_count", "input_tokens"),
        _num(u, "total_output_tokens", "candidates_token_count", "output_tokens"),
        _num(u, "total_thought_tokens", "thoughts_token_count", "thinking_tokens")
        + _num(u, "total_tool_use_tokens", "tool_use_prompt_token_count"),
    )


def interaction_text(i: object) -> str:
    direct = getattr(i, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for step in getattr(i, "steps", None) or []:
        for c in getattr(step, "content", None) or []:
            t = c.get("text") if isinstance(c, dict) else getattr(c, "text", None)
            if isinstance(t, str) and t:
                parts.append(t)
    return "\n".join(parts)


def classify(exc: Exception) -> Exception:
    """Map SDK/network errors to TransientError (retry with backoff) or a clear fatal RuntimeError."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    msg = str(exc)[:400]
    local_fault = isinstance(exc, FileNotFoundError | PermissionError | IsADirectoryError | NotADirectoryError)
    if code in _TRANSIENT_HTTP or isinstance(exc, TimeoutError | ConnectionError) or (
            isinstance(exc, OSError) and not local_fault):  # e.g. [Errno 65] No route to host: the network dropped
        return TransientError(msg)
    if any(s in msg.lower() for s in ("timed out", "timeout", "connection", "temporarily", "unavailable")):
        return TransientError(msg)
    if code in (401, 403):
        return RuntimeError(f"Gemini API rejected the key or project (HTTP {code}): {msg}")
    if code == 404:
        return RuntimeError(f"Model or resource not found (HTTP 404): {msg}")
    return RuntimeError(msg)


def state_name(state: object) -> str:
    return str(getattr(state, "name", state) or "").upper()


class LiveEngine(AssessmentEngine):
    api_path = "interactions"

    def __init__(self, settings: Settings):
        from google import genai  # lazy: only needed in live mode

        self.settings = settings
        self.model = settings.model
        self.client = genai.Client(api_key=settings.gemini_api_key)  # key is never logged

    # ----- Files API -----
    def prepare_video(self, path: Path, mime_type: str) -> VideoHandle:
        try:
            f = self.client.files.upload(file=str(path), config={"mime_type": mime_type})
            deadline = time.time() + 1800
            while state_name(f.state) == "PROCESSING":
                if time.time() > deadline:
                    raise TransientError("Files API processing timed out")
                time.sleep(10)
                f = self.client.files.get(name=f.name)
        except TransientError:
            raise
        except Exception as e:
            raise classify(e) from e
        if state_name(f.state) != "ACTIVE":
            raise RuntimeError(f"Files API object {f.name} ended in state {state_name(f.state)}")
        return VideoHandle(f.name, f.uri, f.mime_type or mime_type, "ACTIVE")

    # ----- Stage 1 -----
    def extract(self, handle: VideoHandle, prompt: str, ctx: dict) -> StageResult:
        try:
            return self._extract_interactions(handle, prompt)
        except TransientError:
            raise
        except Exception as e:
            err = classify(e) if not isinstance(e, RuntimeError) else e
            if isinstance(err, TransientError):
                raise err from e
            if not self.settings.extraction_fallback or "rejected the key" in str(err):
                raise err from e
            log.warning("Interactions path failed (%s); falling back to generate_content", str(err)[:200])
            return self._extract_generate_content(handle, prompt)

    def _extract_interactions(self, handle: VideoHandle, prompt: str) -> StageResult:
        video: dict = {"type": "video", "uri": handle.uri, "mime_type": handle.mime_type,
                       "processing": "agentic"}
        if self.settings.video_resolution:
            video["resolution"] = self.settings.video_resolution
        try:
            inter = self.client.interactions.create(
                model=self.model, input=[video, {"type": "text", "text": prompt}], background=True)
            deadline = time.time() + self.settings.interaction_timeout_seconds
            while str(getattr(inter, "status", "")).lower() not in {"completed"}:
                status = str(getattr(inter, "status", "")).lower()
                if status in _FAILED:
                    errs = getattr(inter, "errors", None)
                    raise RuntimeError(f"Interaction ended with status '{status}'" + (f": {errs}" if errs else ""))
                if time.time() > deadline:
                    raise TransientError("Interaction polling timed out")
                time.sleep(self.settings.poll_interval_seconds)
                inter = self.client.interactions.get(inter.id)
        except (TransientError, RuntimeError):
            raise
        except Exception as e:
            raise classify(e) from e
        text = interaction_text(inter)
        if not text:
            raise RuntimeError("Interaction completed but returned no text")
        return StageResult(text, usage_from(inter), api_path="interactions")

    def _extract_generate_content(self, handle: VideoHandle, prompt: str) -> StageResult:
        from google.genai import types

        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_uri(file_uri=handle.uri, mime_type=handle.mime_type), prompt],
                config={"temperature": 0})
        except Exception as e:
            raise classify(e) from e
        if not (resp.text or "").strip():
            raise RuntimeError("generate_content returned no text for the video")
        return StageResult(resp.text, usage_from(resp), api_path="generate_content-fallback")

    # ----- Stage 2 -----
    def score(self, prompt: str, ctx: dict) -> StageResult:
        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0})
        except Exception as e:
            raise classify(e) from e
        return StageResult(resp.text or "", usage_from(resp), api_path="generate_content")

    def release(self, handle: VideoHandle) -> None:
        try:
            self.client.files.delete(name=handle.name)
        except Exception:  # already expired/deleted is fine; Google also removes files after 48h
            log.info("Files API delete skipped for %s", handle.name)

    # ----- diagnostics for `make doctor` -----
    def check_model(self) -> tuple[bool, str]:
        try:
            names = {getattr(m, "name", "").removeprefix("models/") for m in self.client.models.list()}
        except Exception as e:
            return False, f"Could not list models: {classify(e)}"
        if self.model in names:
            return True, f"{self.model} is available to this key"
        return False, f"{self.model} is NOT in this key's model list ({len(names)} models visible)"

    def ping(self) -> tuple[bool, str]:
        try:
            r = self.client.models.generate_content(model=self.model, contents="Reply with the single word OK.")
            return True, f"generate_content works ({(r.text or '').strip()[:20]!r})"
        except Exception as e:
            return False, str(classify(e))
