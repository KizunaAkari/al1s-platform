from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from al1s.maa.archive_format import (
    ARCHIVE_SCHEMA as ARCHIVE_SCHEMA,
)
from al1s.maa.archive_format import (
    PORTABLE_ARCHIVE_SCHEMA as PORTABLE_ARCHIVE_SCHEMA,
)
from al1s.maa.archive_format import (
    STRATEGY_ARCHIVE_SCHEMA as STRATEGY_ARCHIVE_SCHEMA,
)
from al1s.maa.archive_format import (
    canonical_json as _canonical_json,
)
from al1s.maa.archive_format import (
    logical_payload as _logical_payload,
)
from al1s.maa.errors import ArchiveValidationError

MAX_SCRIPT_BYTES = 16 * 1024 * 1024
MAX_RESOURCE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*);base64,(?P<data>.*)$",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ArchiveResource:
    sha256: str
    media_type: str
    size_bytes: int
    path: str
    body: bytes


@dataclass(frozen=True, slots=True)
class ArchiveResourceReference:
    pointer: str
    sha256: str
    media_type: str
    size_bytes: int
    role: str


@dataclass(frozen=True, slots=True)
class ArchiveScript:
    ordinal: int
    name: str
    script_type: str
    application_package: str | None
    application_display_name: str | None
    document: dict[str, Any]
    resources: tuple[ArchiveResourceReference, ...]
    source_script_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedScriptArchive:
    schema: str
    logical_sha256: str
    archive_sha256: str
    categories: tuple[tuple[str, str], ...]
    scripts: tuple[ArchiveScript, ...]
    resources: dict[str, ArchiveResource]
    resource_reference_count: int
    strategy: dict[str, Any] | None = None
    target_applications: dict[str, UUID] = field(default_factory=dict)
    confirmed_overwrites: dict[UUID, int] = field(default_factory=dict)


def parse_script_archive(content: bytes) -> ParsedScriptArchive:
    try:
        return _parse_script_archive(content)
    except ArchiveValidationError:
        raise
    except (
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ) as exc:
        raise ArchiveValidationError(
            "malformed_archive", "Archive metadata or compressed content is malformed"
        ) from exc


def _parse_script_archive(content: bytes) -> ParsedScriptArchive:
    archive_sha256 = _sha256(content)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except zipfile.BadZipFile as exc:
        raise ArchiveValidationError("invalid_zip", "Archive is not a valid ZIP file") from exc

    with archive:
        names = _validate_entry_set_shape(archive)
        manifest = _read_json(archive, "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("schema") not in {
            ARCHIVE_SCHEMA,
            PORTABLE_ARCHIVE_SCHEMA,
            STRATEGY_ARCHIVE_SCHEMA,
        }:
            schema = manifest.get("schema") if isinstance(manifest, dict) else None
            raise ArchiveValidationError(
                "unsupported_archive_schema",
                "Archive schema is not supported",
                context={"schema": schema},
            )

        categories_path = _required_path(manifest, "categories_path")
        report_path = _required_path(manifest, "report_path")
        categories_content = _read_entry(archive, categories_path)
        report_content = _read_entry(archive, report_path)
        _require_hash(categories_content, manifest.get("categories_sha256"), categories_path)
        _require_hash(report_content, manifest.get("report_sha256"), report_path)
        categories_value = _decode_json(categories_content, categories_path)
        report_value = _decode_json(report_content, report_path)
        if not isinstance(categories_value, list) or not isinstance(report_value, dict):
            raise ArchiveValidationError(
                "invalid_archive_metadata", "Categories or report has the wrong JSON shape"
            )
        categories = _parse_categories(categories_value)

        expected_names = {"manifest.json", categories_path, report_path}
        resources = _parse_resources(archive, manifest, expected_names)
        scripts, reference_count = _parse_scripts(archive, manifest, resources, expected_names)
        _validate_statistics(
            manifest,
            len(categories),
            len(scripts),
            reference_count,
            len(resources),
        )
        logical = str(manifest.get("logical_sha256") or "")
        expected_logical = _sha256(_canonical_json(_logical_payload(manifest)))
        if logical != expected_logical:
            raise ArchiveValidationError(
                "logical_hash_mismatch", "Archive logical digest does not match"
            )
        unexpected = sorted(names - expected_names)
        missing = sorted(expected_names - names)
        if unexpected or missing:
            raise ArchiveValidationError(
                "archive_entry_set_mismatch",
                "Archive contains missing or unlisted entries",
                context={"missing": missing, "unexpected": unexpected},
            )

        return ParsedScriptArchive(
            schema=manifest["schema"],
            logical_sha256=logical,
            archive_sha256=archive_sha256,
            categories=categories,
            scripts=scripts,
            resources=resources,
            resource_reference_count=reference_count,
            strategy=_strategy_metadata(manifest, scripts),
        )


