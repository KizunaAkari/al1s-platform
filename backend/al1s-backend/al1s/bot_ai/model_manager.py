import asyncio
import hashlib
import io
import json
import re
import wave
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import httpx

from al1s.bot_ai.executor import AiExecutor
from al1s.bot_ai.model_audio import _gemini_audio, _openai_audio, synthesize, transcribe
from al1s.bot_ai.model_provider import ModelProvider
from al1s.bot_ai.managed.comfy_client import ComfyWorkflowClient
from al1s.bot_ai.managed.smart_reply import SmartReplyPolicy
from al1s.bot_ai.provider_clients import create_client
from al1s.bot_drawing.comfy import DrawingComfyClient


@dataclass(repr=False)
class _Snapshot:
    service_id: UUID
    version: int
    executor: AiExecutor
    profiles: dict
    vision: object
    clients: list
    roles: dict
    comfy: object = None

    async def close(self):
        for client in reversed(self.clients):
            try:
                closer = getattr(client, 'aclose', None) or getattr(client, 'close', None)
                if closer:
                    await closer()
            except Exception:
                pass


class LocalModelManager:
    local_runtime = True

    def __init__(self, runtime, repository, history, media_root, *, stickers=None, client_factory=create_client,
                 resource_factory=DrawingComfyClient, gpu_queue=None):
        self.runtime, self.repository, self.history = runtime, repository, history
        self.media_root, self.stickers, self.client_factory = media_root, stickers, client_factory
        self.resource_factory = resource_factory
        self.gpu_queue = gpu_queue
        self._text_slots = asyncio.Semaphore(2)
        self._active, self._staged, self._policies, self._disabled = {}, {}, {}, {}
        self._pending_unpins = {}
        self._warmups = {}

    def cancel_waiting_vision(self, service_id):
        if self.gpu_queue is not None:
            self.gpu_queue.cancel_waiting_vision(service_id)

    def needs_rehydrate(self, service_id, version):
        snapshot = self._active.get(service_id)
        return not (snapshot and snapshot.version == version) and self._disabled.get(service_id) != version

    def note_disabled(self, service_id, version):
        self._disabled[service_id] = version

    async def protect_vision(self, service_id, version):
        snapshot = self._active.get(service_id)
        comfy = snapshot.comfy if snapshot and snapshot.version == version else None
        resource = getattr(comfy, 'resource', None)
        if resource is None:
            return False
        try:
            await resource.set_pin(service_id, version, True)
        except Exception:
            return False
        return True

    async def _warm_vision_once(self, service_id, snapshot):
        if self.gpu_queue is None or not self.gpu_queue.idle or self._active.get(service_id) is not snapshot:
            return False
        comfy = snapshot.comfy
        resource = getattr(comfy, "resource", None) if comfy else None
        if resource is None:
            return False
        state = await resource.state()
        ready = state.get("vision_state") == "warm" or (
            "vision_state" not in state and bool(state.get("vision_resources")))
        if (not state.get("vision_pins") or ready or state.get("drawing_resources")
                or state.get("active") or not self.gpu_queue.idle or self._active.get(service_id) is not snapshot):
            return False
        if await resource.switch("vision", None) != "ready" or not self.gpu_queue.idle:
            return False
        await comfy.generate("请回复 OK", max_tokens=64)
        return True

    def _start_warmup(self, service_id, snapshot):
        if self.gpu_queue is None or not snapshot or not snapshot.comfy:
            return
        async def run():
            while self._active.get(service_id) is snapshot:
                try:
                    await self._warm_vision_once(service_id, snapshot)
                except (httpx.HTTPError, OSError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(5)
        self._warmups[service_id] = asyncio.create_task(run())

    async def _stop_warmup(self, service_id):
        task = self._warmups.pop(service_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stage(self, record, secrets, *, chat_profiles=None, chat_secrets=None):
        clients, profiles, role_clients, comfy = [], {}, {}, None
        try:
            for profile in chat_profiles or []:
                if not profile.secret_configured and not profile.is_default:
                    continue
                secret = (chat_secrets or {}).get(str(profile.id))
                if not secret and profile.is_default:
                    secret = secrets.get('chat')
                if not secret:
                    raise ValueError('ai_provider_unconfigured')
                client = self.client_factory(profile, secret)
                clients.append(client)
                profiles[profile.id] = SimpleNamespace(
                    provider=ModelProvider(profile.adapter_kind, profile.model_id, client),
                    supports_vision=profile.supports_vision)
            if not any(profile.is_default and profile.id in profiles for profile in chat_profiles or []):
                raise ValueError('ai_provider_unconfigured')
            for role in ('vision', 'stt', 'tts'):
                provider = record.providers.get(role)
                needed = (record.vision_enabled if role == 'vision' else
                          record.stt_enabled if role == 'stt' else record.tts_mode != 'off')
                if not needed or (role == 'vision' and (provider is None or provider.adapter_kind == 'follow_chat')):
                    continue
                if provider is None or not provider.configured:
                    raise ValueError('ai_provider_unconfigured')
                if provider.adapter_kind == 'al1s_comfyui_vision':
                    resource = await self._vision_resource(record, provider)
                    comfy = self._comfy(record.service_id, provider, resource=resource,
                                        version=record.desired_version)
                    clients.append(comfy)
                    role_clients[role] = (provider, comfy)
                else:
                    client = self.client_factory(provider, secrets.get(role, ''))
                    clients.append(client)
                    role_clients[role] = (provider, client)
            stt = self._stt(role_clients.get('stt'))
            tts = self._tts(role_clients.get('tts'))
            vision = self._vision(role_clients.get('vision'))
            policy = self._policies.setdefault(record.service_id, SmartReplyPolicy())
            executor = AiExecutor(profiles, self.history, self.media_root, policy=policy,
                                  stickers=self.stickers, stt=stt, tts=tts, vision=vision,
                                  text_slots=self._text_slots)
            snapshot = _Snapshot(record.service_id, record.desired_version, executor,
                                 profiles, role_clients.get('vision'), clients, role_clients, comfy)
            self._staged.setdefault(record.service_id, []).append(snapshot)
            return snapshot
        except BaseException:
            await _Snapshot(record.service_id, record.desired_version, None, {}, None, clients, {}).close()
            raise

    @staticmethod
    def _comfy_address(service_id, provider):
        from al1s.bot_ai.managed.smart_contract import local_endpoint
        original_endpoint = provider.base_url or 'http://host.docker.internal:8188'
        endpoint = local_endpoint(original_endpoint)
        owner = 'al1s-vision-' + hashlib.sha256((service_id.hex + '|' + original_endpoint).encode()).hexdigest()[:24]
        return endpoint, owner

    async def _vision_resource(self, record, provider):
        getter = getattr(self.repository, 'drawing_resource_credentials', None)
        if getter is None:
            return None
        token, _ = await asyncio.to_thread(getter, record.service_id)
        if not token:
            raise ValueError('ai_vision_resources_unavailable')
        endpoint, owner = self._comfy_address(record.service_id, provider)
        resource = self.resource_factory(endpoint, owner, token)
        try:
            await asyncio.wait_for(resource.state(), timeout=3)
        except asyncio.CancelledError:
            await resource.close()
            raise
        except (httpx.RequestError, OSError, TimeoutError):
            return resource
        except Exception:
            await resource.close()
            raise ValueError('ai_vision_resources_unavailable') from None
        return resource

    def _comfy(self, service_id, provider, *, resource=None, version=None):
        endpoint, owner = self._comfy_address(service_id, provider)
        async def load():
            return await asyncio.to_thread(self.history.vision_jobs, service_id, endpoint)
        async def save(rows):
            task = asyncio.create_task(asyncio.to_thread(self.history.save_vision_jobs, service_id, endpoint, rows))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        if resource is not None:
            return ComfyWorkflowClient(endpoint, owner, load, save, resource=resource,
                                       service_id=service_id, config_version=version, gpu_queue=self.gpu_queue)
        return ComfyWorkflowClient(endpoint, owner, load, save)

    @staticmethod
    def _stt(value):
        if not value:
            return None
        provider, client = value
        async def run(body):
            return await transcribe(client, provider.model_id, body)
        return run

    def _tts(self, value):
        if not value:
            return None
        provider, client = value
        async def run(text, request_id):
            return str(await synthesize(client, provider.adapter_kind, provider.model_id,
                                        provider.voice, text, self.media_root, request_id))
        return run

    @staticmethod
    def _vision(value):
        if not value:
            return None
        provider, client = value
        async def run(image, hint):
            system = '准确描述图片中的内容和可见文字；无法辨认的部分明确说明。'
            if provider.adapter_kind == 'al1s_comfyui_vision':
                return await client.generate(system + '\n' + hint, image_data=image)
            model = ModelProvider(provider.adapter_kind, provider.model_id, client)
            result = await model.generate(system, [{'role':'user', 'content':[
                {'type':'text','text':hint or '请描述图片。'},
                {'type':'image_url','image_url':{'url':image}}]}])
            return result['text']
        return run

    async def binding(self, service_id):
        return {'snapshot': self._active.get(service_id)}

    async def activate(self, record, snapshot, binding):
        await self._stop_warmup(record.service_id)
        previous = self._active.get(record.service_id)
        if previous and self.stickers and any(job['service'] == str(record.service_id)
                                               for job in self.stickers._jobs):
            raise ValueError('ai_sticker_queue_pending')
        if previous and previous.comfy:
            endpoint = previous.comfy.base_url
            pending = await asyncio.to_thread(self.history.vision_jobs, record.service_id, endpoint)
            if pending:
                await previous.comfy.cleanup()
                pending = await asyncio.to_thread(self.history.vision_jobs, record.service_id, endpoint)
                if pending:
                    raise ValueError('ai_vision_cleanup_pending')
        new_resource = getattr(snapshot.comfy, 'resource', None) if snapshot and snapshot.comfy else None
        old_resource = getattr(previous.comfy, 'resource', None) if previous and previous.comfy else None
        if new_resource:
            try:
                await new_resource.set_pin(record.service_id, record.desired_version, True)
            except (httpx.RequestError, OSError, TimeoutError):
                pass
            self._pending_unpins.pop(record.service_id, None)
        elif old_resource:
            self._pending_unpins[record.service_id] = (old_resource, record.desired_version)
        if snapshot is None:
            await self.runtime.deactivate(record.service_id)
            self._active.pop(record.service_id, None)
            self.note_disabled(record.service_id, record.desired_version)
        else:
            await self.runtime.activate(record.service_id, snapshot.version, snapshot.executor)
            self._active[record.service_id] = snapshot
            self._disabled.pop(record.service_id, None)
            self._start_warmup(record.service_id, snapshot)

    async def restore(self, service_id, binding):
        await self._stop_warmup(service_id)
        self._pending_unpins.pop(service_id, None)
        previous = binding['snapshot']
        if previous:
            await self.runtime.activate(service_id, previous.version, previous.executor)
            self._active[service_id] = previous
            self._disabled.pop(service_id, None)
            self._start_warmup(service_id, previous)
        else:
            await self.runtime.deactivate(service_id)
            self._active.pop(service_id, None)

    async def cleanup(self, service_id, _version):
        pending_unpin = self._pending_unpins.get(service_id)
        if pending_unpin is not None:
            resource, version = pending_unpin
            await resource.set_pin(service_id, version, False)
            self._pending_unpins.pop(service_id, None)
        active = self._active.get(service_id)
        if active and active.comfy:
            active.comfy.start_cleanup()
        if active and self.stickers:
            self.stickers.start_imports(self.classify, self.authorize_import)
        snapshots = self._staged.get(service_id, [])
        self._staged[service_id] = [item for item in snapshots if item is active]
        for snapshot in snapshots:
            if snapshot is not active:
                await snapshot.close()

    async def discard(self, service_id, snapshot):
        if self._active.get(service_id) is snapshot:
            return
        staged = self._staged.get(service_id, [])
        if snapshot in staged:
            staged.remove(snapshot)
            await snapshot.close()

    async def authorize_import(self, service_id, group, provider_id):
        from al1s.bot_ai.sticker_library import StickerRetryLater

        snapshot = self._active.get(service_id)
        if snapshot is None:
            raise StickerRetryLater()
        try:
            settings = await asyncio.to_thread(self.repository.get_settings, service_id)
        except ValueError:
            return False
        except Exception:
            raise StickerRetryLater() from None
        prefix = f'al1s_{service_id.hex}_{snapshot.version}_'
        draining = settings.desired_version != settings.applied_version
        return bool((settings.enabled or draining) and settings.applied_version == snapshot.version
                    and group in settings.groups and provider_id.startswith(prefix))

    async def classify(self, service_id, provider_id, image, hint):
        snapshot = self._active.get(service_id)
        if snapshot is None:
            raise ValueError('ai_sticker_vision_unavailable')
        prefix = f'al1s_{service_id.hex}_{snapshot.version}_'
        if not provider_id.startswith(prefix):
            raise ValueError('ai_sticker_vision_unavailable')
        categories = [item['key'] for item in self.stickers._categories()]
        prompt = ('只返回JSON，包含category、description、tags、scenes、overlay_text。'
                  '描述图片内容与适合的聊天场景，不需要判断图片是否为表情。'
                  'category必须从以下列表选择：' + ','.join(categories) + '\n' + hint)
        suffix = provider_id[len(prefix):]
        if suffix == 'vision' and snapshot.vision:
            provider, client = snapshot.vision
            if provider.adapter_kind == 'al1s_comfyui_vision':
                text = await client.generate(prompt, image_data=image)
            else:
                model = ModelProvider(provider.adapter_kind, provider.model_id, client)
                text = (await model.generate(prompt, [{'role':'user','content':[
                    {'type':'image_url','image_url':{'url':image}}]}], (), json_output=True))['text']
        elif suffix.startswith('chat_'):
            identity = UUID(suffix[5:])
            profile = snapshot.profiles.get(identity)
            if not profile or not profile.supports_vision:
                raise ValueError('ai_sticker_vision_unavailable')
            text = (await profile.provider.generate(prompt, [{'role':'user','content':[
                {'type':'image_url','image_url':{'url':image}}]}], (), json_output=True))['text']
        else:
            raise ValueError('ai_sticker_vision_unavailable')
        match = re.search(r'\{.*\}', text, re.S)
        data = json.loads(match.group() if match else text)
        if not isinstance(data, dict):
            raise ValueError('ai_sticker_classification_invalid')
        return data

    async def test_chat_profile(self, service_id, version, profile_id):
        snapshot = self._active.get(service_id)
        if not snapshot or snapshot.version != version or profile_id not in snapshot.profiles:
            raise ValueError('ai_sync_pending')
        try:
            response = await snapshot.profiles[profile_id].provider.generate('连接测试',
                        [{'role':'user','content':'请回复 OK'}])
            return {'state':'success' if response['text'] else 'failed',
                    'message':'连接测试通过' if response['text'] else '模型未返回文本'}
        except Exception:
            return {'state':'failed', 'message':'连接测试失败，请检查模型配置和网络。'}

    async def test_provider(self, service_id, version, role, provider):
        snapshot = self._active.get(service_id)
        if not snapshot or snapshot.version != version or role not in snapshot.roles:
            raise ValueError('ai_sync_pending')
        spec, client = snapshot.roles[role]
        try:
            if role == 'vision':
                if spec.adapter_kind == 'al1s_comfyui_vision':
                    result = await client.generate('请回复 OK', image_data=None)
                else:
                    result = (await ModelProvider(spec.adapter_kind, spec.model_id, client).generate(
                        '连接测试', [{'role':'user','content':'请回复 OK'}]))['text']
                return {'state':'success' if result else 'failed',
                        'message':'连接测试通过' if result else '模型未返回文本'}
            if role == 'stt':
                buffer = io.BytesIO()
                with wave.open(buffer, 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(b'\0\0' * 8000)
                response = await client.audio.transcriptions.create(model=spec.model_id,
                    file=('test.wav', buffer.getvalue(), 'audio/wav'))
                if not (hasattr(response, 'text') or isinstance(response, dict) and 'text' in response):
                    raise ValueError('ai_transcription_unavailable')
                return {'state':'success', 'message':'连接测试通过；识别效果请用实际语音验证。'}
            if role == 'tts':
                body = (await _gemini_audio(client, spec.model_id, spec.voice, '连接测试')
                        if spec.adapter_kind == 'gemini_tts' else
                        await _openai_audio(client, spec.model_id, spec.voice, '连接测试'))
                return {'state':'success' if body else 'failed',
                        'message':'连接测试通过' if body else '服务未返回语音'}
        except Exception:
            return {'state':'failed', 'message':'连接测试失败，请检查模型配置和网络。'}
        raise ValueError('ai_provider_test_unavailable')

    async def close(self):
        for service_id in tuple(self._warmups):
            await self._stop_warmup(service_id)
        if self.stickers:
            await self.stickers.close()
        for snapshots in self._staged.values():
            for snapshot in snapshots:
                await snapshot.close()
        self._active.clear()
        self._staged.clear()
        self._disabled.clear()
        self._pending_unpins.clear()
