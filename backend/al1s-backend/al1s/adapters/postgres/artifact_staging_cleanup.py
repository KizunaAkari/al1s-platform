"""Persisted, lease-backed staging cleanup cursor."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow
from al1s.maa.artifact_staging_cleanup import StagingCandidate, StagingSettlement


class PostgresArtifactStagingCleanup:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def claim(
        self, now: datetime, *, limit: int, lease: timedelta
    ) -> tuple[StagingCandidate, ...]:
        if not 1 <= limit <= 10:
            raise ValueError("staging cleanup limit must be between 1 and 10")
        lease_until = now + lease
        with self.sessions.begin() as session:
            rows = session.scalars(
                select(TerminalArtifactRow)
                .where(
                    TerminalArtifactRow.status.in_(("ready", "expired")),
                    TerminalArtifactRow.staging_cleanup_at <= now,
                )
                .order_by(TerminalArtifactRow.staging_cleanup_at, TerminalArtifactRow.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            candidates = tuple(
                StagingCandidate(
                    artifact_id=row.id,
                    terminal_id=row.terminal_id,
                    object_key=row.object_key,
                    expires_at=row.expires_at,
                    completed_at=row.completed_at,
                    lease_until=lease_until,
                )
                for row in rows
                if row.completed_at is not None
            )
            for row in rows:
                row.staging_cleanup_at = lease_until
            return candidates

    def settle_many(self, outcomes: Sequence[StagingSettlement]) -> set[UUID]:
        if not outcomes:
            return set()
        lease_until = outcomes[0].candidate.lease_until
        if any(item.candidate.lease_until != lease_until for item in outcomes):
            raise ValueError("staging settlements must share one lease")
        next_times = {item.candidate.artifact_id: item.next_at for item in outcomes}
        if len(next_times) != len(outcomes):
            raise ValueError("duplicate staging settlement")
        with self.sessions.begin() as session:
            updated_ids = session.scalars(
                update(TerminalArtifactRow)
                .where(
                    TerminalArtifactRow.id.in_(next_times),
                    TerminalArtifactRow.status.in_(("ready", "expired")),
                    TerminalArtifactRow.staging_cleanup_at == lease_until,
                )
                .values(staging_cleanup_at=case(
                    next_times,
                    value=TerminalArtifactRow.id,
                    else_=TerminalArtifactRow.staging_cleanup_at,
                ))
                .returning(TerminalArtifactRow.id)
            ).all()
            return set(updated_ids)
