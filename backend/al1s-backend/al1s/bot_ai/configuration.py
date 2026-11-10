import asyncio
import contextlib
import os
from urllib.parse import urlsplit
from uuid import UUID


class AiConfigurationError(ValueError):
    pass


def existing_https_proxy() -> str:
    value = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise AiConfigurationError("ai_proxy_credentials_unsupported")
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(char.isspace() for char in value)):
            raise AiConfigurationError("ai_proxy_unavailable")
        _ = parsed.port
    except ValueError as error:
        if str(error) in {"ai_proxy_unavailable", "ai_proxy_credentials_unsupported"}:
            raise
        raise AiConfigurationError("ai_proxy_unavailable") from error
    return value


class ConfigController:
    def __init__(self, repository, engine, hub, sessions) -> None:
        self.repository, self.engine, self.hub, self.sessions = repository, engine, hub, sessions
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._latest: dict[UUID, int] = {}
        self._reconciler: asyncio.Task | None = None
        self._cleanup_pending: dict[UUID, int] = {}
        self._initial = asyncio.Event()

    def start(self) -> None:
        if self._reconciler is None:
            self._reconciler = asyncio.create_task(self._reconcile())

    async def wait_initial(self) -> None:
        await self._initial.wait()

    async def _reconcile(self) -> None:
        discover_cleanup = True
        first = True
        try:
            while True:
                try:
                    pending = await asyncio.to_thread(self.repository.pending_settings)
                    for service_id, version in pending:
                        if service_id not in self._tasks:
                            self.request_apply(service_id, version)
                    if discover_cleanup or getattr(self.engine, 'local_runtime', False):
                        applied = await asyncio.to_thread(self.repository.applied_settings)
                        for service_id, version in applied:
                            if discover_cleanup:
                                self._cleanup_pending[service_id] = max(
                                    version, self._cleanup_pending.get(service_id, 0))
                            if (getattr(self.engine, 'local_runtime', False) and service_id not in self._tasks
                                    and self.engine.needs_rehydrate(service_id, version)):
                                self.request_apply(service_id, version)
                        discover_cleanup = False
                    for service_id, version in list(self._cleanup_pending.items()):
                        if service_id not in self._tasks:
                            await self._cleanup(service_id, version)
                except Exception:
                    pass
                if first:
                    tasks = tuple(self._tasks.values())
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    self._initial.set()
                    first = False
                await asyncio.sleep(30)
        finally:
            self._initial.set()

    def request_apply(self, service_id: UUID, version: int) -> None:
        self._pause(service_id)
        self._latest[service_id] = max(version, self._latest.get(service_id, 0))
        if service_id not in self._tasks:
            task = asyncio.create_task(self._run(service_id))
            self._tasks[service_id] = task
            task.add_done_callback(lambda done: self._finished(service_id, done))

    def _finished(self, service_id: UUID, task: asyncio.Task) -> None:
        if self._tasks.get(service_id) is task:
            self._tasks.pop(service_id, None)
        if service_id in self._latest:
            self.request_apply(service_id, self._latest[service_id])

    async def _run(self, service_id: UUID) -> None:
        while service_id in self._latest:
            version = self._latest.pop(service_id)
            await self.apply(service_id, version)

    def _pause(self, service_id: UUID) -> None:
        self.sessions.pause(service_id)

    def _resume(self, service_id: UUID) -> None:
        self.sessions.resume(service_id)

    async def _cleanup(self, service_id: UUID, version: int) -> None:
        try:
            await self.engine.cleanup(service_id, version)
            deleted = await asyncio.to_thread(self.repository.cleanup_secrets, service_id)
            if deleted == 100:
                raise AiConfigurationError("ai_secret_cleanup_pending")
        except Exception:
            self._cleanup_pending[service_id] = max(version, self._cleanup_pending.get(service_id, 0))
        else:
            if self._cleanup_pending.get(service_id, 0) <= version:
                self._cleanup_pending.pop(service_id, None)

    async def apply(self, service_id: UUID, version: int) -> None:
        binding = None
        profile = None
        switched = False
        try:
            record = await asyncio.to_thread(self.repository.get_settings, service_id)
            if record.desired_version != version:
                return
            cancel_vision = getattr(self.engine, "cancel_waiting_vision", None)
            if not record.enabled or not record.vision_enabled:
                if cancel_vision is not None:
                    cancel_vision(service_id)
                cancel_queued = getattr(self.sessions, "cancel_waiting_vision", None)
                if cancel_queued is not None:
                    cancel_queued(service_id)
            state = self.hub.status(service_id)
            if (record.applied_version == version and
                    (not getattr(self.engine, 'local_runtime', False) or (not record.enabled and not state.get('connected'))
                     or (record.enabled and state.get('connected') and state.get('bridge_version') == version))):
                if not record.enabled and getattr(self.engine, 'local_runtime', False):
                    self.engine.note_disabled(service_id, version)
                self._resume(service_id)
                return
            self._pause(service_id)
            secrets = await asyncio.to_thread(self.repository.provider_secrets, service_id)
            if record.enabled:
                chat_profiles = await asyncio.to_thread(self.repository.list_chat_profiles, service_id)
                chat_secrets = await asyncio.to_thread(self.repository.chat_profile_secrets, service_id)
                profile = await self.engine.stage(record, secrets, chat_profiles=chat_profiles,
                                                  chat_secrets=chat_secrets)
            else:
                profile = None
            binding = await self.engine.binding(service_id)
            if not await self.sessions.wait_idle(service_id):
                raise ValueError("ai_drain_unconfirmed")
            current = await asyncio.to_thread(self.repository.get_settings, service_id)
            if current.desired_version != version:
                return
            switched = True
            await self.engine.activate(record, profile, binding)
            if record.enabled:
                async with asyncio.timeout(30):
                    while True:
                        state = self.hub.status(service_id)
                        if state.get("connected") and state.get("bridge_version") == version:
                            break
                        await asyncio.sleep(0.2)
            if not await asyncio.to_thread(self.repository.mark_applied, service_id, version):
                raise ValueError("ai_settings_changed")
            self._resume(service_id)
            await self._cleanup(service_id, version)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if switched and binding is not None:
                with contextlib.suppress(Exception):
                    await self.engine.restore(service_id, binding)
            transient = {"ai_sticker_queue_pending", "ai_vision_cleanup_pending", "ai_drawing_busy"}
            code = (str(error) if ((isinstance(error, AiConfigurationError) and str(error) in {
                "ai_proxy_unavailable", "ai_proxy_credentials_unsupported"}) or
                (isinstance(error, ValueError) and str(error) in transient)) else "ai_apply_failed")
            await asyncio.to_thread(self.repository.mark_error, service_id, version,
                                    code)
        finally:
            discard = getattr(self.engine, 'discard', None)
            if profile is not None and discard is not None:
                with contextlib.suppress(Exception):
                    await discard(service_id, profile)

    def status(self, service_id: UUID) -> dict:
        return {"state": "applying" if service_id in self._tasks else "idle",
                **self.hub.status(service_id)}

    async def test_provider(self, service_id: UUID, role: str) -> dict:
        record = await asyncio.to_thread(self.repository.get_settings, service_id)
        if (not record.enabled or record.desired_version != record.applied_version
                or role not in record.providers):
            raise ValueError("ai_sync_pending")
        if role == "chat" or record.providers[role].adapter_kind == "follow_chat":
            profiles = await asyncio.to_thread(self.repository.list_chat_profiles, service_id)
            default = next((profile for profile in profiles if profile.is_default), None)
            if default is None:
                raise ValueError("ai_profile_unconfigured")
            return await self.engine.test_chat_profile(service_id, record.applied_version, default.id)
        return await self.engine.test_provider(service_id, record.applied_version, role,
                                               record.providers[role])

    async def test_chat_profile(self, service_id: UUID, profile_id: UUID) -> dict:
        record = await asyncio.to_thread(self.repository.get_settings, service_id)
        if not record.enabled or record.desired_version != record.applied_version:
            raise ValueError("ai_sync_pending")
        profiles = await asyncio.to_thread(self.repository.list_chat_profiles, service_id)
        profile = next((item for item in profiles if item.id == profile_id), None)
        if profile is None:
            raise ValueError("ai_profile_not_found")
        if not profile.secret_configured and not profile.is_default:
            raise ValueError("ai_profile_unconfigured")
        return await self.engine.test_chat_profile(service_id, record.applied_version, profile_id)

    async def clear_session(self, service_id: UUID, scope: str, target: str) -> dict:
        return await self.sessions.clear_session(service_id, scope, target)

    async def stickers(self, service_id, command):
        record = await asyncio.to_thread(self.repository.get_settings, service_id)
        if command.group_id not in record.groups:
            raise ValueError("ai_sticker_scope_invalid")
        if not record.enabled or record.desired_version != record.applied_version:
            raise ValueError("ai_sync_pending")
        return await self.hub.sticker_control(service_id, command, record.applied_version)

    async def import_sticker(self, service_id, group_id, request_id, media_ref):
        from al1s.bot_ai.managed.smart_contract import StickerImportCommand

        record = await asyncio.to_thread(self.repository.get_settings, service_id)
        if group_id not in record.groups:
            raise ValueError("ai_sticker_scope_invalid")
        if not record.enabled or record.desired_version != record.applied_version:
            raise ValueError("ai_sync_pending")
        if not record.vision_enabled:
            raise ValueError("ai_sticker_vision_unavailable")
        vision = record.providers.get("vision")
        if vision and vision.adapter_kind != "follow_chat" and vision.configured:
            provider = f"al1s_{service_id.hex}_{record.applied_version}_vision"
        else:
            profile = await asyncio.to_thread(self.repository.selected_chat_profile, service_id,
                "qq_group", group_id, version=record.applied_version)
            if not profile or not profile.supports_vision or (
                    not profile.secret_configured and not profile.is_default):
                raise ValueError("ai_sticker_vision_unavailable")
            provider = f"al1s_{service_id.hex}_{record.applied_version}_chat_{profile.id.hex}"
        command = StickerImportCommand(group_id=group_id, request_id=request_id,
                                       media_ref=media_ref, vision_provider_id=provider)
        return await self.hub.sticker_control(service_id, command, record.applied_version)

    async def close(self) -> None:
        if self._reconciler is not None:
            self._reconciler.cancel()
            await asyncio.gather(self._reconciler, return_exceptions=True)
            self._reconciler = None
        self._latest.clear()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.engine.close()
