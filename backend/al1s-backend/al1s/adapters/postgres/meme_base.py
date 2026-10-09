import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.meme_models import (
    MemeAliasRow,
    MemeMutationRow,
    MemeTemplateRow,
    MemeVersionBlobRow,
    MemeVersionRow,
)
from al1s.meme_workshop.contracts import Layout
from al1s.meme_workshop.records import TemplateRecord


def fingerprint(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class MemeRepositoryBase:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def lock(session: Session) -> None:
        # Serialize rare catalog/configuration changes, including first creation and the cap.
        session.execute(text("SELECT pg_advisory_xact_lock(716202810)"))

    @staticmethod
    def replay(session: Session, key: str, digest: str) -> dict[str, Any] | None:
        receipt = session.get(MemeMutationRow, key)
        if receipt is None:
            return None
        if receipt.request_sha256 != digest:
            raise ValueError("idempotency_conflict")
        return receipt.result

    @staticmethod
    def mutation_key(kind: str, target: UUID, operation: str) -> str:
        if not 1 <= len(operation) <= 128:
            raise ValueError("invalid_idempotency_key")
        return hashlib.sha256(f"{kind}:{target}:{operation}".encode()).hexdigest()

    @staticmethod
    def add_mutation(
        session: Session,
        key: str,
        digest: str,
        result: dict[str, Any],
        *,
        template_id: UUID | None = None,
        bot_service_id: UUID | None = None,
        version_id: UUID | None = None,
    ) -> None:
        session.add(
            MemeMutationRow(
                key=key,
                request_sha256=digest,
                result=result,
                template_id=template_id,
                bot_service_id=bot_service_id,
                version_id=version_id,
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )

    @staticmethod
    def records(
        session: Session, rows: Sequence[tuple[MemeTemplateRow, MemeVersionRow]]
    ) -> list[TemplateRecord]:
        if not rows:
            return []
        aliases = session.scalars(
            select(MemeAliasRow).where(
                MemeAliasRow.template_id.in_([row.id for row, _ in rows]),
            )
        ).all()
        references = session.scalars(
            select(MemeVersionBlobRow).where(
                MemeVersionBlobRow.version_id.in_([version.id for _, version in rows]),
            )
        ).all()
        grouped: dict[UUID, list[MemeAliasRow]] = {}
        blobs: dict[UUID, dict[str, UUID]] = {}
        for alias in aliases:
            grouped.setdefault(alias.template_id, []).append(alias)
        for reference in references:
            blobs.setdefault(reference.version_id, {})[reference.role] = reference.blob_id
        return [
            TemplateRecord(
                id=row.id,
                name=row.name,
                enabled=row.enabled,
                random_enabled=row.random_enabled,
                builtin=row.builtin,
                keyword=next(
                    (
                        alias.normalized_alias
                        for alias in grouped.get(row.id, [])
                        if alias.is_primary
                    ),
                    "已删除",
                ),
                aliases=sorted(
                    alias.normalized_alias
                    for alias in grouped.get(row.id, [])
                    if not alias.is_primary
                ),
                layout=Layout.model_validate(version.layout),
                version_id=version.id,
                version_no=version.version_no,
                row_version=row.row_version,
                background_blob_id=blobs.get(version.id, {}).get("background"),
                foreground_blob_id=blobs.get(version.id, {}).get("foreground"),
            )
            for row, version in rows
        ]
