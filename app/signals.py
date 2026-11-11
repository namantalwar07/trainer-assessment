"""Facts measured from the file itself with FFmpeg, independent of the AI.

Silence is measured from the audio, frozen picture from the video, and timestamp jumps and metadata from the
container. These are authoritative inputs for the integrity checks and gap detection, and are also shown to the
model as context. Every step is non-fatal: a failure is recorded, never raised.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from app.config import Settings

_EDITOR_TAGS = ("premiere", "davinci", "capcut", "final cut", "imovie", "camtasia", "filmora", "inshot",
                "vegas", "kinemaster", "vn video", "movavi", "shotcut", "openshot", "wondershare")


def _ffmpeg(args: list[str], timeout: int) -> str:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True,
                       timeout=timeout, check=False)
    return p.stderr


def parse_silence(stderr: str, duration: float) -> list[dict]:
    out: list[dict] = []
    start: float | None = None
    for line in stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            start = max(0.0, float(m[1]))
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            out.append({"start": round(start, 1), "end": round(min(float(m[1]), duration), 1)})
            start = None
    if start is not None:  # silent until the end of the recording
        out.append({"start": round(start, 1), "end": round(duration, 1)})
    return [s for s in out if s["end"] > s["start"]]


def parse_freeze(stderr: str, duration: float) -> list[dict]:
    out: list[dict] = []
    start: float | None = None
    for line in stderr.splitlines():
        if m := re.search(r"freeze_start: ([\d.]+)", line):
            start = float(m[1])
        elif (m := re.search(r"freeze_end: ([\d.]+)", line)) and start is not None:
            out.append({"start": round(start, 1), "end": round(float(m[1]), 1)})
            start = None
    if start is not None:
        out.append({"start": round(start, 1), "end": round(duration, 1)})
    return out


def measure_signals(path: Path, duration: float, settings: Settings) -> dict:
    sig: dict = {"silences": [], "freezes": [], "pts_jumps": [], "metadata": {}, "notes": [], "errors": []}
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sig["errors"].append("FFmpeg not installed: no measured signals")
        return sig
    try:
        err = _ffmpeg(["-i", str(path), "-vn", "-af",
                       f"silencedetect=noise={settings.silence_noise_db}dB:d={settings.silence_min_seconds}",
                       "-f", "null", "-"], timeout=1800)
        sig["silences"] = parse_silence(err, duration)
        if "Stream #0" in err and "Audio" not in err:
            sig["notes"].append("No audio stream found")
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"silence detection failed: {str(e)[:120]}")
    if settings.run_freeze_detect:
        try:
            err = _ffmpeg(["-i", str(path), "-an", "-vf",
                           f"fps=2,scale=160:-2,freezedetect=n=0.003:d={settings.freeze_min_seconds}",
                           "-f", "null", "-"], timeout=2400)
            sig["freezes"] = parse_freeze(err, duration)
        except Exception as e:  # noqa: BLE001
            sig["errors"].append(f"freeze detection failed: {str(e)[:120]}")
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=900, check=False)
        last = None
        for line in p.stdout.split():
            try:
                t = float(line.strip(","))
            except ValueError:
                continue
            if last is not None and t - last >= settings.pts_jump_seconds:
                sig["pts_jumps"].append({"at": round(last, 1), "jump_seconds": round(t - last, 1)})
            last = t if last is None or t > last else last
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"timestamp check failed: {str(e)[:120]}")
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                            str(path)], capture_output=True, text=True, timeout=120, check=False)
        data = json.loads(p.stdout or "{}")
        tags = {k.lower(): str(v) for k, v in (data.get("format", {}).get("tags") or {}).items()}
        enc = " ".join([tags.get("encoder", ""), tags.get("software", ""), tags.get("handler_name", "")]).lower()
        sig["metadata"] = {k: tags[k] for k in ("creation_time", "encoder", "major_brand", "title") if k in tags}
        if any(e in enc for e in _EDITOR_TAGS):
            sig["notes"].append("File metadata names a video editing tool: " + enc.strip()[:80])
            sig["editor_tool"] = True
        durs = {s.get("codec_type"): float(s["duration"]) for s in data.get("streams", []) if s.get("duration")}
        if "audio" in durs and "video" in durs and abs(durs["audio"] - durs["video"]) > 3:
            sig["notes"].append(f"Audio and video lengths differ by {abs(durs['audio'] - durs['video']):.0f} s")
            sig["av_mismatch_seconds"] = round(abs(durs["audio"] - durs["video"]), 1)
    except Exception as e:  # noqa: BLE001
        sig["errors"].append(f"metadata check failed: {str(e)[:120]}")
    return sig


def signals_for_prompt(sig: dict) -> str:
    """Compact text for the model: authoritative measured facts it should explain, not re-measure."""
    from app.timeutil import fmt_timestamp as f

    lines = []
    if sig.get("silences"):
        lines.append("Silent audio (no sound above the threshold) measured at: " + "; ".join(
            f"{f(s['start'])}-{f(s['end'])}" for s in sig["silences"][:30]))
    else:
        lines.append("No long silent stretches were measured in the audio.")
    if sig.get("freezes"):
        lines.append("Frozen picture measured at: " + "; ".join(
            f"{f(s['start'])}-{f(s['end'])}" for s in sig["freezes"][:30]))
    if sig.get("pts_jumps"):
        lines.append("Video timestamp jumps measured after: " + "; ".join(
            f"{f(j['at'])} (+{j['jump_seconds']:.0f}s)" for j in sig["pts_jumps"][:20]))
    for n in sig.get("notes", []):
        lines.append(n)
    return "\n".join(lines)


def long_silences(sig: dict, minimum: float) -> list[dict]:
    return [s for s in sig.get("silences", []) if s["end"] - s["start"] >= minimum]
