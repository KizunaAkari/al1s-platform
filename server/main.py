import base64
import binascii
import hashlib
import json
import logging
import math
import shutil
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request as UrlRequest, urlopen
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .compatibility import analyze_script_compatibility
from .emailer import (
    SMTPConfiguration,
    environment_configuration,
    failure_notifications_configured,
    send_failure_notification,
    send_message,
    smtp_configured,
)
from .schemas import (
    AgentRegistration,
    CommandResult,
    DeviceControl,
    EmailMessage,
    Heartbeat,
    InteractiveStart,
    InteractiveStop,
    LogCreate,
    MaintenanceCleanup,
    NotificationSettingsUpdate,
    QuickTestPayload,
    ScriptCategoryMove,
    ScriptCategoryUpdate,
    ScriptImportPayload,
    ScriptPayload,
    ScriptStepPayload,
    TaskBatchCreate,
    TaskBatchItem,
    TaskCompositionCreate,
    TaskCompositionModule,
    TaskCreate,
    TerminalDeployRequest,
    TestEmailRequest,
)
from .secret_store import SecretBox
from .store import store, utc_now
from .terminal_deploy import TerminalArtifactStore

log = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"
APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.is_file() else "0.0.0"
app = FastAPI(title=settings.app_name, version=APP_VERSION)
configured_frontend = Path(settings.frontend_dist)
FRONTEND_DIR = configured_frontend if configured_frontend.is_absolute() else ROOT_DIR / configured_frontend
SCREEN_DIR = Path(settings.screen_dir)
SCREEN_DIR.mkdir(parents=True, exist_ok=True)
FAILURE_DIR = SCREEN_DIR.parent / "failures"
FAILURE_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_TEMP_DIR = SCREEN_DIR.parent / "feedback-temp"
FEEDBACK_TEMP_DIR.mkdir(parents=True, exist_ok=True)
RECORDING_DIR = Path(settings.recording_dir)
RECORDING_DIR.mkdir(parents=True, exist_ok=True)
TERMINAL_ARTIFACTS = TerminalArtifactStore(settings.terminal_artifact_dir)
notification_secrets = SecretBox(settings.notification_key_path)
maintenance_stop = threading.Event()

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if (FRONTEND_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="frontend-assets")


def agent_auth(authorization: str | None = Header(default=None)):
    expected = f"Bearer {settings.agent_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid agent token")


def get_agent_or_404(agent_id: str):
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


def is_tronlong_npu_agent(agent: dict) -> bool:
    capabilities = agent.get("capabilities") or {}
    metadata = agent.get("metadata") or {}
    machine = str(metadata.get("machine") or metadata.get("architecture") or "").lower()
    marker = " ".join(
        str(value or "")
        for value in (
            agent.get("id"),
            agent.get("name"),
            metadata.get("kernel"),
            (metadata.get("yolo") or {}).get("provider") if isinstance(metadata.get("yolo"), dict) else "",
        )
    ).lower()
    is_arm64 = machine in {"aarch64", "arm64"} or "rk3576" in marker
    return capabilities.get("yolo") is True and is_arm64 and (
        "rk3576" in marker or "创龙" in marker or "tronlong" in marker
    )


def terminal_deployer_url(agent: dict, path: str = "") -> str:
    address = str(agent.get("address") or "")
    parsed = urlsplit(address if "://" in address else f"http://{address}")
    if not parsed.hostname:
        raise HTTPException(status_code=422, detail="终端没有可用的部署地址")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.terminal_deployer_port}{path}"


def call_terminal_deployer(agent: dict, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = UrlRequest(
        terminal_deployer_url(agent, path),
        data=body,
        method="POST" if body is not None else "GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.terminal_deploy_token}",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"终端部署服务不可用：{exc}") from exc


def terminal_artifact_public_url() -> str:
    return (settings.terminal_deploy_public_url or settings.public_base_url).rstrip("/")


def screen_path(agent_id: str) -> Path:
    safe_name = hashlib.sha256(agent_id.encode()).hexdigest()[:24]
    return SCREEN_DIR / f"{safe_name}.png"


def failure_screen_path(failure_id: str) -> Path:
    safe_name = hashlib.sha256(failure_id.encode()).hexdigest()[:32]
    return FAILURE_DIR / f"{safe_name}.png"


def persist_latest_screen(agent_id: str, result: dict) -> dict:
    encoded = result.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("screenshot result does not contain data_base64")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("screenshot result contains invalid base64") from exc
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("screenshot result is not a PNG image")
    target = screen_path(agent_id)
    temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.replace(target)
    compact = {key: value for key, value in result.items() if key != "data_base64"}
    compact["screen_url"] = f"/api/agents/{agent_id}/screen"
    compact["captured_at"] = utc_now()
    return compact


def public_failure(item: dict) -> dict:
    value = dict(item)
    screenshot_path = value.pop("screenshot_path", None)
    if screenshot_path:
        value["screenshot_url"] = f"/api/failures/{value['id']}/screenshot"
    return value


def public_task(item: dict) -> dict:
    value = dict(item)
    recording_path = value.pop("recording_path", None)
    if recording_path:
        value["recording_url"] = f"/api/tasks/{value['id']}/recording"
    return value


def safe_remove_managed_file(path_value: str | None, root: Path) -> int:
    if not path_value:
        return 0
    target = Path(path_value).resolve()
    managed_root = root.resolve()
    if target != managed_root and managed_root not in target.parents:
        return 0
    try:
        size = target.stat().st_size
        target.unlink()
        return size
    except FileNotFoundError:
        return 0


def parse_script_document(content: str) -> dict:
    try:
        script_document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"任务脚本不是有效 JSON：{exc.msg}") from exc
    if not isinstance(script_document, dict) or not isinstance(script_document.get("steps"), list):
        raise HTTPException(status_code=422, detail="任务脚本必须是包含 steps 数组的 JSON 对象")
    return script_document


def script_type_of(document: dict) -> str:
    value = str(document.get("script_type") or "standard")
    if value not in {"standard", "module_start", "module_process", "composition"}:
        raise HTTPException(status_code=422, detail=f"不支持的脚本类型：{value}")
    return value


def validate_post_assertion(step: dict, position: int) -> None:
    assertion = step.get("post_assertion")
    if assertion is None:
        return
    if not isinstance(assertion, dict):
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的执行后断言必须是对象",
        )
    enabled = assertion.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的执行后断言 enabled 必须是布尔值",
        )
    if not enabled:
        return
    if not str(assertion.get("template_base64") or "").strip():
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步开启了执行后断言，但没有截取断言图片",
        )
    raw_max_retries = assertion.get("max_retries", 2)
    try:
        threshold = float(assertion.get("threshold", 0.85))
        timeout_seconds = float(assertion.get("timeout_seconds", 3))
        poll_interval_seconds = float(assertion.get("poll_interval_seconds", 0.5))
        max_retries = int(raw_max_retries)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的执行后断言参数格式无效",
        ) from exc
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的断言匹配阈值必须在 0 到 1 之间",
        )
    if not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 300:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的单次断言等待必须在 0.1 到 300 秒之间",
        )
    if not math.isfinite(poll_interval_seconds) or not 0.05 <= poll_interval_seconds <= 10:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的断言轮询间隔必须在 0.05 到 10 秒之间",
        )
    try:
        retries_are_integral = (
            not isinstance(raw_max_retries, bool)
            and math.isfinite(float(raw_max_retries))
            and float(raw_max_retries) == max_retries
        )
    except (TypeError, ValueError):
        retries_are_integral = False
    if not retries_are_integral or not 1 <= max_retries <= 20:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position} 步的断言重试次数必须是 1 到 20 之间的整数",
        )


def validate_failure_retry(steps: list[dict], position: int) -> None:
    step = steps[position]
    config = step.get("failure_retry")
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return
    if str(step.get("action") or "") == "start":
        raise HTTPException(status_code=422, detail="【开始】步骤不能配置失败重试过程脚本")
    process_script_name = str(config.get("process_script_name") or "").strip()
    if not process_script_name:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position + 1} 步开启了失败重试，但没有选择过程脚本",
        )
    if len(process_script_name) > 200:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position + 1} 步引用的过程脚本名称过长",
        )
    raw_max_retries = config.get("max_retries", 2)
    try:
        max_retries = int(raw_max_retries)
        integral = (
            not isinstance(raw_max_retries, bool)
            and math.isfinite(float(raw_max_retries))
            and float(raw_max_retries) == max_retries
        )
    except (TypeError, ValueError):
        integral = False
        max_retries = 0
    if not integral or not 1 <= max_retries <= 20:
        raise HTTPException(
            status_code=422,
            detail=f"第 {position + 1} 步的失败重试次数必须是 1 到 20 之间的整数",
        )


