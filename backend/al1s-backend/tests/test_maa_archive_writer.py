from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from al1s.maa import archive_upload, archive_writer
from al1s.maa.archive import parse_script_archive
from al1s.maa.archive_upload import MaaDomainError
from al1s.maa.archive_writer import ExportScript, build_script_archive

FIRST_ID = "11111111-1111-4111-8111-111111111111"
SECOND_ID = "22222222-2222-4222-8222-222222222222"


def _data_url(body: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}"


def _script(
    source_script_id: str = FIRST_ID,
    *,
    name: str = "main",
    package_name: str = "com.example.one",
    display_name: str = "One",
    image: str | None = None,
) -> ExportScript:
    document: dict[str, object] = {
        "script_type": "MAA",
        "steps": [{"name": "tap", "template": image}],
        "unknown_field": {"keep": True},
    }
    return ExportScript(source_script_id, name, package_name, display_name, document)


def _manifest(content: bytes) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return json.loads(archive.read("manifest.json"))


def test_v2_round_trip_deduplicates_images_and_allows_same_name_across_packages() -> None:
    image = _data_url(b"same-image")
    scripts = [
        _script(image=image),
        _script(
            SECOND_ID,
            package_name="com.example.two",
            display_name="Two",
            image=image,
        ),
    ]

    content = build_script_archive(scripts)
    parsed = parse_script_archive(content)
    manifest = _manifest(content)

    assert manifest["schema"] == "al1s-script-archive/v2"
    assert [entry["source_script_id"] for entry in manifest["scripts"]] == [
        FIRST_ID,
        SECOND_ID,
    ]
    assert len(manifest["resources"]) == 1
    assert len(parsed.scripts) == 2
    assert [script.source_script_id for script in parsed.scripts] == [FIRST_ID, SECOND_ID]
    assert parsed.scripts[0].document == scripts[0].document
    assert parsed.scripts[1].document == scripts[1].document

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
    assert names[3:5] == ["scripts/0001.json", "scripts/0002.json"]
    assert all(name.startswith("resources/sha256/") for name in names[5:])


def test_logical_hash_is_deterministic_and_includes_source_identity() -> None:
    image = _data_url(b"stable")
    scripts = [_script(image=image)]

    first = parse_script_archive(build_script_archive(scripts))
    second = parse_script_archive(build_script_archive(scripts))
    changed_id = parse_script_archive(
        build_script_archive([_script(SECOND_ID, image=image)])
    )

    assert first.logical_sha256 == second.logical_sha256
    assert first.logical_sha256 != changed_id.logical_sha256


@pytest.mark.parametrize(
    "document",
    [
        {
            "script_type": "MAA",
            "image": {"$blob": "legacy-id"},
        },
        {"script_type": "MAA", "image": "data:image/png;base64,not-base64"},
        {"script_type": "MAA", "image": "data:image/png;base64,@@@@"},
        {"script_type": "MAA", "image": "data:invalid;base64,AA=="},
    ],
)
def test_rejects_legacy_blob_and_invalid_data_urls(document: dict[str, object]) -> None:
    script = ExportScript(FIRST_ID, "main", "com.example.one", "One", document)

    with pytest.raises(ValueError):
        build_script_archive([script])


def test_checks_script_expanded_and_entry_limits_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _data_url(b"large-enough")
    script = _script(image=image)

    monkeypatch.setattr(archive_writer, "MAX_SCRIPT_BYTES", 10)
    with pytest.raises(ValueError, match="Document"):
        build_script_archive([script])

    monkeypatch.setattr(archive_writer, "MAX_SCRIPT_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(archive_writer, "MAX_EXPANDED_BYTES", 10)
    with pytest.raises(ValueError, match="expands"):
        build_script_archive([script])

    monkeypatch.setattr(archive_writer, "MAX_EXPANDED_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(archive_writer, "MAX_ARCHIVE_ENTRIES", 3)
    with pytest.raises(ValueError, match="entries"):
        build_script_archive([script])


def test_final_archive_uses_current_upload_size_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_upload, "MAX_UPLOAD_BYTES", 1)

    with pytest.raises(MaaDomainError, match=r"32 MiB"):
        build_script_archive([_script()])
