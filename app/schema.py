"""JSON Schema (draft 2020-12) for model output, plus the backend checks the schema cannot express."""

from __future__ import annotations

import copy
import json
from collections import Counter

from jsonschema import Draft202012Validator

from app.config import Settings
from app.rubric import Rubric
from app.timeline import CONDUCT_TYPES, PARTICIPANT_SOURCES, STATES, check_timeline
from app.timeutil import parse_clock, parse_timestamp  # noqa: F401  (re-exported)

DURATION_TOLERANCE_S = 2


TS = {"type": "string", "pattern": r"^\d{2}:\d{2}:\d{2}$"}
CLOCK = {"type": "string", "pattern": r"^\d{1,2}:\d{2}(:\d{2})?$"}
EDIT_TYPES = ["jump_cut", "clock_jump", "background_change", "audio_discontinuity", "repeated_footage", "other"]
FREEZE_CAUSES = ["cameras_off", "network", "unknown"]


def build_schema(rubric: Rubric, zero_anchor: bool = False) -> dict:
    ev = {
        "type": "object",
        "required": ["timestamp", "observation", "assessment"],
        "properties": {
            "timestamp": {"type": "string", "pattern": r"^\d{2}:\d{2}:\d{2}$"},
            "observation": {"type": "string", "minLength": 1},
            "assessment": {"type": "string"},
        },
    }
    comp = {
        "type": "object",
        "required": ["criterion_id", "weight", "rating", "na", "evidence", "strengths", "improvement",
                     "confidence"],
        "properties": {
            "criterion_id": {"enum": rubric.ids},
            "weight": {"type": "integer", "minimum": 1, "maximum": 100},
            "rating": {"type": ["integer", "null"], "minimum": 0 if zero_anchor else 1, "maximum": 5},
            "na": {"type": "boolean"},
            "evidence": {"type": "array", "items": ev},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "improvement": {"type": "string"},
            "confidence": {"enum": ["High", "Medium", "Low"]},
        },
    }
    n = len(rubric.criteria)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["video_id", "trainer_id", "primary_language", "duration_minutes", "competencies",
                     "top_strengths", "development_areas", "coaching_plan", "critical_flags", "timeline",
                     "participants", "conduct_events", "clock_readings", "integrity", "observation_note"],
        "properties": {
            "video_id": {"type": "string"},
            "trainer_id": {"type": "string"},
            "primary_language": {"type": "string"},
            "duration_minutes": {"type": "integer", "minimum": 1},
            "competencies": {"type": "array", "minItems": n, "maxItems": n, "items": comp},
            "top_strengths": {"type": "array", "items": {"type": "string"}},
            "development_areas": {"type": "array", "items": {"type": "string"}},
            "coaching_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action", "competency", "priority"],
                    "properties": {
                        "action": {"type": "string"}, "competency": {"type": "string"},
                        "priority": {"enum": ["High", "Medium", "Low"]},
                    },
                },
            },
            "timeline": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object", "required": ["start", "end", "state"],
                    "properties": {"start": TS, "end": TS, "state": {"enum": STATES}, "note": {"type": "string"},
                                   "before": {"type": "string"}, "after": {"type": "string"}},
                },
            },
            "mentor_name": {"type": "string", "maxLength": 100},
            "participants": {
                "type": "array",
                "items": {
                    "type": "object", "required": ["name", "first_seen", "source", "confidence"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 100}, "first_seen": TS,
                        "source": {"enum": PARTICIPANT_SOURCES}, "confidence": {"enum": ["High", "Medium", "Low"]},
                    },
                },
            },
            "conduct_events": {
                "type": "array",
                "items": {
                    "type": "object", "required": ["timestamp", "type", "duration_seconds", "note"],
                    "properties": {
                        "timestamp": TS, "type": {"enum": CONDUCT_TYPES},
                        "duration_seconds": {"type": "integer", "minimum": 0, "maximum": 14400},
                        "note": {"type": "string"},
                    },
                },
            },
            "clock_readings": {
                "type": "array",
                "items": {"type": "object", "required": ["video_time", "clock_time"],
                          "properties": {"video_time": TS, "clock_time": CLOCK}},
            },
            "integrity": {
                "type": "object",
                "required": ["clock_visible", "same_mentor_throughout", "same_participant_throughout",
                             "identity_summary", "edit_signs", "freezes"],
                "properties": {
                    "clock_visible": {"type": "boolean"},
                    "same_mentor_throughout": {"type": ["boolean", "null"]},
                    "same_participant_throughout": {"type": ["boolean", "null"]},
                    "identity_summary": {"type": "string"},
                    "edit_signs": {"type": "array", "items": {
                        "type": "object", "required": ["timestamp", "type", "description"],
                        "properties": {"timestamp": TS, "type": {"enum": EDIT_TYPES}, "description": {"type": "string"}}}},
                    "freezes": {"type": "array", "items": {
                        "type": "object", "required": ["timestamp", "duration_seconds", "likely_cause", "description"],
                        "properties": {"timestamp": TS, "duration_seconds": {"type": "integer", "minimum": 0},
                                       "likely_cause": {"enum": FREEZE_CAUSES}, "description": {"type": "string"}}}},
                },
            },
            "observation_note": {"type": "string", "maxLength": 600},
            "critical_flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["timestamp", "description"],
                    "properties": {"timestamp": {"type": "string"}, "description": {"type": "string"}},
                },
            },
        },
    }


