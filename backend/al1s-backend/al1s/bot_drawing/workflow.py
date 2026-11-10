import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import PureWindowsPath
from uuid import UUID

from .contracts import (
    AssetDependency, ExecutionGraph, InputCapability, OutputBinding, PortBinding, PresetDraft,
    ValidatedWorkflow, WorkflowCapabilities,
)
from .media import MAX_IMAGE_DATA, validate_image_data

MAX_GRAPH_BYTES = 256 * 1024
_PRIMITIVES = {"STRING", "INT", "FLOAT", "BOOLEAN"}
_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _encoded(value, *, limit: int) -> bytes:
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeEncodeError) as error:
        raise ValueError("drawing_workflow_invalid") from error
    if len(body) > limit:
        raise ValueError("drawing_workflow_too_large")
    return body


def _hash(value, *, limit: int = MAX_GRAPH_BYTES) -> str:
    return hashlib.sha256(_encoded(value, limit=limit)).hexdigest()


def _asset_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("drawing_asset_invalid")
    windows = PureWindowsPath(value)
    segments = value.replace("\\", "/").split("/")
    if (windows.drive or value.startswith(("/", "\\")) or any(p in {"", ".", ".."} for p in segments)
            or any(ord(c) < 32 or c in ':*?"<>|' for c in value)):
        raise ValueError("drawing_asset_invalid")
    return value


def _literal(value: object, spec: InputCapability) -> None:
    if spec.kind == "STRING":
        valid = isinstance(value, str) and len(value) <= MAX_GRAPH_BYTES
    elif spec.kind == "BOOLEAN":
        valid = type(value) is bool
    elif spec.kind == "INT":
        valid = type(value) is int
    elif spec.kind == "FLOAT":
        valid = type(value) in {int, float} and math.isfinite(value)
    else:
        valid = False
    if not valid:
        raise ValueError("drawing_input_type_invalid")
    if spec.choices is not None and value not in spec.choices:
        raise ValueError("drawing_input_choice_invalid")
    if type(value) in {int, float}:
        if (spec.minimum is not None and value < spec.minimum) or (
                spec.maximum is not None and value > spec.maximum):
            raise ValueError("drawing_input_range_invalid")


def _link(value: object) -> tuple[str, int] | None:
    if not isinstance(value, list):
        return None
    if (len(value) != 2 or not isinstance(value[0], str) or type(value[1]) is not int
            or value[1] < 0):
        raise ValueError("drawing_link_invalid")
    return value[0], value[1]


def _binding(graph, capabilities, binding: PortBinding, role: str) -> None:
    node = graph.get(binding.node_id)
    if node is None:
        raise ValueError("drawing_binding_invalid")
    node_spec = capabilities.nodes[node["class_type"]]
    spec = node_spec.inputs.get(binding.input_name)
    expected = "text" if role in {"positive", "negative"} else role
    if (spec is None or spec.bind != expected or binding.input_name not in node["inputs"]
            or _link(node["inputs"][binding.input_name]) is not None):
        raise ValueError("drawing_binding_invalid")
    value = node["inputs"][binding.input_name]
    if role == "image" and value not in {"", "#DATA"}:
        raise ValueError("drawing_image_template_not_empty")


