"""Back up the database and generated reports (not the videos): python scripts/backup.py

SQLite: consistent online copy. PostgreSQL: pg_dump (must be installed). Keeps the newest 14 backups.
Run daily from cron / launchd; copy data/backups/ to a second location.
"""

import datetime as dt
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402

KEEP = 14
s = Settings.from_env()
out = s.data_dir / "backups"
out.mkdir(parents=True, exist_ok=True)
stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

if s.db_url.startswith("sqlite"):
    src = Path(s.db_url.removeprefix("sqlite:///"))
    dest = out / f"db-{stamp}.sqlite"
    with sqlite3.connect(src) as a, sqlite3.connect(dest) as b:
        a.backup(b)
else:
    if not shutil.which("pg_dump"):
        sys.exit("pg_dump not found; install PostgreSQL client tools.")
    dest = out / f"db-{stamp}.sql"
    url = s.db_url.replace("+psycopg", "")
    subprocess.run(["pg_dump", "--no-owner", "-f", str(dest), url], check=True)

reports = s.data_dir / "reports"
if reports.exists():
    with tarfile.open(out / f"reports-{stamp}.tar.gz", "w:gz") as t:
        t.add(reports, arcname="reports")

for pattern in ("db-*", "reports-*"):
    for old in sorted(out.glob(pattern))[:-KEEP]:
        old.unlink()
print("Backup written to", out)