def resolve_failure_retry_scripts(
    agent_id: str,
    content: str,
    *,
    reference_chain: tuple[str, ...] = (),
) -> str:
    document = validate_task_script(content)
    _resolve_failure_retry_document(agent_id, document, reference_chain)
    return json.dumps(document, ensure_ascii=False)


def _resolve_failure_retry_document(
    agent_id: str,
    document: dict,
    reference_chain: tuple[str, ...],
) -> None:
    if len(reference_chain) > 10:
        raise HTTPException(status_code=422, detail="失败重试过程脚本的嵌套层级不能超过 10 层")
    for position, step in enumerate(document.get("steps") or [], start=1):
        config = step.get("failure_retry")
        if not isinstance(config, dict) or config.get("enabled") is not True:
            continue
        process_script_name = str(config.get("process_script_name") or "").strip()
        if process_script_name in reference_chain:
            chain = " → ".join((*reference_chain, process_script_name))
            raise HTTPException(
                status_code=422,
                detail=f"失败重试过程脚本存在循环引用：{chain}",
            )
        item = store.get_script(agent_id, process_script_name)
        if not item:
            raise HTTPException(
                status_code=422,
                detail=f"第 {position} 步引用的失败重试过程脚本不存在：{process_script_name}",
            )
        process_document = validate_task_script(
            str(item.get("content") or ""),
            {"module_process"},
        )
        if bool(process_document.get("cleanup_on_finish", False)):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"失败重试过程脚本【{process_script_name}】启用了结束清理；"
                    "恢复后还要重试原步骤，请改为保留应用"
                ),
            )
        _resolve_failure_retry_document(
            agent_id,
            process_document,
            (*reference_chain, process_script_name),
        )
        config["process_script"] = process_document


def validate_task_script(content: str, allowed_types: set[str] | None = None) -> dict:
    document = parse_script_document(content)
    script_type = script_type_of(document)
    if allowed_types is not None and script_type not in allowed_types:
        if allowed_types == {"standard"}:
            raise HTTPException(status_code=422, detail="普通任务只能下发普通脚本；模块化脚本请使用组合任务")
        raise HTTPException(status_code=422, detail="脚本类型不符合当前任务要求")

    steps = document["steps"]
    actions = [str(step.get("action") or "") for step in steps if isinstance(step, dict)]
    if len(actions) != len(steps):
        raise HTTPException(status_code=422, detail="脚本步骤必须全部是对象")
    for position, step in enumerate(steps, start=1):
        validate_post_assertion(step, position)
    for position in range(len(steps)):
        validate_failure_retry(steps, position)
    try:
        version = int(document.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="脚本 version 必须是整数") from exc
    if script_type in {"module_start", "module_process"} and version < 2:
        raise HTTPException(status_code=422, detail="模块化脚本必须使用 DSL v2")
    if script_type == "module_start":
        if not actions or actions[0] != "start" or actions.count("start") != 1:
            raise HTTPException(status_code=422, detail="开始脚本必须且只能以一个【开始】步骤开头")
        if "launch_app" not in actions:
            raise HTTPException(status_code=422, detail="开始脚本必须包含【打开应用】步骤")
    elif script_type == "module_process":
        if not actions:
            raise HTTPException(status_code=422, detail="过程脚本至少需要一个步骤")
        forbidden = sorted({action for action in actions if action in {"start", "launch_app"}})
        if forbidden:
            raise HTTPException(status_code=422, detail="过程脚本不能包含【开始】或【打开应用】步骤")
        cleanup = document.get("cleanup_on_finish", False)
        if not isinstance(cleanup, bool):
            raise HTTPException(status_code=422, detail="过程脚本 cleanup_on_finish 必须是布尔值")
    return document


def public_script(item: dict, device: dict | None = None) -> dict:
    value = dict(item)
    try:
        document = validate_task_script(
            str(item.get("content") or ""),
            {"standard", "module_start", "module_process"},
        )
        value["script_type"] = script_type_of(document)
        value["cleanup_on_finish"] = bool(document.get("cleanup_on_finish", False))
        value["valid"] = True
    except HTTPException as exc:
        value["script_type"] = "invalid"
        value["cleanup_on_finish"] = False
        value["valid"] = False
        value["validation_error"] = str(exc.detail)
    if device is not None:
        value["compatibility"] = analyze_script_compatibility(
            str(item.get("content") or ""),
            device,
        )
    return value


def normalized_script_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise HTTPException(status_code=422, detail="脚本名称不能为空")
    if len(value) > 240 or any(character in value for character in ("/", "\\", "\0")):
        raise HTTPException(status_code=422, detail="脚本名称包含不允许的字符")
    return value if value.lower().endswith(".json") else f"{value}.json"


def normalized_android_component(package_value: object, activity_value: object = "") -> tuple[str, str]:
    package_name = str(package_value or "").strip()
    activity = str(activity_value or "").strip()
    if "/" in package_name:
        package_name, embedded_activity = package_name.split("/", 1)
        activity = activity or embedded_activity.strip()
    package_name = package_name.strip()
    if (
        not package_name
        or len(package_name) > 255
        or any(character.isspace() for character in package_name)
        or any(character in package_name for character in ("/", "\\", "\0"))
    ):
        raise HTTPException(status_code=422, detail="Android 包名格式无效")
    if len(activity) > 500 or any(character.isspace() for character in activity):
        raise HTTPException(status_code=422, detail="Activity 格式无效")
    return package_name, activity


def script_application(document: dict) -> tuple[str, str] | None:
    for step in document.get("steps", []):
        if not isinstance(step, dict) or step.get("action") != "launch_app":
            continue
        if not str(step.get("package") or "").strip():
            return None
        return normalized_android_component(step.get("package"), step.get("activity"))
    return None


def script_save_metadata(agent_id: str, name: str, document: dict) -> tuple[str | None, str | None, str | None]:
    application = script_application(document)
    if application:
        package_name, activity = application
        return package_name, package_name, activity or None
    previous = store.get_script(agent_id, name)
    if previous:
        return (
            previous.get("category_package"),
            previous.get("source_package"),
            previous.get("source_activity"),
        )
    return None, None, None


def script_references(agent_id: str, target_name: str) -> list[str]:
    references = []
    for item in store.list_scripts(agent_id):
        if item["name"] == target_name:
            continue
        try:
            document = json.loads(str(item.get("content") or ""))
        except json.JSONDecodeError:
            continue
        for step in document.get("steps", []):
            retry = step.get("failure_retry") if isinstance(step, dict) else None
            if (
                isinstance(retry, dict)
                and retry.get("enabled") is True
                and str(retry.get("process_script_name") or "").strip() == target_name
            ):
                references.append(item["name"])
                break
    return references


