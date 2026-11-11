"""Enforce RETENTION_DAYS: delete expired video files (scores and evidence are kept). Run daily via cron."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import make_session_factory  # noqa: E402
from app.retention import purge_expired_videos  # noqa: E402
from app.storage import make_storage  # noqa: E402

s = Settings.from_env()
with make_session_factory(s.db_url)() as db:
    print("Removed", purge_expired_videos(db, make_storage(s), s), "video file(s)")
