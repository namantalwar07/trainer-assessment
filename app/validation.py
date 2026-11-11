"""Upload checks (magic bytes) and FFmpeg-based video validation."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings

_NON_VIDEO_BRANDS = {b"M4A ", b"M4B ", b"heic", b"heix", b"mif1", b"avif", b"crx "}


def check_magic(head: bytes) -> str | None:
    """Return an error string, or None when the bytes look like an MP4/MOV (ISO BMFF 'ftyp')."""
    if len(head) < 12 or head[4:8] != b"ftyp":
        return "File content is not a real MP4/MOV (missing 'ftyp' signature)"
    if head[8:12] in _NON_VIDEO_BRANDS:
        return "File is an ISO media file but not a video container"
    return None


@dataclass
class ValidationResult:
    ok: bool
    duration_seconds: float | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "duration_seconds": self.duration_seconds, "errors": self.errors,
                "warnings": self.warnings, "info": self.info}


def ffmpeg_available() -> bool:
    return shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None


def _friendly(stderr: str, path: Path) -> str:
    """Operator-readable reason; never includes server paths."""
    text = stderr.replace(str(path), "<file>").strip()
    low = text.lower()
    if "moov atom not found" in low:
        return "The file looks incomplete or corrupted (the video index is missing). Re-export or re-copy the recording and upload again."
    if "invalid data found" in low or "error while decoding" in low or "corrupt" in low:
        return "The file contains damaged video or audio data. Re-export the recording and upload again."
    return "The file could not be read as a video: " + text[:160]


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def validate_video(path: Path, settings: Settings) -> ValidationResult:
    res = ValidationResult(ok=True)
    if not ffmpeg_available():
        if settings.mock_gemini:
            res.duration_seconds = settings.mock_default_duration_min * 60
            res.warnings.append("FFmpeg not installed: validation skipped (mock mode only)")
            res.info["skipped"] = True
            return res
        res.ok = False
        res.errors.append("FFmpeg/ffprobe not installed; cannot validate in live mode")
        return res

    p = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
              str(path)], timeout=120)
    if p.returncode != 0:
        res.ok = False
        res.errors.append(_friendly(p.stderr, path))
        return res
    data = json.loads(p.stdout or "{}")
    fmt, streams = data.get("format", {}), data.get("streams", [])
    names = fmt.get("format_name", "")
    if "mp4" not in names and "mov" not in names:
        res.errors.append(f"Unsupported container: {names or 'unknown'}")
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        res.errors.append("No video stream")
    if not audio:
        res.errors.append("No audio track")
    try:
        res.duration_seconds = float(fmt.get("duration"))
    except (TypeError, ValueError):
        res.errors.append("Could not determine duration")
    res.info = {
        "container": names,
        "video_codec": video[0].get("codec_name") if video else None,
        "audio_codec": audio[0].get("codec_name") if audio else None,
        "width": video[0].get("width") if video else None,
        "height": video[0].get("height") if video else None,
        "size_bytes": int(fmt.get("size", 0) or 0),
    }
    if res.duration_seconds:
        minutes = res.duration_seconds / 60
        if minutes < settings.duration_warn_low or minutes > settings.duration_warn_high:
            res.warnings.append(f"Duration {minutes:.0f} min is outside {settings.duration_warn_low}-"
                                f"{settings.duration_warn_high} min")
        elif minutes < settings.duration_ok_min or minutes > settings.duration_ok_max:
            res.warnings.append(f"Duration {minutes:.0f} min is outside the expected "
                                f"{settings.duration_ok_min}-{settings.duration_ok_max} min")
    if not res.errors:
        # Integrity: decode the first and last 20 s (a full decode of 90 min is too slow).
        for label, args in (("start", ["-t", "20"]), ("end", ["-sseof", "-20"])):
            d = _run(["ffmpeg", "-v", "error", "-xerror", *args, "-i", str(path), "-f", "null", "-"])
            if d.returncode != 0 or d.stderr.strip():
                res.errors.append(f"Part of the recording ({label}) could not be decoded. " + _friendly(d.stderr, path))
    res.ok = not res.errors
    return res
