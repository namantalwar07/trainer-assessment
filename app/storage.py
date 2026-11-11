"""Private video storage. Keys are generated server-side; nothing here is ever served statically."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from app.config import Settings


class Storage(Protocol):
    def put(self, src: Path, key: str) -> None: ...
    def local_copy(self, key: str): ...
    def move(self, key: str, new_key: str) -> None: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:  # blocks traversal even if a key were ever user-influenced
            raise ValueError("Invalid storage key")
        return p

    def put(self, src: Path, key: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)

    @contextmanager
    def local_copy(self, key: str) -> Iterator[Path]:
        yield self._path(key)  # already local; no temp copy to clean up

    def path(self, key: str) -> Path:
        return self._path(key)

    def move(self, key: str, new_key: str) -> None:
        dest = self._path(new_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self._path(key)), dest)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class GCSStorage:
    """Private bucket (enforce public-access-prevention on the bucket itself)."""

    def __init__(self, bucket: str, tmp_dir: Path):
        from google.cloud import storage  # lazy: live mode only

        self.bucket = storage.Client().bucket(bucket)
        self.tmp_dir = tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)

    def put(self, src: Path, key: str) -> None:
        self.bucket.blob(key).upload_from_filename(str(src))
        src.unlink(missing_ok=True)

    @contextmanager
    def local_copy(self, key: str) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(dir=self.tmp_dir) as d:
            dest = Path(d) / Path(key).name
            self.bucket.blob(key).download_to_filename(str(dest))
            yield dest  # directory (and the video copy) is deleted on exit

    def path(self, key: str) -> Path:
        raise NotImplementedError("Use signed_url for GCS objects")

    def signed_url(self, key: str, minutes: int = 10) -> str:
        import datetime as dt

        return self.bucket.blob(key).generate_signed_url(
            version="v4", expiration=dt.timedelta(minutes=minutes), method="GET")

    def move(self, key: str, new_key: str) -> None:
        blob = self.bucket.blob(key)
        self.bucket.copy_blob(blob, self.bucket, new_key)
        blob.delete()

    def delete(self, key: str) -> None:
        self.bucket.blob(key).delete()


def make_storage(settings: Settings) -> LocalStorage | GCSStorage:
    if settings.storage_backend == "gcs":
        return GCSStorage(settings.gcs_bucket or "", settings.data_dir / "tmp")
    return LocalStorage(settings.data_dir / "videos")
