import asyncio
import inspect
import json
import logging

from al1s.bot_ai.drawing_inference import decode_wish
from al1s.bot_ai.model_provider import ModelProvider
from al1s.bot_ai.provider_clients import create_client


class DrawingPromptModel:
    def __init__(self, repository, *, client_factory=create_client, provider_factory=ModelProvider):
        self.repository = repository
        self.client_factory = client_factory
        self.provider_factory = provider_factory
        self._slots = asyncio.Semaphore(2)

    async def _generate(self, config, secret, system, messages, *, json_output=False):
        client = None
        async with self._slots:
            try:
                client = self.client_factory(config, secret)
                provider = self.provider_factory(config.adapter_kind, config.model_id, client)
                response = await asyncio.wait_for(provider.generate(system,
                    messages, (), json_output=json_output), timeout=15)
            except TimeoutError as error:
                raise ValueError("drawing_llm_timeout") from error
            except Exception as error:
                logging.getLogger(__name__).warning("drawing_llm_provider_failed:%s", type(error).__name__)
                raise ValueError("drawing_llm_provider_error") from error
            finally:
                if client is not None:
                    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
                    if closer:
                        result = closer()
                        if inspect.isawaitable(result):
                            try:
                                await asyncio.wait_for(result, timeout=2)
                            except (TimeoutError, OSError):
                                logging.getLogger(__name__).warning("drawing_llm_close_failed")
        return response

    async def expand_drawing(self, service_id, profile_id, revision, user_text, intent, *, workflow_name, aliases):
        if intent.mode != "wish" or not isinstance(workflow_name, str) or len(workflow_name) > 32:
            raise ValueError("drawing_wish_invalid")
        config, secret = await asyncio.to_thread(self.repository.prompt_credentials, service_id, profile_id, revision)
        system = ("根据用户许愿整理适合当前已选工作流的正面和负面提示词；保留用户的条件，"
                  "明确不要或排除的条件放入负面。工作流已固定，不能选择或增加LoRA、模型或节点。"
                  '仅返回JSON：{"positive":"...","negative":"..."}，不要调用工具。')
        request = {"text": user_text, "workflow": workflow_name, "available_lora_aliases": aliases}
        response = await self._generate(config, secret, system,
            [{"role": "user", "content": json.dumps(request, ensure_ascii=False)}], json_output=True)
        if not isinstance(response, dict) or response.get("tool_calls"):
            raise ValueError("drawing_wish_invalid")
        return decode_wish(response.get("text"), intent.negative)

    async def test_model(self, service_id, profile_id):
        catalog = await asyncio.to_thread(self.repository.list_prompt_models, service_id)
        profile = next((item for item in catalog["items"] if item["id"] == str(profile_id)), None)
        if profile is None:
            raise ValueError("drawing_prompt_not_found")
        config, secret = await asyncio.to_thread(self.repository.prompt_credentials,
                                                  service_id, profile_id, profile["revision"])
        try:
            response = await self._generate(config, secret, "连接测试", [{"role": "user", "content": "请回复 OK"}])
        except ValueError:
            return {"state": "failed", "message": "连接测试失败，请检查模型配置和网络。"}
        success = isinstance(response, dict) and bool(response.get("text"))
        return {"state": "success" if success else "failed",
                "message": "连接测试通过。" if success else "模型未返回文本。"}
