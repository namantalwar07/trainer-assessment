"""Score calculation, N/A redistribution, bands and the backend human-review decision.

Nothing here reads model-computed totals or a model-set review flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.rubric import Rubric


@dataclass
class CompetencyResult:
    criterion_id: str
    name: str
    weight: int
    rating: int | None
    na: bool
    adjusted_weight: float
    points: float
    confidence: str
    improvement: str
    strengths: list[str]
    evidence: list[dict]

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "criterion_id", "name", "weight", "rating", "na", "adjusted_weight", "points",
            "confidence", "improvement", "strengths")}


@dataclass
class ScoreResult:
    competencies: list[CompetencyResult]
    overall_score: float | None
    band: str
    observed_weight: float
    total_weight: float
    coverage_text: str
    review_required: bool
    review_reasons: list[str] = field(default_factory=list)


def classify(score: float | None, rubric: Rubric, settings: Settings) -> str:
    if score is None:
        return "Not scorable"
    value = int(score + 0.5) if settings.band_rounding_rule == "round_half_up" else score
    for band in rubric.bands:  # sorted by min descending: explicit thresholds, no gaps
        if value >= band.min:
            return band.label
    return rubric.bands[-1].label


def compute_scores(payload: dict, rubric: Rubric, settings: Settings, *, validation_retries: int = 0,
                   notes: list[str] | None = None, has_reference: bool = True,
                   extra_reasons: list[str] | None = None) -> ScoreResult:
    """`payload` must already be schema-validated and normalised."""
    notes = notes or []
    by_id = {c["criterion_id"]: c for c in payload["competencies"]}
    total = float(rubric.total_weight)
    observed = float(sum(rubric.weights[cid] for cid, c in by_id.items() if not c["na"]))

    results: list[CompetencyResult] = []
    for crit in rubric.criteria:
        c = by_id[crit.id]
        if c["na"] or observed == 0:
            adj, pts = 0.0, 0.0
        else:
            adj = crit.weight * total / observed  # N/A weight redistributed proportionally
            pts = (c["rating"] / 5) * adj
        results.append(CompetencyResult(
            crit.id, crit.name, crit.weight, c["rating"], c["na"], round(adj, 4), round(pts, 4),
            c["confidence"], c["improvement"], list(c["strengths"]), list(c["evidence"])))

    overall = round(sum(r.points for r in results), 1) if observed > 0 else None
    band = classify(overall, rubric, settings)

    reasons: list[str] = []
    if payload["critical_flags"]:
        reasons.append(f"{len(payload['critical_flags'])} critical flag(s) raised")
    if observed < settings.min_coverage_points:
        reasons.append(f"Coverage {observed:g}/{total:g} points is below the {settings.min_coverage_points:g}"
                       " minimum: human review or re-recording needed")
    if not has_reference or any("C01 forced" in n for n in notes):
        reasons.append("C01 is N/A because no module reference content was supplied")
    low = [r.criterion_id for r in results
           if not r.na and r.confidence == "Low" and r.weight >= settings.low_conf_weight_threshold]
    if low:
        reasons.append(f"Low confidence on high-weight competencies: {', '.join(low)}")
    if validation_retries > 0:
        reasons.append(f"Model output needed {validation_retries} validation retr"
                       f"{'y' if validation_retries == 1 else 'ies'}")
    if observed == 0:
        reasons.append("No scorable competencies")
    reasons.extend(extra_reasons or [])  # effective-time and conduct reasons (never change the score)

    return ScoreResult(
        results, overall, band, observed, total,
        f"Score based on {observed:g} of {total:g} rubric points observed",
        bool(reasons), reasons)


def human_overall(ratings: dict[str, int | None], rubric: Rubric) -> float | None:
    """Same redistribution formula, applied to a reviewer's ratings (None = N/A)."""
    observed = sum(rubric.weights[c] for c, r in ratings.items() if r is not None)
    if not observed:
        return None
    total = rubric.total_weight
    return round(sum(r / 5 * rubric.weights[c] * total / observed for c, r in ratings.items()
                     if r is not None), 1)
