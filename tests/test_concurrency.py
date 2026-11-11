"""Simultaneous uploads (the browser uploads two files at once) must never produce a server error."""

import io
import struct
import threading

from sqlalchemy import func, select

from app.db import Trainer, User, VideoSession, make_session_factory
from app.ingest import ingest_upload
from app.security import hash_password
from app.storage import LocalStorage


def stub(tag: int) -> bytes:
    ftyp = b"ftypisom" + struct.pack(">I", 512) + b"isomiso2mp41"
    return struct.pack(">I", 4 + len(ftyp)) + ftyp + bytes([tag]) * 4096


def test_parallel_uploads_for_a_new_trainer(settings):
    sm = make_session_factory(settings.db_url)
    storage = LocalStorage(settings.data_dir / "videos")
    with sm() as db:
        db.add(User(email="op@t.test", role="operator", password_hash=hash_password("x")))
        db.commit()
    results, lock = [], threading.Lock()

    def worker(i: int):
        with sm() as db:
            user = db.scalar(select(User))
            r = ingest_upload(db, settings, storage, user, f"UP_MOD_TR050_S{i:03d}_20261105.mp4",
                              io.BytesIO(stub(i)), "Hindi")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert all(r.ok for r in results), [r.message for r in results if not r.ok]
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(Trainer)) == 1
        assert db.scalar(select(func.count()).select_from(VideoSession)) == 8


def test_parallel_identical_uploads_only_one_wins(settings):
    sm = make_session_factory(settings.db_url)
    storage = LocalStorage(settings.data_dir / "videos")
    with sm() as db:
        db.add(User(email="op@t.test", role="operator", password_hash=hash_password("x")))
        db.commit()
    results, lock = [], threading.Lock()

    def worker():
        with sm() as db:
            r = ingest_upload(db, settings, storage, db.scalar(select(User)),
                              "UP_MOD_TR051_S001_20261105.mp4", io.BytesIO(stub(7)), "Hindi")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r.ok for r in results) == 1
    assert all("uplicate" in r.message for r in results if not r.ok)
    with sm() as db:
        assert db.scalar(select(func.count()).select_from(VideoSession)) == 1