def compile_composition(agent_id: str, modules: list) -> tuple[str, list[dict]]:
    documents: list[tuple[object, dict]] = []
    for module in modules:
        item = store.get_script(agent_id, module.script_name)
        if not item:
            raise HTTPException(status_code=422, detail=f"组合脚本不存在：{module.script_name}")
        document = validate_task_script(item["content"], {"module_start", "module_process"})
        _resolve_failure_retry_document(agent_id, document, (module.script_name,))
        documents.append((module, document))

    if script_type_of(documents[0][1]) != "module_start":
        raise HTTPException(status_code=422, detail="组合任务第一个脚本必须是开始脚本")
    for index, (_module, document) in enumerate(documents[1:], start=2):
        if script_type_of(document) != "module_process":
            raise HTTPException(status_code=422, detail=f"组合任务第 {index} 个脚本必须是过程脚本")
    for index, (_module, document) in enumerate(documents[1:-1], start=2):
        if bool(document.get("cleanup_on_finish", False)):
            raise HTTPException(status_code=422, detail=f"第 {index} 个过程脚本会清理应用，只能放在组合队列最后")
    if not bool(documents[-1][1].get("cleanup_on_finish", False)):
        raise HTTPException(status_code=422, detail="组合任务最后一个过程脚本必须启用清理和关闭应用")
    if float(documents[-1][0].interval_after_seconds):
        raise HTTPException(status_code=422, detail="最后一个模块后面不能设置执行间隔")

    start_target = documents[0][1].get("target") or {}
    expected_serial = str(start_target.get("device_serial") or "")
    combined_steps: list[dict] = []
    combined_popups: list[dict] = []
    summary: list[dict] = []
    for module_index, (module, document) in enumerate(documents):
        module_type = script_type_of(document)
        module_target = document.get("target") or {}
        module_serial = str(module_target.get("device_serial") or "")
        if expected_serial and module_serial and module_serial != expected_serial:
            raise HTTPException(status_code=422, detail=f"模块 {module.script_name} 绑定了不同的 Android 手机")
        module_step_offset = len(combined_steps)
        module_step_count = len(document["steps"])
        for popup in document.get("global_popups") or []:
            if not isinstance(popup, dict):
                raise HTTPException(status_code=422, detail=f"模块 {module.script_name} 的全局弹窗格式无效")
            scoped_popup = dict(popup)
            raw_step_indexes = popup.get("step_indexes")
            if raw_step_indexes is None:
                local_step_indexes = list(range(1, module_step_count + 1))
            elif isinstance(raw_step_indexes, list):
                local_step_indexes = []
                for raw_step_index in raw_step_indexes:
                    if isinstance(raw_step_index, bool):
                        raise HTTPException(status_code=422, detail="popup step indexes must be integers")
                    try:
                        local_step_index = int(raw_step_index)
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=422, detail="popup step indexes must be integers") from exc
                    if local_step_index < 1 or local_step_index > module_step_count:
                        raise HTTPException(status_code=422, detail="popup step index is outside the module")
                    local_step_indexes.append(local_step_index)
            else:
                raise HTTPException(status_code=422, detail="popup step indexes must be an array")
            scoped_popup["step_indexes"] = [
                module_step_offset + local_step_index
                for local_step_index in local_step_indexes
            ]
            combined_popups.append({
                **scoped_popup,
                "name": f"{module.script_name} · {popup.get('name') or '弹窗规则'}",
                "_module_index": module_index,
                "_module_name": module.script_name,
            })
        for module_step_index, step in enumerate(document["steps"]):
            combined_steps.append({
                **step,
                "_module_index": module_index,
                "_module_name": module.script_name,
                "_module_step_index": module_step_index,
            })
        interval = float(module.interval_after_seconds)
        if module_index + 1 < len(documents) and interval > 0:
            combined_steps.append({
                "action": "wait",
                "seconds": interval,
                "_module_index": module_index,
                "_module_name": module.script_name,
                "_module_step_index": len(document["steps"]),
                "_composition_interval": True,
            })
        summary.append({
            "position": module_index + 1,
            "script_name": module.script_name,
            "script_type": module_type,
            "cleanup_on_finish": bool(document.get("cleanup_on_finish", False)),
            "interval_after_seconds": interval,
            "step_count": len(document["steps"]),
        })

    compiled = {
        "version": 2,
        "script_type": "composition",
        "target": start_target,
        "global_popups": combined_popups,
        "composition": summary,
        "steps": combined_steps,
    }
    return json.dumps(compiled, ensure_ascii=False), summary


def reload_retry_source(
    task_item: dict,
    *,
    strict: bool = False,
) -> tuple[str | None, list[dict] | None, str | None]:
    """Load the latest saved scripts before creating a retry task.

    The task itself remains the immutable execution snapshot. A retry gets a
    fresh script, while its previous result still supplies composition resume
    evidence when that evidence is reliable.
    """
    agent_id = str(task_item.get("agent_id") or "")
    if task_item.get("task_kind") == "composition":
        source_task = task_item
        root_id = task_item.get("root_task_id")
        if root_id and root_id != task_item.get("id"):
            root_task = store.get_task(str(root_id))
            if root_task:
                source_task = root_task
        source_modules = source_task.get("composition")
        if not isinstance(source_modules, list) or not source_modules:
            message = "任务没有保存组合模块信息，无法加载最新组合脚本"
            if strict:
                raise HTTPException(status_code=409, detail=message)
            log.warning("%s: %s", message, task_item.get("id"))
            return None, None, None
        modules = []
        for raw_module in source_modules:
            if not isinstance(raw_module, dict) or not str(raw_module.get("script_name") or "").strip():
                continue
            try:
                interval = float(raw_module.get("interval_after_seconds") or 0)
            except (TypeError, ValueError):
                interval = 0
            modules.append(TaskCompositionModule(
                script_name=str(raw_module["script_name"]),
                interval_after_seconds=interval,
            ))
        try:
            if len(modules) < 2:
                raise HTTPException(status_code=409, detail="任务没有完整的组合模块信息")
            script, composition = compile_composition(agent_id, modules)
            return script, composition, None
        except HTTPException:
            if strict:
                raise
            log.exception("无法加载组合任务的最新脚本，保留原重试快照: %s", task_item.get("id"))
            return None, None, None

    script_name = str(task_item.get("script_name") or "").strip()
    if not script_name:
        task_name = str(task_item.get("name") or "").strip()
        candidate = task_name if task_name.lower().endswith(".json") else f"{task_name}.json"
        if candidate and store.get_script(agent_id, candidate):
            script_name = candidate
    if not script_name:
        message = "任务没有保存脚本名称，无法加载最新脚本；可先下载旧脚本镜像"
        if strict:
            raise HTTPException(status_code=409, detail=message)
        log.warning("%s: %s", message, task_item.get("id"))
        return None, None, None
    saved = store.get_script(agent_id, script_name)
    if not saved:
        message = f"脚本库中不存在最新脚本：{script_name}"
        if strict:
            raise HTTPException(status_code=409, detail=f"{message}；可先下载旧脚本镜像")
        log.warning("%s，保留原重试快照: %s", message, task_item.get("id"))
        return None, None, None
    try:
        content = resolve_failure_retry_scripts(agent_id, saved["content"])
        validate_task_script(content, {"standard"})
    except HTTPException:
        if strict:
            raise
        log.exception("最新脚本校验失败，保留原重试快照: %s", task_item.get("id"))
        return None, None, None
    return content, None, script_name


def storage_gib(value: int) -> str:
    return f"{value / 1024 ** 3:.2f} GiB"


def deliver_storage_alert(status: dict[str, object]):
    configuration = current_notification_configuration()
    if not smtp_configured(configuration) or not configuration.failure_recipients:
        return
    try:
        send_message(
            configuration.failure_recipients,
            f"[MAA] 存储空间不足：{status['label']}",
            "\n".join([
                "MAA 测试环境检测到可用存储空间低于安全阈值。",
                "",
                f"位置：{status['label']}",
                f"可用：{storage_gib(int(status['free_bytes']))}",
                f"总量：{storage_gib(int(status['total_bytes']))}",
                f"告警阈值：{storage_gib(settings.storage_warning_bytes)}",
                f"检查时间：{status['last_seen']}",
                "",
                "请清理任务历史、录屏缓存或设备存储。持续不足时每 24 小时提醒一次。",
            ]),
            configuration=configuration,
        )
        store.mark_storage_alert_sent(str(status["scope"]))
    except Exception as exc:
        store.mark_storage_alert_failed(str(status["scope"]), str(exc))
        agent_id = str(status["scope"]).split(":", 1)[-1]
        if str(status["scope"]).startswith(("terminal:", "phone:")):
            store.add_log(agent_id, "ERROR", f"存储空间告警邮件发送失败：{exc}")


def inspect_storage(scope: str, label: str, free_bytes: int, total_bytes: int):
    status = store.update_storage_status(
        scope,
        label,
        int(free_bytes),
        int(total_bytes),
        settings.storage_warning_bytes,
    )
    if status["should_alert"]:
        deliver_storage_alert(status)
    return status


def check_platform_storage():
    target = Path(settings.database_path).resolve().parent
    usage = shutil.disk_usage(target)
    return inspect_storage("platform", "平台 Docker 数据卷", usage.free, usage.total)


def maintenance_loop():
    while not maintenance_stop.is_set():
        try:
            store.activate_due_tasks()
            check_platform_storage()
        except Exception:
            log.exception("platform maintenance check failed")
        maintenance_stop.wait(max(5, settings.maintenance_interval_seconds))


@app.on_event("startup")
def start_maintenance_worker():
    maintenance_stop.clear()
    thread = threading.Thread(target=maintenance_loop, daemon=True, name="platform-maintenance")
    thread.start()
    app.state.maintenance_thread = thread


@app.on_event("shutdown")
def stop_maintenance_worker():
    maintenance_stop.set()


def current_notification_configuration() -> SMTPConfiguration:
    saved = store.get_notification_settings()
    if not saved:
        return environment_configuration()
    password = notification_secrets.decrypt(saved.get("smtp_password_encrypted", ""))
    return SMTPConfiguration(
        host=str(saved.get("smtp_host") or ""),
        port=int(saved.get("smtp_port") or 587),
        user=str(saved.get("smtp_user") or ""),
        password=password,
        from_address=str(saved.get("smtp_from") or ""),
        starttls=bool(saved.get("smtp_starttls")),
        ssl=bool(saved.get("smtp_ssl")),
        failure_recipients=tuple(saved.get("failure_recipients") or []),
        failure_enabled=bool(saved.get("failure_enabled")),
        public_base_url=str(saved.get("public_base_url") or settings.public_base_url).rstrip("/"),
        source="control_center",
        updated_at=saved.get("updated_at"),
    )


