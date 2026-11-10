import math

from .contracts import InputCapability, NodeCapability, WorkflowCapabilities


def _field(kind, bind=None, asset=None):
    return kind, bind, asset


_POLICIES = {
    "CheckpointLoaderSimple": ("nodes", ("MODEL", "CLIP", "VAE"), {
        "ckpt_name": _field("STRING", asset="checkpoints")}),
    "UNETLoader": ("nodes", ("MODEL",), {
        "unet_name": _field("STRING", asset="diffusion_models"), "weight_dtype": _field("STRING")}),
    "CLIPLoader": ("nodes", ("CLIP",), {
        "clip_name": _field("STRING", asset="text_encoders"), "type": _field("STRING")}),
    "VAELoader": ("nodes", ("VAE",), {"vae_name": _field("STRING", asset="vae")}),
    "LoraLoaderModelOnly": ("nodes", ("MODEL",), {
        "model": _field("MODEL"), "lora_name": _field("STRING", asset="loras"), "strength_model": _field("FLOAT")}),
    "SkipLayerGuidanceDiTSimple": ("comfy_extras.nodes_slg", ("MODEL",), {
        "model": _field("MODEL"), "double_layers": _field("STRING"), "single_layers": _field("STRING"),
        "start_percent": _field("FLOAT"), "end_percent": _field("FLOAT")}),
    "TemporalScoreRescaling": ("comfy_extras.nodes_eps", ("MODEL",), {
        "model": _field("MODEL"), "tsr_k": _field("FLOAT"), "tsr_sigma": _field("FLOAT")}),
    "EmptyFlux2LatentImage": ("comfy_extras.nodes_flux", ("LATENT",), {
        "width": _field("INT"), "height": _field("INT"), "batch_size": _field("INT")}),
    "TextEncodeQwenImage21": ("comfy_extras.nodes_qwen", ("CONDITIONING", "CONDITIONING", "LATENT"), {
        "clip": _field("CLIP"), "prompt": _field("STRING", bind="text"),
        "negative_prompt": _field("STRING", bind="text"), "resolution": _field("INT"),
        "vae": _field("VAE"), "images.image_1": _field("IMAGE")}),
    "LoraLoader": ("nodes", ("MODEL", "CLIP"), {
        "model": _field("MODEL"), "clip": _field("CLIP"),
        "lora_name": _field("STRING", asset="loras"),
        "strength_model": _field("FLOAT"), "strength_clip": _field("FLOAT")}),
    "CLIPSetLastLayer": ("nodes", ("CLIP",), {
        "clip": _field("CLIP"), "stop_at_clip_layer": _field("INT")}),
    "CLIPTextEncode": ("nodes", ("CONDITIONING",), {
        "clip": _field("CLIP"), "text": _field("STRING", bind="text")}),
    "EmptyLatentImage": ("nodes", ("LATENT",), {
        "width": _field("INT"), "height": _field("INT"), "batch_size": _field("INT")}),
    "KSampler": ("nodes", ("LATENT",), {
        "model": _field("MODEL"), "seed": _field("INT", bind="seed"), "steps": _field("INT"),
        "cfg": _field("FLOAT"), "sampler_name": _field("STRING"), "scheduler": _field("STRING"),
        "positive": _field("CONDITIONING"), "negative": _field("CONDITIONING"),
        "latent_image": _field("LATENT"), "denoise": _field("FLOAT")}),
    "VAEDecode": ("nodes", ("IMAGE",), {"samples": _field("LATENT"), "vae": _field("VAE")}),
    "VAEDecodeTiled": ("nodes", ("IMAGE",), {
        "samples": _field("LATENT"), "vae": _field("VAE"), "tile_size": _field("INT"),
        "overlap": _field("INT"), "temporal_size": _field("INT"), "temporal_overlap": _field("INT")}),
    "VAEEncodeTiled": ("nodes", ("LATENT",), {
        "pixels": _field("IMAGE"), "vae": _field("VAE"), "tile_size": _field("INT"),
        "overlap": _field("INT"), "temporal_size": _field("INT"), "temporal_overlap": _field("INT")}),
    "VAEEncode": ("nodes", ("LATENT",), {"pixels": _field("IMAGE"), "vae": _field("VAE")}),
    "ImageReceiver": ("custom_nodes.ComfyUI-Impact-Pack", ("IMAGE", "MASK"), {
        "image": _field("STRING"), "link_id": _field("INT"), "save_to_workflow": _field("BOOLEAN"),
        "image_data": _field("STRING", bind="image"), "trigger_always": _field("BOOLEAN")}),
    "ImageScale": ("nodes", ("IMAGE",), {
        "image": _field("IMAGE"), "upscale_method": _field("STRING"),
        "width": _field("INT"), "height": _field("INT"), "crop": _field("STRING")}),
    "UpscaleModelLoader": ("comfy_extras.nodes_upscale_model", ("UPSCALE_MODEL",), {
        "model_name": _field("STRING", asset="upscale_models")}),
    "ImageUpscaleWithModel": ("comfy_extras.nodes_upscale_model", ("IMAGE",), {
        "upscale_model": _field("UPSCALE_MODEL"), "image": _field("IMAGE")}),
    "SaveImage": ("nodes", ("IMAGE",), {
        "images": _field("IMAGE"), "filename_prefix": _field("STRING")}),
}


