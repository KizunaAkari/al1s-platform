import base64
import asyncio
import hashlib
import json
from copy import deepcopy
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image

from al1s.bot_drawing.contracts import (
    InputBindings,
    InputCapability,
    NodeCapability,
    OutputBinding,
    PortBinding,
    PresetDraft,
    WorkflowCapabilities,
)
from al1s.bot_drawing.workflow import instantiate_workflow, managed_graph, validate_workflow


def image_data(format="PNG", *, mime="png"):
    output = BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format=format)
    return f"data:image/{mime};base64," + base64.b64encode(output.getvalue()).decode("ascii")


@pytest.fixture
def capabilities():
    def port(kind, **kwargs):
        return InputCapability(kind=kind, **kwargs)

    return WorkflowCapabilities(adapter_version="test-v1", nodes={
        "CheckpointLoaderSimple": NodeCapability(
            inputs={"ckpt_name": port("STRING", asset_kind="checkpoints")},
            outputs=("MODEL", "CLIP", "VAE"),
        ),
        "LoraLoader": NodeCapability(
            inputs={"model": port("MODEL"), "clip": port("CLIP"),
                    "lora_name": port("STRING", asset_kind="loras"),
                    "strength_model": port("FLOAT"), "strength_clip": port("FLOAT")},
            outputs=("MODEL", "CLIP"),
        ),
        "CLIPTextEncode": NodeCapability(
            inputs={"clip": port("CLIP"), "text": port("STRING", bind="text")},
            outputs=("CONDITIONING",),
        ),
        "EmptyLatentImage": NodeCapability(
            inputs={"width": port("INT"), "height": port("INT"), "batch_size": port("INT")},
            outputs=("LATENT",),
        ),
        "KSampler": NodeCapability(
            inputs={"model": port("MODEL"), "positive": port("CONDITIONING"),
                    "negative": port("CONDITIONING"), "latent_image": port("LATENT"),
                    "seed": port("INT", bind="seed"), "steps": port("INT")},
            outputs=("LATENT",),
        ),
        "VAEDecode": NodeCapability(inputs={"samples": port("LATENT"), "vae": port("VAE")},
                                    outputs=("IMAGE",)),
        "SaveImage": NodeCapability(inputs={"images": port("IMAGE"), "filename_prefix": port("STRING")},
                                    outputs=("IMAGE",), image_output=True,
                                    owned_output_input="filename_prefix"),
        "ImageReceiver": NodeCapability(inputs={"image": port("STRING", choices=("#DATA",)),
            "link_id": port("INT"), "save_to_workflow": port("BOOLEAN"),
            "image_data": port("STRING", bind="image"), "trigger_always": port("BOOLEAN")},
            outputs=("IMAGE", "MASK")),
        "VAEEncode": NodeCapability(inputs={"pixels": port("IMAGE"), "vae": port("VAE")},
                                    outputs=("LATENT",)),
    })


@pytest.fixture
def graph():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
        "2": {"class_type": "LoraLoader", "inputs": {"model": ["1", 0], "clip": ["1", 1],
            "lora_name": "portrait.safetensors", "strength_model": 0.7, "strength_clip": 0.5}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 1], "text": "positive"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 1], "text": "negative"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "positive": ["3", 0],
            "negative": ["4", 0], "latent_image": ["5", 0], "seed": 1, "steps": 20}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "ComfyUI"}},
    }


def preset(graph, *, kind="txt2img", image=None):
    return PresetDraft(name="动漫", kind=kind, graph=graph, bindings=InputBindings(
        positive=PortBinding(node_id="3", input_name="text"),
        negative=PortBinding(node_id="4", input_name="text"), negative_prefix="negative",
        seed=PortBinding(node_id="6", input_name="seed"),
        image=image,
        output=OutputBinding(node_id="8", output_index=0),
    ))


