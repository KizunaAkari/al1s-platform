from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from al1s.maa.archive import parse_script_archive
from al1s.maa.errors import ArchiveValidationError


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _archive(consecutive_match_count: int | None = None) -> bytes:
    resource = b"small-png-fixture"
    resource_hash = _sha(resource)
    resource_path = f"resources/sha256/{resource_hash}.png"
    data_url = "data:image/png;base64," + base64.b64encode(resource).decode("ascii")
    categories = [{"package_name": "com.example.game", "display_name": "示例游戏"}]
    report = {"errors": [], "warnings": []}
    document = {
        "version": 2,
        "script_type": "module_process",
        "steps": [{"action": "wait_image", "template_base64": data_url}],
        "cleanup_on_finish": False,
        "target": {"agent_id": "legacy-agent", "device_serial": "legacy-device"},
    }
    if consecutive_match_count is not None:
        document["steps"][0]["consecutive_match_count"] = consecutive_match_count
    document_content = _canonical(document)
    categories_content = _canonical(categories)
    report_content = _canonical(report)
    resources = [
        {
            "mime_types": ["image/png"],
            "path": resource_path,
            "sha256": resource_hash,
            "size": len(resource),
        }
    ]
    scripts = [
        {
            "category_display_name": "示例游戏",
            "category_package": "com.example.game",
            "document_path": "scripts/0001.json",
            "document_sha256": _sha(document_content),
            "document_size": len(document_content),
            "name": "示例脚本.json",
            "resources": [
                {
                    "mime": "image/png",
                    "path": resource_path,
                    "pointer": "/steps/0/template_base64",
                    "sha256": resource_hash,
                    "size": len(resource),
                }
            ],
            "script_type": "module_process",
            "source_activity": None,
            "source_package": None,
        }
    ]
    statistics = {
        "categories": 1,
        "resource_references": 1,
        "scripts": 1,
        "unique_resources": 1,
    }
    manifest: dict[str, Any] = {
        "categories_path": "categories.json",
        "categories_sha256": _sha(categories_content),
        "generated_at": "2026-08-30T00:00:00Z",
        "report_path": "report.json",
        "report_sha256": _sha(report_content),
        "resources": resources,
        "schema": "al1s-script-archive/v1",
        "scripts": scripts,
        "source": {"scope": "__global__"},
        "statistics": statistics,
    }
    logical = {
        "schema": manifest["schema"],
        "scope": manifest["source"]["scope"],
        "categories_sha256": manifest["categories_sha256"],
        "report_sha256": manifest["report_sha256"],
        "statistics": statistics,
        "scripts": scripts,
        "resources": resources,
    }
    manifest["logical_sha256"] = _sha(_canonical(logical))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", _canonical(manifest))
        archive.writestr("categories.json", categories_content)
        archive.writestr("report.json", report_content)
        archive.writestr("scripts/0001.json", document_content)
        archive.writestr(resource_path, resource)
    return output.getvalue()


def test_parses_and_verifies_a_complete_archive() -> None:
    content = _archive()
    parsed = parse_script_archive(content)
    assert parsed.schema == "al1s-script-archive/v1"
    assert parsed.archive_sha256 == _sha(content)
    assert parsed.categories == (("com.example.game", "示例游戏"),)
    assert len(parsed.scripts) == 1
    assert parsed.scripts[0].name == "示例脚本.json"
    assert parsed.scripts[0].application_package == "com.example.game"
    assert parsed.resource_reference_count == 1
    assert len(parsed.resources) == 1


def test_image_stability_is_retained_when_importing_an_exported_document() -> None:
    parsed = parse_script_archive(_archive(consecutive_match_count=3))
    assert parsed.scripts[0].document["steps"][0]["consecutive_match_count"] == 3
    assert parsed.resource_reference_count == 1


def test_rejects_tampered_resource() -> None:
    source = zipfile.ZipFile(io.BytesIO(_archive()))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename.startswith("resources/"):
                body += b"tampered"
            target.writestr(info.filename, body)
    with pytest.raises(ArchiveValidationError) as captured:
        parse_script_archive(output.getvalue())
    assert captured.value.code == "hash_mismatch"


def test_real_migration_archive_matches_recorded_baseline_when_available() -> None:
    archive_path = Path(__file__).parents[2] / "backups/migration/al1s-script-archive-v1.zip"
    if not archive_path.is_file():
        pytest.skip("workspace migration archive is outside the backend build context")
    parsed = parse_script_archive(archive_path.read_bytes())
    assert parsed.logical_sha256 == (
        "969a673bef2832df497575d3820f61cec12aa2c37c01ff4e3aeee1bcd0173795"
    )
    assert len(parsed.categories) == 2
    assert len(parsed.scripts) == 23
    assert parsed.resource_reference_count == 289
    assert len(parsed.resources) == 240
