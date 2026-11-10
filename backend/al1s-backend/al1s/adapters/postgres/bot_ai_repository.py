from datetime import UTC, datetime, timedelta
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_ai_models import (
    BotAiChatProfileRow, BotAiProviderRow, BotAiRequestRow, BotAiScopeRow,
    BotAiSessionProfileRow, BotAiSettingsRow,
)
from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.freqtrade_models import FreqtradeWordRow
from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import MemeAliasRow, MemeSettingsRow
from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.notification_models import EncryptedSecretRow, NotificationChannelRow
from al1s.adapters.postgres.quant_models import QuantCommandRow
from al1s.adapters.postgres.secret_repository import PostgresSecretRepository
from al1s.bot_ai.settings import (
    AiChatProfileView, AiChatProfileWrite, AiProviderView, AiSettingsRecord, AiSettingsWrite,
    AiSmartOptions,
)
from al1s.meme_workshop.contracts import CommandSource
from al1s.bot_ai.managed.smart_contract import SMART_RESULT_CODES
from al1s.bot_ai.contracts import LOCAL_GPU_ERROR_CODES
from al1s.secrets.security import FernetSecretCipher

ACTIVE = ("running", "stopping", "blocked")
TERMINAL = ("completed", "failed", "expired", "interrupted", "unknown")


