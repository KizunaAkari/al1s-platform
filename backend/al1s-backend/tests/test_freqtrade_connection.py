from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.meme_workshop.qq_worker import QqWorker


def test_sync_requires_the_actual_onebot_socket_to_match_the_freqtrade_url():
    identity = uuid4()
    adapter = MagicMock()
    adapter.matches_connection.return_value = True
    worker = QqWorker(MagicMock(), MagicMock(), freqtrade_adapter=adapter)
    worker._sockets[identity] = MagicMock()
    worker._tasks[identity] = (SimpleNamespace(event_url="ws://actual:3001"), MagicMock())
    assert not worker.is_freqtrade_connected(identity, "ws://other:3001")
    assert worker.is_freqtrade_connected(identity, "ws://actual:3001")
