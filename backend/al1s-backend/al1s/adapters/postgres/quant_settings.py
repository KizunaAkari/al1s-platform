"""Versioned QQ quant settings; activation is acknowledged by the gateway separately."""

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import MemeAliasRow, MemeSettingsRow
from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.adapters.postgres.quant_models import QuantCommandRow, QuantScopeRow, QuantSettingsRow
from al1s.quant.commands import DEFAULT_COMMANDS, normalize_keyword, validate_mappings
from al1s.quant.contracts import QuantSettings


class QuantSettingsRecord(BaseModel):
    settings: QuantSettings
    commands: dict[str, str]
    row_version: int
    mapping_version: int


class QuantSettingsRepository(MemeRepositoryBase):
    @staticmethod
    def _service(session: Session, service_id: UUID) -> BotServiceRow:
        service = session.get(BotServiceRow, service_id)
        if service is None or service.deleted_at is not None or service.kind != "onebot_gateway":
            raise ValueError("qq_bot_not_found")
        return service

    @staticmethod
    def _read(session: Session, service_id: UUID) -> QuantSettingsRecord:
        row = session.get(QuantSettingsRow, service_id, with_for_update={"read": True})
        if row is None:
            meme = session.get(MemeSettingsRow, service_id)
            return QuantSettingsRecord(
                settings=QuantSettings(event_ws_url=meme.qq_event_ws_url if meme else None),
                commands=DEFAULT_COMMANDS.copy(),
                row_version=0,
                mapping_version=0,
            )
        scopes = session.scalars(
            select(QuantScopeRow.group_id)
            .where(QuantScopeRow.service_id == service_id)
            .order_by(QuantScopeRow.group_id)
        ).all()
        commands = session.execute(
            select(QuantCommandRow.action, QuantCommandRow.keyword).where(
                QuantCommandRow.service_id == service_id
            )
        ).all()
        return QuantSettingsRepository._record(row, list(scopes), dict(commands))

    @staticmethod
    def _record(
        row: QuantSettingsRow, scopes: list[str], commands: dict[str, str]
    ) -> QuantSettingsRecord:
        settings = QuantSettings(
            enabled=row.enabled,
            private_enabled=row.private_enabled,
            event_ws_url=row.event_ws_url,
            groups=scopes,
            initial_cash=row.initial_cash,
            commission_rate=row.commission_rate,
            minimum_commission=row.minimum_commission,
            stamp_tax_rate=row.stamp_tax_rate,
            slippage=row.slippage,
            max_quote_age_seconds=row.quote_max_age_seconds,
            bar_cache_days=row.bar_cache_days,
            refresh_seconds=row.refresh_seconds,
            reply_interval_seconds=row.reply_interval_seconds,
            provider=row.provider,
            provider_order=row.provider_order,
        )
        return QuantSettingsRecord(
            settings=settings,
            commands=commands,
            row_version=row.row_version,
            mapping_version=row.mapping_version,
        )

    def get(self, service_id: UUID) -> QuantSettingsRecord:
        with self.sessions() as session:
            self._service(session, service_id)
            return self._read(session, service_id)

    def cache_days(self) -> int:
        with self.sessions() as session:
            value = session.scalar(select(func.max(QuantSettingsRow.bar_cache_days)))
            return int(value) if value is not None else 30

    def snapshots(self, service_ids: list[UUID]) -> dict[UUID, QuantSettingsRecord]:
        if not service_ids:
            return {}
        if len(service_ids) > 100:
            raise ValueError("quant_connections_limit")
        with self.sessions() as session:
            rows = session.scalars(
                select(QuantSettingsRow)
                .where(QuantSettingsRow.service_id.in_(service_ids))
                .with_for_update(read=True)
            ).all()
            scopes = session.execute(
                select(QuantScopeRow.service_id, QuantScopeRow.group_id)
                .where(QuantScopeRow.service_id.in_(service_ids))
                .order_by(QuantScopeRow.group_id)
            ).all()
            commands = session.execute(
                select(
                    QuantCommandRow.service_id, QuantCommandRow.action, QuantCommandRow.keyword
                ).where(QuantCommandRow.service_id.in_(service_ids))
            ).all()
            groups_by_id: dict[UUID, list[str]] = {row.service_id: [] for row in rows}
            commands_by_id: dict[UUID, dict[str, str]] = {row.service_id: {} for row in rows}
            for service_id, group in scopes:
                if service_id in groups_by_id:
                    groups_by_id[service_id].append(group)
            for service_id, action, keyword in commands:
                if service_id in commands_by_id:
                    commands_by_id[service_id][action] = keyword
            return {
                row.service_id: self._record(
                    row, groups_by_id[row.service_id], commands_by_id[row.service_id]
                )
                for row in rows
            }

    def keyword_conflicts(self, keyword: str) -> bool:
        with self.sessions() as session:
            return session.get(MemeAliasRow, normalize_keyword(keyword)) is not None

    def qq_connections(self) -> list[QqConnection]:
        with self.sessions() as session:
            rows = session.execute(
                select(
                    QuantSettingsRow,
                    BotConfigVersionRow,
                    EncryptedSecretRow.ciphertext,
                    EncryptedSecretRow.key_id,
                )
                .join(BotServiceRow, BotServiceRow.id == QuantSettingsRow.service_id)
                .join(
                    BotConfigVersionRow,
                    BotConfigVersionRow.id == BotServiceRow.applied_config_version_id,
                )
                .outerjoin(
                    EncryptedSecretRow, EncryptedSecretRow.id == BotConfigVersionRow.secret_id
                )
                .where(
                    QuantSettingsRow.enabled,
                    QuantSettingsRow.event_ws_url.is_not(None),
                    BotServiceRow.kind == "onebot_gateway",
                    BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None),
                    BotConfigVersionRow.service_id == BotServiceRow.id,
                )
                .order_by(BotServiceRow.id)
                .limit(100)
            ).all()
            result = []
            for settings, version, ciphertext, key_id in rows:
                base = version.settings.get("ONEBOT_BASE_URL")
                if isinstance(base, str) and settings.event_ws_url:
                    result.append(
                        QqConnection(
                            settings.service_id,
                            version.id,
                            settings.row_version,
                            settings.event_ws_url,
                            base,
                            5,
                            ciphertext,
                            key_id,
                        )
                    )
            return result

    def save(
        self,
        service_id: UUID,
        settings: QuantSettings,
        commands: dict[str, str],
        *,
        expected_version: int,
        operation_id: str,
    ) -> QuantSettingsRecord:
        key = self.mutation_key("quant_settings", service_id, operation_id)
        digest = fingerprint(
            {
                "settings": settings.model_dump(mode="json"),
                "commands": commands,
                "expected": expected_version,
            }
        )
        with self.sessions.begin() as session:
            # Shares the catalog lock with meme alias writes to prevent save-time races.
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return QuantSettingsRecord.model_validate(replay)
            self._service(session, service_id)
            previous = self._read(session, service_id)
            if previous.row_version != expected_version:
                raise ValueError("quant_settings_conflict")
            aliases = set(session.scalars(select(MemeAliasRow.normalized_alias)).all())
            normalized = validate_mappings(commands, aliases)
            row = session.get(QuantSettingsRow, service_id, with_for_update=True)
            if row is None:
                row = QuantSettingsRow(service_id=service_id, row_version=1, mapping_version=1)
                session.add(row)
            else:
                row.row_version += 1
                if previous.commands != commands:
                    row.mapping_version += 1
            for name in (
                "enabled",
                "private_enabled",
                "event_ws_url",
                "initial_cash",
                "commission_rate",
                "minimum_commission",
                "stamp_tax_rate",
                "slippage",
                "bar_cache_days",
                "refresh_seconds",
                "reply_interval_seconds",
                "provider",
                "provider_order",
            ):
                setattr(row, name, getattr(settings, name))
            row.quote_max_age_seconds = settings.max_quote_age_seconds
            session.flush()
            session.execute(delete(QuantScopeRow).where(QuantScopeRow.service_id == service_id))
            session.execute(delete(QuantCommandRow).where(QuantCommandRow.service_id == service_id))
            session.add_all(
                [QuantScopeRow(service_id=service_id, group_id=group) for group in settings.groups]
            )
            session.add_all(
                [
                    QuantCommandRow(
                        service_id=service_id,
                        action=action,
                        keyword=keyword,
                        normalized_keyword=normalized[action],
                    )
                    for action, keyword in commands.items()
                ]
            )
            session.flush()
            result = self._read(session, service_id)
            self.add_mutation(
                session, key, digest, result.model_dump(mode="json"), bot_service_id=service_id
            )
            return result
