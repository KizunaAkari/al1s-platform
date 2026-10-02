#!/usr/bin/env python3
"""Export and validate the only legacy asset allowed into AL-1S next.

The archive is intentionally independent from the legacy platform package.  It
reads two legacy SQLite tables in query-only mode, preserves each script JSON
document, and creates content-addressed copies of embedded data URL resources.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ARCHIVE_SCHEMA = "al1s-script-archive/v1"
GLOBAL_SCRIPT_SCOPE = "__global__"
REQUIRED_TABLE_COLUMNS = {
    "scripts": {
        "agent_id",
        "name",
        "content",
        "category_package",
        "source_package",
        "source_activity",
    },
    "script_categories": {"agent_id", "package_name", "display_name"},
}
MAX_SCRIPT_BYTES = 16 * 1024 * 1024
MAX_RESOURCE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*);base64,(?P<data>.*)$",
    re.DOTALL,
)
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "application/json": ".json",
    "text/plain": ".txt",
}


class ArchiveError(Exception):
    """Expected data, validation, or path failure with a stable error code."""

    def __init__(self, code: str, message: str, *, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def walk_strings(value: Any, pointer: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key in sorted(value):
            child_pointer = f"{pointer}/{pointer_escape(str(key))}"
            yield from walk_strings(value[key], child_pointer)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_strings(item, f"{pointer}/{index}")
    elif isinstance(value, str):
        yield pointer, value


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ArchiveError("invalid_json_pointer", "JSON Pointer must start with '/'", context={"pointer": pointer})
    current = document
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise ArchiveError("missing_json_pointer", "JSON Pointer list index does not exist", context={"pointer": pointer})
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ArchiveError("missing_json_pointer", "JSON Pointer does not exist", context={"pointer": pointer})
    return current


def decode_data_url(value: str, *, pointer: str, script_name: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.fullmatch(value)
    if not match:
        raise ArchiveError(
            "invalid_data_url",
            "Embedded data URL must use a MIME type and base64 payload",
            context={"script": script_name, "pointer": pointer},
        )
    try:
        payload = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArchiveError(
            "invalid_base64",
            "Embedded resource is not valid base64",
            context={"script": script_name, "pointer": pointer},
        ) from exc
    if len(payload) > MAX_RESOURCE_BYTES:
        raise ArchiveError(
            "resource_too_large",
            "Embedded resource exceeds the per-resource limit",
            context={"script": script_name, "pointer": pointer, "size": len(payload)},
        )
    return match.group("mime").lower(), payload


def safe_resource_path(digest: str, mime: str) -> str:
    extension = MIME_EXTENSIONS.get(mime, ".bin")
    return f"resources/sha256/{digest}{extension}"


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.create_system = 3
    return info


def write_entry(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    archive.writestr(zip_info(name), content, compresslevel=9)


def require_schema(connection: sqlite3.Connection) -> None:
    for table, required in REQUIRED_TABLE_COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        available = {str(row[1]) for row in rows}
        missing = sorted(required - available)
        if missing:
            raise ArchiveError(
                "legacy_schema_incompatible",
                "Legacy database is missing required columns",
                context={"table": table, "missing": missing},
            )


def logical_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest["schema"],
        "scope": manifest["source"]["scope"],
        "categories_sha256": manifest["categories_sha256"],
        "report_sha256": manifest["report_sha256"],
        "statistics": manifest["statistics"],
        "scripts": manifest["scripts"],
        "resources": manifest["resources"],
    }


def export_archive(database: Path, output: Path) -> dict[str, Any]:
    database = database.resolve()
    output = output.resolve()
    if not database.is_file():
        raise ArchiveError("database_not_found", "Legacy database does not exist", context={"database": str(database)})
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with closing(sqlite3.connect(str(database))) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            require_schema(connection)
            categories = [
                {
                    "package_name": row["package_name"],
                    "display_name": row["display_name"],
                }
                for row in connection.execute(
                    """SELECT package_name, display_name
                    FROM script_categories
                    WHERE agent_id=?
                    ORDER BY display_name, package_name""",
                    (GLOBAL_SCRIPT_SCOPE,),
                )
            ]
            category_names = {item["package_name"]: item["display_name"] for item in categories}
            categories_content = canonical_json(categories)

            handle, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
            os.close(handle)
            temporary_path = Path(temporary_name)

            scripts: list[dict[str, Any]] = []
            resources: dict[str, dict[str, Any]] = {}
            action_counts: Counter[str] = Counter()
            script_type_counts: Counter[str] = Counter()
            resource_reference_count = 0

            with zipfile.ZipFile(temporary_path, "w", allowZip64=True) as archive:
                write_entry(archive, "categories.json", categories_content)
                cursor = connection.execute(
                    """SELECT name, content, category_package, source_package, source_activity
                    FROM scripts
                    WHERE agent_id=?
                    ORDER BY name""",
                    (GLOBAL_SCRIPT_SCOPE,),
                )
                for index, row in enumerate(cursor, start=1):
                    name = str(row["name"])
                    raw_content = str(row["content"])
                    if len(raw_content.encode("utf-8")) > MAX_SCRIPT_BYTES:
                        raise ArchiveError("script_too_large", "Script exceeds the per-script limit", context={"script": name})
                    try:
                        document = json.loads(raw_content)
                    except json.JSONDecodeError as exc:
                        raise ArchiveError(
                            "invalid_script_json",
                            "Script content is not valid JSON",
                            context={"script": name, "line": exc.lineno, "column": exc.colno},
                        ) from exc
                    if not isinstance(document, dict):
                        raise ArchiveError("invalid_script_document", "Script JSON root must be an object", context={"script": name})

                    document_content = canonical_json(document)
                    document_path = f"scripts/{index:04d}.json"
                    write_entry(archive, document_path, document_content)
                    script_references: list[dict[str, Any]] = []
                    for pointer, value in walk_strings(document):
                        if not value.startswith("data:"):
                            continue
                        mime, payload = decode_data_url(value, pointer=pointer, script_name=name)
                        digest = sha256_bytes(payload)
                        resource = resources.get(digest)
                        if resource is None:
                            resource_path = safe_resource_path(digest, mime)
                            resource = {
                                "sha256": digest,
                                "path": resource_path,
                                "size": len(payload),
                                "mime_types": [mime],
                            }
                            resources[digest] = resource
                            write_entry(archive, resource_path, payload)
                        elif mime not in resource["mime_types"]:
                            resource["mime_types"].append(mime)
                            resource["mime_types"].sort()
                        script_references.append(
                            {
                                "pointer": pointer,
                                "mime": mime,
                                "size": len(payload),
                                "sha256": digest,
                                "path": resource["path"],
                            }
                        )
                        resource_reference_count += 1

                    script_type = str(document.get("script_type") or "standard")
                    script_type_counts[script_type] += 1
                    for step in document.get("steps", []):
                        if isinstance(step, dict) and isinstance(step.get("action"), str):
                            action_counts[step["action"]] += 1
                    category_package = row["category_package"] or None
                    scripts.append(
                        {
                            "name": name,
                            "script_type": script_type,
                            "category_package": category_package,
                            "category_display_name": category_names.get(category_package),
                            "source_package": row["source_package"] or None,
                            "source_activity": row["source_activity"] or None,
                            "document_path": document_path,
                            "document_sha256": sha256_bytes(document_content),
                            "document_size": len(document_content),
                            "resources": script_references,
                        }
                    )

                statistics = {
                    "scripts": len(scripts),
                    "categories": len(categories),
                    "resource_references": resource_reference_count,
                    "unique_resources": len(resources),
                    "script_types": dict(sorted(script_type_counts.items())),
                    "actions": dict(sorted(action_counts.items())),
                }
                report = {"status": "ok", "errors": [], "warnings": [], "statistics": statistics}
                report_content = canonical_json(report)
                write_entry(archive, "report.json", report_content)
                manifest: dict[str, Any] = {
                    "schema": ARCHIVE_SCHEMA,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": {"type": "legacy-sqlite", "scope": GLOBAL_SCRIPT_SCOPE},
                    "categories_path": "categories.json",
                    "categories_sha256": sha256_bytes(categories_content),
                    "report_path": "report.json",
                    "report_sha256": sha256_bytes(report_content),
                    "statistics": statistics,
                    "scripts": scripts,
                    "resources": [resources[key] for key in sorted(resources)],
                }
                manifest["logical_sha256"] = sha256_bytes(canonical_json(logical_payload(manifest)))
                write_entry(archive, "manifest.json", canonical_json(manifest))

        summary = validate_archive(temporary_path)
        os.replace(temporary_path, output)
        temporary_path = None
        summary["archive_path"] = str(output)
        summary["archive_sha256"] = sha256_file(output)
        return summary
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def safe_zip_name(info: zipfile.ZipInfo) -> None:
    name = info.filename
    path = PurePosixPath(name)
    mode = (info.external_attr >> 16) & 0xFFFF
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or stat.S_ISLNK(mode)
    ):
        raise ArchiveError("unsafe_archive_path", "Archive contains an unsafe path", context={"path": name})


def read_entry(archive: zipfile.ZipFile, name: str, *, maximum: int = MAX_SCRIPT_BYTES) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ArchiveError("missing_archive_entry", "Archive entry is missing", context={"path": name}) from exc
    if info.file_size > maximum:
        raise ArchiveError("archive_entry_too_large", "Archive entry exceeds its limit", context={"path": name, "size": info.file_size})
    return archive.read(info)


def parse_json_entry(archive: zipfile.ZipFile, name: str, *, maximum: int = MAX_SCRIPT_BYTES) -> tuple[Any, bytes]:
    content = read_entry(archive, name, maximum=maximum)
    try:
        return json.loads(content), content
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("invalid_archive_json", "Archive JSON entry is invalid", context={"path": name}) from exc


def validate_archive(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise ArchiveError("archive_not_found", "Archive does not exist", context={"archive": str(path)})
    try:
        archive = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise ArchiveError("invalid_zip", "Archive is not a valid ZIP file") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError("too_many_archive_entries", "Archive contains too many entries", context={"count": len(infos)})
        names: set[str] = set()
        total_size = 0
        for info in infos:
            safe_zip_name(info)
            if info.filename in names:
                raise ArchiveError("duplicate_archive_entry", "Archive contains duplicate paths", context={"path": info.filename})
            names.add(info.filename)
            total_size += info.file_size
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ArchiveError("archive_too_large", "Archive uncompressed size exceeds the limit", context={"size": total_size})

        manifest, _ = parse_json_entry(archive, "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schema") != ARCHIVE_SCHEMA:
            raise ArchiveError("unsupported_archive_schema", "Archive schema is not supported", context={"schema": getattr(manifest, "get", lambda _k: None)("schema")})
        categories, categories_content = parse_json_entry(archive, str(manifest.get("categories_path")))
        report, report_content = parse_json_entry(archive, str(manifest.get("report_path")))
        if not isinstance(categories, list) or not isinstance(report, dict):
            raise ArchiveError("invalid_archive_metadata", "Categories or report has the wrong JSON shape")
        if sha256_bytes(categories_content) != manifest.get("categories_sha256"):
            raise ArchiveError("hash_mismatch", "Categories hash does not match", context={"path": manifest.get("categories_path")})
        if sha256_bytes(report_content) != manifest.get("report_sha256"):
            raise ArchiveError("hash_mismatch", "Report hash does not match", context={"path": manifest.get("report_path")})

        expected_names = {"manifest.json", str(manifest["categories_path"]), str(manifest["report_path"])}
        resource_by_hash: dict[str, dict[str, Any]] = {}
        for resource in manifest.get("resources", []):
            if not isinstance(resource, dict):
                raise ArchiveError("invalid_resource_entry", "Resource manifest entry must be an object")
            digest = str(resource.get("sha256") or "")
            resource_path = str(resource.get("path") or "")
            content = read_entry(archive, resource_path, maximum=MAX_RESOURCE_BYTES)
            if sha256_bytes(content) != digest or len(content) != resource.get("size"):
                raise ArchiveError("hash_mismatch", "Resource hash or size does not match", context={"path": resource_path})
            if digest in resource_by_hash:
                raise ArchiveError("duplicate_resource_hash", "Resource hash appears more than once", context={"sha256": digest})
            resource_by_hash[digest] = resource
            expected_names.add(resource_path)

        script_names: set[str] = set()
        reference_count = 0
        for script in manifest.get("scripts", []):
            if not isinstance(script, dict):
                raise ArchiveError("invalid_script_entry", "Script manifest entry must be an object")
            script_name = str(script.get("name") or "")
            if not script_name or script_name in script_names:
                raise ArchiveError("duplicate_script_name", "Script name is empty or duplicated", context={"script": script_name})
            script_names.add(script_name)
            document_path = str(script.get("document_path") or "")
            document, document_content = parse_json_entry(archive, document_path)
            if not isinstance(document, dict):
                raise ArchiveError("invalid_script_document", "Archived script root must be an object", context={"script": script_name})
            if sha256_bytes(document_content) != script.get("document_sha256") or len(document_content) != script.get("document_size"):
                raise ArchiveError("hash_mismatch", "Script document hash or size does not match", context={"script": script_name})
            expected_names.add(document_path)
            for reference in script.get("resources", []):
                pointer = str(reference.get("pointer") or "")
                value = resolve_pointer(document, pointer)
                if not isinstance(value, str):
                    raise ArchiveError("resource_reference_not_string", "Resource pointer does not reference a string", context={"script": script_name, "pointer": pointer})
                mime, payload = decode_data_url(value, pointer=pointer, script_name=script_name)
                digest = sha256_bytes(payload)
                if (
                    digest != reference.get("sha256")
                    or len(payload) != reference.get("size")
                    or mime != reference.get("mime")
                    or digest not in resource_by_hash
                    or reference.get("path") != resource_by_hash[digest].get("path")
                ):
                    raise ArchiveError("resource_reference_mismatch", "Script resource reference does not match its content", context={"script": script_name, "pointer": pointer})
                reference_count += 1

        statistics = manifest.get("statistics") or {}
        if (
            statistics.get("scripts") != len(script_names)
            or statistics.get("categories") != len(categories)
            or statistics.get("resource_references") != reference_count
            or statistics.get("unique_resources") != len(resource_by_hash)
        ):
            raise ArchiveError("statistics_mismatch", "Archive statistics do not match its contents")
        if sha256_bytes(canonical_json(logical_payload(manifest))) != manifest.get("logical_sha256"):
            raise ArchiveError("logical_hash_mismatch", "Archive logical digest does not match")
        unexpected = sorted(names - expected_names)
        missing = sorted(expected_names - names)
        if unexpected or missing:
            raise ArchiveError("archive_entry_set_mismatch", "Archive contains missing or unlisted entries", context={"missing": missing, "unexpected": unexpected})

        return {
            "status": "ok",
            "schema": ARCHIVE_SCHEMA,
            "logical_sha256": manifest["logical_sha256"],
            "statistics": statistics,
            "scripts": [
                {
                    "name": item["name"],
                    "script_type": item["script_type"],
                    "category": item.get("category_display_name") or item.get("category_package"),
                }
                for item in manifest["scripts"]
            ],
        }


def inspect_archive(path: Path) -> dict[str, Any]:
    result = validate_archive(path)
    result["archive_path"] = str(path.resolve())
    result["archive_sha256"] = sha256_file(path.resolve())
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="export legacy SQLite scripts")
    export_parser.add_argument("--database", required=True, type=Path)
    export_parser.add_argument("--output", required=True, type=Path)
    validate_parser = subparsers.add_parser("validate", help="validate an archive")
    validate_parser.add_argument("archive", type=Path)
    inspect_parser = subparsers.add_parser("inspect", help="show a validated archive summary")
    inspect_parser.add_argument("archive", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            result = export_archive(args.database, args.output)
        elif args.command == "validate":
            result = validate_archive(args.archive)
        else:
            result = inspect_archive(args.archive)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ArchiveError as exc:
        print(json.dumps({"status": "error", "error": exc.as_dict()}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"status": "error", "error": {"code": "io_error", "message": str(exc)}}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - final CLI safety net
        print(json.dumps({"status": "error", "error": {"code": "internal_error", "message": type(exc).__name__}}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