_LEGACY_V5_NODES = frozenset({
    "CheckpointLoaderSimple", "LoraLoader", "CLIPSetLastLayer", "CLIPTextEncode", "EmptyLatentImage",
    "KSampler", "VAEDecode", "VAEDecodeTiled", "VAEEncodeTiled", "VAEEncode", "ImageReceiver",
    "ImageScale", "UpscaleModelLoader", "ImageUpscaleWithModel", "SaveImage",
})
_V5 = "comfy-0.43-bb878b9-al1s-v5"
_V6 = "comfy-0.43-bb878b9-al1s-v6"
_V7 = "comfy-0.43-bb878b9-al1s-v7"
_V8 = "comfy-0.43-bb878b9-al1s-v8"
_LEGACY_V6_NODES = _LEGACY_V5_NODES | frozenset({
    "UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly", "SkipLayerGuidanceDiTSimple",
    "TemporalScoreRescaling", "EmptyFlux2LatentImage",
})


def compatible_inspection(result: dict, expected_adapter: str, nodes: list[str]) -> dict:
    actual = result.get("adapter")
    if actual == expected_adapter:
        return result
    if (actual in {_V6, _V7, _V8} and expected_adapter == _V5 and set(nodes) <= _LEGACY_V5_NODES
            or actual in {_V7, _V8} and expected_adapter == _V6 and set(nodes) <= _LEGACY_V6_NODES
            or actual == _V8 and expected_adapter == _V7 and set(nodes) <= set(_POLICIES)):
        return {**result, "adapter": expected_adapter}
    raise ValueError("drawing_dependency_changed")


def _input_capability(descriptor, policy):
    if not isinstance(descriptor, list) or not descriptor:
        raise ValueError("drawing_node_schema_invalid")
    expected, bind, asset = policy
    kind = descriptor[0]
    options = descriptor[1] if len(descriptor) > 1 else {}
    if not isinstance(options, dict):
        raise ValueError("drawing_node_schema_invalid")
    choices = None
    if isinstance(kind, list):
        choices, kind = kind, "STRING"
    elif kind == "COMBO":
        choices, kind = options.get("options"), "STRING"
    if kind != expected or (choices is not None and (
            not isinstance(choices, list) or any(not isinstance(value, str) for value in choices))):
        raise ValueError("drawing_node_schema_invalid")
    if descriptor[0] == "COMBO" and choices is None:
        raise ValueError("drawing_node_schema_invalid")
    bounds = {}
    for source, target in (("min", "minimum"), ("max", "maximum")):
        if source in options and expected in {"INT", "FLOAT"}:
            value = options[source]
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError("drawing_node_schema_invalid")
            bounds[target] = value
    return InputCapability(kind=expected, bind=bind, asset_kind=asset,
                           choices=tuple(choices) if choices is not None else None, **bounds)


