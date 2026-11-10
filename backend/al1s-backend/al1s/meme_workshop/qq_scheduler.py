import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from enum import StrEnum

from al1s.meme_workshop.contracts import CommandSource


class Admission(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    BUSY = "busy"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ScheduledItem:
    kind: str
    session_key: tuple[Hashable, ...]
    source: CommandSource
    operation: Callable[[], Awaitable[None]]
    expires_at: float


class QqScheduler:
    def __init__(self, *, active_limit: int = 2, queue_limit: int = 32, per_session: int = 3,
                 queue_timeout: float = 15, on_expire: Callable | None = None,
                 on_error: Callable | None = None, vision_limit: int = 7) -> None:
        if min(active_limit, queue_limit, per_session, queue_timeout, vision_limit) <= 0:
            raise ValueError("qq_scheduler_invalid_limits")
        self.active_limit, self.queue_limit = active_limit, queue_limit
        self.vision_limit = vision_limit
        self.per_session, self.queue_timeout = per_session, queue_timeout
        self.on_expire, self.on_error = on_expire, on_error
        self._queues: dict[tuple[Hashable, ...], deque[ScheduledItem]] = {}
        self._ready: deque[tuple[Hashable, ...]] = deque()
        self._ready_set: set[tuple[Hashable, ...]] = set()
        self._active: dict[tuple[Hashable, ...], asyncio.Task] = {}
        self._active_kinds: dict[tuple[Hashable, ...], str] = {}
        self._blocked: set[tuple[Hashable, ...]] = set()
        self._notifications: set[asyncio.Task] = set()
        self._seen: set[tuple[Hashable, str]] = set()
        self._pending = 0
        self._timer: asyncio.TimerHandle | None = None
        self._stopped = False

    def try_submit(self, kind: str, session_key: tuple[Hashable, ...], source: CommandSource,
                   operation: Callable[[], Awaitable[None]], *, per_session: int | None = None,
                   queue_timeout: float | None = None) -> Admission:
        if self._stopped or not session_key:
            return Admission.BUSY
        identity = (session_key[0], source.message_id)
        if identity in self._seen:
            return Admission.DUPLICATE
        queue = self._queues.get(session_key)
        limit = self.per_session if per_session is None else per_session
        timeout = self.queue_timeout if queue_timeout is None else queue_timeout
        if limit <= 0 or timeout <= 0:
            raise ValueError("qq_scheduler_invalid_limits")
        if (self._pending >= self.queue_limit
                or (queue is not None and len(queue) >= limit)):
            return Admission.BUSY
        loop = asyncio.get_running_loop()
        item = ScheduledItem(kind, session_key, source, operation, loop.time() + timeout)
        self._queues.setdefault(session_key, deque()).append(item)
        self._pending += 1
        self._seen.add(identity)
        self._mark_ready(session_key)
        self._pump()
        return Admission.ACCEPTED

    def _mark_ready(self, key: tuple[Hashable, ...]) -> None:
        if (key not in self._active and key not in self._blocked
                and key not in self._ready_set and self._queues.get(key)):
            self._ready.append(key)
            self._ready_set.add(key)

    def _remove_pending(self, item: ScheduledItem, *, expired: bool) -> None:
        self._pending -= 1
        self._seen.discard((item.session_key[0], item.source.message_id))
        if expired and self.on_expire is not None:
            result = self.on_expire(item)
            if inspect.isawaitable(result):
                task = asyncio.create_task(result)
                self._notifications.add(task)
                task.add_done_callback(lambda completed: self._notification_done(item, completed))

    def _notification_done(self, item: ScheduledItem, task: asyncio.Task) -> None:
        self._notifications.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None and self.on_error:
            self.on_error(item, error)

    def _pump(self) -> None:
        if self._stopped:
            return
        now = asyncio.get_running_loop().time()
        for key, queue in list(self._queues.items()):
            remaining = deque()
            for item in queue:
                if item.expires_at <= now:
                    self._remove_pending(item, expired=True)
                else:
                    remaining.append(item)
            if remaining:
                self._queues[key] = remaining
            else:
                self._queues.pop(key, None)
        attempts = len(self._ready)
        while self._ready and attempts:
            key = self._ready.popleft()
            self._ready_set.discard(key)
            queue = self._queues.get(key)
            if not queue or key in self._active or key in self._blocked:
                attempts -= 1
                continue
            vision = queue[0].kind == "ai_vision"
            active = sum((kind == "ai_vision") == vision for kind in self._active_kinds.values())
            if active >= (self.vision_limit if vision else self.active_limit):
                self._mark_ready(key)
                attempts -= 1
                continue
            item = queue.popleft()
            self._pending -= 1
            if not queue:
                self._queues.pop(key, None)
            task = asyncio.create_task(item.operation())
            self._active[key] = task
            self._active_kinds[key] = item.kind
            task.add_done_callback(lambda completed, item=item: self._completed(item, completed))
            attempts = len(self._ready)
        self._schedule_expiry()

    def _completed(self, item: ScheduledItem, task: asyncio.Task) -> None:
        self._active.pop(item.session_key, None)
        self._active_kinds.pop(item.session_key, None)
        self._seen.discard((item.session_key[0], item.source.message_id))
        if not task.cancelled():
            error = task.exception()
            if error is not None and self.on_error is not None:
                self.on_error(item, error)
        self._mark_ready(item.session_key)
        self._pump()

    def _schedule_expiry(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._pending:
            earliest = min(item.expires_at for queue in self._queues.values() for item in queue)
            self._timer = asyncio.get_running_loop().call_at(earliest, self._pump)

    async def drain(self) -> None:
        while self._active or self._pending:
            if self._active:
                await asyncio.gather(*tuple(self._active.values()), return_exceptions=True)
                await asyncio.sleep(0)
            else:
                self._pump()
                await asyncio.sleep(0)
        if self._notifications:
            await asyncio.gather(*tuple(self._notifications), return_exceptions=True)

    def block(self, session_key: tuple[Hashable, ...]) -> None:
        self._blocked.add(session_key)

    def active(self, session_key: tuple[Hashable, ...]) -> bool:
        return session_key in self._active

    def active_for_service(self, service_id: Hashable) -> bool:
        return any(key[0] == service_id for key in self._active)

    def drop_pending(self, session_key: tuple[Hashable, ...]) -> list[ScheduledItem]:
        items = list(self._queues.pop(session_key, ()))
        for item in items:
            self._remove_pending(item, expired=False)
        self._pump()
        return items

    def drop_vision(self, service_id: Hashable) -> list[ScheduledItem]:
        dropped = []
        for key, items in list(self._queues.items()):
            if key[0] != service_id:
                continue
            keep = deque()
            for item in items:
                if item.kind == "ai_vision":
                    dropped.append(item)
                    self._remove_pending(item, expired=False)
                else:
                    keep.append(item)
            if keep:
                self._queues[key] = keep
            else:
                self._queues.pop(key, None)
        self._pump()
        return dropped

    def unblock(self, session_key: tuple[Hashable, ...]) -> None:
        self._blocked.discard(session_key)
        self._mark_ready(session_key)
        self._pump()

    async def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        for queue in self._queues.values():
            for item in queue:
                self._seen.discard((item.session_key[0], item.source.message_id))
        self._queues.clear()
        self._ready.clear()
        self._ready_set.clear()
        self._blocked.clear()
        self._pending = 0
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        notifications = tuple(self._notifications)
        if notifications:
            done, pending = await asyncio.wait(notifications, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
