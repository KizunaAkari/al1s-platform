from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.discord_forward_models import (
    DiscordForwardActionRow,
    DiscordForwardRuleRow,
)
from al1s.adapters.postgres.notification_models import NotificationChannelRow


class PostgresDiscordForwardRepository:
    def __init__(self, session: Session):
        self._session = session

    def enabled_actions(
        self,
        service_id: UUID,
        rule_id: UUID,
        version: int,
        guild_id: str,
        channel_id: str,
    ) -> list[DiscordForwardActionRow] | None:
        rule = self._session.scalar(
            select(DiscordForwardRuleRow)
            .where(
                DiscordForwardRuleRow.id == rule_id,
                DiscordForwardRuleRow.bot_service_id == service_id,
                DiscordForwardRuleRow.row_version == version,
                DiscordForwardRuleRow.guild_id == guild_id,
                DiscordForwardRuleRow.channel_id == channel_id,
                DiscordForwardRuleRow.enabled.is_(True),
                DiscordForwardRuleRow.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if rule is None:
            return None
        return list(
            self._session.scalars(
                select(DiscordForwardActionRow)
                .join(
                    NotificationChannelRow,
                    NotificationChannelRow.id == DiscordForwardActionRow.channel_id,
                )
                .where(
                    DiscordForwardActionRow.rule_id == rule_id,
                    NotificationChannelRow.enabled.is_(True),
                    NotificationChannelRow.deleted_at.is_(None),
                )
                .order_by(DiscordForwardActionRow.position)
            )
        )