def schema_text(rubric: Rubric, settings: Settings) -> str:
    return json.dumps(build_schema(rubric, settings.zero_anchor), indent=2)


def normalize_payload(payload: dict, rubric: Rubric, has_reference: bool) -> tuple[dict, list[str]]:
    """Apply backend overrides that must hold regardless of what the model said.

    Returns (payload copy, notes). C01 is forced to N/A when no module reference is supplied: the
    model must never invent an accuracy judgment without ground truth.
    """
    out = copy.deepcopy(payload)
    notes: list[str] = []
    if not has_reference:
        for c in out.get("competencies", []):
            if isinstance(c, dict) and c.get("criterion_id") == "C01" and not c.get("na"):
                c["na"], c["rating"] = True, None
                notes.append("C01 forced to N/A: no module reference content supplied")
    return out, notes


def validate_payload(payload: object, rubric: Rubric, settings: Settings,
                     duration_seconds: float) -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    schema = build_schema(rubric, settings.zero_anchor)
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
              for e in sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path))]
    if errors:
        return errors[:20]

    comps = payload["competencies"]  # type: ignore[index]
    counts = Counter(c["criterion_id"] for c in comps)
    for cid in rubric.ids:
        if counts[cid] == 0:
            errors.append(f"Missing criterion {cid}")
        elif counts[cid] > 1:
            errors.append(f"Duplicate criterion {cid} ({counts[cid]}x)")
    limit = duration_seconds + DURATION_TOLERANCE_S
    weights = rubric.weights
    for c in comps:
        cid = c["criterion_id"]
        if cid in weights and c["weight"] != weights[cid]:
            errors.append(f"{cid}: weight {c['weight']} does not match rubric weight {weights[cid]}")
        if (c["rating"] is None) != c["na"]:
            errors.append(f"{cid}: rating must be null if and only if na is true")
        if not c["na"] and not c["evidence"]:
            errors.append(f"{cid}: rated competency has no timestamped evidence")
        for e in c["evidence"]:
            secs = parse_timestamp(e["timestamp"])
            if secs is None:
                errors.append(f"{cid}: unparseable timestamp {e['timestamp']!r}")
            elif secs > limit:
                errors.append(f"{cid}: timestamp {e['timestamp']} is beyond video duration "
                              f"({int(duration_seconds)}s)")
    errors += check_timeline(payload["timeline"], duration_seconds)
    for r in payload["clock_readings"]:
        t, c = parse_timestamp(r["video_time"]), parse_clock(r["clock_time"])
        if t is None or t > limit:
            errors.append(f"clock reading at {r['video_time']} is beyond the video duration")
        if c is None:
            errors.append(f"clock reading {r['clock_time']!r} is not a valid 24-hour time (HH:MM)")
    for e in payload["integrity"]["edit_signs"] + payload["integrity"]["freezes"]:
        t = parse_timestamp(e["timestamp"])
        if t is None or t > limit:
            errors.append(f"integrity item at {e['timestamp']} is invalid or beyond the video duration")
    for p in payload["participants"]:
        t = parse_timestamp(p["first_seen"])
        if t is None or t > limit:
            errors.append(f"participant {p['name']!r}: first_seen {p['first_seen']} is invalid or beyond the video")
    for e in payload["conduct_events"]:
        t = parse_timestamp(e["timestamp"])
        if t is None or t > limit:
            errors.append(f"conduct event at {e['timestamp']} is invalid or beyond the video duration")
    return errors
