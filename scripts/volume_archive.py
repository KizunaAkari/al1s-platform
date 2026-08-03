#!/usr/bin/env python3
"""Create and restore portable archives for the platform Docker data volume."""

from __future__ import annotations

import argparse
import json
import shutil
import tarfile
import uuid
from pathlib import Path, PurePosixPath


class ArchiveValidationError(ValueError):
    pass


def _validated_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArchiveValidationError(f"archive contains an unsafe path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise ArchiveValidationError(f"archive contains an unsupported entry: {member.name}")
    return members


def create_archive(root: Path, archive_path: Path) -> dict[str, int | str]:
    root = root.resolve()
    archive_path = archive_path.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"data volume directory does not exist: {root}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(f".{archive_path.name}.{uuid.uuid4().hex}.tmp")
    files = 0
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for item in sorted(root.iterdir(), key=lambda value: value.name):
                archive.add(item, arcname=item.name, recursive=True)
                files += 1
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"operation": "backup", "entries": files, "bytes": archive_path.stat().st_size}


def _remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def restore_archive(root: Path, archive_path: Path) -> dict[str, int | str]:
    root = root.resolve()
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"archive does not exist: {archive_path}")
    root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = _validated_members(archive)
        staging = root / f".restore-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            archive.extractall(staging, members=members, filter="data")
            for item in list(root.iterdir()):
                if item != staging:
                    _remove_entry(item)
            for item in list(staging.iterdir()):
                item.replace(root / item.name)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"operation": "restore", "entries": len(members), "bytes": archive_path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    result = (
        create_archive(args.root, args.archive)
        if args.operation == "backup"
        else restore_archive(args.root, args.archive)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
