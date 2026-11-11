"""Effective teaching time, gap flags and conduct summary, computed in code from the activity timeline.

The model only describes WHAT happened WHEN (state per time span). All arithmetic, tolerances, flags and the
60-minute check are done here, so they are reproducible and configurable.

States
  counted as effective : teaching, participant_activity (configurable), technical_issue (not the trainer's fault)
  non-effective ("gap"): break, trainer_absent, pause, off_task
Consecutive non-effective spans form ONE gap. A gap up to GAP_TOLERANCE_SECONDS is forgiven (counted as
effective); a gap of at least GAP_FLAG_SECONDS is flagged; in between it is not forgiven but not flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import Settings
from app.timeutil import fmt_timestamp, parse_timestamp

STATES = ["teaching", "participant_activity", "technical_issue", "break", "trainer_absent", "pause", "off_task",
          "trainer_silent"]
GAP_STATES = {"break", "trainer_absent", "pause", "off_task"}
CONDUCT_TYPES = ["phone_use", "left_session_area", "unrelated_conversation", "not_attending_to_participants",
                 "repeatedly_out_of_view", "eating_or_drinking", "other"]
PARTICIPANT_SOURCES = ["introduction", "attendance", "spoken_in_session", "on_screen_label"]

HOLE_FILL_SECONDS = 60  # small undescribed holes are bridged by the previous state
MIN_COVERAGE = 0.95  # the timeline must describe at least this much of the recording
STATE_COLORS = {
    "teaching": "#067647", "participant_activity": "#2b8a8a", "technical_issue": "#98a2b3", "break": "#d69e2e",
    "trainer_absent": "#b42318", "pause": "#dd6b20", "off_task": "#7b2cbf", "trainer_silent": "#e0a800",
}
CONDUCT_LABELS = {
    "phone_use": "Phone use", "left_session_area": "Left the session area",
    "unrelated_conversation": "Unrelated conversation", "not_attending_to_participants": "Not attending to participants",
    "repeatedly_out_of_view": "Repeatedly out of view", "eating_or_drinking": "Eating or drinking", "other": "Other",
}
STATE_LABELS = {
    "teaching": "Teaching", "participant_activity": "Participant activity (trainer present)",
    "technical_issue": "Technical issue", "break": "Break", "trainer_absent": "Trainer absent",
    "pause": "Pause / dead air", "off_task": "Off task", "trainer_silent": "Trainer silent",
}


@dataclass
class Span:
    start: int
    end: int
    state: str
    note: str = ""
    before: str = ""  # what was said or shown just before this span (models fill it for non-teaching spans)
    after: str = ""

    @property
    def seconds(self) -> int:
        return self.end - self.start


def _spans(segments: list[dict]) -> list[Span]:
    out = []
    for s in segments:
        a, b = parse_timestamp(s.get("start", "")), parse_timestamp(s.get("end", ""))
        if a is not None and b is not None:
            out.append(Span(a, b, s.get("state", "pause"), str(s.get("note", ""))[:300],
                            str(s.get("before", ""))[:400], str(s.get("after", ""))[:400]))
    return out


def normalise(segments: list[dict], duration: float) -> list[Span]:
    """Sort, clip to [0, duration], trim overlaps and bridge small holes so spans tile the recording."""
    dur = int(duration)
    spans = sorted((s for s in _spans(segments) if s.end > s.start), key=lambda s: (s.start, s.end))
    out: list[Span] = []
    for s in spans:
        s.start, s.end = max(0, s.start), min(dur, s.end)
        if out:
            s.start = max(s.start, out[-1].end)  # trim overlap with the previous span
        if s.end <= s.start:
            continue
        if out and s.start - out[-1].end > 0:
            hole = s.start - out[-1].end
            if hole <= HOLE_FILL_SECONDS:
                out[-1].end = s.start
            else:
                out.append(Span(out[-1].end, s.start, "pause", "not described in the analysis"))
        elif not out and s.start > 0:
            if s.start <= HOLE_FILL_SECONDS:
                s.start = 0
            else:
                out.append(Span(0, s.start, "pause", "not described in the analysis"))
        out.append(s)
    if out and dur - out[-1].end > 0:
        if dur - out[-1].end <= HOLE_FILL_SECONDS:
            out[-1].end = dur
        else:
            out.append(Span(out[-1].end, dur, "pause", "not described in the analysis"))
    return out


def check_timeline(segments: list[dict], duration: float, tolerance: int = 2) -> list[str]:
    """Validation errors for the model's timeline (empty list = acceptable)."""
    errors: list[str] = []
    dur = int(duration)
    described = 0
    prev_end = -1
    for i, s in enumerate(sorted(segments, key=lambda x: parse_timestamp(x.get("start", "")) or 0)):
        a, b = parse_timestamp(s.get("start", "")), parse_timestamp(s.get("end", ""))
        if a is None or b is None:
            errors.append(f"timeline[{i}]: unparseable start/end")
            continue
        if b <= a:
            errors.append(f"timeline[{i}]: end {s['end']} must be after start {s['start']}")
            continue
        if b > dur + tolerance:
            errors.append(f"timeline[{i}]: end {s['end']} is beyond the video duration ({dur}s)")
        described += max(0, min(b, dur) - max(a, prev_end, 0))
        prev_end = max(prev_end, b)
    if not errors and dur > 0 and described / dur < MIN_COVERAGE:
        errors.append(f"timeline describes only {described / dur:.0%} of the recording; it must cover the whole "
                      "recording from 00:00:00 to the end with no large gaps")
    return errors


