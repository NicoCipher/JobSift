"""Recoverable CSV replacement for a persisted batch, without changing legacy append export."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from job_scout.domain.daily_batch import BatchConflict
from job_scout.export.csv_exporter import CSV_COLUMNS


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def _image(path: Path, rows: list[dict[str, str]]) -> bytes:
    existing = path.read_bytes() if path.exists() else None
    if existing is not None:
        reader = csv.reader(io.StringIO(existing.decode("utf-8"), newline=""), strict=True)
        if next(reader, None) != CSV_COLUMNS or any(len(row) != len(CSV_COLUMNS) for row in reader):
            raise BatchConflict("existing CSV does not match the export contract")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    if existing is None:
        writer.writeheader()
    writer.writerows(rows)
    prefix = existing or b""
    if rows and prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += b"\r\n"
    return prefix + output.getvalue().encode("utf-8")


def plan_csv(path: Path, rows: list[dict[str, str]]) -> tuple[str, str]:
    return file_digest(path), hashlib.sha256(_image(path, rows)).hexdigest()


def _sync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def publish_csv(path: Path, rows: list[dict[str, str]], before: str, after: str) -> None:
    current = file_digest(path)
    if current == after:
        _sync(path)
        return
    if current != before:
        raise BatchConflict("destination changed; no rows written")
    image = _image(path, rows)
    if hashlib.sha256(image).hexdigest() != after:
        raise BatchConflict("export image does not match the persisted journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as f:
            name = f.name
            f.write(image)
            f.flush()
            os.fsync(f.fileno())
        if file_digest(path) != before:
            raise BatchConflict("destination changed before replacement; no rows written")
        os.replace(name, path)
        name = None
        _sync(path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


@contextmanager
def destination_lock(path: Path):
    # POSIX local-file locking: replacement changes the CSV inode, so lock a stable sibling.
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.daily-batch.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
