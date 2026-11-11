"""Console + rotating file logs. Never log secrets, video content or full model output."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.config import Settings

_done = False


def setup_logging(settings: Settings) -> None:
    global _done
    if _done:
        return
    _done = True
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_h = RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger("app")
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    root.addHandler(console)
