from copy import deepcopy
import base64
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image

from al1s.bot_drawing.capabilities import build_capabilities
from al1s.bot_drawing.contracts import InputBindings, OutputBinding, PortBinding, PresetDraft
from al1s.bot_drawing.workflow import instantiate_workflow, managed_graph, validate_workflow


def node(module, outputs, **inputs):
    return {"python_module": module, "output": list(outputs), "output_node": False,
            "input": {"required": inputs}}


@pytest.fixture
def moss_info():
    info = {
        "UNETLoader": node("nodes", ("MODEL",), unet_name=[["moss.safetensors"]],
                           weight_dtype=[["default", "fp8_e4m3fn"]]),
        "CLIPLoader": node("nodes", ("CLIP",), clip_name=[["qwen.safetensors"]],
                           type=[["moss_image", "flux2"]]),
        "VAELoader": node("nodes", ("VAE",), vae_name=[["flux2.safetensors"]]),
        "LoraLoaderModelOnly": node("nodes", ("MODEL",), model=["MODEL"],
                                    lora_name=[["style.safetensors"]], strength_model=["FLOAT"]),
        "SkipLayerGuidanceDiTSimple": node("comfy_extras.nodes_slg", ("MODEL",), model=["MODEL"],
             double_layers=["STRING"], single_layers=["STRING"], start_percent=["FLOAT"], end_percent=["FLOAT"]),
        "TemporalScoreRescaling": node("comfy_extras.nodes_eps", ("MODEL",), model=["MODEL"],
                                       tsr_k=["FLOAT"], tsr_sigma=["FLOAT"]),
        "EmptyFlux2LatentImage": node("comfy_extras.nodes_flux", ("LATENT",), width=["INT"],
                                      height=["INT"], batch_size=["INT"]),
        "CLIPTextEncode": node("nodes", ("CONDITIONING",), clip=["CLIP"], text=["STRING"]),
        "KSampler": node("nodes", ("LATENT",), model=["MODEL"], seed=["INT"], steps=["INT"], cfg=["FLOAT"],
                         sampler_name=[["er_sde"]], scheduler=[["simple"]], positive=["CONDITIONING"],
                         negative=["CONDITIONING"], latent_image=["LATENT"], denoise=["FLOAT"]),
        "VAEDecode": node("nodes", ("IMAGE",), samples=["LATENT"], vae=["VAE"]),
        "SaveImage": node("nodes", ("IMAGE",), images=["IMAGE"], filename_prefix=["STRING"]),
    }
    info["SaveImage"]["output_node"] = True
    info["CLIPLoader"]["input"]["optional"] = {"device": [["default", "cpu"]]}
    return info


@pytest.fixture
def moss_graph():
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "moss.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen.safetensors", "type": "moss_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2.safetensors"}},
        "4": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "style.safetensors", "strength_model": 0.7}},
        "5": {"class_type": "SkipLayerGuidanceDiTSimple", "inputs": {"model": ["4", 0], "double_layers": "", "single_layers": "22, 24", "start_percent": 0, "end_percent": 0.5}},
        "6": {"class_type": "TemporalScoreRescaling", "inputs": {"model": ["5", 0], "tsr_k": 0.95, "tsr_sigma": 1}},
        "7": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": 1792, "height": 2512, "batch_size": 1}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "positive"}},
        "9": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "negative"}},
        "10": {"class_type": "KSampler", "inputs": {"model": ["6", 0], "positive": ["8", 0],
               "negative": ["9", 0], "latent_image": ["7", 0], "seed": 1, "steps": 10, "cfg": 3,
               "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": "ComfyUI"}},
    }


def draft(graph):
    return PresetDraft(name="MOSS", kind="txt2img", graph=graph, bindings=InputBindings(
        positive=PortBinding(node_id="8", input_name="text"),
        negative=PortBinding(node_id="9", input_name="text"),
        seed=PortBinding(node_id="10", input_name="seed"), output=OutputBinding(node_id="12")))


def test_moss_graph_freezes_assets_and_wraps_every_managed_model(moss_info, moss_graph):
    capabilities = build_capabilities(moss_info, list(moss_info), adapter_version="moss-v6")
    validated = validate_workflow(draft(moss_graph), capabilities)
    assert {(asset.kind, asset.name) for asset in validated.manifest} == {
        ("diffusion_models", "moss.safetensors"), ("text_encoders", "qwen.safetensors"),
        ("vae", "flux2.safetensors"), ("loras", "style.safetensors")}
    prompt = UUID("00000000-0000-4000-8000-000000000123")
    execution = instantiate_workflow(validated, "一只猫", "模糊", image_data=None, seed=42,
                                     prompt_id=prompt, mode="green")
    managed = managed_graph(execution.graph, prompt)
    assert execution.graph["8"]["inputs"]["text"].endswith("\nsfw")
    assert execution.graph["9"]["inputs"]["text"].endswith("\nnsfw")
    assert [managed.graph[str(i)]["class_type"] for i in range(1, 7)] == [
        "AL1SUNETLoader", "AL1SCLIPLoader", "AL1SVAELoader", "AL1SLoraLoaderModelOnly",
        "AL1SSkipLayerGuidanceDiTSimple", "AL1STemporalScoreRescaling"]
    assert all(managed.graph[str(i)]["inputs"]["al1s_prompt_id"] == str(prompt) for i in range(1, 7))
    assert moss_graph["10"]["inputs"]["seed"] == 1


