from dataclasses import replace

from app.schema import normalize_payload, parse_timestamp, validate_payload
from tests.conftest import make_payload

DURATION = 75 * 60


def errs(payload, rubric, settings, duration=DURATION):
    return validate_payload(payload, rubric, settings, duration)


def test_valid_payload_passes(rubric, settings):
    assert errs(make_payload(), rubric, settings) == []


def test_missing_required_field(rubric, settings):
    p = make_payload()
    del p["critical_flags"]
    assert any("critical_flags" in e for e in errs(p, rubric, settings))


def test_missing_criterion_is_rejected(rubric, settings):
    p = make_payload()
    p["competencies"].pop()  # 9 items -> schema minItems fails
    assert errs(p, rubric, settings)


def test_duplicate_criterion_and_missing_id(rubric, settings):
    p = make_payload()
    p["competencies"][9]["criterion_id"] = "C01"  # 10 items, C01 twice, C10 missing
    e = errs(p, rubric, settings)
    assert any("Duplicate criterion C01" in x for x in e)
    assert any("Missing criterion C10" in x for x in e)


def test_unknown_criterion_id(rubric, settings):
    p = make_payload()
    p["competencies"][0]["criterion_id"] = "C99"
    assert errs(p, rubric, settings)


def test_weight_must_match_rubric(rubric, settings):
    p = make_payload()
    p["competencies"][0]["weight"] = 25
    assert any("does not match rubric weight" in x for x in errs(p, rubric, settings))


def test_rating_out_of_range(rubric, settings):
    for bad in (0, 6, -1):
        p = make_payload()
        p["competencies"][1]["rating"] = bad
        assert errs(p, rubric, settings), bad


def test_rating_zero_allowed_only_in_zero_anchor_mode(rubric, settings):
    p = make_payload({"C02": 0})
    assert errs(p, rubric, settings)
    assert errs(p, rubric, replace(settings, score_floor_mode="zero_anchor")) == []


def test_rating_null_iff_na(rubric, settings):
    p = make_payload()
    p["competencies"][2]["rating"] = None  # na still false
    assert any("null if and only if" in x for x in errs(p, rubric, settings))
    p = make_payload()
    p["competencies"][2]["na"] = True  # rating still 4
    assert any("null if and only if" in x for x in errs(p, rubric, settings))


def test_rated_competency_needs_evidence(rubric, settings):
    p = make_payload()
    p["competencies"][3]["evidence"] = []
    assert any("no timestamped evidence" in x for x in errs(p, rubric, settings))


def test_na_competency_needs_no_evidence(rubric, settings):
    assert errs(make_payload({"C01": None}), rubric, settings) == []


def test_timestamp_beyond_duration(rubric, settings):
    p = make_payload()
    p["competencies"][0]["evidence"][0]["timestamp"] = "01:30:00"
    assert any("beyond video duration" in x for x in errs(p, rubric, settings))


def test_timestamp_at_duration_edge_is_accepted(rubric, settings):
    p = make_payload()
    p["competencies"][0]["evidence"][0]["timestamp"] = "01:15:00"
    assert errs(p, rubric, settings) == []


def test_timestamp_bad_format_and_minutes(rubric, settings):
    p = make_payload()
    p["competencies"][0]["evidence"][0]["timestamp"] = "10:00"
    assert errs(p, rubric, settings)
    p["competencies"][0]["evidence"][0]["timestamp"] = "00:75:00"
    assert any("unparseable" in x for x in errs(p, rubric, settings))


def test_empty_observation_rejected(rubric, settings):
    p = make_payload()
    p["competencies"][0]["evidence"][0]["observation"] = ""
    assert errs(p, rubric, settings)


def test_bad_confidence_and_priority(rubric, settings):
    p = make_payload()
    p["competencies"][0]["confidence"] = "Certain"
    assert errs(p, rubric, settings)
    p = make_payload()
    p["coaching_plan"][0]["priority"] = "Urgent"
    assert errs(p, rubric, settings)


def test_parse_timestamp():
    assert parse_timestamp("01:02:03") == 3723
    assert parse_timestamp("00:60:00") is None
    assert parse_timestamp("abc") is None


def test_c01_forced_na_without_reference(rubric, settings):
    p = make_payload()  # model rated C01 = 4 despite no reference
    fixed, notes = normalize_payload(p, rubric, has_reference=False)
    c01 = fixed["competencies"][0]
    assert c01["na"] is True and c01["rating"] is None
    assert notes and "C01 forced" in notes[0]
    assert errs(fixed, rubric, settings) == []
    assert p["competencies"][0]["rating"] == 4  # original untouched


def test_c01_untouched_with_reference(rubric):
    fixed, notes = normalize_payload(make_payload(), rubric, has_reference=True)
    assert fixed["competencies"][0]["rating"] == 4 and notes == []


def test_timeline_participants_and_conduct_are_required(rubric, settings):
    for key in ("timeline", "participants", "conduct_events"):
        p = make_payload()
        del p[key]
        assert any(key in e for e in errs(p, rubric, settings)), key


def test_timeline_must_cover_the_recording(rubric, settings):
    p = make_payload()
    p["timeline"] = [{"start": "00:00:00", "end": "00:20:00", "state": "teaching", "note": ""}]
    assert any("describes only" in e for e in errs(p, rubric, settings))


def test_timeline_unknown_state_and_bad_order(rubric, settings):
    p = make_payload()
    p["timeline"][0]["state"] = "napping"
    assert errs(p, rubric, settings)
    p = make_payload()
    p["timeline"] = [{"start": "00:10:00", "end": "00:05:00", "state": "teaching", "note": ""}]
    assert any("must be after start" in e for e in errs(p, rubric, settings))


def test_participant_beyond_video_and_conduct_beyond_video(rubric, settings):
    p = make_payload()
    p["participants"][0]["first_seen"] = "02:00:00"
    assert any("participant" in e for e in errs(p, rubric, settings))
    p = make_payload()
    p["conduct_events"] = [{"timestamp": "02:00:00", "type": "phone_use", "duration_seconds": 30, "note": "x"}]
    assert any("conduct event" in e for e in errs(p, rubric, settings))


def test_bad_conduct_type_and_negative_duration(rubric, settings):
    p = make_payload()
    p["conduct_events"] = [{"timestamp": "00:10:00", "type": "rude", "duration_seconds": 30, "note": "x"}]
    assert errs(p, rubric, settings)
    p["conduct_events"] = [{"timestamp": "00:10:00", "type": "phone_use", "duration_seconds": -5, "note": "x"}]
    assert errs(p, rubric, settings)
