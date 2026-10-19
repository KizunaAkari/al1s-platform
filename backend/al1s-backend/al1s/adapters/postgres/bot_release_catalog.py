"""Bounded Bot history ordered by immutable numeric version segments."""

import re
from typing import Any
from uuid import UUID

from sqlalchemy import Numeric, case, cast, func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from al1s.adapters.postgres.component_models import ComponentReleaseRow as Row
from al1s.adapters.postgres.component_records import release_record
from al1s.components.bot_contracts import BotAlias
from al1s.components.contracts import ReleasePage
from al1s.execution.errors import NotFoundError

VERSION = r"^[vV]?([0-9]+)(?:\.([0-9]+))?(?:\.([0-9]+))?(?:[-+].*)?$"


def version_values(value: str | None) -> tuple[int, int, int, int]:
    match = re.fullmatch(VERSION, value or "")
    if match is None:
        return (-1, -1, -1, 0)
    major, minor, patch = (int(part or 0) for part in match.groups())
    return (major, minor, patch, int("-" not in (value or "")))


def version_columns(value: Any) -> list[ColumnElement[Any]]:
    clean = func.regexp_replace(func.coalesce(value, ""), "^[vV]", "")
    numeric = clean.op("~")(r"^[0-9]+(\.[0-9]+){0,2}([-+].*)?$")
    core = func.split_part(func.split_part(clean, "-", 1), "+", 1)
    parts = [
        case(
            (
                numeric,
                cast(func.coalesce(func.nullif(func.split_part(core, ".", i), ""), "0"), Numeric),
            ),
            else_=cast(-1, Numeric),
        )
        for i in (1, 2, 3)
    ]
    return [*parts, case((numeric & ~clean.contains("-"), 1), else_=0)]


class BotReleaseCatalogRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def list_bot_releases(self, alias: BotAlias, limit: int, cursor: UUID | None) -> ReleasePage:
        limit = max(1, min(25, limit))
        component = alias + "-bot"
        keys = [
            *version_columns(Row.native_version),
            *version_columns(Row.runtime_version),
            Row.created_at,
            Row.id,
        ]
        statement = select(Row).where(Row.component == component)
        with self.sessions() as session:
            if cursor is not None:
                anchor = session.get(Row, cursor)
                if anchor is None or anchor.component != component:
                    raise NotFoundError("bot_release_cursor")
                values = (
                    *version_values(anchor.native_version),
                    *version_values(anchor.runtime_version),
                    anchor.created_at,
                    anchor.id,
                )
                statement = statement.where(tuple_(*keys) < values)
            rows = list(
                session.scalars(statement.order_by(*(key.desc() for key in keys)).limit(limit + 1))
            )
            return ReleasePage(
                items=[release_record(row) for row in rows[:limit]],
                next_cursor=rows[limit - 1].id if len(rows) > limit else None,
            )
