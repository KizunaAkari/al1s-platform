#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触发范围与冷却 —— 纯逻辑层（不依赖 AstrBot 运行时，便于离线测试）。

职责：冷却记录和滑动窗口限次。
"""
from __future__ import annotations

import time
from collections import deque

class CooldownTracker:
    """基于 ``time.monotonic`` 的冷却记录器（内存态，重启即清空）。"""

    def __init__(self) -> None:
        self._next_time: dict[str, float] = {}

    def remaining(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self._next_time.get(key, 0.0) - now)

    def allow(self, key: str, cooldown: float, now: float | None = None) -> bool:
        if cooldown <= 0:
            return True
        return self.remaining(key, now) <= 0

    def touch(self, key: str, cooldown: float, now: float | None = None) -> None:
        if cooldown <= 0:
            return
        now = time.monotonic() if now is None else now
        self._next_time[key] = now + cooldown

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._next_time.clear()
        else:
            self._next_time.pop(key, None)

    def cleanup(self, now: float | None = None) -> None:
        """清理已经过期的记录，避免长期运行内存缓慢增长。"""
        now = time.monotonic() if now is None else now
        for k in [k for k, v in self._next_time.items() if v <= now]:
            self._next_time.pop(k, None)


class BurstLimiter:
    """滑动窗口限次：一段时间内最多自动回复 N 次（内存态，重启即清空）。

    与 ``CooldownTracker`` 互补：冷却管「两次回复之间至少隔多久」，
    限次管「一小段时间里一共最多回几次」，防止窗口一到就连续插话。
    """

    def __init__(self) -> None:
        self._marks: dict[str, deque] = {}

    def _prune(self, key: str, window: float, now: float) -> deque:
        q = self._marks.get(key)
        if q is None:
            q = deque()
            self._marks[key] = q
        while q and now - q[0] >= window:
            q.popleft()
        return q

    def count(self, key: str, window: float = 120.0,
              now: float | None = None) -> int:
        """窗口内已经回复过几次。"""
        if window <= 0:
            return 0
        now = time.monotonic() if now is None else now
        q = self._marks.get(key)
        if not q:
            return 0
        while q and now - q[0] >= window:
            q.popleft()
        return len(q)

    def allow(self, key: str, limit: int = 2, window: float = 120.0,
              now: float | None = None) -> bool:
        """还能不能再回一次；``limit <= 0`` 表示不限次。"""
        if not limit or int(limit) <= 0:
            return True
        return self.count(key, window=window, now=now) < int(limit)

    def remaining(self, key: str, limit: int = 2, window: float = 120.0,
                  now: float | None = None) -> float:
        """达到上限时，还要等多少秒才腾出配额；未达上限返回 0。"""
        if not limit or int(limit) <= 0 or window <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        q = self._marks.get(key)
        if not q:
            return 0.0
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) < int(limit):
            return 0.0
        oldest = q[len(q) - int(limit)]
        return max(0.0, window - (now - oldest))

    def note(self, key: str, window: float = 120.0,
             now: float | None = None) -> None:
        """记一次已经发出去的回复。"""
        if window <= 0:
            return
        now = time.monotonic() if now is None else now
        self._prune(key, window, now).append(now)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._marks.clear()
        else:
            self._marks.pop(str(key or ""), None)

    def cleanup(self, now: float | None = None, window: float = 120.0) -> None:
        """清理窗口内已经没有记录的空队列，避免长期运行内存缓慢增长。"""
        now = time.monotonic() if now is None else now
        for k in list(self._marks):
            if not self._prune(k, window, now):
                self._marks.pop(k, None)
