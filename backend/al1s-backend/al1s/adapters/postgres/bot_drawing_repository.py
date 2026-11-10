from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, exists, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.bot_drawing_models import (
    BotDrawingGroupModeRow, BotDrawingGroupPresetRow, BotDrawingLoraRouteRow, BotDrawingPresetRow, BotDrawingRevisionRow,
    BotDrawingScopeRow, BotDrawingSelectionRow, BotDrawingSettingsRow, BotDrawingTaskRow,
)
from al1s.adapters.postgres.bot_drawing_prompt import DrawingPromptRepository
from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import MemeMutationRow, MemeSettingsRow
from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.bot_drawing.contracts import (
    AssetDependency, InputBindings, PortBinding, PresetKind, PromptIntent, RouteDecision, ValidatedWorkflow,
    WorkflowCatalog, WorkflowChoice,
)
from al1s.bot_drawing.routing import normalize_lora_key, select_workflow
from al1s.bot_drawing.moderation import AuditDecision, MODEL_REVISION, POLICY_VERSION, PREVIEW_ALLOW_POLICIES
from al1s.bot_drawing.workflow import ensure_workflow_intact
from al1s.meme_workshop.contracts import CommandSource


@dataclass(frozen=True)
class DrawingSettings:
    enabled: bool
    keep_drawing_warm: bool
    default_text_preset_id: UUID | None
    default_edit_preset_id: UUID | None
    row_version: int
    applied_version: int
    allow_private: bool = False
    event_ws_url: str | None = None
    comfy_base_url: str = "http://host.docker.internal:8188"
    groups: tuple[str, ...] = ()
    default_online_preset_id: UUID | None = None
    resource_applied_version: int = 0

    def json_dict(self) -> dict:
        values = asdict(self)
        for key in ("default_text_preset_id", "default_edit_preset_id", "default_online_preset_id"):
            if values[key] is not None:
                values[key] = str(values[key])
        return values

    @classmethod
    def from_json(cls, data: dict):
        values = dict(data)
        for key in ("default_text_preset_id", "default_edit_preset_id", "default_online_preset_id"):
            if values.get(key) is not None:
                values[key] = UUID(values[key])
        values["groups"] = tuple(values.get("groups", ()))
        return cls(**values)


def _settings(row: BotDrawingSettingsRow, groups=()) -> DrawingSettings:
    return DrawingSettings(row.enabled, row.keep_drawing_warm, row.default_text_preset_id,
                           row.default_edit_preset_id, row.row_version, row.applied_version,
                           row.allow_private, row.event_ws_url, row.comfy_base_url, tuple(groups),
                           row.default_online_preset_id, row.resource_applied_version)


@dataclass(frozen=True)
class DrawingPreset:
    id: UUID
    number: int
    revision: int
    row_version: int
    enabled: bool
    kind: str

    def json_dict(self) -> dict:
        return {**asdict(self), "id": str(self.id)}

    @classmethod
    def from_json(cls, data: dict):
        return cls(**{**data, "id": UUID(data["id"])})


@dataclass(frozen=True)
class PreparedRoute:
    service_id: UUID
    source: tuple[str, str, str, str]
    ai_version: int | None
    drawing_version: int
    chat_profile_id: UUID | None
    group_mode: str
    selection_id: UUID | None
    preset_row_version: int | None
    dependency_signature: str | None
    intent: PromptIntent
    kind: PresetKind
    force_default: bool
    decision: RouteDecision
    workflow_number: int | None = None


@dataclass(frozen=True)
class PreparedTag:
    service_id: UUID
    source: tuple[str, str, str, str]
    ai_version: int | None
    drawing_version: int
    group_mode: str


@dataclass(frozen=True)
class ClaimedTask:
    id: UUID
    service_id: UUID
    prompt_id: UUID
    operation: str
    status: str
    row_version: int


_TRANSITIONS = {
    "queued": {"cancelled", "expired", "failed"},
    "submitting": {"running", "succeeded", "failed", "cancelling", "cancelled", "reconciling"},
    "running": {"succeeded", "failed", "cancelled", "cancelling", "reconciling"},
    "cancelling": {"cancelled", "succeeded", "failed", "reconciling"},
    "reconciling": {"running", "succeeded", "failed", "cancelled", "cancelling"},
}
_TRANSITION_FIELDS = {"next_check_at", "finished_at", "error_code", "output_reference",
                      "output_sha256", "output_bytes", "tag_result"}
MAX_MEDIA_BYTES = 1024 ** 3
_UNSET = object()
ONLINE_ENDPOINT = "openai-images"


def _input_image_for_flow(media_present: bool, audit_status: str, generation_status: str) -> bool | None:
    if media_present or audit_status in {"allow", "block", "unknown"}:
        return True
    if generation_status in {"succeeded", "failed", "expired", "cancelled"}:
        return None
    return False


def _valid_input(task_id, reference, checksum, size):
    return bool(isinstance(task_id, UUID) and isinstance(reference, str)
        and re.fullmatch(rf"al1s-drawing/{task_id}/input\.(?:png|jpeg|webp)", reference)
        and isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum)
        and type(size) is int and 0 < size <= 8 * 1024 * 1024)


def _dependency_signature(value: dict) -> str:
    return fingerprint({key: value[key] for key in ("adapter", "nodes", "assets")})


def _preview_visible(origin, reference, _expires, mode, audit, audit_hash, output_hash, model, policy):
    if origin not in {"admin_preview", "qq"} or not reference or not output_hash:
        return False
    return bool(mode == "r18" or (audit == "allow" and audit_hash == output_hash
                                 and model == MODEL_REVISION and policy in PREVIEW_ALLOW_POLICIES))


