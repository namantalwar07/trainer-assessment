import copy

import pytest

from app.config import Settings
from app.rubric import load_rubric

WEIGHTS = {"C01": 20, "C02": 10, "C03": 15, "C04": 10, "C05": 15, "C06": 10, "C07": 5, "C08": 5,
           "C09": 5, "C10": 5}


@pytest.fixture
def rubric():
    return load_rubric()


@pytest.fixture
def settings(tmp_path):
    return Settings(mock_gemini=True, data_dir=tmp_path, session_secret="test-secret",
                    backoff_base_seconds=0.0, embedded_worker=False)


def make_payload(ratings: dict[str, int | None] | None = None) -> dict:
    """Fixed, valid example payload (video 75 min). Ratings default to 4 everywhere."""
    ratings = ratings or {}
    comps = []
    for cid, weight in WEIGHTS.items():
        rating = ratings.get(cid, 4)
        na = rating is None
        comps.append({
            "criterion_id": cid, "weight": weight, "rating": rating, "na": na,
            "evidence": [] if na else [
                {"timestamp": "00:10:00", "observation": "Explains with an example", "assessment": "Strong"}],
            "strengths": ["Clear"], "improvement": "More practice", "confidence": "High",
        })
    return {
        "video_id": "VID-0001", "trainer_id": "TR-001", "primary_language": "Hindi",
        "duration_minutes": 75, "competencies": comps, "top_strengths": ["Clarity"],
        "development_areas": ["Engagement"],
        "coaching_plan": [{"action": "Add knowledge checks", "competency": "C05", "priority": "High"}],
        "critical_flags": [],
        "timeline": [{"start": "00:00:00", "end": "01:15:00", "state": "teaching", "note": ""}],
        "participants": [{"name": "Ravi Kumar", "first_seen": "00:02:00", "source": "introduction",
                          "confidence": "High"}],
        "conduct_events": [],
        "clock_readings": [{"video_time": "00:00:00", "clock_time": "14:15"},
                           {"video_time": "01:15:00", "clock_time": "15:30"}],
        "integrity": {"clock_visible": True, "same_mentor_throughout": True, "same_participant_throughout": True,
                      "identity_summary": "The same mentor and participant appear throughout.", "edit_signs": [],
                      "freezes": []},
        "observation_note": "",
    }


@pytest.fixture
def payload():
    return make_payload()


@pytest.fixture
def clone():
    return copy.deepcopy
