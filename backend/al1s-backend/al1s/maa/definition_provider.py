from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast
from uuid import UUID

from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.execution.scheduling_types import (
    CapabilityRequirements,
    ResolvedExecutionDefinition,
    SnapshotBlobReference,
)
from al1s.maa.compiler import MaaRegisteredActionCompiler, resource_key_for
from al1s.maa.errors import MaaDomainError
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.recovery import MaaRecoveryClosureResolver, RecoveryClosure
from al1s.maa.strategy_manifest import build_strategy_manifest
from al1s.maa.types import (
    QualificationKind,
    ScriptBlobReference,
    ScriptStatus,
    ScriptType,
    ScriptVersionRecord,
)
from al1s.maa.validation import VALIDATOR_VERSION

MaaUowFactory = Callable[[], MaaUnitOfWork]


class MaaExecutionDefinitionProvider:
    """Resolve only published Maa identities into immutable execution closures."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        compiler: MaaRegisteredActionCompiler | None = None,
        recovery_resolver: MaaRecoveryClosureResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._compiler = compiler or MaaRegisteredActionCompiler()
        self._recovery_resolver = recovery_resolver or MaaRecoveryClosureResolver()

    def resolve(
        self, logical_content_id: str, parameters: dict[str, Any]
    ) -> ResolvedExecutionDefinition:
        kind, content_id = self._parse_identity(logical_content_id)
        with self._uow_factory() as uow:
            if kind == "script":
                return self._resolve_script(uow, content_id)
            return self._resolve_strategy(uow, content_id)

    def authorize_target(self, logical_content_id: str, target_device_id: UUID | None) -> None:
        if target_device_id is None:
            raise InvalidRequestError("maa_target_device_required", "Maa tasks require a phone")
        kind, content_id = self._parse_identity(logical_content_id)
        with self._uow_factory() as uow:
            if not uow.application_devices.lock_active_device(target_device_id):
                raise NotFoundError("target_device")
            if kind == "script":
                script = uow.scripts.get_active(content_id)
                if script is None or script.script_type is ScriptType.STANDARD:
                    raise NotFoundError("maa_script")
                application_id = script.application_id
            else:
                strategy = uow.strategies.get_active(content_id)
                if strategy is None:
                    raise NotFoundError("maa_strategy")
                application_id = strategy.application_id
            application = uow.applications.get_active(application_id)
            if application is None:
                raise NotFoundError("maa_application")
            binding = uow.application_devices.for_phone_package(
                target_device_id,
                application.package_name,
            )
            if binding is None or binding.application_id != application_id:
                raise ConflictError(
                    "maa_device_not_applicable",
                    "This category is not authorized for the phone",
                )

    def resolve_candidate(
        self, script_id: UUID, candidate_version_id: UUID
    ) -> ResolvedExecutionDefinition:
        """Build a non-formal quick-test definition for the exact static-checked candidate."""
        with self._uow_factory() as uow:
            script = uow.scripts.get_active(script_id)
            if script is None or candidate_version_id not in {
                script.candidate_version_id,
                script.current_version_id,
            }:
                raise NotFoundError("maa_script_candidate")
            version = uow.script_versions.get(candidate_version_id)
            if version is None or version.script_id != script.script_id:
                raise ConflictError(
                    "maa_candidate_version_missing",
                    "Maa candidate version is unavailable",
                )
            passed = uow.qualifications.has_passed(
                version.script_version_id,
                version.manifest_hash,
                (QualificationKind.STATIC_CHECK,),
                static_executor_version=VALIDATOR_VERSION,
            )
            if QualificationKind.STATIC_CHECK not in passed:
                raise ConflictError(
                    "maa_candidate_static_check_required",
                    "Maa candidate must pass the current static validator before quick testing",
                )
            closure = self._resolve_recovery_closure(
                uow,
                {script.script_id: version},
                application_id=script.application_id,
            )
            definitions, providers = self._compile_closure(uow, closure)
            manifest = {
                "schema_version": 1,
                "definition_type": "quick_test",
                "formal_execution": False,
                "script_id": str(script.script_id),
                "candidate_version_id": str(version.script_version_id),
                "candidate_manifest_hash": version.manifest_hash,
                "entry_definition_key": closure.definition_keys[script.script_id],
                "definitions": definitions,
            }
            return _definition(
                revision_id=f"candidate-version:{version.script_version_id}",
                manifest=manifest,
                references=_closure_references(closure),
                provider_keys=providers,
            )

    def _resolve_script(self, uow: MaaUnitOfWork, script_id: UUID) -> ResolvedExecutionDefinition:
        script = uow.scripts.get_active(script_id)
        if (
            script is None
            or script.script_type is ScriptType.STANDARD
            or script.status is not ScriptStatus.ACTIVE
            or script.current_version_id is None
        ):
            raise NotFoundError("maa_published_script")
        version = uow.script_versions.get(script.current_version_id)
        if version is None or version.script_id != script.script_id:
            raise ConflictError(
                "maa_script_version_missing",
                "Published Maa script version is unavailable",
            )
        closure = self._resolve_recovery_closure(
            uow,
            {script.script_id: version},
            application_id=script.application_id,
        )
        definitions, providers = self._compile_closure(uow, closure)
        entry_key = closure.definition_keys[script.script_id]
        manifest = {
            "schema_version": 1,
            "definition_type": "script",
            "script_id": str(script.script_id),
            "entry_definition_key": entry_key,
            "definitions": definitions,
        }
        return _definition(
            revision_id=f"script-version:{version.script_version_id}",
            manifest=manifest,
            references=_closure_references(closure),
            provider_keys=providers,
        )

    def _resolve_strategy(
        self, uow: MaaUnitOfWork, strategy_id: UUID
    ) -> ResolvedExecutionDefinition:
        strategy = uow.strategies.get_active(strategy_id)
        if strategy is None or strategy.current_version_id is None:
            raise NotFoundError("maa_published_strategy")
        version = uow.strategy_versions.get(strategy.current_version_id)
        if version is None or version.strategy_id != strategy.strategy_id:
            raise ConflictError(
                "maa_strategy_version_missing",
                "Published Maa strategy version is unavailable",
            )
        modules = uow.strategy_versions.list_modules(version.strategy_version_id)
        script_version_ids = [item.script_version_id for item in modules]
        script_versions = uow.script_versions.find_by_ids(script_version_ids)
        if set(script_versions) != set(script_version_ids):
            raise ConflictError(
                "maa_strategy_closure_missing",
                "Published Maa strategy script closure is incomplete",
            )
        defaults = version.manifest.get("default_parameters", {})
        if not isinstance(defaults, dict):
            raise ConflictError(
                "maa_strategy_manifest_invalid",
                "Published Maa strategy parameters are invalid",
            )
        expected_manifest = build_strategy_manifest(
            strategy.application_id,
            modules,
            default_parameters=cast(dict[str, Any], defaults),
        )
        if canonical_manifest_hash(expected_manifest) != version.manifest_hash:
            raise ConflictError(
                "maa_strategy_closure_mismatch",
                "Published Maa strategy closure differs from its immutable manifest",
            )
        # The stored strategy version fixes module order and waits. Script content is
        # resolved at dispatch; already materialized executions keep their snapshot.
        script_ids = [script_versions[item.script_version_id].script_id for item in modules]
        active_scripts = uow.scripts.find_active_by_ids(script_ids)
        if set(active_scripts) != set(script_ids) or any(
            item.current_version_id is None for item in active_scripts.values()
        ):
            raise ConflictError(
                "maa_strategy_script_missing", "Strategy contains an unavailable script"
            )
        if any(item.script_type is ScriptType.STANDARD for item in active_scripts.values()):
            raise ConflictError("maa_legacy_script_type", "Strategy contains a legacy script")
        current_ids = [
            cast(UUID, active_scripts[script_id].current_version_id) for script_id in script_ids
        ]
        current_versions = uow.script_versions.find_by_ids(current_ids)
        if set(current_versions) != set(current_ids):
            raise ConflictError(
                "maa_strategy_script_version_missing", "Saved script version is missing"
            )
        roots = {
            script_id: current_versions[current_id]
            for script_id, current_id in zip(script_ids, current_ids, strict=True)
        }
        closure = self._resolve_recovery_closure(
            uow,
            roots,
            application_id=strategy.application_id,
        )
        definitions, providers = self._compile_closure(uow, closure)
        definition_keys = closure.definition_keys
        manifest = {
            "schema_version": 1,
            "definition_type": "strategy",
            "strategy_id": str(strategy.strategy_id),
            "strategy_version_id": str(version.strategy_version_id),
            "strategy_manifest_hash": version.manifest_hash,
            "default_parameters": cast(dict[str, Any], defaults),
            "modules": [
                {
                    "position": item.position,
                    "role": item.module_role.value,
                    "wait_after_ms": item.wait_after_ms,
                    "definition_key": definition_keys[
                        script_versions[item.script_version_id].script_id
                    ],
                }
                for item in modules
            ],
            "definitions": definitions,
        }
        return _definition(
            revision_id="strategy-save:"
            + canonical_manifest_hash(
                {
                    "strategy_version_id": str(version.strategy_version_id),
                    "script_versions": [str(item) for item in current_ids],
                }
            ),
            manifest=manifest,
            references=_closure_references(closure),
            provider_keys=providers,
        )

    def _resolve_recovery_closure(
        self,
        uow: MaaUnitOfWork,
        roots: dict[UUID, ScriptVersionRecord],
        *,
        application_id: UUID,
    ) -> RecoveryClosure:
        try:
            return self._recovery_resolver.resolve(
                uow,
                roots,
                application_id=application_id,
            )
        except MaaDomainError as exc:
            raise ConflictError(exc.code, exc.message) from exc

    def _compile_closure(
        self,
        uow: MaaUnitOfWork,
        closure: RecoveryClosure,
    ) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
        keys = closure.definition_keys
        scripts = uow.scripts.find_active_by_ids(tuple(closure.versions_by_script))
        if set(scripts) != set(closure.versions_by_script):
            raise ConflictError(
                "maa_script_closure_missing",
                "Maa execution closure references an unavailable script",
            )
        providers: set[str] = set()
        definitions: dict[str, dict[str, Any]] = {}
        for script_id, version in closure.versions_by_script.items():
            compiled = self._compiler.compile(
                version,
                closure.references_by_version.get(version.script_version_id, []),
                script_name=scripts[script_id].name,
                recovery_keys=keys,
            )
            definitions[keys[script_id]] = compiled.definition
            providers.update(compiled.provider_keys)
        return definitions, tuple(sorted(providers))

    @staticmethod
    def _parse_identity(logical_content_id: str) -> tuple[str, UUID]:
        kind, separator, raw_id = logical_content_id.partition(":")
        if not separator or kind not in {"script", "strategy"}:
            raise InvalidRequestError(
                "invalid_maa_content_identity",
                "Maa content identity must be script:<uuid> or strategy:<uuid>",
            )
        try:
            return kind, UUID(raw_id)
        except ValueError as exc:
            raise InvalidRequestError(
                "invalid_maa_content_identity",
                "Maa content identity contains an invalid UUID",
            ) from exc


def _definition(
    *,
    revision_id: str,
    manifest: dict[str, Any],
    references: Sequence[ScriptBlobReference],
    provider_keys: tuple[str, ...],
) -> ResolvedExecutionDefinition:
    unique_references: list[ScriptBlobReference] = []
    seen: set[tuple[UUID, str]] = set()
    for item in references:
        key = (item.blob_id, item.resource_role)
        if key not in seen:
            unique_references.append(item)
            seen.add(key)
    return ResolvedExecutionDefinition(
        revision_id=revision_id,
        schema_version=1,
        manifest_hash=canonical_manifest_hash(manifest),
        manifest=manifest,
        capability_requirements=CapabilityRequirements(
            provider_keys=provider_keys,
            requires_target_device=True,
        ),
        blobs=tuple(
            SnapshotBlobReference(
                blob_id=item.blob_id,
                resource_key=resource_key_for(item),
                role=item.resource_role,
            )
            for item in unique_references
        ),
    )


def _closure_references(closure: RecoveryClosure) -> list[ScriptBlobReference]:
    return [
        reference
        for version in closure.versions_by_script.values()
        for reference in closure.references_by_version.get(version.script_version_id, [])
    ]