class BotDrawingRepository(DrawingPromptRepository, MemeRepositoryBase):
    def __init__(self, sessions, *, cipher=None):
        super().__init__(sessions)
        self.cipher = cipher

    @staticmethod
    def _service(session: Session, service_id: UUID) -> BotServiceRow:
        service = session.get(BotServiceRow, service_id)
        if service is None or service.deleted_at is not None or service.kind != "onebot_gateway":
            raise ValueError("qq_bot_not_found")
        return service

    @staticmethod
    def _allowed(
        session: Session, service_id: UUID, source: CommandSource, *, require_applied: bool = True
    ) -> None:
        service = BotDrawingRepository._service(session, service_id)
        drawing = session.get(BotDrawingSettingsRow, service_id)
        if (not service.enabled or drawing is None or not drawing.enabled
                or drawing.applied_version == 0
                or (require_applied and drawing.applied_version != drawing.row_version)):
            raise ValueError("drawing_scope_denied")
        if source.scope_kind == "qq_group":
            allowed = session.get(BotDrawingScopeRow, (service_id, source.target_id)) is not None
        else:
            allowed = (source.scope_kind == "qq_private" and drawing.allow_private
                       and source.sender_id == source.target_id)
        if not allowed:
            raise ValueError("drawing_scope_denied")

    @staticmethod
    def _reserve_media(session: Session, service_id: UUID, amount: int) -> None:
        used = session.scalar(select(func.coalesce(func.sum(BotDrawingSettingsRow.media_bytes), 0)))
        if used + amount > MAX_MEDIA_BYTES or (amount == 0 and used >= MAX_MEDIA_BYTES):
            raise ValueError("drawing_media_full")
        if amount == 0:
            return
        settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
        if settings is None:
            raise ValueError("drawing_settings_unavailable")
        settings.media_bytes += amount

    @staticmethod
    def _check_queue_capacity(session: Session, endpoint_key: str) -> None:
        active = ("queued", "submitting", "running", "cancelling", "reconciling")
        condition = BotDrawingTaskRow.generation_status.in_(active)
        if endpoint_key != ONLINE_ENDPOINT:
            condition = or_(condition, BotDrawingTaskRow.cleanup_status.in_(("deferred", "failed")))
        candidates = select(BotDrawingTaskRow.id).where(
            BotDrawingTaskRow.endpoint_key == endpoint_key, condition).limit(6).subquery()
        if session.scalar(select(func.count()).select_from(candidates)) >= 6:
            raise ValueError("drawing_queue_full")

    def get_settings(self, service_id: UUID) -> dict:
        with self.sessions() as session:
            self._service(session, service_id)
            row = session.get(BotDrawingSettingsRow, service_id)
            if row is None:
                return {"service_id": str(service_id), "enabled": True, "keep_drawing_warm": False,
                    "default_text_preset_id": None, "default_edit_preset_id": None,
                    "default_online_preset_id": None,
                    "resource_applied_version": 0,
                    "row_version": 0, "applied_version": 0, "resource_configured": False,
                    "allow_private": False, "event_ws_url": None,
                    "comfy_base_url": "http://host.docker.internal:8188", "groups": []}
            groups = session.scalars(select(BotDrawingScopeRow.group_id)
                .where(BotDrawingScopeRow.service_id == service_id)
                .order_by(BotDrawingScopeRow.group_id).limit(100)).all()
            return {"service_id": str(service_id), **_settings(row, groups).json_dict(),
                    "resource_configured": row.resource_secret_id is not None}

    def list_group_modes(self, service_id: UUID) -> list[dict]:
        with self.sessions() as session:
            self._service(session, service_id)
            rows = session.execute(select(BotDrawingScopeRow.group_id, BotDrawingGroupModeRow.mode,
                BotDrawingGroupModeRow.restrict_workflows,
                BotDrawingGroupModeRow.row_version).outerjoin(BotDrawingGroupModeRow, and_(
                    BotDrawingGroupModeRow.service_id == BotDrawingScopeRow.service_id,
                    BotDrawingGroupModeRow.group_id == BotDrawingScopeRow.group_id))
                .where(BotDrawingScopeRow.service_id == service_id)
                .order_by(BotDrawingScopeRow.group_id).limit(100)).all()
            allowed = session.execute(select(BotDrawingGroupPresetRow.group_id, BotDrawingGroupPresetRow.preset_id)
                .where(BotDrawingGroupPresetRow.service_id == service_id)
                .order_by(BotDrawingGroupPresetRow.group_id, BotDrawingGroupPresetRow.preset_id)).all()
            by_group = {}
            for group, preset in allowed:
                by_group.setdefault(group, []).append(str(preset))
            return [{"group_id": group, "mode": mode or "green", "restrict_workflows": restricted or False,
                     "allowed_preset_ids": by_group.get(group, []), "row_version": version or 0}
                    for group, mode, restricted, version in rows]

    def list_presets(self, service_id: UUID, source: CommandSource | None = None) -> list[dict]:
        with self.sessions() as session:
            if source is None:
                self._service(session, service_id)
            else:
                self._allowed(session, service_id, source)
            restricted = (session.get(BotDrawingGroupModeRow, (service_id, source.target_id))
                          if source and source.scope_kind == "qq_group" else None)
            query = (select(BotDrawingPresetRow.id, BotDrawingPresetRow.number,
                BotDrawingPresetRow.name, BotDrawingPresetRow.kind, BotDrawingPresetRow.engine, BotDrawingPresetRow.enabled,
                BotDrawingPresetRow.current_revision, BotDrawingPresetRow.row_version,
                BotDrawingRevisionRow.validation_state, BotDrawingLoraRouteRow.normalized_key)
                .join(BotDrawingRevisionRow, and_(BotDrawingRevisionRow.preset_id == BotDrawingPresetRow.id,
                    BotDrawingRevisionRow.revision == BotDrawingPresetRow.current_revision))
                .outerjoin(BotDrawingLoraRouteRow, and_(BotDrawingLoraRouteRow.preset_id == BotDrawingPresetRow.id,
                    BotDrawingLoraRouteRow.revision == BotDrawingPresetRow.current_revision))
                .where(BotDrawingPresetRow.service_id == service_id))
            if restricted and restricted.restrict_workflows:
                allowed = select(BotDrawingGroupPresetRow.preset_id).where(
                    BotDrawingGroupPresetRow.service_id == service_id,
                    BotDrawingGroupPresetRow.group_id == source.target_id)
                query = query.where(BotDrawingPresetRow.id.in_(allowed))
            rows = session.execute(query.order_by(BotDrawingPresetRow.number,
                BotDrawingLoraRouteRow.normalized_key).limit(2021)).all()
            if len(rows) > 2020:
                raise ValueError("drawing_catalog_invalid")
            result = {}
            for identity, number, name, kind, engine, enabled, revision, version, validation, alias in rows:
                item = result.setdefault(identity, {"id": str(identity), "number": number, "name": name,
                    "kind": kind, "engine": engine, "enabled": enabled, "revision": revision, "row_version": version,
                    "validation_state": validation, "lora_keys": []})
                if alias is not None:
                    item["lora_keys"].append(alias)
            if len(result) > 20 or any(len(item["lora_keys"]) > 100 for item in result.values()):
                raise ValueError("drawing_catalog_invalid")
            return list(result.values())

    def list_scope_presets(self, service_id: UUID, source: CommandSource) -> list[dict]:
        return self.list_presets(service_id, source)

    def get_preset(self, service_id: UUID, preset_id: UUID) -> dict:
        with self.sessions() as session:
            self._service(session, service_id)
            row = session.execute(select(BotDrawingPresetRow, BotDrawingRevisionRow)
                .join(BotDrawingRevisionRow, and_(BotDrawingRevisionRow.preset_id == BotDrawingPresetRow.id,
                    BotDrawingRevisionRow.revision == BotDrawingPresetRow.current_revision))
                .where(BotDrawingPresetRow.service_id == service_id, BotDrawingPresetRow.id == preset_id)).one_or_none()
            if row is None:
                raise ValueError("drawing_preset_not_found")
            preset, revision = row
            if preset.engine == "openai":
                return {"id": str(preset.id), "number": preset.number, "name": preset.name,
                    "kind": preset.kind, "engine": preset.engine, "enabled": preset.enabled,
                    "row_version": preset.row_version, "revision": preset.current_revision,
                    "validation_state": revision.validation_state, "model_id": revision.online_model_id,
                    "base_url": revision.online_base_url, "network_mode": revision.online_network_mode,
                    "size": revision.online_size, "quality": revision.online_quality,
                    "use_prompt_model": revision.online_prompt_model,
                    "secret_configured": revision.online_secret_id is not None}
            routes = session.execute(select(BotDrawingLoraRouteRow.normalized_key, BotDrawingLoraRouteRow.actual_lora_name)
                .where(BotDrawingLoraRouteRow.service_id == service_id, BotDrawingLoraRouteRow.preset_id == preset_id,
                       BotDrawingLoraRouteRow.revision == preset.current_revision)
                .order_by(BotDrawingLoraRouteRow.normalized_key).limit(100)).all()
            return {"id": str(preset.id), "number": preset.number, "name": preset.name, "kind": preset.kind, "engine": preset.engine,
                "enabled": preset.enabled, "row_version": preset.row_version, "revision": preset.current_revision,
                "validation_state": revision.validation_state, "graph": revision.source_graph,
                "bindings": revision.source_bindings, "aliases": dict(routes)}

    def get_workflow(self, service_id: UUID, preset_id: UUID, revision_number: int | None = None) -> ValidatedWorkflow:
        with self.sessions() as session:
            preset = session.get(BotDrawingPresetRow, preset_id)
            if preset is None or preset.service_id != service_id:
                raise ValueError("drawing_preset_not_found")
            number = preset.current_revision if revision_number is None else revision_number
            row = session.get(BotDrawingRevisionRow, (preset_id, number))
            if row is None or row.service_id != service_id:
                raise ValueError("drawing_preset_not_found")
            bindings = InputBindings.model_validate(row.bindings)
            workflow = ValidatedWorkflow(preset.kind, row.source_graph, row.execution_template, bindings,
                row.graph_hash, row.execution_template_hash, row.adapter_version,
                tuple(AssetDependency(**item) for item in row.manifest),
                PortBinding(node_id=bindings.output.node_id, input_name="filename_prefix"), row.snapshot_hash,
                InputBindings.model_validate(row.source_bindings))
            ensure_workflow_intact(workflow)
            return workflow

    def get_dependencies(self, service_id: UUID, preset_id: UUID, revision_number: int, *, for_preview=False) -> dict:
        with self.sessions() as session:
            row = session.get(BotDrawingRevisionRow, (preset_id, revision_number))
            states = {"ready", "needs_preview"} if for_preview else {"ready"}
            if (row is None or row.service_id != service_id or row.validation_state not in states
                    or not isinstance(row.dependency_manifest, dict)):
                raise ValueError("drawing_dependency_unverified")
            try:
                valid = _dependency_signature(row.dependency_manifest) == row.dependency_signature
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("drawing_dependency_unverified")
            return row.dependency_manifest

    def check_receipt(self, service_id: UUID, preset_id: UUID, expected_version: int, operation_id: str) -> str | None:
        key = self.mutation_key("drawing_check", service_id, operation_id)
        digest = fingerprint({"preset_id": str(preset_id), "expected_version": expected_version})
        with self.sessions() as session:
            replay = self.replay(session, key, digest)
            return replay["validation_state"] if replay is not None else None

    def record_check(self, service_id: UUID, preset_id: UUID, revision_number: int,
                     expected_version: int, dependencies: dict, *, operation_id: str | None = None) -> str:
        if (not isinstance(dependencies, dict) or set(dependencies) != {"adapter", "boot_id", "nodes", "assets"}
                or not isinstance(dependencies["adapter"], str) or not isinstance(dependencies["boot_id"], str)
                or not 1 <= len(dependencies["boot_id"]) <= 64 or not isinstance(dependencies["nodes"], list)
                or not isinstance(dependencies["assets"], list) or len(dependencies["nodes"]) > 100
                or len(dependencies["assets"]) > 100):
            raise ValueError("drawing_dependency_invalid")
        try:
            if len(json.dumps(dependencies, ensure_ascii=False, allow_nan=False).encode()) > 256 * 1024:
                raise ValueError("drawing_dependency_invalid")
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("drawing_dependency_invalid") from error
        nodes, assets = dependencies["nodes"], dependencies["assets"]
        if (any(not isinstance(item, dict) or set(item) != {"name", "sha256"}
                or not isinstance(item["name"], str) or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in nodes)
                or any(not isinstance(item, dict) or set(item) != {"kind", "name", "size", "sha256"}
                       or not isinstance(item["kind"], str) or not isinstance(item["name"], str)
                       or type(item["size"]) is not int or item["size"] < 0
                       or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                       for item in assets)):
            raise ValueError("drawing_dependency_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            key = self.mutation_key("drawing_check", service_id, operation_id) if operation_id else None
            digest = fingerprint({"preset_id": str(preset_id), "expected_version": expected_version})
            replay = self.replay(session, key, digest) if key else None
            if replay is not None:
                return replay["validation_state"]
            preset = session.get(BotDrawingPresetRow, preset_id, with_for_update=True)
            if (preset is None or preset.service_id != service_id or preset.row_version != expected_version
                    or preset.current_revision != revision_number):
                raise ValueError("drawing_preset_conflict")
            revision = session.get(BotDrawingRevisionRow, (preset_id, revision_number), with_for_update=True)
            if revision is None:
                raise ValueError("drawing_preset_conflict")
            expected_nodes = {item["class_type"] for item in revision.execution_template.values()}
            expected_assets = {(item["kind"], item["name"]) for item in revision.manifest}
            actual_nodes = {item["name"] for item in nodes}
            actual_assets = {(item["kind"], item["name"]) for item in assets}
            if (dependencies["adapter"] != revision.adapter_version or actual_nodes != expected_nodes
                    or actual_assets != expected_assets or len(nodes) != len(actual_nodes)
                    or len(assets) != len(actual_assets)):
                raise ValueError("drawing_dependency_mismatch")
            signature = _dependency_signature(dependencies)
            if revision.dependency_signature != signature or revision.validation_state != "ready":
                revision.validation_state = "needs_preview"
                revision.measured_peak_bytes = None
            revision.dependency_manifest = dependencies
            revision.dependency_signature = signature
            revision.validation_code = None
            revision.validated_at = datetime.now(UTC)
            if key:
                self.add_mutation(session, key, digest, {"validation_state": revision.validation_state},
                                  bot_service_id=service_id)
            return revision.validation_state

    def save_settings(self, service_id: UUID, *, enabled: bool, keep_drawing_warm: bool,
                      default_text_preset_id: UUID | None, default_edit_preset_id: UUID | None,
                      expected_version: int, operation_id: str, allow_private: bool | None = None,
                      event_ws_url: str | None = None, comfy_base_url: str | None = None,
                      groups: list[str] | None = None, default_online_preset_id=_UNSET) -> DrawingSettings:
        if type(enabled) is not bool or type(keep_drawing_warm) is not bool:
            raise ValueError("drawing_settings_invalid")
        if keep_drawing_warm:
            raise ValueError("drawing_legacy_warm_unsupported")
        if allow_private is not None and type(allow_private) is not bool:
            raise ValueError("drawing_settings_invalid")
        if groups is not None and (not isinstance(groups, list) or len(groups) > 100
                or any(not isinstance(group, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", group) for group in groups)
                or len(set(groups)) != len(groups)):
            raise ValueError("drawing_groups_invalid")
        if event_ws_url:
            parsed = urlsplit(event_ws_url)
            if (len(event_ws_url) > 1024 or parsed.scheme not in {"ws", "wss"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("drawing_event_url_invalid")
        if comfy_base_url is not None:
            from al1s.bot_ai.managed.smart_contract import local_endpoint
            comfy_base_url = local_endpoint(comfy_base_url)
        payload = {"enabled": enabled, "keep_drawing_warm": keep_drawing_warm,
                   "default_text_preset_id": str(default_text_preset_id) if default_text_preset_id else None,
                   "default_edit_preset_id": str(default_edit_preset_id) if default_edit_preset_id else None,
                   "expected_version": expected_version}
        if default_online_preset_id is not _UNSET:
            payload["default_online_preset_id"] = str(default_online_preset_id) if default_online_preset_id else None
        for name, value in (("allow_private", allow_private), ("event_ws_url", event_ws_url),
                            ("comfy_base_url", comfy_base_url), ("groups", groups)):
            if value is not None:
                payload[name] = value
        key = self.mutation_key("drawing_settings", service_id, operation_id)
        digest = fingerprint(payload)
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return DrawingSettings.from_json(replay)
            self._service(session, service_id)
            row = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            if (row is None and expected_version != 0) or (row is not None and row.row_version != expected_version):
                raise ValueError("drawing_settings_conflict")
            ids = {value for value in (default_text_preset_id, default_edit_preset_id) if value is not None}
            presets = {item.id: item for item in session.scalars(select(BotDrawingPresetRow).where(
                BotDrawingPresetRow.service_id == service_id, BotDrawingPresetRow.id.in_(ids))).all()} if ids else {}
            for value, kind in ((default_text_preset_id, "txt2img"), (default_edit_preset_id, "image_edit")):
                if value is not None and (value not in presets or presets[value].kind != kind):
                    raise ValueError("drawing_default_invalid")
            if default_online_preset_id is not _UNSET and default_online_preset_id is not None:
                online = session.get(BotDrawingPresetRow, default_online_preset_id)
                if online is None or online.service_id != service_id or online.engine != "openai":
                    raise ValueError("drawing_default_invalid")
            if row is None:
                row = BotDrawingSettingsRow(service_id=service_id, row_version=1, applied_version=0,
                                           next_workflow_number=1)
                session.add(row)
            else:
                row.row_version += 1
            row.enabled, row.keep_drawing_warm = enabled, keep_drawing_warm
            row.default_text_preset_id, row.default_edit_preset_id = default_text_preset_id, default_edit_preset_id
            if default_online_preset_id is not _UNSET:
                row.default_online_preset_id = default_online_preset_id
            if allow_private is not None:
                row.allow_private = allow_private
            if event_ws_url is not None:
                row.event_ws_url = event_ws_url or None
            if comfy_base_url is not None:
                row.comfy_base_url = comfy_base_url
            session.flush()
            previous = set(session.scalars(select(BotDrawingScopeRow.group_id)
                .where(BotDrawingScopeRow.service_id == service_id)).all())
            desired = set(groups) if groups is not None else previous
            removed = previous - desired
            if removed:
                session.execute(delete(BotDrawingScopeRow).where(BotDrawingScopeRow.service_id == service_id,
                    BotDrawingScopeRow.group_id.in_(removed)))
            session.add_all([BotDrawingScopeRow(service_id=service_id, group_id=group)
                             for group in sorted(desired - previous)])
            session.flush()
            result = _settings(row, sorted(desired))
            self.add_mutation(session, key, digest, result.json_dict(), bot_service_id=service_id)
            return result

    def mark_applied(self, service_id: UUID, version: int) -> bool:
        with self.sessions.begin() as session:
            result = session.execute(update(BotDrawingSettingsRow).where(
                BotDrawingSettingsRow.service_id == service_id, BotDrawingSettingsRow.row_version == version)
                .values(applied_version=version))
            return result.rowcount == 1

    def mark_resource_applied(self, service_id: UUID, version: int) -> bool:
        with self.sessions.begin() as session:
            result = session.execute(update(BotDrawingSettingsRow).where(
                BotDrawingSettingsRow.service_id == service_id, BotDrawingSettingsRow.row_version == version)
                .values(resource_applied_version=version))
            return result.rowcount == 1

    def resource_credentials(self, service_id: UUID) -> str | None:
        with self.sessions() as session:
            row = session.execute(select(EncryptedSecretRow.ciphertext, EncryptedSecretRow.key_id)
                .join(BotDrawingSettingsRow, BotDrawingSettingsRow.resource_secret_id == EncryptedSecretRow.id)
                .where(BotDrawingSettingsRow.service_id == service_id)).one_or_none()
            if row is None or self.cipher is None:
                return None
            return self.cipher.decrypt(row.ciphertext, row.key_id)

    def qq_connections(self) -> list[QqConnection]:
        with self.sessions() as session:
            rows = session.execute(select(BotDrawingSettingsRow, BotConfigVersionRow,
                    MemeSettingsRow.cooldown_seconds, EncryptedSecretRow.ciphertext, EncryptedSecretRow.key_id)
                .join(BotServiceRow, BotServiceRow.id == BotDrawingSettingsRow.service_id)
                .join(BotConfigVersionRow, BotConfigVersionRow.id == BotServiceRow.applied_config_version_id)
                .outerjoin(MemeSettingsRow, MemeSettingsRow.bot_service_id == BotServiceRow.id)
                .outerjoin(EncryptedSecretRow, EncryptedSecretRow.id == BotConfigVersionRow.secret_id)
                .where(BotDrawingSettingsRow.enabled, BotDrawingSettingsRow.event_ws_url.is_not(None),
                    BotDrawingSettingsRow.row_version == BotDrawingSettingsRow.applied_version,
                    BotDrawingSettingsRow.applied_version > 0, BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None), BotServiceRow.kind == "onebot_gateway",
                    BotConfigVersionRow.service_id == BotServiceRow.id)).all()
            result = []
            for settings, config, cooldown, ciphertext, key_id in rows:
                base = config.settings.get("ONEBOT_BASE_URL")
                if isinstance(base, str) and settings.event_ws_url:
                    result.append(QqConnection(settings.service_id, config.id, settings.row_version,
                        settings.event_ws_url, base, cooldown or 5, ciphertext, key_id))
            return result

    def next_settings(self, after: UUID | None = None) -> tuple[UUID, int] | None:
        with self.sessions() as session:
            query = select(BotDrawingSettingsRow.service_id, BotDrawingSettingsRow.row_version).join(
                BotServiceRow, BotServiceRow.id == BotDrawingSettingsRow.service_id).where(
                BotServiceRow.deleted_at.is_(None)).order_by(BotDrawingSettingsRow.service_id).limit(1)
            if after is not None:
                query = query.where(BotDrawingSettingsRow.service_id > after)
            row = session.execute(query).one_or_none()
            return (row[0], row[1]) if row else None

    def bind_resource_token(self, service_id: UUID, token: str) -> None:
        if (self.cipher is None or not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
            raise ValueError("drawing_resource_token_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            created = settings is None
            if settings is None:
                settings = BotDrawingSettingsRow(service_id=service_id, enabled=True, keep_drawing_warm=False,
                    next_workflow_number=1, row_version=1, applied_version=0)
                session.add(settings)
            old_id = settings.resource_secret_id
            old = session.get(EncryptedSecretRow, old_id) if old_id else None
            if old is not None and self.cipher.decrypt(old.ciphertext, old.key_id) == token:
                return
            secret = EncryptedSecretRow(id=uuid4(), purpose=f"bot-drawing:{service_id}:resource",
                ciphertext=self.cipher.encrypt(token), key_id=self.cipher.key_id, row_version=1)
            session.add(secret)
            session.flush()
            settings.resource_secret_id = secret.id
            if not created:
                settings.row_version += 1
            if old_id is not None:
                session.flush()
                from al1s.adapters.postgres.secret_repository import PostgresSecretRepository
                PostgresSecretRepository(session).delete_if_unreferenced(old_id)

    def save_preset(self, service_id: UUID, name: str, workflow: ValidatedWorkflow,
                    aliases: dict[str, str], *, enabled: bool, preset_id: UUID | None,
                    expected_version: int, operation_id: str) -> DrawingPreset:
        name = unicodedata.normalize("NFKC", name).strip()
        if (not name or len(name) > 32 or any(not char.isprintable() for char in name)
                or type(enabled) is not bool or len(aliases) > 100):
            raise ValueError("drawing_preset_invalid")
        ensure_workflow_intact(workflow)
        actual_loras = {item.name for item in workflow.manifest if item.kind == "loras"}
        normalized = {}
        for alias, actual in aliases.items():
            key = normalize_lora_key(alias)
            if len(key) > 128 or key in normalized or actual not in actual_loras:
                raise ValueError("drawing_lora_invalid")
            normalized[key] = actual
        key = self.mutation_key("drawing_preset", service_id, operation_id)
        digest = fingerprint({"name": name, "snapshot": workflow.snapshot_hash, "aliases": normalized,
            "enabled": enabled, "preset_id": str(preset_id) if preset_id else None,
            "expected_version": expected_version})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return DrawingPreset.from_json(replay)
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            if settings is None:
                settings = BotDrawingSettingsRow(service_id=service_id, enabled=True, keep_drawing_warm=False,
                    next_workflow_number=1, row_version=1, applied_version=0)
                session.add(settings)
                session.flush()
            row = session.get(BotDrawingPresetRow, preset_id, with_for_update=True) if preset_id else None
            if preset_id is None:
                if expected_version != 0:
                    raise ValueError("drawing_preset_conflict")
                count = session.scalar(select(func.count()).select_from(BotDrawingPresetRow)
                                       .where(BotDrawingPresetRow.service_id == service_id))
                if count >= 20:
                    raise ValueError("drawing_preset_limit")
                row = BotDrawingPresetRow(id=uuid4(), service_id=service_id,
                    number=settings.next_workflow_number, name=name, normalized_name=name.casefold(),
                    kind=workflow.kind, enabled=enabled, current_revision=1, row_version=1)
                settings.next_workflow_number += 1
                session.add(row)
            else:
                if row is None or row.service_id != service_id or row.row_version != expected_version:
                    raise ValueError("drawing_preset_conflict")
                if row.kind != workflow.kind:
                    raise ValueError("drawing_preset_kind_conflict")
                row.name, row.normalized_name, row.enabled = name, name.casefold(), enabled
                row.current_revision, row.row_version = row.current_revision + 1, row.row_version + 1
                row.updated_at = datetime.now(UTC)
            session.flush()
            revision = BotDrawingRevisionRow(preset_id=row.id, revision=row.current_revision,
                service_id=service_id, source_graph=workflow.source_graph, execution_template=workflow.graph,
                source_bindings=(workflow.source_bindings or workflow.bindings).model_dump(mode="json"),
                bindings=workflow.bindings.model_dump(mode="json"), manifest=[asdict(item) for item in workflow.manifest],
                graph_hash=workflow.graph_hash, execution_template_hash=workflow.execution_template_hash,
                snapshot_hash=workflow.snapshot_hash,
                adapter_version=workflow.adapter_version, validation_state="unchecked")
            session.add(revision)
            session.flush()
            session.add_all([BotDrawingLoraRouteRow(service_id=service_id, preset_id=row.id,
                revision=row.current_revision, normalized_key=alias, actual_lora_name=actual)
                for alias, actual in normalized.items()])
            result = DrawingPreset(row.id, row.number, row.current_revision, row.row_version, row.enabled, row.kind)
            self.add_mutation(session, key, digest, result.json_dict(), bot_service_id=service_id)
            return result

    def save_online_preset(self, service_id, data, *, preset_id, expected_version, operation_id):
        if self.cipher is None:
            raise ValueError("drawing_online_unconfigured")
        if data.network_mode == "proxy":
            from al1s.bot_ai.configuration import existing_https_proxy
            existing_https_proxy()
        name = unicodedata.normalize("NFKC", data.name).strip()
        if not name or len(name) > 32 or any(not char.isprintable() for char in name):
            raise ValueError("drawing_preset_invalid")
        serialized = data.model_dump(mode="json")
        serialized["secret_input"] = (self.cipher.fingerprint(data.secret_input.get_secret_value())
                                      if data.secret_input else None)
        key = self.mutation_key("drawing_online_preset", service_id, operation_id)
        omitted = {field for field in ("size", "quality") if field not in data.model_fields_set}
        request = {"version": 2, "preset_id": str(preset_id) if preset_id else None,
                   "expected": expected_version, "data": serialized}
        if preset_id is not None and omitted:
            request["omitted"] = sorted(omitted)
        digest = fingerprint(request)
        legacy = dict(serialized)
        if "size" in omitted:
            legacy["size"] = "1024x1024"
        if "quality" in omitted:
            legacy["quality"] = "medium"
        legacy_digest = fingerprint({"preset_id": str(preset_id) if preset_id else None,
                                     "expected": expected_version, "data": legacy})
        with self.sessions.begin() as session:
            self.lock(session)
            try:
                replay = self.replay(session, key, digest)
            except ValueError as error:
                if str(error) != "idempotency_conflict":
                    raise
                replay = self.replay(session, key, legacy_digest)
            if replay is not None:
                return replay
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            if settings is None:
                raise ValueError("drawing_settings_unavailable")
            duplicate = session.scalar(select(BotDrawingPresetRow.id).where(
                BotDrawingPresetRow.service_id == service_id, BotDrawingPresetRow.normalized_name == name.casefold(),
                BotDrawingPresetRow.id != preset_id if preset_id is not None else True))
            if duplicate is not None:
                raise ValueError("drawing_preset_name_duplicate")
            row = session.get(BotDrawingPresetRow, preset_id, with_for_update=True) if preset_id else None
            if preset_id is None:
                if expected_version != 0:
                    raise ValueError("drawing_preset_conflict")
                count = session.scalar(select(func.count()).select_from(BotDrawingPresetRow)
                                       .where(BotDrawingPresetRow.service_id == service_id)) or 0
                if count >= 20:
                    raise ValueError("drawing_preset_limit")
                row = BotDrawingPresetRow(id=uuid4(), service_id=service_id, number=settings.next_workflow_number,
                    name=name, normalized_name=name.casefold(), kind="both", engine="openai",
                    enabled=data.enabled, current_revision=1, row_version=1)
                settings.next_workflow_number += 1
                session.add(row)
                old = None
            else:
                if (row is None or row.service_id != service_id or row.engine != "openai"
                        or row.row_version != expected_version):
                    raise ValueError("drawing_preset_conflict")
                old = session.get(BotDrawingRevisionRow, (row.id, row.current_revision))
                if old is None:
                    raise ValueError("drawing_preset_not_found")
                row.name, row.normalized_name, row.enabled = name, name.casefold(), data.enabled
                row.current_revision += 1
                row.row_version += 1
                row.updated_at = datetime.now(UTC)
            secret_id = old.online_secret_id if old is not None else None
            if data.clear_secret:
                secret_id = None
            elif data.secret_input:
                secret = EncryptedSecretRow(id=uuid4(), purpose=f"bot-drawing:{service_id}:online",
                    ciphertext=self.cipher.encrypt(data.secret_input.get_secret_value()),
                    key_id=self.cipher.key_id, row_version=1)
                session.add(secret)
                session.flush([secret])
                secret_id = secret.id
            size = old.online_size if old is not None and "size" in omitted else data.size
            quality = old.online_quality if old is not None and "quality" in omitted else data.quality
            snapshot = fingerprint({"model": data.model_id, "base_url": data.base_url.strip(),
                "network_mode": data.network_mode, "size": size, "quality": quality,
                "prompt_model": data.use_prompt_model, "secret_id": str(secret_id) if secret_id else None})
            dependencies = {"adapter": "openai-images", "nodes": [], "assets": []}
            session.flush()
            session.add(BotDrawingRevisionRow(preset_id=row.id, revision=row.current_revision, service_id=service_id,
                manifest=[], snapshot_hash=snapshot, adapter_version="openai-images",
                dependency_manifest=dependencies, dependency_signature=_dependency_signature(dependencies),
                validation_state="ready" if secret_id else "missing", online_model_id=data.model_id,
                online_base_url=data.base_url.strip(), online_network_mode=data.network_mode,
                online_size=size, online_quality=quality, online_prompt_model=data.use_prompt_model,
                online_secret_id=secret_id))
            result = DrawingPreset(row.id, row.number, row.current_revision, row.row_version, row.enabled, row.kind).json_dict()
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return result

    def online_credentials(self, service_id, preset_id, revision):
        with self.sessions() as session:
            row = session.execute(select(BotDrawingRevisionRow, EncryptedSecretRow)
                .join(EncryptedSecretRow, EncryptedSecretRow.id == BotDrawingRevisionRow.online_secret_id)
                .where(BotDrawingRevisionRow.service_id == service_id, BotDrawingRevisionRow.preset_id == preset_id,
                       BotDrawingRevisionRow.revision == revision)).one_or_none()
            if row is None or self.cipher is None:
                raise ValueError("drawing_online_unconfigured")
            preset, secret = row
            return {"model_id": preset.online_model_id, "base_url": preset.online_base_url,
                "network_mode": preset.online_network_mode, "size": preset.online_size,
                "quality": preset.online_quality}, self.cipher.decrypt(secret.ciphertext, secret.key_id)

    def online_task_allowed(self, task_id):
        with self.sessions() as session:
            task = session.get(BotDrawingTaskRow, task_id)
            if task is None or task.endpoint_key != ONLINE_ENDPOINT or task.cancel_requested:
                return False
            preset = session.get(BotDrawingPresetRow, task.preset_id)
            if preset is None or preset.engine != "openai":
                return False
            if task.origin == "admin_preview":
                return True
            if not preset.enabled:
                return False
            source = CommandSource(message_id=task.source_message_id, scope_kind=task.scope_kind,
                                   target_id=task.target_id, sender_id=task.sender_id)
            try:
                self._allowed(session, task.service_id, source, require_applied=False)
            except ValueError:
                return False
            if source.scope_kind == "qq_group":
                group = session.get(BotDrawingGroupModeRow, (task.service_id, source.target_id))
                if group and group.restrict_workflows and session.get(
                        BotDrawingGroupPresetRow, (task.service_id, source.target_id, task.preset_id)) is None:
                    return False
            return True

    def save_group_mode(self, service_id: UUID, group_id: str, mode: str, *,
                        expected_version: int, operation_id: str, restrict_workflows: bool | None = None,
                        allowed_preset_ids: list[UUID] | None = None) -> int:
        if mode not in {"green", "r18"}:
            raise ValueError("drawing_group_mode_invalid")
        if ((restrict_workflows is None) != (allowed_preset_ids is None)
                or restrict_workflows is not None and type(restrict_workflows) is not bool
                or allowed_preset_ids is not None and (len(allowed_preset_ids) > 20
                    or any(not isinstance(item, UUID) for item in allowed_preset_ids)
                    or len(set(allowed_preset_ids)) != len(allowed_preset_ids))):
            raise ValueError("drawing_group_presets_invalid")
        key = self.mutation_key("drawing_group_mode", service_id, operation_id)
        payload = {"group_id": group_id, "mode": mode, "expected_version": expected_version}
        if restrict_workflows is not None:
            payload.update(restrict_workflows=restrict_workflows,
                           allowed_preset_ids=sorted(str(item) for item in allowed_preset_ids))
        digest = fingerprint(payload)
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay["row_version"]
            self._service(session, service_id)
            if session.get(BotDrawingScopeRow, (service_id, group_id)) is None:
                raise ValueError("drawing_scope_denied")
            row = session.get(BotDrawingGroupModeRow, (service_id, group_id), with_for_update=True)
            if (row is None and expected_version != 0) or (row is not None and row.row_version != expected_version):
                raise ValueError("drawing_group_mode_conflict")
            if allowed_preset_ids:
                owned = set(session.scalars(select(BotDrawingPresetRow.id).where(
                    BotDrawingPresetRow.service_id == service_id,
                    BotDrawingPresetRow.id.in_(allowed_preset_ids))).all())
                if owned != set(allowed_preset_ids):
                    raise ValueError("drawing_group_presets_invalid")
            if row is None:
                row = BotDrawingGroupModeRow(service_id=service_id, group_id=group_id, mode=mode, row_version=1)
                session.add(row)
            else:
                row.mode, row.row_version = mode, row.row_version + 1
            if restrict_workflows is not None:
                row.restrict_workflows = restrict_workflows
            session.flush()
            if allowed_preset_ids is not None:
                session.execute(delete(BotDrawingGroupPresetRow).where(
                    BotDrawingGroupPresetRow.service_id == service_id, BotDrawingGroupPresetRow.group_id == group_id))
                session.add_all([BotDrawingGroupPresetRow(service_id=service_id, group_id=group_id, preset_id=preset)
                                 for preset in allowed_preset_ids])
            self.add_mutation(session, key, digest, {"row_version": row.row_version}, bot_service_id=service_id)
            return row.row_version

    def set_selection(self, service_id: UUID, source: CommandSource, number: int) -> UUID | None:
        if type(number) is not int or number < 0:
            raise ValueError("drawing_selection_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            self._allowed(session, service_id, source)
            return self._set_selection_in(session, service_id, source, number)

    @staticmethod
    def _set_selection_in(session: Session, service_id: UUID, source: CommandSource, number: int) -> UUID | None:
        key = (service_id, source.scope_kind, source.target_id, source.sender_id)
        row = session.get(BotDrawingSelectionRow, key, with_for_update=True)
        if number == 0:
            if row is not None:
                session.delete(row)
            return None
        preset = session.scalar(select(BotDrawingPresetRow).join(BotDrawingRevisionRow,
            (BotDrawingRevisionRow.preset_id == BotDrawingPresetRow.id)
            & (BotDrawingRevisionRow.revision == BotDrawingPresetRow.current_revision)).where(
                BotDrawingPresetRow.service_id == service_id, BotDrawingPresetRow.number == number,
                BotDrawingPresetRow.enabled.is_(True), BotDrawingRevisionRow.validation_state == "ready"))
        if preset is None:
            raise ValueError("drawing_selection_unavailable")
        if source.scope_kind == "qq_group":
            group = session.get(BotDrawingGroupModeRow, (service_id, source.target_id))
            if group is not None and group.restrict_workflows and session.get(
                    BotDrawingGroupPresetRow, (service_id, source.target_id, preset.id)) is None:
                raise ValueError("drawing_group_preset_denied")
        if row is None:
            row = BotDrawingSelectionRow(service_id=service_id, scope_kind=source.scope_kind,
                target_id=source.target_id, sender_id=source.sender_id, preset_id=preset.id)
            session.add(row)
        else:
            row.preset_id, row.updated_at = preset.id, datetime.now(UTC)
        return preset.id

    def claim_control(self, service_id: UUID, source: CommandSource, bot_self_id: str, action: str, reply: str,
                      *, workflow_number: int | None = None) -> tuple[str, dict]:
        if (not isinstance(bot_self_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", bot_self_id)
                or not isinstance(action, str) or not 1 <= len(action) <= 64 or not isinstance(reply, str)
                or not 1 <= len(reply) <= 3000 or workflow_number is not None
                and (type(workflow_number) is not int or workflow_number < 0)):
            raise ValueError("drawing_command_invalid")
        event = hashlib.sha256(f"{bot_self_id}:{source.scope_kind}:{source.target_id}:{source.message_id}".encode()).hexdigest()
        key = self.mutation_key("drawing_control", service_id, event)
        digest = fingerprint({"source": source.model_dump(mode="json"), "bot_self": bot_self_id, "action": action})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return key, replay
            self._allowed(session, service_id, source)
            if workflow_number is not None:
                self._set_selection_in(session, service_id, source, workflow_number)
            result = {"status": "claimed", "text": reply, "source": source.model_dump(mode="json")}
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return key, result

    def find_control(self, service_id: UUID, source: CommandSource, bot_self_id: str) -> tuple[str, dict] | None:
        event = hashlib.sha256(f"{bot_self_id}:{source.scope_kind}:{source.target_id}:{source.message_id}".encode()).hexdigest()
        key = self.mutation_key("drawing_control", service_id, event)
        with self.sessions() as session:
            row = session.get(MemeMutationRow, key)
            if (row is None or row.bot_service_id != service_id
                    or row.result.get("source") != source.model_dump(mode="json")):
                return None
            return key, dict(row.result)

    def start_control(self, service_id: UUID, key: str, source: CommandSource, drawing_version: int) -> bool:
        with self.sessions.begin() as session:
            self.lock(session)
            row = session.get(MemeMutationRow, key, with_for_update=True)
            if (row is None or row.bot_service_id != service_id or row.result.get("status") != "claimed"
                    or row.result.get("source") != source.model_dump(mode="json")):
                return False
            try:
                self._allowed(session, service_id, source)
            except ValueError:
                return False
            drawing = session.get(BotDrawingSettingsRow, service_id)
            if drawing is None or drawing.applied_version != drawing_version:
                return False
            row.result = {**row.result, "status": "sending"}
            return True

    def finish_control(self, service_id: UUID, key: str, status: str) -> bool:
        if status not in {"sent", "unknown"}:
            raise ValueError("drawing_send_invalid")
        with self.sessions.begin() as session:
            row = session.get(MemeMutationRow, key, with_for_update=True)
            if row is None or row.bot_service_id != service_id or row.result.get("status") != "sending":
                return False
            row.result = {**row.result, "status": status}
            return True

    @staticmethod
    def _prepare_route(session: Session, service_id: UUID, source: CommandSource, intent: PromptIntent,
                       kind: PresetKind, force_default: bool, workflow_number: int | None = None) -> PreparedRoute:
        if workflow_number is not None and (
                type(workflow_number) is not int or workflow_number < 1 or force_default):
            raise ValueError("drawing_workflow_number_invalid")
        authorized_group = exists(select(BotDrawingScopeRow.group_id).where(
            BotDrawingScopeRow.service_id == service_id, BotDrawingScopeRow.group_id == source.target_id))
        state = session.execute(select(
            BotDrawingSettingsRow.enabled, BotDrawingSettingsRow.row_version, BotDrawingSettingsRow.applied_version,
            BotDrawingSettingsRow.resource_applied_version,
            BotDrawingSettingsRow.default_text_preset_id, BotDrawingSettingsRow.default_edit_preset_id,
            BotDrawingSettingsRow.allow_private, BotServiceRow.enabled, BotServiceRow.kind, BotServiceRow.deleted_at,
            BotDrawingGroupModeRow.mode, BotDrawingGroupModeRow.restrict_workflows,
            BotDrawingSelectionRow.preset_id, authorized_group)
            .select_from(BotDrawingSettingsRow)
            .join(BotServiceRow, BotServiceRow.id == BotDrawingSettingsRow.service_id)
            .outerjoin(BotDrawingGroupModeRow, and_(BotDrawingGroupModeRow.service_id == service_id,
                BotDrawingGroupModeRow.group_id == source.target_id))
            .outerjoin(BotDrawingSelectionRow, and_(BotDrawingSelectionRow.service_id == service_id,
                BotDrawingSelectionRow.scope_kind == source.scope_kind,
                BotDrawingSelectionRow.target_id == source.target_id,
                BotDrawingSelectionRow.sender_id == source.sender_id))
            .where(BotDrawingSettingsRow.service_id == service_id)).one_or_none()
        if state is None:
            raise ValueError("drawing_scope_denied")
        (drawing_on, drawing_version, drawing_applied, resource_applied, default_text, default_edit, allow_private,
         service_on, service_kind, deleted_at, mode, restricted, selection_id, group_allowed) = state
        allowed = ((source.scope_kind == "qq_group" and group_allowed)
                   or (source.scope_kind == "qq_private" and allow_private and source.target_id == source.sender_id))
        if (not drawing_on or not drawing_applied or drawing_applied != drawing_version
                or not service_on or service_kind != "onebot_gateway" or deleted_at or not allowed):
            raise ValueError("drawing_scope_denied")
        if resource_applied != drawing_version:
            raise ValueError("drawing_resources_unavailable")
        rows = session.execute(select(BotDrawingPresetRow.id, BotDrawingPresetRow.number,
            BotDrawingPresetRow.kind, BotDrawingPresetRow.current_revision, BotDrawingPresetRow.enabled,
            BotDrawingPresetRow.row_version, BotDrawingRevisionRow.validation_state,
            BotDrawingRevisionRow.dependency_signature, BotDrawingLoraRouteRow.normalized_key,
            BotDrawingGroupPresetRow.preset_id)
            .join(BotDrawingRevisionRow, and_(BotDrawingRevisionRow.preset_id == BotDrawingPresetRow.id,
                BotDrawingRevisionRow.revision == BotDrawingPresetRow.current_revision))
            .outerjoin(BotDrawingLoraRouteRow, and_(BotDrawingLoraRouteRow.preset_id == BotDrawingPresetRow.id,
                BotDrawingLoraRouteRow.revision == BotDrawingPresetRow.current_revision))
            .outerjoin(BotDrawingGroupPresetRow, and_(BotDrawingGroupPresetRow.service_id == service_id,
                BotDrawingGroupPresetRow.group_id == source.target_id,
                BotDrawingGroupPresetRow.preset_id == BotDrawingPresetRow.id))
            .where(BotDrawingPresetRow.service_id == service_id, BotDrawingPresetRow.engine == "comfy")
            .order_by(BotDrawingPresetRow.number, BotDrawingLoraRouteRow.normalized_key).limit(2021)).all()
        if len(rows) > 2020:
            raise ValueError("drawing_catalog_invalid")
        workflows, versions, signatures, group_allowed_presets = {}, {}, {}, set()
        for preset, number, preset_kind, revision, enabled, version, validation, signature, lora, group_preset in rows:
            if preset not in workflows:
                workflows[preset] = {"id": preset, "number": number, "kind": preset_kind, "revision": revision,
                                     "enabled": enabled, "validation_state": validation, "lora_keys": []}
                versions[preset] = version
                signatures[preset] = signature
            if lora is not None:
                workflows[preset]["lora_keys"].append(lora)
            if group_preset is not None:
                group_allowed_presets.add(group_preset)
        if len(workflows) > 20 or any(len(row["lora_keys"]) > 100 for row in workflows.values()):
            raise ValueError("drawing_catalog_invalid")
        catalog = WorkflowCatalog(workflows=tuple(WorkflowChoice(**row) for row in workflows.values()),
                                  default_text_id=default_text, default_edit_id=default_edit)
        if (selection_id is not None and selection_id not in workflows
                and workflow_number is None and not force_default):
            raise ValueError("drawing_catalog_invalid")
        selection_number = workflow_number if workflow_number is not None else (
            workflows[selection_id]["number"] if selection_id is not None and selection_id in workflows else None)
        decision = select_workflow(catalog, selection_number, intent, kind, force_default=force_default)
        chosen_id = decision.workflow.id if decision.workflow else None
        if source.scope_kind == "qq_group" and restricted and chosen_id is not None and chosen_id not in group_allowed_presets:
            raise ValueError("drawing_group_preset_denied")
        if chosen_id is not None and not signatures.get(chosen_id):
            raise ValueError("drawing_dependency_unverified")
        return PreparedRoute(service_id, (source.scope_kind, source.target_id, source.sender_id, source.message_id),
            None, drawing_version, None, mode if source.scope_kind == "qq_group" and mode else "green",
            None if force_default or workflow_number is not None else selection_id,
            versions.get(chosen_id), signatures.get(chosen_id), intent, kind, force_default, decision, workflow_number)

    def prepare_route(self, service_id: UUID, source: CommandSource, intent: PromptIntent,
                      kind: PresetKind, *, force_default: bool = False,
                      workflow_number: int | None = None) -> PreparedRoute:
        with self.sessions() as session:
            return self._prepare_route(session, service_id, source, intent, kind, force_default, workflow_number)

    @staticmethod
    def _prepare_tag(session: Session, service_id: UUID, source: CommandSource) -> PreparedTag:
        BotDrawingRepository._allowed(session, service_id, source)
        drawing = session.get(BotDrawingSettingsRow, service_id)
        if drawing.resource_applied_version != drawing.row_version:
            raise ValueError("drawing_resources_unavailable")
        group = session.get(BotDrawingGroupModeRow, (service_id, source.target_id)) if source.scope_kind == "qq_group" else None
        return PreparedTag(service_id, (source.scope_kind, source.target_id, source.sender_id, source.message_id),
                           None, drawing.row_version, group.mode if group else "green")

    def prepare_tag(self, service_id: UUID, source: CommandSource) -> PreparedTag:
        with self.sessions() as session:
            return self._prepare_tag(session, service_id, source)

    def find_source(self, service_id: UUID, source: CommandSource, bot_self_id: str) -> UUID | None:
        with self.sessions() as session:
            return session.scalar(select(BotDrawingTaskRow.id).where(BotDrawingTaskRow.service_id == service_id,
                BotDrawingTaskRow.bot_self_id == bot_self_id, BotDrawingTaskRow.scope_kind == source.scope_kind,
                BotDrawingTaskRow.target_id == source.target_id, BotDrawingTaskRow.source_message_id == source.message_id,
                BotDrawingTaskRow.origin == "qq"))

    def list_tasks(self, service_id: UUID, *, after_id: UUID | None = None, limit: int = 20,
                   view: str = "all") -> dict:
        if type(limit) is not int or not 1 <= limit <= 50 or view not in {"all", "active", "history"}:
            raise ValueError("drawing_task_limit_invalid")
        with self.sessions() as session:
            self._service(session, service_id)
            query = (select(BotDrawingTaskRow.id, BotDrawingTaskRow.operation, BotDrawingTaskRow.origin,
                BotDrawingTaskRow.generation_status, BotDrawingTaskRow.audit_status, BotDrawingTaskRow.send_status,
                BotDrawingTaskRow.cleanup_status, BotDrawingTaskRow.error_code, BotDrawingTaskRow.created_at,
                BotDrawingTaskRow.processing_stage, BotDrawingTaskRow.online_request_state,
                BotDrawingTaskRow.online_request_sent, BotDrawingTaskRow.online_image_received,
                BotDrawingTaskRow.wish_text.is_not(None).label("prompt_model_used"),
                BotDrawingTaskRow.input_sha256.is_not(None).label("input_media_present"),
                BotDrawingTaskRow.started_at.is_not(None).label("has_started"),
                BotDrawingTaskRow.prompt_status, BotDrawingTaskRow.input_audit_status,
                BotDrawingTaskRow.resource_registration_state, BotDrawingTaskRow.prompt_transport_started,
                BotDrawingTaskRow.reply_kind,
                BotDrawingTaskRow.cancel_requested, BotDrawingTaskRow.deletion_state,
                BotDrawingTaskRow.scope_kind, BotDrawingTaskRow.target_id, BotDrawingGroupModeRow.mode.label("current_mode"),
                BotDrawingTaskRow.output_reference, BotDrawingTaskRow.media_expires_at, BotDrawingTaskRow.audit_mode,
                BotDrawingTaskRow.audit_sha256, BotDrawingTaskRow.output_sha256,
                BotDrawingTaskRow.audit_model_revision, BotDrawingTaskRow.audit_policy_version)
                .outerjoin(BotDrawingGroupModeRow, and_(BotDrawingTaskRow.scope_kind == "qq_group",
                    BotDrawingGroupModeRow.service_id == BotDrawingTaskRow.service_id,
                    BotDrawingGroupModeRow.group_id == BotDrawingTaskRow.target_id))
                .where(BotDrawingTaskRow.service_id == service_id, BotDrawingTaskRow.deletion_state != "deleted"))
            active = or_(BotDrawingTaskRow.generation_status.in_(("queued", "submitting", "running", "cancelling", "reconciling")),
                BotDrawingTaskRow.processing_stage.in_(("cleaning", "input_audit", "output_audit", "waiting_audit_stop")),
                BotDrawingTaskRow.send_status.in_(("ready", "sending")),
                BotDrawingTaskRow.cleanup_status.in_(("pending", "deferred", "failed")))
            if view == "active":
                query = query.where(active, BotDrawingTaskRow.deletion_state == "visible")
            elif view == "history":
                query = query.where(BotDrawingTaskRow.generation_status.in_(("succeeded", "failed", "cancelled", "expired")))
            if after_id is not None:
                cursor = select(BotDrawingTaskRow.created_at).where(BotDrawingTaskRow.service_id == service_id,
                    BotDrawingTaskRow.id == after_id).scalar_subquery()
                query = query.where(tuple_(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id) < tuple_(cursor, after_id))
            rows = session.execute(query.order_by(BotDrawingTaskRow.created_at.desc(), BotDrawingTaskRow.id.desc())
                                   .limit(limit + 1)).all()
            items = [{"id": str(row.id), "operation": row.operation, "origin": row.origin,
                "generation_status": row.generation_status, "audit_status": row.audit_status,
                "send_status": row.send_status, "cleanup_status": row.cleanup_status, "error_code": row.error_code,
                "processing_stage": row.processing_stage, "cancel_requested": row.cancel_requested,
                "online_request_state": row.online_request_state,
                "online_request_sent": row.online_request_sent, "online_image_received": row.online_image_received,
                "prompt_model_used": row.prompt_model_used, "prompt_status": row.prompt_status,
                "has_input_image": _input_image_for_flow(row.input_media_present, row.input_audit_status, row.generation_status),
                "input_audit_status": row.input_audit_status, "resource_registration_state": row.resource_registration_state,
                "prompt_transport_started": row.prompt_transport_started, "has_started": row.has_started, "reply_kind": row.reply_kind,
                "deletion_state": row.deletion_state, "scope_kind": row.scope_kind, "target_id": row.target_id,
                "created_at": row.created_at.isoformat(), "has_image": row.deletion_state == "visible" and _preview_visible(
                    row.origin, row.output_reference, row.media_expires_at,
                    (row.current_mode or "green") if row.origin == "qq" else row.audit_mode, row.audit_status,
                    row.audit_sha256, row.output_sha256, row.audit_model_revision, row.audit_policy_version)} for row in rows[:limit]]
            return {"items": items, "next_after_id": str(rows[limit - 1].id) if len(rows) > limit else None}

    def get_task(self, service_id: UUID, task_id: UUID) -> dict:
        with self.sessions() as session:
            task = session.get(BotDrawingTaskRow, task_id)
            if task is None or task.service_id != service_id or task.deletion_state == "deleted":
                raise ValueError("drawing_task_not_found")
            group = (session.get(BotDrawingGroupModeRow, (service_id, task.target_id))
                     if task.origin == "qq" and task.scope_kind == "qq_group" else None)
            mode = (group.mode if group else "green") if task.origin == "qq" else task.audit_mode
            return {"id": str(task.id), "operation": task.operation, "origin": task.origin,
                "generation_status": task.generation_status, "audit_status": task.audit_status,
                "send_status": task.send_status, "cleanup_status": task.cleanup_status,
                "processing_stage": task.processing_stage, "cancel_requested": task.cancel_requested,
                "online_request_state": task.online_request_state,
                "online_request_sent": task.online_request_sent, "online_image_received": task.online_image_received,
                "prompt_model_used": task.wish_text is not None, "prompt_status": task.prompt_status,
                "has_input_image": _input_image_for_flow(task.input_sha256 is not None, task.input_audit_status, task.generation_status),
                "input_audit_status": task.input_audit_status, "resource_registration_state": task.resource_registration_state,
                "prompt_transport_started": task.prompt_transport_started, "has_started": task.started_at is not None,
                "reply_kind": task.reply_kind,
                "deletion_state": task.deletion_state, "scope_kind": task.scope_kind, "target_id": task.target_id,
                "error_code": task.error_code, "online_moderation_details": task.online_moderation_details,
                "created_at": task.created_at.isoformat(),
                "wish_text": task.wish_text, "positive": task.positive, "negative": task.negative,
                "has_image": task.deletion_state == "visible" and _preview_visible(task.origin, task.output_reference, task.media_expires_at,
                    mode, task.audit_status, task.audit_sha256, task.output_sha256,
                    task.audit_model_revision, task.audit_policy_version)}

    def task_for_worker(self, task_id: UUID) -> dict:
        with self.sessions() as session:
            task = session.get(BotDrawingTaskRow, task_id)
            if task is None:
                raise ValueError("drawing_task_not_found")
            return {column.name: getattr(task, column.name) for column in BotDrawingTaskRow.__table__.columns}

    def next_registration(self, endpoint_key: str, expected_task_id: UUID | None = None) -> UUID | None:
        with self.sessions() as session:
            blocked = session.scalar(select(exists(select(BotDrawingTaskRow.id).where(
                BotDrawingTaskRow.endpoint_key == endpoint_key,
                BotDrawingTaskRow.cleanup_status.in_(("deferred", "failed"))))))
            if blocked:
                return None
            query = select(BotDrawingTaskRow.id).where(BotDrawingTaskRow.endpoint_key == endpoint_key,
                BotDrawingTaskRow.generation_status == "queued",
                BotDrawingTaskRow.resource_registration_state.in_(("pending", "unknown")),
                (BotDrawingTaskRow.next_check_at.is_(None) | (BotDrawingTaskRow.next_check_at <= datetime.now(UTC))))
            if expected_task_id is not None:
                query = query.where(BotDrawingTaskRow.id == expected_task_id)
            return session.scalar(query.order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(1))

    def queued_count(self, endpoint_key: str) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(BotDrawingTaskRow).where(
                BotDrawingTaskRow.endpoint_key == endpoint_key, BotDrawingTaskRow.generation_status == 'queued')) or 0

    def media_usage(self, service_id: UUID) -> dict:
        with self.sessions() as session:
            self._service(session, service_id)
            used = session.scalar(select(func.coalesce(func.sum(BotDrawingSettingsRow.media_bytes), 0))) or 0
            return {"media_used_bytes": used, "media_limit_bytes": MAX_MEDIA_BYTES}

    def gpu_recovery(self, endpoint_key: str) -> list[dict]:
        with self.sessions() as session:
            rows = session.execute(select(BotDrawingTaskRow.id, BotDrawingTaskRow.service_id,
                BotDrawingTaskRow.sender_id, BotDrawingTaskRow.origin, BotDrawingTaskRow.generation_status)
                .where(BotDrawingTaskRow.endpoint_key == endpoint_key,
                    (BotDrawingTaskRow.generation_status.in_(("queued", "submitting", "running", "cancelling", "reconciling")) |
                     (BotDrawingTaskRow.generation_status.in_(("succeeded", "failed", "cancelled", "expired")) &
                      BotDrawingTaskRow.cleanup_status.in_(("none", "pending", "deferred", "failed")))))
                .order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(12)).all()
        if len(rows) > 11:
            raise ValueError("gpu_recovery_overflow")
        return [{"id": row.id, "owner": (row.service_id, row.sender_id) if row.origin == "qq" else None,
                 "active": row.generation_status != "queued"} for row in rows]

    def next_active(self, endpoint_key: str) -> UUID | None:
        with self.sessions() as session:
            return session.scalar(select(BotDrawingTaskRow.id).where(BotDrawingTaskRow.endpoint_key == endpoint_key,
                BotDrawingTaskRow.generation_status.in_(("submitting", "running", "cancelling", "reconciling")),
                (BotDrawingTaskRow.next_check_at.is_(None) | (BotDrawingTaskRow.next_check_at <= datetime.now(UTC))))
                .order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(1))

    def next_cleanup(self, endpoint_key: str) -> UUID | None:
        with self.sessions() as session:
            return session.scalar(select(BotDrawingTaskRow.id).where(BotDrawingTaskRow.endpoint_key == endpoint_key,
                BotDrawingTaskRow.generation_status.in_(("succeeded", "failed", "cancelled", "expired")),
                BotDrawingTaskRow.cleanup_status.in_(("none", "pending", "deferred", "failed")),
                (BotDrawingTaskRow.next_check_at.is_(None) | (BotDrawingTaskRow.next_check_at <= datetime.now(UTC))))
                .order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(1))

    def next_media_cleanup(self) -> UUID | None:
        with self.sessions() as session:
            return session.scalar(select(BotDrawingTaskRow.id).where(
                BotDrawingTaskRow.media_expires_at <= datetime.now(UTC), BotDrawingTaskRow.cleanup_status == "done",
                (BotDrawingTaskRow.input_bytes.is_not(None) |
                 (BotDrawingTaskRow.output_bytes.is_not(None) & BotDrawingTaskRow.output_reference.is_(None))))
                .order_by(BotDrawingTaskRow.media_expires_at, BotDrawingTaskRow.id).limit(1))

    def cleanup_orphan_media(self, media) -> int:
        with media.staging():
            candidates = media.orphan_candidates()
            if not candidates:
                return 0
            with self.sessions.begin() as session:
                self.lock(session)
                existing = set(session.scalars(select(BotDrawingTaskRow.id).where(
                    BotDrawingTaskRow.id.in_(candidates))).all())
                return sum(media.delete_orphan(task_id) for task_id in candidates if task_id not in existing)

    def clear_media(self, task_id: UUID, kind: str, *, checksum: str, size: int) -> bool:
        if kind not in {"input", "output"} or type(size) is not int or size < 1:
            raise ValueError("drawing_media_reference_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.cleanup_status != "done" or task.media_expires_at is None
                    or task.media_expires_at > datetime.now(UTC)):
                return False
            if getattr(task, f"{kind}_sha256") != checksum or getattr(task, f"{kind}_bytes") != size:
                return False
            settings = session.get(BotDrawingSettingsRow, task.service_id, with_for_update=True)
            if settings is None or settings.media_bytes < size:
                raise ValueError("drawing_media_accounting_invalid")
            settings.media_bytes -= size
            setattr(task, f"{kind}_reference", None)
            setattr(task, f"{kind}_sha256", None)
            setattr(task, f"{kind}_bytes", None)
            task.row_version += 1
            return True

    def begin_delete_tasks(self, service_id: UUID, task_ids: list[UUID], *, operation_id: str) -> list[UUID]:
        if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 50
                or any(not isinstance(task_id, UUID) for task_id in task_ids)
                or len(set(task_ids)) != len(task_ids)):
            raise ValueError("drawing_delete_invalid")
        key = self.mutation_key("drawing_history_delete", service_id, operation_id)
        digest = fingerprint({"tasks": [str(task_id) for task_id in task_ids]})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return [UUID(value) for value in replay["tasks"]]
            self._service(session, service_id)
            rows = session.scalars(select(BotDrawingTaskRow).where(
                BotDrawingTaskRow.service_id == service_id, BotDrawingTaskRow.id.in_(task_ids))
                .with_for_update()).all()
            if len(rows) != len(task_ids):
                raise ValueError("drawing_task_not_found")
            for task in rows:
                if task.deletion_state == "deleted":
                    continue
                if (task.generation_status not in {"succeeded", "failed", "cancelled", "expired"}
                        or task.cleanup_status != "done" or task.send_status == "sending"
                        or task.send_status == "ready" and task.result_transport_started
                        or task.notice_status == "sending"):
                    raise ValueError("drawing_task_busy")
                if task.deletion_state == "visible":
                    task.deletion_state, task.processing_stage = "deleting", "deleting"
                    task.next_check_at = None
                    task.row_version += 1
            result = {"tasks": [str(task_id) for task_id in task_ids]}
            self.add_mutation(session, key, digest, result, bot_service_id=service_id)
            return task_ids

    def next_deletion(self) -> UUID | None:
        with self.sessions() as session:
            return session.scalar(select(BotDrawingTaskRow.id).where(
                BotDrawingTaskRow.deletion_state == "deleting",
                or_(BotDrawingTaskRow.next_check_at.is_(None), BotDrawingTaskRow.next_check_at <= datetime.now(UTC)))
                .order_by(BotDrawingTaskRow.id).limit(1))

    def clear_deleted_media(self, task_id: UUID, kind: str, *, checksum: str, size: int) -> bool:
        if kind not in {"input", "output"} or type(size) is not int or size < 1:
            raise ValueError("drawing_media_reference_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.deletion_state != "deleting":
                return False
            if getattr(task, f"{kind}_sha256") != checksum or getattr(task, f"{kind}_bytes") != size:
                return False
            settings = session.get(BotDrawingSettingsRow, task.service_id, with_for_update=True)
            if settings is None or settings.media_bytes < size:
                raise ValueError("drawing_media_accounting_invalid")
            settings.media_bytes -= size
            setattr(task, f"{kind}_reference", None)
            setattr(task, f"{kind}_sha256", None)
            setattr(task, f"{kind}_bytes", None)
            task.row_version += 1
            return True

    def finish_delete_task(self, task_id: UUID) -> bool:
        from al1s.adapters.postgres.bot_drawing_models import BotDrawingPromptProfileRow

        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.deletion_state == "deleted":
                return False
            if (task.deletion_state != "deleting" or any(getattr(task, f"{kind}_{field}") is not None
                    for kind in ("input", "output") for field in ("reference", "sha256", "bytes"))):
                raise ValueError("drawing_delete_pending")
            profile = session.get(BotDrawingPromptProfileRow, task.prompt_profile_id) if task.prompt_profile_id else None
            task.wish_text = task.positive = task.negative = None
            task.prompt_workflow_name = task.prompt_aliases = None
            task.requested_lora_keys = task.ignored_lora_keys = None
            task.prompt_profile_id = task.prompt_profile_revision = None
            task.tag_result = task.reply_kind = None
            if task.send_status == "ready" and not task.result_transport_started:
                task.send_status = "none"
            task.deletion_state, task.processing_stage, task.deleted_at = "deleted", "deleted", datetime.now(UTC)
            task.row_version += 1
            session.flush()
            if profile is not None:
                self._prune_prompt_revisions(session, profile)
            return True

    def purge_records(self, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("drawing_task_limit_invalid")
        with self.sessions.begin() as session:
            candidates = select(BotDrawingTaskRow.id).where(
                BotDrawingTaskRow.deletion_state == "deleted",
                BotDrawingTaskRow.deleted_at <= datetime.now(UTC) - timedelta(days=30),
                BotDrawingTaskRow.cleanup_status == "done", BotDrawingTaskRow.input_bytes.is_(None),
                BotDrawingTaskRow.output_bytes.is_(None),
                BotDrawingTaskRow.generation_status.in_(("succeeded", "failed", "expired", "cancelled")))
            candidates = candidates.order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(limit).with_for_update(skip_locked=True)
            return session.execute(delete(BotDrawingTaskRow).where(BotDrawingTaskRow.id.in_(candidates))).rowcount

    def next_delivery(self, endpoint_key: str) -> tuple[UUID, str] | None:
        with self.sessions() as session:
            group_allowed = exists(select(BotDrawingScopeRow.group_id).where(
                BotDrawingScopeRow.service_id == BotDrawingTaskRow.service_id,
                BotDrawingScopeRow.group_id == BotDrawingTaskRow.target_id))
            preset_enabled = exists(select(BotDrawingPresetRow.id).where(
                BotDrawingPresetRow.service_id == BotDrawingTaskRow.service_id,
                BotDrawingPresetRow.id == BotDrawingTaskRow.preset_id,
                BotDrawingPresetRow.enabled.is_(True)))
            group_restricted = exists(select(BotDrawingGroupModeRow.group_id).where(
                BotDrawingGroupModeRow.service_id == BotDrawingTaskRow.service_id,
                BotDrawingGroupModeRow.group_id == BotDrawingTaskRow.target_id,
                BotDrawingGroupModeRow.restrict_workflows.is_(True)))
            group_preset_allowed = exists(select(BotDrawingGroupPresetRow.preset_id).where(
                BotDrawingGroupPresetRow.service_id == BotDrawingTaskRow.service_id,
                BotDrawingGroupPresetRow.group_id == BotDrawingTaskRow.target_id,
                BotDrawingGroupPresetRow.preset_id == BotDrawingTaskRow.preset_id))
            row = session.execute(
                select(BotDrawingTaskRow.id, BotDrawingTaskRow.send_status)
                .join(BotServiceRow, BotServiceRow.id == BotDrawingTaskRow.service_id)
                .join(BotDrawingSettingsRow,
                      BotDrawingSettingsRow.service_id == BotDrawingTaskRow.service_id)
                .where(
                    BotDrawingTaskRow.endpoint_key == endpoint_key, BotDrawingTaskRow.origin == "qq",
                    BotDrawingTaskRow.deletion_state == "visible",
                    BotServiceRow.kind == "onebot_gateway", BotServiceRow.deleted_at.is_(None),
                    BotServiceRow.enabled.is_(True), BotDrawingSettingsRow.enabled.is_(True),
                    BotDrawingSettingsRow.applied_version > 0,
                    or_(and_(BotDrawingTaskRow.scope_kind == "qq_group", group_allowed),
                        and_(BotDrawingTaskRow.scope_kind == "qq_private",
                             BotDrawingSettingsRow.allow_private.is_(True),
                             BotDrawingTaskRow.sender_id == BotDrawingTaskRow.target_id)),
                    or_(BotDrawingTaskRow.send_status != "ready",
                        BotDrawingTaskRow.reply_kind != "image", preset_enabled),
                    or_(BotDrawingTaskRow.send_status != "ready", BotDrawingTaskRow.reply_kind != "image",
                        BotDrawingTaskRow.endpoint_key != ONLINE_ENDPOINT, BotDrawingTaskRow.scope_kind != "qq_group",
                        ~group_restricted, group_preset_allowed),
                    or_(BotDrawingTaskRow.send_status == "ready",
                        and_(BotDrawingTaskRow.notice_status == "none",
                             BotDrawingTaskRow.send_status == "none",
                             or_(BotDrawingTaskRow.endpoint_key != ONLINE_ENDPOINT,
                                 BotDrawingTaskRow.generation_status == "queued"),
                             or_(BotDrawingTaskRow.expires_at.is_(None),
                                 BotDrawingTaskRow.expires_at > datetime.now(UTC)),
                             BotDrawingTaskRow.generation_status.in_(("queued", "submitting", "running"))))
                )
                .order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(1)
            ).one_or_none()
            if row is None:
                return None
            return row.id, "result" if row.send_status == "ready" else "notice"

    def defer_check(self, task_id: UUID, *, expected_version: int, seconds: int = 2) -> int:
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise ValueError("drawing_task_invalid")
        with self.sessions.begin() as session:
            version = session.scalar(update(BotDrawingTaskRow).where(BotDrawingTaskRow.id == task_id,
                BotDrawingTaskRow.row_version == expected_version).values(
                    next_check_at=datetime.now(UTC) + timedelta(seconds=seconds),
                    row_version=BotDrawingTaskRow.row_version + 1).returning(BotDrawingTaskRow.row_version))
            if version is None:
                raise ValueError("drawing_task_conflict")
            return version

    def set_stage(self, task_id: UUID, stage: str, *, expected_version: int) -> int:
        if stage not in {"input_audit", "preparing_resources", "output_audit", "cleaning"}:
            raise ValueError("drawing_stage_invalid")
        with self.sessions.begin() as session:
            version = session.scalar(update(BotDrawingTaskRow).where(
                BotDrawingTaskRow.id == task_id, BotDrawingTaskRow.row_version == expected_version,
                BotDrawingTaskRow.cancel_requested.is_(False)).values(
                    processing_stage=stage, row_version=BotDrawingTaskRow.row_version + 1)
                .returning(BotDrawingTaskRow.row_version))
            if version is None:
                raise ValueError("drawing_task_conflict")
            return version

    def current_mode(self, service_id: UUID, source: CommandSource, drawing_version: int) -> str:
        with self.sessions() as session:
            self._allowed(session, service_id, source, require_applied=False)
            group = session.get(BotDrawingGroupModeRow, (service_id, source.target_id)) if source.scope_kind == "qq_group" else None
            return group.mode if group else "green"

    def preview_media(self, service_id: UUID, task_id: UUID) -> tuple[str, str]:
        with self.sessions() as session:
            task = session.get(BotDrawingTaskRow, task_id)
            if task is None or task.service_id != service_id or task.deletion_state != "visible":
                raise ValueError("drawing_task_not_found")
            group = (session.get(BotDrawingGroupModeRow, (service_id, task.target_id))
                     if task.origin == "qq" and task.scope_kind == "qq_group" else None)
            mode = (group.mode if group else "green") if task.origin == "qq" else task.audit_mode
            if task.generation_status != "succeeded" or not _preview_visible(
                    task.origin, task.output_reference, task.media_expires_at, mode, task.audit_status,
                    task.audit_sha256, task.output_sha256, task.audit_model_revision, task.audit_policy_version):
                raise ValueError("drawing_media_unavailable")
            return task.output_reference, task.output_sha256

    def admit_tag(self, service_id: UUID, source: CommandSource, bot_self_id: str, endpoint_key: str,
                  prepared: PreparedTag, *, task_id: UUID, input_reference: str, input_sha256: str,
                  input_bytes: int, reference_message_id: str) -> UUID:
        if (prepared.service_id != service_id or prepared.source !=
                (source.scope_kind, source.target_id, source.sender_id, source.message_id)
                or not isinstance(bot_self_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", bot_self_id)
                or not isinstance(endpoint_key, str) or not 1 <= len(endpoint_key) <= 128
                or not _valid_input(task_id, input_reference, input_sha256, input_bytes)
                or not isinstance(reference_message_id, str) or not re.fullmatch(r"-?[0-9]{1,64}", reference_message_id)):
            raise ValueError("drawing_request_invalid")
        identity = hashlib.sha256(f"{bot_self_id}:{source.scope_kind}:{source.target_id}:{source.message_id}".encode()).hexdigest()
        key = self.mutation_key("drawing_event", service_id, identity)
        digest = fingerprint({"source": prepared.source, "bot_self": bot_self_id, "endpoint": endpoint_key,
            "operation": "tag", "ai_version": prepared.ai_version, "drawing_version": prepared.drawing_version,
            "group_mode": prepared.group_mode, "input_reference": input_reference, "input_sha256": input_sha256,
            "input_bytes": input_bytes, "reference_message_id": reference_message_id})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return UUID(replay["task_id"])
            if self._prepare_tag(session, service_id, source) != prepared:
                raise ValueError("drawing_route_changed")
            self._check_queue_capacity(session, endpoint_key)
            self._reserve_media(session, service_id, input_bytes)
            now = datetime.now(UTC)
            settings = session.get(BotDrawingSettingsRow, service_id)
            task = BotDrawingTaskRow(id=task_id, service_id=service_id, operation="tag", origin="qq",
                scope_kind=source.scope_kind, target_id=source.target_id, sender_id=source.sender_id,
                source_message_id=source.message_id, bot_self_id=bot_self_id, ai_version=prepared.ai_version,
                drawing_version=prepared.drawing_version, input_reference=input_reference, input_sha256=input_sha256,
                input_bytes=input_bytes, reference_message_id=reference_message_id, endpoint_key=endpoint_key,
                comfy_endpoint=settings.comfy_base_url,
                prompt_id=uuid4(), audit_mode=prepared.group_mode, created_at=now, expires_at=now + timedelta(seconds=1800))
            session.add(task)
            session.flush()
            self.add_mutation(session, key, digest, {"task_id": str(task.id)}, bot_service_id=service_id)
            return task.id

    def admit_online(self, service_id, *, task_id, positive, negative="", source=None, bot_self_id=None,
                     preset_number=None, preset_id=None, expected_version=None, operation_id=None, mode=None,
                     input_reference=None, input_sha256=None, input_bytes=None, reference_message_id=None,
                     original_positive=None):
        if (not isinstance(task_id, UUID) or not isinstance(positive, str) or not positive.strip()
                or not isinstance(negative, str) or len(positive) + len(negative) > 2000
                or original_positive is not None and (not isinstance(original_positive, str) or len(original_positive) > 2000)
                or preset_number is not None and (type(preset_number) is not int or preset_number < 1)
                or input_reference is not None and not _valid_input(task_id, input_reference, input_sha256, input_bytes)
                or input_reference is None and (input_sha256 is not None or input_bytes is not None)):
            raise ValueError("drawing_request_invalid")
        preview = source is None
        if preview:
            if not isinstance(preset_id, UUID) or mode not in {"green", "r18"} or not operation_id:
                raise ValueError("drawing_request_invalid")
            identity = operation_id
        else:
            if not isinstance(bot_self_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", bot_self_id):
                raise ValueError("drawing_request_invalid")
            identity = hashlib.sha256(
                f"{bot_self_id}:{source.scope_kind}:{source.target_id}:{source.message_id}".encode()).hexdigest()
        key = self.mutation_key("drawing_online_preview" if preview else "drawing_event", service_id, identity)
        digest_input = {"task_id": str(task_id), "preset": str(preset_id) if preset_id else None,
            "number": preset_number, "positive": positive, "negative": negative, "mode": mode,
            "input_sha256": input_sha256, "reference_message_id": reference_message_id,
            "expected_version": expected_version}
        if original_positive is not None:
            digest_input["original_positive"] = original_positive
        digest = fingerprint(digest_input)
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return UUID(replay["task_id"])
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id, with_for_update=True)
            if settings is None:
                raise ValueError("drawing_settings_unavailable")
            if not preview:
                self._allowed(session, service_id, source)
            if preset_id is None and preset_number is None:
                preset_id = settings.default_online_preset_id
            query = select(BotDrawingPresetRow).where(BotDrawingPresetRow.service_id == service_id,
                                                       BotDrawingPresetRow.engine == "openai")
            query = query.where(BotDrawingPresetRow.id == preset_id) if preset_id is not None else query.where(
                BotDrawingPresetRow.number == preset_number)
            preset = session.scalar(query.with_for_update())
            if (preset is None or (not preview and not preset.enabled)
                    or (preview and preset.row_version != expected_version)):
                raise ValueError("drawing_online_preset_unavailable")
            revision = session.get(BotDrawingRevisionRow, (preset.id, preset.current_revision))
            if revision is None or revision.validation_state != "ready" or revision.online_secret_id is None:
                raise ValueError("drawing_online_unconfigured")
            if not preview and source.scope_kind == "qq_group":
                group = session.get(BotDrawingGroupModeRow, (service_id, source.target_id))
                if group and group.restrict_workflows and session.get(
                        BotDrawingGroupPresetRow, (service_id, source.target_id, preset.id)) is None:
                    raise ValueError("drawing_group_preset_denied")
                mode = group.mode if group else "green"
            elif not preview:
                mode = "green"
            self._check_queue_capacity(session, ONLINE_ENDPOINT)
            self._reserve_media(session, service_id, input_bytes or 0)
            now = datetime.now(UTC)
            expand = bool(revision.online_prompt_model and not preview)
            task = BotDrawingTaskRow(id=task_id, service_id=service_id, operation="draw",
                origin="admin_preview" if preview else "qq", scope_kind=source.scope_kind if source else None,
                target_id=source.target_id if source else None, sender_id=source.sender_id if source else None,
                source_message_id=source.message_id if source else None, bot_self_id=bot_self_id,
                preset_id=preset.id, preset_revision=revision.revision,
                dependency_signature=revision.dependency_signature, drawing_version=settings.applied_version,
                intent_mode="wish" if expand else "direct", wish_text=(original_positive or positive) if expand else None,
                positive="" if expand else positive, negative=negative,
                prompt_workflow_name=preset.name if expand else None, prompt_aliases=[] if expand else None,
                prompt_status="waiting" if expand else "ready", requested_lora_keys=[], ignored_lora_keys=[],
                route_reason="manual" if preset_number else "default", input_reference=input_reference,
                input_sha256=input_sha256, input_bytes=input_bytes, reference_message_id=reference_message_id,
                endpoint_key=ONLINE_ENDPOINT, prompt_id=uuid4(), online_request_state="not_started",
                audit_mode=mode, created_at=now, expires_at=now + timedelta(seconds=1800))
            session.add(task)
            session.flush()
            self.add_mutation(session, key, digest, {"task_id": str(task.id)}, bot_service_id=service_id)
            return task.id

    def claim_online_next(self):
        with self.sessions.begin() as session:
            self.lock(session)
            busy = session.scalar(select(exists(select(BotDrawingTaskRow.id).where(
                BotDrawingTaskRow.endpoint_key == ONLINE_ENDPOINT,
                BotDrawingTaskRow.generation_status.in_(("submitting", "running", "cancelling", "reconciling"))))))
            if busy:
                return None
            task = session.scalar(select(BotDrawingTaskRow).where(BotDrawingTaskRow.endpoint_key == ONLINE_ENDPOINT,
                BotDrawingTaskRow.generation_status == "queued").order_by(BotDrawingTaskRow.created_at,
                BotDrawingTaskRow.id).limit(1).with_for_update(skip_locked=True))
            if task is None:
                return None
            now = datetime.now(UTC)
            if task.expires_at is not None and task.expires_at <= now:
                self._expire_queued(task, now)
                return None
            task.generation_status, task.processing_stage = "submitting", "preparing_online"
            task.started_at, task.row_version = now, task.row_version + 1
            return task.id

    def online_state(self, task_id, state):
        if state not in {"submitting", "sent", "received"}:
            raise ValueError("drawing_task_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.endpoint_key != ONLINE_ENDPOINT or task.generation_status != "submitting" or task.cancel_requested:
                return False
            allowed = {"submitting": {"not_started"}, "sent": {"submitting"},
                       "received": {"submitting", "sent"}}
            if task.online_request_state not in allowed[state]:
                return False
            task.online_request_state = state
            if state == "submitting":
                task.prompt_transport_started = True
            elif state == "sent":
                task.online_request_sent = True
            elif state == "received":
                task.online_request_sent, task.online_image_received = True, True
            task.processing_stage = {"submitting": "online_submitting", "sent": "online_waiting",
                                     "received": "online_received"}[state]
            task.row_version += 1
            return True

    def fail_online(self, task_id, code, state, moderation_details=None):
        if state not in {"failed", "unknown"} or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            raise ValueError("drawing_task_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.endpoint_key != ONLINE_ENDPOINT or task.cancel_requested
                    or task.generation_status not in {"queued", "submitting"}):
                return False
            task.generation_status = "failed"
            if task.online_request_state != "received":
                task.online_request_state = state
            task.error_code, task.finished_at = code, datetime.now(UTC)
            task.online_moderation_details = moderation_details if code == "drawing_online_moderation_blocked" else None
            task.cleanup_status, task.reply_kind = "done", "failure_notice"
            task.media_expires_at = datetime.now(UTC) + timedelta(hours=24)
            task.send_status = "ready" if task.origin == "qq" else "none"
            task.processing_stage = "sending" if task.origin == "qq" else "finished"
            if task.prompt_status in {"waiting", "working"}:
                task.prompt_status = "failed"
            task.row_version += 1
            return True

    def recover_online(self):
        with self.sessions.begin() as session:
            self.lock(session)
            tasks = session.scalars(select(BotDrawingTaskRow).where(
                BotDrawingTaskRow.endpoint_key == ONLINE_ENDPOINT,
                BotDrawingTaskRow.generation_status.in_(("submitting", "running", "cancelling", "reconciling")))
                .order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(6).with_for_update()).all()
            for task in tasks:
                if task.online_request_state == "not_started" and not task.cancel_requested:
                    task.generation_status, task.processing_stage = "queued", "queued"
                    task.started_at = None
                else:
                    task.generation_status = "cancelled" if task.cancel_requested else "failed"
                    task.online_request_state = "unknown"
                    task.error_code = "drawing_cancelled" if task.cancel_requested else "drawing_online_unknown"
                    task.finished_at, task.cleanup_status, task.reply_kind = datetime.now(UTC), "done", "failure_notice"
                    task.media_expires_at = datetime.now(UTC) + timedelta(hours=24)
                    task.send_status = "ready" if task.origin == "qq" else "none"
                    task.processing_stage = "sending" if task.origin == "qq" else "finished"
                task.row_version += 1

    def online_positions(self):
        with self.sessions() as session:
            rows = session.execute(select(BotDrawingTaskRow.id, BotDrawingTaskRow.generation_status)
                .where(BotDrawingTaskRow.endpoint_key == ONLINE_ENDPOINT,
                       BotDrawingTaskRow.generation_status.in_(("queued", "submitting", "running")))
                .order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id).limit(6)).all()
            active = [row.id for row in rows if row.generation_status != "queued"]
            waiting = [row.id for row in rows if row.generation_status == "queued"]
            return {str(identity): index for index, identity in enumerate([*active, *waiting])}

    def admit_preview(self, service_id: UUID, preset_id: UUID, endpoint_key: str, *, task_id: UUID,
                      positive: str, negative: str, seed: int, mode: str, expected_version: int,
                      operation_id: str, input_reference: str | None = None, input_sha256: str | None = None,
                      input_bytes: int | None = None) -> UUID:
        if (not isinstance(task_id, UUID) or not isinstance(endpoint_key, str) or not 1 <= len(endpoint_key) <= 128
                or not isinstance(positive, str) or not isinstance(negative, str) or not positive.strip()
                or len(positive) + len(negative) > 2000 or type(seed) is not int or not 0 <= seed < 2 ** 63
                or mode not in {"green", "r18"}):
            raise ValueError("drawing_request_invalid")
        key = self.mutation_key("drawing_preview", service_id, operation_id)
        digest = fingerprint({"preset_id": str(preset_id), "endpoint": endpoint_key, "positive": positive,
            "negative": negative, "seed": seed, "mode": mode, "expected_version": expected_version,
            "input_reference": input_reference, "input_sha256": input_sha256, "input_bytes": input_bytes})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return UUID(replay["task_id"])
            self._service(session, service_id)
            settings = session.get(BotDrawingSettingsRow, service_id)
            preset = session.get(BotDrawingPresetRow, preset_id, with_for_update=True)
            if (settings is None or preset is None or preset.service_id != service_id
                    or preset.row_version != expected_version):
                raise ValueError("drawing_preset_conflict")
            revision = session.get(BotDrawingRevisionRow, (preset_id, preset.current_revision))
            if (revision is None or revision.validation_state not in {"needs_preview", "ready"}
                    or not revision.dependency_signature):
                raise ValueError("drawing_dependency_unverified")
            if preset.kind == "image_edit":
                if not _valid_input(task_id, input_reference, input_sha256, input_bytes):
                    raise ValueError("drawing_request_invalid")
            elif any(value is not None for value in (input_reference, input_sha256, input_bytes)):
                raise ValueError("drawing_request_invalid")
            self._check_queue_capacity(session, endpoint_key)
            self._reserve_media(session, service_id, input_bytes or 0)
            now = datetime.now(UTC)
            task = BotDrawingTaskRow(id=task_id, service_id=service_id, operation="draw", origin="admin_preview",
                preset_id=preset.id, preset_revision=revision.revision, dependency_signature=revision.dependency_signature,
                drawing_version=settings.row_version, intent_mode="direct", positive=positive, negative=negative,
                seed=seed, input_reference=input_reference, input_sha256=input_sha256, input_bytes=input_bytes,
                endpoint_key=endpoint_key, comfy_endpoint=settings.comfy_base_url, prompt_id=uuid4(), audit_mode=mode, created_at=now,
                expires_at=now + timedelta(seconds=1800))
            session.add(task)
            session.flush()
            self.add_mutation(session, key, digest, {"task_id": str(task.id)}, bot_service_id=service_id)
            return task.id

    def mark_preview_validated(self, task_id: UUID, *, measured_peak_bytes: int) -> None:
        if type(measured_peak_bytes) is not int or measured_peak_bytes < 0:
            raise ValueError("drawing_preview_unverified")
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.origin != "admin_preview" or task.operation != "draw"
                    or task.generation_status != "succeeded" or task.cleanup_status != "done"
                    or task.resource_registration_state != "confirmed" or not task.prompt_transport_started
                    or not task.output_reference or not task.output_sha256 or not task.output_bytes):
                raise ValueError("drawing_preview_unverified")
            if task.audit_mode != "r18" and (task.audit_status != "allow" or task.audit_sha256 != task.output_sha256
                    or task.audit_model_revision != MODEL_REVISION or task.audit_policy_version != POLICY_VERSION):
                raise ValueError("drawing_preview_unverified")
            preset = session.get(BotDrawingPresetRow, task.preset_id, with_for_update=True)
            revision = session.get(BotDrawingRevisionRow, (task.preset_id, task.preset_revision), with_for_update=True)
            if (preset is None or preset.service_id != task.service_id or preset.current_revision != task.preset_revision
                    or revision is None or revision.dependency_signature != task.dependency_signature):
                raise ValueError("drawing_route_changed")
            revision.validation_state, revision.validated_at = "ready", datetime.now(UTC)
            revision.measured_peak_bytes = measured_peak_bytes
            task.media_expires_at = datetime.now(UTC) + timedelta(hours=24)
            task.reply_kind = "image"
            task.row_version += 1

    def admit(self, service_id: UUID, source: CommandSource, bot_self_id: str, endpoint_key: str,
              prepared: PreparedRoute, *, positive: str, negative: str, seed: int,
              task_id: UUID | None = None, input_reference: str | None = None, input_sha256: str | None = None,
              input_bytes: int | None = None, reference_message_id: str | None = None,
              wish_text: str | None = None) -> UUID:
        if (prepared.service_id != service_id or prepared.source !=
                (source.scope_kind, source.target_id, source.sender_id, source.message_id)
                or not isinstance(bot_self_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", bot_self_id)
                or not isinstance(endpoint_key, str) or not 1 <= len(endpoint_key) <= 128
                or not isinstance(positive, str) or not isinstance(negative, str)
                or len(positive) + len(negative) > 2000
                or prepared.intent.mode == "wish" and (not isinstance(wish_text, str)
                    or not wish_text.strip() or len(wish_text) > 2000)
                or prepared.intent.mode != "wish" and (wish_text is not None or not (positive.strip() or negative.strip()))
                or type(seed) is not int or not 0 <= seed < 2 ** 63
                or task_id is not None and not isinstance(task_id, UUID)
                or reference_message_id is not None and (not isinstance(reference_message_id, str)
                    or not re.fullmatch(r"-?[0-9]{1,64}", reference_message_id))):
            raise ValueError("drawing_request_invalid")
        if prepared.kind == "image_edit":
            if not _valid_input(task_id, input_reference, input_sha256, input_bytes):
                raise ValueError("drawing_request_invalid")
        elif any(value is not None for value in (input_reference, input_sha256, input_bytes)):
            raise ValueError("drawing_request_invalid")
        identity = hashlib.sha256(f"{bot_self_id}:{source.scope_kind}:{source.target_id}:{source.message_id}".encode()).hexdigest()
        key = self.mutation_key("drawing_event", service_id, identity)
        digest = fingerprint({"source": prepared.source, "bot_self": bot_self_id, "endpoint": endpoint_key,
            "intent": prepared.intent.model_dump(mode="json"), "route": prepared.decision.model_dump(mode="json"),
            "ai_version": prepared.ai_version, "drawing_version": prepared.drawing_version,
            "chat_profile_id": str(prepared.chat_profile_id) if prepared.chat_profile_id else None,
            "group_mode": prepared.group_mode, "preset_version": prepared.preset_row_version,
            "dependency_signature": prepared.dependency_signature,
            "positive": positive, "negative": negative, "wish_text": wish_text, "seed": seed, "input_reference": input_reference,
            "input_sha256": input_sha256, "input_bytes": input_bytes, "reference_message_id": reference_message_id})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return UUID(replay["task_id"])
            try:
                current = self._prepare_route(session, service_id, source, prepared.intent, prepared.kind,
                                              prepared.force_default, prepared.workflow_number)
            except ValueError as error:
                if str(error) in {"drawing_group_preset_denied", "drawing_scope_denied"}:
                    raise ValueError("drawing_route_changed") from error
                raise
            if current != prepared or current.decision.status != "selected":
                raise ValueError("drawing_route_changed")
            self._check_queue_capacity(session, endpoint_key)
            self._reserve_media(session, service_id, input_bytes or 0)
            chosen = current.decision.workflow
            settings = session.get(BotDrawingSettingsRow, service_id)
            preset = session.get(BotDrawingPresetRow, chosen.id)
            now = datetime.now(UTC)
            task = BotDrawingTaskRow(id=task_id or uuid4(), service_id=service_id, operation="draw", origin="qq",
                scope_kind=source.scope_kind, target_id=source.target_id, sender_id=source.sender_id,
                source_message_id=source.message_id, bot_self_id=bot_self_id, preset_id=chosen.id,
                preset_revision=chosen.revision, dependency_signature=current.dependency_signature,
                ai_version=current.ai_version, drawing_version=current.drawing_version,
                chat_profile_id=current.chat_profile_id,
                intent_mode=current.intent.mode, positive=positive, negative=negative, wish_text=wish_text,
                prompt_workflow_name=preset.name if wish_text is not None else None,
                prompt_aliases=list(chosen.lora_keys) if wish_text is not None else None,
                prompt_status="waiting" if wish_text is not None else "ready",
                requested_lora_keys=list(current.intent.lora_keys), ignored_lora_keys=list(current.decision.ignored_lora_keys),
                route_reason=current.decision.reason, seed=seed, input_reference=input_reference,
                input_sha256=input_sha256, input_bytes=input_bytes,
                reference_message_id=reference_message_id, endpoint_key=endpoint_key,
                comfy_endpoint=settings.comfy_base_url, prompt_id=uuid4(),
                audit_mode=current.group_mode, created_at=now, expires_at=now + timedelta(seconds=1800))
            session.add(task)
            session.flush()
            self.add_mutation(session, key, digest, {"task_id": str(task.id)}, bot_service_id=service_id)
            return task.id

    @staticmethod
    def _expire_queued(task, now):
        task.generation_status, task.finished_at = 'expired', now
        task.processing_stage = 'sending' if task.origin == 'qq' else 'finished'
        task.error_code, task.reply_kind = 'drawing_queue_expired', 'failure_notice'
        task.media_expires_at, task.next_check_at = now + timedelta(hours=24), None
        if task.origin == 'qq':
            task.send_status = 'ready'
        task.cleanup_status = ('done' if task.resource_registration_state == 'pending' and task.boot_id is None
                               else 'deferred' if task.resource_registration_state == 'unknown' else 'pending')
        task.row_version += 1

    def expire_next(self, endpoint_key: str) -> UUID | None:
        with self.sessions.begin() as session:
            self.lock(session)
            now = datetime.now(UTC)
            task = session.scalar(select(BotDrawingTaskRow).where(
                BotDrawingTaskRow.endpoint_key == endpoint_key, BotDrawingTaskRow.generation_status == 'queued',
                BotDrawingTaskRow.expires_at <= now).order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id)
                .limit(1).with_for_update(skip_locked=True))
            if task is None:
                return None
            self._expire_queued(task, now)
            return task.id

    def claim_next(self, endpoint_key: str, expected_task_id: UUID | None = None) -> ClaimedTask | None:
        with self.sessions.begin() as session:
            self.lock(session)
            active_query = select(BotDrawingTaskRow.id).where(
                BotDrawingTaskRow.endpoint_key == endpoint_key,
                (BotDrawingTaskRow.generation_status.in_(("submitting", "running", "cancelling", "reconciling")) |
                 BotDrawingTaskRow.cleanup_status.in_(("deferred", "failed"))))
            busy = session.scalar(select(exists(active_query)))
            if busy:
                return None
            query = select(BotDrawingTaskRow).where(BotDrawingTaskRow.endpoint_key == endpoint_key,
                BotDrawingTaskRow.generation_status == "queued")
            if expected_task_id is not None:
                query = query.where(BotDrawingTaskRow.id == expected_task_id)
            else:
                query = query.order_by(BotDrawingTaskRow.created_at, BotDrawingTaskRow.id)
            task = session.scalar(query.limit(1).with_for_update(skip_locked=True))
            if task is None:
                return None
            now = datetime.now(UTC)
            if task.expires_at is not None and task.expires_at <= now:
                self._expire_queued(task, now)
                return None
            if task.resource_registration_state != "confirmed":
                return None
            task.generation_status, task.started_at, task.processing_stage = "submitting", None, "submitting"
            task.row_version += 1
            return ClaimedTask(task.id, task.service_id, task.prompt_id, task.operation,
                               task.generation_status, task.row_version)

    def mark_native_started(self, task_id: UUID, *, expected_version: int) -> int:
        with self.sessions.begin() as session:
            version = session.scalar(update(BotDrawingTaskRow).where(
                BotDrawingTaskRow.id == task_id, BotDrawingTaskRow.row_version == expected_version,
                BotDrawingTaskRow.generation_status.in_(("running", "reconciling")),
                BotDrawingTaskRow.started_at.is_(None)).values(
                    started_at=datetime.now(UTC), processing_stage="executing",
                    row_version=BotDrawingTaskRow.row_version + 1)
                .returning(BotDrawingTaskRow.row_version))
            if version is None:
                raise ValueError("drawing_task_conflict")
            return version

    def mark_registration(self, task_id: UUID, *, expected_version: int, boot_id: str, execution_hash: str) -> int:
        if (not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 64 or not isinstance(execution_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", execution_hash)):
            raise ValueError("drawing_registration_invalid")
        with self.sessions.begin() as session:
            version = session.scalar(update(BotDrawingTaskRow).where(
                BotDrawingTaskRow.id == task_id, BotDrawingTaskRow.row_version == expected_version,
                BotDrawingTaskRow.generation_status.in_(("queued", "submitting")),
                BotDrawingTaskRow.resource_registration_state.in_(("pending", "unknown")),
                (BotDrawingTaskRow.boot_id.is_(None) | (BotDrawingTaskRow.boot_id == boot_id)),
                (BotDrawingTaskRow.execution_hash.is_(None) | (BotDrawingTaskRow.execution_hash == execution_hash)),
                BotDrawingTaskRow.prompt_transport_started.is_(False)).values(
                    resource_registration_state="confirmed", registered_at=datetime.now(UTC), boot_id=boot_id,
                    execution_hash=execution_hash, row_version=BotDrawingTaskRow.row_version + 1)
                .returning(BotDrawingTaskRow.row_version))
            if version is None:
                raise ValueError("drawing_task_conflict")
            return version

    def reserve_output(self, task_id: UUID, *, expected_version: int, size: int, checksum: str) -> int:
        if (type(size) is not int or not 0 < size <= 8 * 1024 * 1024 or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("drawing_output_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.row_version != expected_version
                    or task.cancel_requested or (task.generation_status not in {"running", "reconciling", "cancelling"}
                        and not (task.endpoint_key == ONLINE_ENDPOINT and task.generation_status == "submitting"
                                 and task.online_request_state == "received"))):
                raise ValueError("drawing_task_conflict")
            if task.output_bytes is not None:
                if task.output_bytes != size or task.output_sha256 != checksum:
                    raise ValueError("drawing_media_conflict")
                return task.row_version
            self._reserve_media(session, task.service_id, size)
            task.output_bytes, task.output_sha256, task.row_version = size, checksum, task.row_version + 1
            return task.row_version

    def registration_unknown(self, task_id: UUID, *, expected_version: int, boot_id: str, execution_hash: str) -> int:
        if (not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 64 or not isinstance(execution_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", execution_hash)):
            raise ValueError("drawing_registration_invalid")
        with self.sessions.begin() as session:
            version = session.scalar(update(BotDrawingTaskRow).where(
                BotDrawingTaskRow.id == task_id, BotDrawingTaskRow.row_version == expected_version,
                BotDrawingTaskRow.generation_status == "queued",
                BotDrawingTaskRow.resource_registration_state.in_(("pending", "unknown")),
                (BotDrawingTaskRow.boot_id.is_(None) | (BotDrawingTaskRow.boot_id == boot_id)),
                (BotDrawingTaskRow.execution_hash.is_(None) | (BotDrawingTaskRow.execution_hash == execution_hash)),
                BotDrawingTaskRow.prompt_transport_started.is_(False)).values(
                    resource_registration_state="unknown", boot_id=boot_id, execution_hash=execution_hash,
                    next_check_at=datetime.now(UTC) + timedelta(seconds=5), row_version=BotDrawingTaskRow.row_version + 1)
                .returning(BotDrawingTaskRow.row_version))
            if version is None:
                raise ValueError("drawing_task_conflict")
            return version

    def begin_submit(self, task_id: UUID, *, expected_version: int) -> int:
        with self.sessions.begin() as session:
            version = session.scalar(update(BotDrawingTaskRow).where(
                BotDrawingTaskRow.id == task_id, BotDrawingTaskRow.row_version == expected_version,
                BotDrawingTaskRow.generation_status == "submitting",
                BotDrawingTaskRow.resource_registration_state == "confirmed",
                BotDrawingTaskRow.prompt_transport_started.is_(False)).values(
                    prompt_transport_started=True, row_version=BotDrawingTaskRow.row_version + 1)
                .returning(BotDrawingTaskRow.row_version))
            if version is None:
                raise ValueError("drawing_task_conflict")
            return version

    def transition(self, task_id: UUID, before: str, after: str, *, expected_version: int, **values) -> int:
        if after not in _TRANSITIONS.get(before, set()) or set(values) - _TRANSITION_FIELDS:
            raise ValueError("drawing_transition_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.generation_status != before or task.row_version != expected_version:
                raise ValueError("drawing_task_conflict")
            if before == "submitting" and after == "succeeded" and task.endpoint_key != ONLINE_ENDPOINT:
                raise ValueError("drawing_transition_invalid")
            if after == "running" and (task.resource_registration_state != "confirmed"
                                       or not task.prompt_transport_started
                                       or not isinstance(task.execution_hash, str)
                                       or not re.fullmatch(r"[0-9a-f]{64}", task.execution_hash)):
                raise ValueError("drawing_task_not_registered")
            if after == "succeeded" and task.operation == "draw":
                reference = values.get("output_reference", task.output_reference)
                checksum = values.get("output_sha256", task.output_sha256)
                size = values.get("output_bytes", task.output_bytes)
                if (not isinstance(reference, str) or not re.fullmatch(
                        rf"al1s-drawing/{task.id}/output\.(?:png|jpg|jpeg|webp|gif)", reference)
                        or not re.fullmatch(r"[0-9a-f]{64}", checksum or "")
                        or type(size) is not int or not 0 < size <= 8 * 1024 * 1024):
                    raise ValueError("drawing_output_invalid")
                if task.output_bytes != size or task.output_sha256 != checksum:
                    raise ValueError("drawing_media_not_reserved")
            if after == "succeeded" and task.operation == "tag":
                tags = values.get("tag_result", task.tag_result)
                if not isinstance(tags, str) or not tags.strip() or len(tags) > 2500:
                    raise ValueError("drawing_tagger_output_invalid")
            for name, value in values.items():
                setattr(task, name, value)
            task.processing_stage = {"running": "waiting_native", "reconciling": "reconciling",
                "cancelling": "cancelling", "succeeded": "cleaning", "failed": "finished",
                "cancelled": "finished"}.get(after, task.processing_stage)
            if after in {"failed", "cancelled"} and task.prompt_status in {"waiting", "working"}:
                task.prompt_status = "failed"
            task.generation_status, task.row_version = after, task.row_version + 1
            return task.row_version

    def set_result_ready(self, task_id: UUID, *, expected_version: int, reply_kind: str) -> int:
        if reply_kind not in {"image", "tag_text", "failure_notice"}:
            raise ValueError("drawing_reply_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.row_version != expected_version or task.send_status != "none":
                raise ValueError("drawing_task_conflict")
            if (reply_kind == "image" and (task.operation != "draw" or task.generation_status != "succeeded")
                    or reply_kind == "tag_text" and (task.operation != "tag" or task.generation_status != "succeeded")
                    or reply_kind == "failure_notice" and task.generation_status not in {"succeeded", "failed", "expired", "cancelled"}):
                raise ValueError("drawing_reply_invalid")
            task.reply_kind = reply_kind
            if reply_kind == "failure_notice" and task.error_code is None:
                failure = task.input_audit_status if task.operation == "tag" else task.audit_status
                task.error_code = "drawing_audit_blocked" if failure == "block" else "drawing_audit_unknown"
            task.send_status = "ready" if task.origin == "qq" else "none"
            task.processing_stage = "sending" if task.origin == "qq" else "finished"
            if task.media_expires_at is None:
                task.media_expires_at = datetime.now(UTC) + timedelta(hours=24)
            task.row_version += 1
            return task.row_version

    def mark_cleanup(self, task_id: UUID, *, expected_version: int, status: str) -> int:
        if status not in {"pending", "done", "deferred", "failed"}:
            raise ValueError("drawing_cleanup_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.row_version != expected_version:
                raise ValueError("drawing_task_conflict")
            if status == "done" and task.generation_status in {"submitting", "running", "cancelling", "reconciling"}:
                raise ValueError("drawing_cleanup_unconfirmed")
            task.cleanup_status, task.row_version = status, task.row_version + 1
            if status == "done" and task.generation_status in {"succeeded", "failed", "cancelled", "expired"} \
                    and (task.origin == "admin_preview" or task.send_status in {"sent", "failed", "unknown"}):
                task.processing_stage = "finished" if task.send_status != "unknown" else "delivery_unknown"
            return task.row_version

    def allowed(self, service_id: UUID, source: CommandSource, drawing_version: int) -> bool:
        with self.sessions() as session:
            try:
                self._allowed(session, service_id, source, require_applied=False)
            except ValueError:
                return False
            return True

    def record_audit(self, task_id: UUID, *, expected_version: int, decision: AuditDecision) -> int:
        if (not isinstance(decision, AuditDecision) or decision.status not in {"allow", "block", "unknown"}
                or decision.model_revision != MODEL_REVISION or decision.policy_version != POLICY_VERSION):
            raise ValueError("drawing_audit_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.row_version != expected_version:
                raise ValueError("drawing_task_conflict")
            subject = task.output_sha256
            if not subject or subject != decision.image_sha256:
                raise ValueError("drawing_audit_image_changed")
            task.audit_status, task.audit_sha256 = decision.status, decision.image_sha256
            task.audit_model_revision, task.audit_policy_version = decision.model_revision, decision.policy_version
            task.row_version += 1
            return task.row_version

    def record_input_audit(self, task_id: UUID, *, expected_version: int, decision: AuditDecision) -> int:
        if (not isinstance(decision, AuditDecision) or decision.status not in {"allow", "block", "unknown"}
                or decision.model_revision != MODEL_REVISION or decision.policy_version != POLICY_VERSION):
            raise ValueError("drawing_audit_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.row_version != expected_version:
                raise ValueError("drawing_task_conflict")
            if not task.input_sha256 or task.input_sha256 != decision.image_sha256:
                raise ValueError("drawing_audit_image_changed")
            task.input_audit_status, task.input_audit_sha256 = decision.status, decision.image_sha256
            task.input_audit_model_revision, task.input_audit_policy_version = decision.model_revision, decision.policy_version
            if task.operation == "tag":
                task.audit_status, task.audit_sha256 = decision.status, decision.image_sha256
                task.audit_model_revision, task.audit_policy_version = decision.model_revision, decision.policy_version
            task.row_version += 1
            return task.row_version

    def start_send(self, task_id: UUID, kind: str) -> bool:
        if kind not in {"notice", "result"}:
            raise ValueError("drawing_send_invalid")
        with self.sessions.begin() as session:
            field = BotDrawingTaskRow.notice_status if kind == "notice" else BotDrawingTaskRow.send_status
            started = BotDrawingTaskRow.notice_transport_started if kind == "notice" else BotDrawingTaskRow.result_transport_started
            conditions = [BotDrawingTaskRow.id == task_id, BotDrawingTaskRow.origin == "qq",
                          BotDrawingTaskRow.deletion_state == "visible",
                          field.in_(("none", "ready")), started.is_(False)]
            if kind == "notice":
                conditions.extend([BotDrawingTaskRow.send_status == "none",
                    BotDrawingTaskRow.generation_status.in_(("queued", "submitting", "running")),
                    BotDrawingTaskRow.expires_at.is_(None) | (BotDrawingTaskRow.expires_at > datetime.now(UTC))])
            else:
                conditions.append(BotDrawingTaskRow.reply_kind.is_not(None))
            values = {"notice_status" if kind == "notice" else "send_status": "sending",
                      "row_version": BotDrawingTaskRow.row_version + 1}
            return session.scalar(update(BotDrawingTaskRow).where(*conditions).values(**values)
                                  .returning(BotDrawingTaskRow.id)) is not None

    def mark_transport_started(self, task_id: UUID, kind: str, digest: str | None) -> bool:
        if kind not in {"notice", "result"}:
            raise ValueError("drawing_send_invalid")
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.origin != "qq" or task.deletion_state != "visible":
                return False
            state = task.notice_status if kind == "notice" else task.send_status
            started = task.notice_transport_started if kind == "notice" else task.result_transport_started
            if state != "sending" or started:
                return False
            source = CommandSource(message_id=task.source_message_id, scope_kind=task.scope_kind,
                                   target_id=task.target_id, sender_id=task.sender_id)
            try:
                self._allowed(session, task.service_id, source, require_applied=False)
            except ValueError:
                return False
            if kind == "result":
                mode = "green"
                if task.scope_kind == "qq_group":
                    row = session.get(BotDrawingGroupModeRow, (task.service_id, task.target_id))
                    mode = row.mode if row else "green"
                if task.reply_kind == "image":
                    if task.cancel_requested:
                        return False
                    preset = session.get(BotDrawingPresetRow, task.preset_id)
                    if preset is None or preset.service_id != task.service_id or not preset.enabled:
                        return False
                    if task.endpoint_key == ONLINE_ENDPOINT and task.scope_kind == "qq_group":
                        group = session.get(BotDrawingGroupModeRow, (task.service_id, task.target_id))
                        if group and group.restrict_workflows and session.get(
                                BotDrawingGroupPresetRow, (task.service_id, task.target_id, task.preset_id)) is None:
                            return False
                    if task.generation_status != "succeeded" or digest != task.output_sha256 or digest is None:
                        return False
                    audited_hash = task.output_sha256
                elif task.reply_kind == "tag_text":
                    if task.cancel_requested:
                        return False
                    if task.generation_status != "succeeded" or digest is not None or not task.tag_result:
                        return False
                    audited_hash = task.input_sha256
                elif task.reply_kind == "failure_notice" and digest is None:
                    audited_hash = None
                else:
                    return False
                if mode == "green" and audited_hash is not None:
                    if task.reply_kind == "tag_text":
                        valid_audit = (task.input_audit_status == "allow" and task.input_audit_sha256 == audited_hash
                            and task.input_audit_model_revision == MODEL_REVISION
                            and task.input_audit_policy_version == POLICY_VERSION)
                    else:
                        valid_audit = (task.audit_status == "allow" and task.audit_sha256 == audited_hash
                            and task.audit_model_revision == MODEL_REVISION and task.audit_policy_version == POLICY_VERSION)
                    if not valid_audit:
                        return False
                task.result_transport_started = True
            else:
                if (digest is not None or task.send_status != "none"
                        or task.generation_status not in {"queued", "submitting", "running"}
                        or task.expires_at is not None and task.expires_at <= datetime.now(UTC)):
                    return False
                task.notice_transport_started = True
            task.row_version += 1
            return True

    def finish_send(self, task_id: UUID, kind: str, status: str, count: int, receipt: str | None) -> bool:
        if (kind not in {"notice", "result"} or status not in {"sent", "failed", "unknown"}
                or count not in {0, 1} or status == "sent" and (count != 1 or not isinstance(receipt, str)
                                                                 or not receipt or len(receipt) > 64)):
            raise ValueError("drawing_send_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or (task.notice_status if kind == "notice" else task.send_status) != "sending":
                return False
            started = task.notice_transport_started if kind == "notice" else task.result_transport_started
            if status == "sent" and not started:
                return False
            if status == "failed" and started:
                status = "unknown"
            if kind == "notice":
                task.notice_status, task.notice_receipt = status, receipt if status == "sent" else None
            else:
                task.send_status, task.result_receipt = status, receipt if status == "sent" else None
                task.processing_stage = "delivery_unknown" if status == "unknown" else "finished"
                if status == "sent":
                    task.media_expires_at = datetime.now(UTC) + timedelta(hours=1)
            task.row_version += 1
            return True

    def requeue_result(self, task_id: UUID, reply_kind: str) -> bool:
        if reply_kind not in {"image", "tag_text", "failure_notice"}:
            raise ValueError("drawing_reply_invalid")
        with self.sessions.begin() as session:
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.origin != "qq" or task.send_status != "failed"
                    or task.result_transport_started):
                return False
            if (reply_kind == "image" and (task.operation != "draw" or task.generation_status != "succeeded")
                    or reply_kind == "tag_text" and (task.operation != "tag" or task.generation_status != "succeeded")):
                return False
            task.reply_kind, task.send_status, task.row_version = reply_kind, "ready", task.row_version + 1
            return True

    def request_cancel(self, service_id: UUID, task_id: UUID, *, operation_id: str) -> str:
        key = self.mutation_key("drawing_cancel", service_id, operation_id)
        digest = fingerprint({"task_id": str(task_id)})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay["status"]
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.service_id != service_id:
                raise ValueError("drawing_task_not_found")
            if task.generation_status == "succeeded" and task.result_transport_started:
                status = "transport_started"
            elif (task.generation_status == "succeeded" and task.send_status in {"none", "ready", "sending"}
                    and not task.cancel_requested):
                task.cancel_requested = True
                audit_running = task.processing_stage in {"input_audit", "output_audit"}
                task.error_code = "drawing_cancelled"
                task.reply_kind = None if audit_running else "failure_notice"
                if audit_running:
                    task.cleanup_status, task.processing_stage = "deferred", "waiting_audit_stop"
                    if task.send_status == "ready":
                        task.send_status = "none"
                elif task.origin == "qq" and task.send_status != "sending":
                    task.send_status = "ready"
                if not audit_running:
                    task.processing_stage = ("cancelling" if task.send_status == "sending" else
                                             "sending" if task.origin == "qq" else "finished")
                task.row_version += 1
                status = "cancelled"
            elif task.generation_status == "succeeded" and task.cancel_requested:
                status = "cancelled"
            elif task.generation_status in {"succeeded", "failed", "expired", "cancelled"}:
                status = task.generation_status
            elif task.endpoint_key == ONLINE_ENDPOINT and task.prompt_transport_started:
                task.generation_status, task.cancel_requested = "cancelled", True
                if task.online_request_state == "submitting":
                    task.online_request_state = "unknown"
                task.finished_at, task.cleanup_status = datetime.now(UTC), "done"
                task.media_expires_at = datetime.now(UTC) + timedelta(hours=24)
                task.error_code, task.reply_kind = "drawing_cancelled", "failure_notice"
                task.send_status = "ready" if task.origin == "qq" else "none"
                task.processing_stage = "sending" if task.origin == "qq" else "finished"
                task.row_version += 1
                status = "cancelled"
            elif not task.prompt_transport_started:
                audit_running = task.processing_stage == "input_audit"
                task.generation_status = "cancelled"
                task.cancel_requested = True
                task.finished_at = datetime.now(UTC)
                task.cleanup_status = ("done" if not audit_running and task.resource_registration_state == "pending" and task.boot_id is None
                                       else "deferred")
                task.error_code = "drawing_cancelled"
                task.reply_kind = None if audit_running else "failure_notice"
                task.media_expires_at = datetime.now(UTC) + timedelta(hours=24)
                if task.origin == "qq" and not audit_running:
                    task.send_status = "ready"
                task.processing_stage = ("waiting_audit_stop" if audit_running else
                                         "sending" if task.origin == "qq" else "finished")
                if task.prompt_status in {"waiting", "working"}:
                    task.prompt_status = "failed"
                task.row_version += 1
                status = "cancelled"
            else:
                task.generation_status, task.row_version = "cancelling", task.row_version + 1
                task.cancel_requested, task.processing_stage = True, "cancelling"
                task.next_check_at = None
                status = "cancelling"
            self.add_mutation(session, key, digest, {"status": status}, bot_service_id=service_id)
            return status

    def finish_restart_cancel(
        self, task_id: UUID, *, expected_version: int, drawing_version: int, endpoint: str | None = None
    ) -> None:
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.row_version != expected_version
                    or task.generation_status != "cancelling" or not task.prompt_transport_started
                    or not task.boot_id or task.drawing_version != drawing_version
                    or task.send_status != "none"):
                raise ValueError("drawing_task_conflict")
            if endpoint is not None:
                if task.comfy_endpoint != endpoint:
                    raise ValueError("drawing_task_conflict")
            else:
                settings = session.get(BotDrawingSettingsRow, task.service_id, with_for_update=True)
                if (settings is None or settings.row_version != drawing_version
                        or settings.applied_version != drawing_version):
                    raise ValueError("drawing_task_conflict")
            now = datetime.now(UTC)
            task.generation_status, task.cleanup_status = "cancelled", "done"
            task.cancel_requested, task.processing_stage = True, "sending" if task.origin == "qq" else "finished"
            task.finished_at, task.error_code, task.reply_kind = now, "drawing_cancelled", "failure_notice"
            task.media_expires_at = task.media_expires_at or now + timedelta(hours=24)
            if task.origin == "qq":
                task.send_status = "ready"
            task.row_version += 1

    def reset_unsubmitted_after_restart(self, task_id: UUID, *, expected_version: int,
                                        boot_id: str, endpoint: str | None) -> None:
        with self.sessions.begin() as session:
            self.lock(session)
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if (task is None or task.row_version != expected_version or task.boot_id != boot_id
                    or task.generation_status not in {"queued", "submitting"}
                    or task.prompt_transport_started or task.cancel_requested):
                raise ValueError("drawing_task_conflict")
            if endpoint is not None:
                if task.comfy_endpoint != endpoint:
                    raise ValueError("drawing_task_conflict")
            else:
                settings = session.get(BotDrawingSettingsRow, task.service_id, with_for_update=True)
                if (settings is None or settings.row_version != task.drawing_version
                        or settings.applied_version != task.drawing_version):
                    raise ValueError("drawing_task_conflict")
            task.generation_status, task.resource_registration_state = "queued", "pending"
            task.boot_id, task.registered_at, task.next_check_at = None, None, None
            task.processing_stage = "preparing_resources"
            task.row_version += 1

    def retry_send(self, service_id: UUID, task_id: UUID, *, operation_id: str) -> bool:
        key = self.mutation_key("drawing_retry_send", service_id, operation_id)
        digest = fingerprint({"task_id": str(task_id)})
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return replay["queued"]
            task = session.get(BotDrawingTaskRow, task_id, with_for_update=True)
            if task is None or task.service_id != service_id:
                raise ValueError("drawing_task_not_found")
            if (task.origin != "qq" or task.send_status != "failed" or task.result_transport_started
                    or task.reply_kind not in {"image", "tag_text", "failure_notice"}
                    or task.reply_kind == "image" and not task.output_reference
                    or task.reply_kind == "tag_text" and not task.input_reference):
                raise ValueError("drawing_send_retry_invalid")
            task.send_status, task.row_version = "ready", task.row_version + 1
            self.add_mutation(session, key, digest, {"queued": True}, bot_service_id=service_id)
            return True
