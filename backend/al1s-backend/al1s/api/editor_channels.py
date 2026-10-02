"""Optional same-origin editor transport. No decoding, recording, or arbitrary URLs."""
import asyncio
import os
import ssl
from contextlib import suppress
from uuid import UUID

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from websockets.asyncio.client import ClientConnection

from al1s.app.admin_auth import COOKIE
from al1s.execution.errors import ExecutionDomainError
from al1s.infrastructure.editor_relay import DirectConnect, allowed_target

router = APIRouter()
CHANNELS = {"video", "control", "screenshot", "foreground"}


def browser_connection(
    session_id: UUID, connection: dict[str, str] | None,
) -> dict[str, str] | None:
    if not connection:
        return connection
    result = dict(connection)
    result.pop("app_icon_ws_url", None)
    result.pop("ocr_ws_url", None)
    if not allowed_target(connection["video_ws_url"]):
        return result
    for channel in CHANNELS:
        key = f"{channel}_ws_url"
        if key in result:
            result[key] = f"/api/v1/editor-sessions/{session_id}/channels/{channel}"
    # The browser does not need the upstream bearer capability in relay mode.
    result.pop("session_token", None)
    return result


def authorized(socket: WebSocket) -> bool:
    settings = socket.app.state.settings
    return (
        socket.headers.get("origin") in settings.cors_origin_list
        and socket.headers.get("authorization") is None
        and socket.app.state.admin_sessions.valid(socket.cookies.get(COOKIE, ""))
    )


async def pump(socket: WebSocket, upstream: ClientConnection, channel: str) -> None:
    async def downstream() -> None:
        async for message in upstream:
            if not isinstance(message, bytes):
                raise ValueError("binary channel required")
            await asyncio.wait_for(socket.send_bytes(message), 3)

    async def upload() -> None:
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
            body = message.get("bytes")
            if channel != "control" or body is None or len(body) not in {14, 32}:
                await socket.close(code=1008, reason="channel input rejected")
                return
            await asyncio.wait_for(upstream.send(body), 1)

    async def check_login() -> None:
        while True:
            await asyncio.sleep(5)
            if not authorized(socket):
                await socket.close(code=1008, reason="login expired")
                return

    tasks = [asyncio.create_task(fn()) for fn in (downstream, upload, check_login)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/editor-sessions/{session_id}/channels/{channel}")
async def editor_channel(socket: WebSocket, session_id: UUID, channel: str) -> None:
    if channel not in CHANNELS or not authorized(socket):
        await socket.close(code=1008)
        return
    try:
        _, connection = await run_in_threadpool(socket.app.state.editor_sessions.detail, session_id)
    except ExecutionDomainError:
        await socket.close(code=1008)
        return
    target = connection.get(f"{channel}_ws_url") if connection else None
    if target is None or not allowed_target(target):
        await socket.close(code=1008)
        return
    await socket.accept()
    code, reason = 1000, "channel closed"
    try:
        # Explicit CA, DNS/SNI verification, no environment proxy or redirects to other origins.
        context = ssl.create_default_context(cafile=os.environ["AL1S_EDITOR_RELAY_CA_FILE"])
        async with DirectConnect(
            target, ssl=context, proxy=None, open_timeout=8, close_timeout=2,
            max_size=16 * 1024 * 1024, max_queue=2, write_limit=65536,
            compression=None, ping_interval=20, ping_timeout=10,
        ) as upstream:
            await pump(socket, upstream, channel)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Never log upstream URLs/capability tokens or image content.
        structlog.get_logger(__name__).warning(
            "editor_channel_failed", error_type=type(exc).__name__, channel=channel,
        )
        code, reason = 1011, "terminal channel unavailable"
    finally:
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code=code, reason=reason)
