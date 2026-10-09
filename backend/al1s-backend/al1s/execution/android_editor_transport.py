"""Bounded reverse channels owned by one authenticated editor generation.

The API deployment uses one ASGI worker. Channels are ephemeral; losing that
worker closes the sockets, while the persisted editor session remains authoritative.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class AndroidEditorChannel:
    terminal_id: UUID
    device_id: UUID
    instance_id: UUID
    to_browser: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(2))
    to_terminal: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(2))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    browser_attached: bool = False


class AndroidEditorHub:
    """One event-loop owner; no database access or unbounded buffering per frame."""

    def __init__(self) -> None:
        self._channels: dict[tuple[UUID, str], AndroidEditorChannel] = {}

    def open(
        self,
        session_id: UUID,
        channel: str,
        terminal_id: UUID,
        device_id: UUID,
        instance_id: UUID,
    ) -> AndroidEditorChannel:
        key = session_id, channel
        if key in self._channels:
            raise ValueError("editor uplink already attached")
        item = AndroidEditorChannel(terminal_id, device_id, instance_id)
        self._channels[key] = item
        return item

    def get(
        self,
        session_id: UUID,
        channel: str,
        terminal_id: UUID,
        device_id: UUID,
    ) -> AndroidEditorChannel:
        item = self._channels.get((session_id, channel))
        if item is None or item.closed.is_set():
            raise ValueError("editor uplink unavailable")
        if item.terminal_id != terminal_id or item.device_id != device_id:
            raise ValueError("editor uplink identity mismatch")
        return item

    def close(
        self,
        session_id: UUID,
        channel: str,
        item: AndroidEditorChannel,
    ) -> None:
        if self._channels.get((session_id, channel)) is item:
            del self._channels[session_id, channel]
            item.closed.set()

    async def request(
        self,
        session_id: UUID,
        channel: str,
        terminal_id: UUID,
        device_id: UUID,
        instance_id: UUID | None,
        payload: bytes,
    ) -> bytes:
        item = self.get(session_id, channel, terminal_id, device_id)
        if item.browser_attached or item.instance_id != instance_id:
            raise ValueError("terminal request channel unavailable")
        item.browser_attached = True
        receive = None
        closed = None
        try:
            while not item.to_browser.empty():
                item.to_browser.get_nowait()
            await asyncio.wait_for(item.to_terminal.put(payload), 3)
            receive = asyncio.create_task(item.to_browser.get())
            closed = asyncio.create_task(item.closed.wait())
            done, _ = await asyncio.wait(
                [receive, closed], timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            if closed in done:
                raise ValueError("terminal request channel closed")
            if receive not in done:
                raise TimeoutError("terminal request timed out")
            return receive.result()
        except BaseException:
            # A late response must never be consumed by a later request.
            self.close(session_id, channel, item)
            raise
        finally:
            item.browser_attached = False
            for task in (receive, closed):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(t for t in (receive, closed) if t is not None), return_exceptions=True
            )
