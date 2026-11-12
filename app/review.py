"""Builds the 'Session Recording Review' content (sir's format) from stored facts.

Every number and sentence here is computed in code from: the activity timeline and measured signals, the
meeting-clock readings, the integrity findings, and the linked invoice row. The AI only supplies observations
(clock readings, short quotes, integrity notes); it never writes totals or verdicts.
"""

from __future__ import annotations

import datetime as dt
import statistics

from app.config import Settings
from app.recdate import DATE_NOTE
from app.timeline import STATE_LABELS
from app.timeutil import fmt_timestamp, parse_clock, parse_timestamp


# ---------- formatting ----------
def _mins(seconds: float) -> int:
    return int(seconds / 60 + 0.5)


def fmt_len(seconds: float) -> str:
    return f"{_mins(seconds)} min"


def _clock_parts(sec: float) -> tuple[str, str]:
    m = _mins(sec) % (24 * 60)
    h24, mi = divmod(m, 60)
    h12 = h24 % 12 or 12
    return f"{h12}:{mi:02d}", "PM" if h24 >= 12 else "AM"


def fmt_clock(sec: float) -> str:
    t, mer = _clock_parts(sec)
    return f"{t} {mer}"


def fmt_range(a: float, b: float) -> str:
    """'2:15–2:35 PM' (meridiem once when both ends share it, as in the sir's report)."""
    ta, ma = _clock_parts(a)
    tb, mb = _clock_parts(b)
    return f"{ta}–{tb} {mb}" if ma == mb else f"{ta} {ma}–{tb} {mb}"