def test_frozen_models_only_bound_inputs_change(graph, capabilities):
    original = deepcopy(graph)
    validated = validate_workflow(preset(graph), capabilities)
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    execution = instantiate_workflow(validated, "一只猫", image_data=None, seed=42, prompt_id=prompt_id)
    assert execution.graph["3"]["inputs"]["text"] == "一只猫"
    assert execution.graph["6"]["inputs"]["seed"] == 42
    assert execution.graph["1"] == original["1"]
    assert execution.graph["2"] == original["2"]
    assert execution.graph["4"] == original["4"]
    assert execution.graph["8"]["inputs"]["filename_prefix"] == f"al1s-drawing/{prompt_id}/output"
    assert graph == original
    assert {(item.kind, item.name) for item in validated.manifest} == {
        ("checkpoints", "sdxl.safetensors"), ("loras", "portrait.safetensors")}


def test_managed_graph_wraps_model_and_output_nodes_without_changing_values(graph, capabilities):
    validated = validate_workflow(preset(graph), capabilities)
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    execution = instantiate_workflow(validated, "一只猫", image_data=None, seed=42, prompt_id=prompt_id)
    original = deepcopy(execution.graph)
    managed = managed_graph(execution.graph, prompt_id)
    assert execution.graph == original
    for node_id, class_name in (("1", "AL1SCheckpointLoader"), ("2", "AL1SLoraLoader"), ("8", "AL1SSaveImage")):
        assert managed.graph[node_id]["class_type"] == class_name
        assert managed.graph[node_id]["inputs"]["al1s_prompt_id"] == str(prompt_id)
        assert {key: value for key, value in managed.graph[node_id]["inputs"].items()
                if key != "al1s_prompt_id"} == original[node_id]["inputs"]
    assert managed.graph_hash != execution.graph_hash


def test_managed_graph_matches_comfy_float_normalization(graph, capabilities):
    validated = validate_workflow(preset(graph), capabilities)
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    execution = instantiate_workflow(validated, "cat", image_data=None, seed=42, prompt_id=prompt_id)
    execution.graph["6"]["inputs"].update(cfg=7, denoise=1)
    execution.graph["2"]["inputs"]["strength_model"] = 1
    original = deepcopy(execution.graph)

    managed = managed_graph(execution.graph, prompt_id)
    normalized = deepcopy(managed.graph)
    for node, fields in (("6", ("cfg", "denoise")), ("2", ("strength_model",))):
        for field in fields:
            normalized[node]["inputs"][field] = float(normalized[node]["inputs"][field])
    digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert managed.graph_hash == digest
    assert type(managed.graph["6"]["inputs"]["steps"]) is int
    assert execution.graph == original


def test_ui_json_and_unknown_loader_rejected(graph, capabilities):
    with pytest.raises(ValueError, match="drawing_workflow"):
        validate_workflow(preset({"nodes": [], "links": []}), capabilities)
    graph["1"]["class_type"] = "UnreviewedRemoteModelLoader"
    with pytest.raises(ValueError, match="drawing_node_unsupported"):
        validate_workflow(preset(graph), capabilities)


def test_explicit_output_is_single_image(graph, capabilities):
    graph["9"] = deepcopy(graph["8"])
    with pytest.raises(ValueError, match="drawing_output"):
        validate_workflow(preset(graph), capabilities)


def test_rejects_paths_cycles_type_mismatch_and_batch(graph, capabilities):
    bad = deepcopy(graph)
    bad["2"]["inputs"]["lora_name"] = "../other/secret.safetensors"
    with pytest.raises(ValueError, match="drawing_asset"):
        validate_workflow(preset(bad), capabilities)
    bad = deepcopy(graph)
    bad["6"]["inputs"]["latent_image"] = ["6", 0]
    with pytest.raises(ValueError, match="drawing_cycle"):
        validate_workflow(preset(bad), capabilities)
    bad = deepcopy(graph)
    bad["7"]["inputs"]["vae"] = ["2", 0]
    with pytest.raises(ValueError, match="drawing_link_type"):
        validate_workflow(preset(bad), capabilities)
    bad = deepcopy(graph)
    bad["5"]["inputs"]["batch_size"] = 2
    with pytest.raises(ValueError, match="drawing_batch"):
        validate_workflow(preset(bad), capabilities)


