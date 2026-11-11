"""Pre-flight check: python scripts/doctor.py   (make doctor)

Verifies everything the app needs before real recordings are processed. Exit code 1 if anything blocks.
Never prints secrets.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT, ConfigError, Settings  # noqa: E402

OK, WARN, BAD = "  [ OK ]", "  [WARN]", "  [FAIL]"
failed = 0


def report(level: str, msg: str) -> None:
    global failed
    failed += level == BAD
    print(f"{level} {msg}")


def main() -> int:
    try:
        s = Settings.from_env()
    except ConfigError as e:
        report(BAD, f"Configuration: {e}")
        return 1
    report(OK, f"Configuration loaded (mode: {'MOCK - dummy scores' if s.mock_gemini else 'LIVE'}, model {s.model})")
    if s.mock_gemini:
        report(WARN, "MOCK_GEMINI=true: scores are fixtures. Do NOT use for real recordings.")

    for tool in ("ffmpeg", "ffprobe"):
        report(OK if shutil.which(tool) else BAD, f"{tool} {'found' if shutil.which(tool) else 'missing (brew install ffmpeg)'}")

    try:
        from app.db import make_session_factory

        with make_session_factory(s.db_url)() as db:
            db.execute(__import__("sqlalchemy").text("select 1"))
        report(OK, f"Database reachable ({s.db_url.split('@')[-1] if '@' in s.db_url else s.db_url})")
        from sqlalchemy import func, select

        from app.db import Module, User

        with make_session_factory(s.db_url)() as db:
            admins = db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active))
            mods = list(db.scalars(select(Module)))
        report(OK if admins else BAD, f"{admins} active admin user(s)" + ("" if admins else " - run: make create-admin"))
        withref = [m.code for m in mods if m.reference_content.strip()]
        report(OK if withref else WARN, f"{len(withref)} module(s) with reference content"
               + ("" if withref else " - C01 will be N/A until you add them under Admin > Modules"))
    except Exception as e:  # noqa: BLE001
        report(BAD, f"Database: {str(e)[:200]}")

    free_gb = shutil.disk_usage(s.data_dir if s.data_dir.exists() else ROOT).free / 1024**3
    need = 0.4 * 20  # one operational batch of 20 recordings
    report(OK if free_gb > need * 2 else WARN,
           f"{free_gb:.0f} GB free for videos (about 0.4 GB per recording; a batch of 20 needs ~{need:.0f} GB)"
           + ("" if free_gb > need * 2 else
              ". Process in small waves and set DELETE_VIDEO_AFTER_ASSESSMENT=true, or use STORAGE_BACKEND=gcs"))

    if s.storage_backend == "gcs":
        try:
            from app.storage import make_storage

            make_storage(s)
            report(OK, f"GCS bucket {s.gcs_bucket} reachable")
        except Exception as e:  # noqa: BLE001
            report(BAD, f"GCS: {str(e)[:200]}")

    if not s.mock_gemini:
        if not s.cookie_secure:
            report(WARN, "COOKIE_SECURE=false: set true when served over HTTPS")
        try:
            from app.gemini.live import LiveEngine

            eng = LiveEngine(s)
            ok, msg = eng.check_model()
            report(OK if ok else BAD, msg)
            ok, msg = eng.ping()
            report(OK if ok else BAD, msg)
        except ImportError:
            report(BAD, "google-genai not installed (make setup-live)")
        except Exception as e:  # noqa: BLE001
            report(BAD, f"Gemini: {str(e)[:200]}")

    print("\nAll blocking checks passed." if not failed else f"\n{failed} blocking problem(s) above.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