class BotAiRepository(MemeRepositoryBase):
    def __init__(self, sessions: sessionmaker[Session], cipher: FernetSecretCipher) -> None:
        super().__init__(sessions)
        self.cipher = cipher

    @staticmethod
    def _service(session: Session, service_id: UUID) -> BotServiceRow:
        service = session.get(BotServiceRow, service_id)
        if service is None or service.deleted_at is not None or service.kind != "onebot_gateway":
            raise ValueError("qq_bot_not_found")
        return service

    @staticmethod
    def _settings(session: Session, service_id: UUID) -> AiSettingsRecord:
        row = session.get(BotAiSettingsRow, service_id)
        if row is None:
            meme = session.get(MemeSettingsRow, service_id)
            return AiSettingsRecord(service_id=service_id,
                event_ws_url=meme.qq_event_ws_url if meme else None)
        scopes = session.execute(select(BotAiScopeRow.target_id, BotAiScopeRow.reply_all,
                                         BotAiScopeRow.smart_reply)
            .where(BotAiScopeRow.service_id == service_id)
            .order_by(BotAiScopeRow.target_id).limit(100)).all()
        providers = session.scalars(select(BotAiProviderRow)
            .where(BotAiProviderRow.service_id == service_id).limit(4)).all()
        return AiSettingsRecord(
            service_id=service_id, enabled=row.enabled, event_ws_url=row.event_ws_url,
            allow_private=row.allow_private, groups=[target for target, _, _ in scopes],
            reply_all_groups=[target for target, reply_all, _ in scopes if reply_all],
            smart_reply_groups=[target for target, _, smart in scopes if smart],
            smart_reply=AiSmartOptions(**{name: getattr(row, "smart_" + name)
                                          for name in AiSmartOptions.model_fields}),
            wake_words=row.wake_words,
            persona=row.persona, vision_enabled=row.vision_enabled, stt_enabled=row.stt_enabled,
            tts_mode=row.tts_mode, desired_version=row.desired_version,
            applied_version=row.applied_version, last_error=row.last_error,
            providers={p.role: AiProviderView(adapter_kind=p.adapter_kind, model_id=p.model_id,
                base_url=p.base_url, voice=p.voice, secret_configured=p.secret_id is not None,
                network_mode=(p.options or {}).get("network_mode", "direct"))
                for p in providers},
        )

    def get_settings(self, service_id: UUID) -> AiSettingsRecord:
        with self.sessions() as session:
            self._service(session, service_id)
            return self._settings(session, service_id)

    def drawing_resource_credentials(self, service_id: UUID) -> tuple[str | None, bool]:
        from al1s.adapters.postgres.bot_drawing_models import BotDrawingSettingsRow
        with self.sessions() as session:
            row = session.execute(select(BotDrawingSettingsRow.enabled, EncryptedSecretRow.ciphertext,
                EncryptedSecretRow.key_id).outerjoin(EncryptedSecretRow,
                EncryptedSecretRow.id == BotDrawingSettingsRow.resource_secret_id)
                .where(BotDrawingSettingsRow.service_id == service_id)).one_or_none()
            if row is None:
                return None, False
            enabled, ciphertext, key_id = row
            return (self.cipher.decrypt(ciphertext, key_id) if ciphertext is not None else None, enabled)

    def save_settings(self, service_id: UUID, settings: AiSettingsWrite, *,
                      expected_version: int, operation_id: str) -> AiSettingsRecord:
        key = self.mutation_key("bot_ai_settings", service_id, operation_id)
        serialized = settings.model_dump(mode="json")
        for name in ("reply_all_groups", "smart_reply_groups", "smart_reply"):
            if getattr(settings, name) is None:
                serialized.pop(name)
        for role, provider in settings.providers.items():
            serialized["providers"][role]["secret_input"] = (
                self.cipher.fingerprint(provider.secret_input.get_secret_value())
                if provider.secret_input else None
            )
        digest = fingerprint({"settings": serialized, "expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return AiSettingsRecord.model_validate(replay)
            if any(item.network_mode == "proxy" and item.adapter_kind != "follow_chat"
                   for item in settings.providers.values()):
                from al1s.bot_ai.configuration import existing_https_proxy
                existing_https_proxy()
            self._service(session, service_id)
            row = session.get(BotAiSettingsRow, service_id, with_for_update=True)
            if (row is None and expected_version != 0) or (
                    row is not None and row.desired_version != expected_version):
                raise ValueError("ai_settings_conflict")
            if row is None:
                row = BotAiSettingsRow(service_id=service_id, desired_version=1,
                                       applied_version=0)
                session.add(row)
            else:
                row.desired_version += 1
            for name in ("enabled", "event_ws_url", "allow_private", "vision_enabled",
                         "stt_enabled", "tts_mode", "persona", "wake_words"):
                setattr(row, name, getattr(settings, name))
            if settings.smart_reply is not None:
                for name, value in settings.smart_reply.model_dump().items():
                    setattr(row, "smart_" + name, value)
            row.last_error = None
            session.flush()
            scopes = session.execute(select(BotAiScopeRow.target_id, BotAiScopeRow.reply_all,
                BotAiScopeRow.smart_reply).where(BotAiScopeRow.service_id == service_id)).all()
            selected = set(settings.reply_all_groups) if settings.reply_all_groups is not None else {
                target for target, reply_all, _ in scopes if reply_all}
            smart = set(settings.smart_reply_groups) if settings.smart_reply_groups is not None else {
                target for target, _, smart_reply in scopes if smart_reply}
            selected.intersection_update(settings.groups)
            smart.intersection_update(settings.groups)
            if selected.intersection(smart):
                raise ValueError("ai_group_reply_mode_conflict")
            session.execute(delete(BotAiScopeRow).where(BotAiScopeRow.service_id == service_id))
            session.add_all([BotAiScopeRow(service_id=service_id, scope_kind="qq_group",
                                           target_id=group, reply_all=group in selected,
                                           smart_reply=group in smart)
                             for group in settings.groups])
            existing = {p.role: p for p in session.scalars(select(BotAiProviderRow)
                .where(BotAiProviderRow.service_id == service_id).limit(4)).all()}
            for role, item in settings.providers.items():
                provider = existing.pop(role, None)
                if provider is None:
                    provider = BotAiProviderRow(service_id=service_id, role=role, options={})
                    session.add(provider)
                provider.adapter_kind, provider.model_id = item.adapter_kind, item.model_id
                provider.base_url, provider.voice = item.base_url, item.voice
                if item.network_mode is not None:
                    provider.options = {**(provider.options or {}), "network_mode": item.network_mode}
                if item.adapter_kind == "al1s_comfyui_vision":
                    provider.options = {**(provider.options or {}), "network_mode": "direct"}
                if item.clear_secret:
                    provider.secret_id = None
                elif item.secret_input:
                    secret_id = uuid4()
                    secret = EncryptedSecretRow(id=secret_id, purpose=f"bot-ai:{service_id}:{role}",
                        ciphertext=self.cipher.encrypt(item.secret_input.get_secret_value()),
                        key_id=self.cipher.key_id, row_version=1)
                    session.add(secret)
                    session.flush([secret])
                    provider.secret_id = secret_id
            for obsolete in existing.values():
                session.delete(obsolete)
            session.flush()
            self._sync_default_profile(session, service_id, settings.persona, settings.vision_enabled)
            session.flush()
            self._cleanup_secrets(session, service_id)
            result = self._settings(session, service_id)
            self.add_mutation(session, key, digest, result.model_dump(mode="json"),
                              bot_service_id=service_id)
            return result

    @staticmethod
    def _sync_default_profile(session: Session, service_id: UUID, persona: str,
                              vision_enabled: bool) -> None:
        provider = session.get(BotAiProviderRow, (service_id, "chat"))
        profile = session.scalar(select(BotAiChatProfileRow).where(
            BotAiChatProfileRow.service_id == service_id, BotAiChatProfileRow.is_default.is_(True)))
        if provider is None:
            if profile is not None:
                raise ValueError("ai_profile_default_required")
            return
        if profile is None:
            name = "大肥鱼" if urlsplit(provider.base_url).hostname == "api.deepseek.com" else "默认人格"
            vision = session.get(BotAiProviderRow, (service_id, "vision"))
            profile = BotAiChatProfileRow(id=uuid4(), service_id=service_id, name=name,
                normalized_name=name.casefold(), description="", is_default=True,
                supports_vision=vision_enabled and (vision is None or vision.adapter_kind == "follow_chat"))
            session.add(profile)
        profile.persona = persona
        profile.adapter_kind, profile.model_id = provider.adapter_kind, provider.model_id
        profile.base_url, profile.secret_id = provider.base_url, provider.secret_id
        profile.network_mode = (provider.options or {}).get("network_mode", "direct")

    def list_chat_profiles(self, service_id: UUID) -> list[AiChatProfileView]:
        with self.sessions() as session:
            self._service(session, service_id)
            rows = session.scalars(select(BotAiChatProfileRow)
                .where(BotAiChatProfileRow.service_id == service_id)
                .order_by(BotAiChatProfileRow.is_default.desc(), BotAiChatProfileRow.created_at,
                          BotAiChatProfileRow.id).limit(50)).all()
            return [self._chat_profile_view(row) for row in rows]

    @staticmethod
    def _chat_profile_view(row: BotAiChatProfileRow) -> AiChatProfileView:
        return AiChatProfileView(id=row.id, name=row.name, description=row.description,
            persona=row.persona, adapter_kind=row.adapter_kind, model_id=row.model_id,
            base_url=row.base_url, secret_configured=row.secret_id is not None,
            supports_vision=row.supports_vision, is_default=row.is_default,
            network_mode=row.network_mode)

    def save_chat_profile(self, service_id: UUID, profile_id: UUID, data: AiChatProfileWrite, *,
                          expected_version: int, operation_id: str) -> AiChatProfileView:
        key = self.mutation_key("bot_ai_chat_profile", service_id, operation_id)
        serialized = data.model_dump(mode="json")
        serialized["secret_input"] = (self.cipher.fingerprint(data.secret_input.get_secret_value())
                                      if data.secret_input else None)
        digest = fingerprint({"profile_id": str(profile_id), "profile": serialized,
                              "expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return AiChatProfileView.model_validate(replay)
            if data.network_mode == "proxy":
                from al1s.bot_ai.configuration import existing_https_proxy
                existing_https_proxy()
            self._service(session, service_id)
            settings = session.get(BotAiSettingsRow, service_id, with_for_update=True)
            created_settings = settings is None
            if created_settings and expected_version == 0:
                initial = self._settings(session, service_id)
                settings = BotAiSettingsRow(service_id=service_id, enabled=False,
                    event_ws_url=initial.event_ws_url, allow_private=False, vision_enabled=False,
                    stt_enabled=False, tts_mode="off", persona="", wake_words=[],
                    desired_version=1, applied_version=0)
                session.add(settings)
                session.flush()
            elif settings is None or settings.desired_version != expected_version:
                raise ValueError("ai_settings_conflict")
            profile = session.get(BotAiChatProfileRow, profile_id)
            if profile is not None and profile.service_id != service_id:
                raise ValueError("ai_profile_not_found")
            normalized = data.name.casefold()
            duplicate = session.scalar(select(BotAiChatProfileRow.id).where(
                BotAiChatProfileRow.service_id == service_id,
                BotAiChatProfileRow.normalized_name == normalized,
                BotAiChatProfileRow.id != profile_id))
            if duplicate is not None:
                raise ValueError("ai_profile_name_conflict")
            if profile is None:
                count = session.scalar(select(func.count()).select_from(BotAiChatProfileRow)
                                       .where(BotAiChatProfileRow.service_id == service_id))
                if count >= 50:
                    raise ValueError("ai_profile_limit")
                profile = BotAiChatProfileRow(id=profile_id, service_id=service_id, is_default=False,
                                              network_mode="direct")
                session.add(profile)
            profile.name, profile.normalized_name = data.name, normalized
            profile.description, profile.persona = data.description, data.persona
            profile.adapter_kind, profile.model_id = data.adapter_kind, data.model_id
            profile.base_url, profile.supports_vision = data.base_url, data.supports_vision
            if data.network_mode is not None:
                profile.network_mode = data.network_mode
            if data.clear_secret:
                profile.secret_id = None
            elif data.secret_input:
                secret_id = uuid4()
                secret = EncryptedSecretRow(id=secret_id, purpose=f"bot-ai:{service_id}:chat",
                    ciphertext=self.cipher.encrypt(data.secret_input.get_secret_value()),
                    key_id=self.cipher.key_id, row_version=1)
                session.add(secret)
                session.flush([secret])
                profile.secret_id = secret_id
            default = session.scalar(select(BotAiChatProfileRow).where(
                BotAiChatProfileRow.service_id == service_id,
                BotAiChatProfileRow.is_default.is_(True),
                BotAiChatProfileRow.id != profile_id))
            if data.is_default or (not profile.is_default and default is None):
                if settings.enabled and profile.secret_id is None and not profile.is_default:
                    raise ValueError("ai_profile_unconfigured")
                if default is not None:
                    default.is_default = False
                    session.flush()
                profile.is_default = True
            if profile.is_default:
                provider = session.get(BotAiProviderRow, (service_id, "chat"))
                if provider is None:
                    provider = BotAiProviderRow(service_id=service_id, role="chat", options={})
                    session.add(provider)
                provider.adapter_kind, provider.model_id = profile.adapter_kind, profile.model_id
                provider.base_url, provider.secret_id, provider.voice = profile.base_url, profile.secret_id, ""
                provider.options = {**(provider.options or {}), "network_mode": profile.network_mode}
                settings.persona = profile.persona
            if not created_settings:
                settings.desired_version += 1
            settings.last_error = None
            session.flush()
            self._cleanup_secrets(session, service_id)
            result = self._chat_profile_view(profile)
            self.add_mutation(session, key, digest, result.model_dump(mode="json"),
                              bot_service_id=service_id)
            return result

    def selected_chat_profile(self, service_id: UUID, scope: str, target: str, *,
                              version: int | None = None) -> AiChatProfileView | None:
        with self.sessions() as session:
            self._service(session, service_id)
            if version is not None:
                settings = session.get(BotAiSettingsRow, service_id, with_for_update=True)
                if (settings is None or settings.desired_version != version
                        or settings.applied_version != version):
                    raise ValueError("ai_sync_pending")
            selection = session.get(BotAiSessionProfileRow, (service_id, scope, target))
            profile = (session.get(BotAiChatProfileRow, selection.profile_id) if selection else
                session.scalar(select(BotAiChatProfileRow).where(
                    BotAiChatProfileRow.service_id == service_id,
                    BotAiChatProfileRow.is_default.is_(True))))
            return self._chat_profile_view(profile) if profile is not None else None

    def select_chat_profile(self, service_id: UUID, source: CommandSource, name: str,
                            version: int) -> AiChatProfileView:
        normalized = unicodedata.normalize("NFKC", name).strip().casefold()
        with self.sessions.begin() as session:
            self.lock(session)
            service = self._service(session, service_id)
            settings = session.get(BotAiSettingsRow, service_id, with_for_update=True)
            if (not service.enabled or settings is None or not settings.enabled
                    or settings.desired_version != version or settings.applied_version != version):
                raise ValueError("ai_scope_denied")
            if source.scope_kind == "qq_group":
                allowed = session.get(BotAiScopeRow, (service_id, "qq_group", source.target_id)) is not None
            else:
                allowed = (source.scope_kind == "qq_private" and settings.allow_private
                           and source.target_id == source.sender_id)
            if not allowed:
                raise ValueError("ai_scope_denied")
            profile = session.scalar(select(BotAiChatProfileRow).where(
                BotAiChatProfileRow.service_id == service_id,
                BotAiChatProfileRow.normalized_name == normalized))
            if profile is None:
                raise ValueError("ai_profile_not_found")
            if profile.secret_id is None and not profile.is_default:
                raise ValueError("ai_profile_unconfigured")
            key = (service_id, source.scope_kind, source.target_id)
            selection = session.get(BotAiSessionProfileRow, key)
            if selection is None:
                selection = BotAiSessionProfileRow(service_id=service_id, scope_kind=source.scope_kind,
                                                   target_id=source.target_id, profile_id=profile.id,
                                                   updated_at=datetime.now(UTC))
                session.add(selection)
            else:
                selection.profile_id, selection.updated_at = profile.id, datetime.now(UTC)
            return self._chat_profile_view(profile)

    def delete_chat_profile(self, service_id: UUID, profile_id: UUID, *, expected_version: int,
                            operation_id: str) -> dict:
        key = self.mutation_key("bot_ai_chat_profile_delete", service_id, operation_id)
        digest = fingerprint({"profile_id": str(profile_id), "expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay
            self._service(session, service_id)
            settings = session.get(BotAiSettingsRow, service_id, with_for_update=True)
            if settings is None or settings.desired_version != expected_version:
                raise ValueError("ai_settings_conflict")
            profile = session.get(BotAiChatProfileRow, profile_id)
            if profile is None or profile.service_id != service_id:
                raise ValueError("ai_profile_not_found")
            if profile.is_default:
                others = session.scalar(select(func.count()).select_from(BotAiChatProfileRow)
                    .where(BotAiChatProfileRow.service_id == service_id,
                           BotAiChatProfileRow.id != profile_id))
                if others or settings.enabled:
                    raise ValueError("ai_profile_default_required")
                provider = session.get(BotAiProviderRow, (service_id, "chat"))
                if provider is not None:
                    session.delete(provider)
            session.execute(delete(BotAiSessionProfileRow).where(
                BotAiSessionProfileRow.service_id == service_id,
                BotAiSessionProfileRow.profile_id == profile_id))
            session.delete(profile)
            settings.desired_version += 1
            settings.last_error = None
            session.flush()
            self._cleanup_secrets(session, service_id)
            result = {"deleted": str(profile_id), "desired_version": settings.desired_version}
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return result

    @staticmethod
    def _cleanup_secrets(session: Session, service_id: UUID) -> int:
        candidates = session.scalars(select(EncryptedSecretRow.id).where(
            EncryptedSecretRow.purpose.startswith(f"bot-ai:{service_id}:"),
            ~select(BotAiProviderRow.service_id).where(
                BotAiProviderRow.secret_id == EncryptedSecretRow.id).exists(),
            ~select(BotAiChatProfileRow.id).where(
                BotAiChatProfileRow.secret_id == EncryptedSecretRow.id).exists(),
            ~select(BotConfigVersionRow.id).where(
                BotConfigVersionRow.secret_id == EncryptedSecretRow.id).exists(),
            ~select(NotificationChannelRow.id).where(
                NotificationChannelRow.secret_id == EncryptedSecretRow.id).exists(),
        ).limit(100)).all()
        if candidates:
            try:
                with session.begin_nested():
                    return PostgresSecretRepository(session).delete_many_if_unreferenced(candidates)
            except IntegrityError:
                pass
        return 0

    def cleanup_secrets(self, service_id: UUID) -> int:
        with self.sessions.begin() as session:
            self.lock(session)
            return self._cleanup_secrets(session, service_id)

    def provider_secrets(self, service_id: UUID) -> dict[str, str]:
        with self.sessions() as session:
            rows = session.execute(select(BotAiProviderRow.role, EncryptedSecretRow)
                .join(EncryptedSecretRow, EncryptedSecretRow.id == BotAiProviderRow.secret_id)
                .where(BotAiProviderRow.service_id == service_id).limit(4)).all()
            return {role: self.cipher.decrypt(secret.ciphertext, secret.key_id)
                    for role, secret in rows}

    def chat_profile_secrets(self, service_id: UUID) -> dict[str, str]:
        with self.sessions() as session:
            rows = session.execute(select(BotAiChatProfileRow.id, EncryptedSecretRow)
                .join(EncryptedSecretRow, EncryptedSecretRow.id == BotAiChatProfileRow.secret_id)
                .where(BotAiChatProfileRow.service_id == service_id).limit(50)).all()
            return {str(identity): self.cipher.decrypt(secret.ciphertext, secret.key_id)
                    for identity, secret in rows}

    def pending_settings(self) -> list[tuple[UUID, int]]:
        with self.sessions() as session:
            return list(session.execute(select(BotAiSettingsRow.service_id,
                                               BotAiSettingsRow.desired_version)
                .where(BotAiSettingsRow.desired_version != BotAiSettingsRow.applied_version)
                .order_by(BotAiSettingsRow.service_id).limit(100)).all())

    def applied_settings(self) -> list[tuple[UUID, int]]:
        with self.sessions() as session:
            return list(session.execute(select(BotAiSettingsRow.service_id,
                                               BotAiSettingsRow.applied_version)
                .join(BotServiceRow, BotServiceRow.id == BotAiSettingsRow.service_id)
                .where(BotAiSettingsRow.applied_version > 0, BotServiceRow.deleted_at.is_(None))
                .order_by(BotAiSettingsRow.service_id).limit(100)).all())

    def mark_applied(self, service_id: UUID, version: int) -> bool:
        with self.sessions.begin() as session:
            return session.scalar(update(BotAiSettingsRow)
                .where(BotAiSettingsRow.service_id == service_id,
                       BotAiSettingsRow.desired_version == version)
                .values(applied_version=version, last_error=None)
                .returning(BotAiSettingsRow.service_id)) is not None

    def mark_error(self, service_id: UUID, version: int, error: str) -> bool:
        if not error or len(error) > 64:
            raise ValueError("ai_error_invalid")
        with self.sessions.begin() as session:
            return session.scalar(update(BotAiSettingsRow)
                .where(BotAiSettingsRow.service_id == service_id,
                       BotAiSettingsRow.desired_version == version)
                .values(last_error=error).returning(BotAiSettingsRow.service_id)) is not None

    def qq_connections(self) -> list[QqConnection]:
        with self.sessions() as session:
            rows = session.execute(select(BotAiSettingsRow, BotConfigVersionRow,
                    MemeSettingsRow.cooldown_seconds, EncryptedSecretRow.ciphertext,
                    EncryptedSecretRow.key_id)
                .join(BotServiceRow, BotServiceRow.id == BotAiSettingsRow.service_id)
                .join(BotConfigVersionRow, BotConfigVersionRow.id == BotServiceRow.applied_config_version_id)
                .outerjoin(MemeSettingsRow, MemeSettingsRow.bot_service_id == BotServiceRow.id)
                .outerjoin(EncryptedSecretRow, EncryptedSecretRow.id == BotConfigVersionRow.secret_id)
                .where(BotAiSettingsRow.enabled, BotAiSettingsRow.event_ws_url.is_not(None),
                    BotAiSettingsRow.desired_version == BotAiSettingsRow.applied_version,
                    BotAiSettingsRow.applied_version > 0, BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None), BotServiceRow.kind == "onebot_gateway",
                    BotConfigVersionRow.service_id == BotServiceRow.id)).all()
            result = []
            for settings, version, cooldown, ciphertext, key_id in rows:
                base = version.settings.get("ONEBOT_BASE_URL")
                if isinstance(base, str) and settings.event_ws_url:
                    result.append(QqConnection(settings.service_id, version.id,
                        settings.desired_version, settings.event_ws_url, base, cooldown or 5,
                        ciphertext, key_id))
            return result

    def allowed(self, service_id: UUID, source: CommandSource, version: int, *,
                admission: bool = False) -> bool:
        with self.sessions() as session:
            query = select(BotAiSettingsRow.enabled, BotAiSettingsRow.allow_private)
            query = (query
                .join(BotServiceRow, BotServiceRow.id == BotAiSettingsRow.service_id)
                .where(BotAiSettingsRow.service_id == service_id, BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None),
                    BotAiSettingsRow.applied_version == version))
            if admission:
                query = query.where(BotAiSettingsRow.desired_version == version)
            row = session.execute(query).one_or_none()
            if row is None or not row.enabled:
                return False
            if source.scope_kind == "qq_private":
                return row.allow_private and source.target_id == source.sender_id
            return source.scope_kind == "qq_group" and session.get(
                BotAiScopeRow, (service_id, "qq_group", source.target_id)) is not None

    def trigger_snapshots(self, service_ids: list[UUID]) -> dict[UUID, AiSettingsRecord]:
        if len(service_ids) > 100:
            raise ValueError("ai_service_limit")
        if not service_ids:
            return {}
        with self.sessions() as session:
            rows = session.scalars(select(BotAiSettingsRow)
                .where(BotAiSettingsRow.service_id.in_(service_ids)).limit(len(service_ids))).all()
            scopes = session.execute(select(BotAiScopeRow.service_id, BotAiScopeRow.target_id,
                BotAiScopeRow.reply_all, BotAiScopeRow.smart_reply)
                .where(BotAiScopeRow.service_id.in_(service_ids))
                .order_by(BotAiScopeRow.service_id, BotAiScopeRow.target_id)
                .limit(len(service_ids) * 100)).all()
            groups: dict[UUID, list[str]] = {}
            reply_all_groups: dict[UUID, list[str]] = {}
            smart_reply_groups: dict[UUID, list[str]] = {}
            for service_id, target, reply_all, smart_reply in scopes:
                groups.setdefault(service_id, []).append(target)
                if reply_all:
                    reply_all_groups.setdefault(service_id, []).append(target)
                if smart_reply:
                    smart_reply_groups.setdefault(service_id, []).append(target)
            providers = session.scalars(select(BotAiProviderRow)
                .where(BotAiProviderRow.service_id.in_(service_ids))
                .limit(len(service_ids) * 4)).all()
            by_service: dict[UUID, dict[str, AiProviderView]] = {}
            for provider in providers:
                by_service.setdefault(provider.service_id, {})[provider.role] = AiProviderView(
                    adapter_kind=provider.adapter_kind, model_id=provider.model_id,
                    base_url=provider.base_url, voice=provider.voice,
                    secret_configured=provider.secret_id is not None,
                    network_mode=(provider.options or {}).get("network_mode", "direct"))
            return {row.service_id: AiSettingsRecord(
                service_id=row.service_id, enabled=row.enabled, event_ws_url=row.event_ws_url,
                allow_private=row.allow_private, groups=groups.get(row.service_id, []),
                reply_all_groups=reply_all_groups.get(row.service_id, []),
                smart_reply_groups=smart_reply_groups.get(row.service_id, []),
                smart_reply=AiSmartOptions(**{name: getattr(row, "smart_" + name)
                                              for name in AiSmartOptions.model_fields}),
                wake_words=row.wake_words, persona=row.persona, vision_enabled=row.vision_enabled,
                stt_enabled=row.stt_enabled, tts_mode=row.tts_mode,
                providers=by_service.get(row.service_id, {}),
                desired_version=row.desired_version, applied_version=row.applied_version,
                last_error=row.last_error) for row in rows}

    def is_reserved(self, service_id: UUID, word: str) -> bool:
        if not word or len(word) > 64:
            return False
        with self.sessions() as session:
            row = session.execute(select(
                select(MemeAliasRow.normalized_alias)
                    .where(MemeAliasRow.normalized_alias == word).exists(),
                select(QuantCommandRow.action).where(QuantCommandRow.service_id == service_id,
                    QuantCommandRow.normalized_keyword == word).exists(),
                select(FreqtradeWordRow.action).where(FreqtradeWordRow.service_id == service_id,
                    FreqtradeWordRow.normalized_keyword == word).exists(),
            )).one()
            return any(row)

    def claim(self, service_id: UUID, source: CommandSource, config_version: int,
              now: datetime) -> UUID | None:
        if (source.scope_kind not in {"qq_group", "qq_private"} or config_version < 1
                or now.tzinfo is None):
            raise ValueError("ai_request_invalid")
        identity = uuid4()
        with self.sessions.begin() as session:
            return session.scalar(
                insert(BotAiRequestRow).values(
                    id=identity, service_id=service_id, scope_kind=source.scope_kind,
                    target_id=source.target_id, sender_id=source.sender_id,
                    source_message_id=source.message_id, config_version=config_version,
                    status="queued", send_status="none", created_at=now,
                    expires_at=now + timedelta(days=7), sent_count=0,
                ).on_conflict_do_nothing(constraint="uq_bot_ai_source_message")
                .returning(BotAiRequestRow.id)
            )

    def activate(self, request_id: UUID, epoch: str) -> bool:
        if not epoch or len(epoch) > 32:
            raise ValueError("ai_epoch_invalid")
        try:
            with self.sessions.begin() as session:
                return session.scalar(
                    update(BotAiRequestRow).where(BotAiRequestRow.id == request_id,
                        BotAiRequestRow.status == "queued")
                    .values(status="running", epoch=epoch, started_at=datetime.now(UTC))
                    .returning(BotAiRequestRow.id)
                ) is not None
        except IntegrityError:
            return False

    def start_send(self, request_id: UUID) -> bool:
        with self.sessions.begin() as session:
            return session.scalar(
                update(BotAiRequestRow).where(BotAiRequestRow.id == request_id,
                    BotAiRequestRow.status == "running",
                    BotAiRequestRow.send_status.in_(("none", "ready")))
                .values(send_status="sending").returning(BotAiRequestRow.id)
            ) is not None

    def finish_send(self, request_id: UUID, status: str, sent_count: int) -> bool:
        if status not in {"sent", "failed", "unknown"} or not 0 <= sent_count <= 5:
            raise ValueError("ai_send_result_invalid")
        with self.sessions.begin() as session:
            return session.scalar(
                update(BotAiRequestRow).where(BotAiRequestRow.id == request_id,
                    BotAiRequestRow.send_status == "sending")
                .values(send_status=status, sent_count=sent_count).returning(BotAiRequestRow.id)
            ) is not None

    def complete(self, request_id: UUID) -> bool:
        with self.sessions.begin() as session:
            return session.scalar(
                update(BotAiRequestRow).where(BotAiRequestRow.id == request_id,
                    BotAiRequestRow.status == "running",
                    BotAiRequestRow.send_status.in_(("none", "sent", "failed", "unknown")))
                .values(status=case((BotAiRequestRow.send_status == "unknown", "unknown"),
                                    else_="completed"), finished_at=datetime.now(UTC))
                .returning(BotAiRequestRow.id)
            ) is not None

    def begin_stop(self, request_id: UUID) -> bool:
        with self.sessions.begin() as session:
            return session.scalar(update(BotAiRequestRow)
                .where(BotAiRequestRow.id == request_id, BotAiRequestRow.status == "running")
                .values(status="stopping").returning(BotAiRequestRow.id)) is not None

    def block(self, request_id: UUID, *, reason: str = "stop_unconfirmed") -> bool:
        if reason not in {"stop_unconfirmed", "meme_pending", "send_pending"}:
            raise ValueError("ai_block_reason_invalid")
        with self.sessions.begin() as session:
            return session.scalar(update(BotAiRequestRow)
                .where(BotAiRequestRow.id == request_id,
                       BotAiRequestRow.status.in_(("running", "stopping")))
                .values(status="blocked", result_code=reason).returning(BotAiRequestRow.id)) is not None

    def confirm_finished(self, request_id: UUID, native_status: str, *,
                         result_code: str | None = None) -> bool:
        if native_status not in {"completed", "failed", "interrupted", "unknown"}:
            raise ValueError("ai_completion_invalid")
        if result_code is not None and result_code not in SMART_RESULT_CODES | LOCAL_GPU_ERROR_CODES:
            raise ValueError("ai_completion_invalid")
        status = case(
            (BotAiRequestRow.send_status == "unknown", "unknown"),
            (BotAiRequestRow.send_status == "failed", "failed"),
            (BotAiRequestRow.status == "stopping", "interrupted"),
            (and_(BotAiRequestRow.status == "blocked",
                  or_(BotAiRequestRow.result_code.is_(None),
                      BotAiRequestRow.result_code.not_in(("meme_pending", "send_pending")))), "interrupted"),
            else_=native_status,
        )
        detail = {"result_code": result_code} if result_code is not None else {}
        with self.sessions.begin() as session:
            return session.scalar(update(BotAiRequestRow)
                .where(BotAiRequestRow.id == request_id, BotAiRequestRow.status.in_(ACTIVE),
                       BotAiRequestRow.send_status != "sending")
                .values(status=status, finished_at=datetime.now(UTC), **detail)
                .returning(BotAiRequestRow.id)) is not None

    def fail_queued(self, request_id: UUID, code: str) -> bool:
        if not code or len(code) > 64:
            raise ValueError("ai_error_invalid")
        with self.sessions.begin() as session:
            return session.scalar(update(BotAiRequestRow)
                .where(BotAiRequestRow.id == request_id, BotAiRequestRow.status == "queued")
                .values(status="failed", result_code=code, finished_at=datetime.now(UTC))
                .returning(BotAiRequestRow.id)) is not None

    def blocked_sessions(self, service_id: UUID) -> list[tuple[UUID, str, str]]:
        with self.sessions() as session:
            return list(session.execute(select(BotAiRequestRow.id, BotAiRequestRow.scope_kind,
                    BotAiRequestRow.target_id)
                .where(BotAiRequestRow.service_id == service_id, BotAiRequestRow.status == "blocked")
                .order_by(BotAiRequestRow.created_at, BotAiRequestRow.id).limit(100)).all())

    def get(self, request_id: UUID) -> BotAiRequestRow | None:
        with self.sessions() as session:
            return session.get(BotAiRequestRow, request_id)

    def list_requests(self, service_id: UUID, *, after_id: UUID | None = None,
                      limit: int = 20) -> dict:
        if not 1 <= limit <= 50:
            raise ValueError("ai_request_limit_invalid")
        with self.sessions() as session:
            self._service(session, service_id)
            query = select(BotAiRequestRow).where(BotAiRequestRow.service_id == service_id)
            if after_id is not None:
                cursor = session.execute(select(BotAiRequestRow.created_at, BotAiRequestRow.id)
                    .where(BotAiRequestRow.service_id == service_id,
                           BotAiRequestRow.id == after_id)).one_or_none()
                if cursor is None:
                    raise ValueError("ai_cursor_invalid")
                query = query.where(or_(BotAiRequestRow.created_at < cursor.created_at,
                    and_(BotAiRequestRow.created_at == cursor.created_at,
                         BotAiRequestRow.id < cursor.id)))
            rows = session.scalars(query.order_by(BotAiRequestRow.created_at.desc(),
                BotAiRequestRow.id.desc()).limit(limit + 1)).all()
            items = [{"id": str(row.id), "scope_kind": row.scope_kind,
                      "target_id": row.target_id, "sender_id": row.sender_id,
                      "status": row.status, "send_status": row.send_status,
                      "result_code": row.result_code,
                      "created_at": (row.created_at if row.created_at.tzinfo
                                     else row.created_at.replace(tzinfo=UTC)).isoformat()}
                     for row in rows[:limit]]
            return {"items": items, "next_after_id": str(rows[limit - 1].id)
                    if len(rows) > limit else None}

    def recover(self, *, native_stopped: bool) -> int:
        with self.sessions.begin() as session:
            queued = session.execute(update(BotAiRequestRow)
                .where(BotAiRequestRow.status == "queued")
                .values(status="interrupted", finished_at=datetime.now(UTC)))
            statuses = ACTIVE if native_stopped else ("running", "stopping")
            active = session.execute(update(BotAiRequestRow)
                .where(BotAiRequestRow.status.in_(statuses))
                .values(status=(case((BotAiRequestRow.send_status == "sending", "unknown"),
                                     else_="interrupted") if native_stopped else "blocked"),
                        send_status=case((BotAiRequestRow.send_status == "sending", "unknown"),
                                         else_=BotAiRequestRow.send_status)))
            return int(queued.rowcount or 0) + int(active.rowcount or 0)

    def cleanup(self, now: datetime, *, limit: int = 100) -> int:
        if now.tzinfo is None or not 1 <= limit <= 100:
            raise ValueError("ai_cleanup_invalid")
        with self.sessions.begin() as session:
            identities = session.scalars(select(BotAiRequestRow.id)
                .where(BotAiRequestRow.status.in_(TERMINAL), BotAiRequestRow.expires_at <= now)
                .order_by(BotAiRequestRow.expires_at, BotAiRequestRow.id).limit(limit)
                .with_for_update(skip_locked=True)).all()
            if identities:
                session.execute(delete(BotAiRequestRow).where(BotAiRequestRow.id.in_(identities)))
            return len(identities)
