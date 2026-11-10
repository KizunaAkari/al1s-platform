import base64
import hashlib
import inspect
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path

from PIL import Image

_VISION_FILES = {
    "model-00001-of-00002.safetensors": "30a01a0556622645a3cce87b655bbbbbc1f170c196099f1b666c93202c3339a9",
    "model-00002-of-00002.safetensors": "046296a2a387efb43b0c997d5833c789604d168834f6e0d3064bf7bb13d002a6",
    "chat_template.json": "6f8a6a55027e3da5160105556cda5dd69f6423f1c32645f6730d32de7773d0c4",
    "config.json": "edac7703329133edfc53e46ac0081835144c99d7eebf28b71c732694d435224d",
    "generation_config.json": "8469742d1fce0de951c8909b26a2c0c0d8490837ce476efb114da9e0cefc4d44",
    "merges.txt": "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3",
    "model.safetensors.index.json": "58a7841d7bff2548dd91577d216274a83cf1b500bc6a534b809d6c1b1707cf2b",
    "preprocessor_config.json": "27225450ac9c6529872ee1924fcb0962ff5634834f817040f444118116f4e516",
    "tokenizer_config.json": "c2da771801886ad9ae98181793ffd3dfb7f1af30f6f7c6a4e15d7dbba52e2399",
    "tokenizer.json": "a5d85b6dcc535e6b93115a9ef287e6132fdbf30270da6218194ba742261173c7",
    "video_preprocessor_config.json": "7768af27c1fafa9cc9011c1dc20067e03f8915e03b63504550e11d5066986d13",
    "vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
}
_vision_fingerprint = None


def _check_vision_assets(folder_paths, native):
    global _vision_fingerprint
    from .safety import _not_link, check_files
    root = Path(folder_paths.models_dir) / "LLM" / "Qwen-VL" / "Qwen3-VL-4B-Instruct"
    module = inspect.getmodule(native)
    resolved = module.resolve_hf_model_path("Qwen/Qwen3-VL-4B-Instruct") if module else None
    if not resolved or Path(resolved).resolve() != root.resolve() or any(root.glob("*.py")):
        raise ValueError("resource_vision_model_invalid")
    current = []
    for name in _VISION_FILES:
        info = _not_link(root / name)
        current.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    fingerprint = (str(root.resolve()), tuple(current))
    if fingerprint != _vision_fingerprint:
        check_files(root, _VISION_FILES)
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        if config.get("model_type") != "qwen3_vl" or config.get("auto_map"):
            raise ValueError("resource_vision_model_invalid")
        _vision_fingerprint = fingerprint