def test_image_edit_requires_explicit_image_and_keeps_preset_unchanged(graph, capabilities):
    graph["9"] = {"class_type": "ImageReceiver", "inputs": {"image": "#DATA", "link_id": 0,
        "save_to_workflow": True, "image_data": "", "trigger_always": False}}
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
    validated = validate_workflow(preset(graph, kind="image_edit",
        image=PortBinding(node_id="9", input_name="image_data")), capabilities)
    with pytest.raises(ValueError, match="drawing_image_required"):
        instantiate_workflow(validated, "把背景换成雪山", image_data=None, seed=3, prompt_id=UUID(int=1))
    reference = image_data()
    result = instantiate_workflow(validated, "把背景换成雪山", image_data=reference,
                                  seed=3, prompt_id=UUID(int=1))
    assert result.graph["9"]["inputs"]["image_data"] == reference
    assert validated.graph["9"]["inputs"]["image_data"] == ""


def test_digest_is_stable_and_snapshot_tampering_is_rejected(graph, capabilities):
    first = validate_workflow(preset(graph), capabilities)
    second = validate_workflow(preset(dict(reversed(list(graph.items())))), capabilities)
    assert first.graph_hash == second.graph_hash
    first.graph["2"]["inputs"]["strength_model"] = 99
    with pytest.raises(ValueError, match="drawing_snapshot_changed"):
        instantiate_workflow(first, "cat", image_data=None, seed=1, prompt_id=UUID(int=1))


def test_positive_and_negative_conditions_keep_fixed_prefixes_and_model_settings(graph, capabilities):
    node = capabilities.nodes["CLIPTextEncode"].model_copy(update={
        "inputs": {"clip": InputCapability(kind="CLIP"),
                   "text": InputCapability(kind="STRING", bind="text")},
    })
    capabilities = capabilities.model_copy(update={"nodes": {**capabilities.nodes, "CLIPTextEncode": node}})
    draft = PresetDraft(name="动漫", kind="txt2img", graph=graph, bindings=InputBindings(
        positive=PortBinding(node_id="3", input_name="text"),
        negative=PortBinding(node_id="4", input_name="text"),
        positive_prefix="masterpiece, trigger_word", negative_prefix="low quality",
        seed=PortBinding(node_id="6", input_name="seed"),
        output=OutputBinding(node_id="8")))
    original = deepcopy(graph)
    validated = validate_workflow(draft, capabilities)
    result = instantiate_workflow(validated, "一只猫", "猫耳", image_data=None, seed=42, prompt_id=UUID(int=1))
    assert result.graph["3"]["inputs"]["text"] == "masterpiece, trigger_word\n一只猫"
    assert result.graph["4"]["inputs"]["text"] == "low quality\n猫耳"
    assert result.graph["2"] == original["2"] and graph == original


def test_green_conditions_are_added_to_execution_without_changing_the_preset(graph, capabilities):
    validated = validate_workflow(preset(graph), capabilities)
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    before = deepcopy(validated.graph)
    green = instantiate_workflow(validated, "cat", "watermark", image_data=None, seed=42,
                                 prompt_id=prompt_id, mode="green")
    assert green.graph["3"]["inputs"]["text"] == "cat\nsfw"
    assert green.graph["4"]["inputs"]["text"] == "negative\nwatermark\nnsfw"
    assert validated.graph == before
    r18 = instantiate_workflow(validated, "cat", "watermark", image_data=None, seed=42,
                               prompt_id=prompt_id, mode="r18")
    assert r18.graph["3"]["inputs"]["text"] == "cat"
    assert r18.graph["4"]["inputs"]["text"] == "negative\nwatermark"
    assert green.graph_hash != r18.graph_hash