def replace_resource_pointer(document: dict[str, Any], pointer: str, value: Any) -> None:
    if not pointer.startswith("/"):
        raise ArchiveValidationError(
            "invalid_json_pointer", "JSON Pointer must start with '/'", context={"pointer": pointer}
        )
    parts = pointer[1:].split("/")
    current: Any = document
    for encoded in parts[:-1]:
        token = _decode_pointer_token(encoded)
        if isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ArchiveValidationError(
                "missing_json_pointer", "JSON Pointer does not exist", context={"pointer": pointer}
            )
    final = _decode_pointer_token(parts[-1])
    if isinstance(current, list) and final.isdigit() and int(final) < len(current):
        current[int(final)] = value
    elif isinstance(current, dict) and final in current:
        current[final] = value
    else:
        raise ArchiveValidationError(
            "missing_json_pointer", "JSON Pointer does not exist", context={"pointer": pointer}
        )


def _validate_entry_set_shape(archive: zipfile.ZipFile) -> set[str]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ArchiveValidationError(
            "too_many_archive_entries",
            "Archive contains too many entries",
            context={"count": len(infos)},
        )
    names: set[str] = set()
    total = 0
    for info in infos:
        _validate_zip_name(info)
        if info.filename in names:
            raise ArchiveValidationError(
                "duplicate_archive_entry",
                "Archive contains duplicate paths",
                context={"path": info.filename},
            )
        names.add(info.filename)
        total += info.file_size
    if total > MAX_ARCHIVE_BYTES:
        raise ArchiveValidationError(
            "archive_too_large",
            "Archive uncompressed size exceeds the limit",
            context={"size": total},
        )
    return names