def public_notification_configuration() -> dict:
    active = current_notification_configuration()
    return {
        "smtp_configured": smtp_configured(active),
        "failure_email_enabled": failure_notifications_configured(active),
        "failure_enabled": active.failure_enabled,
        "recipients": list(active.failure_recipients),
        "from": active.from_address,
        "smtp_host": active.host,
        "smtp_port": active.port,
        "smtp_user": active.user,
        "security": active.security,
        "public_base_url": active.public_base_url,
        "password_configured": bool(active.password),
        "source": active.source,
        "updated_at": active.updated_at,
    }


def create_failure_record(command_item: dict, result_payload: dict) -> dict:
    failure_id = str(uuid.uuid4())
    capture = result_payload.pop("failure_screenshot", None)
    target: Path | None = None
    screenshot_mime: str | None = None
    screenshot_size: int | None = None
    compact_capture: dict = {}
    if isinstance(capture, dict):
        compact_capture = {
            key: value for key, value in capture.items()
            if key not in {"data_base64", "path"}
        }
        encoded = capture.get("data_base64")
        if isinstance(encoded, str) and encoded:
            try:
                content = base64.b64decode(encoded, validate=True)
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("失败现场截图不是 PNG 图片")
                target = failure_screen_path(failure_id)
                temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(content)
                temporary.replace(target)
                screenshot_mime = "image/png"
                screenshot_size = len(content)
                compact_capture["captured_at"] = utc_now()
            except (binascii.Error, ValueError) as exc:
                result_payload["failure_screenshot_error"] = str(exc)
        elif not result_payload.get("failure_screenshot_error"):
            result_payload["failure_screenshot_error"] = "终端未回传失败截图数据"
    elif not result_payload.get("failure_screenshot_error"):
        result_payload["failure_screenshot_error"] = "终端未回传失败截图"

    if compact_capture:
        result_payload["failure_screenshot"] = compact_capture
    if target:
        result_payload["failure_screenshot_url"] = f"/api/failures/{failure_id}/screenshot"

    command_payload = command_item.get("payload") or {}
    failed_step = result_payload.get("failed_step") or {}
    step_index = failed_step.get("index")
    if not isinstance(step_index, int) or step_index < 0:
        step_index = None
    failed_module = failed_step.get("module") if isinstance(failed_step, dict) else None
    if isinstance(failed_module, dict):
        try:
            module_index = int(failed_module.get("index"))
            module_step_index = int(failed_module.get("step_index"))
        except (TypeError, ValueError):
            module_index = None
            module_step_index = None
        if module_index is not None:
            result_payload["failed_module"] = {
                "position": module_index + 1,
                "name": str(failed_module.get("name") or ""),
                "step_number": (module_step_index + 1) if module_step_index is not None else None,
                "interval": bool(failed_module.get("interval", False)),
            }
    email_status = "pending" if failure_notifications_configured(current_notification_configuration()) else "disabled"
    result_payload["failure_id"] = failure_id
    result_payload["failure_email_status"] = email_status
    failure = {
        "id": failure_id,
        "command_id": command_item["id"],
        "task_id": command_payload.get("task_id"),
        "agent_id": command_item["agent_id"],
        "script_name": str(command_payload.get("name") or "未命名脚本"),
        "error": str(result_payload.get("error") or "未知脚本错误"),
        "failed_step_index": step_index,
        "failed_step_action": str(failed_step.get("action") or "") or None,
        "screenshot_path": str(target) if target else None,
        "screenshot_mime": screenshot_mime,
        "screenshot_size": screenshot_size,
        "email_status": email_status,
        "email_error": None,
        "result": dict(result_payload),
        "created_at": utc_now(),
    }
    return store.create_failure(failure)


def deliver_failure_email(failure_id: str):
    failure = store.get_failure(failure_id)
    if not failure:
        return
    screenshot = Path(failure["screenshot_path"]) if failure.get("screenshot_path") else None
    try:
        recipients = send_failure_notification(
            failure,
            screenshot,
            configuration=current_notification_configuration(),
        )
        store.update_failure_email(failure_id, "sent")
        store.add_log(failure["agent_id"], "INFO", f"失败告警邮件已发送至 {', '.join(recipients)}")
    except Exception as exc:
        store.update_failure_email(failure_id, "failed", str(exc))
        store.add_log(failure["agent_id"], "ERROR", f"失败告警邮件发送失败：{exc}")


def prepare_success_feedbacks(command_item: dict, result_payload: dict) -> list[dict]:
    jobs: list[dict] = []
    command_payload = command_item.get("payload") or {}
    script_name = str(command_payload.get("name") or "单步反馈")
    configuration = current_notification_configuration()
    mail_available = smtp_configured(configuration) and bool(configuration.failure_recipients)
    steps = result_payload.get("steps")
    if not isinstance(steps, list):
        return jobs

    for step in steps:
        if not isinstance(step, dict) or step.get("action") != "feedback":
            continue
        step_result = step.get("result")
        feedback = step_result.get("feedback") if isinstance(step_result, dict) else None
        if not isinstance(feedback, dict):
            continue
        feedback_id = str(uuid.uuid4())
        capture = feedback.pop("capture", None)
        feedback.update({
            "id": feedback_id,
            "step_number": int(step.get("index", 0)) + 1,
            "captured_at": utc_now(),
            "email_status": "disabled",
            "email_error": None,
            "recipients": [],
        })
        if not isinstance(capture, dict):
            feedback["email_status"] = "failed"
            feedback["email_error"] = "终端未回传反馈截图"
            continue
        feedback["capture"] = {
            key: value for key, value in capture.items()
            if key not in {"data_base64", "path"}
        }
        encoded = capture.get("data_base64")
        try:
            content = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("反馈截图不是有效 PNG 图片")
            target = FEEDBACK_TEMP_DIR / f"{hashlib.sha256(feedback_id.encode()).hexdigest()[:32]}.png"
            temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        except (binascii.Error, ValueError, OSError) as exc:
            feedback["email_status"] = "failed"
            feedback["email_error"] = str(exc)
            continue
        if not mail_available:
            feedback["email_error"] = "通知设置中尚未配置 SMTP 和邮件收件人"
            target.unlink(missing_ok=True)
            continue
        feedback["email_status"] = "pending"
        jobs.append({
            "command_id": command_item["id"],
            "feedback_id": feedback_id,
            "agent_id": command_item["agent_id"],
            "script_name": script_name,
            "step_number": feedback["step_number"],
            "subject": str(feedback.get("subject") or "").strip(),
            "message": str(feedback.get("message") or "").strip(),
            "path": str(target),
        })
    return jobs


def deliver_success_feedback(job: dict):
    target = Path(job["path"])
    configuration = current_notification_configuration()
    try:
        body = "\n".join([
            "MAA 自动化脚本已执行到反馈事件。",
            "",
            f"脚本：{job['script_name']}",
            f"终端：{job['agent_id']}",
            f"步骤：第 {job['step_number']} 步（反馈事件）",
            f"时间：{utc_now()}",
            "",
            job.get("message") or "反馈截图已作为附件发送。",
        ])
        recipients = send_message(
            configuration.failure_recipients,
            job.get("subject") or f"[MAA] 脚本反馈：{job['script_name']}",
            body,
            attachment=target,
            attachment_mime="image/png",
            configuration=configuration,
        )
        store.update_feedback_email(job["command_id"], job["feedback_id"], "sent", recipients=recipients)
        store.add_log(job["agent_id"], "INFO", f"脚本反馈邮件已发送至 {', '.join(recipients)}")
    except Exception as exc:
        store.update_feedback_email(job["command_id"], job["feedback_id"], "failed", str(exc))
        store.add_log(job["agent_id"], "ERROR", f"脚本反馈邮件发送失败：{exc}")
    finally:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


@app.get("/api/health")
def health():
    return {"ok": True, "service": settings.app_name, "version": app.version}


@app.get("/api/terminal-deploy/artifacts")
def terminal_artifacts():
    return TERMINAL_ARTIFACTS.list()


@app.post("/api/terminal-deploy/artifacts")
def upload_terminal_artifact(file: UploadFile = File(...)):
    try:
        artifact_name = TERMINAL_ARTIFACTS.validate_name(file.filename or "")
        return TERMINAL_ARTIFACTS.save_stream(artifact_name, file.file)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        file.file.close()


