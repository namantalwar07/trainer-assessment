"""Very long recordings are watched in parts: Gemini places events accurately on a 60-minute video and drifts on a
130-minute one (it once put a trainer's exit 40 minutes early). Parts are cut at keyframes without re-encoding, each is
analysed on its own, and every timestamp is shifted back onto the full recording before the parts are merged."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.timeutil import fmt_timestamp, parse_timestamp

_HMS = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")


@dataclass(frozen=True)
class Chunk:
    path: Path
    offset: float
    duration: float


def plan_parts(duration: float, chunk_seconds: float) -> int:
    """How many roughly equal parts a recording needs (1 = analyse whole)."""
    if chunk_seconds <= 0 or duration <= chunk_seconds * 1.15:  # a slightly long video is not worth splitting
        return 1
    return -(-int(duration) // int(chunk_seconds))


def _probe_duration(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def split_video(src: Path, workdir: Path, duration: float, chunk_seconds: float) -> list[Chunk]:
    """Cut `src` into equal-ish parts at keyframes (stream copy). Offsets come from the real part durations."""
    n = plan_parts(duration, chunk_seconds)
    if n <= 1:
        return [Chunk(src, 0.0, duration)]
    workdir.mkdir(parents=True, exist_ok=True)
    target = duration / n
    pattern = workdir / f"part%02d{src.suffix.lower()}"
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
         "-f", "segment", "-segment_time", str(int(target)), "-reset_timestamps", "1", str(pattern)],
        capture_output=True, text=True, timeout=1800)
    files = sorted(workdir.glob(f"part*{src.suffix.lower()}"))
    if p.returncode != 0 or len(files) < 2:
        raise RuntimeError("Could not split the long recording into parts: " + (p.stderr or "no output")[-200:])
    chunks, offset = [], 0.0
    for f in files:
        d = _probe_duration(f)
        if d <= 0:
            raise RuntimeError(f"Part {f.name} has no readable duration")
        chunks.append(Chunk(f, offset, d))
        offset += d
    if abs(offset - duration) > max(30.0, duration * 0.02):  # parts must add up to the whole recording
        raise RuntimeError(f"Parts add up to {offset:.0f}s but the recording is {duration:.0f}s")
    return chunks


def slice_signals(sig: dict, start: float, end: float) -> dict:
    """The measured signals that fall inside one part, with times relative to that part's start."""
    def clip(items: list[dict]) -> list[dict]:
        out = []
        for s in items:
            a, b = max(s["start"], start), min(s["end"], end)
            if b > a:
                out.append({**s, "start": a - start, "end": b - start})
        return out

    return {**sig, "silences": clip(sig.get("silences", [])), "freezes": clip(sig.get("freezes", [])),
            "pts_jumps": [{**j, "at": j["at"] - start} for j in sig.get("pts_jumps", []) if start <= j["at"] <= end]}


def part_note(i: int, n: int, start: float, end: float) -> str:
    return (f"This recording is PART {i} of {n} of one longer session (it covers {fmt_timestamp(start)} to "
            f"{fmt_timestamp(end)} of the whole recording). Treat this video as starting at 00:00:00 and write every "
            f"timestamp relative to the start of THIS video; they are converted afterwards. Earlier or later parts are "
            f"analysed separately, so describe only what is in this video. If the trainer's or mentees' names are not "
            f"given in this part, leave them out.")


def shift(obj, offset: float):
    """Add `offset` seconds to every HH:MM:SS timestamp in a parsed JSON structure (keys named clock_time excluded)."""
    if isinstance(obj, dict):
        return {k: (v if k == "clock_time" else shift(v, offset)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shift(v, offset) for v in obj]
    if isinstance(obj, str):
        def bump(m: re.Match) -> str:
            t = parse_timestamp(m.group(0))
            return fmt_timestamp(t + offset) if t is not None else m.group(0)
        return _HMS.sub(bump, obj)
    return obj


def _merge(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return a + b
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: (_merge(a[k], b[k]) if k in a and k in b else a.get(k, b.get(k))) for k in {**a, **b}}
    if isinstance(a, bool) and isinstance(b, bool):
        return a and b
    if isinstance(a, str) and isinstance(b, str):
        return a if (not b or a == b) else (b if not a else f"{a} {b}")
    return a if a not in (None, "", [], {}) else b


def merge_extractions(parts: list[tuple[str, float]]) -> str:
    """Combine per-part extraction JSON (text, offset seconds) into one document on the full-recording clock."""
    merged: dict | None = None
    for text, offset in parts:
        d = shift(json.loads(_strip(text)), offset)
        merged = d if merged is None else _merge(merged, d)
    return json.dumps(merged, ensure_ascii=False)


def _strip(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    return t
