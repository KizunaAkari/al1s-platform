from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import MemeScopeRow, MemeSettingsRow
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.meme_workshop.contracts import CommandSource, ConnectionSettings, Scope
from al1s.meme_workshop.records import SettingsRecord


@dataclass(frozen=True)
class QqConnection:
    service_id: UUID
    config_version_id: UUID
    row_version: int
    event_url: str
    base_url: str
    cooldown_seconds: int
    secret_ciphertext: str | None = field(repr=False)
    secret_key_id: str | None = field(default=None, repr=False)


class MemeSettingsRepository(MemeRepositoryBase):
    @staticmethod
    def _settings(session: Session, bot_id: UUID) -> SettingsRecord:
        row = session.get(MemeSettingsRow, bot_id)
        if row is None:
            return SettingsRecord(bot_service_id=bot_id)
        scopes = session.scalars(
            select(MemeScopeRow)
            .where(
                MemeScopeRow.bot_service_id == bot_id,
            )
            .order_by(MemeScopeRow.scope_kind, MemeScopeRow.target_id)
        ).all()
        return SettingsRecord(
            bot_service_id=bot_id,
            enabled=row.enabled,
            private_enabled=row.private_enabled,
            qq_event_ws_url=row.qq_event_ws_url,
            cooldown_seconds=row.cooldown_seconds,
            row_version=row.row_version,
            scopes=[
                Scope.model_validate(
                    {
                        "kind": scope.scope_kind,
                        "target_id": scope.target_id,
                        "guild_id": scope.guild_id,
                    }
                )
                for scope in scopes
            ],
        )

    def get_settings(self, bot_id: UUID) -> SettingsRecord:
        with self.sessions() as session:
            service = session.get(BotServiceRow, bot_id)
            if service is None or service.deleted_at:
                raise ValueError("bot_not_found")
            return self._settings(session, bot_id)

    def save_settings(
        self, bot_id: UUID, data: ConnectionSettings, *, expected_version: int, operation_id: str
    ) -> SettingsRecord:
        key = self.mutation_key("settings", bot_id, operation_id)
        digest = fingerprint({"data": data.model_dump(mode="json"), "expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return SettingsRecord.model_validate(replay)
            service = session.get(BotServiceRow, bot_id)
            if service is None or service.deleted_at:
                raise ValueError("bot_not_found")
            if any(
                (scope.kind == "qq_group") != (service.kind == "onebot_gateway")
                for scope in data.scopes
            ):
                raise ValueError("scope_platform_mismatch")
            if service.kind == "discord_bridge" and data.qq_event_ws_url:
                raise ValueError("discord_has_no_onebot_endpoint")
            row = session.get(MemeSettingsRow, bot_id, with_for_update=True)
            if (row is None and expected_version != 0) or (
                row and row.row_version != expected_version
            ):
                raise ValueError("settings_conflict")
            if row is None:
                row = MemeSettingsRow(bot_service_id=bot_id, row_version=1)
                session.add(row)
            else:
                row.row_version += 1
            row.enabled, row.private_enabled = data.enabled, data.private_enabled
            row.qq_event_ws_url, row.cooldown_seconds = data.qq_event_ws_url, data.cooldown_seconds
            session.flush()
            session.execute(delete(MemeScopeRow).where(MemeScopeRow.bot_service_id == bot_id))
            session.add_all(
                MemeScopeRow(
                    bot_service_id=bot_id,
                    scope_kind=scope.kind,
                    target_id=scope.target_id,
                    guild_id=scope.guild_id,
                )
                for scope in data.scopes
            )
            session.flush()
            result = self._settings(session, bot_id)
            self.add_mutation(
                session, key, digest, result.model_dump(mode="json"), bot_service_id=bot_id
            )
            return result

    @staticmethod
    def allowed_in(
        session: Session, bot_id: UUID, source: CommandSource, *, lock: bool = False
    ) -> bool:
        query = (
            select(MemeSettingsRow, BotServiceRow.kind)
            .join(
                BotServiceRow,
                BotServiceRow.id == MemeSettingsRow.bot_service_id,
            )
            .where(
                MemeSettingsRow.bot_service_id == bot_id,
                MemeSettingsRow.enabled,
                BotServiceRow.enabled,
                BotServiceRow.deleted_at.is_(None),
            )
        )
        if lock:
            query = query.with_for_update(read=True, of=[MemeSettingsRow, BotServiceRow])
        pair = session.execute(query).first()
        if pair is None:
            return False
        row, kind = pair
        if source.scope_kind.startswith("qq_") != (kind == "onebot_gateway"):
            return False
        if source.scope_kind.endswith("_private"):
            return bool(row.private_enabled)
        scope = session.get(MemeScopeRow, (bot_id, source.scope_kind, source.target_id))
        return scope is not None and (scope.guild_id is None or scope.guild_id == source.guild_id)

    def allowed(self, bot_id: UUID, source: CommandSource) -> bool:
        with self.sessions() as session:
            return self.allowed_in(session, bot_id, source)

    def qq_connections(self) -> list[QqConnection]:
        with self.sessions() as session:
            rows = session.execute(
                select(
                    MemeSettingsRow,
                    BotConfigVersionRow,
                    EncryptedSecretRow.ciphertext,
                    EncryptedSecretRow.key_id,
                )
                .join(BotServiceRow, BotServiceRow.id == MemeSettingsRow.bot_service_id)
                .join(
                    BotConfigVersionRow,
                    BotConfigVersionRow.id == BotServiceRow.applied_config_version_id,
                )
                .outerjoin(
                    EncryptedSecretRow, EncryptedSecretRow.id == BotConfigVersionRow.secret_id
                )
                .where(
                    MemeSettingsRow.enabled,
                    MemeSettingsRow.qq_event_ws_url.is_not(None),
                    BotServiceRow.kind == "onebot_gateway",
                    BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None),
                    BotConfigVersionRow.service_id == BotServiceRow.id,
                )
                .order_by(BotServiceRow.id)
                .limit(100)
            ).all()
            result: list[QqConnection] = []
            for settings, version, ciphertext, key_id in rows:
                base: Any = version.settings.get("ONEBOT_BASE_URL")
                if not isinstance(base, str) or not settings.qq_event_ws_url:
                    continue
                result.append(
                    QqConnection(
                        settings.bot_service_id,
                        version.id,
                        settings.row_version,
                        settings.qq_event_ws_url,
                        base,
                        settings.cooldown_seconds,
                        ciphertext,
                        key_id,
                    )
                )
            return result
