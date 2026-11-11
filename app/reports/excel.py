"""Master workbook (7 sheets), regenerated after each batch. Written atomically."""

from __future__ import annotations

import os
import statistics
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Assessment, CoachingAction, Trainer, VideoSession
from app.pipeline import review_for
from app.rubric import Rubric
from app.timeutil import fmt_timestamp

HEAD_FILL = PatternFill("solid", fgColor="2457C5")
HEAD_FONT = Font(bold=True, color="FFFFFF")
BAND_FILL = {"Excellent": "C6EFCE", "Good": "DDEBF7", "Satisfactory": "FFF2CC", "Needs improvement": "FCE4D6",
             "Unsatisfactory": "F8CBAD"}


def workbook_path(settings: Settings) -> Path:
    return settings.data_dir / "exports" / "master.xlsx"


def _table(ws, headers: list[str], rows: list[list], start_row: int = 1) -> int:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=c, value=h)
        cell.font, cell.fill = HEAD_FONT, HEAD_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r, row in enumerate(rows, start_row + 1):
        for c, v in enumerate(row, 1):
            ws.cell(row=r, column=c, value=v)
    return start_row + len(rows) + 1


def _autosize(ws) -> None:
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width + 2, 10), 60)
    ws.freeze_panes = "A2"
    if ws.max_row > 1 and ws.max_column > 0:
        ws.auto_filter.ref = ws.dimensions


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 1) if xs else None


def _slope(points: list[tuple[int, float]]) -> float | None:
    """Score change per session (ordinal), least squares. Needs >= 2 points."""
    if len(points) < 2:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den, 2) if den else None


