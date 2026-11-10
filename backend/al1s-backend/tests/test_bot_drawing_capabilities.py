from importlib import import_module

import pytest


def object_info():
    return {
        "CheckpointLoaderSimple": {
            "python_module": "nodes", "output": ["MODEL", "CLIP", "VAE"], "output_node": False,
            "input": {"required": {"ckpt_name": [["wai.safetensors"], {}]}},
        },
        "UpscaleModelLoader": {
            "python_module": "comfy_extras.nodes_upscale_model", "output": ["UPSCALE_MODEL"],
            "output_node": False,
            "input": {"required": {"model_name": ["COMBO", {"options": ["lollypop.pth"]}]}},
        },
    }


def build(info, nodes):
    return import_module("al1s.bot_drawing.capabilities").build_capabilities(
        info, nodes, adapter_version="test-v1")


def test_reviewed_loaders_use_server_choices_and_separate_asset_directories():
    result = build(object_info(), ["CheckpointLoaderSimple", "UpscaleModelLoader"])
    checkpoint = result.nodes["CheckpointLoaderSimple"].inputs["ckpt_name"]
    upscale = result.nodes["UpscaleModelLoader"].inputs["model_name"]
    assert checkpoint.kind == "STRING" and checkpoint.choices == ("wai.safetensors",)
    assert checkpoint.asset_kind == "checkpoints"
    assert upscale.kind == "STRING" and upscale.choices == ("lollypop.pth",)
    assert upscale.asset_kind == "upscale_models"


def test_unknown_or_replaced_node_is_not_trusted_from_object_info():
    info = object_info()
    info["RemoteLoader"] = {"python_module": "custom_nodes.unknown", "output": [], "input": {"required": {}}}
    with pytest.raises(ValueError, match="drawing_node_unsupported"):
        build(info, ["RemoteLoader"])
    info["CheckpointLoaderSimple"]["python_module"] = "custom_nodes.replacement"
    with pytest.raises(ValueError, match="drawing_node_origin_invalid"):
        build(info, ["CheckpointLoaderSimple"])


def test_changed_required_inputs_or_output_are_rejected():
    info = object_info()
    info["CheckpointLoaderSimple"]["input"]["required"]["path"] = ["STRING", {}]
    with pytest.raises(ValueError, match="drawing_node_schema_invalid"):
        build(info, ["CheckpointLoaderSimple"])
    info = object_info()
    info["UpscaleModelLoader"]["output"] = ["IMAGE"]
    with pytest.raises(ValueError, match="drawing_node_schema_invalid"):
        build(info, ["UpscaleModelLoader"])


def test_malformed_remote_metadata_is_rejected_as_invalid_schema():
    info = object_info()
    info["UpscaleModelLoader"]["output"] = None
    with pytest.raises(ValueError, match="drawing_node_schema_invalid"):
        build(info, ["UpscaleModelLoader"])
    info = object_info()
    info["UpscaleModelLoader"]["input"]["required"]["model_name"] = ["COMBO", {}]
    with pytest.raises(ValueError, match="drawing_node_schema_invalid"):
        build(info, ["UpscaleModelLoader"])
    with pytest.raises(ValueError, match="drawing_node_schema_invalid"):
        build(object_info(), [["CheckpointLoaderSimple"]])


def test_image_receiver_accepts_only_embedded_images_not_user_paths():
    info = object_info()
    info["ImageReceiver"] = {
        "python_module": "custom_nodes.ComfyUI-Impact-Pack", "output": ["IMAGE", "MASK"],
        "output_node": False, "input": {"required": {
            "image": [["private-reference.png"], {}], "link_id": ["INT", {"min": 0}],
            "save_to_workflow": ["BOOLEAN", {}], "image_data": ["STRING", {}],
            "trigger_always": ["BOOLEAN", {}],
        }},
    }
    capability = build(info, ["ImageReceiver"]).nodes["ImageReceiver"]
    assert capability.inputs["image"].choices == ("#DATA",)
    assert capability.inputs["image_data"].bind == "image"
    assert capability.outputs == ("IMAGE", "MASK")


def test_img2img_vae_encode_node_can_be_reviewed():
    info = object_info()
    info["VAEEncode"] = {"python_module": "nodes", "output": ["LATENT"], "output_node": False,
                         "input": {"required": {"pixels": ["IMAGE"], "vae": ["VAE"]}}}
    capability = build(info, ["VAEEncode"]).nodes["VAEEncode"]
    assert capability.inputs["pixels"].kind == "IMAGE" and capability.outputs == ("LATENT",)


def test_v8_resources_keep_reviewed_v7_workflow_identity_without_accepting_unknown_nodes():
    from al1s.bot_drawing.capabilities import compatible_inspection

    v7 = "comfy-0.43-bb878b9-al1s-v7"
    v8 = "comfy-0.43-bb878b9-al1s-v8"
    result = {"adapter": v8, "nodes": [], "assets": []}
    assert compatible_inspection(result, v7, ["TextEncodeQwenImage21"])["adapter"] == v7
    with pytest.raises(ValueError, match="drawing_dependency_changed"):
        compatible_inspection(result, v7, ["UnreviewedNode"])
