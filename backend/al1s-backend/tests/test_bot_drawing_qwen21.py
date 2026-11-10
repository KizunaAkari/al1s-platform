import base64
from copy import deepcopy
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image

from al1s.bot_drawing.capabilities import build_capabilities, compatible_inspection
from al1s.bot_drawing.contracts import InputBindings, OutputBinding, PortBinding, PresetDraft
from al1s.bot_drawing.workflow import instantiate_workflow, managed_graph, validate_workflow

ADAPTER = "comfy-0.43-bb878b9-al1s-v7"


def node(module, outputs, required, optional=None):
    return {"python_module": module, "output": list(outputs), "output_node": False,
            "input": {"required": required, "optional": optional or {}}}


@pytest.fixture
def qwen_info():
    autogrow = ["COMFY_AUTOGROW_V3", {"template": {
        "input": {"required": {"image": ["IMAGE", {}]}},
        "names": [f"image_{i}" for i in range(1, 17)], "min": 0}}]
    result = {
        "UNETLoader": node("nodes", ("MODEL",), {"unet_name": [["edit.safetensors"]], "weight_dtype": [["default"]]}),
        "CLIPLoader": node("nodes", ("CLIP",), {"clip_name": [["encoder.safetensors"]],
                           "type": [["moss_image", "qwen_image", "flux2"]]}, {"device": [["default", "cpu"]]}),
        "VAELoader": node("nodes", ("VAE",), {"vae_name": [["vae.safetensors"]]}),
        "ImageReceiver": node("custom_nodes.ComfyUI-Impact-Pack", ("IMAGE", "MASK"), {
            "image": ["STRING"], "link_id": ["INT"], "save_to_workflow": ["BOOLEAN"],
            "image_data": ["STRING"], "trigger_always": ["BOOLEAN"]}),
        "TextEncodeQwenImage21": node("comfy_extras.nodes_qwen", ("CONDITIONING", "CONDITIONING", "LATENT"), {
            "clip": ["CLIP"], "prompt": ["STRING"], "negative_prompt": ["STRING"],
            "resolution": ["INT", {"min": 0, "max": 4096, "step": 32}], "images": autogrow},
            {"vae": ["VAE"]}),
        "KSampler": node("nodes", ("LATENT",), {"model": ["MODEL"], "seed": ["INT"], "steps": ["INT"],
            "cfg": ["FLOAT"], "sampler_name": [["euler"]], "scheduler": [["simple"]],
            "positive": ["CONDITIONING"], "negative": ["CONDITIONING"], "latent_image": ["LATENT"],
            "denoise": ["FLOAT"]}),
        "VAEDecode": node("nodes", ("IMAGE",), {"samples": ["LATENT"], "vae": ["VAE"]}),
        "SaveImage": node("nodes", ("IMAGE",), {"images": ["IMAGE"], "filename_prefix": ["STRING"]}),
    }
    result["SaveImage"]["output_node"] = True
    return result


@pytest.fixture
def qwen_graph():
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "edit.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "encoder.safetensors", "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "vae.safetensors"}},
        "4": {"class_type": "ImageReceiver", "inputs": {"image": "#DATA", "link_id": 0,
            "save_to_workflow": True, "image_data": "", "trigger_always": False}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0],
            "prompt": "", "negative_prompt": "", "resolution": 0, "images.image_1": ["4", 0]}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["5", 0],
            "negative": ["5", 1], "latent_image": ["5", 2], "seed": 0, "steps": 25, "cfg": 2,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "ComfyUI"}},
    }


def draft(graph):
    return PresetDraft(name="Qwen 编辑", kind="image_edit", graph=graph, bindings=InputBindings(
        positive=PortBinding(node_id="5", input_name="prompt"),
        negative=PortBinding(node_id="5", input_name="negative_prompt"),
        image=PortBinding(node_id="4", input_name="image_data"),
        seed=PortBinding(node_id="6", input_name="seed"), output=OutputBinding(node_id="8")))


def test_qwen_image_edit_passes_single_reference_and_both_conditions_to_sampler(qwen_info, qwen_graph):
    capabilities = build_capabilities(qwen_info, list(qwen_info), adapter_version=ADAPTER)
    validated = validate_workflow(draft(qwen_graph), capabilities)
    assert {(item.kind, item.name) for item in validated.manifest} == {
        ("diffusion_models", "edit.safetensors"), ("text_encoders", "encoder.safetensors"),
        ("vae", "vae.safetensors")}
    image = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(image, format="PNG")
    reference = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    execution = instantiate_workflow(validated, "红色裙子", "模糊", image_data=reference,
                                     seed=42, prompt_id=prompt_id, mode="green")
    managed = managed_graph(execution.graph, prompt_id).graph
    assert managed["4"]["inputs"]["image_data"] == reference
    assert managed["5"]["inputs"]["images.image_1"] == ["4", 0]
    assert managed["5"]["inputs"]["prompt"] == "红色裙子\nsfw"
    assert managed["5"]["inputs"]["negative_prompt"] == "模糊\nnsfw"
    assert managed["6"]["inputs"]["positive"] == ["5", 0]
    assert managed["6"]["inputs"]["negative"] == ["5", 1]
    assert managed["6"]["inputs"]["latent_image"] == ["5", 2]
    assert managed["2"]["class_type"] == "AL1SCLIPLoader"


@pytest.mark.parametrize("mutation", ["extra_reference", "missing_vae", "wrong_encoder", "cfg_one"])
def test_qwen_edit_rejects_unverified_reference_or_ineffective_negative(qwen_info, qwen_graph, mutation):
    if mutation == "extra_reference":
        qwen_graph["5"]["inputs"]["images.image_2"] = ["4", 0]
    elif mutation == "missing_vae":
        del qwen_graph["5"]["inputs"]["vae"]
    elif mutation == "wrong_encoder":
        qwen_graph["2"]["inputs"]["type"] = "flux2"
    else:
        qwen_graph["6"]["inputs"]["cfg"] = 1
    with pytest.raises(ValueError):
        capabilities = build_capabilities(qwen_info, list(qwen_info), adapter_version=ADAPTER)
        validate_workflow(draft(qwen_graph), capabilities)


def test_qwen_rejects_untrusted_dynamic_input_schema(qwen_info, qwen_graph):
    modified = deepcopy(qwen_info)
    modified["TextEncodeQwenImage21"]["input"]["required"]["images"][1]["template"]["input"] = {
        "required": {"image": ["STRING", {}]}}
    with pytest.raises(ValueError, match="drawing_node_schema_invalid"):
        build_capabilities(modified, list(modified), adapter_version=ADAPTER)


def test_v7_keeps_only_original_v5_and_v6_node_contracts_compatible():
    v5, v6, v7 = (f"comfy-0.43-bb878b9-al1s-v{i}" for i in (5, 6, 7))
    result = {"adapter": v7, "nodes": [], "assets": []}
    assert compatible_inspection(result, v5, ["CheckpointLoaderSimple"])["adapter"] == v5
    assert compatible_inspection(result, v6, ["UNETLoader", "TemporalScoreRescaling"])["adapter"] == v6
    with pytest.raises(ValueError, match="drawing_dependency_changed"):
        compatible_inspection(result, v6, ["TextEncodeQwenImage21"])
