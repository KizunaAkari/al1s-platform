from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from al1s.execution.definitions import canonical_manifest_hash
from al1s.kernel.ports import BlobStore
from al1s.maa.archive import replace_resource_pointer
from al1s.maa.archive_writer import ExportScript, build_script_archive
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import ScriptType

MAX_EXPORT_SCRIPTS = 1000
MAX_RESOURCE_BYTES = 32 * 1024 * 1024


class MaaScriptExportService:
    """Capture immutable content in a short transaction; fetch S3 after closing it."""

    def __init__(self, uow_factory: Callable[[], MaaUnitOfWork], store: BlobStore,
                 *, clock: Callable[[], float] = time.monotonic):
        self._uow_factory, self._store = uow_factory, store
        self._clock = clock

    def _check_budget(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise MaaDomainError("archive_export_timeout", "Archive export timed out", 504)

    def export(self, script_id: UUID, version_id: UUID) -> bytes:
        return self._export({script_id: version_id})

    def export_strategy(self, strategy_id: UUID) -> bytes:
        deadline = self._clock() + 180
        with self._uow_factory() as uow:
            strategy = uow.strategies.get_active(strategy_id)
            if strategy is None or strategy.current_version_id is None:
                raise MaaDomainError("strategy_not_found", "Strategy was not found", 404)
            version = uow.strategy_versions.get(strategy.current_version_id)
            modules = uow.strategy_versions.list_modules(strategy.current_version_id)
            if version is None or not modules:
                raise MaaDomainError("strategy_version_missing", "Strategy is unavailable", 409)
            versions = uow.script_versions.find_by_ids([m.script_version_id for m in modules])
            roots: dict[UUID, UUID] = {}
            exported_modules = []
            for module in modules:
                script_version = versions.get(module.script_version_id)
                if script_version is None:
                    raise MaaDomainError("script_version_missing", "Module is unavailable", 409)
                identity = script_version.script_id
                if identity in roots and roots[identity] != module.script_version_id:
                    raise MaaDomainError(
                        "archive_version_conflict", "Multiple versions of one script are used", 409,
                    )
                roots[identity] = module.script_version_id
                exported_modules.append({
                    "source_script_id": str(identity), "wait_after_ms": module.wait_after_ms,
                })
            metadata = {"name": strategy.name, "modules": exported_modules,
                        "default_parameters": version.manifest.get("default_parameters", {})}
        return self._export(roots, metadata, deadline=deadline)

    def _export(self, roots: dict[UUID, UUID], strategy: dict[str, Any] | None = None,
                *, deadline: float | None = None) -> bytes:
        deadline = self._clock() + 180 if deadline is None else deadline
        self._check_budget(deadline)
        script_id, version_id = next(iter(roots.items()))
        with self._uow_factory() as uow:
            root = uow.scripts.get_active(script_id)
            version = uow.script_versions.get(version_id)
            if root is None or version is None or version.script_id != script_id:
                raise MaaDomainError(
                    "script_version_not_found", "Script version was not found", 404
                )
            if strategy is None and (root.current_version_id != version_id
                                     or root.script_type is ScriptType.STANDARD):
                raise MaaDomainError(
                    "script_not_saved", "Only the current saved script can be exported", 409
                )
            application = uow.applications.get_active(root.application_id)
            if application is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            scripts = uow.scripts.find_active_by_ids(tuple(roots))
            initial_versions = uow.script_versions.find_by_ids(tuple(roots.values()))
            versions = {}
            for identity, selected in roots.items():
                item, record = initial_versions.get(selected), scripts.get(identity)
                if (item is None or record is None or item.script_id != identity
                        or record.application_id != root.application_id):
                    raise MaaDomainError("archive_module_missing", "Module is unavailable", 409)
                versions[identity] = item
            document_bytes = sum(len(json.dumps(v.manifest, ensure_ascii=False).encode())
                                 for v in versions.values())
            pending = set().union(*(self._dependencies(v.manifest) for v in versions.values()))
            pending.difference_update(scripts)
            while pending:
                self._check_budget(deadline)
                if len(scripts) + len(pending) > MAX_EXPORT_SCRIPTS:
                    raise MaaDomainError("archive_too_large", "Too many dependent scripts", 422)
                batch = sorted(pending, key=str)[:8]
                loaded = uow.scripts.find_active_by_ids(batch)
                if len(loaded) != len(batch) or any(
                    s.application_id != root.application_id for s in loaded.values()
                ):
                    raise MaaDomainError(
                        "archive_recovery_missing",
                        "Recovery scripts must exist in the same application",
                        409,
                    )
                ids = {
                    s.script_id: s.current_version_id
                    for s in loaded.values()
                }
                found = uow.script_versions.find_by_ids([v for v in ids.values() if v is not None])
                for identity, record in loaded.items():
                    selected_id = ids[identity]
                    item = found.get(selected_id) if selected_id is not None else None
                    if item is None or item.script_id != identity:
                        raise MaaDomainError(
                            "archive_recovery_missing", "Recovery version is unavailable", 409
                        )
                    document_bytes += len(json.dumps(item.manifest, ensure_ascii=False).encode())
                    if document_bytes > 32 * 1024 * 1024:
                        raise MaaDomainError(
                            "archive_too_large", "Script documents exceed 32 MiB", 422
                        )
                    scripts[identity], versions[identity] = record, item
                    pending.update(self._dependencies(item.manifest))
                pending.difference_update(scripts)
            references = uow.script_versions.list_blob_references_many(
                [v.script_version_id for v in versions.values()]
            )
            blob_ids = {r.blob_id for refs in references.values() for r in refs}
            if len(blob_ids) > 4000:
                raise MaaDomainError("archive_too_large", "Too many exported resources", 422)
            blobs = uow.blobs.find_ready_by_ids(tuple(blob_ids))
            if len(blobs) != len(blob_ids):
                raise MaaDomainError(
                    "required_resource_missing", "An exported resource is not ready", 409
                )
            if sum(b.size_bytes for b in blobs.values()) > MAX_RESOURCE_BYTES:
                raise MaaDomainError("archive_too_large", "Export resources exceed 32 MiB", 422)
        # No remote I/O inside the database transaction. Deletion during export
        # produces an error, never a partially valid archive.
        resources = {}
        for identity, blob in blobs.items():
            self._check_budget(deadline)
            body = bytearray()
            for offset in range(0, blob.size_bytes, 4 * 1024 * 1024):
                self._check_budget(deadline)
                end = min(blob.size_bytes, offset + 4 * 1024 * 1024) - 1
                chunk = self._store.get_range(blob.object_key, offset, end)
                self._check_budget(deadline)
                if len(chunk) != end - offset + 1:
                    raise MaaDomainError("archive_resource_mismatch", "Resource was truncated", 502)
                body.extend(chunk)
            if hashlib.sha256(body).hexdigest() != blob.sha256:
                raise MaaDomainError("archive_resource_mismatch", "Resource checksum differs", 502)
            resources[identity] = f"data:{blob.media_type};base64," + base64.b64encode(body).decode(
                "ascii"
            )
        exported = []
        for identity, version in versions.items():
            self._check_budget(deadline)
            if canonical_manifest_hash(version.manifest) != version.manifest_hash:
                raise MaaDomainError(
                    "archive_manifest_mismatch", "Immutable script checksum differs", 409
                )
            document = copy.deepcopy(version.manifest)
            for reference in references.get(version.script_version_id, []):
                replace_resource_pointer(
                    document, reference.json_pointer, resources[reference.blob_id]
                )
            exported.append(
                ExportScript(
                    str(identity),
                    scripts[identity].name,
                    application.package_name,
                    application.display_name,
                    document,
                )
            )
        try:
            archive_body = build_script_archive(exported, strategy=strategy)
            self._check_budget(deadline)
            return archive_body
        except (ValueError, RecursionError) as exc:
            raise MaaDomainError(
                "archive_export_invalid",
                "Script archive exceeds limits or contains invalid resources",
                422,
            ) from exc

    @staticmethod
    def _dependencies(document: dict[str, Any]) -> set[UUID]:
        if len(json.dumps(document, ensure_ascii=False).encode()) > 16 * 1024 * 1024:
            raise MaaDomainError("archive_too_large", "Script exceeds archive size limit", 422)
        found = set()
        for step in document.get("steps", []):
            retry = step.get("failure_retry") if isinstance(step, dict) else None
            if isinstance(retry, dict) and retry.get("process_script_id"):
                try:
                    found.add(UUID(str(retry["process_script_id"])))
                except ValueError as exc:
                    raise MaaDomainError(
                        "archive_recovery_invalid", "Recovery identity is invalid", 422
                    ) from exc
        return found