def _validate_zip_name(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    mode = (info.external_attr >> 16) & 0xFFFF
    if (
        not info.filename
        or "\\" in info.filename
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or stat.S_ISLNK(mode)
    ):
        raise ArchiveValidationError(
            "unsafe_archive_path",
            "Archive contains an unsafe path",
            context={"path": info.filename},
        )


def _parse_categories(value: list[Any]) -> tuple[tuple[str, str], ...]:
    categories: list[tuple[str, str]] = []
    packages: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ArchiveValidationError(
                "invalid_category_entry", "Category manifest entry must be an object"
            )
        package = str(item.get("package_name") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        if not package or not display_name or package in packages:
            raise ArchiveValidationError(
                "invalid_category_entry",
                "Category package and display name must be unique and non-empty",
                context={"package": package},
            )
        packages.add(package)
        categories.append((package, display_name))
    return tuple(categories)


def _parse_resources(
    archive: zipfile.ZipFile, manifest: dict[str, Any], expected_names: set[str]
) -> dict[str, ArchiveResource]:
    raw_resources = manifest.get("resources")
    if not isinstance(raw_resources, list):
        raise ArchiveValidationError("invalid_resource_list", "Resources must be an array")
    resources: dict[str, ArchiveResource] = {}
    for raw in raw_resources:
        if not isinstance(raw, dict):
            raise ArchiveValidationError(
                "invalid_resource_entry", "Resource manifest entry must be an object"
            )
        digest = str(raw.get("sha256") or "")
        path = str(raw.get("path") or "")
        raw_media_types = raw.get("mime_types")
        media_types = (
            sorted({str(item).lower() for item in raw_media_types if str(item).strip()})
            if isinstance(raw_media_types, list)
            else []
        )
        if not media_types and raw.get("mime"):
            media_types = [str(raw["mime"]).lower()]
        media_type = media_types[0] if media_types else ""
        size = raw.get("size")
        body = _read_entry(archive, path, maximum=MAX_RESOURCE_BYTES)
        if not _valid_hash(digest) or _sha256(body) != digest or len(body) != size:
            raise ArchiveValidationError(
                "hash_mismatch", "Resource hash or size does not match", context={"path": path}
            )
        if not media_type or digest in resources:
            raise ArchiveValidationError(
                "duplicate_resource_hash",
                "Resource hash appears more than once",
                context={"sha256": digest},
            )
        resources[digest] = ArchiveResource(digest, media_type, len(body), path, body)
        expected_names.add(path)
    return resources


def _parse_scripts(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    resources: dict[str, ArchiveResource],
    expected_names: set[str],
) -> tuple[tuple[ArchiveScript, ...], int]:
    raw_scripts = manifest.get("scripts")
    if not isinstance(raw_scripts, list):
        raise ArchiveValidationError("invalid_script_list", "Scripts must be an array")
    result: list[ArchiveScript] = []
    names: set[tuple[str, str]] = set()
    identities: set[str] = set()
    reference_count = 0
    for ordinal, raw in enumerate(raw_scripts, start=1):
        if not isinstance(raw, dict):
            raise ArchiveValidationError(
                "invalid_script_entry", "Script manifest entry must be an object"
            )
        name = str(raw.get("name") or "").strip()
        key = (str(raw.get("category_package") or ""), name.casefold())
        if not name or key in names:
            raise ArchiveValidationError(
                "duplicate_script_name",
                "Script name is empty or duplicated",
                context={"script": name},
            )
        names.add(key)
        source_id = None
        if manifest["schema"] in {PORTABLE_ARCHIVE_SCHEMA, STRATEGY_ARCHIVE_SCHEMA}:
            source_id = str(UUID(str(raw.get("source_script_id"))))
            if source_id in identities:
                raise ArchiveValidationError(
                    "duplicate_script_identity",
                    "Script identity is duplicated",
                )
            identities.add(source_id)
        document_path = str(raw.get("document_path") or "")
        content = _read_entry(archive, document_path)
        _require_hash(content, raw.get("document_sha256"), document_path)
        if len(content) != raw.get("document_size"):
            raise ArchiveValidationError(
                "hash_mismatch", "Script document size does not match", context={"script": name}
            )
        document = _decode_json(content, document_path)
        if not isinstance(document, dict):
            raise ArchiveValidationError(
                "invalid_script_document",
                "Archived script root must be an object",
                context={"script": name},
            )
        expected_names.add(document_path)
        refs = _parse_references(document, raw, resources, name)
        reference_count += len(refs)
        result.append(
            ArchiveScript(
                ordinal=ordinal,
                name=name,
                script_type=str(raw.get("script_type") or ""),
                application_package=_optional_string(raw.get("category_package")),
                application_display_name=_optional_string(raw.get("category_display_name")),
                document=document,
                resources=refs,
                source_script_id=source_id,
            )
        )
    return tuple(result), reference_count


def _parse_references(
    document: dict[str, Any],
    script_entry: dict[str, Any],
    resources: dict[str, ArchiveResource],
    script_name: str,
) -> tuple[ArchiveResourceReference, ...]:
    raw_references = script_entry.get("resources")
    if not isinstance(raw_references, list):
        raise ArchiveValidationError(
            "invalid_resource_references", "Script resources must be an array"
        )
    references: list[ArchiveResourceReference] = []
    pointers: set[str] = set()
    for raw in raw_references:
        if not isinstance(raw, dict):
            raise ArchiveValidationError(
                "invalid_resource_reference", "Resource reference must be an object"
            )
        pointer = str(raw.get("pointer") or "")
        if pointer in pointers:
            raise ArchiveValidationError(
                "duplicate_resource_pointer",
                "Resource pointer appears more than once",
                context={"script": script_name, "pointer": pointer},
            )
        pointers.add(pointer)
        value = _resolve_pointer(document, pointer)
        if not isinstance(value, str):
            raise ArchiveValidationError(
                "resource_reference_not_string",
                "Resource pointer does not reference a string",
                context={"script": script_name, "pointer": pointer},
            )
        media_type, body = _decode_data_url(value, script_name, pointer)
        digest = _sha256(body)
        resource = resources.get(digest)
        if (
            resource is None
            or digest != raw.get("sha256")
            or len(body) != raw.get("size")
            or media_type != raw.get("mime")
            or raw.get("path") != resource.path
        ):
            raise ArchiveValidationError(
                "resource_reference_mismatch",
                "Script resource reference does not match its content",
                context={"script": script_name, "pointer": pointer},
            )
        references.append(
            ArchiveResourceReference(
                pointer=pointer,
                sha256=digest,
                media_type=media_type,
                size_bytes=len(body),
                role=_resource_role(pointer),
            )
        )
    return tuple(references)


def _validate_statistics(
    manifest: dict[str, Any], categories: int, scripts: int, references: int, resources: int
) -> None:
    statistics = manifest.get("statistics")
    if not isinstance(statistics, dict) or (
        statistics.get("categories") != categories
        or statistics.get("scripts") != scripts
        or statistics.get("resource_references") != references
        or statistics.get("unique_resources") != resources
    ):
        raise ArchiveValidationError(
            "statistics_mismatch", "Archive statistics do not match its contents"
        )


def _strategy_metadata(
    manifest: dict[str, Any],
    scripts: tuple[ArchiveScript, ...],
) -> dict[str, Any] | None:
    if manifest["schema"] != STRATEGY_ARCHIVE_SCHEMA:
        return None
    strategy = manifest.get("strategy")
    if not isinstance(strategy, dict) or set(strategy) != {
        "name",
        "modules",
        "default_parameters",
    }:
        raise ValueError("Invalid strategy metadata")
    if not isinstance(strategy["name"], str) or not 1 <= len(strategy["name"].strip()) <= 200:
        raise ValueError("Invalid strategy name")
    if not isinstance(strategy["default_parameters"], dict):
        raise ValueError("Invalid strategy parameters")
    modules = strategy["modules"]
    if not isinstance(modules, list) or not 2 <= len(modules) <= 1002:
        raise ValueError("Invalid strategy modules")
    available = {script.source_script_id: script for script in scripts}
    package = None
    for index, module in enumerate(modules):
        if not isinstance(module, dict) or set(module) != {"source_script_id", "wait_after_ms"}:
            raise ValueError("Invalid strategy module")
        script = available.get(module["source_script_id"])
        expected = (
            "module_start"
            if index == 0
            else ("module_end" if index == len(modules) - 1 else "module_process")
        )
        if script is None or script.script_type != expected:
            raise ValueError("Strategy module is missing or has the wrong role")
        if package is not None and package != script.application_package:
            raise ValueError("Strategy modules must use the same application")
        package = script.application_package
        wait = module["wait_after_ms"]
        if type(wait) is not int or not 0 <= wait <= 14_400_000:
            raise ValueError("Invalid strategy module wait")
        if index == len(modules) - 1 and wait != 0:
            raise ValueError("End script cannot have a following wait")
    return strategy


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ArchiveValidationError(
            "invalid_json_pointer", "JSON Pointer must start with '/'", context={"pointer": pointer}
        )
    current = document
    for encoded in pointer[1:].split("/"):
        token = _decode_pointer_token(encoded)
        if isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ArchiveValidationError(
                "missing_json_pointer", "JSON Pointer does not exist", context={"pointer": pointer}
            )
    return current


def _decode_data_url(value: str, script_name: str, pointer: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.fullmatch(value)
    if match is None:
        raise ArchiveValidationError(
            "invalid_data_url",
            "Embedded data URL must use a MIME type and base64 payload",
            context={"script": script_name, "pointer": pointer},
        )
    try:
        body = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArchiveValidationError(
            "invalid_base64",
            "Embedded resource is not valid base64",
            context={"script": script_name, "pointer": pointer},
        ) from exc
    if len(body) > MAX_RESOURCE_BYTES:
        raise ArchiveValidationError(
            "resource_too_large",
            "Embedded resource exceeds the per-resource limit",
            context={"script": script_name, "pointer": pointer, "size": len(body)},
        )
    return match.group("mime").lower(), body


def _read_json(archive: zipfile.ZipFile, name: str) -> Any:
    return _decode_json(_read_entry(archive, name), name)


def _read_entry(archive: zipfile.ZipFile, name: str, *, maximum: int = MAX_SCRIPT_BYTES) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ArchiveValidationError(
            "missing_archive_entry", "Archive entry is missing", context={"path": name}
        ) from exc
    if info.file_size > maximum:
        raise ArchiveValidationError(
            "archive_entry_too_large",
            "Archive entry exceeds its limit",
            context={"path": name, "size": info.file_size},
        )
    return archive.read(info)


def _decode_json(content: bytes, path: str) -> Any:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveValidationError(
            "invalid_archive_json", "Archive JSON entry is invalid", context={"path": path}
        ) from exc


def _required_path(manifest: dict[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise ArchiveValidationError(
            "invalid_archive_metadata", "Archive metadata path is missing", context={"field": key}
        )
    return value


def _require_hash(content: bytes, expected: Any, path: str) -> None:
    if not isinstance(expected, str) or _sha256(content) != expected:
        raise ArchiveValidationError(
            "hash_mismatch", "Archive entry hash does not match", context={"path": path}
        )


def _resource_role(pointer: str) -> str:
    token = _decode_pointer_token(pointer.rsplit("/", 1)[-1])
    role = token.removesuffix("_base64")
    return role[:64] or "resource"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _decode_pointer_token(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
