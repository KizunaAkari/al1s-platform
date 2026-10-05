from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import exists, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.component_guard import ACTIVE, admission_lock
from al1s.adapters.postgres.component_models import ComponentOperationRow as Row
from al1s.adapters.postgres.maa_models import MaaQuickTestSessionRow
from al1s.adapters.postgres.scheduling_models import ExecutionRow
from al1s.components.contracts import (
    ComponentOperation,
    ComponentTarget,
    ComponentUpgradeRequest,
    OperationPage,
)
from al1s.components.operations import FINAL, receipt_transition
from al1s.execution.errors import ConflictError, NotFoundError


def operation_record(row: Row) -> ComponentOperation:
    return ComponentOperation(
        operation_id=row.id,
        component=row.component,
        target=ComponentTarget(
            kind="terminal" if row.terminal_id else "platform", terminal_id=row.terminal_id
        ),
        release_id=row.release_id,
        expected_source=row.expected_source,
        state=row.state,
        stage=row.stage,
        created_at=row.created_at,
        updated_at=row.updated_at,
        row_version=row.row_version,
        error_code=row.error_code,
        recovery_point=row.recovery_point,
    )


def active_work(session: Session, target: ComponentTarget) -> bool:
    executions = select(ExecutionRow.id).where(
        ExecutionRow.status.in_(("waiting", "queued", "running"))
    )
    debug = select(MaaQuickTestSessionRow.id).where(
        MaaQuickTestSessionRow.status.in_(("issued", "claimed"))
    )
    if target.terminal_id:
        executions = executions.where(ExecutionRow.terminal_id == target.terminal_id)
        debug = debug.where(MaaQuickTestSessionRow.terminal_id == target.terminal_id)
    return bool(session.scalar(select(exists(executions))) or session.scalar(select(exists(debug))))


class ComponentOperationRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def blockers(self, target: ComponentTarget) -> bool:
        with self.sessions() as session:
            scopes = ["platform", target.scope]
            operations = select(Row.id).where(Row.state.in_(ACTIVE))
            if target.kind == "terminal":
                operations = operations.where(Row.scope.in_(scopes))
            return bool(session.scalar(select(exists(operations))) or active_work(session, target))

    def find_operation(self, scope: str, key: str) -> tuple[ComponentOperation, str] | None:
        with self.sessions() as session:
            row = session.scalar(select(Row).where(Row.scope == scope, Row.idempotency_key == key))
            return (operation_record(row), row.request_hash) if row else None

    def reserve_operation(
        self, request: ComponentUpgradeRequest, request_hash: str, now: datetime
    ) -> ComponentOperation:
        try:
            with self.sessions.begin() as session:
                admission_lock(session)
                previous = session.scalar(
                    select(Row).where(
                        Row.scope == request.target.scope,
                        Row.idempotency_key == request.idempotency_key,
                    )
                )
                if previous:
                    if previous.request_hash != request_hash:
                        raise ConflictError(
                            "component_operation_identity_conflict", "Upgrade identity changed"
                        )
                    return operation_record(previous)
                active = select(Row.id).where(Row.state.in_(ACTIVE))
                if request.target.kind == "terminal":
                    active = active.where(Row.scope.in_(["platform", request.target.scope]))
                if session.scalar(select(exists(active))) or active_work(session, request.target):
                    raise ConflictError(
                        "component_active_work", "Resolve active work or previous upgrade"
                    )
                row = Row(
                    id=uuid4(),
                    component=request.component,
                    scope=request.target.scope,
                    terminal_id=request.target.terminal_id,
                    release_id=request.release_id,
                    expected_source=request.expected_source,
                    idempotency_key=request.idempotency_key,
                    request_hash=request_hash,
                    state="accepted",
                    stage="accepted",
                    created_at=now,
                    updated_at=now,
                    row_version=1,
                )
                session.add(row)
                session.flush()
                return operation_record(row)
        except IntegrityError:
            raise ConflictError(
                "component_upgrade_conflict", "Another upgrade holds this scope"
            ) from None

    def get_operation(self, identity: UUID) -> ComponentOperation:
        with self.sessions() as session:
            row = session.get(Row, identity)
            if row is None:
                raise NotFoundError("component_operation")
            return operation_record(row)

    def update_operation(
        self, identity: UUID, receipt: dict[str, object], now: datetime
    ) -> ComponentOperation:
        if receipt.get("operation_id") is not None and str(receipt["operation_id"]) != str(
            identity
        ):
            raise ConflictError("component_receipt_identity", "Upgrade receipt identity changed")
        with self.sessions.begin() as session:
            row = session.get(Row, identity, with_for_update=True)
            if row is None:
                raise NotFoundError("component_operation")
            state = str(receipt.get("state", "unknown"))
            if row.state in FINAL:
                return operation_record(row)
            row.state = receipt_transition(row.state, state, receipt.get("proof") is True)
            row.stage = str(receipt.get("stage", state))[:64]
            error = receipt.get("error_code")
            row.error_code = str(error)[:64] if error is not None else None
            point = receipt.get("recovery_point")
            if isinstance(point, str):
                row.recovery_point = point[:128]
            row.updated_at, row.row_version = now, row.row_version + 1
            return operation_record(row)

    def list_operations(self, limit: int = 25, cursor: UUID | None = None) -> OperationPage:
        limit = max(1, min(25, limit))
        with self.sessions() as session:
            statement = select(Row)
            if cursor:
                anchor = session.get(Row, cursor)
                if anchor is None:
                    raise NotFoundError("component_operation_cursor")
                statement = statement.where(
                    tuple_(Row.created_at, Row.id) < (anchor.created_at, anchor.id)
                )
            rows = list(
                session.scalars(
                    statement.order_by(Row.created_at.desc(), Row.id.desc()).limit(limit + 1)
                )
            )
            return OperationPage(
                items=[operation_record(row) for row in rows[:limit]],
                next_cursor=rows[limit - 1].id if len(rows) > limit else None,
            )

    def active_operations(self) -> list[ComponentOperation]:
        with self.sessions() as session:
            return [
                operation_record(row)
                for row in session.scalars(
                    select(Row)
                    .where(Row.state.in_(ACTIVE))
                    .order_by(Row.updated_at, Row.id)
                    .limit(25)
                )
            ]
