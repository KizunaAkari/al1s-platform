import hashlib
import inspect
import os
import stat
import threading
from pathlib import Path, PureWindowsPath

from .safety import _not_link

ASSET_KINDS = {"checkpoints", "loras", "vae", "text_encoders", "diffusion_models",
               "upscale_models", "controlnet", "clip_vision"}
_CORE_NODES = {"CheckpointLoaderSimple", "UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly",
               "LoraLoader", "CLIPSetLastLayer", "CLIPTextEncode",
               "EmptyLatentImage", "KSampler", "VAEDecode", "VAEDecodeTiled", "VAEEncode",
               "VAEEncodeTiled", "ImageScale", "SaveImage"}
_EXTRA_NODES = {"UpscaleModelLoader": "comfy_extras/nodes_upscale_model.py",
                "ImageUpscaleWithModel": "comfy_extras/nodes_upscale_model.py",
                "EmptyFlux2LatentImage": "comfy_extras/nodes_flux.py",
                "TextEncodeQwenImage21": "comfy_extras/nodes_qwen.py",
                "SkipLayerGuidanceDiTSimple": "comfy_extras/nodes_slg.py",
                "TemporalScoreRescaling": "comfy_extras/nodes_eps.py"}
_CUSTOM_NODES = {
    "ImageReceiver": ("custom_nodes/ComfyUI-Impact-Pack/modules/impact/impact_pack.py",
        "53552ac51c53275738e6e06b1fce1bc54434a048feec75a54676593ca5d59623"),
    "WD14Tagger|pysssss": ("custom_nodes/comfyui-wd14-tagger/wd14tagger.py",
        "de0d5502f6ae11a32fbcabd447fb9c9d2e3b0d3c5d18a96715648614075d92f7"),
    "AILab_QwenVL_Advanced": ("custom_nodes/ComfyUI-QwenVL/py/AILab_QwenVL.py",
        "b7b00977c6b8780f58b82bd1f4dc9675cf2a0e7516ac743023ff44cce1b1578f"),
    "ShowText|pysssss": ("custom_nodes/ComfyUI-Custom-Scripts/py/show_text.py",
        "6d1a3164c7a11458e8d1a8eb99cdbe26717b02e5425eb7260d599977f774c951"),
}


def _name(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("resource_asset_invalid")
    windows = PureWindowsPath(value)
    parts = value.replace("\\", "/").split("/")
    if (windows.drive or value.startswith(("/", "\\")) or any(part in {"", ".", ".."} for part in parts)
            or any(ord(char) < 32 or char in ':*?"<>|' for char in value)):
        raise ValueError("resource_asset_invalid")
    return value


def _asset_path(folder_paths, kind, name):
    raw = folder_paths.get_full_path(kind, name)
    if not isinstance(raw, str):
        raise ValueError("resource_asset_missing")
    path = Path(raw)
    resolved = path.resolve(strict=True)
    roots = []
    for value in folder_paths.get_folder_paths(kind):
        try:
            roots.append(Path(value).resolve(strict=True))
        except FileNotFoundError:
            continue
    for root in roots:
        try:
            relative = path.absolute().relative_to(root)
        except ValueError:
            continue
        current = root
        for part in relative.parts:
            current = current / part
            _not_link(current)
        if resolved == current.resolve(strict=True):
            return current
    raise ValueError("resource_asset_invalid")


def inspect_assets(folder_paths, assets, cache=None):
    if not isinstance(assets, list) or len(assets) > 100:
        raise ValueError("resource_asset_invalid")
    result, seen = [], set()
    for item in assets:
        if not isinstance(item, dict) or set(item) != {"kind", "name"} or item["kind"] not in ASSET_KINDS:
            raise ValueError("resource_asset_invalid")
        kind, name = item["kind"], _name(item["name"])
        if (kind, name) in seen:
            continue
        seen.add((kind, name))
        try:
            path = _asset_path(folder_paths, kind, name)
            before = _not_link(path)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("resource_asset_invalid")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            try:
                opened = os.fstat(fd)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("resource_asset_invalid")
                signature = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                checksum = cache.get(str(path), signature) if cache is not None else None
                if checksum is None:
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    checksum = digest.hexdigest()
                after = os.fstat(fd)
            finally:
                os.close(fd)
            final = _not_link(path)
            if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != signature):
                raise ValueError("resource_asset_changed")
            if cache is not None:
                cache.put(str(path), signature, checksum)
            result.append({"kind": kind, "name": name, "size": after.st_size, "sha256": checksum})
        except OSError as error:
            raise ValueError("resource_asset_missing") from error
    return result


class AssetHashCache:
    def __init__(self):
        self._lock, self._values = threading.Lock(), {}

    def get(self, path, signature):
        with self._lock:
            current = self._values.get(path)
            return current[1] if current and current[0] == signature else None

    def put(self, path, signature, checksum):
        with self._lock:
            if len(self._values) >= 256 and path not in self._values:
                self._values.pop(next(iter(self._values)))
            self._values[path] = (signature, checksum)


def inspect_dependencies(root, native_nodes, folder_paths, nodes, assets, *, cache=None):
    from .safety import CORE_HASHES, check_files

    if (not isinstance(nodes, list) or len(nodes) > 100
            or any(not isinstance(value, str) or len(value) > 80 for value in nodes)):
        raise ValueError("resource_node_invalid")
    root = Path(root).resolve(strict=True)
    paths, node_checksums = {}, {}
    for name in dict.fromkeys(nodes):
        if name in _CORE_NODES:
            relative, checksum = "nodes.py", CORE_HASHES["nodes.py"]
        elif name in _EXTRA_NODES:
            relative = _EXTRA_NODES[name]
            checksum = CORE_HASHES[relative]
        elif name in _CUSTOM_NODES:
            relative, checksum = _CUSTOM_NODES[name]
        else:
            raise ValueError("resource_node_unsupported")
        node = native_nodes.get(name)
        source = inspect.getsourcefile(node) if node is not None else None
        if not source or Path(source).resolve() != (root / relative).resolve():
            raise ValueError("resource_node_origin_invalid")
        paths[relative] = checksum
        node_checksums[name] = checksum
        if name == "AILab_QwenVL_Advanced":
            paths.update({
                "custom_nodes/ComfyUI-QwenVL/AILab_Utils.py": "ce82e87245b10172f4f8e9fb7040f44f0894d7be365658fa70297dfa8f9b330f",
                "custom_nodes/ComfyUI-QwenVL/hf_models.json": "ab0c675ed9e3dbcef537f05a764dfd6ce15186ff2d5134dcf99fafe8cbd4e2f4",
                "custom_nodes/ComfyUI-QwenVL/system_prompts.json": "86922aaf0f0d3d7a19e622eb8aa35affc4c1b087a89d655bb285c03f9a67f430",
            })
    check_files(root, paths)
    if "WD14Tagger|pysssss" in nodes:
        module = inspect.getmodule(native_nodes["WD14Tagger|pysssss"])
        if module is None or not isinstance(getattr(module, "models_dir", None), str):
            raise ValueError("resource_tagger_model_invalid")
        check_files(Path(module.models_dir), {
            "wd-vit-tagger-v3.onnx": "35f23693620b668f4d53fd3c62bf65e40af739bc52c7eb0fbc49258b58d065b6",
            "wd-vit-tagger-v3.csv": "298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217",
        })
    return {"nodes": [{"name": name, "sha256": node_checksums[name]} for name in dict.fromkeys(nodes)],
            "assets": inspect_assets(folder_paths, assets, cache=cache)}