def _qwen_single_image_inputs(inputs):
    required, optional = inputs.get("required"), inputs.get("optional")
    if (not isinstance(required, dict) or set(required) != {"clip", "prompt", "negative_prompt", "resolution", "images"}
            or not isinstance(optional, dict) or set(optional) != {"vae"}):
        raise ValueError("drawing_node_schema_invalid")
    dynamic = required["images"]
    if (not isinstance(dynamic, list) or len(dynamic) != 2 or dynamic[0] != "COMFY_AUTOGROW_V3"
            or not isinstance(dynamic[1], dict) or not isinstance(dynamic[1].get("template"), dict)):
        raise ValueError("drawing_node_schema_invalid")
    template = dynamic[1]["template"]
    image = template.get("input")
    if (template.get("names") != [f"image_{i}" for i in range(1, 17)] or template.get("min") != 0
            or not isinstance(image, dict) or set(image) != {"required"}
            or not isinstance(image["required"], dict) or set(image["required"]) != {"image"}):
        raise ValueError("drawing_node_schema_invalid")
    _input_capability(image["required"]["image"], _field("IMAGE"))
    return {**{key: value for key, value in required.items() if key != "images"},
            "vae": optional["vae"], "images.image_1": image["required"]["image"]}


def build_capabilities(object_info: dict, class_types: list[str], *, adapter_version: str) -> WorkflowCapabilities:
    if (not isinstance(object_info, dict) or not isinstance(class_types, list) or len(class_types) > 100
            or any(not isinstance(name, str) or not name or len(name) > 80 for name in class_types)):
        raise ValueError("drawing_node_schema_invalid")
    result = {}
    for name in dict.fromkeys(class_types):
        policy = _POLICIES.get(name)
        if policy is None:
            raise ValueError("drawing_node_unsupported")
        module, outputs, fields = policy
        node = object_info.get(name)
        if not isinstance(node, dict) or node.get("python_module") != module:
            raise ValueError("drawing_node_origin_invalid")
        inputs = node.get("input")
        required = inputs.get("required") if isinstance(inputs, dict) else None
        actual_outputs = node.get("output")
        if name == "TextEncodeQwenImage21":
            if adapter_version != _V7 or not isinstance(inputs, dict):
                raise ValueError("drawing_node_unsupported")
            required = _qwen_single_image_inputs(inputs)
        if (not isinstance(required, dict) or set(required) != set(fields)
                or not isinstance(actual_outputs, list) or tuple(actual_outputs) != outputs
                or node.get("output_node") is not (name == "SaveImage")):
            raise ValueError("drawing_node_schema_invalid")
        field_caps = {key: _input_capability(required[key], value) for key, value in fields.items()}
        if name in {"UNETLoader", "CLIPLoader"}:
            field = "weight_dtype" if name == "UNETLoader" else "type"
            allowed = (("default",) if name == "UNETLoader" else
                       ("moss_image", "qwen_image") if adapter_version == _V7 else ("moss_image",))
            if field_caps[field].choices is None or not set(allowed) <= set(field_caps[field].choices):
                raise ValueError("drawing_node_schema_invalid")
            field_caps[field] = field_caps[field].model_copy(update={"choices": allowed})
        if name == "CLIPLoader" and "device" in inputs.get("optional", {}):
            device = _input_capability(inputs["optional"]["device"], _field("STRING"))
            if device.choices is None or "default" not in device.choices:
                raise ValueError("drawing_node_schema_invalid")
            field_caps["device"] = device.model_copy(update={"choices": ("default",), "required": False})
        if name == "ImageReceiver":
            field_caps["image"] = field_caps["image"].model_copy(update={"choices": ("#DATA",)})
        result[name] = NodeCapability(inputs=field_caps,
            outputs=outputs, image_output=name == "SaveImage",
            owned_output_input="filename_prefix" if name == "SaveImage" else None)
    return WorkflowCapabilities(adapter_version=adapter_version, nodes=result)