@app.get("/api/terminal-deploy/artifacts/{artifact_name}", dependencies=[Depends(agent_auth)])
def download_terminal_artifact(artifact_name: str):
    try:
        metadata = TERMINAL_ARTIFACTS.metadata(artifact_name)
        target = TERMINAL_ARTIFACTS.path(artifact_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="terminal artifact not found") from exc
    if not metadata or not target.is_file():
        raise HTTPException(status_code=404, detail="terminal artifact not found")
    return FileResponse(
        target,
        media_type="application/x-tar",
        filename=metadata["name"],
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/agents/{agent_id}/deploy")
def deploy_terminal(agent_id: str, payload: TerminalDeployRequest):
    agent = get_agent_or_404(agent_id)
    if not is_tronlong_npu_agent(agent):
        raise HTTPException(status_code=400, detail="当前仅支持创龙 RK3576 ARM64 NPU 终端")
    metadata = TERMINAL_ARTIFACTS.metadata(payload.artifact_name)
    if not metadata:
        raise HTTPException(status_code=404, detail="平台缓存中没有这个终端镜像")
    public_url = terminal_artifact_public_url()
    if not public_url or "127.0.0.1" in public_url or "localhost" in public_url:
        raise HTTPException(status_code=422, detail="请将 PUBLIC_BASE_URL 或 TERMINAL_DEPLOY_PUBLIC_URL 设置为终端可访问的平台地址")
    deployment_id = uuid.uuid4().hex
    deployer_payload = {
        "deployment_id": deployment_id,
        "artifact_url": f"{public_url}/api/terminal-deploy/artifacts/{quote(metadata['name'], safe='')}",
        "artifact_sha256": metadata["sha256"],
        "artifact_name": metadata["name"],
        "image_tag": settings.terminal_image_tag,
        "platform_token": settings.agent_token,
    }
    result = call_terminal_deployer(agent, "/deploy", deployer_payload)
    return {
        "deployment_id": deployment_id,
        "agent_id": agent_id,
        "artifact": metadata,
        "status": result.get("status", "accepted"),
        **result,
    }


@app.get("/api/agents/{agent_id}/deployments/{deployment_id}")
def terminal_deployment_status(agent_id: str, deployment_id: str):
    agent = get_agent_or_404(agent_id)
    if not is_tronlong_npu_agent(agent):
        raise HTTPException(status_code=400, detail="当前仅支持创龙 RK3576 ARM64 NPU 终端")
    if not deployment_id or len(deployment_id) > 100 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in deployment_id):
        raise HTTPException(status_code=422, detail="invalid deployment id")
    return call_terminal_deployer(agent, f"/deploy/status/{quote(deployment_id, safe='')}")


@app.get("/api/overview")
def overview():
    store.mark_stale(settings.heartbeat_timeout_seconds)
    return store.overview()


@app.post("/api/agents/register", dependencies=[Depends(agent_auth)])
def register_agent(payload: AgentRegistration):
    return store.upsert_agent(payload.model_dump())


@app.post("/api/agents/{agent_id}/heartbeat", dependencies=[Depends(agent_auth)])
def heartbeat(agent_id: str, payload: Heartbeat, background_tasks: BackgroundTasks):
    if not store.get_agent(agent_id):
        raise HTTPException(status_code=404, detail="agent not registered")
    values = payload.model_dump()
    terminal_storage = values.get("metadata", {}).get("storage", {})
    phone_storage = values.get("device", {}).get("storage", {})
    if terminal_storage.get("free_bytes") is not None:
        background_tasks.add_task(
            inspect_storage,
            f"terminal:{agent_id}",
            f"终端 {agent_id} 工作目录",
            int(terminal_storage["free_bytes"]),
            int(terminal_storage.get("total_bytes") or 0),
        )
    if phone_storage.get("free_bytes") is not None:
        background_tasks.add_task(
            inspect_storage,
            f"phone:{agent_id}",
            f"终端 {agent_id} 连接的 Android 手机",
            int(phone_storage["free_bytes"]),
            int(phone_storage.get("total_bytes") or 0),
        )
    return store.heartbeat(agent_id, values)


@app.get("/api/agents")
def agents():
    store.mark_stale(settings.heartbeat_timeout_seconds)
    return store.list_agents()


@app.get("/api/agents/{agent_id}")
def agent(agent_id: str):
    return get_agent_or_404(agent_id)


def enqueue(agent_id: str, kind: str, payload: dict):
    get_agent_or_404(agent_id)
    command = {"id": str(uuid.uuid4()), "agent_id": agent_id, "kind": kind, "payload": payload}
    store.create_command(command)
    return {"command_id": command["id"], "status": "queued"}


def prepare_quick_test_content(content: str) -> str:
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"快速测试脚本不是有效 JSON：{exc}") from exc
    if not isinstance(document, dict):
        raise HTTPException(status_code=422, detail="快速测试脚本必须是 JSON 对象")
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=422, detail="快速测试至少需要一个脚本步骤")
    if any(not isinstance(step, dict) for step in steps):
        raise HTTPException(status_code=422, detail="快速测试的每个脚本步骤都必须是对象")
    document["execution_mode"] = "quick_test"
    return json.dumps(document, ensure_ascii=False)


def create_task_batch_records(
    payload: TaskBatchCreate,
    *,
    task_kind: str = "standard",
    composition: list[dict] | None = None,
) -> dict:
    get_agent_or_404(payload.agent_id)
    for item in payload.items:
        item.script = resolve_failure_retry_scripts(payload.agent_id, item.script)
        validate_task_script(
            item.script,
            {"standard"} if task_kind == "standard" else {"composition"},
        )
        if item.script_name:
            item.script_name = normalized_script_name(item.script_name)

    slots: list[tuple[int, int, str | None, str | None, str]] = []
    if payload.mode == "scheduled":
        if not all((payload.schedule_start_date, payload.schedule_end_date, payload.daily_start_time, payload.daily_end_time)):
            raise HTTPException(status_code=422, detail="定时任务需要开始/结束日期以及每日开始/结束时间")
        if payload.schedule_end_date < payload.schedule_start_date:
            raise HTTPException(status_code=422, detail="结束日期不能早于开始日期")
        if payload.daily_end_time <= payload.daily_start_time:
            raise HTTPException(status_code=422, detail="每日结束时间必须晚于开始时间")
        total_days = (payload.schedule_end_date - payload.schedule_start_date).days + 1
        if total_days > 366:
            raise HTTPException(status_code=422, detail="一次定时计划最多创建 366 天")
        local_zone = ZoneInfo(settings.timezone)
        for day_index in range(total_days):
            local_date = payload.schedule_start_date + timedelta(days=day_index)
            local_start = datetime.combine(local_date, payload.daily_start_time, tzinfo=local_zone)
            local_end = datetime.combine(local_date, payload.daily_end_time, tzinfo=local_zone)
            slots.append((
                day_index + 1,
                total_days,
                local_start.astimezone(timezone.utc).isoformat(),
                local_end.astimezone(timezone.utc).isoformat(),
                local_start.strftime("%Y-%m-%d %H:%M"),
            ))
    else:
        total_runs = payload.repeat_count if payload.mode == "repeat" else 1
        slots = [(index, total_runs, None, None, "") for index in range(1, total_runs + 1)]

    batch_id = str(uuid.uuid4())
    tasks_to_create: list[dict] = []
    commands: list[dict] = []
    sequence = 0
    created_base = datetime.now(timezone.utc)
    for run_index, run_total, scheduled_for, window_end, schedule_label in slots:
        for item in payload.items:
            sequence += 1
            task_id = str(uuid.uuid4())
            if payload.mode == "repeat":
                task_name = f"{item.name} · 循环 {run_index}/{run_total}"
            elif payload.mode == "scheduled":
                task_name = f"{item.name} · {schedule_label}"
            else:
                task_name = item.name
            task = {
                "id": task_id,
                "agent_id": payload.agent_id,
                "name": task_name,
                "script_name": str(item.script_name or ""),
                "script": item.script,
                "params": item.params,
                "status": "scheduled" if payload.mode == "scheduled" else "queued",
                "batch_id": batch_id,
                "batch_sequence": sequence,
                "schedule_type": payload.mode,
                "scheduled_for": scheduled_for,
                "schedule_window_end": window_end,
                "run_index": run_index,
                "run_total": run_total,
                "attempt": 1,
                "max_retries": payload.max_retries,
                "record_video": payload.record_video,
                "task_kind": task_kind,
                "composition": composition or [],
                "created_at": (created_base + timedelta(microseconds=sequence)).isoformat(),
            }
            tasks_to_create.append(task)
            if task["status"] == "queued":
                commands.append({
                    "id": str(uuid.uuid4()),
                    "agent_id": payload.agent_id,
                    "kind": "task",
                    "payload": store.task_command_payload(task),
                    "created_at": task["created_at"],
                })

    created = store.create_task_batch(tasks_to_create, commands)
    return {
        "batch_id": batch_id,
        "mode": payload.mode,
        "count": len(created),
        "tasks": [public_task(item) for item in created],
    }


@app.post("/api/tasks")
def create_task(payload: TaskCreate):
    if not payload.script or not payload.script.strip():
        raise HTTPException(status_code=422, detail="任务脚本不能为空")
    batch = create_task_batch_records(TaskBatchCreate(
        agent_id=payload.agent_id,
        items=[TaskBatchItem(
            name=payload.name,
            script=payload.script,
            script_name=payload.script_name,
            params=payload.params,
        )],
        max_retries=payload.max_retries,
        record_video=payload.record_video,
    ))
    return batch["tasks"][0]


