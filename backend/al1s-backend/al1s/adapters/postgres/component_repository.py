from datetime import datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.component_models import ComponentReleaseRow
from al1s.adapters.postgres.component_operation_repository import ComponentOperationRepository
from al1s.adapters.postgres.component_records import release_record
from al1s.adapters.postgres.component_release_repository import ComponentReleaseRepository
from al1s.adapters.postgres.execution_models import TerminalCapabilityProfileRow, TerminalRow
from al1s.components.contracts import ComponentRelease, ComponentReleaseInput, ReleasePage
from al1s.components.types import TerminalProfile
from al1s.execution.errors import ConflictError, NotFoundError


class ComponentRepository(ComponentReleaseRepository, ComponentOperationRepository):
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def list_linux_profiles(
        self, limit: int, cursor: UUID | None
    ) -> tuple[list[TerminalProfile], UUID | None]:
        limit = max(1, min(20, limit))
        statement = (
            select(TerminalRow, TerminalCapabilityProfileRow)
            .outerjoin(
                TerminalCapabilityProfileRow,
                TerminalRow.current_capability_profile_id == TerminalCapabilityProfileRow.id,
            )
            .where(TerminalRow.terminal_type == "linux", TerminalRow.deleted_at.is_(None))
        )
        if cursor is not None:
            statement = statement.where(TerminalRow.id > cursor)
        statement = statement.order_by(TerminalRow.id).limit(min(20, limit) + 1)
        with self.sessions() as session:
            rows = session.execute(statement).all()
            profiles = [
                TerminalProfile(
                    terminal.id,
                    terminal.display_name,
                    terminal.service_status,
                    terminal.agent_version,
                    profile.architecture if profile else None,
                    profile.created_at if profile else None,
                    profile.details if profile else {},
                    terminal.last_seen_at,
                )
                for terminal, profile in rows[:limit]
            ]
            return profiles, profiles[-1].terminal_id if len(rows) > limit else None

    def get_linux_profile(self, identity: UUID) -> TerminalProfile | None:
        with self.sessions() as session:
            pair = session.execute(
                select(TerminalRow, TerminalCapabilityProfileRow)
                .outerjoin(
                    TerminalCapabilityProfileRow,
                    TerminalRow.current_capability_profile_id == TerminalCapabilityProfileRow.id,
                )
                .where(
                    TerminalRow.id == identity,
                    TerminalRow.terminal_type == "linux",
                    TerminalRow.deleted_at.is_(None),
                )
            ).one_or_none()
            if pair is None:
                return None
            terminal, profile = pair
            return TerminalProfile(
                terminal.id,
                terminal.display_name,
                terminal.service_status,
                terminal.agent_version,
                profile.architecture if profile else None,
                profile.created_at if profile else None,
                profile.details if profile else {},
                terminal.last_seen_at,
            )

    def get_release(self, identity: UUID) -> ComponentRelease:
        with self.sessions() as session:
            row = session.get(ComponentReleaseRow, identity)
            if row is None:
                raise NotFoundError("component_release")
            return release_record(row)

    def create_release(self, body: ComponentReleaseInput, now: datetime) -> ComponentRelease:
        try:
            with self.sessions.begin() as session:
                row = ComponentReleaseRow(
                    id=body.release_id,
                    **body.model_dump(exclude={"release_id"}),
                    state="draft",
                    created_at=now,
                    row_version=1,
                )
                session.add(row)
                session.flush()
                return release_record(row)
        except IntegrityError:
            with self.sessions() as session:
                previous = session.get(ComponentReleaseRow, body.release_id)
                if (
                    previous is not None
                    and ComponentReleaseInput.model_validate(
                        release_record(previous).model_dump(
                            include=set(ComponentReleaseInput.model_fields)
                        )
                    )
                    == body
                ):
                    return release_record(previous)
            raise ConflictError(
                "component_release_conflict", "Release identity or version exists"
            ) from None

    def list_releases(self, component: str, limit: int, cursor: UUID | None) -> ReleasePage:
        limit = max(1, min(25, limit))
        with self.sessions() as session:
            statement = select(ComponentReleaseRow).where(
                ComponentReleaseRow.component == component
            )
            if cursor is not None:
                anchor = session.get(ComponentReleaseRow, cursor)
                if anchor is None or anchor.component != component:
                    raise NotFoundError("component_release_cursor")
                statement = statement.where(
                    tuple_(ComponentReleaseRow.created_at, ComponentReleaseRow.id)
                    < (anchor.created_at, anchor.id)
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        ComponentReleaseRow.created_at.desc(), ComponentReleaseRow.id.desc()
                    ).limit(limit + 1)
                )
            )
            return ReleasePage(
                items=[release_record(row) for row in rows[:limit]],
                next_cursor=rows[limit - 1].id if len(rows) > limit else None,
            )
