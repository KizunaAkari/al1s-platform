"""Build portable script archives for the Maa library export endpoint."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from al1s.maa.archive import (
    MAX_RESOURCE_BYTES,
    MAX_SCRIPT_BYTES,
    PORTABLE_ARCHIVE_SCHEMA,
    STRATEGY_ARCHIVE_SCHEMA,
)
from al1s.maa.archive_format import (
    canonical_json as _canonical_json,
)
from al1s.maa.archive_format import (
    logical_payload as _logical_payload,
)
from al1s.maa.archive_upload import (
    MAX_EXPANDED_BYTES,
    MAX_UPLOAD_BYTES,
    validate_upload_size,
)

MAX_ARCHIVE_ENTRIES = 4096

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*);"
    r"base64,(?P<data>.*)$",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ExportScript:
    source_script_id: str
    name: str
    package_name: str
    display_name: str
    document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Resource:
    mime: str
    body: bytes


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _decode_data_url(value: str, pointer: str) -> tuple[str, bytes]:
    match = _DATA_URL_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid data URL at {pointer or '/'}")

    try:
        body = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 data URL at {pointer or '/'}") from exc

    if len(body) > MAX_RESOURCE_BYTES:
        raise ValueError(f"Resource at {pointer or '/'} exceeds {MAX_RESOURCE_BYTES} bytes")
    return match.group("mime"), body


def _iter_resources(
    value: Any,
    pointer: str,
) -> Iterator[tuple[str, str, bytes]]:
    if isinstance(value, dict):
        if "$blob" in value:
            raise ValueError(f"Legacy $blob resource at {pointer or '/'} is not allowed")
        for key, nested in value.items():
            child_pointer = f"{pointer}/{_json_pointer_token(str(key))}"
            yield from _iter_resources(nested, child_pointer)
        return

    if isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _iter_resources(nested, f"{pointer}/{index}")
        return

    if isinstance(value, str) and value.startswith("data:"):
        mime, body = _decode_data_url(value, pointer)
        yield pointer, mime, body


def _canonical_source_id(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid source_script_id: {value!r}") from exc


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _resource_path(digest: str, mime: str) -> str:
    suffix = mime.split("/", 1)[1]
    if suffix == "jpeg":
        suffix = "jpg"
    return f"resources/sha256/{digest}.{suffix}"


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    return info


def build_script_archive(
    scripts: Sequence[ExportScript],
    *,
    strategy: dict[str, Any] | None = None,
) -> bytes:
    """Build a deterministic-layout v2 archive from exported script documents."""

    normalized_scripts: list[
        tuple[str, str, str, str, dict[str, Any], bytes, list[dict[str, Any]]]
    ] = []
    source_ids: set[str] = set()
    script_keys: set[tuple[str, str]] = set()
    categories: dict[str, str] = {}
    resources: dict[str, _Resource] = {}
    document_total_size = 0
    resource_total_size = 0

    for script in scripts:
        script_ordinal = len(normalized_scripts) + 1
        if 3 + script_ordinal + len(resources) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"Archive has more than {MAX_ARCHIVE_ENTRIES} entries")

        source_id = _canonical_source_id(script.source_script_id)
        if source_id in source_ids:
            raise ValueError(f"Duplicate source_script_id: {source_id}")
        source_ids.add(source_id)

        name = _required_text(script.name, "name")
        package_name = _required_text(script.package_name, "package_name")
        display_name = _required_text(script.display_name, "display_name")
        script_key = (package_name, name.casefold())
        if script_key in script_keys:
            raise ValueError(f"Duplicate script name in package: {package_name}/{name}")
        script_keys.add(script_key)

        previous_display_name = categories.get(package_name)
        if previous_display_name is not None and previous_display_name != display_name:
            raise ValueError(f"Package has conflicting display names: {package_name}")
        categories[package_name] = display_name

        if not isinstance(script.document, dict):
            raise ValueError(f"Document for {name} must be an object")

        document_body = _canonical_json(script.document)
        if len(document_body) > MAX_SCRIPT_BYTES:
            raise ValueError(f"Document for {name} exceeds {MAX_SCRIPT_BYTES} bytes")
        document_total_size += len(document_body)
        if document_total_size + resource_total_size > MAX_EXPANDED_BYTES:
            raise ValueError(f"Archive expands beyond {MAX_EXPANDED_BYTES} bytes")

        script_resources: list[dict[str, Any]] = []
        for pointer, mime, body in _iter_resources(script.document, ""):
            digest = _sha256(body)
            previous = resources.get(digest)
            if previous is not None and previous.mime != mime:
                raise ValueError(f"Resource digest has conflicting MIME types: {digest}")
            if previous is None:
                next_resource_total_size = resource_total_size + len(body)
                if document_total_size + next_resource_total_size > MAX_EXPANDED_BYTES:
                    raise ValueError(f"Archive expands beyond {MAX_EXPANDED_BYTES} bytes")
                if 3 + script_ordinal + len(resources) + 1 > MAX_ARCHIVE_ENTRIES:
                    raise ValueError(f"Archive has more than {MAX_ARCHIVE_ENTRIES} entries")
                resources[digest] = _Resource(mime=mime, body=body)
                resource_total_size = next_resource_total_size
            script_resources.append(
                {
                    "mime": mime,
                    "path": _resource_path(digest, mime),
                    "pointer": pointer,
                    "sha256": digest,
                    "size": len(body),
                }
            )

        normalized_scripts.append(
            (
                source_id,
                name,
                package_name,
                display_name,
                script.document,
                document_body,
                script_resources,
            )
        )

    category_entries = [
        {"display_name": display_name, "package_name": package_name}
        for package_name, display_name in sorted(categories.items())
    ]

    script_entries: list[dict[str, Any]] = []
    document_entries: list[tuple[str, bytes]] = []
    for ordinal, (
        source_id,
        name,
        package_name,
        display_name,
        document,
        document_body,
        script_resources,
    ) in enumerate(normalized_scripts, start=1):
        document_path = f"scripts/{ordinal:04d}.json"
        script_type = document.get("script_type")
        if not isinstance(script_type, str) or not script_type:
            raise ValueError(f"Document for {name} must contain script_type")
        script_entries.append(
            {
                "category_display_name": display_name,
                "category_package": package_name,
                "document_path": document_path,
                "document_sha256": _sha256(document_body),
                "document_size": len(document_body),
                "name": name,
                "resources": script_resources,
                "script_type": script_type,
                "source_activity": None,
                "source_package": None,
                "source_script_id": source_id,
            }
        )
        document_entries.append((document_path, document_body))

    resource_entries: list[dict[str, Any]] = []
    resource_bodies: list[tuple[str, bytes]] = []
    for digest, resource in sorted(resources.items()):
        path = _resource_path(digest, resource.mime)
        resource_entries.append(
            {
                "mime_types": [resource.mime],
                "path": path,
                "sha256": digest,
                "size": len(resource.body),
            }
        )
        resource_bodies.append((path, resource.body))

    categories_body = _canonical_json(category_entries)
    report_body = _canonical_json({"errors": [], "warnings": []})
    manifest: dict[str, Any] = {
        "categories_path": "categories.json",
        "categories_sha256": _sha256(categories_body),
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "report_path": "report.json",
        "report_sha256": _sha256(report_body),
        "resources": resource_entries,
        "schema": STRATEGY_ARCHIVE_SCHEMA if strategy is not None else PORTABLE_ARCHIVE_SCHEMA,
        **({"strategy": strategy} if strategy is not None else {}),
        "scripts": script_entries,
        "source": {"scope": "__global__"},
        "statistics": {
            "categories": len(category_entries),
            "resource_references": sum(
                len(script_entry["resources"]) for script_entry in script_entries
            ),
            "scripts": len(script_entries),
            "unique_resources": len(resource_entries),
        },
    }
    manifest["logical_sha256"] = _sha256(_canonical_json(_logical_payload(manifest)))
    manifest_body = _canonical_json(manifest)

    entry_count = 3 + len(document_entries) + len(resource_bodies)
    if entry_count > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"Archive has more than {MAX_ARCHIVE_ENTRIES} entries")

    uncompressed_size = len(manifest_body) + len(categories_body) + len(report_body)
    uncompressed_size += sum(len(body) for _, body in document_entries)
    uncompressed_size += sum(len(body) for _, body in resource_bodies)
    if uncompressed_size > MAX_EXPANDED_BYTES:
        raise ValueError(f"Archive expands beyond {MAX_EXPANDED_BYTES} bytes")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, body in (
            ("manifest.json", manifest_body),
            ("categories.json", categories_body),
            ("report.json", report_body),
            *document_entries,
            *resource_bodies,
        ):
            archive.writestr(_zip_info(path), body)

    content = output.getvalue()
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Archive exceeds {MAX_UPLOAD_BYTES} bytes")
    validate_upload_size(content)
    return content