@app.post("/api/tasks/batch")
def create_tasks_batch(payload: TaskBatchCreate):
    return create_task_batch_records(payload)


@app.post("/api/tasks/compositions")
def create_task_composition(payload: TaskCompositionCreate):
    get_agent_or_404(payload.agent_id)
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="组合任务名称不能为空")
    compiled_script, composition = compile_composition(payload.agent_id, payload.modules)
    batch_payload = TaskBatchCreate(
        agent_id=payload.agent_id,
        items=[TaskBatchItem(name=payload.name, script=compiled_script, params=payload.params)],
        mode=payload.mode,
        repeat_count=payload.repeat_count,
        schedule_start_date=payload.schedule_start_date,
        schedule_end_date=payload.schedule_end_date,
        daily_start_time=payload.daily_start_time,
        daily_end_time=payload.daily_end_time,
        max_retries=payload.max_retries,
        record_video=payload.record_video,
    )
    return create_task_batch_records(
        batch_payload,
        task_kind="composition",
        composition=composition,
    )


@app.get("/api/tasks")
def tasks(agent_id: str | None = Query(default=None)):
    store.activate_due_tasks()
    return [public_task(item) for item in store.list_tasks(agent_id)]


@app.get("/api/tasks/{task_id}")
def task(task_id: str):
    item = store.get_task(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="task not found")
    return public_task(item)


@app.get("/api/tasks/{task_id}/script")
def download_task_script(task_id: str):
    item = store.get_task(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="task not found")
    content = str(item.get("script") or "")
    if not content:
        raise HTTPException(status_code=404, detail="task script snapshot not found")
    filename = str(item.get("script_name") or f"{item.get('name') or task_id}-script.json")
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"
    return PlainTextResponse(
        content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    item = store.get_task(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="task not found")
    if item["status"] not in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="only finished tasks can be deleted")
    deleted = store.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="task not found")
    removed_bytes = safe_remove_managed_file(deleted.get("recording_path"), RECORDING_DIR)
    check_platform_storage()
    return {"ok": True, "id": task_id, "recording_deleted_bytes": removed_bytes}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    item = store.get_task(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="task not found")
    if item["status"] not in {"queued", "scheduled"}:
        raise HTTPException(status_code=409, detail="只有尚未被终端领取的排队或定时任务可以取消")
    cancelled = store.cancel_task(task_id)
    if not cancelled or cancelled["status"] != "cancelled":
        raise HTTPException(status_code=409, detail="任务已被终端领取，无法取消")
    return public_task(cancelled)


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    item = store.get_task(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="task not found")
    if item["status"] != "failed":
        raise HTTPException(status_code=409, detail="只有失败任务可以重试")

    latest_script, latest_composition, latest_script_name = reload_retry_source(item, strict=True)
    retry = store.enqueue_retry(
        task_id,
        manual=True,
        script_override=latest_script,
        script_name_override=latest_script_name,
        composition_override=latest_composition,
    )
    if not retry:
        raise HTTPException(status_code=409, detail="任务当前无法重试，请刷新任务列表后再试")
    store.merge_task_result(task_id, {
        "retry_task_id": retry["id"],
        "retry_attempt": retry["attempt"],
        "retry_queued": True,
        "retry_manual": True,
    })
    return public_task(retry)


@app.post("/api/agent/{agent_id}/tasks/{task_id}/recording", dependencies=[Depends(agent_auth)])
async def upload_task_recording(
    agent_id: str,
    task_id: str,
    recording: UploadFile = File(...),
):
    task_item = store.get_task(task_id)
    if not task_item or task_item["agent_id"] != agent_id:
        raise HTTPException(status_code=404, detail="task not found")
    base_name = hashlib.sha256(task_id.encode()).hexdigest()[:32]
    temporary = RECORDING_DIR / f".{base_name}.{uuid.uuid4().hex}.tmp"
    size = 0
    try:
        with temporary.open("wb") as stream:
            while chunk := await recording.read(1024 * 1024):
                size += len(chunk)
                if size > 4 * 1024 ** 3:
                    raise HTTPException(status_code=413, detail="单个任务录屏不能超过 4 GiB")
                stream.write(chunk)
        if size < 16:
            raise HTTPException(status_code=422, detail="录屏文件为空或不完整")
        with temporary.open("rb") as stream:
            header = stream.read(16)
        if b"ftyp" in header:
            suffix = ".mp4"
            mime = "video/mp4"
        elif header.startswith(b"PK") and zipfile.is_zipfile(temporary):
            with zipfile.ZipFile(temporary, "r") as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                if not entries or any(not entry.filename.lower().endswith(".mp4") for entry in entries):
                    raise HTTPException(status_code=422, detail="录屏 ZIP 中没有有效的 MP4 分段")
                if any(entry.file_size < 16 for entry in entries):
                    raise HTTPException(status_code=422, detail="录屏 ZIP 包含空分段")
                bad_entry = archive.testzip()
                if bad_entry:
                    raise HTTPException(status_code=422, detail=f"录屏 ZIP 分段损坏：{bad_entry}")
            suffix = ".zip"
            mime = "application/zip"
        else:
            raise HTTPException(status_code=422, detail="录屏文件必须是 MP4 或分段 ZIP")
        target = RECORDING_DIR / f"{base_name}{suffix}"
        previous = task_item.get("recording_path")
        temporary.replace(target)
        if previous and Path(previous).resolve() != target.resolve():
            safe_remove_managed_file(previous, RECORDING_DIR)
        updated = store.set_task_recording(task_id, str(target), mime, size)
        return {
            "ok": True,
            "task_id": task_id,
            "size_bytes": size,
            "mime": mime,
            "recording_url": f"/api/tasks/{task_id}/recording",
            "task": public_task(updated),
        }
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        await recording.close()


@app.get("/api/tasks/{task_id}/recording")
def download_task_recording(task_id: str):
    task_item = store.get_task(task_id)
    if not task_item:
        raise HTTPException(status_code=404, detail="task not found")
    target = Path(task_item["recording_path"]) if task_item.get("recording_path") else None
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="task recording not found")
    return FileResponse(
        target,
        media_type=task_item.get("recording_mime") or "video/mp4",
        filename=f"{task_item['name']}-{task_id[:8]}{'.zip' if task_item.get('recording_mime') == 'application/zip' else '.mp4'}",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/maintenance/status")
def maintenance_status():
    platform_storage = check_platform_storage()
    stats = store.maintenance_stats()
    try:
        stats["database_bytes"] = Path(settings.database_path).stat().st_size
    except FileNotFoundError:
        stats["database_bytes"] = 0
    return {
        "threshold_bytes": settings.storage_warning_bytes,
        "platform": platform_storage,
        "storage": store.list_storage_status(),
        "cache": stats,
    }


@app.post("/api/maintenance/cleanup")
def cleanup_history(payload: MaintenanceCleanup):
    cutoff = None
    if payload.older_than_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=payload.older_than_days)).isoformat()
    removed_bytes = 0
    recordings_deleted = 0
    history_result = {
        "tasks_deleted": 0,
        "commands_deleted": 0,
        "failure_records_deleted": 0,
    }

    if payload.target == "recordings":
        candidates = store.recording_cleanup_candidates(cutoff)
        for item in candidates:
            removed_bytes += safe_remove_managed_file(item.get("recording_path"), RECORDING_DIR)
        store.clear_task_recordings([item["id"] for item in candidates])
        recordings_deleted = len(candidates)
    else:
        history = store.delete_task_history(cutoff, payload.include_failure_evidence)
        for path in history.pop("recording_paths"):
            removed_bytes += safe_remove_managed_file(path, RECORDING_DIR)
            recordings_deleted += 1
        for path in history.pop("failure_paths"):
            removed_bytes += safe_remove_managed_file(path, FAILURE_DIR)
        history_result.update(history)
        if payload.target == "all":
            candidates = store.recording_cleanup_candidates(cutoff)
            for item in candidates:
                removed_bytes += safe_remove_managed_file(item.get("recording_path"), RECORDING_DIR)
            store.clear_task_recordings([item["id"] for item in candidates])
            recordings_deleted += len(candidates)
        store.vacuum()

    check_platform_storage()
    return {
        "ok": True,
        "target": payload.target,
        "older_than_days": payload.older_than_days,
        "recordings_deleted": recordings_deleted,
        "removed_bytes": removed_bytes,
        **history_result,
        "cache": store.maintenance_stats(),
    }


