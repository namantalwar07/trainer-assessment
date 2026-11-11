from __future__ import annotations

from app.config import Settings
from app.gemini.base import AssessmentEngine


def make_engine(settings: Settings) -> AssessmentEngine:
    if settings.mock_gemini:
        from app.gemini.mock import MockEngine

        return MockEngine(settings)
    from app.gemini.live import LiveEngine

    return LiveEngine(settings)
