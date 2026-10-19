"""Small server-originated network events; business work remains on existing APIs."""
import asyncio
from contextlib import suppress
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.execution.errors import ExecutionDomainError

router = APIRouter()
PULSE_SECONDS = 10
AUTH_SECONDS = 30
PULSE = "al1s-reachability-v1"


async def _pulse(socket: WebSocket, terminal: UUID) -> None:
    checked = monotonic()
    while True:
        if monotonic() - checked >= AUTH_SECONDS:
            current = await run_in_threadpool(
                authenticated_terminal_id, socket, socket.headers.get("authorization")
            )
            if current != terminal:
                raise ValueError("terminal_identity_changed")
            checked = monotonic()
        await asyncio.wait_for(socket.send_text(PULSE), 3)
        await asyncio.sleep(PULSE_SECONDS)


async def _disconnect(socket: WebSocket) -> None:
    message = await socket.receive()
    if message["type"] != "websocket.disconnect":
        raise ValueError("reachability_is_receive_only")


@router.websocket("/terminal/reachability")
async def terminal_reachability(socket: WebSocket) -> None:
    tasks: list[asyncio.Task[None]] = []
    code = 1000
    try:
        if socket.headers.get("origin") is not None:
            raise ValueError("terminal_context_required")
        terminal = await run_in_threadpool(
            authenticated_terminal_id, socket, socket.headers.get("authorization")
        )
        await socket.accept()
        tasks = [asyncio.create_task(_pulse(socket, terminal)),
                 asyncio.create_task(_disconnect(socket))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (ExecutionDomainError, ValueError, TimeoutError):
        code = 1008
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close(code=code)
