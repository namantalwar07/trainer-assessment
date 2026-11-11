"""Invoice import, upload with an invoice row, late linking (report regenerated), roles."""

import io
import re

from sqlalchemy import select

from app.db import Assessment, InvoiceEntry, VideoSession
from app.worker import WorkerPool
from tests.test_app_flow import GOOD, create_app, csrf_of, fake_mp4, login  # noqa: F401
from tests.test_app_flow import app as app  # noqa: F401  (fixture)

CSV = "Invoice Row,Mentor Name,Trainer ID,Date,MSME,Invoiced time\n22,Shrishti Pathak,TR-001,04 Nov 2026,Shyam,1:56-3:26 PM\n"


def import_csv(c, text=CSV):
    tok = csrf_of(c, "/invoices")
    return c.post("/invoices/import", data={"csrf": tok}, files={"file": ("inv.csv", io.BytesIO(text.encode()), "text/csv")})


def api_upload(c, name=GOOD, row=""):
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get("/upload").text).group(1)
    return c.post("/api/upload", headers={"X-CSRF-Token": tok}, data={"language": "Hindi", "invoice_row": row},
                  files={"file": (name, io.BytesIO(fake_mp4()), "video/mp4")})


def test_roles_for_invoices(app):  # noqa: F811
    assert login(app, "operator").get("/invoices").status_code == 200
    assert login(app, "admin").get("/invoices").status_code == 200
    assert login(app, "reviewer").get("/invoices").status_code == 403


def test_import_then_upload_auto_links_by_trainer_and_date(app):  # noqa: F811
    op = login(app, "operator")
    assert import_csv(op).status_code == 303
    r = api_upload(op)  # GOOD is TR-001 on 2026-11-04, same as the invoice row
    assert r.json()["ok"]
    with app.state.sm() as db:
        s = db.scalar(select(VideoSession))
        assert s.invoice_entry_id == db.scalar(select(InvoiceEntry.id)) and s.trainer.name == "Shrishti Pathak"


def test_upload_with_explicit_row_and_unknown_row(app):  # noqa: F811
    op = login(app, "operator")
    bad = api_upload(op, row="99")
    assert bad.status_code == 422 and "not been imported" in bad.json()["message"]
    import_csv(op)
    assert api_upload(op, row="22").json()["ok"]
    dup = api_upload(op, name="UP_BUSINESSFUNDAMENTALS_TR001_S002_20261104.mp4", row="22")
    assert dup.status_code == 422 and "already has a recording" in dup.json()["message"]


def test_linking_after_processing_regenerates_the_report_and_flags(app):  # noqa: F811
    op, rev = login(app, "operator"), login(app, "reviewer")
    api_upload(op)
    WorkerPool(app.state.sm, app.state.settings, app.state.storage, app.state.rubric).run_until_empty()
    with app.state.sm() as db:
        a = db.scalar(select(Assessment))
        aid, sid = a.id, a.session_id
        assert "Not linked" in rev.get(f"/assessments/{aid}").text
    import_csv(op, CSV.replace("04 Nov 2026", "01 Jan 2030"))  # different date: no auto-link
    tok = csrf_of(op, "/invoices")
    assert op.post(f"/sessions/{sid}/invoice", data={"csrf": tok, "invoice_row": "22"}).status_code == 303
    page = rev.get(f"/assessments/{aid}").text
    assert "Row 22" in page and "Shyam" in page and "invoiced" in page.lower()
    with app.state.sm() as db:
        a = db.get(Assessment, aid)
        assert a.session.invoice_entry_id is not None
        assert (app.state.settings.data_dir / a.report_path).stat().st_size > 3000
    assert rev.get(f"/assessments/{aid}/report.pdf").content.startswith(b"%PDF")