def _check_dag(dependencies: dict[str, set[str]], output: str) -> None:
    visiting, visited = set(), set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("drawing_cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for parent in dependencies[node_id]:
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in dependencies:
        visit(node_id)
    reachable = set()

    def collect(node_id: str) -> None:
        if node_id not in reachable:
            reachable.add(node_id)
            for parent in dependencies[node_id]:
                collect(parent)

    collect(output)
    if reachable != set(dependencies):
        raise ValueError("drawing_disconnected_node")


def _snapshot(workflow: ValidatedWorkflow) -> dict:
    return {"kind": workflow.kind, "source_graph": workflow.source_graph, "graph": workflow.graph,
            "bindings": workflow.bindings.model_dump(mode="json"),
            "source_bindings": (workflow.source_bindings or workflow.bindings).model_dump(mode="json"),
            "adapter_version": workflow.adapter_version,
            "output_prefix": workflow.output_prefix.model_dump(mode="json"),
            "manifest": [asdict(item) for item in workflow.manifest]}


def _adapt_inputs_and_output(source, draft):
    graph = deepcopy(source)
    bindings = draft.bindings
    image = bindings.image
    if image and image.node_id in graph and graph[image.node_id]["class_type"] == "LoadImage":
        node = graph[image.node_id]
        if draft.kind != "image_edit" or image.input_name != "image" or set(node["inputs"]) != {"image"}:
            raise ValueError("drawing_image_binding_invalid")
        _asset_name(node["inputs"]["image"])
        for item in graph.values():
            for value in item["inputs"].values():
                if _link(value) == (image.node_id, 1):
                    raise ValueError("drawing_image_mask_unsupported")
        graph[image.node_id] = {"class_type": "ImageReceiver", "inputs": {
            "image": "#DATA", "link_id": 0, "save_to_workflow": True,
            "image_data": "", "trigger_always": False}}
        bindings = bindings.model_copy(update={"image": PortBinding(node_id=image.node_id, input_name="image_data")})

    output = bindings.output.node_id
    if output in graph and graph[output]["class_type"] == "PreviewImage":
        inputs = graph[output]["inputs"]
        if set(inputs) != {"images"}:
            raise ValueError("drawing_output_invalid")
        graph[output] = {"class_type": "SaveImage", "inputs": {
            "images": deepcopy(inputs["images"]), "filename_prefix": "al1s-drawing/placeholder"}}
    return graph, bindings


def _check_qwen_edit(graph, draft, bindings):
    qwen = [(key, value) for key, value in graph.items() if value["class_type"] == "TextEncodeQwenImage21"]
    if not qwen:
        return
    samplers = [value for value in graph.values() if value["class_type"] == "KSampler"]
    if len(qwen) != 1 or len(samplers) != 1 or draft.kind != "image_edit" or not bindings.image:
        raise ValueError("drawing_qwen_edit_invalid")
    node_id, node = qwen[0]
    values, sample = node["inputs"], samplers[0]["inputs"]
    clip = _link(values["clip"])
    vae = _link(values["vae"])
    if (bindings.positive != PortBinding(node_id=node_id, input_name="prompt")
            or bindings.negative != PortBinding(node_id=node_id, input_name="negative_prompt")
            or _link(values["images.image_1"]) != (bindings.image.node_id, 0)
            or graph.get(bindings.image.node_id, {}).get("class_type") != "ImageReceiver"
            or clip is None or clip[1] != 0 or graph[clip[0]]["class_type"] != "CLIPLoader"
            or graph[clip[0]]["inputs"].get("type") != "qwen_image"
            or vae is None or vae[1] != 0 or graph[vae[0]]["class_type"] != "VAELoader"
            or values["resolution"] != 0 or type(values["resolution"]) is not int
            or _link(sample["positive"]) != (node_id, 0) or _link(sample["negative"]) != (node_id, 1)
            or _link(sample["latent_image"]) != (node_id, 2)
            or type(sample["cfg"]) not in {int, float} or sample["cfg"] <= 1
            or sample["denoise"] != 1):
        raise ValueError("drawing_qwen_edit_invalid")


def validate_workflow(draft: PresetDraft, capabilities: WorkflowCapabilities) -> ValidatedWorkflow:
    _encoded(draft.graph, limit=MAX_GRAPH_BYTES)
    if not 1 <= len(draft.graph) <= 100:
        raise ValueError("drawing_workflow_invalid")
    source_graph = {}
    for node_id, node in draft.graph.items():
        if (not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id)
                or not isinstance(node, dict) or set(node) - {"class_type", "inputs", "_meta"}
                or not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict)):
            raise ValueError("drawing_workflow_invalid")
        source_graph[node_id] = {"class_type": node["class_type"], "inputs": deepcopy(node["inputs"])}

    graph, bindings = _adapt_inputs_and_output(source_graph, draft)
    for node_id, node in graph.items():
        if node["class_type"] not in capabilities.nodes:
            raise ValueError("drawing_node_unsupported")
        if node["class_type"] == "ImageReceiver":
            inputs = node["inputs"]
            if (draft.kind != "image_edit" or bindings.image != PortBinding(node_id=node_id, input_name="image_data")
                    or set(inputs) != {"image", "link_id", "save_to_workflow", "image_data", "trigger_always"}
                    or inputs["image"] != "#DATA" or type(inputs["link_id"]) is not int
                    or inputs["link_id"] != 0 or inputs["save_to_workflow"] is not True
                    or inputs["image_data"] != "" or inputs["trigger_always"] is not False):
                raise ValueError("drawing_image_binding_invalid")
            if any(_link(value) == (node_id, 1) for item in graph.values() for value in item["inputs"].values()):
                raise ValueError("drawing_image_mask_unsupported")

    dependencies = {node_id: set() for node_id in graph}
    manifest = []
    output_nodes = []
    for node_id, node in graph.items():
        node_spec = capabilities.nodes[node["class_type"]]
        inputs = node["inputs"]
        if set(inputs) - set(node_spec.inputs):
            raise ValueError("drawing_input_unknown")
        if any(spec.required and name not in inputs for name, spec in node_spec.inputs.items()):
            raise ValueError("drawing_input_missing")
        if node_spec.image_output:
            output_nodes.append(node_id)
        for name, value in inputs.items():
            spec = node_spec.inputs[name]
            link = _link(value)
            if link is not None:
                source, index = link
                if source not in graph:
                    raise ValueError("drawing_link_missing")
                outputs = capabilities.nodes[graph[source]["class_type"]].outputs
                if index >= len(outputs) or outputs[index] != spec.kind:
                    raise ValueError("drawing_link_type_invalid")
                dependencies[node_id].add(source)
            else:
                _literal(value, spec)
                if spec.asset_kind:
                    manifest.append(AssetDependency(node_id, name, spec.asset_kind, _asset_name(value)))
            if name == "batch_size" and value != 1:
                raise ValueError("drawing_batch_invalid")
        if node["class_type"] in {"EmptyLatentImage", "EmptyFlux2LatentImage", "ImageScale"}:
            width, height = inputs.get("width"), inputs.get("height")
            minimum = 1 if node["class_type"] != "ImageScale" else 0
            if (type(width) is not int or type(height) is not int or min(width, height) < minimum
                    or max(width, height) > 4096 or width * height > 8_000_000
                    or node["class_type"] == "EmptyFlux2LatentImage" and (width % 16 or height % 16)):
                raise ValueError("drawing_dimensions_invalid")

    _check_qwen_edit(graph, draft, bindings)
    output = bindings.output.node_id
    if output_nodes != [output] or output not in graph:
        raise ValueError("drawing_output_invalid")
    output_spec = capabilities.nodes[graph[output]["class_type"]]
    prefix_name = output_spec.owned_output_input
    if (not prefix_name or prefix_name not in graph[output]["inputs"]
            or prefix_name not in output_spec.inputs or output_spec.inputs[prefix_name].kind != "STRING"
            or _link(graph[output]["inputs"][prefix_name]) is not None):
        raise ValueError("drawing_output_invalid")
    _check_dag(dependencies, output)
    _binding(graph, capabilities, bindings.positive, "positive")
    if bindings.negative:
        _binding(graph, capabilities, bindings.negative, "negative")
        if bindings.negative == bindings.positive:
            raise ValueError("drawing_binding_invalid")
    elif bindings.negative_prefix:
        raise ValueError("drawing_negative_unsupported")
    if bindings.seed:
        _binding(graph, capabilities, bindings.seed, "seed")
    if (draft.kind == "image_edit") != bool(bindings.image):
        raise ValueError("drawing_image_binding_invalid")
    if bindings.image:
        _binding(graph, capabilities, bindings.image, "image")
    output_prefix = PortBinding(node_id=output, input_name=prefix_name)
    if output_prefix in (bindings.positive, bindings.negative, bindings.seed, bindings.image):
        raise ValueError("drawing_binding_invalid")
    result = ValidatedWorkflow(draft.kind, source_graph, graph, bindings, _hash(source_graph), _hash(graph),
                               capabilities.adapter_version, tuple(manifest), output_prefix, "", draft.bindings)
    return ValidatedWorkflow(result.kind, result.source_graph, result.graph, result.bindings,
                             result.graph_hash, result.execution_template_hash, result.adapter_version,
                             result.manifest, result.output_prefix, _hash(_snapshot(result), limit=MAX_GRAPH_BYTES * 3),
                             result.source_bindings)


