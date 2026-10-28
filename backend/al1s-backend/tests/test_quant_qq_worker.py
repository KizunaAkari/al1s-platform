import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.quant_settings import QuantSettingsRecord
from al1s.meme_workshop.qq_worker import QqWorker
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings
from al1s.quant.gateway import QuantGateway


def event():
    return {"post_type": "message", "message_type": "group", "self_id": 999,
            "user_id": 12345, "group_id": 123456, "message_id": 1, "time": 1790000000,
            "message": [{"type": "text", "data": {"text": "模拟开户"}}]}


async def case():
    service = uuid4()
    connection = QqConnection(service, uuid4(), 1, "ws://llbot:3001", "http://llbot:3000", 5, None)
    meme = MagicMock()
    meme.qq_connections.return_value = []
    meme.list_templates.return_value = []
    quant = MagicMock()
    quant.qq_connections.return_value = [connection]
    quant.keyword_conflicts.return_value = False
    settings = QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["123456"])
    quant.snapshots.return_value = {service: QuantSettingsRecord(settings=settings, commands=DEFAULT_COMMANDS,
                                                                 row_version=1, mapping_version=1)}
    process = MagicMock()
    process.activate.return_value = (1, 1)
    gateway, dispatch = QuantGateway(), AsyncMock()
    worker = QqWorker(MagicMock(repository=meme), MagicMock(), quant_repository=quant,
                      quant_gateway=gateway, quant_process=process, quant_dispatch=dispatch)
    async def idle(_row):
        await asyncio.Event().wait()
    worker.listen = idle
    await worker.refresh()
    assert worker.connection_count == 1 and gateway.applied_versions(service) == (1, 1)
    await worker.handle_event(connection, MagicMock(), event())
    await asyncio.sleep(0)
    assert dispatch.await_count == 1 and dispatch.call_args.args[2].source.sender_id == "12345"
    quant.keyword_conflicts.return_value = True
    await worker.handle_event(connection, MagicMock(), event())
    await asyncio.sleep(0)
    assert dispatch.await_count == 1
    # A changed socket address cannot consume events from the previous connection.
    changed = QuantSettingsRecord(settings=QuantSettings(enabled=True, event_ws_url="ws://other:3001",
                                                          groups=["123456"]),
                                  commands=DEFAULT_COMMANDS, row_version=2, mapping_version=1)
    gateway.activate(service, changed)
    quant.keyword_conflicts.return_value = False
    await worker.handle_event(connection, MagicMock(), event())
    await asyncio.sleep(0)
    assert dispatch.await_count == 1
    await worker.stop()


def test_quant_receives_without_meme_and_respects_new_meme_conflicts():
    asyncio.run(case())