@app.get("/api/agent/poll", dependencies=[Depends(agent_auth)])
def poll(
    agent_id: str,
    wait_seconds: float = Query(default=0, ge=0, le=25),
    accept_tasks: bool = Query(default=True),
    limit: int = Query(default=1, ge=1, le=10),
):
    get_agent_or_404(agent_id)
    commands = (
        store.wait_for_commands(agent_id, wait_seconds, limit, accept_tasks=accept_tasks)
        if wait_seconds
        else store.poll_commands(agent_id, limit, accept_tasks=accept_tasks)
    )
    return {"commands": commands}


@app.post("/api/agent/commands/{command_id}/result", dependencies=[Depends(agent_auth)])
def command_result(command_id: str, payload: CommandResult, background_tasks: BackgroundTasks):
    command_item = store.get_command(command_id)
    result_payload = dict(payload.result)
    success = payload.success
    if command_item and command_item["kind"] == "quick_test":
        # Quick tests intentionally keep only diagnostic text/step metadata.
        # Never persist terminal failure evidence in the command result.
        result_payload.pop("failure_screenshot", None)
        result_payload.pop("failure_screenshot_url", None)
        result_payload.pop("failure_screenshot_error", None)
        result_payload["quick_test"] = True
    if command_item and command_item["kind"] in {"screenshot", "control"} and success and result_payload.get("data_base64"):
        try:
            result_payload = persist_latest_screen(command_item["agent_id"], result_payload)
        except ValueError as exc:
            success = False
            result_payload = {"error": str(exc)}
    feedback_jobs = []
    if command_item and command_item["kind"] in {"task", "run_script", "run_step"} and success:
        feedback_jobs = prepare_success_feedbacks(command_item, result_payload)
    failure_record = None
    if command_item and command_item["kind"] in {"task", "run_script"} and not success:
        failure_record = create_failure_record(command_item, result_payload)
    result = store.complete_command(command_id, result_payload, success)
    if not result:
        raise HTTPException(status_code=404, detail="command not found")
    retry_task = None
    task_id = (command_item.get("payload") or {}).get("task_id") if command_item else None
    if command_item and command_item["kind"] == "task" and not success and task_id:
        failed_task = store.get_task(task_id)
        latest_script = None
        latest_composition = None
        latest_script_name = None
        if failed_task:
            try:
                latest_script, latest_composition, latest_script_name = reload_retry_source(
                    failed_task,
                    strict=True,
                )
            except HTTPException as exc:
                log.error("自动重试未排队（任务 %s）：%s", task_id, exc.detail)
            else:
                retry_task = store.enqueue_retry(
                    task_id,
                    script_override=latest_script,
                    script_name_override=latest_script_name,
                    composition_override=latest_composition,
                )
        if retry_task:
            result_payload["retry_task_id"] = retry_task["id"]
            result_payload["retry_attempt"] = retry_task["attempt"]
            result_payload["retry_queued"] = True
            store.merge_task_result(task_id, {
                "retry_task_id": retry_task["id"],
                "retry_attempt": retry_task["attempt"],
                "retry_queued": True,
            })
            result["result"] = result_payload
    if command_item:
        level = "INFO" if success else "ERROR"
        summary = result_payload.get("error") or f"{command_item['kind']} completed"
        store.add_log(command_item["agent_id"], level, str(summary))
    if failure_record and failure_record["email_status"] == "pending":
        background_tasks.add_task(deliver_failure_email, failure_record["id"])
    for feedback_job in feedback_jobs:
        background_tasks.add_task(deliver_success_feedback, feedback_job)
    if retry_task:
        result["retry_task"] = public_task(retry_task)
    return result


@app.get("/api/agents/{agent_id}/screen")
def latest_screen(agent_id: str):
    get_agent_or_404(agent_id)
    target = screen_path(agent_id)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="terminal has not uploaded a screenshot")
    return FileResponse(target, media_type="image/png", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/commands/{command_id}")
def command(command_id: str, wait_seconds: float = Query(default=0, ge=0, le=25)):
    item = store.wait_for_command(command_id, wait_seconds) if wait_seconds else store.get_command(command_id)
    if not item:
        raise HTTPException(status_code=404, detail="command not found")
    return item


@app.get("/api/failures")
def failures(agent_id: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=1000)):
    return [public_failure(item) for item in store.list_failures(agent_id, limit)]


@app.get("/api/failures/{failure_id}")
def failure(failure_id: str):
    item = store.get_failure(failure_id)
    if not item:
        raise HTTPException(status_code=404, detail="failure record not found")
    return public_failure(item)


@app.post("/api/failures/{failure_id}/confirm")
def confirm_failure(failure_id: str):
    item = store.get_failure(failure_id)
    if not item:
        raise HTTPException(status_code=404, detail="failure record not found")
    confirmed = store.confirm_failure(failure_id)
    if not confirmed:
        raise HTTPException(status_code=404, detail="failure record not found")
    return public_failure(confirmed)


@app.delete("/api/failures/{failure_id}")
def delete_failure(failure_id: str):
    item = store.get_failure(failure_id)
    if not item:
        raise HTTPException(status_code=404, detail="failure record not found")
    deleted = store.delete_failure(failure_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="failure record not found")
    removed_bytes = safe_remove_managed_file(deleted.get("screenshot_path"), FAILURE_DIR)
    check_platform_storage()
    return {"ok": True, "id": failure_id, "removed_bytes": removed_bytes}


@app.get("/api/failures/{failure_id}/screenshot")
def failure_screenshot(failure_id: str):
    item = store.get_failure(failure_id)
    if not item:
        raise HTTPException(status_code=404, detail="failure record not found")
    target = Path(item["screenshot_path"]) if item.get("screenshot_path") else None
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="failure screenshot not found")
    return FileResponse(
        target,
        media_type=item.get("screenshot_mime") or "image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/notifications/status")
def notification_status():
    return public_notification_configuration()


@app.get("/api/notifications/settings")
def get_notification_settings():
    return public_notification_configuration()


@app.put("/api/notifications/settings")
def save_notification_settings(payload: NotificationSettingsUpdate):
    recipients = list(dict.fromkeys(item.strip() for item in payload.failure_recipients if item.strip()))
    addresses = recipients + ([payload.smtp_from.strip()] if payload.smtp_from.strip() else [])
    if any("@" not in item or " " in item for item in addresses):
        raise HTTPException(status_code=422, detail="发件人和收件人必须是有效的邮箱地址")
    host = payload.smtp_host.strip()
    from_address = payload.smtp_from.strip()
    public_base_url = payload.public_base_url.strip().rstrip("/")
    if public_base_url and not public_base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="平台访问地址必须以 http:// 或 https:// 开头")
    if payload.failure_enabled:
        missing = []
        if not host:
            missing.append("SMTP 服务器")
        if not from_address:
            missing.append("发件地址")
        if not recipients:
            missing.append("失败通知收件人")
        if missing:
            raise HTTPException(status_code=422, detail=f"启用失败邮件前请填写：{', '.join(missing)}")

    saved = store.get_notification_settings()
    encrypted_password = str(saved.get("smtp_password_encrypted") or "") if saved else ""
    if payload.clear_password:
        encrypted_password = ""
    elif payload.smtp_password:
        encrypted_password = notification_secrets.encrypt(payload.smtp_password)
    elif not saved:
        environment_password = environment_configuration().password
        if environment_password:
            encrypted_password = notification_secrets.encrypt(environment_password)

    store.save_notification_settings({
        "smtp_host": host,
        "smtp_port": payload.smtp_port,
        "smtp_user": payload.smtp_user.strip(),
        "smtp_password_encrypted": encrypted_password,
        "smtp_from": from_address,
        "smtp_starttls": payload.security == "starttls",
        "smtp_ssl": payload.security == "ssl",
        "failure_recipients": recipients,
        "failure_enabled": payload.failure_enabled,
        "public_base_url": public_base_url or settings.public_base_url,
    })
    return public_notification_configuration()


@app.post("/api/notifications/test")
def test_notification(payload: TestEmailRequest):
    active = current_notification_configuration()
    recipient = (payload.to or "").strip() or next(iter(active.failure_recipients), "")
    if not recipient or "@" not in recipient or " " in recipient:
        raise HTTPException(status_code=422, detail="请填写有效的测试邮件收件人")
    try:
        recipients = send_message(
            [recipient],
            "[MAA] 测试平台邮件配置验证",
            "这是一封来自 MAA Test Console 的测试邮件。收到此邮件说明页面中的 SMTP 配置可用。",
            configuration=active,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"测试邮件发送失败：{exc}") from exc
    return {"ok": True, "to": recipients}


@app.post("/api/agent/{agent_id}/logs", dependencies=[Depends(agent_auth)])
def agent_log(agent_id: str, payload: LogCreate):
    get_agent_or_404(agent_id)
    store.add_log(agent_id, payload.level, payload.message)
    return {"ok": True}


@app.get("/api/agents/{agent_id}/logs")
def logs(agent_id: str, limit: int = Query(default=200, le=1000)):
    get_agent_or_404(agent_id)
    return store.list_logs(agent_id, limit)