def ensure_workflow_intact(workflow: ValidatedWorkflow) -> None:
    if (_hash(workflow.source_graph) != workflow.graph_hash
            or _hash(workflow.graph) != workflow.execution_template_hash
            or _hash(_snapshot(workflow), limit=MAX_GRAPH_BYTES * 3) != workflow.snapshot_hash):
        raise ValueError("drawing_snapshot_changed")


def instantiate_workflow(workflow: ValidatedWorkflow, positive: str, negative: str = "", *,
                         image_data: str | None, seed: int, prompt_id: UUID,
                         mode: str | None = None) -> ExecutionGraph:
    ensure_workflow_intact(workflow)
    if (not isinstance(positive, str) or not isinstance(negative, str)
            or not (positive.strip() or negative.strip()) or len(positive) + len(negative) > 2000):
        raise ValueError("drawing_prompt_invalid")
    if negative.strip() and not workflow.bindings.negative:
        raise ValueError("drawing_negative_unsupported")
    if mode not in {None, "green", "r18"}:
        raise ValueError("drawing_mode_invalid")
    if mode == "green":
        if not workflow.bindings.negative:
            raise ValueError("drawing_negative_unsupported")
        positive = "\n".join(value for value in (positive, "sfw") if value.strip())
        negative = "\n".join(value for value in (negative, "nsfw") if value.strip())
    if type(seed) is not int or not 0 <= seed < 2 ** 63 or not isinstance(prompt_id, UUID):
        raise ValueError("drawing_request_invalid")
    if workflow.kind == "image_edit" and not image_data:
        raise ValueError("drawing_image_required")
    if workflow.kind == "txt2img" and image_data is not None:
        raise ValueError("drawing_image_unexpected")
    if image_data is not None:
        validate_image_data(image_data)

    graph = deepcopy(workflow.graph)
    binding = workflow.bindings.positive
    graph[binding.node_id]["inputs"][binding.input_name] = "\n".join(
        value for value in (workflow.bindings.positive_prefix, positive) if value.strip())
    if workflow.bindings.negative:
        binding = workflow.bindings.negative
        graph[binding.node_id]["inputs"][binding.input_name] = "\n".join(
            value for value in (workflow.bindings.negative_prefix, negative) if value.strip())
    if workflow.bindings.seed:
        graph[workflow.bindings.seed.node_id]["inputs"][workflow.bindings.seed.input_name] = seed
    if workflow.bindings.image:
        graph[workflow.bindings.image.node_id]["inputs"][workflow.bindings.image.input_name] = image_data
    graph[workflow.output_prefix.node_id]["inputs"][workflow.output_prefix.input_name] = (
        f"al1s-drawing/{prompt_id}/output")
    return ExecutionGraph(graph, _hash(graph, limit=MAX_GRAPH_BYTES + MAX_IMAGE_DATA),
                          workflow.bindings.output)