def fmt_date(d: dt.date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


# ---------- meeting clock ----------
def clock_model(readings: list[dict], tolerance: int) -> dict | None:
    """Map video time -> wall-clock time from the readings. None when fewer than one usable reading."""
    pts = []
    for r in readings or []:
        t, c = parse_timestamp(r.get("video_time", "")), parse_clock(r.get("clock_time", ""))
        if t is not None and c is not None:
            pts.append((t, c))
    pts.sort()
    if not pts:
        return None
    unwrapped, day = [], 0
    for i, (t, c) in enumerate(pts):
        if i and c + day * 86400 < unwrapped[-1][1] - 12 * 3600:
            day += 1  # clock passed midnight
        unwrapped.append((t, c + day * 86400))
    offsets = [c - t for t, c in unwrapped]
    jumps = [{"at": unwrapped[i][0], "seconds": offsets[i] - offsets[i - 1]}
             for i in range(1, len(offsets)) if abs(offsets[i] - offsets[i - 1]) > tolerance]
    return {"offset": statistics.median(offsets), "readings": len(pts), "jumps": jumps}


# ---------- main builder ----------
def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def build_review(*, session, trainer_name: str, invoice, ta: dict | None, integrity: dict | None,
                 clock_readings: list[dict] | None, forensics: dict | None, observation_note: str | None,
                 settings: Settings, mentor_ai: str = "", mentees: list[str] | None = None) -> dict:
    ta = ta or {"total_seconds": int(session.duration_seconds or 0), "effective_seconds": 0, "gaps": []}
    integrity = integrity or {}
    forensics = forensics or {}
    total = float(ta["total_seconds"])
    flag_s = settings.gap_flag_seconds
    threshold_text = f"{flag_s / 60:g} minutes"
    mentor = trainer_name or (invoice.mentor_name if invoice and invoice.mentor_name else "") or mentor_ai.strip() \
        or session.trainer.trainer_code
    row = invoice.invoice_row if invoice else None
    date = invoice.session_date if invoice else session.session_date

    clock = clock_model(clock_readings or [], settings.clock_tolerance_seconds)
    start_c = clock["offset"] if clock else None
    end_c = start_c + total if clock else None

    def at(t: float) -> float:
        return (start_c or 0) + t

    def span_text(a: float, b: float) -> str:
        return fmt_range(at(a), at(b)) if clock else f"{fmt_timestamp(a)}–{fmt_timestamp(b)}"

    # ----- gaps -----
    gaps = [g for g in ta["gaps"] if g["flagged"]]
    leading = next((g for g in gaps if g["start_seconds"] <= 5), None)
    others = [g for g in gaps if g is not leading]
    gap_rows = []
    for n, g in enumerate(gaps, 1):
        note = "; ".join(x for x in g["notes"] if "measured" not in x.lower())
        if g["before"]:
            before = g["before"]
        elif g["start_seconds"] <= 5:
            low = note.lstrip()
            if low.lower().startswith(("the recording opens", "recording opens")):
                before = "Nothing; " + (low if low.lower().startswith("the ") else "the " + low)
            else:
                before = "Nothing; the recording opens with this gap" + (f" ({note})" if note else "")
        else:
            before = "—"
        gap_rows.append({"n": n, "clock": span_text(g["start_seconds"], g["end_seconds"]),
                         "length": fmt_len(g["seconds"]), "before": before, "after": g["after"] or "—",
                         "seconds": g["seconds"], "start_seconds": g["start_seconds"],
                         "end_seconds": g["end_seconds"], "note": note})

    # ----- technical issues (listed for transparency; not held against the trainer) -----
    tech_rows = []
    for sp in ta.get("spans", []):
        if sp["state"] != "technical_issue":
            continue
        if tech_rows and sp["start"] - tech_rows[-1]["end_seconds"] <= 5:  # merge adjacent technical spans
            prev = tech_rows[-1]
            prev["end_seconds"] = sp["end"]
            if sp.get("note") and sp["note"] not in prev["note"]:
                prev["note"] = (prev["note"] + " " + sp["note"]).strip()
            continue
        tech_rows.append({"start_seconds": sp["start"], "end_seconds": sp["end"], "note": sp.get("note", "")})
    for n, t in enumerate(tech_rows, 1):
        secs = t["end_seconds"] - t["start_seconds"]
        t.update({"n": n, "clock": span_text(t["start_seconds"], t["end_seconds"]), "length": fmt_len(secs),
                  "recording": f"{fmt_timestamp(t['start_seconds'])}–{fmt_timestamp(t['end_seconds'])}",
                  "seconds": secs})

    # ----- every non-teaching stretch of a minute or more, one table (gaps, silences, technical issues) -----
    events = []
    for g in ta.get("gaps", []):
        if g["seconds"] >= settings.silent_flag_seconds:
            kinds = ", ".join(STATE_LABELS[s_] for s_ in g["states"])
            ctx = " ".join(x for x in (f"Before: {g['before']}" if g["before"] else "",
                                       f"After: {g['after']}" if g["after"] else "") if x)
            note = "; ".join(x for x in g["notes"] if "measured" not in x.lower())
            events.append((g["start_seconds"], g["end_seconds"], kinds, " ".join(x for x in (note, ctx) if x)))
    for sp in ta.get("silent_periods", []):
        events.append((sp["start_seconds"], sp["end_seconds"], STATE_LABELS["trainer_silent"],
                       "; ".join(sp["notes"])))
    for t_ in tech_rows:
        if t_["seconds"] >= 30:
            events.append((t_["start_seconds"], t_["end_seconds"], STATE_LABELS["technical_issue"], t_["note"]))
    events.sort()
    event_rows = [{"n": n, "type": k, "clock": span_text(a_, b_), "recording": f"{fmt_timestamp(a_)}–{fmt_timestamp(b_)}",
                   "length": fmt_len(b_ - a_), "seconds": b_ - a_, "detail": d or "—"}
                  for n, (a_, b_, k, d) in enumerate(events, 1)]

    # ----- integrity -----
    issues: list[str] = []
    lines: list[str] = []
    notes: list[str] = []
    if clock and clock["readings"] >= 2:
        if clock["jumps"]:
            for j in clock["jumps"]:
                issues.append(f"The meeting clock jumps by about {_mins(abs(j['seconds']))} minutes near "
                              f"{fmt_timestamp(j['at'])} of the recording.")
        else:
            lines.append(f"The meeting clock shown in the recording runs continuously from "
                         f"{fmt_range(start_c, end_c).replace('–', ' to ')} with no jumps or breaks.")
    elif clock:
        notes.append("Only one meeting-clock reading was available, so clock continuity could not be checked.")
    else:
        notes.append("No meeting clock was visible in the recording, so times below are recording times and the "
                     "invoiced time cannot be compared with clock time.")
    same_m, same_p = integrity.get("same_mentor_throughout"), integrity.get("same_participant_throughout")
    summary = (integrity.get("identity_summary") or "").strip()
    if same_m is False or same_p is False:
        issues.append(summary or "The mentor or participant does not appear to be the same throughout the recording.")
    elif same_m and same_p:
        lines.append(summary or "The same mentor and the same participant appear throughout the recording.")
    elif summary:
        notes.append(summary)
    for e in integrity.get("edit_signs", []):
        issues.append(f"Possible edit at {e['timestamp']} ({e['type'].replace('_', ' ')}): {e['description']}")
    # Screen-sharing and static slides freeze constantly; only long freezes are worth a human's attention.
    freezes = [{"start": f["start"], "end": f["end"]} for f in forensics.get("freezes", [])
               if f["end"] - f["start"] >= 30]
    for f in integrity.get("freezes", []):
        t = parse_timestamp(f["timestamp"]) or 0
        if not any(x["start"] - 3 <= t <= x["end"] + 3 for x in freezes):
            freezes.append({"start": t, "end": t + f["duration_seconds"]})
    for j in forensics.get("pts_jumps", []):
        if not any(x["start"] - 3 <= j["at"] <= x["end"] + 3 for x in freezes):
            issues.append(f"The video's internal timestamps jump by {j['jump_seconds']:.0f} s after "
                          f"{fmt_timestamp(j['at'])}, which can indicate a cut.")
    if forensics.get("editor_tool"):
        issues.append("The file's metadata names a video editing tool, so it may not be the original recording.")
    if not any(i.startswith(("Possible edit", "The video's internal", "The file's metadata", "The meeting clock jumps"))
               for i in issues):
        causes = [f["likely_cause"] for f in integrity.get("freezes", [])]
        tail = ""
        if freezes and causes and all(c == "cameras_off" for c in causes):
            tail = " Short freezes in the picture coincide with cameras being switched off."
        elif freezes:
            tail = f" {len(freezes)} freeze(s) in the picture were noted."
        lines.append("No spliced or edited sections were found." + tail)
    unexplained = [f for f in integrity.get("freezes", []) if f["likely_cause"] != "cameras_off"
                   and f["duration_seconds"] >= 30]
    for f in unexplained:
        notes.append(f"Picture froze for {f['duration_seconds']} s at {f['timestamp']} (cause: "
                     f"{f['likely_cause'].replace('_', ' ')}): {f['description']}")
    integrity_out = {
        "ok": not issues, "heading": "No issues found:" if not issues else "Possible issues found:",
        "issues": issues, "lines": lines, "notes": notes,
    }

    # ----- invoice comparison -----
    rec_start = start_c
    rec_end = end_c
    metrics: dict = {"recording_minutes": _mins(total), "gap_count": len(gaps), "clock_available": bool(clock)}
    comparison_rows: list[dict] = []
    para: list[str] = []
    inv_s = inv_e = None
    uncovered_start = uncovered_end = after_end = overlap = 0.0
    conv_seconds = float(ta["effective_seconds"])
    first_words = (leading["end_seconds"] if leading else 0)
    if invoice:
        inv_s, inv_e = float(invoice.start_seconds), float(invoice.end_seconds)
        comparison_rows.append({"measure": "Invoiced session", "time": fmt_range(inv_s, inv_e),
                                "length": fmt_len(inv_e - inv_s)})
    comparison_rows.append({"measure": "Recording provided", "time": span_text(0, total), "length": fmt_len(total)})
    if invoice and clock:
        overlap = max(0.0, min(rec_end, inv_e) - max(rec_start, inv_s))
        uncovered_start = max(0.0, rec_start - inv_s)
        uncovered_end = max(0.0, inv_e - rec_end)
        after_end = max(0.0, rec_end - inv_e)
        comparison_rows.append({"measure": "Recording within the invoiced time",
                                "time": fmt_range(max(rec_start, inv_s), min(rec_end, inv_e)) if overlap else "—",
                                "length": fmt_len(overlap)})
    if leading:
        comparison_rows.append({"measure": "Silence at the start of the recording",
                                "time": span_text(0, leading["end_seconds"]), "length": fmt_len(leading["seconds"])})
    if others:
        comparison_rows.append({"measure": f"Other gaps of {threshold_text} or more",
                                "time": f"{len(others)} gap(s)", "length": fmt_len(sum(g["seconds"] for g in others))})
    comparison_rows.append({"measure": "Conversation on the recording", "time": span_text(first_words, total),
                            "length": f"about {_mins(conv_seconds)} min"})

    # ----- result and observation -----
    tamper = ("No signs of tampering." if not issues else
              "Possible signs of tampering were found (see Integrity below).")
    parts: list[str] = []
    if invoice:
        inv_len = inv_e - inv_s
        if _mins(total) < _mins(inv_len):
            parts.append(f"The recording is shorter than the invoiced session ({_mins(total)} min against "
                         f"{_mins(inv_len)} min)")
        elif _mins(total) > _mins(inv_len):
            parts.append(f"The recording is longer than the invoiced session ({_mins(total)} min against "
                         f"{_mins(inv_len)} min)")
        else:
            parts.append(f"The recording is the same length as the invoiced session ({_mins(total)} min)")
        if clock and _mins(uncovered_start) >= 1:
            parts.append(f"it starts {_mins(uncovered_start)} minutes after the invoiced start time")
        elif clock and _mins(rec_start - inv_s) <= -1:
            parts.append(f"it starts {_mins(inv_s - rec_start)} minutes before the invoiced start time")
        if clock and _mins(uncovered_end) >= 1:
            parts.append(f"it ends {_mins(uncovered_end)} minutes before the invoiced end time")
    else:
        parts.append(f"The recording is {_mins(total)} min long (no invoice row is linked to compare against)")
    if leading:
        parts.append(f"its first {_mins(leading['seconds'])} minutes are completely silent"
                     if leading["measured_silence_seconds"] >= 0.8 * leading["seconds"]
                     else f"its first {_mins(leading['seconds'])} minutes contain no teaching")
    if others:
        parts.append(f"it contains {len(others)} further gap{'s' if len(others) != 1 else ''} of "
                     f"{threshold_text} or more")
    if len(parts) == 1 and not leading and not others:
        parts[0] += f" and it has no gaps of {threshold_text} or more"
    result = tamper + " " + (_join([parts[0]] + parts[1:]) if parts else "") + "."

    obs: list[str] = []
    if invoice and clock:
        facts = []
        if _mins(uncovered_start) >= 1:
            facts.append(f"The recording starts {_mins(uncovered_start)} minutes after the invoiced start time")
        short = (inv_e - inv_s) - total
        if _mins(short) >= 1:
            facts.append(f"{'and ' if facts else 'The recording '}is {_mins(short)} minutes shorter than the invoiced session")
        if facts:
            obs.append(" ".join(facts) + ".")
    if leading:
        lead = (f"A further {_mins(leading['seconds'])} minutes at the start of the recording are silent"
                if obs else f"The first {_mins(leading['seconds'])} minutes of the recording are silent")
        if clock:
            lead += f", with the first words spoken at {fmt_clock(at(first_words))}"
        obs.append(lead + ".")
    if others:
        obs.append("Other gaps: " + "; ".join(f"{g['clock']} ({g['length']})" for g in gap_rows if g["n"] > (1 if leading else 0)) + ".")
    if (observation_note or "").strip():
        obs.append(observation_note.strip())
    observation = " ".join(obs) if obs else "No gaps or integrity concerns were observed in this recording."

    if invoice and clock and _mins(uncovered_start) >= 1:
        para.append(f"The first {_mins(uncovered_start)} minutes of the invoiced session "
                    f"({fmt_range(inv_s, rec_start)}) are not covered by the recording.")
    if invoice and clock and _mins(uncovered_end) >= 1:
        para.append(f"The last {_mins(uncovered_end)} minutes of the invoiced session "
                    f"({fmt_range(rec_end, inv_e)}) are not covered by the recording.")
    if invoice and clock and _mins(after_end) >= 1:
        para.append(f"The recording continues {_mins(after_end)} minutes after the invoiced end time.")
    if leading:
        why = f": {leading_note}" if (leading_note := next((g["note"] for g in gap_rows if g["n"] == 1 and g["note"]), "")) else ""
        para.append(f"Of the {_mins(total)} minutes recorded, the first {_mins(leading['seconds'])} minutes are "
                    f"{'complete silence' if leading['measured_silence_seconds'] >= 0.8 * leading['seconds'] else 'without teaching'}{why}. "
                    f"Conversation begins at {fmt_clock(at(first_words)) if clock else fmt_timestamp(first_words)}, so about "
                    f"{_mins(conv_seconds)} minutes of conversation is recorded"
                    + (f" against {_mins(inv_e - inv_s)} minutes invoiced." if invoice else "."))
    elif invoice:
        para.append(f"About {_mins(conv_seconds)} minutes of conversation is recorded against "
                    f"{_mins(inv_e - inv_s)} minutes invoiced.")
    comparison_para = " ".join(para)

    # ----- flags for human review -----
    flags: list[str] = [f"Integrity: {i}" for i in issues[:3]]
    if invoice and clock:
        shortfall = _mins(uncovered_start + uncovered_end) if overlap or True else 0
        if (uncovered_start + uncovered_end) / 60 >= settings.invoice_shortfall_minutes:
            flags.append(f"Invoice check: the recording covers {fmt_len(overlap)} of the {fmt_len(inv_e - inv_s)} invoiced "
                         f"({shortfall} min not covered)")
    if invoice and (inv_e - inv_s - conv_seconds) / 60 >= settings.invoice_shortfall_minutes:
        flags.append(f"Invoice check: about {_mins(conv_seconds)} min of conversation recorded against "
                     f"{_mins(inv_e - inv_s)} min invoiced")

    metrics.update({
        "invoiced_minutes": _mins(inv_e - inv_s) if invoice else None,
        "within_invoice_minutes": _mins(overlap) if invoice and clock else None,
        "late_start_minutes": _mins(uncovered_start) if invoice and clock else None,
        "leading_silence_minutes": _mins(leading["seconds"]) if leading else 0,
        "conversation_minutes": _mins(conv_seconds),
        "recording_clock_start": fmt_clock(rec_start) if clock else None,
        "recording_clock_end": fmt_clock(rec_end) if clock else None,
        "integrity_ok": not issues,
    })
    title_row = f"Row {row}" if row is not None else session.video_id
    return {
        "mentor": mentor, "mentee_names": [m for m in (mentees or []) if m], "row": row, "date": date, "title_row": title_row,
        "title": f"Session Recording Review – {mentor}, {title_row} ({fmt_date(date)})",
        "header_title": f"Session Recording Review – {mentor} – {title_row}",
        "session": {
            "invoice_row": str(row) if row is not None else "Not linked",
            "date": fmt_date(date) + (DATE_NOTE.get(getattr(session, "date_source", "") or "", "") if not invoice else ""),
            "msme": (invoice.msme_name if invoice and invoice.msme_name else "—"),
            "invoiced": (f"{fmt_range(inv_s, inv_e)} ({fmt_len(inv_e - inv_s)})" if invoice else "—"),
            "recording": (f"{span_text(0, total)} ({fmt_len(total)})"),
        },
        "result": result, "integrity": integrity_out, "gaps": gap_rows, "threshold_text": threshold_text,
        "technical": tech_rows, "events": event_rows,
        "no_other_gaps": (f"No other gaps of {threshold_text} or more were found." if gaps
                          else f"No gaps of {threshold_text} or more were found."),
        "comparison": {"rows": comparison_rows, "paragraph": comparison_para, "has_invoice": bool(invoice)},
        "observation": observation, "flags": flags, "metrics": metrics, "clock_available": bool(clock),
        "clock_note": notes[0] if not clock and notes else "",
    }


def report_filename(video_id: str, mentor: str, mentees: list[str]) -> str:
    """Download name that says whose session it is: 'Mentor - Mentee - VID-0001.pdf' (safe characters only)."""
    import re

    def clean(t: str) -> str:
        return re.sub(r"[^\w .'-]", "", t, flags=re.UNICODE).strip(" .-")[:40]

    mentor_c = clean(mentor)
    if mentor_c.upper().startswith("TR-") or not mentor_c:
        mentor_c = ""
    names = [clean(m) for m in mentees[:2] if clean(m)]
    mentee_c = " & ".join(names) + (f" +{len(mentees) - 2}" if len(mentees) > 2 else "")
    return " - ".join(x for x in (mentor_c, mentee_c, video_id) if x) + ".pdf"
