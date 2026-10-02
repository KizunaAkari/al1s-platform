from __future__ import annotations

from threading import Event

from al1s.infrastructure.notification_worker import _dispatch_notifications, run_loop
from al1s.notifications.types import NotificationDispatchResult


class FailingDispatcher:
    def dispatch_once(self) -> NotificationDispatchResult:
        raise RuntimeError("database temporarily unavailable")


class LoggerSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def error(self, event: str, **event_kw: object) -> None:
        self.calls.append((event, str(event_kw["error"])))

    def info(self, event: str, **event_kw: object) -> None:
        pass


def test_notification_failure_is_isolated_from_platform_worker_cycle() -> None:
    logger = LoggerSpy()

    result = _dispatch_notifications(FailingDispatcher(), logger)

    assert result.claimed == 0
    assert result.sent == 0
    assert logger.calls == [("notification_dispatch_cycle_failed", "RuntimeError")]


def test_cycle_error_does_not_prevent_next_attempt_and_stop_is_honored() -> None:
    stop = Event()

    class Dispatcher:
        calls = 0

        def dispatch_once(self) -> NotificationDispatchResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("do-not-log-payload-secret")
            stop.set()
            return NotificationDispatchResult(1, 1, 0, 0, 0)

    dispatcher = Dispatcher()
    logger = LoggerSpy()
    run_loop(dispatcher, logger, stop, interval=0.001)
    assert dispatcher.calls == 2
    assert logger.calls == [("notification_dispatch_cycle_failed", "RuntimeError")]


def test_stopped_process_does_not_claim_more_work() -> None:
    stop = Event()
    stop.set()
    logger = LoggerSpy()
    run_loop(FailingDispatcher(), logger, stop, interval=0.001)
    assert not logger.calls
