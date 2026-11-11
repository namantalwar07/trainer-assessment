"""The sir's example (Shrishti Pathak, invoice row 22) reproduced from raw facts."""

import datetime as dt
from types import SimpleNamespace as NS

from app.review import build_review, clock_model, fmt_clock, fmt_range
from app.timeline import analyse

H, M = 3600, 60
INVOICE = NS(invoice_row=22, mentor_name="Shrishti Pathak", session_date=dt.date(2026, 8, 13),
             msme_name="Shyam / Shri Krishna", start_seconds=13 * H + 56 * M, end_seconds=15 * H + 26 * M)
SESSION = NS(video_id="VID-0022", session_date=dt.date(2026, 8, 13), duration_seconds=4400,
             trainer=NS(trainer_code="TR-001"))
TOTAL = 4400  # 73 min 20 s


def seg(a, b, state="teaching", **kw):
    f = lambda s: f"{int(s) // 3600:02d}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}"  # noqa: E731
    return {"start": f(a), "end": f(b), "state": state, "note": kw.get("note", ""), "before": kw.get("before", ""),
            "after": kw.get("after", "")}


# recording starts 2:15:20 PM on the meeting clock; first 20 minutes are silent
READINGS = [{"video_time": "00:00:00", "clock_time": "14:15:20"}, {"video_time": "00:30:00", "clock_time": "14:45:20"},
            {"video_time": "01:13:20", "clock_time": "15:28:40"}]
INTEGRITY = {"clock_visible": True, "same_mentor_throughout": True, "same_participant_throughout": True,
             "identity_summary": "The same mentor and the same participant (in his shop) appear throughout the recording.",
             "edit_signs": [], "freezes": [{"timestamp": "00:40:00", "duration_seconds": 6, "likely_cause": "cameras_off",
                                            "description": "picture froze as cameras were switched off"}]}
AFTER = ("“Yes Shyam, right now you are free?” / “Yes madam, I have some Wi-Fi problem today.” "
         "The mentor then shares her screen and the session begins.")


def review(settings, invoice=INVOICE, readings=READINGS, integrity=INTEGRITY, note="", signals=None):
    segs = [seg(0, 1200, "pause", note="recording opens on the mentor's tile with camera off and no microphone active",
                after=AFTER), seg(1200, TOTAL)]
    ta = analyse(segs, TOTAL, settings, silences=signals or [{"start": 0, "end": 1200}])
    return build_review(session=SESSION, trainer_name="Shrishti Pathak", invoice=invoice, ta=ta.as_dict(),
                        integrity=integrity, clock_readings=readings, forensics={"freezes": [], "pts_jumps": []},
                        observation_note=note, settings=settings)


def test_formatting_matches_the_report_style():
    assert fmt_range(14 * H + 15 * M, 14 * H + 35 * M) == "2:15–2:35 PM"
    assert fmt_range(11 * H + 45 * M, 12 * H + 30 * M) == "11:45 AM–12:30 PM"
    assert fmt_clock(15 * H + 29 * M) == "3:29 PM"


def test_session_table_and_title(settings):
    r = review(settings)
    assert r["title"] == "Session Recording Review – Shrishti Pathak, Row 22 (13 Aug 2026)"
    assert r["header_title"] == "Session Recording Review – Shrishti Pathak – Row 22"
    assert r["session"] == {"invoice_row": "22", "date": "13 Aug 2026", "msme": "Shyam / Shri Krishna",
                            "invoiced": "1:56–3:26 PM (90 min)", "recording": "2:15–3:29 PM (73 min)"}


def test_result_sentence_matches_the_sirs_wording(settings):
    assert review(settings)["result"] == (
        "No signs of tampering. The recording is shorter than the invoiced session (73 min against 90 min), "
        "it starts 19 minutes after the invoiced start time, and its first 20 minutes are completely silent.")


def test_integrity_bullets(settings):
    i = review(settings)["integrity"]
    assert i["heading"] == "No issues found:" and i["ok"]
    assert i["lines"][0] == ("The meeting clock shown in the recording runs continuously from 2:15 to 3:29 PM "
                             "with no jumps or breaks.")
    assert "same mentor and the same participant" in i["lines"][1]
    assert i["lines"][2] == ("No spliced or edited sections were found. Short freezes in the picture coincide with "
                             "cameras being switched off.")


def test_gap_table(settings):
    r = review(settings)
    g = r["gaps"][0]
    assert (g["n"], g["clock"], g["length"]) == (1, "2:15–2:35 PM", "20 min")
    assert g["before"].startswith("Nothing; the recording opens") and "no microphone active" in g["before"]
    assert g["after"] == AFTER
    assert r["no_other_gaps"] == "No other gaps of 2.5 minutes or more were found."


