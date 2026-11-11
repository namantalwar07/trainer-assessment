import datetime as dt

import pytest

from app.db import InvoiceEntry, Trainer, User, VideoSession, make_session_factory
from app.invoices import InvoiceError, import_csv, link_matches, link_row, parse_date, parse_time_range
from app.security import hash_password


@pytest.mark.parametrize("text,expected", [
    ("1:56-3:26 PM", (13 * 3600 + 56 * 60, 15 * 3600 + 26 * 60)),
    ("1:56 PM – 3:26 PM", (13 * 3600 + 56 * 60, 15 * 3600 + 26 * 60)),
    ("13:56 to 15:26", (13 * 3600 + 56 * 60, 15 * 3600 + 26 * 60)),
    ("11:30-12:30 PM", (11 * 3600 + 30 * 60, 12 * 3600 + 30 * 60)),
    ("10:00 AM - 11:30 AM", (10 * 3600, 11 * 3600 + 30 * 60)),
])
def test_time_ranges(text, expected):
    assert parse_time_range(text) == expected


@pytest.mark.parametrize("bad", ["", "1:56", "3:26 PM - 1:56 PM", "xx-yy"])
def test_bad_ranges(bad):
    with pytest.raises(InvoiceError):
        parse_time_range(bad)


def test_dates():
    assert parse_date("13 Aug 2026") == parse_date("2026-08-13") == parse_date("13/08/2026") == dt.date(2026, 8, 13)
    with pytest.raises(InvoiceError):
        parse_date("32 Aug 2026")


CSV = """Invoice Row,Mentor Name,Trainer ID,Date,MSME,Invoiced time
22,Shrishti Pathak,TR-001,13 Aug 2026,Shyam / Shri Krishna,1:56-3:26 PM
23,Another Mentor,,14 Aug 2026,Other Shop,10:00 AM - 11:30 AM
bad,X,,13 Aug 2026,Y,1:00-2:00 PM
"""


def test_csv_import_with_flexible_headers_and_error_report(settings):
    sm = make_session_factory(settings.db_url)
    with sm() as db:
        r = import_csv(db, CSV)
        assert (r.created, r.updated) == (2, 0) and len(r.errors) == 1 and "Line 4" in r.errors[0]
        e = db.query(InvoiceEntry).filter_by(invoice_row=22).one()
        assert e.mentor_name == "Shrishti Pathak" and e.trainer_code == "TR-001"
        assert (e.start_seconds, e.end_seconds) == (13 * 3600 + 56 * 60, 15 * 3600 + 26 * 60)
        assert import_csv(db, CSV).updated == 2  # re-import updates in place


def test_csv_missing_columns_is_reported(settings):
    with make_session_factory(settings.db_url)() as db:
        assert "Missing column" in import_csv(db, "a,b\n1,2\n").errors[0]


def test_matching_by_trainer_and_date_and_manual_link(settings):
    sm = make_session_factory(settings.db_url)
    with sm() as db:
        u = User(email="a@t.test", role="admin", password_hash=hash_password("x"))
        t = Trainer(trainer_code="TR-001")
        db.add_all([u, t])
        db.flush()

        def mk(n, date, sha):
            s = VideoSession(trainer_id=t.id, module_code="M", state_code="UP", state="UP", session_code=f"S{n:03d}",
                             session_date=date, language="Hindi", original_filename="f", storage_key="k",
                             sha256=sha, size_bytes=1, uploaded_by=u.id, video_id=f"VID-{n}")
            db.add(s)
            return s

        s1, s2 = mk(1, dt.date(2026, 8, 13), "a"), mk(2, dt.date(2026, 8, 20), "b")
        db.commit()
        import_csv(db, CSV)
        assert link_matches(db) == 1
        assert s1.invoice_entry_id and s2.invoice_entry_id is None and t.name == "Shrishti Pathak"
        with pytest.raises(InvoiceError, match="already linked"):
            link_row(db, s2, 22)
        with pytest.raises(InvoiceError, match="not been imported"):
            link_row(db, s2, 99)
        assert link_row(db, s2, 23).invoice_row == 23