def test_green_conditions_require_negative_mapping_without_reducing_user_limit(graph, capabilities):
    validated = validate_workflow(preset(graph), capabilities)
    prompt_id = UUID("00000000-0000-4000-8000-000000000123")
    green = instantiate_workflow(validated, "x" * 2000, image_data=None, seed=1,
                                 prompt_id=prompt_id, mode="green")
    assert green.graph["3"]["inputs"]["text"].endswith("\nsfw")
    draft = PresetDraft(name="动漫", kind="txt2img", graph=graph, bindings=InputBindings(
        positive=PortBinding(node_id="3", input_name="text"), output=OutputBinding(node_id="8")))
    no_negative = validate_workflow(draft, capabilities)
    with pytest.raises(ValueError, match="drawing_negative_unsupported"):
        instantiate_workflow(no_negative, "cat", image_data=None, seed=1, prompt_id=prompt_id, mode="green")


def test_runner_builds_green_conditions_from_the_frozen_task_mode(graph, capabilities):
    from al1s.adapters.postgres.meme_base import fingerprint
    from al1s.bot_drawing.runner import DrawingTaskRunner

    validated = validate_workflow(preset(graph), capabilities)
    dependencies = {'adapter': 'test-v1', 'nodes': [], 'assets': []}
    class Repo:
        def get_workflow(self, *_): return validated
        def get_dependencies(self, *_, **__): return dependencies
    task = {'operation': 'draw', 'origin': 'admin_preview', 'input_reference': None,
            'service_id': UUID(int=1), 'preset_id': UUID(int=2), 'preset_revision': 1,
            'dependency_signature': fingerprint(dependencies), 'positive': 'cat', 'negative': 'watermark',
            'seed': 42, 'prompt_id': UUID('00000000-0000-4000-8000-000000000123'), 'audit_mode': 'green'}
    runner = DrawingTaskRunner(Repo(), None, None, None, None, lambda _: None)
    built = asyncio.run(runner._build_execution(task))
    assert built.graph['3']['inputs']['text'] == 'cat\nsfw'
    assert built.graph['4']['inputs']['text'] == 'negative\nwatermark\nnsfw'
    encoded = json.dumps(built.graph, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert built.execution_hash == hashlib.sha256(encoded).hexdigest()


def test_negative_condition_is_not_silently_dropped_when_unmapped(graph, capabilities):
    node = capabilities.nodes["CLIPTextEncode"].model_copy(update={
        "inputs": {"clip": InputCapability(kind="CLIP"),
                   "text": InputCapability(kind="STRING", bind="text")},
    })
    capabilities = capabilities.model_copy(update={"nodes": {**capabilities.nodes, "CLIPTextEncode": node}})
    draft = PresetDraft(name="动漫", kind="txt2img", graph=graph, bindings=InputBindings(
        positive=PortBinding(node_id="3", input_name="text"),
        output=OutputBinding(node_id="8")))
    validated = validate_workflow(draft, capabilities)
    with pytest.raises(ValueError, match="drawing_negative_unsupported"):
        instantiate_workflow(validated, "一只猫", "猫耳", image_data=None, seed=42, prompt_id=UUID(int=1))


def test_large_user_workflow_size_is_allowed_but_eight_megapixels_is_enforced(graph, capabilities):
    graph["5"]["inputs"].update(width=1792, height=2512)
    assert validate_workflow(preset(graph), capabilities)
    graph["5"]["inputs"].update(width=4096, height=2048)
    with pytest.raises(ValueError, match="drawing_dimensions_invalid"):
        validate_workflow(preset(graph), capabilities)


def test_mapped_positive_and_negative_cannot_share_one_input(graph, capabilities):
    binding = PortBinding(node_id="3", input_name="text")
    draft = PresetDraft(name="动漫", kind="txt2img", graph=graph, bindings=InputBindings(
        positive=binding, negative=binding, output=OutputBinding(node_id="8")))
    with pytest.raises(ValueError, match="drawing_binding_invalid"):
        validate_workflow(draft, capabilities)


def test_img2img_load_and_preview_are_safely_adapted_without_changing_source(graph, capabilities):
    graph["9"] = {"class_type": "LoadImage", "inputs": {"image": "sample.png"}}
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
    graph["8"] = {"class_type": "PreviewImage", "inputs": {"images": ["7", 0]}}
    original = deepcopy(graph)
    draft = preset(graph, kind="image_edit", image=PortBinding(node_id="9", input_name="image"))
    validated = validate_workflow(draft, capabilities)
    assert graph == original and validated.source_graph["9"]["class_type"] == "LoadImage"
    assert validated.graph["9"]["class_type"] == "ImageReceiver"
    assert validated.graph["9"]["inputs"]["image"] == "#DATA"
    assert validated.graph["8"]["class_type"] == "SaveImage"
    assert validated.graph_hash != validated.execution_template_hash
    assert validated.bindings.image.input_name == "image_data"
    assert validated.source_bindings.image.input_name == "image"
    assert "sample.png" not in str(validated.graph)


def test_img2img_conversion_rejects_mask_links_and_traversal_paths(graph, capabilities):
    graph["9"] = {"class_type": "LoadImage", "inputs": {"image": "sample.png"}}
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 1], "vae": ["1", 2]}}
    draft = preset(graph, kind="image_edit", image=PortBinding(node_id="9", input_name="image"))
    with pytest.raises(ValueError, match="drawing_image_mask_unsupported"):
        validate_workflow(draft, capabilities)
    graph["5"]["inputs"]["pixels"] = ["9", 0]
    graph["9"]["inputs"]["image"] = "../private.png"
    draft = preset(graph, kind="image_edit", image=PortBinding(node_id="9", input_name="image"))
    with pytest.raises(ValueError, match="drawing_asset_invalid"):
        validate_workflow(draft, capabilities)