def test_comparison_table_and_paragraph(settings):
    c = review(settings)["comparison"]
    assert [(x["measure"], x["time"], x["length"]) for x in c["rows"]] == [
        ("Invoiced session", "1:56–3:26 PM", "90 min"),
        ("Recording provided", "2:15–3:29 PM", "73 min"),
        ("Recording within the invoiced time", "2:15–3:26 PM", "71 min"),
        ("Silence at the start of the recording", "2:15–2:35 PM", "20 min"),
        ("Conversation on the recording", "2:35–3:29 PM", "about 53 min")]
    assert c["paragraph"].startswith("The first 19 minutes of the invoiced session (1:56–2:15 PM) are not covered by the recording.")
    assert "the first 20 minutes are complete silence" in c["paragraph"]
    assert "Conversation begins at 2:35 PM, so about 53 minutes of conversation is recorded against 90 minutes invoiced." in c["paragraph"]


def test_observation(settings):
    note = "The opening exchange after the silence points to a connectivity problem on the participant's side."
    o = review(settings, note=note)["observation"]
    assert o.startswith("The recording starts 19 minutes after the invoiced start time and is 17 minutes shorter than the invoiced session.")
    assert "A further 20 minutes at the start of the recording are silent, with the first words spoken at 2:35 PM." in o
    assert o.endswith(note)


def test_review_flags_for_invoice_shortfall(settings):
    flags = review(settings)["flags"]
    assert any("Invoice check" in f for f in flags)


def test_clean_session_has_no_gap_text_or_flags(settings):
    segs = [seg(0, 5400)]
    ta = analyse(segs, 5400, settings)
    inv = NS(**{**INVOICE.__dict__, "start_seconds": 14 * H + 15 * M + 20, "end_seconds": 14 * H + 15 * M + 20 + 5400})
    sess = NS(**{**SESSION.__dict__, "duration_seconds": 5400})
    readings = [{"video_time": "00:00:00", "clock_time": "14:15:20"}, {"video_time": "01:30:00", "clock_time": "15:45:20"}]
    r = build_review(session=sess, trainer_name="X", invoice=inv, ta=ta.as_dict(), integrity=INTEGRITY,
                     clock_readings=readings, forensics={}, observation_note="", settings=settings)
    assert r["gaps"] == [] and r["no_other_gaps"] == "No gaps of 2.5 minutes or more were found."
    assert r["result"].startswith("No signs of tampering. The recording is the same length as the invoiced session (90 min)")
    assert r["flags"] == [] and r["observation"].startswith("No gaps or integrity")


def test_clock_jump_is_an_integrity_issue(settings):
    bad = READINGS[:2] + [{"video_time": "01:13:20", "clock_time": "15:50:00"}]
    r = review(settings, readings=bad)
    assert not r["integrity"]["ok"] and r["integrity"]["heading"] == "Possible issues found:"
    assert "clock jumps" in r["integrity"]["issues"][0]
    assert r["result"].startswith("Possible signs of tampering were found")
    assert any(f.startswith("Integrity:") for f in r["flags"])


def test_edit_sign_and_identity_change_are_issues(settings):
    integ = {**INTEGRITY, "same_participant_throughout": False, "identity_summary": "A different person appears after 00:40:00.",
             "edit_signs": [{"timestamp": "00:41:00", "type": "jump_cut", "description": "abrupt change of background"}]}
    r = review(settings, integrity=integ)
    assert len(r["integrity"]["issues"]) == 2 and not r["integrity"]["ok"]


def test_without_a_visible_clock_uses_recording_time_and_says_so(settings):
    r = review(settings, readings=[], integrity={**INTEGRITY, "clock_visible": False})
    assert r["gaps"][0]["clock"] == "00:00:00–00:20:00" and r["clock_note"]
    assert not any("Recording within the invoiced time" == x["measure"] for x in r["comparison"]["rows"])


def test_without_an_invoice_the_report_still_works(settings):
    r = review(settings, invoice=None)
    assert r["session"]["invoice_row"] == "Not linked" and r["title"].startswith("Session Recording Review – Shrishti Pathak, VID-0022")
    assert "no invoice row is linked" in r["result"] and r["comparison"]["has_invoice"] is False


def test_clock_model_unwraps_midnight_and_reports_jumps():
    m = clock_model([{"video_time": "00:00:00", "clock_time": "23:50"}, {"video_time": "00:20:00", "clock_time": "00:10"}], 120)
    assert m["jumps"] == [] and m["offset"] == 23 * H + 50 * M
    assert clock_model([], 120) is None


def test_report_filename_names_mentor_and_mentees():
    from app.review import report_filename
    assert report_filename("VID-0007", "Shrishti Pathak", ["Aatika Gour", "Kashish", "Ravi"]) == \
        "Shrishti Pathak - Aatika Gour & Kashish +1 - VID-0007.pdf"
    assert report_filename("VID-0001", "TR-001", []) == "VID-0001.pdf"
    assert "/" not in report_filename("VID-0002", "A/B ../C", ["x\\y"])
