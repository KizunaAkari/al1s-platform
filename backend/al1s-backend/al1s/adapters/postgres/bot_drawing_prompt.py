import unicodedata
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import delete, exists, func, select

from al1s.adapters.postgres.bot_drawing_models import (
    BotDrawingPromptProfileRow, BotDrawingPromptRevisionRow, BotDrawingSettingsRow, BotDrawingTaskRow,
)
from al1s.adapters.postgres.meme_base import fingerprint
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.bot_ai.settings import ADAPTERS
from al1s.meme_workshop.contracts import CommandSource


class DrawingPromptRepository:
    @staticmethod
    def _prompt_view(profile, revision):
        return {"id": str(profile.id), "name": profile.name, "row_version": profile.row_version,
                "revision": revision.revision, "adapter_kind": revision.adapter_kind,
                "model_id": revision.model_id, "base_url": revision.base_url,
                "network_mode": revision.network_mode, "secret_configured": revision.secret_id is not None}

    def list_prompt_models(self, service_id):
        with self.sessions() as session:
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id)
            if settings is None:
                return {"items": [], "active_id": None, "selection_version": 0}
            rows = session.execute(select(BotDrawingPromptProfileRow, BotDrawingPromptRevisionRow)
                .join(BotDrawingPromptRevisionRow,
                    (BotDrawingPromptRevisionRow.profile_id == BotDrawingPromptProfileRow.id)
                    & (BotDrawingPromptRevisionRow.revision == BotDrawingPromptProfileRow.current_revision))
                .where(BotDrawingPromptProfileRow.service_id == service_id,
                       BotDrawingPromptProfileRow.deleted_at.is_(None))
                .order_by(BotDrawingPromptProfileRow.created_at, BotDrawingPromptProfileRow.id).limit(21)).all()
            if len(rows) > 20:
                raise ValueError("drawing_prompt_catalog_invalid")
            return {"items": [self._prompt_view(profile, revision) for profile, revision in rows],
                    "active_id": str(settings.active_prompt_profile_id) if settings.active_prompt_profile_id else None,
                    "selection_version": settings.prompt_selection_version}

    def _prune_prompt_revisions(self, session, profile):
        used = exists(select(BotDrawingTaskRow.id).where(
            BotDrawingTaskRow.prompt_profile_id == BotDrawingPromptRevisionRow.profile_id,
            BotDrawingTaskRow.prompt_profile_revision == BotDrawingPromptRevisionRow.revision))
        query = select(BotDrawingPromptRevisionRow.revision, BotDrawingPromptRevisionRow.secret_id).where(
            BotDrawingPromptRevisionRow.profile_id == profile.id, ~used)
        if profile.deleted_at is None:
            query = query.where(BotDrawingPromptRevisionRow.revision != profile.current_revision)
        rows = session.execute(query.limit(100)).all()
        if rows:
            session.execute(delete(BotDrawingPromptRevisionRow).where(
                BotDrawingPromptRevisionRow.profile_id == profile.id,
                BotDrawingPromptRevisionRow.revision.in_([row.revision for row in rows])))
            session.flush()
            secret_ids = list({row.secret_id for row in rows if row.secret_id is not None})
            if secret_ids:
                from al1s.adapters.postgres.secret_repository import PostgresSecretRepository
                PostgresSecretRepository(session).delete_many_if_unreferenced(secret_ids)
        if profile.deleted_at is not None and not session.scalar(select(exists(
                select(BotDrawingPromptRevisionRow.profile_id).where(BotDrawingPromptRevisionRow.profile_id == profile.id)))):
            session.delete(profile)

    def save_prompt_model(self, service_id, data, *, profile_id, expected_version, operation_id):
        if self.cipher is None:
            raise ValueError("drawing_prompt_model_unconfigured")
        name = unicodedata.normalize("NFKC", data.name).strip()
        if not name or len(name) > 32 or any(not char.isprintable() for char in name):
            raise ValueError("drawing_prompt_name_invalid")
        if data.adapter_kind not in ADAPTERS["chat"]:
            raise ValueError("drawing_prompt_adapter_invalid")
        serialized = data.model_dump(mode="json")
        serialized["secret_input"] = (self.cipher.fingerprint(data.secret_input.get_secret_value())
                                      if data.secret_input else None)
        digest = fingerprint({"profile": str(profile_id) if profile_id else None,
                              "provider": serialized, "expected": expected_version})
        key = self.mutation_key("drawing_prompt_model", service_id, operation_id)
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            if settings is None:
                raise ValueError("drawing_settings_unavailable")
            duplicate = session.scalar(select(BotDrawingPromptProfileRow.id).where(
                BotDrawingPromptProfileRow.service_id == service_id,
                BotDrawingPromptProfileRow.normalized_name == name.casefold(),
                BotDrawingPromptProfileRow.deleted_at.is_(None),
                BotDrawingPromptProfileRow.id != profile_id if profile_id is not None else True))
            if duplicate is not None:
                raise ValueError("drawing_prompt_name_duplicate")
            if profile_id is None:
                if expected_version != 0:
                    raise ValueError("drawing_prompt_conflict")
                count = session.scalar(select(func.count()).select_from(BotDrawingPromptProfileRow).where(
                    BotDrawingPromptProfileRow.service_id == service_id,
                    BotDrawingPromptProfileRow.deleted_at.is_(None))) or 0
                if count >= 20:
                    raise ValueError("drawing_prompt_limit")
                profile = BotDrawingPromptProfileRow(id=uuid4(), service_id=service_id, name=name,
                    normalized_name=name.casefold(), current_revision=1, row_version=1)
                session.add(profile)
                session.flush([profile])
                old = None
            else:
                profile = session.get(BotDrawingPromptProfileRow, profile_id, with_for_update=True)
                if (profile is None or profile.service_id != service_id or profile.deleted_at is not None
                        or profile.row_version != expected_version):
                    raise ValueError("drawing_prompt_conflict")
                old = session.get(BotDrawingPromptRevisionRow, (profile.id, profile.current_revision))
                if old is None:
                    raise ValueError("drawing_prompt_catalog_invalid")
                profile.name, profile.normalized_name = name, name.casefold()
                profile.row_version += 1
                profile.updated_at = datetime.now(UTC)
            secret_id = old.secret_id if old is not None else None
            if data.clear_secret:
                secret_id = None
            elif data.secret_input:
                secret = EncryptedSecretRow(id=uuid4(), purpose=f"bot-drawing:{service_id}:prompt",
                    ciphertext=self.cipher.encrypt(data.secret_input.get_secret_value()),
                    key_id=self.cipher.key_id, row_version=1)
                session.add(secret)
                session.flush([secret])
                secret_id = secret.id
            values = (data.adapter_kind, data.model_id.strip(), data.base_url.strip(),
                      data.network_mode or "direct", secret_id)
            previous = ((old.adapter_kind, old.model_id, old.base_url, old.network_mode, old.secret_id)
                        if old is not None else None)
            if old is None or values != previous:
                if old is not None:
                    profile.current_revision += 1
                revision = BotDrawingPromptRevisionRow(profile_id=profile.id, revision=profile.current_revision,
                    service_id=service_id, adapter_kind=values[0], model_id=values[1], base_url=values[2],
                    network_mode=values[3], secret_id=values[4])
                session.add(revision)
            else:
                revision = old
            session.flush()
            self._prune_prompt_revisions(session, profile)
            result = self._prompt_view(profile, revision)
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return result

    def activate_prompt_model(self, service_id, profile_id, *, expected_version, operation_id):
        key = self.mutation_key("drawing_prompt_select", service_id, operation_id)
        digest = fingerprint({"profile": str(profile_id) if profile_id else None, "expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            if settings is None or settings.prompt_selection_version != expected_version:
                raise ValueError("drawing_prompt_conflict")
            if profile_id is not None:
                profile = session.get(BotDrawingPromptProfileRow, profile_id)
                if profile is None or profile.service_id != service_id or profile.deleted_at is not None:
                    raise ValueError("drawing_prompt_not_found")
            settings.active_prompt_profile_id = profile_id
            settings.prompt_selection_version += 1
            result = {"active_id": str(profile_id) if profile_id else None,
                      "selection_version": settings.prompt_selection_version}
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return result

    def delete_prompt_model(self, service_id, profile_id, *, expected_version, operation_id):
        key = self.mutation_key("drawing_prompt_delete", service_id, operation_id)
        digest = fingerprint({"profile": str(profile_id), "expected": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            profile = session.get(BotDrawingPromptProfileRow, profile_id, with_for_update=True)
            if (settings is None or profile is None or profile.service_id != service_id
                    or profile.deleted_at is not None or profile.row_version != expected_version):
                raise ValueError("drawing_prompt_conflict")
            if settings.active_prompt_profile_id == profile_id:
                settings.active_prompt_profile_id = None
                settings.prompt_selection_version += 1
            profile.deleted_at = datetime.now(UTC)
            profile.row_version += 1
            session.flush()
            self._prune_prompt_revisions(session, profile)
            result = {"deleted": True, "active_id": str(settings.active_prompt_profile_id)
                      if settings.active_prompt_profile_id else None,
                      "selection_version": settings.prompt_selection_version}
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return result

    def prompt_credentials(self, service_id, profile_id, revision):
        with self.sessions() as session:
            row = session.execute(select(BotDrawingPromptRevisionRow, EncryptedSecretRow)
                .outerjoin(EncryptedSecretRow, EncryptedSecretRow.id == BotDrawingPromptRevisionRow.secret_id)
                .where(BotDrawingPromptRevisionRow.service_id == service_id,
                       BotDrawingPromptRevisionRow.profile_id == profile_id,
                       BotDrawingPromptRevisionRow.revision == revision)).one_or_none()
            if row is None or row[1] is None or self.cipher is None:
                raise ValueError("drawing_prompt_model_unconfigured")
            provider, secret = row
            details = SimpleNamespace(adapter_kind=provider.adapter_kind, model_id=provider.model_id,
                base_url=provider.base_url, network_mode=provider.network_mode)
            return details, self.cipher.decrypt(secret.ciphertext, secret.key_id)

    def begin_prompt_expansion(self, task_id):
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or (task.generation_status != "queued" and not
                    (task.endpoint_key == "openai-images" and task.generation_status == "submitting"))
                    or task.cancel_requested):
                raise ValueError("drawing_task_conflict")
            source = CommandSource(message_id=task.source_message_id, scope_kind=task.scope_kind,
                                   target_id=task.target_id, sender_id=task.sender_id)
            self._allowed(session, task.service_id, source, require_applied=False)
            if task.prompt_status == "working" and task.prompt_profile_id is not None:
                return task.prompt_profile_id, task.prompt_profile_revision
            if task.prompt_status != "waiting" or not task.wish_text:
                raise ValueError("drawing_task_conflict")
            settings = session.get(BotDrawingSettingsRow, task.service_id, with_for_update=True)
            profile = (session.get(BotDrawingPromptProfileRow, settings.active_prompt_profile_id)
                       if settings and settings.active_prompt_profile_id is not None else None)
            if profile is None or profile.deleted_at is not None or profile.service_id != task.service_id:
                raise ValueError("drawing_prompt_model_unconfigured")
            revision = session.get(BotDrawingPromptRevisionRow, (profile.id, profile.current_revision))
            if revision is None or revision.secret_id is None:
                raise ValueError("drawing_prompt_model_unconfigured")
            task.prompt_profile_id, task.prompt_profile_revision = profile.id, revision.revision
            task.prompt_status, task.processing_stage = "working", "prompting"
            task.row_version += 1
            return profile.id, revision.revision

    def finish_prompt_expansion(self, task_id, profile_id, revision, positive, negative):
        if (not isinstance(positive, str) or not positive.strip() or not isinstance(negative, str)
                or len(positive) + len(negative) > 2000):
            raise ValueError("drawing_wish_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or (task.generation_status != "queued" and not
                    (task.endpoint_key == "openai-images" and task.generation_status == "submitting")) or task.cancel_requested
                    or task.prompt_status != "working" or task.prompt_profile_id != profile_id
                    or task.prompt_profile_revision != revision):
                raise ValueError("drawing_task_conflict")
            task.positive, task.negative = positive, negative
            task.prompt_status = "ready"
            task.processing_stage = "preparing_online" if task.endpoint_key == "openai-images" else "preparing_resources"
            task.row_version += 1
