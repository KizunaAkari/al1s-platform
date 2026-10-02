"""SQLAlchemy persistence for Discord forwarding rules.

One write transaction holds the Bot lock through action validation and replacement.
Action rows are fetched in one batched query per page or match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.discord_forward_models import (
    DiscordForwardActionRow,
    DiscordForwardRuleRow,
)
from al1s.adapters.postgres.notification_models import NotificationChannelRow
from al1s.notifications.discord_forward_types import (
    MAX_ACTIVE_RULES,
    ActionInput,
    ActionValidator,
    ChannelState,
    RuleInput,
)
from al1s.notifications.errors import NotificationDomainError


def _rule_dict(
    row: DiscordForwardRuleRow, actions: list[DiscordForwardActionRow]
) -> dict[str, object]:
    return {
        "id": str(row.id),
        "bot_service_id": str(row.bot_service_id),
        "name": row.name,
        "guild_id": row.guild_id,
        "channel_id": row.channel_id,
        "trigger_kind": row.trigger_kind,
        "trigger_text": row.trigger_text,
        "frequency_count": row.frequency_count,
        "frequency_window_seconds": row.frequency_window_seconds,
        "cooldown_seconds": row.cooldown_seconds,
        "enabled": row.enabled,
        "row_version": row.row_version,
        "created_at": row.created_at.isoformat(),
        "actions": [
            {
                "id": str(action.id),
                "channel_id": str(action.channel_id),
                "kind": action.kind,
                "target": action.target,
                "review_policy": action.review_policy,
            }
            for action in actions
        ],
    }


class DiscordForwardRuleRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    def list_page(
        self,
        bot_service_id: UUID,
        *,
        after_id: UUID | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 50:
            raise NotificationDomainError("invalid_page_limit", "Invalid page limit")
        with self._sessions() as session:
            statement = select(DiscordForwardRuleRow).where(
                DiscordForwardRuleRow.bot_service_id == bot_service_id,
                DiscordForwardRuleRow.deleted_at.is_(None),
            )
            if after_id is not None:
                statement = statement.where(DiscordForwardRuleRow.id > after_id)
            rows = list(
                session.scalars(
                    statement.order_by(DiscordForwardRuleRow.id).limit(limit)
                )
            )
            actions = self._actions(session, [row.id for row in rows])
            return [_rule_dict(row, actions.get(row.id, [])) for row in rows]

    def enabled_for_worker(self, bot_service_id: UUID) -> list[dict[str, object]]:
        with self._sessions() as session:
            rows = session.scalars(
                select(DiscordForwardRuleRow)
                .where(
                    DiscordForwardRuleRow.bot_service_id == bot_service_id,
                    DiscordForwardRuleRow.enabled.is_(True),
                    DiscordForwardRuleRow.deleted_at.is_(None),
                )
                .order_by(DiscordForwardRuleRow.id)
                .limit(MAX_ACTIVE_RULES + 1)
            ).all()
            if len(rows) > MAX_ACTIVE_RULES:
                raise NotificationDomainError("discord_rule_limit", "Too many enabled rules", 409)
            return [_rule_dict(row, []) for row in rows]

    def save(
        self,
        data: RuleInput,
        *,
        rule_id: UUID | None = None,
        expected_version: int | None = None,
        validate_actions: ActionValidator,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            service = session.scalar(
                select(BotServiceRow)
                .where(BotServiceRow.id == data.bot_service_id)
                .with_for_update()
            )
            if service is None or service.kind != "discord_bridge" or service.deleted_at:
                raise NotificationDomainError("discord_bot_not_found", "Discord Bot not found", 404)
            validate_actions(self._channels(session, data.actions), data.actions)
            if rule_id is None:
                row = DiscordForwardRuleRow(
                    id=uuid4(), bot_service_id=data.bot_service_id, created_at=now, row_version=1
                )
                session.add(row)
            else:
                existing_row = session.scalar(
                    select(DiscordForwardRuleRow)
                    .where(
                        DiscordForwardRuleRow.id == rule_id,
                    )
                    .with_for_update()
                )
                if (
                    existing_row is None
                    or existing_row.deleted_at is not None
                    or existing_row.bot_service_id != data.bot_service_id
                ):
                    raise NotificationDomainError("discord_rule_not_found", "Rule not found", 404)
                row = existing_row
                if expected_version is None or row.row_version != expected_version:
                    raise NotificationDomainError("discord_rule_conflict", "Rule changed", 409)
                row.row_version += 1
                session.execute(
                    delete(DiscordForwardActionRow).where(
                        DiscordForwardActionRow.rule_id == row.id,
                    )
                )
            if data.enabled and (rule_id is None or not row.enabled):
                with session.no_autoflush:
                    active_count = session.scalar(
                        select(func.count()).select_from(DiscordForwardRuleRow).where(
                            DiscordForwardRuleRow.bot_service_id == data.bot_service_id,
                            DiscordForwardRuleRow.enabled.is_(True),
                            DiscordForwardRuleRow.deleted_at.is_(None),
                        )
                    )
                if active_count is not None and active_count >= MAX_ACTIVE_RULES:
                    raise NotificationDomainError(
                        "discord_rule_limit", "Too many enabled rules", 409
                    )
            row.name = data.name.strip()
            row.guild_id = data.guild_id
            row.channel_id = data.channel_id
            row.trigger_kind = data.trigger_kind
            row.trigger_text = data.trigger_text
            row.frequency_count = data.frequency_count
            row.frequency_window_seconds = data.frequency_window_seconds
            row.cooldown_seconds = data.cooldown_seconds
            row.enabled = data.enabled
            row.updated_at = now
            # Flush the parent before inserting action rows with a raw FK value.
            session.flush()
            actions = [
                DiscordForwardActionRow(
                    id=uuid4(),
                    rule_id=row.id,
                    channel_id=action.channel_id,
                    kind=action.kind,
                    target=action.target.strip(),
                    review_policy=action.review_policy,
                    position=position,
                )
                for position, action in enumerate(data.actions)
            ]
            session.add_all(actions)
            session.flush()
            return _rule_dict(row, actions)

    def delete(self, rule_id: UUID, expected_version: int) -> None:
        with self._sessions.begin() as session:
            row = session.scalar(
                select(DiscordForwardRuleRow)
                .where(
                    DiscordForwardRuleRow.id == rule_id,
                )
                .with_for_update()
            )
            if row is None or row.deleted_at is not None:
                raise NotificationDomainError("discord_rule_not_found", "Rule not found", 404)
            if row.row_version != expected_version:
                raise NotificationDomainError("discord_rule_conflict", "Rule changed", 409)
            row.enabled = False
            row.deleted_at = row.updated_at = datetime.now(UTC)
            row.row_version += 1

    def match_actions(
        self,
        bot_service_id: UUID,
        rule_id: UUID,
        version: int,
        guild_id: str,
        channel_id: str,
    ) -> list[dict[str, object]]:
        with self._sessions() as session:
            row = session.scalar(
                select(DiscordForwardRuleRow).where(
                    DiscordForwardRuleRow.id == rule_id,
                    DiscordForwardRuleRow.bot_service_id == bot_service_id,
                    DiscordForwardRuleRow.row_version == version,
                    DiscordForwardRuleRow.guild_id == guild_id,
                    DiscordForwardRuleRow.channel_id == channel_id,
                    DiscordForwardRuleRow.enabled.is_(True),
                    DiscordForwardRuleRow.deleted_at.is_(None),
                )
            )
            if row is None:
                raise NotificationDomainError(
                    "discord_rule_stale", "Rule changed or is disabled", 409
                )
            actions = self._actions(session, [rule_id])[rule_id]
            return _rule_dict(row, actions)["actions"]  # type: ignore[return-value]

    @staticmethod
    def _actions(
        session: Session, rule_ids: list[UUID]
    ) -> dict[UUID, list[DiscordForwardActionRow]]:
        if not rule_ids:
            return {}
        result: dict[UUID, list[DiscordForwardActionRow]] = {value: [] for value in rule_ids}
        for row in session.scalars(
            select(DiscordForwardActionRow)
            .where(
                DiscordForwardActionRow.rule_id.in_(rule_ids),
            )
            .order_by(DiscordForwardActionRow.rule_id, DiscordForwardActionRow.position)
        ):
            result[row.rule_id].append(row)
        return result


    @staticmethod
    def _channels(
        session: Session, actions: tuple[ActionInput, ...]
    ) -> dict[UUID, ChannelState]:
        rows = session.scalars(
            select(NotificationChannelRow).where(
                NotificationChannelRow.id.in_([action.channel_id for action in actions])
            )
        )
        return {
            row.id: ChannelState(row.kind, row.enabled, row.deleted_at is not None)
            for row in rows
        }