@app.get("/api/agents/{agent_id}/logs/download", response_class=PlainTextResponse)
def download_logs(agent_id: str):
    get_agent_or_404(agent_id)
    with store.connect() as db:
        latest = db.execute("""SELECT result FROM commands
            WHERE agent_id=? AND kind='get_logs' AND status='succeeded'
            ORDER BY finished_at DESC LIMIT 1""", (agent_id,)).fetchone()
    if latest:
        terminal_log = json.loads(latest["result"]).get("content")
        if terminal_log is not None:
            return terminal_log
    rows = reversed(store.list_logs(agent_id, 1000))
    return "\n".join(f"{row['created_at']} {row['level']} {row['message']}" for row in rows)


@app.post("/api/agents/{agent_id}/logs/collect")
def collect_logs(agent_id: str):
    return enqueue(agent_id, "get_logs", {})


@app.post("/api/messages/email")
def send_email(payload: EmailMessage):
    try:
        recipients = send_message(
            payload.to,
            payload.subject,
            payload.body,
            html=payload.html,
            configuration=current_notification_configuration(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "to": recipients}


@app.post("/api/agents/{agent_id}/restart")
def restart(agent_id: str):
    return enqueue(agent_id, "restart_device", {})


@app.post("/api/agents/{agent_id}/service/start")
def start_agent_service(agent_id: str):
    return enqueue(agent_id, "agent_start", {})


@app.post("/api/agents/{agent_id}/service/stop")
def stop_agent_service(agent_id: str):
    return enqueue(agent_id, "agent_stop", {})


@app.post("/api/agents/{agent_id}/device/{action}")
def device_action(agent_id: str, action: str, enabled: bool | None = None):
    if action not in {"screenshot", "sleep", "wake", "charging", "detect-app"}:
        raise HTTPException(status_code=400, detail="unsupported device action")
    payload = {} if enabled is None else {"enabled": enabled}
    command_kind = "detect_app" if action == "detect-app" else action
    return enqueue(agent_id, command_kind, payload)


@app.post("/api/agents/{agent_id}/control")
def control_device(agent_id: str, payload: DeviceControl):
    values = payload.model_dump(exclude_none=True)
    if payload.action == "tap" and not {"x", "y"}.issubset(values):
        raise HTTPException(status_code=422, detail="tap requires x and y")
    if payload.action == "swipe" and not {"x1", "y1", "x2", "y2"}.issubset(values):
        raise HTTPException(status_code=422, detail="swipe requires x1, y1, x2 and y2")
    if payload.action == "orientation" and "orientation" not in values:
        raise HTTPException(status_code=422, detail="orientation action requires orientation")
    return enqueue(agent_id, "control", values)


@app.post("/api/agents/{agent_id}/interactive/start")
def start_interactive(agent_id: str, payload: InteractiveStart):
    return enqueue(agent_id, "interactive_start", payload.model_dump())


@app.post("/api/agents/{agent_id}/interactive/stop")
def stop_interactive(agent_id: str, payload: InteractiveStop):
    return enqueue(agent_id, "interactive_stop", payload.model_dump())


@app.post("/api/agents/{agent_id}/open-editor")
def open_editor(agent_id: str):
    result = enqueue(agent_id, "open_editor", {"url": f"/editor/{agent_id}"})
    result["editor_url"] = f"/editor/{agent_id}"
    return result


@app.get("/api/agents/{agent_id}/scripts")
def scripts(agent_id: str):
    agent = get_agent_or_404(agent_id)
    device = (agent.get("metadata") or {}).get("device")
    return [public_script(item, device) for item in store.list_scripts(agent_id)]


@app.get("/api/agents/{agent_id}/script-categories")
def script_categories(agent_id: str):
    get_agent_or_404(agent_id)
    return store.list_script_categories(agent_id)


@app.put("/api/agents/{agent_id}/script-categories/{package_name}")
def rename_script_category(agent_id: str, package_name: str, payload: ScriptCategoryUpdate):
    get_agent_or_404(agent_id)
    normalized_package, _activity = normalized_android_component(package_name)
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="分类显示名称不能为空")
    return store.rename_script_category(agent_id, normalized_package, display_name)


@app.get("/api/agents/{agent_id}/script-audit")
def script_audit(agent_id: str, limit: int = Query(default=200, ge=1, le=1000)):
    get_agent_or_404(agent_id)
    return store.list_script_audit(agent_id, limit)


@app.get("/api/agents/{agent_id}/scripts/{name}")
def script(agent_id: str, name: str):
    agent = get_agent_or_404(agent_id)
    item = store.get_script(agent_id, name)
    if not item:
        raise HTTPException(status_code=404, detail="script not found")
    device = (agent.get("metadata") or {}).get("device")
    return public_script(item, device)


@app.put("/api/agents/{agent_id}/scripts/{name}")
def save_script(agent_id: str, name: str, payload: ScriptPayload):
    agent = get_agent_or_404(agent_id)
    script_name = normalized_script_name(name)
    document = validate_task_script(payload.content, {"standard", "module_start", "module_process"})
    resolve_failure_retry_scripts(agent_id, payload.content, reference_chain=(script_name,))
    category_package, source_package, source_activity = script_save_metadata(
        agent_id,
        script_name,
        document,
    )
    return public_script(store.save_script(
        agent_id,
        script_name,
        payload.content,
        category_package,
        source_package,
        source_activity,
    ), (agent.get("metadata") or {}).get("device"))


@app.post("/api/agents/{agent_id}/scripts/import")
def import_script(agent_id: str, payload: ScriptImportPayload):
    agent = get_agent_or_404(agent_id)
    script_name = normalized_script_name(payload.name)
    document = validate_task_script(payload.content, {"standard", "module_start", "module_process"})
    resolve_failure_retry_scripts(agent_id, payload.content, reference_chain=(script_name,))
    application = script_application(document)
    source_package = application[0] if application else None
    source_activity = (application[1] or None) if application else None
    category_package = None
    if payload.category_package:
        category_package, _activity = normalized_android_component(payload.category_package)
    return public_script(store.save_script(
        agent_id,
        script_name,
        payload.content,
        category_package,
        source_package,
        source_activity,
        audit_operation="uploaded",
    ), (agent.get("metadata") or {}).get("device"))


@app.patch("/api/agents/{agent_id}/scripts/{name}/category")
def move_script_category(agent_id: str, name: str, payload: ScriptCategoryMove):
    agent = get_agent_or_404(agent_id)
    category_package = None
    if payload.category_package:
        category_package, _activity = normalized_android_component(payload.category_package)
    item = store.move_script_category(agent_id, name, category_package)
    if not item:
        raise HTTPException(status_code=404, detail="script not found")
    return public_script(item, (agent.get("metadata") or {}).get("device"))


@app.get("/api/agents/{agent_id}/scripts/{name}/download")
def download_script(agent_id: str, name: str):
    get_agent_or_404(agent_id)
    item = store.get_script(agent_id, name)
    if not item:
        raise HTTPException(status_code=404, detail="script not found")
    return PlainTextResponse(
        item["content"],
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(item['name'])}"},
    )


@app.delete("/api/agents/{agent_id}/scripts/{name}")
def delete_script(agent_id: str, name: str):
    get_agent_or_404(agent_id)
    references = script_references(agent_id, name)
    if references:
        labels = "、".join(references[:5])
        suffix = " 等" if len(references) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail=f"脚本正被失败重试引用：{labels}{suffix}；请先移除引用",
        )
    deleted = store.delete_script(agent_id, name)
    if not deleted:
        raise HTTPException(status_code=404, detail="script not found")
    return {"ok": True, "name": name}


@app.post("/api/agents/{agent_id}/steps/run")
def run_script_step(agent_id: str, payload: ScriptStepPayload):
    get_agent_or_404(agent_id)
    return enqueue(agent_id, "run_step", {
        "content": resolve_failure_retry_scripts(agent_id, payload.content),
        "params": {},
        "keep_interactive": payload.keep_interactive,
    })


@app.post("/api/agents/{agent_id}/quick-tests")
def run_quick_test(agent_id: str, payload: QuickTestPayload):
    get_agent_or_404(agent_id)
    repeat_count = payload.repeat_count if payload.mode == "repeat" else 1
    resolved_content = resolve_failure_retry_scripts(agent_id, payload.content)
    return enqueue(agent_id, "quick_test", {
        "content": prepare_quick_test_content(resolved_content),
        "params": {},
        "mode": payload.mode,
        "repeat_count": repeat_count,
    })


@app.get("/{requested_path:path}", include_in_schema=False)
def spa(requested_path: str):
    if FRONTEND_DIR.is_dir():
        candidate = (FRONTEND_DIR / requested_path).resolve()
        if FRONTEND_DIR.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        index_file = FRONTEND_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="frontend bundle not found")
