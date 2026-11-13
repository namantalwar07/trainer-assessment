"""Versioned prompt files + prompt assembly."""

from __future__ import annotations

from app.config import ROOT, Settings
from app.rubric import Rubric
from app.schema import schema_text

EXTRACT_VERSION = "extract_v3"
SCORE_VERSION = "master_v3"
PROMPT_VERSION = f"{SCORE_VERSION}+{EXTRACT_VERSION}"


def _load(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.txt").read_text()


def metadata_block(ctx: dict) -> str:
    lines = [
        f"Video ID: {ctx['video_id']}", f"Trainer ID: {ctx['trainer_id']}", f"Module: {ctx['module']}",
        f"Primary language: {ctx['language']}", f"Duration: {ctx['duration_seconds'] / 60:.0f} minutes "
        f"({int(ctx['duration_seconds'])} seconds)", f"State: {ctx['state']}",
    ]
    if ctx.get("part_note"):
        lines += ["", "PART OF A LONGER RECORDING: " + ctx["part_note"]]
    if ctx.get("signals_text"):
        lines += ["", "MEASURED SIGNALS (measured by software from the file; treat as facts and explain them):",
                  ctx["signals_text"]]
    if ctx["has_reference"]:
        lines += ["", "EXPECTED LEARNING OUTCOMES:", ctx["learning_outcomes"] or "(not supplied)", "",
                  "MODULE REFERENCE CONTENT (ground truth for subject accuracy):", ctx["reference_content"]]
    else:
        lines += ["", "NO MODULE REFERENCE CONTENT SUPPLIED: C01 must be N/A."]
    return "\n".join(lines)


def build_extract_prompt(ctx: dict) -> str:
    return _load(EXTRACT_VERSION).replace("{{METADATA}}", metadata_block(ctx))


def build_score_prompt(ctx: dict, rubric: Rubric, settings: Settings, extraction: str,
                       feedback: list[str] | None = None) -> str:
    rubric_text = "\n".join(
        f"{c.id} | {c.name} | weight {c.weight} | primary evidence: {c.primary_evidence} | {c.description}"
        for c in rubric.criteria)
    anchors = "\n".join(f"{k} = {v}" for k, v in rubric.anchors.items()
                        if k != "0" or settings.zero_anchor)
    prompt = (_load(SCORE_VERSION)
            .replace("{{RUBRIC}}", rubric_text)
            .replace("{{ANCHORS}}", anchors)
            .replace("{{METADATA}}", metadata_block(ctx))
            .replace("{{SCHEMA}}", schema_text(rubric, settings))
            .replace("{{EXTRACTION}}", extraction))
    if feedback:  # retry after a rejected output: tell the model exactly what to fix
        prompt += ("\n\nYOUR PREVIOUS ANSWER WAS REJECTED for these reasons. Fix every one and return the complete "
                   "corrected JSON object:\n" + "\n".join(f"- {e}" for e in feedback[:15]) + "\n")
    return prompt
