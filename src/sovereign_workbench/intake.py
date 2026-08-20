from __future__ import annotations

import hashlib
import mimetypes
import os
from collections import defaultdict
from pathlib import Path

from .extract import extract_text
from .model import FileRecord


class IntakeError(ValueError):
    pass


def _contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def scan_files(root: Path, *, max_file_bytes: int = 50 * 1024 * 1024) -> list[FileRecord]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise IntakeError("Intake root must be a directory")

    records: list[FileRecord] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix().casefold()):
        if path.is_symlink() or not path.is_file() or not _contained(root, path):
            continue
        stat = path.stat()
        if stat.st_size > max_file_bytes:
            records.append(FileRecord(
                relative_path=path.relative_to(root).as_posix(),
                sha256="",
                size_bytes=stat.st_size,
                media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                modified_ns=stat.st_mtime_ns,
                extraction_status="skipped_size_limit",
            ))
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        text, status = extract_text(path)
        records.append(FileRecord(
            relative_path=path.relative_to(root).as_posix(),
            sha256=digest.hexdigest(),
            size_bytes=stat.st_size,
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            modified_ns=stat.st_mtime_ns,
            extracted_text=text,
            extraction_status=status,
        ))
    return records


def duplicate_groups(records: list[FileRecord]) -> list[list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if record.sha256:
            grouped[record.sha256].append(record.relative_path)
    return [sorted(paths) for paths in grouped.values() if len(paths) > 1]
