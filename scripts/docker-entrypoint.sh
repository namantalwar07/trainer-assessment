#!/bin/sh
# Hosted disks are mounted root-owned: fix ownership, create the first admin, then drop to the app user.
set -e
mkdir -p "$DATA_DIR"
chown -R appuser "$DATA_DIR"
runuser -u appuser -- python scripts/bootstrap_admin.py
exec runuser -u appuser -- python -m uvicorn app.web:get_app --factory --host 0.0.0.0 --port "${PORT:-8000}" \
     --proxy-headers --forwarded-allow-ips "*" --timeout-keep-alive 75
