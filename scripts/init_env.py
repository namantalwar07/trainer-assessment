"""Create .env with a fresh SESSION_SECRET if it does not exist: python scripts/init_env.py"""

import secrets
import sys
from pathlib import Path

env = Path(__file__).resolve().parent.parent / ".env"
if env.exists():
    sys.exit(".env already exists; left untouched.")
env.write_text(f"""# Trainer Assessment configuration. Never commit or share this file.
# LIVE mode: real Gemini scoring. Paste your key below, then run: make doctor
MOCK_GEMINI=false
GEMINI_API_KEY=
SESSION_SECRET={secrets.token_urlsafe(48)}

# Set to true when served over HTTPS (required for a company deployment)
COOKIE_SECURE=false
PUBLIC_BASE_URL=http://127.0.0.1:8000

# Database: default is a local SQLite file in ./data. For daily company use prefer PostgreSQL:
# DATABASE_URL=postgresql+psycopg://trainer:CHANGE_ME@localhost:5432/trainer_assessment

# Storage: local disk by default. For a private Google Cloud Storage bucket:
# STORAGE_BACKEND=gcs
# GCS_BUCKET=

# Policy (defaults shown)
# MAX_UPLOAD_BYTES=1073741824
# RETENTION_DAYS=30
# DELETE_VIDEO_AFTER_ASSESSMENT=true   # frees disk after each job (reviewers then cannot re-watch the video)
# MIN_FREE_DISK_GB=1.5                 # uploads are refused below this
# MIN_COVERAGE_POINTS=70
# SCORE_FLOOR_MODE=accept
# USD_INR=90
""")
env.chmod(0o600)
print("Created .env (permissions 600). Add GEMINI_API_KEY, then run: make doctor")
