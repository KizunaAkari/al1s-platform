import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass


gpu_owner = ContextVar("al1s_gpu_owner", default=None)


@dataclass
class _Ticket:
    identity: str
    owner: object
    ready: asyncio.Future
    cancel_requested: bool = False


class GpuQueue:
    def __init__(self, *, recovering=False, max_pending=5):
        self.max_pending = max_pending
        self._recovering = recovering
        self._entries = {}
        self._waiting = deque()
        self._active = None
        self._recovered = asyncio.Event()
        if not recovering:
            self._recovered.set()

    @property
    def active(self):
        return self._active.identity if self._active else None

    @property
    def pending(self):
        return len(self._waiting)

    def position(self, identity):
        if self._recovering:
            return None
        if self._active is not None and self._active.identity == identity:
            return 0
        return next((index for index, ticket in enumerate(self._waiting, 1)
                     if ticket.identity == identity), None)

    @property
    def idle(self):
        return not self._recovering and self._active is None and not self._waiting

    @property
    def recovering(self):
        return self._recovering

    def _ticket(self, identity, owner):
        if not isinstance(identity, str) or not identity:
            raise ValueError("gpu_request_invalid")
        return _Ticket(identity, owner, asyncio.get_running_loop().create_future())

    def reserve(self, identity, *, owner=None):
        existing = self._entries.get(identity)
        if existing is not None:
            if existing.owner != owner:
                raise ValueError("gpu_request_conflict")
            return False
        if len(self._entries) >= self.max_pending + 1:
            raise ValueError("gpu_queue_full")
        ticket = self._ticket(identity, owner)
        self._entries[identity] = ticket
        self._waiting.append(ticket)
        self._promote()
        return True

    def _promote(self):
        if self._recovering or self._active is not None or not self._waiting:
            return
        self._active = self._waiting.popleft()
        if not self._active.ready.done():
            self._active.ready.set_result(None)

    async def wait(self, identity, *, timeout=1800):
        ticket = self._entries.get(identity)
        if ticket is None:
            raise ValueError("gpu_request_missing")
        try:
            await asyncio.wait_for(asyncio.shield(ticket.ready), timeout)
        except (asyncio.CancelledError, TimeoutError):
            self.finish(identity)
            raise

    async def wait_recovery(self, *, timeout=30):
        await asyncio.wait_for(self._recovered.wait(), timeout)

    def check_vision(self, identity):
        ticket = self._entries.get(identity)
        if ticket is None or ticket.cancel_requested:
            raise ValueError("gpu_vision_disabled")

    def vision_cancel_requested(self, identity):
        ticket = self._entries.get(identity)
        return bool(ticket and ticket.cancel_requested)

    def cancel_waiting_vision(self, service_id):
        for ticket in list(self._waiting):
            if (ticket.identity.startswith("vision:") and isinstance(ticket.owner, tuple)
                    and ticket.owner[0] == service_id):
                self._waiting.remove(ticket)
                self._entries.pop(ticket.identity, None)
                ticket.ready.set_exception(ValueError("gpu_vision_disabled"))
                ticket.ready.add_done_callback(lambda future: future.exception())
        ticket = self._active
        if (ticket is not None and ticket.identity.startswith("vision:")
                and isinstance(ticket.owner, tuple) and ticket.owner[0] == service_id):
            ticket.cancel_requested = True

    def finish(self, identity):
        ticket = self._entries.pop(identity, None)
        if ticket is None:
            return False
        if self._active is ticket:
            self._active = None
        else:
            self._waiting.remove(ticket)
            if not ticket.ready.done():
                ticket.ready.cancel()
        self._promote()
        return True

    def restore(self, *, active=None, pending=()):
        if not self._recovering:
            raise ValueError("gpu_recovery_closed")
        existing = list(self._waiting)
        recovered = ([active] if active else []) + list(pending)
        if len({identity for identity, _owner in recovered}) != len(recovered):
            raise ValueError("gpu_recovery_conflict")
        restored = []
        for identity, owner in recovered:
            ticket = self._entries.get(identity)
            if ticket is not None and ticket.owner != owner:
                raise ValueError("gpu_recovery_conflict")
            if ticket is None:
                ticket = self._ticket(identity, owner)
                self._entries[identity] = ticket
            else:
                existing.remove(ticket)
            restored.append(ticket)
        available = max(0, self.max_pending + 1 - len(restored))
        keep, overflow = existing[:available], existing[available:]
        for ticket in overflow:
            self._entries.pop(ticket.identity, None)
            ticket.ready.set_exception(ValueError("gpu_queue_full"))
            ticket.ready.add_done_callback(lambda future: future.exception())
        self._waiting = deque([*restored, *keep])
        self._recovering = False
        self._recovered.set()
        self._promote()