@dataclass
class Gap:
    start: int
    end: int
    seconds: int
    states: list[str]
    notes: list[str]
    forgiven: bool
    flagged: bool
    before: str = ""
    after: str = ""
    measured_silence_seconds: int = 0

    def as_dict(self) -> dict:
        return {"start": fmt_timestamp(self.start), "end": fmt_timestamp(self.end), "start_seconds": self.start,
                "end_seconds": self.end, "seconds": self.seconds, "states": self.states, "notes": self.notes,
                "forgiven": self.forgiven, "flagged": self.flagged, "before": self.before, "after": self.after,
                "measured_silence_seconds": self.measured_silence_seconds}


@dataclass
class TimeAnalysis:
    total_seconds: int
    effective_seconds: int
    threshold_minutes: float
    meets_threshold: bool
    breakdown: dict[str, int]
    gaps: list[dict]
    flagged_gaps: int
    longest_gap_seconds: int
    technical_seconds: int
    spans: list[dict] = field(default_factory=list)
    silent_periods: list[dict] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    @property
    def effective_minutes(self) -> float:
        return round(self.effective_seconds / 60, 1)

    def as_dict(self) -> dict:
        return {
            "total_seconds": self.total_seconds, "effective_seconds": self.effective_seconds,
            "effective_minutes": self.effective_minutes, "threshold_minutes": self.threshold_minutes,
            "meets_threshold": self.meets_threshold, "breakdown": self.breakdown, "gaps": self.gaps,
            "flagged_gaps": self.flagged_gaps, "longest_gap_seconds": self.longest_gap_seconds,
            "technical_seconds": self.technical_seconds, "spans": self.spans,
            "silent_periods": self.silent_periods,
        }


def overlay_silence(spans: list[Span], silences: list[dict], minimum: float) -> list[Span]:
    """Measured silence of at least `minimum` seconds overrides a span the model called 'teaching'."""
    out = list(spans)
    for sil in silences:
        a, b = int(sil["start"]), int(sil["end"])
        if b - a < minimum:
            continue
        nxt: list[Span] = []
        for sp in out:
            if sp.state != "teaching" or sp.end <= a or sp.start >= b:
                nxt.append(sp)
                continue
            if sp.start < a:
                nxt.append(Span(sp.start, a, sp.state, sp.note, sp.before, ""))
            nxt.append(Span(max(sp.start, a), min(sp.end, b), "pause", "No audio detected (measured)",
                            sp.before if sp.start >= a else "", sp.after if sp.end <= b else ""))
            if sp.end > b:
                nxt.append(Span(b, sp.end, sp.state, sp.note, "", sp.after))
        out = nxt
    return out


