"""Persist all selected archive versions inside the caller's finalization transaction."""

from datetime import datetime

from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import (
    ScriptBlobReference,
    ScriptQualificationReceipt,
    ScriptRecord,
    ScriptVersionRecord,
)


def persist_imported_scripts(
    uow: MaaUnitOfWork,
    scripts: list[ScriptRecord],
    versions: list[ScriptVersionRecord],
    references: list[ScriptBlobReference],
    qualifications: list[ScriptQualificationReceipt],
    replacements: dict[int, ScriptRecord],
    now: datetime,
) -> None:
    replaced_ids = {s.script_id for s in replacements.values()}
    new_scripts = [s for s in scripts if s.script_id not in replaced_ids]
    uow.scripts.add_many(new_scripts)
    uow.flush()
    uow.script_versions.add_many(versions)
    uow.flush()
    uow.script_versions.add_blob_references(references)
    uow.qualifications.add_many(qualifications)
    expected_updates = {
        item.script_id: item.script_version_id
        for item in versions
        if item.script_id not in replaced_ids
    }
    if uow.scripts.activate_imported_versions(expected_updates, now) != len(new_scripts):
        raise MaaDomainError("script_version_conflict", "Script versions changed concurrently", 409)
    expected = {s.script_id: s.row_version for s in replacements.values()}
    updates = [
        (v.script_id, expected[v.script_id], v.script_version_id)
        for v in versions
        if v.script_id in replaced_ids
    ]
    if uow.scripts.replace_imported_versions(updates, now) != len(updates):
        raise MaaDomainError(
            "archive_overwrite_stale",
            "A confirmed script changed; confirm again",
            409,
        )