def regenerate_workbook(sm: sessionmaker, settings: Settings, rubric: Rubric) -> Path:
    min_n = settings.min_sessions_for_aggregate
    with sm() as db:
        sessions = db.scalars(select(VideoSession).order_by(VideoSession.id)).all()
        latest: dict[int, Assessment] = {}
        for a in db.scalars(select(Assessment).order_by(Assessment.id)):
            latest[a.session_id] = a
        coaching = db.scalars(select(CoachingAction).order_by(CoachingAction.id)).all()
        trainers = {t.id: t for t in db.scalars(select(Trainer))}

        wb = Workbook()

        # 1. Master Assessment
        ws = wb.active
        ws.title = "Master Assessment"
        rows = []
        for s in sessions:
            a = latest.get(s.id)
            rows.append([s.video_id, trainers[s.trainer_id].trainer_code, trainers[s.trainer_id].name, s.state,
                         s.city, s.module_code, str(s.session_date), round((s.duration_seconds or 0) / 60),
                         s.language, s.status, a.overall_score if a else None, a.band if a else None,
                         ("Yes" if a.human_review_required else "No") if a else None,
                         a.coverage_text if a else None,
                         round(a.time_analysis["effective_seconds"] / 60, 1) if a and a.time_analysis else None,
                         (("Yes" if a.time_analysis["meets_threshold"] else "No") if a and a.time_analysis else None),
                         a.time_analysis["flagged_gaps"] if a and a.time_analysis else None,
                         len(a.participants or []) if a else None,
                         f"{settings.public_base_url}/assessments/{a.id}/report.pdf" if a and a.report_path else None])
        _table(ws, ["Video_ID", "Trainer_ID", "Trainer_Name", "State", "City", "Module", "Session_Date",
                    "Duration_Min", "Primary_Language", "Processing_Status", "Overall_Score",
                    "Performance_Band", "Human_Review", "Coverage", "Effective_Min", "Meets_Min_Effective_Time",
                    "Long_Pauses_Flagged", "Mentees_Named", "Report_Link"], rows)
        _autosize(ws)
        for r in range(2, ws.max_row + 1):
            band_cell = ws.cell(row=r, column=12)
            if band_cell.value in BAND_FILL:
                band_cell.fill = PatternFill("solid", fgColor=BAND_FILL[band_cell.value])
            link = ws.cell(row=r, column=19)
            if ws.cell(row=r, column=16).value == "No":
                ws.cell(row=r, column=16).font = Font(bold=True, color="B42318")
            if (ws.cell(row=r, column=17).value or 0) > 0:
                ws.cell(row=r, column=17).font = Font(bold=True, color="B42318")
            if link.value:
                link.hyperlink, link.font = link.value, Font(color="0563C1", underline="single")
            if ws.cell(row=r, column=13).value == "Yes":
                ws.cell(row=r, column=13).font = Font(bold=True, color="B54708")
        ws.column_dimensions["S"].width = 22

        # 1b. Session Review (the standard session-recording-review summary, one row per recording)
        ws = wb.create_sheet("Session Review")
        rows = []
        for s in sessions:
            if not (a := latest.get(s.id)):
                continue
            rv = review_for(db, settings, a)
            m = rv["metrics"]
            rows.append([s.video_id, rv["row"], rv["mentor"], rv["session"]["date"], rv["session"]["msme"],
                         rv["session"]["invoiced"], rv["session"]["recording"], m["invoiced_minutes"],
                         m["recording_minutes"], m["within_invoice_minutes"], m["late_start_minutes"],
                         m["leading_silence_minutes"], m["conversation_minutes"], m["gap_count"],
                         "Yes" if m["integrity_ok"] else "Check", rv["result"], rv["observation"]])
        _table(ws, ["Video_ID", "Invoice_Row", "Mentor", "Date", "MSME", "Invoiced_Time", "Recording_Time",
                    "Invoiced_Min", "Recording_Min", "Within_Invoice_Min", "Late_Start_Min", "Leading_Silence_Min",
                    "Conversation_Min", "Gaps_Flagged", "Integrity_OK", "Result", "Observation"], rows)
        _autosize(ws)
        ws.column_dimensions["P"].width = 70
        ws.column_dimensions["Q"].width = 70
        for r in range(2, ws.max_row + 1):
            if ws.cell(row=r, column=15).value == "Check":
                ws.cell(row=r, column=15).font = Font(bold=True, color="B42318")

        # 2. Competency Scores (long format)
        ws = wb.create_sheet("Competency Scores")
        rows = [[s.video_id, c["criterion_id"], c["name"], c["weight"], "N/A" if c["na"] else c["rating"],
                 c["adjusted_weight"], c["points"]]
                for s in sessions if (a := latest.get(s.id)) for c in a.scores]
        _table(ws, ["Video_ID", "Criterion_ID", "Competency", "Weight", "Rating", "Adjusted_Weight",
                    "Weighted_Score"], rows)
        _autosize(ws)

        # 3. Evidence Register
        ws = wb.create_sheet("Evidence Register")
        rows = []
        for s in sessions:
            if not (a := latest.get(s.id)):
                continue
            meta = {c["criterion_id"]: c for c in a.scores}
            for e in sorted(a.evidence, key=lambda x: (x.criterion_id, x.seconds)):
                m = meta[e.criterion_id]
                rows.append([s.video_id, f"{e.criterion_id} {m['name']}", e.timestamp, e.observation,
                             m["improvement"], m["confidence"]])
        _table(ws, ["Video_ID", "Competency", "Timestamp", "Observation", "Recommendation", "Confidence"], rows)
        _autosize(ws)

        # 4. Trainer Summary: aggregate only with comparable session counts
        ws = wb.create_sheet("Trainer Summary")
        by_trainer: dict[int, list[tuple[VideoSession, Assessment]]] = defaultdict(list)
        for s in sessions:
            if (a := latest.get(s.id)) and a.overall_score is not None:
                by_trainer[s.trainer_id].append((s, a))
        rows = []
        for tid, items in sorted(by_trainer.items(), key=lambda kv: trainers[kv[0]].trainer_code):
            items.sort(key=lambda sa: (sa[0].session_date, sa[0].id))
            scores = [a.overall_score for _, a in items]
            n = len(items)
            base = [trainers[tid].trainer_code, trainers[tid].name, n]
            if n < min_n:
                rows.append(base + [None] * (6 + len(rubric.ids)) + [f"Insufficient sessions (<{min_n}); not aggregated"])
                continue
            comp_avgs = [_mean([next(c for c in a.scores if c["criterion_id"] == cid)["rating"] for _, a in items
                                if not next(c for c in a.scores if c["criterion_id"] == cid)["na"]])
                         for cid in rubric.ids]
            rows.append(base + [_mean(scores), max(scores), min(scores), round(statistics.pstdev(scores), 1),
                                _slope(list(enumerate(scores))), sum(a.human_review_required for _, a in items)]
                        + comp_avgs + ["Aggregated"])
        _table(ws, ["Trainer_ID", "Trainer_Name", "Sessions_Evaluated", "Average_Score", "Highest", "Lowest",
                    "Score_Variation_SD", "Trend_Per_Session", "Human_Review_Flags",
                    *[f"Avg_{cid}" for cid in rubric.ids], "Status"], rows)
        _autosize(ws)

        # 5. Language and Regional Analysis
        ws = wb.create_sheet("Language and Regional")
        groups: dict[tuple[str, str], list[Assessment]] = defaultdict(list)
        sess_by_id = {s.id: s for s in sessions}
        for sid, a in latest.items():
            if a.overall_score is not None:
                groups[(sess_by_id[sid].state, sess_by_id[sid].language)].append(a)
        rows = []
        for (state, lang), items in sorted(groups.items()):
            n = len(items)

            def comp_avg(cid: str, items=items) -> float | None:
                return _mean([c["rating"] for a in items for c in a.scores
                              if c["criterion_id"] == cid and not c["na"]])

            ok = n >= min_n
            rows.append([state, lang, n, _mean([a.overall_score for a in items]) if ok else None,
                         comp_avg("C03") if ok else None, comp_avg("C05") if ok else None,
                         sum(a.human_review_required for a in items),
                         "" if ok else f"Insufficient sessions (<{min_n})"])
        end = _table(ws, ["State", "Primary_Language", "Sessions", "Average_Score", "Avg_Communication_C03",
                          "Avg_Engagement_C05", "Human_Review_Flags", "Note"], rows)
        ws.cell(row=end + 1, column=1, value=(
            "Use this sheet to detect assessment-system inconsistency (e.g. one language scoring "
            "systematically lower). It must never be used to rank languages, accents or regions."))
        _autosize(ws)

        # 6a. Time and Conduct (one row per pause/gap and per conduct observation)
        ws = wb.create_sheet("Time and Conduct")
        rows = []
        for s in sessions:
            if not (a := latest.get(s.id)):
                continue
            for g in (a.time_analysis or {}).get("gaps", []):
                rows.append([s.video_id, trainers[s.trainer_id].trainer_code, "Pause", g["start"], g["end"],
                             round(g["seconds"] / 60, 1), ", ".join(g["states"]), "; ".join(g["notes"]),
                             "Flagged" if g["flagged"] else ("Tolerated" if g["forgiven"] else "Counted off")])
            for sp in (a.time_analysis or {}).get("silent_periods", []):
                rows.append([s.video_id, trainers[s.trainer_id].trainer_code, "Trainer silent", sp["start"], sp["end"],
                             round(sp["seconds"] / 60, 1), "trainer_silent", "; ".join(sp["notes"]),
                             "Flagged (not teaching time)"])
            for sp in (a.time_analysis or {}).get("spans", []):
                if sp["state"] == "technical_issue":
                    rows.append([s.video_id, trainers[s.trainer_id].trainer_code, "Technical issue",
                                 fmt_timestamp(sp["start"]), fmt_timestamp(sp["end"]),
                                 round((sp["end"] - sp["start"]) / 60, 1), "technical_issue", sp.get("note", ""),
                                 "Not held against trainer"])
            for e in a.conduct_events or []:
                rows.append([s.video_id, trainers[s.trainer_id].trainer_code, "Conduct observation", e["timestamp"],
                             "", round(e["duration_seconds"] / 60, 1), e["type"], e["note"], "For human verification"])
        _table(ws, ["Video_ID", "Trainer_ID", "Kind", "From", "To", "Minutes", "Type", "Detail", "Status"], rows)
        _autosize(ws)

        # 6b. Participants (mentees named in each session)
        ws = wb.create_sheet("Participants")
        rows = [[s.video_id, trainers[s.trainer_id].trainer_code, s.module_code, str(s.session_date), p["name"],
                 p["first_seen"], p["source"].replace("_", " "), p["confidence"]]
                for s in sessions if (a := latest.get(s.id)) for p in a.participants or []]
        _table(ws, ["Video_ID", "Trainer_ID", "Module", "Session_Date", "Mentee_Name_As_Heard", "First_Seen",
                    "Source", "Spelling_Confidence"], rows)
        _autosize(ws)

        # 6. Coaching Tracker
        ws = wb.create_sheet("Coaching Tracker")
        rows = [[trainers[c.trainer_id].trainer_code, c.competency, c.action, c.priority, c.owner or "To be assigned",
                 str(c.review_date) if c.review_date else "To be assigned", c.status] for c in coaching]
        _table(ws, ["Trainer_ID", "Development_Area", "Action", "Priority", "Owner", "Review_Date", "Status"], rows)
        _autosize(ws)

        # 7. Management Dashboard
        ws = wb.create_sheet("Management Dashboard")
        scored = [a for a in latest.values() if a.overall_score is not None]
        ws["A1"] = "Management Dashboard"
        ws["A1"].font = Font(bold=True, size=14)
        r = _table(ws, ["Metric", "Value"], [
            ["Total videos", len(sessions)],
            ["Videos assessed", len(scored)],
            ["Average score", _mean([a.overall_score for a in scored])],
            ["Human review flagged", sum(a.human_review_required for a in latest.values())],
            ["Failed / quarantined", sum(s.status in ("failed", "quarantined") for s in sessions)],
            ["Average effective teaching minutes", _mean([a.time_analysis["effective_seconds"] / 60 for a in scored
                                                         if a.time_analysis])],
            [f"Recordings below {settings.min_effective_minutes:g} effective minutes",
             sum(1 for a in scored if a.time_analysis and not a.time_analysis["meets_threshold"])],
            ["Recordings with a flagged long pause", sum(1 for a in scored if a.time_analysis
                                                         and a.time_analysis["flagged_gaps"])],
        ], start_row=3)
        band_hdr = r + 1
        r = _table(ws, ["Band", "Videos"], [[b.label, sum(a.band == b.label for a in scored)]
                                            for b in rubric.bands], start_row=band_hdr)

        def avg_by(key) -> list[list]:
            g: dict[str, list[float]] = defaultdict(list)
            for sid, a in latest.items():
                if a.overall_score is not None:
                    g[key(sess_by_id[sid])].append(a.overall_score)
            return [[k, len(v), _mean(v) if len(v) >= min_n else f"n<{min_n}"] for k, v in sorted(g.items())]

        r = _table(ws, ["State", "Videos", "Average score"], avg_by(lambda s: s.state), start_row=r + 1)
        r = _table(ws, ["Module", "Videos", "Average score"], avg_by(lambda s: s.module_code), start_row=r + 1)
        prof = []
        for crit in rubric.criteria:
            pct = [c["rating"] / 5 * 100 for a in scored for c in a.scores
                   if c["criterion_id"] == crit.id and not c["na"]]
            prof.append([f"{crit.id} {crit.name}", crit.weight, len(pct), _mean(pct)])
        prof_hdr = r + 1
        r = _table(ws, ["Competency", "Weight", "Sessions rated", "Avg % of max"], prof, start_row=prof_hdr)
        flagged = [[sess_by_id[sid].video_id, a.overall_score, a.band, "; ".join(a.review_reasons)]
                   for sid, a in latest.items() if a.human_review_required]
        _table(ws, ["Flagged video", "Score", "Band", "Reasons"], flagged, start_row=r + 1)
        _autosize(ws)
        ws.freeze_panes = None
        ws.auto_filter.ref = None
        ws.column_dimensions["A"].width = 36
        for (title, anchor, hdr_row, n) in ((" Band distribution", "F3", band_hdr, len(rubric.bands)),
                                            (" Competency profile (avg % of max)", "F20", prof_hdr, len(rubric.criteria))):
            ch = BarChart()
            ch.type, ch.title, ch.height, ch.width = "bar", title.strip(), 8, 16
            col = 2 if "Band" in title else 4
            ch.add_data(Reference(ws, min_col=col, min_row=hdr_row, max_row=hdr_row + n), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=hdr_row + 1, max_row=hdr_row + n))
            ch.legend = None
            ws.add_chart(ch, anchor)
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = Alignment(wrap_text=False, vertical="top")

    path = workbook_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    wb.save(tmp)
    os.replace(tmp, path)
    return path
