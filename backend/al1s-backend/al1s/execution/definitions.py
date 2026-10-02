from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID

from al1s.execution.errors import ConflictError, InvalidRequestError
from al1s.execution.scheduling_types import MaterializationCandidate, ResolvedExecutionDefinition


class ExecutionDefinitionProvider(Protocol):
    """Resolve a published logical content identity without exposing module ORM state."""

    def resolve(
        self, logical_content_id: str, parameters: dict[str, Any]
    ) -> ResolvedExecutionDefinition: ...


class ExecutionDefinitionRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ExecutionDefinitionProvider] = {}

    def register(self, module_id: str, provider: ExecutionDefinitionProvider) -> None:
        normalized = module_id.strip()
        if not normalized:
            raise ValueError("module_id cannot be empty")
        if normalized in self._providers:
            raise ValueError(f"definition provider already registered: {normalized}")
        self._providers[normalized] = provider

    def resolve(
        self,
        source_module: str,
        logical_content_id: str,
        parameters: dict[str, Any],
    ) -> ResolvedExecutionDefinition:
        provider = self._providers.get(source_module)
        if provider is None:
            raise InvalidRequestError(
                "execution_definition_provider_unavailable",
                "Execution definition provider is not registered",
            )
        definition = provider.resolve(logical_content_id, parameters)
        actual_hash = canonical_manifest_hash(definition.manifest)
        if actual_hash != definition.manifest_hash:
            raise ConflictError(
                "execution_definition_hash_mismatch",
                "Execution definition manifest hash does not match its content",
            )
        return definition

    def authorize_target(
        self,
        source_module: str,
        logical_content_id: str,
        target_device_id: UUID | None,
    ) -> None:
        provider = self._providers.get(source_module)
        if provider is None:
            raise InvalidRequestError(
                "execution_definition_provider_unavailable",
                "Definition provider is unavailable",
            )
        authorize = getattr(provider, "authorize_target", None)
        if callable(authorize):
            authorize(logical_content_id, target_device_id)

    def resolve_candidates(
        self, candidates: list[MaterializationCandidate]
    ) -> dict[UUID, ResolvedExecutionDefinition]:
        result = {}
        for module in {c.task.source_module for c in candidates}:
            items = [c for c in candidates if c.task.source_module == module]
            provider = self._providers.get(module)
            bulk = getattr(provider, "resolve_occurrences", None)
            if callable(bulk):
                values = bulk([c.occurrence.occurrence_id for c in items])
                for item in items:
                    definition = values[item.occurrence.occurrence_id]
                    if canonical_manifest_hash(definition.manifest) != definition.manifest_hash:
                        raise ConflictError(
                            "execution_definition_hash_mismatch", "Definition hash mismatch"
                        )
                    result[item.occurrence.occurrence_id] = definition
            else:
                for item in items:
                    result[item.occurrence.occurrence_id] = self.resolve(
                        module, item.task.logical_content_id, item.task.parameters
                    )
        return result


def canonical_manifest_hash(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