_MANAGED_CLASSES = {
    "CheckpointLoaderSimple": "AL1SCheckpointLoader",
    "UNETLoader": "AL1SUNETLoader",
    "CLIPLoader": "AL1SCLIPLoader",
    "VAELoader": "AL1SVAELoader",
    "LoraLoaderModelOnly": "AL1SLoraLoaderModelOnly",
    "SkipLayerGuidanceDiTSimple": "AL1SSkipLayerGuidanceDiTSimple",
    "TemporalScoreRescaling": "AL1STemporalScoreRescaling",
    "LoraLoader": "AL1SLoraLoader",
    "CLIPSetLastLayer": "AL1SCLIPSetLastLayer",
    "UpscaleModelLoader": "AL1SUpscaleModelLoader",
    "ImageReceiver": "AL1SImageReceiver",
    "SaveImage": "AL1SSaveImage",
    "WD14Tagger|pysssss": "AL1SWD14Tagger",
}


def managed_graph(graph: dict, prompt_id: UUID) -> ExecutionGraph:
    from .capabilities import _POLICIES

    if not isinstance(graph, dict) or not isinstance(prompt_id, UUID) or not 1 <= len(graph) <= 100:
        raise ValueError("drawing_workflow_invalid")
    result, outputs = deepcopy(graph), []
    for node_id, node in result.items():
        if (not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) or not isinstance(node, dict)
                or set(node) != {"class_type", "inputs"} or not isinstance(node["inputs"], dict)):
            raise ValueError("drawing_workflow_invalid")
        kind = node["class_type"]
        if kind not in _POLICIES and kind != "WD14Tagger|pysssss":
            raise ValueError("drawing_node_unsupported")
        if kind in _POLICIES:
            for name, (input_type, _, _) in _POLICIES[kind][2].items():
                if input_type == "FLOAT" and type(node["inputs"].get(name)) is int:
                    node["inputs"][name] = float(node["inputs"][name])
        if kind in {"SaveImage", "WD14Tagger|pysssss"}:
            outputs.append(node_id)
        if kind == "SaveImage" and node["inputs"].get("filename_prefix") != f"al1s-drawing/{prompt_id}/output":
            raise ValueError("drawing_output_invalid")
        if kind in _MANAGED_CLASSES:
            if "al1s_prompt_id" in node["inputs"]:
                raise ValueError("drawing_input_unknown")
            node["class_type"] = _MANAGED_CLASSES[kind]
            node["inputs"]["al1s_prompt_id"] = str(prompt_id)
    if len(outputs) != 1:
        raise ValueError("drawing_output_invalid")
    return ExecutionGraph(result, _hash(result, limit=MAX_GRAPH_BYTES + MAX_IMAGE_DATA),
                          OutputBinding(node_id=outputs[0]))