def test_moss_reference_image_reaches_vae_and_sampler_with_managed_models(moss_info, moss_graph):
    moss_info["ImageReceiver"] = node("custom_nodes.ComfyUI-Impact-Pack", ("IMAGE", "MASK"),
        image=["STRING"], link_id=["INT"], save_to_workflow=["BOOLEAN"], image_data=["STRING"],
        trigger_always=["BOOLEAN"])
    moss_info["VAEEncode"] = node("nodes", ("LATENT",), pixels=["IMAGE"], vae=["VAE"])
    moss_graph["7"] = {"class_type": "ImageReceiver", "inputs": {"image": "#DATA", "link_id": 0,
        "save_to_workflow": True, "image_data": "", "trigger_always": False}}
    moss_graph["13"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["7", 0], "vae": ["3", 0]}}
    moss_graph["10"]["inputs"].update(latent_image=["13", 0], denoise=0.85)
    edit = PresetDraft(name="MOSS 重绘", kind="image_edit", graph=moss_graph, bindings=InputBindings(
        positive=PortBinding(node_id="8", input_name="text"),
        negative=PortBinding(node_id="9", input_name="text"),
        image=PortBinding(node_id="7", input_name="image_data"),
        seed=PortBinding(node_id="10", input_name="seed"), output=OutputBinding(node_id="12")))
    capabilities = build_capabilities(moss_info, list(moss_info), adapter_version="moss-v6")
    validated = validate_workflow(edit, capabilities)
    image = BytesIO()
    Image.new("RGB", (16, 16), "blue").save(image, format="PNG")
    reference = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    with pytest.raises(ValueError, match="drawing_image_required"):
        instantiate_workflow(validated, "红色裙子", image_data=None, seed=42, prompt_id=prompt_id)
    execution = instantiate_workflow(validated, "红色裙子", "模糊", image_data=reference,
                                     seed=42, prompt_id=prompt_id, mode="green")
    managed = managed_graph(execution.graph, prompt_id).graph
    assert managed["7"]["class_type"] == "AL1SImageReceiver"
    assert managed["7"]["inputs"]["image_data"] == reference
    assert managed["13"]["inputs"]["pixels"] == ["7", 0]
    assert managed["10"]["inputs"]["latent_image"] == ["13", 0]
    assert managed["3"]["class_type"] == "AL1SVAELoader"


@pytest.mark.parametrize("field,value", [("width", 4096), ("width", 1793), ("batch_size", 2)])
def test_moss_latent_obeys_existing_output_limits(moss_info, moss_graph, field, value):
    capabilities = build_capabilities(moss_info, list(moss_info), adapter_version="moss-v6")
    moss_graph["7"]["inputs"][field] = value
    with pytest.raises(ValueError, match="drawing_(dimensions|batch)_invalid"):
        validate_workflow(draft(moss_graph), capabilities)


def test_moss_loader_rejects_another_encoder_family_and_untrusted_node(moss_info, moss_graph):
    capabilities = build_capabilities(moss_info, list(moss_info), adapter_version="moss-v6")
    bad = deepcopy(moss_graph)
    bad["2"]["inputs"]["type"] = "flux2"
    with pytest.raises(ValueError, match="drawing_input_choice_invalid"):
        validate_workflow(draft(bad), capabilities)
    moss_info["UNETLoader"]["python_module"] = "custom_nodes.replacement"
    with pytest.raises(ValueError, match="drawing_node_origin_invalid"):
        build_capabilities(moss_info, list(moss_info), adapter_version="moss-v6")


def test_exported_moss_graph_accepts_default_optional_encoder_device_only(moss_info, moss_graph):
    capabilities = build_capabilities(moss_info, list(moss_info), adapter_version="moss-v6")
    moss_graph["2"]["inputs"]["device"] = "default"
    assert validate_workflow(draft(moss_graph), capabilities)
    moss_graph["2"]["inputs"]["device"] = "cpu"
    with pytest.raises(ValueError, match="drawing_input_choice_invalid"):
        validate_workflow(draft(moss_graph), capabilities)


def test_v6_only_preserves_v5_inspections_for_unchanged_legacy_node_sets():
    from al1s.bot_drawing.capabilities import compatible_inspection

    v5 = "comfy-0.43-bb878b9-al1s-v5"
    v6 = "comfy-0.43-bb878b9-al1s-v6"
    result = {"adapter": v6, "boot_id": "boot", "nodes": [
        {"name": "CheckpointLoaderSimple", "sha256": "a" * 64}], "assets": []}
    normalized = compatible_inspection(result, v5, ["CheckpointLoaderSimple"])
    assert normalized["adapter"] == v5 and normalized["nodes"] == result["nodes"]
    assert result["adapter"] == v6
    with pytest.raises(ValueError, match="drawing_dependency_changed"):
        compatible_inspection(result, v5, ["UNETLoader"])
    with pytest.raises(ValueError, match="drawing_dependency_changed"):
        compatible_inspection({**result, "adapter": "unrecognized"}, v5, ["CheckpointLoaderSimple"])
    with pytest.raises(ValueError, match="drawing_dependency_changed"):
        compatible_inspection({**result, "adapter": v5}, v6, ["CheckpointLoaderSimple"])
