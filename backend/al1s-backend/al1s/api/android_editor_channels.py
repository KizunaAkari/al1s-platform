"""Authenticated phone uplinks; browsers never receive terminal credentials."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import cast
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.admin_auth import COOKIE, AdminSessions
from al1s.app.service_state import service_state
from al1s.execution.android_editor_transport import AndroidEditorChannel
from al1s.execution.editor_sessions import EditorStatus
from al1s.execution.errors import ExecutionDomainError

router = APIRouter()
CHANNELS = {"video", "control", "screenshot", "foreground", "ocr", "app_icon"}
MAX_FRAME = 16 * 1024 * 1024


async def relay(
    socket: WebSocket,
    item: AndroidEditorChannel,
    *,
    browser: bool,
    channel: str,
    check_authority: Callable[[], Awaitable[bool]],
) -> None:
    receive_queue = item.to_browser if browser else item.to_terminal
    send_queue = item.to_terminal if browser else item.to_browser

    async def receive() -> None:
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
            body = message.get("bytes")
            if body is None or not 0 < len(body) <= MAX_FRAME:
                raise ValueError("invalid editor frame")
            if browser and (channel != "control" or len(body) not in {14, 32}):
                raise ValueError("invalid input frame")
            await asyncio.wait_for(send_queue.put(body), 3)

    async def send() -> None:
        while True:
            await asyncio.wait_for(socket.send_bytes(await receive_queue.get()), 3)

    async def check() -> None:
        while True:
            await asyncio.sleep(5)
            if not await check_authority():
                raise ValueError("editor authority expired")

    tasks = [asyncio.create_task(fn()) for fn in (receive, send, check, item.closed.wait)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve_browser(socket: WebSocket, session_id: UUID, channel: str) -> None:
    services = service_state(socket)
    item = None
    try:
        session, connection = await run_in_threadpool(services.editor_sessions.detail, session_id)
        if (
            not connection
            or connection.get("transport") != "android-reverse-v1"
            or session.status is not EditorStatus.ACTIVE
        ):
            raise ValueError("reverse session unavailable")
        item = services.android_editor_hub.get(
            session_id,
            channel,
            session.terminal_id,
            session.device_id,
        )
        if item.browser_attached or item.instance_id != session.terminal_instance_id:
            raise ValueError("reverse generation unavailable")
        item.browser_attached = True
        await socket.accept()
        await item.to_terminal.put(b"open")

        async def check() -> bool:
            return cast(AdminSessions, socket.app.state.admin_sessions).valid(
                socket.cookies.get(COOKIE, "")
            )

        await relay(socket, item, browser=True, channel=channel, check_authority=check)
    except (ExecutionDomainError, ValueError, TimeoutError, WebSocketDisconnect):
        pass
    finally:
        if item is not None:
            item.browser_attached = False
            services.android_editor_hub.close(session_id, channel, item)
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code=1000)


@router.websocket("/terminal/editor-sessions/{session_id}/uplink/{channel}")
async def android_uplink(
    socket: WebSocket,
    session_id: UUID,
    channel: str,
    instance_id: UUID,
) -> None:
    services = service_state(socket)
    item = None
    try:
        if channel not in CHANNELS or socket.headers.get("origin") is not None:
            raise ValueError("invalid terminal channel")
        terminal_id = await run_in_threadpool(
            authenticated_terminal_id,
            socket,
            socket.headers.get("authorization"),
        )
        session, connection = await run_in_threadpool(services.editor_sessions.detail, session_id)
        if (
            session.terminal_id != terminal_id
            or session.terminal_instance_id != instance_id
            or session.status is not EditorStatus.ACTIVE
            or not connection
            or connection.get("transport") != "android-reverse-v1"
        ):
            raise ValueError("invalid terminal generation")
        item = services.android_editor_hub.open(
            session_id,
            channel,
            terminal_id,
            session.device_id,
            instance_id,
        )
        await socket.accept()

        async def check() -> bool:
            current_terminal = await run_in_threadpool(
                authenticated_terminal_id,
                socket,
                socket.headers.get("authorization"),
            )
            current, _ = await run_in_threadpool(services.editor_sessions.detail, session_id)
            return (
                current_terminal == terminal_id
                and current.status is EditorStatus.ACTIVE
                and current.terminal_instance_id == instance_id
            )

        await relay(socket, item, browser=False, channel=channel, check_authority=check)
    except (ExecutionDomainError, ValueError, TimeoutError, WebSocketDisconnect):
        pass
    finally:
        if item is not None:
            services.android_editor_hub.close(session_id, channel, item)
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code=1000)
