import asyncio
from dataclasses import replace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.meme_workshop.qq_worker import QqWorker


async def stop_case() -> None:
    repository = MagicMock()
    started, release = asyncio.Event(), asyncio.Event()

    async def late_connections():
        started.set()
        await release.wait()
        return [MagicMock()]

    worker = QqWorker(MagicMock(repository=repository), MagicMock(), connections=late_connections)
    refreshing = asyncio.create_task(worker.refresh())
    await started.wait()
    await worker.stop()
    release.set()
    await refreshing
    assert worker.connection_count == 0


def test_stop_during_configuration_refresh_does_not_start_a_late_connection() -> None:
    asyncio.run(stop_case())


async def changed_connection_case():
    old = QqConnection(uuid4(), uuid4(), 1, "ws://gateway:3001", "http://gateway:3000", 5, None)
    rows = [old]
    repository = MagicMock()
    repository.list_templates.return_value = []
    worker = QqWorker(MagicMock(repository=repository), MagicMock())
    started, stopped = [], []

    async def connections():
        return rows

    async def listen(row):
        started.append(row)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(row)

    worker._connections = connections
    worker.listen = listen
    await worker.refresh()
    await asyncio.sleep(0)
    rows = [replace(old, row_version=2, event_url="ws://gateway:3002")]
    await worker.refresh()
    await asyncio.sleep(0)
    assert started == [old, rows[0]] and stopped == [old]
    assert worker.connection_count == 1
    rows = []
    await worker.refresh()
    assert worker.connection_count == 0 and stopped == started
    await worker.stop()


def test_changed_or_disabled_connection_closes_the_old_receiver_before_starting_another():
    asyncio.run(changed_connection_case())
