"""Rubric loaded from config/rubric_vN.json. Weights and bands are data, not code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import ROOT


@dataclass(frozen=True)
class Criterion:
    id: str
    name: str
    weight: int
    primary_evidence: str
    description: str


@dataclass(frozen=True)
class Band:
    min: float
    label: str
    action: str


@dataclass(frozen=True)
class Rubric:
    version: str
    criteria: tuple[Criterion, ...]
    bands: tuple[Band, ...]  # sorted by min descending
    anchors: dict[str, str]
    raw: dict

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.criteria]

    @property
    def weights(self) -> dict[str, int]:
        return {c.id: c.weight for c in self.criteria}

    @property
    def total_weight(self) -> int:
        return sum(c.weight for c in self.criteria)

    def by_id(self, cid: str) -> Criterion:
        return next(c for c in self.criteria if c.id == cid)


def load_rubric(path: Path | None = None) -> Rubric:
    path = path or ROOT / "config" / "rubric_v1.json"
    raw = json.loads(path.read_text())
    criteria = tuple(Criterion(**c) for c in raw["criteria"])
    bands = tuple(sorted((Band(**b) for b in raw["bands"]), key=lambda b: -b.min))
    rubric = Rubric(raw["version"], criteria, bands, raw["anchors"], raw)
    if rubric.total_weight != 100:
        raise ValueError(f"Rubric weights must total 100, got {rubric.total_weight}")
    if len(set(rubric.ids)) != len(rubric.ids):
        raise ValueError("Duplicate criterion ids in rubric")
    return rubric