@pytest.mark.parametrize("field,value", [
    ("image", "sample.png"), ("link_id", 12), ("save_to_workflow", False),
    ("image_data", "data:image/png;base64,YQ=="), ("trigger_always", True),
])
def test_img2img_receiver_must_use_owned_inline_input(graph, capabilities, field, value):
    graph["9"] = {"class_type": "ImageReceiver", "inputs": {"image": "#DATA", "link_id": 0,
        "save_to_workflow": True, "image_data": "", "trigger_always": False}}
    graph["9"]["inputs"][field] = value
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
    draft = preset(graph, kind="image_edit", image=PortBinding(node_id="9", input_name="image_data"))
    with pytest.raises(ValueError):
        validate_workflow(draft, capabilities)


def test_img2img_receiver_cannot_use_its_mask_output(graph, capabilities):
    graph["9"] = {"class_type": "ImageReceiver", "inputs": {"image": "#DATA", "link_id": 0,
        "save_to_workflow": True, "image_data": "", "trigger_always": False}}
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 1], "vae": ["1", 2]}}
    draft = preset(graph, kind="image_edit", image=PortBinding(node_id="9", input_name="image_data"))
    with pytest.raises(ValueError, match="drawing_image_mask_unsupported"):
        validate_workflow(draft, capabilities)


@pytest.mark.parametrize("reference", ["data:image/png;base64,YQ==", image_data("GIF"), image_data("JPEG")])
def test_image_data_rejects_invalid_or_mislabeled_content(graph, capabilities, reference):
    graph["9"] = {"class_type": "ImageReceiver", "inputs": {"image": "#DATA", "link_id": 0,
        "save_to_workflow": True, "image_data": "", "trigger_always": False}}
    graph["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
    validated = validate_workflow(preset(graph, kind="image_edit",
        image=PortBinding(node_id="9", input_name="image_data")), capabilities)
    with pytest.raises(ValueError, match="drawing_image_invalid"):
        instantiate_workflow(validated, "换背景", image_data=reference, seed=3, prompt_id=UUID(int=1))
