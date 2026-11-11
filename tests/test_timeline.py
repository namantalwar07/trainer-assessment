from dataclasses import replace

import pytest

from app.timeline import analyse, check_timeline, clean_participants, conduct_summary, normalise
from app.timeutil import fmt_timestamp


def seg(a, b, state="teaching", note=""):
    return {"start": fmt_timestamp(a * 60), "end": fmt_timestamp(b * 60), "state": state, "note": note}


def run(segments, minutes, settings):
    return analyse(segments, minutes * 60, settings)


def test_full_session_of_teaching_is_effective(settings):
    r = run([seg(0, 75)], 75, settings)
    assert r.effective_minutes == 75 and r.meets_threshold and r.flagged_gaps == 0 and r.review_reasons == []


def test_short_pause_up_to_two_minutes_is_not_flagged_but_is_deducted(settings):
    r = run([seg(0, 30), seg(30, 32, "pause"), seg(32, 75)], 75, settings)
    assert r.effective_minutes == 73  # only real teaching counts
    assert r.flagged_gaps == 0 and r.gaps[0]["forgiven"] and not r.gaps[0]["flagged"]


def test_pause_between_tolerance_and_flag_is_subtracted_but_not_flagged(settings):
    r = run([seg(0, 30), seg(30, 32.2, "trainer_absent"), seg(32.2, 75)], 75, settings)  # 2 min 12 s
    assert r.effective_minutes == 72.8
    assert r.flagged_gaps == 0 and not r.gaps[0]["forgiven"] and not r.gaps[0]["flagged"]
    assert r.review_reasons == []


def test_gap_of_six_minutes_is_flagged_and_subtracted(settings):
    r = run([seg(0, 30), seg(30, 36, "trainer_absent", "left the room"), seg(36, 75)], 75, settings)
    assert r.effective_minutes == 69 and r.flagged_gaps == 1 and r.longest_gap_seconds == 360
    assert r.gaps[0]["flagged"] and "left the room" in r.gaps[0]["notes"]
    assert "2.5 min or more" in r.review_reasons[0] and "00:30:00" in r.review_reasons[0]


def test_two_and_a_half_minutes_is_flagged_but_149_seconds_is_not(settings):
    assert run([seg(0, 30), seg(30, 32.5, "break"), seg(32.5, 75)], 75, settings).flagged_gaps == 1
    r = run([seg(0, 30), seg(30, 32 + 29 / 60, "break"), seg(32 + 29 / 60, 75)], 75, settings)  # 2 min 29 s
    assert r.flagged_gaps == 0


def test_adjacent_non_effective_states_form_one_gap(settings):
    r = run([seg(0, 30), seg(30, 32, "break"), seg(32, 34, "pause"), seg(34, 75)], 75, settings)
    assert len(r.gaps) == 1 and r.gaps[0]["seconds"] == 240 and r.gaps[0]["flagged"]
    assert r.effective_minutes == 71


def test_two_short_pauses_are_deducted_not_flagged(settings):
    r = run([seg(0, 20), seg(20, 22, "pause"), seg(22, 40), seg(40, 42, "break"), seg(42, 75)], 75, settings)
    assert len(r.gaps) == 2 and r.flagged_gaps == 0 and r.effective_minutes == 71


def test_below_threshold_is_reported_and_routed_to_review(settings):
    r = run([seg(0, 40), seg(40, 50, "break"), seg(50, 70)], 70, settings)
    assert r.effective_minutes == 60 and r.meets_threshold  # exactly 60 passes
    r = run([seg(0, 40), seg(40, 50, "break"), seg(50, 65)], 65, settings)
    assert r.effective_minutes == 55 and not r.meets_threshold
    assert any("below the 60 min" in x for x in r.review_reasons)


def test_technical_issue_is_not_flagged_but_is_not_teaching_time(settings):
    r = run([seg(0, 30), seg(30, 36, "technical_issue", "projector failed"), seg(36, 75)], 75, settings)
    assert r.flagged_gaps == 0 and r.effective_minutes == 69 and r.technical_seconds == 360


def test_large_technical_downtime_asks_for_review(settings):
    r = run([seg(0, 20), seg(20, 35, "technical_issue"), seg(35, 80)], 80, settings)
    assert any("technical problem" in x for x in r.review_reasons)


def test_participant_activity_counts_by_default_and_is_configurable(settings):
    segs = [seg(0, 30), seg(30, 60, "participant_activity"), seg(60, 75)]
    assert run(segs, 75, settings).effective_minutes == 75
    off = replace(settings, count_participant_activity_as_effective=False)
    r = run(segs, 75, off)
    assert r.effective_minutes == 45 and r.flagged_gaps == 0  # trainer was present: not a gap


def test_off_task_counts_as_a_gap(settings):
    r = run([seg(0, 30), seg(30, 36, "off_task", "on phone"), seg(36, 75)], 75, settings)
    assert r.flagged_gaps == 1 and r.effective_minutes == 69


