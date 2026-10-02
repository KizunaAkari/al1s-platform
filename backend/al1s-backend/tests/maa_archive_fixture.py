from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FixtureScript:
    name: str
    document: dict[str, Any]
    package_name: str = "com.example.game"
    display_name: str = "Example Game"


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_archive(*scripts: FixtureScript) -> bytes:
    categories = [
        {"package_name": package, "display_name": display_name}
        for package, display_name in sorted(
            {(script.package_name, script.display_name) for script in scripts}
        )
    ]
    categories_content = canonical_json(categories)
    report_content = canonical_json({"errors": [], "warnings": []})
    resource_bodies: dict[str, tuple[str, bytes]] = {}
    script_entries: list[dict[str, Any]] = []
    script_documents: list[tuple[str, bytes]] = []
    reference_count = 0

    for ordinal, script in enumerate(scripts, start=1):
        document_content = canonical_json(script.document)
        document_path = f"scripts/{ordinal:04d}.json"
        references: list[dict[str, Any]] = []
        for pointer, media_type, body in _data_url_references(script.document):
            digest = sha256(body)
            resource_path = _resource_path(digest, media_type)
            resource_bodies.setdefault(digest, (media_type, body))
            references.append(
                {
                    "mime": media_type,
                    "path": resource_path,
                    "pointer": pointer,
                    "sha256": digest,
                    "size": len(body),
                }
            )
        reference_count += len(references)
        script_entries.append(
            {
                "category_display_name": script.display_name,
                "category_package": script.package_name,
                "document_path": document_path,
                "document_sha256": sha256(document_content),
                "document_size": len(document_content),
                "name": script.name,
                "resources": references,
                "script_type": script.document["script_type"],
                "source_activity": None,
                "source_package": None,
            }
        )
        script_documents.append((document_path, document_content))

    resources = [
        {
            "mime_types": [media_type],
            "path": _resource_path(digest, media_type),
            "sha256": digest,
            "size": len(body),
        }
        for digest, (media_type, body) in sorted(resource_bodies.items())
    ]
    statistics = {
        "categories": len(categories),
        "resource_references": reference_count,
        "scripts": len(scripts),
        "unique_resources": len(resource_bodies),
    }
    manifest: dict[str, Any] = {
        "categories_path": "categories.json",
        "categories_sha256": sha256(categories_content),
        "generated_at": "2026-08-30T00:00:00Z",
        "report_path": "report.json",
        "report_sha256": sha256(report_content),
        "resources": resources,
        "schema": "al1s-script-archive/v1",
        "scripts": script_entries,
        "source": {"scope": "__global__"},
        "statistics": statistics,
    }
    logical = {
        "schema": manifest["schema"],
        "scope": manifest["source"]["scope"],
        "categories_sha256": manifest["categories_sha256"],
        "report_sha256": manifest["report_sha256"],
        "statistics": statistics,
        "scripts": script_entries,
        "resources": resources,
    }
    manifest["logical_sha256"] = sha256(canonical_json(logical))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", canonical_json(manifest))
        archive.writestr("categories.json", categories_content)
        archive.writestr("report.json", report_content)
        for path, body in script_documents:
            archive.writestr(path, body)
        for digest, (media_type, body) in resource_bodies.items():
            archive.writestr(_resource_path(digest, media_type), body)
    return output.getvalue()


def image_data_url(body: bytes = b"small-png-fixture") -> str:
    return "data:image/png;base64," + base64.b64encode(body).decode("ascii")


def _resource_path(digest: str, media_type: str) -> str:
    suffix = media_type.split("/", 1)[-1].replace("jpeg", "jpg")
    return f"resources/sha256/{digest}.{suffix}"


def _data_url_references(value: Any, pointer: str = "") -> list[tuple[str, str, bytes]]:
    references: list[tuple[str, str, bytes]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            references.extend(
                _data_url_references(nested, f"{pointer}/{_encode_pointer_token(str(key))}")
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            references.extend(_data_url_references(nested, f"{pointer}/{index}"))
    elif isinstance(value, str):
        match = re.fullmatch(r"data:([^;]+);base64,(.*)", value, re.DOTALL)
        if match is not None:
            references.append((pointer, match.group(1).lower(), base64.b64decode(match.group(2))))
    return references


def _encode_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
