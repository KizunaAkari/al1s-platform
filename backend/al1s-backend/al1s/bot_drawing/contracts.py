from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from al1s.meme_workshop.contracts import CommandSource

PresetKind = Literal["txt2img", "image_edit"]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PortBinding(_Contract):
    node_id: str = Field(min_length=1, max_length=80)
    input_name: str = Field(min_length=1, max_length=80)


class OutputBinding(_Contract):
    node_id: str = Field(min_length=1, max_length=80)
    output_index: int = Field(default=0, ge=0, le=0)


class InputBindings(_Contract):
    positive: PortBinding
    negative: PortBinding | None = None
    positive_prefix: str = Field(default="", max_length=24000)
    negative_prefix: str = Field(default="", max_length=24000)
    image: PortBinding | None = None
    seed: PortBinding | None = None
    output: OutputBinding


class InputCapability(_Contract):
    kind: str = Field(min_length=1, max_length=80)
    required: bool = True
    bind: Literal["text", "image", "seed"] | None = None
    asset_kind: Literal["checkpoints", "loras", "vae", "text_encoders", "diffusion_models",
                        "upscale_models", "controlnet", "clip_vision"] | None = None
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None


class NodeCapability(_Contract):
    inputs: dict[str, InputCapability]
    outputs: tuple[str, ...] = ()
    image_output: bool = False
    owned_output_input: str | None = None


class WorkflowCapabilities(_Contract):
    adapter_version: str = Field(min_length=1, max_length=80)
    nodes: dict[str, NodeCapability]


class PresetDraft(_Contract):
    name: str = Field(min_length=1, max_length=32)
    kind: PresetKind
    graph: dict[str, Any]
    bindings: InputBindings


class PromptIntent(_Contract):
    mode: Literal["chat", "wish", "direct", "clarify"]
    positive: str = Field(default="", max_length=2000)
    negative: str = Field(default="", max_length=2000)
    lora_keys: tuple[str, ...] = Field(default=(), max_length=100)


class WorkflowChoice(_Contract):
    id: UUID
    number: int = Field(ge=1)
    kind: PresetKind
    revision: int = Field(ge=1)
    enabled: bool
    validation_state: Literal["unchecked", "missing", "needs_preview", "ready"]
    lora_keys: tuple[str, ...] = Field(default=(), max_length=100)


class WorkflowCatalog(_Contract):
    workflows: tuple[WorkflowChoice, ...] = Field(max_length=20)
    default_text_id: UUID | None = None
    default_edit_id: UUID | None = None

    @model_validator(mode="after")
    def unique_choices(self):
        ids = [item.id for item in self.workflows]
        numbers = [item.number for item in self.workflows]
        if len(ids) != len(set(ids)) or len(numbers) != len(set(numbers)):
            raise ValueError("drawing_catalog_duplicate")
        return self


class RouteDecision(_Contract):
    status: Literal["selected", "ambiguous", "selection_unavailable", "kind_mismatch", "default_unavailable"]
    workflow: WorkflowChoice | None = None
    reason: Literal["manual", "lora", "default", "lora_unmatched"] | None = None
    ignored_lora_keys: tuple[str, ...] = ()
    candidate_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class DrawingCommand:
    source: CommandSource
    self_id: str
    operation: Literal["workflow_draw", "workflow", "tag", "p2p", "online_draw"]
    prompt: str
    workflow_number: int | None
    image_reference: str | None
    reference_message_id: str | None
    timestamp: int
    mentioned: bool
    requires_reply_verification: bool


@dataclass(frozen=True)
class AssetDependency:
    node_id: str
    input_name: str
    kind: str
    name: str


@dataclass(frozen=True)
class ValidatedWorkflow:
    kind: PresetKind
    source_graph: dict[str, Any]
    graph: dict[str, Any]
    bindings: InputBindings
    graph_hash: str
    execution_template_hash: str
    adapter_version: str
    manifest: tuple[AssetDependency, ...]
    output_prefix: PortBinding
    snapshot_hash: str
    source_bindings: InputBindings | None = None


@dataclass(frozen=True)
class ExecutionGraph:
    graph: dict[str, Any]
    graph_hash: str
    output: OutputBinding
