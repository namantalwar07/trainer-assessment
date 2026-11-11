"""DB-backed job queue and worker threads.

Why not Celery/Redis: batches are 10-20 videos, so a Postgres/SQLite table with an atomic
compare-and-set claim gives retries, unique job ids and restart safety without another service.
Run standalone with `python -m app.worker`, or embedded in the web process (EMBEDDED_WORKER=true).
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import ProcessingJob
from app.gemini import make_engine
from app.pipeline import run_job
from app.reports.excel import regenerate_workbook
from app.rubric import Rubric

_excel_lock = threading.Lock()


def claim_next(sm: sessionmaker) -> str | None:
    with sm() as db:
        for (job_id,) in db.execute(select(ProcessingJob.id).where(ProcessingJob.status == "queued")
                                    .order_by(ProcessingJob.created_at).limit(5)):
            won = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id,
                                                         ProcessingJob.status == "queued")
                             .values(status="running"))
            db.commit()
            if won.rowcount:
                return job_id
    return None


def recover_interrupted(sm: sessionmaker) -> int:
    """Jobs left 'running' by a crash go back to the queue (single worker process assumed)."""
    with sm() as db:
        n = db.execute(update(ProcessingJob).where(ProcessingJob.status == "running")
                       .values(status="queued")).rowcount
        db.commit()
        return n


def queue_is_empty(sm: sessionmaker) -> bool:
    with sm() as db:
        return db.scalar(select(ProcessingJob.id).where(
            ProcessingJob.status.in_(("queued", "running"))).limit(1)) is None


class WorkerPool:
    def __init__(self, sm: sessionmaker, settings: Settings, storage, rubric: Rubric, engine=None):
        self.sm, self.settings, self.storage, self.rubric = sm, settings, storage, rubric
        self.engine = engine or make_engine(settings)
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        recover_interrupted(self.sm)
        for i in range(max(1, self.settings.worker_concurrency)):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)

    def _loop(self) -> None:
        while not self.stop.is_set():
            job_id = claim_next(self.sm)
            if job_id is None:
                self.stop.wait(1.0)
                continue
            run_job(job_id, sm=self.sm, settings=self.settings, storage=self.storage,
                    engine=self.engine, rubric=self.rubric)
            if queue_is_empty(self.sm):  # batch drained: regenerate the master workbook
                with _excel_lock:
                    try:
                        regenerate_workbook(self.sm, self.settings, self.rubric)
                    except Exception:  # noqa: BLE001 - never kill the worker over an export
                        pass

    def run_until_empty(self, timeout: float = 60.0) -> None:
        """Test/CLI helper: process synchronously in the calling thread."""
        end = time.time() + timeout
        while time.time() < end and (job_id := claim_next(self.sm)):
            run_job(job_id, sm=self.sm, settings=self.settings, storage=self.storage,
                    engine=self.engine, rubric=self.rubric)
        with _excel_lock:
            regenerate_workbook(self.sm, self.settings, self.rubric)


if __name__ == "__main__":  # standalone worker process
    from app.db import make_session_factory
    from app.rubric import load_rubric
    from app.storage import make_storage

    cfg = Settings.from_env()
    pool = WorkerPool(make_session_factory(cfg.db_url), cfg, make_storage(cfg), load_rubric())
    pool.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pool.shutdown()
