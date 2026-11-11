"""Engine interface: the model can be swapped without touching the pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    def as_dict(self) -> dict:
        return {"input": self.input_tokens, "output": self.output_tokens, "thinking": self.thinking_tokens}


@dataclass
class StageResult:
    text: str
    usage: TokenUsage
    api_path: str = ""


@dataclass
class VideoHandle:
    name: str  # Files API object name, e.g. files/abc123
    uri: str
    mime_type: str
    state: str = "ACTIVE"


class TransientError(RuntimeError):
    """Retry with backoff (rate limit, 5xx, network)."""


class AssessmentEngine(ABC):
    model: str
    api_path: str  # recorded on every job for cost reconciliation

    @abstractmethod
    def prepare_video(self, path: Path, mime_type: str) -> VideoHandle:
        """Upload to the provider file store and wait until usable."""

    @abstractmethod
    def extract(self, handle: VideoHandle, prompt: str, ctx: dict) -> StageResult:
        """Stage 1 (video): session transcript, interaction events and visual observations."""

    @abstractmethod
    def score(self, prompt: str, ctx: dict) -> StageResult:
        """Stage 2 (text only): JSON scorecard matching the schema. Cheap to retry."""

    @abstractmethod
    def release(self, handle: VideoHandle) -> None:
        """Delete the provider-side file. Called on every terminal job state."""
