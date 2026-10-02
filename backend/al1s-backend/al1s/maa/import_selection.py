"""Resolve archive targets before writing resources or changing saved scripts."""

from dataclasses import dataclass
from typing import cast

from al1s.maa.archive import ParsedScriptArchive
from al1s.maa.errors import MaaDomainError
from al1s.maa.naming import normalize_asset_name
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import ApplicationRecord, ScriptRecord, ScriptType


@dataclass(frozen=True)
class ImportSelection:
    applications: dict[str, ApplicationRecord]
    replacements: dict[int, ScriptRecord]


def _applications(
    uow: MaaUnitOfWork,
    parsed: ParsedScriptArchive,
) -> dict[str, ApplicationRecord]:
    if not 0 < len(parsed.scripts) <= 1000:
        raise MaaDomainError("archive_script_count", "Archive must contain 1 to 1000 scripts", 422)
    if any(s.application_package is None for s in parsed.scripts):
        raise MaaDomainError("archive_category_missing", "Application package is required", 422)
    packages = {s.application_package for s in parsed.scripts if s.application_package is not None}
    applications = {}
    grouped = uow.applications.find_import_targets(tuple(p for p in packages if p))
    for package in packages:
        choices = grouped.get(package, [])
        selected = parsed.target_applications.get(str(package))
        if selected:
            choices = [a for a in choices if a.application_id == selected]
        if len(choices) != 1:
            raise MaaDomainError(
                "archive_application_selection_required",
                "Select one existing category matching the application package",
                409,
                context={
                    "application_package": package,
                    "application_ids": [str(a.application_id) for a in choices],
                },
            )
        applications[package] = choices[0]
    return applications


def _target(
    kind: ScriptType,
    named: ScriptRecord | None,
    boundary: ScriptRecord | None,
) -> ScriptRecord | None:
    if kind is ScriptType.STANDARD:
        raise MaaDomainError("archive_script_type_unsupported", "Normal scripts are removed", 422)
    if named and named.script_type is not kind:
        raise MaaDomainError("archive_script_type_conflict", "Name belongs to another type", 409)
    if kind is ScriptType.MODULE_PROCESS:
        return named
    if boundary is None:
        raise MaaDomainError(
            "archive_boundary_missing", "Category start/end script is missing", 409
        )
    return boundary


def resolve_import(uow: MaaUnitOfWork, parsed: ParsedScriptArchive) -> ImportSelection:
    applications = _applications(uow, parsed)
    keys = [
        (
            applications[cast(str, s.application_package)].application_id,
            normalize_asset_name(s.name, asset_kind="script"),
        )
        for s in parsed.scripts
    ]
    names = uow.scripts.find_active_by_keys(keys, for_update=True)
    boundaries = uow.scripts.find_import_boundaries(
        tuple(a.application_id for a in applications.values())
    )
    replacements = {}
    seen = set()
    warnings = []
    for script, key in zip(parsed.scripts, keys, strict=True):
        kind = ScriptType(script.script_type)
        target = _target(kind, names.get(key), boundaries.get((key[0], kind)))
        identity = target.script_id if target else key
        if identity in seen:
            raise MaaDomainError(
                "archive_duplicate_target", "Multiple scripts target one slot", 422
            )
        seen.add(identity)
        if target:
            replacements[script.ordinal] = target
            warnings.append(
                {
                    "script_id": str(target.script_id),
                    "row_version": target.row_version,
                    "application_id": str(target.application_id),
                    "target_name": target.name,
                    "incoming_name": script.name,
                    "script_type": kind.value,
                }
            )
    expected = {s.script_id: s.row_version for s in replacements.values()}
    if expected != parsed.confirmed_overwrites:
        raise MaaDomainError(
            "archive_overwrite_confirmation_required",
            "Confirm these exact targets and versions; previous confirmation may be stale",
            409,
            context={"overwrites": warnings},
        )
    return ImportSelection(applications, replacements)
