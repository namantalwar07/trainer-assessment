"""Session Recording Review PDF.

Pages 1-3 follow the sir's format exactly (Session, Result, Integrity, Gaps, Invoiced length vs recording length,
Observation). Everything after "Additional analysis" is extra: effective time, mentees, conduct, trainer scorecard.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.rubric import Rubric
from app.timeline import CONDUCT_LABELS, STATE_COLORS, STATE_LABELS

INK = colors.HexColor("#1f1f1f")
MUTE = colors.HexColor("#5f5f5f")
HEAD_BG = colors.HexColor("#f3f1ea")  # the warm grey of the sir's table headers
GRID = colors.HexColor("#dcdcd6")
RED = colors.HexColor("#b42318")

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10.5, leading=15, textColor=INK)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=10, leading=13.5)
HEADCELL = ParagraphStyle("headcell", parent=CELL, textColor=MUTE)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.5, leading=11, textColor=MUTE)
TITLE = ParagraphStyle("title", fontName="Times-Bold", fontSize=25, leading=30, textColor=INK, spaceAfter=10)
H2 = ParagraphStyle("h2", fontName="Times-Roman", fontSize=18, leading=22, textColor=INK, spaceBefore=16,
                    spaceAfter=8)
H3 = ParagraphStyle("h3", fontName="Times-Roman", fontSize=14, leading=18, textColor=INK, spaceBefore=12, spaceAfter=6)
RESULT = ParagraphStyle("result", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=16)
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=16, bulletIndent=4, spaceAfter=4)

BAND_COLORS = {"Excellent": "#067647", "Good": "#2b6cb0", "Satisfactory": "#b7791f",
               "Needs improvement": "#c05621", "Unsatisfactory": "#b42318", "Not scorable": "#667085"}


def _p(text: object, style=CELL) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _table(rows: list[list], widths: list[float], header: bool = True, red_rows: tuple[int, ...] = (),
           pad: int = 8) -> Table:
    t = Table(rows, colWidths=[w * mm for w in widths], repeatRows=1 if header else 0, hAlign="LEFT")
    style = [("GRID", (0, 0), (-1, -1), 0.5, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
             ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), HEAD_BG))
    style += [("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fdeceb")) for i in red_rows]
    t.setStyle(TableStyle(style))
    return t


def _header_row(labels: list[str]) -> list[Paragraph]:
    return [_p(x, HEADCELL) for x in labels]


class _NumberedCanvas(rl_canvas.Canvas):
    """Draws the running header and 'Page X of Y' like the sir's report."""

    header_text = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._saved: list[dict] = []

    def showPage(self):  # noqa: N802
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTE)
            self.drawString(16 * mm, LETTER[1] - 11 * mm, self.header_text)
            self.drawRightString(LETTER[0] - 16 * mm, 10 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _timeline_strip(ta: dict) -> Drawing:
    w, h = 175 * mm, 7 * mm
    d = Drawing(w, h + 1 * mm)
    total = max(ta["total_seconds"], 1)
    for sp in ta["spans"]:
        d.add(Rect(w * sp["start"] / total, 0, max(w * (sp["end"] - sp["start"]) / total, 0.4), h,
                   fillColor=colors.HexColor(STATE_COLORS.get(sp["state"], "#98a2b3")), strokeColor=None))
    return d


def _bar(rating: int | None, na: bool) -> Drawing:
    d = Drawing(40 * mm, 4 * mm)
    d.add(Rect(0, 0.8 * mm, 34 * mm, 2.4 * mm, fillColor=colors.HexColor("#e3e7f0"), strokeColor=None))
    if not na and rating:
        col = "#067647" if rating >= 4 else ("#b7791f" if rating == 3 else "#b42318")
        d.add(Rect(0, 0.8 * mm, 34 * mm * rating / 5, 2.4 * mm, fillColor=colors.HexColor(col), strokeColor=None))
    return d


# ---------------------------------------------------------------- the sir's format
def _standard_review(r: dict) -> list:
    s: list = [_p(r["title"], TITLE)]
    if r.get("mentee_names"):
        s.append(_p("Mentor: " + ("not named in the recording" if r["mentor"].startswith("TR-") else r["mentor"]) + "  ·  Mentee(s): " + ", ".join(r["mentee_names"]), BODY))

    s += [_p("Session", H2), _table([
        _header_row(["Invoice row", "Date", "MSME", "Invoiced time", "Recording"]),
        [_p(r["session"][k]) for k in ("invoice_row", "date", "msme", "invoiced", "recording")]],
        [24, 25, 40, 42, 42])]

    s += [_p("Result", H2), _p(r["result"], RESULT)]
    if r["clock_note"]:
        s.append(_p(r["clock_note"], SMALL))

    integ = r["integrity"]
    s += [_p("Integrity", H2), _p(integ["heading"], BODY)]
    items = [(i, True) for i in integ["issues"]] + [(ln, False) for ln in integ["lines"]]
    for text, is_issue in items:
        st = ParagraphStyle("b", parent=BULLET, textColor=RED if is_issue else INK)
        s.append(Paragraph(escape(text), st, bulletText="•"))
    for n in integ["notes"]:
        s.append(_p("Note: " + n, SMALL))

    s.append(_p("Gaps, silences and technical issues of 1 minute or more", H2))
    if r["events"]:
        rows = [_header_row(["No.", "Type", "Clock time", "Recording time", "Length", "What happened"])] + [
            [_p(e["n"]), _p(e["type"]), _p(e["clock"]), _p(e["recording"]), _p(e["length"]), _p(e["detail"])]
            for e in r["events"]]
        s += [_table(rows, [13, 22, 26, 44, 18, 53]), Spacer(1, 2 * mm),
              _p("Every stretch where the trainer was not teaching: gaps, trainer silence and technical problems. "
                 "None of this time is counted as effective teaching time. Technical problems are not held against "
                 "the trainer.", SMALL)]
    else:
        s.append(_p("No gaps, silences or technical issues of 1 minute or more were found.", BODY))
    s.append(Spacer(1, 3 * mm))

    comp = r["comparison"]
    rows = [_header_row(["Measure", "Time", "Length"])] + [
        [_p(x["measure"]), _p(x["time"]), _p(x["length"])] for x in comp["rows"]]
    s.append(KeepTogether([_p("Invoiced length vs recording length" if comp["has_invoice"] else "Recording length", H2),
                           _table(rows, [76, 42, 38])]))
    if comp["paragraph"]:
        s += [Spacer(1, 3 * mm), _p(comp["paragraph"], BODY)]
    if not comp["has_invoice"]:
        s.append(_p("No invoice row is linked to this recording, so it was not compared with an invoiced time.",
                    SMALL))

    s += [_p("Observation", H2), _p(r["observation"], BODY)]
    return s


# ---------------------------------------------------------------- additional analysis
def _additional(a, sess, rubric: Rubric, r: dict, tolerance_text: str) -> list:
    s: list = [PageBreak(), _p("Additional analysis", TITLE),
               _p("The sections below go beyond the standard session review: effective teaching time, mentees, "
                  "trainer conduct and the trainer assessment scorecard. They are produced by the same AI-assisted "
                  "review and are meant for human verification.", SMALL)]

    banner_color = colors.HexColor(BAND_COLORS.get(a.band, "#667085"))
    score = "Not scorable" if a.overall_score is None else f"{a.overall_score:.1f} / 100"
    banner = Table([[_p(f"Trainer assessment score: {score}", ParagraphStyle("bs", parent=BODY, textColor=colors.white,
                                                                              fontName="Helvetica-Bold", fontSize=12)),
                     _p(a.band, ParagraphStyle("bb", parent=BODY, textColor=colors.white, alignment=2,
                                               fontName="Helvetica-Bold", fontSize=12))]], colWidths=[110 * mm, 62 * mm])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), banner_color), ("TOPPADDING", (0, 0), (-1, -1), 7),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 10)]))
    s += [Spacer(1, 3 * mm), banner, _p(a.coverage_text + ". Ratings use a 1-5 scale, so the lowest possible rated "
                                        "score is 20/100 unless a 0 anchor is enabled.", SMALL)]

    if a.human_review_required:
        s.append(_p("Items for human review", H3))
        s += [Paragraph(escape(x), BULLET, bulletText="•") for x in a.review_reasons]

    ta = a.time_analysis
    if ta:
        s.append(_p("Effective teaching time", H3))
        eff, tot = ta["effective_seconds"] / 60, ta["total_seconds"] / 60
        ok = ta["meets_threshold"]
        verdict = (f"MEETS the {ta['threshold_minutes']:g} minute expectation" if ok
                   else f"BELOW the {ta['threshold_minutes']:g} minute expectation")
        s.append(Paragraph(escape(f"{eff:.0f} min effective teaching time out of a {tot:.0f} min recording: {verdict}."),
                           ParagraphStyle("v", parent=BODY, textColor=colors.HexColor("#067647") if ok else RED)))
        s.append(_p("Counts only real teaching and supervised participant exercises. Technical problems, "
                    "trainer silence, pauses, breaks, absence and off-task time are all deducted.", SMALL))
        s += [Spacer(1, 2 * mm), _timeline_strip(ta)]
        legend = "   ".join(f'<font color="{STATE_COLORS[k]}">&#9632;</font> {escape(STATE_LABELS[k])} {v / 60:.0f} min'
                            for k, v in ta["breakdown"].items() if v)
        s.append(Paragraph(legend, SMALL))


    s.append(_p(f"Mentees named in the session ({len(a.participants or [])})", H3))
    if a.participants:
        rows = [_header_row(["Name (as heard or shown)", "First seen", "Source", "Spelling confidence"])] + [
            [_p(x["name"]), _p(x["first_seen"]), _p(x["source"].replace("_", " ")), _p(x["confidence"])]
            for x in a.participants]
        s += [_table(rows, [62, 28, 44, 40], pad=5),
              _p("Names are transcribed by AI from speech and on-screen text and may be misspelled; verify against "
                 "the attendance sheet.", SMALL)]
    else:
        s.append(_p("No mentee names were spoken or shown in the recording.", BODY))

    s.append(_p("Trainer conduct observations", H3))
    if a.conduct_events:
        rows = [_header_row(["Time", "Observation", "Length", "Detail"])] + [
            [_p(e["timestamp"]), _p(CONDUCT_LABELS.get(e["type"], e["type"])), _p(f"{e['duration_seconds']} s"),
             _p(e["note"])] for e in a.conduct_events]
        s += [_table(rows, [22, 48, 20, 82]),
              _p("Observable behaviour only. There may be valid reasons; these items are for human verification and "
                 "do not change the score.", SMALL)]
    else:
        s.append(_p("No conduct concerns were observed.", BODY))

    s.append(_p("Trainer assessment scorecard", H3))
    rows = [_header_row(["ID", "Competency", "Weight", "Rating", "", "Points", "Confidence"])]
    for c in a.scores:
        rows.append([_p(c["criterion_id"]), _p(c["name"]), _p(c["weight"]), _p("N/A" if c["na"] else c["rating"]),
                     _bar(c["rating"], c["na"]), _p("-" if c["na"] else f"{c['points']:.1f} / {c['adjusted_weight']:.1f}"),
                     _p(c["confidence"])])
    s.append(_table(rows, [11, 48, 17, 15, 38, 25, 24], pad=4))

    s.append(_p("Evidence by competency", H3))
    ev_by: dict[str, list] = {}
    for e in sorted(a.evidence, key=lambda x: x.seconds):
        ev_by.setdefault(e.criterion_id, []).append(e)
    for c in a.scores:
        block = [_p(f"{c['criterion_id']} {c['name']} - " + ("N/A" if c["na"] else f"{c['rating']}/5"),
                    ParagraphStyle("eh", parent=BODY, fontName="Helvetica-Bold", fontSize=10))]
        block += [_p(f"[{e.timestamp}] {e.observation} ({e.assessment_note})", CELL) for e in ev_by.get(c["criterion_id"], [])]
        if c["improvement"]:
            block.append(_p("Improvement: " + c["improvement"], SMALL))
        s.append(KeepTogether(block))

    for title, items in (("Top strengths", a.top_strengths), ("Development areas", a.development_areas)):
        s.append(_p(title, H3))
        s += [Paragraph(escape(i), BULLET, bulletText="•") for i in items] or [_p("None recorded", BODY)]

    s.append(_p("Coaching plan", H3))
    if a.coaching_plan:
        rows = [_header_row(["Priority", "Competency", "Action"])] + [
            [_p(c["priority"]), _p(c["competency"]), _p(c["action"])] for c in a.coaching_plan]
        s.append(_table(rows, [22, 28, 122]))
    else:
        s.append(_p("None recorded", BODY))

    s.append(_p("Critical flags", H3))
    s += [_p(f"[{f['timestamp']}] {f['description']}", BODY) for f in a.critical_flags] or [_p("None raised", BODY)]

    s += [Spacer(1, 6 * mm), _p(
        f"Prepared {dt.date.today().isoformat()} by AI-assisted review. Model: {a.model} | Prompt: {a.prompt_version} "
        f"| Rubric: {a.rubric_version} | API path: {a.api_path}. Clock times are read from the meeting clock shown in "
        "the recording; silences, frozen pictures and timestamp jumps are measured by software. This is not a "
        "forensic certification: a person should confirm any flagged item. For trainer development only.", SMALL)]
    return s


def build_pdf(a, sess, rubric: Rubric, path: Path, review: dict, tolerance_text: str = "2 minutes") -> None:
    class Canvas(_NumberedCanvas):
        header_text = review["header_title"]

    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                            bottomMargin=17 * mm, title=review["title"], author="Trainer Assessment System")
    story = _standard_review(review) + _additional(a, sess, rubric, review, tolerance_text)
    doc.build(story, canvasmaker=Canvas)
