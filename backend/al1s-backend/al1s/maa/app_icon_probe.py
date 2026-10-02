"""Fetch one app icon through the active, authenticated editor relay."""

import asyncio
import logging
import os
import ssl
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from al1s.execution.editor_service import EditorSessionService
from al1s.execution.editor_sessions import EditorStatus
from al1s.infrastructure.editor_relay import DirectConnect, allowed_target
from al1s.maa.application_icon import MAX_UPLOAD_BYTES, normalize_icon

logger = logging.getLogger(__name__)


async def fetch_application_icon(
    service: EditorSessionService, device_id: UUID, package_name: str,
) -> bytes | None:
    """A failed optional probe leaves script creation and existing icons intact."""
    try:
        session = await run_in_threadpool(service.current, device_id)
        if session is None or session.status is not EditorStatus.ACTIVE:
            return None
        current, connection = await run_in_threadpool(service.detail, session.session_id)
        if current.device_id != device_id or current.status is not EditorStatus.ACTIVE:
            return None
        target = connection.get("app_icon_ws_url") if connection else None
        if not target or not allowed_target(target):
            return None
        context = ssl.create_default_context(cafile=os.environ["AL1S_EDITOR_RELAY_CA_FILE"])
        async with DirectConnect(
            target, ssl=context, proxy=None, open_timeout=8, close_timeout=2,
            max_size=MAX_UPLOAD_BYTES, max_queue=1, compression=None,
        ) as upstream:
            await asyncio.wait_for(upstream.send(package_name.encode("ascii")), 3)
            payload = await asyncio.wait_for(upstream.recv(), 20)
        if not isinstance(payload, bytes):
            return None
        return normalize_icon(payload)
    except Exception as exc:
        logger.warning("Optional application icon probe failed: %s", type(exc).__name__)
        return None
