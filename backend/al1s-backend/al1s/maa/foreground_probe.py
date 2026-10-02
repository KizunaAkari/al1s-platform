"""Read the actual Android foreground package via the authenticated editor relay."""

import asyncio
import json
import os
import re
import ssl
from uuid import UUID

from starlette.concurrency import run_in_threadpool
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from al1s.execution.editor_service import EditorSessionService
from al1s.execution.editor_sessions import EditorStatus
from al1s.infrastructure.editor_relay import DirectConnect, allowed_target
from al1s.maa.errors import MaaDomainError

PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


async def probe_foreground_package(service: EditorSessionService, device_id: UUID) -> str:
    session = await run_in_threadpool(service.current, device_id)
    if session is None or session.status is not EditorStatus.ACTIVE:
        raise MaaDomainError("editor_session_required", "请先连接该手机的视频会话", 409)
    current, connection = await run_in_threadpool(service.detail, session.session_id)
    if current.device_id != device_id or current.status is not EditorStatus.ACTIVE:
        raise MaaDomainError("editor_session_changed", "手机会话已改变, 请重试", 409)
    target = connection.get("foreground_ws_url") if connection else None
    if not target or not allowed_target(target):
        raise MaaDomainError("foreground_probe_unavailable", "终端暂不支持安全的前台应用查询", 409)
    try:
        context = ssl.create_default_context(cafile=os.environ["AL1S_EDITOR_RELAY_CA_FILE"])
        async with DirectConnect(
            target,
            ssl=context,
            proxy=None,
            open_timeout=8,
            close_timeout=2,
            max_size=1024,
            max_queue=1,
            compression=None,
        ) as upstream:
            response = await asyncio.wait_for(upstream.recv(), 15)
        if not isinstance(response, bytes):
            raise ValueError("binary response required")
        data = json.loads(response)
        package = data.get("package_name") if isinstance(data, dict) else None
        if not isinstance(package, str) or not PACKAGE.fullmatch(package):
            raise ValueError("invalid foreground package")
        return package
    except (OSError, TimeoutError, KeyError, ValueError, TypeError,
            ssl.SSLError, ConnectionClosed, InvalidHandshake) as exc:
        raise MaaDomainError(
            "foreground_probe_failed", "无法确认当前前台应用, 请检查手机与终端连接", 503
        ) from exc