def test_tolerances_are_configurable(settings):
    strict = replace(settings, gap_tolerance_seconds=60, gap_flag_seconds=90)
    r = run([seg(0, 30), seg(30, 32, "pause"), seg(32, 75)], 75, strict)
    assert r.flagged_gaps == 1 and r.effective_minutes == 73


def test_overlaps_and_small_holes_are_normalised(settings):
    spans = normalise([seg(0, 31), seg(30, 60), {"start": "01:00:30", "end": "01:15:00", "state": "teaching"}],
                      75 * 60)
    assert [(s.start, s.end) for s in spans] == [(0, 1860), (1860, 3630), (3630, 4500)]  # overlap trimmed, 30 s hole bridged


def test_large_hole_becomes_a_described_pause(settings):
    spans = normalise([seg(0, 30), seg(40, 75)], 75 * 60)
    assert any(s.state == "pause" and s.seconds == 600 for s in spans)


def test_segments_beyond_duration_are_clipped(settings):
    r = run([seg(0, 90)], 75, settings)
    assert r.effective_minutes == 75


@pytest.mark.parametrize("segments,expect", [
    ([seg(0, 75)], []),
    ([seg(0, 30)], "describes only"),
    ([seg(10, 5)], "must be after start"),
    ([{"start": "xx", "end": "00:10:00", "state": "teaching"}], "unparseable"),
    ([seg(0, 100)], "beyond the video duration"),
])
def test_check_timeline(segments, expect):
    errs = check_timeline(segments, 75 * 60)
    assert errs == [] if expect == [] else any(expect in e for e in errs)


def test_participants_are_cleaned_and_deduplicated():
    items = [{"name": "  Ravi   Kumar ", "first_seen": "00:05:00", "source": "introduction", "confidence": "High"},
             {"name": "ravi kumar", "first_seen": "00:01:00", "source": "attendance", "confidence": "Low"},
             {"name": "", "first_seen": "00:02:00"}, {"name": "Sita Devi", "first_seen": "00:03:00"}]
    out = clean_participants(items)
    assert [p["name"] for p in out] == ["ravi kumar", "Sita Devi"]  # duplicate merged, earliest sighting wins
    assert out[0]["first_seen"] == "00:01:00"


def test_conduct_review_thresholds(settings):
    ev = lambda d: {"type": "phone_use", "duration_seconds": d}  # noqa: E731
    assert conduct_summary([ev(30), ev(20)], settings)[1] == []
    summary, reasons = conduct_summary([ev(30), ev(20), ev(10)], settings)
    assert reasons and summary["count"] == 3 and summary["total_seconds"] == 60
    assert conduct_summary([ev(400)], settings)[1]  # one long event (>= 2.5 min) also triggers review


def test_measured_silence_overrides_teaching_and_is_reported(settings):
    # the model says "teaching" for the whole 75 min, but the audio is silent for the first 20 minutes
    r = analyse([seg(0, 75)], 75 * 60, settings, silences=[{"start": 0, "end": 1200}])
    assert r.flagged_gaps == 1 and r.gaps[0]["seconds"] == 1200 and r.gaps[0]["measured_silence_seconds"] == 1200
    assert r.effective_minutes == 55 and not r.meets_threshold


def test_short_measured_silence_is_ignored(settings):
    r = analyse([seg(0, 75)], 75 * 60, settings, silences=[{"start": 100, "end": 160}])  # 60 s < 150 s
    assert r.gaps == [] and r.effective_minutes == 75


def test_gap_context_before_and_after_is_carried(settings):
    segs = [seg(0, 30), {**seg(30, 36, "trainer_absent"), "before": "Let us take the next question", "after": "Sorry, I am back"},
            seg(36, 75)]
    g = run(segs, 75, settings).gaps[0]
    assert g["before"] == "Let us take the next question" and g["after"] == "Sorry, I am back"


def test_trainer_silent_is_flagged_from_one_minute():
    from app.config import Settings
    from app.timeline import analyse

    s = Settings.from_env()
    segs = [{"start": "00:00:00", "end": "00:05:00", "state": "teaching", "note": ""},
            {"start": "00:05:00", "end": "00:06:10", "state": "trainer_silent", "note": "no speech"},
            {"start": "00:06:10", "end": "00:10:00", "state": "teaching", "note": ""},
            {"start": "00:10:00", "end": "00:10:30", "state": "trainer_silent", "note": "short"},
            {"start": "00:10:30", "end": "00:15:00", "state": "teaching", "note": ""}]
    ta = analyse(segs, 900, s)
    assert [p["seconds"] for p in ta.silent_periods] == [70]  # the 30 s silence is not flagged
    assert ta.effective_seconds == 900 - 70 - 30  # silences are never teaching time
    assert any("silent" in r for r in ta.review_reasons)
    long_ = analyse([{"start": "00:00:00", "end": "00:04:00", "state": "trainer_silent", "note": ""},
                     {"start": "00:04:00", "end": "00:10:00", "state": "teaching", "note": ""}], 600, s)
    assert long_.effective_seconds == 360