def _cache_key(kind, *values):
    encoded = json.dumps([kind, *values], sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _schema(native):
    schema = deepcopy(native.INPUT_TYPES())
    schema.setdefault("required", {})["al1s_prompt_id"] = ("STRING", {"default": ""})
    return schema


def _inline_image(value):
    if not isinstance(value, str) or len(value) > 12 * 1024 * 1024 or not value.startswith("data:image/"):
        raise ValueError("resource_image_invalid")
    try:
        header, encoded = value.split(",", 1)
        if header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
            raise ValueError("resource_image_invalid")
        body = base64.b64decode(encoded, validate=True)
        if not body or len(body) > 8 * 1024 * 1024:
            raise ValueError("resource_image_invalid")
        with Image.open(BytesIO(body), formats=["PNG", "JPEG", "WEBP"]) as image:
            width, height = image.size
            if (image.format.lower() != header[11:-7] or getattr(image, "n_frames", 1) != 1
                    or min(width, height) < 1 or max(width, height) > 4096 or width * height > 8_000_000):
                raise ValueError("resource_image_invalid")
            image.load()
    except Exception as error:
        raise ValueError("resource_image_invalid") from error


def build_nodes(registry, native_nodes, *, current_job, folder_paths=None, verify_asset=None):
    result = {}

    class InvalidRequest:
        RETURN_TYPES = ()
        FUNCTION = "fail"
        OUTPUT_NODE = True
        CATEGORY = "AL1S/internal"
        @classmethod
        def INPUT_TYPES(cls): return {"required": {}}
        def fail(self): raise ValueError("resource_job_conflict")

    result["AL1SInvalidRequest"] = InvalidRequest

    def check(prompt):
        current = current_job()
        if not isinstance(current, tuple) or len(current) != 2 or current[0] != prompt:
            raise ValueError("resource_job_conflict")
        return registry.require_started(prompt, current[1])

    def cached(prompt, key, factory, parents=()):
        value = registry.cached(key)
        if value is not None:
            return registry.remember(prompt, key, value, parents=parents)
        value = factory()
        if not isinstance(value, tuple):
            raise ValueError("resource_loader_invalid")
        return registry.remember(prompt, key, value, parents=parents)

    def asset_hash(prompt, kind, name):
        expected = registry.asset_hash(prompt, kind, name)
        if verify_asset is not None and verify_asset(kind, name) != expected:
            raise ValueError("resource_asset_changed")
        return expected

    class Common:
        FUNCTION = "process"
        CATEGORY = "AL1S/internal"

        @classmethod
        def IS_CHANGED(cls, **_kwargs):
            return float("nan")

    from .moss_nodes import build_moss_nodes
    result.update(build_moss_nodes(registry, native_nodes, common=Common, schema=_schema,
                                  check=check, cached=cached, asset_hash=asset_hash, cache_key=_cache_key))

    native = native_nodes.get("CheckpointLoaderSimple")
    if native is not None:
        checkpoint_native = native
        class Checkpoint(Common):
            RETURN_TYPES = checkpoint_native.RETURN_TYPES
            @classmethod
            def INPUT_TYPES(cls): return _schema(checkpoint_native)
            def process(self, ckpt_name, al1s_prompt_id):
                check(al1s_prompt_id)
                digest = asset_hash(al1s_prompt_id, "checkpoints", ckpt_name)
                key = _cache_key("checkpoint", digest)
                return cached(al1s_prompt_id, key, lambda: checkpoint_native().load_checkpoint(ckpt_name))
        result["AL1SCheckpointLoader"] = Checkpoint

    native = native_nodes.get("LoraLoader")
    if native is not None:
        lora_native = native
        class Lora(Common):
            RETURN_TYPES = lora_native.RETURN_TYPES
            @classmethod
            def INPUT_TYPES(cls): return _schema(lora_native)
            def process(self, model, clip, lora_name, strength_model, strength_clip, al1s_prompt_id):
                check(al1s_prompt_id)
                digest = asset_hash(al1s_prompt_id, "loras", lora_name)
                parents = tuple(dict.fromkeys((registry.key_for_value(model), registry.key_for_value(clip))))
                key = _cache_key("lora", digest, parents, strength_model, strength_clip)
                return cached(al1s_prompt_id, key, lambda: lora_native().load_lora(
                    model, clip, lora_name, strength_model, strength_clip), parents)
        result["AL1SLoraLoader"] = Lora

    native = native_nodes.get("CLIPSetLastLayer")
    if native is not None:
        clip_native = native
        class ClipLayer(Common):
            RETURN_TYPES = clip_native.RETURN_TYPES
            @classmethod
            def INPUT_TYPES(cls): return _schema(clip_native)
            def process(self, clip, stop_at_clip_layer, al1s_prompt_id):
                check(al1s_prompt_id)
                parent = registry.key_for_value(clip)
                key = _cache_key("clip_layer", parent, stop_at_clip_layer)
                return cached(al1s_prompt_id, key,
                    lambda: clip_native().set_last_layer(clip, stop_at_clip_layer), (parent,))
        result["AL1SCLIPSetLastLayer"] = ClipLayer

    native = native_nodes.get("UpscaleModelLoader")
    if native is not None and folder_paths is not None:
        upscale_native = native
        class Upscale(Common):
            RETURN_TYPES = ("UPSCALE_MODEL",)
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"model_name": (folder_paths.get_filename_list("upscale_models"),),
                                     "al1s_prompt_id": ("STRING", {"default": ""})}}
            def process(self, model_name, al1s_prompt_id):
                check(al1s_prompt_id)
                digest = asset_hash(al1s_prompt_id, "upscale_models", model_name)
                key = _cache_key("upscale", digest)
                return cached(al1s_prompt_id, key, lambda: tuple(upscale_native.execute(model_name).result))
        result["AL1SUpscaleModelLoader"] = Upscale

    native = native_nodes.get("ImageReceiver")
    if native is not None:
        image_native = native
        class Receiver(Common):
            RETURN_TYPES = image_native.RETURN_TYPES
            @classmethod
            def INPUT_TYPES(cls):
                schema = _schema(image_native)
                schema["required"]["image"] = (["#DATA"],)
                return schema
            def process(self, image, link_id, save_to_workflow, image_data, trigger_always, al1s_prompt_id):
                check(al1s_prompt_id)
                if image != "#DATA" or link_id != 0 or save_to_workflow is not True or trigger_always is not False:
                    raise ValueError("resource_image_invalid")
                _inline_image(image_data)
                return image_native().doit(image, link_id, save_to_workflow, image_data, trigger_always)
        result["AL1SImageReceiver"] = Receiver

    native = native_nodes.get("SaveImage")
    if native is not None:
        save_native = native
        class Save(Common):
            RETURN_TYPES = save_native.RETURN_TYPES
            OUTPUT_NODE = True
            @classmethod
            def INPUT_TYPES(cls):
                schema = _schema(save_native)
                schema.pop("hidden", None)
                return schema
            def process(self, images, filename_prefix, al1s_prompt_id):
                check(al1s_prompt_id)
                if filename_prefix != f"al1s-drawing/{al1s_prompt_id}/output" or len(images) != 1:
                    raise ValueError("resource_output_invalid")
                return save_native().save_images(images, filename_prefix, prompt=None, extra_pnginfo=None)
        result["AL1SSaveImage"] = Save

    native = native_nodes.get("WD14Tagger|pysssss")
    if native is not None:
        tagger_native = native
        class Tagger(Common):
            RETURN_TYPES = tagger_native.RETURN_TYPES
            OUTPUT_NODE = True
            @classmethod
            def INPUT_TYPES(cls): return _schema(tagger_native)
            def process(self, image, model, threshold, character_threshold, al1s_prompt_id,
                        exclude_tags="", replace_underscore=True, trailing_comma=False):
                check(al1s_prompt_id)
                if model != "wd-vit-tagger-v3":
                    raise ValueError("resource_tagger_model_invalid")
                module = inspect.getmodule(tagger_native)
                model_dir = Path(module.models_dir)
                expected = {"wd-vit-tagger-v3.onnx": "35f23693620b668f4d53fd3c62bf65e40af739bc52c7eb0fbc49258b58d065b6",
                            "wd-vit-tagger-v3.csv": "298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217"}
                from .safety import check_files
                check_files(model_dir, expected)
                return tagger_native().tag(image, model, threshold, character_threshold,
                                           exclude_tags, replace_underscore, trailing_comma)
        result["AL1SWD14Tagger"] = Tagger

    native = native_nodes.get("AILab_QwenVL_Advanced")
    if native is not None and folder_paths is not None:
        vision_native = native
        class SafeVision(vision_native):
            al1s_ready = False
            def clear(self):
                self.model, self.processor, self.tokenizer, self.current_signature = None, None, None, None
                self.al1s_ready = False
        class Vision(Common):
            RETURN_TYPES = vision_native.RETURN_TYPES
            @classmethod
            def INPUT_TYPES(cls): return _schema(vision_native)
            def process(self, al1s_prompt_id, **inputs):
                job = check(al1s_prompt_id)
                expected = {"model_name": "Qwen3-VL-4B-Instruct", "quantization": "4-bit (VRAM-friendly)",
                            "attention_mode": "sdpa", "use_torch_compile": False,
                            "device": "auto", "keep_model_loaded": True}
                if job["operation"] != "vision" or any(inputs.get(key) != value for key, value in expected.items()):
                    raise ValueError("resource_vision_model_invalid")
                _check_vision_assets(folder_paths, vision_native)
                key = _cache_key("qwen-vision", expected, _VISION_FILES)
                value = cached(al1s_prompt_id, key, lambda: (SafeVision(),))
                registry.pin_resource(job["service_id"], key)
                result = value[0].process(**inputs)
                value[0].al1s_ready = True
                return result
        result["AL1SQwenVLAdvanced"] = Vision

    return result