def analyse(segments: list[dict], duration: float, settings: Settings,
            silences: list[dict] | None = None) -> TimeAnalysis:
    spans = normalise(segments, duration)
    if silences:
        spans = overlay_silence(spans, silences, settings.gap_flag_seconds)
    total = int(duration)
    breakdown = {st: 0 for st in STATES}
    for s in spans:
        breakdown[s.state] = breakdown.get(s.state, 0) + s.seconds

    effective_states = {"teaching"}
    if settings.count_participant_activity_as_effective:
        effective_states.add("participant_activity")

    gaps: list[Gap] = []
    effective = 0
    run: list[Span] = []
    silent_run: list[Span] = []
    silent_periods: list[dict] = []

    def close_silent() -> None:
        # Trainer present but saying nothing: never teaching time; from the flag length it is listed for a human.
        if not silent_run:
            return
        secs = sum(r.seconds for r in silent_run)
        if secs >= settings.silent_flag_seconds:
            silent_periods.append({
                "start": fmt_timestamp(silent_run[0].start), "end": fmt_timestamp(silent_run[-1].end),
                "start_seconds": silent_run[0].start, "end_seconds": silent_run[-1].end, "seconds": secs,
                "notes": [r.note for r in silent_run if r.note],
                "before": silent_run[0].before, "after": silent_run[-1].after})
        silent_run.clear()

    def close_run() -> None:
        nonlocal effective
        if not run:
            return
        secs = sum(r.seconds for r in run)
        forgiven = secs <= settings.gap_tolerance_seconds  # short enough not to be flagged; still not teaching time
        flagged = secs >= settings.gap_flag_seconds
        gs, ge = run[0].start, run[-1].end
        measured = sum(max(0, min(ge, int(x["end"])) - max(gs, int(x["start"]))) for x in silences or [])
        gaps.append(Gap(gs, ge, secs, sorted({r.state for r in run}), [r.note for r in run if r.note],
                        forgiven, flagged, run[0].before, run[-1].after, measured))
        run.clear()

    for s in spans:
        if s.state == "trainer_silent":
            close_run()
            silent_run.append(s)
            continue
        close_silent()
        if s.state in GAP_STATES:
            run.append(s)
            continue
        close_run()
        if s.state in effective_states:
            effective += s.seconds
        # participant_activity with the setting off is neither effective nor a gap (trainer was present)
    close_run()
    close_silent()

    flagged = [g for g in gaps if g.flagged]
    longest = max((g.seconds for g in gaps), default=0)
    meets = effective / 60 >= settings.min_effective_minutes
    reasons: list[str] = []
    if flagged:
        worst = max(flagged, key=lambda g: g.seconds)
        reasons.append(f"{len(flagged)} pause(s) of {settings.gap_flag_seconds / 60:g} min or more "
                       f"(longest {worst.seconds / 60:.1f} min at {fmt_timestamp(worst.start)}): trainer was "
                       f"not teaching ({', '.join(STATE_LABELS[s].lower() for s in worst.states)})")
    if silent_periods:
        worst_s = max(silent_periods, key=lambda x: x["seconds"])
        reasons.append(f"Trainer silent for {settings.silent_flag_seconds / 60:g} min or more on "
                       f"{len(silent_periods)} occasion(s) (longest {worst_s['seconds'] / 60:.1f} min at "
                       f"{worst_s['start']}); a human should verify")
    if not meets:
        reasons.append(f"Effective teaching time {effective / 60:.0f} min is below the "
                       f"{settings.min_effective_minutes:g} min expected")
    if breakdown["technical_issue"] / 60 >= settings.technical_review_minutes:
        reasons.append(f"{breakdown['technical_issue'] / 60:.0f} min of the recording is a technical problem "
                       "(not counted against the trainer); verify the recording quality")
    return TimeAnalysis(
        total_seconds=total, effective_seconds=effective, threshold_minutes=settings.min_effective_minutes,
        meets_threshold=meets, breakdown=breakdown, gaps=[g.as_dict() for g in gaps], flagged_gaps=len(flagged),
        longest_gap_seconds=longest, technical_seconds=breakdown["technical_issue"],
        spans=[{"start": s.start, "end": s.end, "state": s.state, "note": s.note} for s in spans],
        silent_periods=silent_periods, review_reasons=reasons)


def clean_participants(items: list[dict], limit: int = 200) -> list[dict]:
    """Trim, collapse whitespace, drop empties and case-insensitive duplicates; keep the earliest sighting."""
    seen: dict[str, dict] = {}
    for it in sorted(items, key=lambda x: parse_timestamp(x.get("first_seen", "")) or 0):
        name = re.sub(r"\s+", " ", str(it.get("name", "")).strip())[:100]
        key = name.casefold()
        if not name or key in seen:
            continue
        seen[key] = {"name": name, "first_seen": it.get("first_seen", "00:00:00"),
                     "source": it.get("source", "spoken_in_session"), "confidence": it.get("confidence", "Medium")}
    return list(seen.values())[:limit]


def conduct_summary(events: list[dict], settings: Settings) -> tuple[dict, list[str]]:
    """Aggregate observable conduct events. Review-only: never changes the score."""
    total = sum(int(e.get("duration_seconds", 0)) for e in events)
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    summary = {"count": len(events), "total_seconds": total, "by_type": by_type}
    reasons: list[str] = []
    long_ones = [e for e in events if int(e.get("duration_seconds", 0)) >= settings.gap_flag_seconds]
    if len(events) >= settings.conduct_review_events or long_ones:
        reasons.append(f"{len(events)} trainer conduct observation(s) totalling {total / 60:.1f} min "
                       "(for example phone use or leaving the session area); a human should verify before any follow-up")
    return summary, reasons
