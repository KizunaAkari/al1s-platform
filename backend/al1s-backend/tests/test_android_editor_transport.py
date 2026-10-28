import asyncio
from uuid import uuid4

import pytest

from al1s.execution.android_editor_transport import AndroidEditorHub


def test_duplicate_uplink_and_wrong_identity_are_rejected() -> None:
    async def scenario() -> None:
        hub = AndroidEditorHub()
        session, terminal, device, instance = (uuid4() for _ in range(4))
        channel = hub.open(session, "video", terminal, device, instance)
        with pytest.raises(ValueError):
            hub.open(session, "video", terminal, device, instance)
        with pytest.raises(ValueError):
            hub.get(session, "video", uuid4(), device)
        assert hub.get(session, "video", terminal, device) is channel
        hub.close(session, "video", channel)

    asyncio.run(scenario())


def test_channel_has_bounded_frame_queue() -> None:
    async def scenario() -> None:
        hub = AndroidEditorHub()
        session, terminal, device, instance = (uuid4() for _ in range(4))
        channel = hub.open(session, "video", terminal, device, instance)
        await channel.to_browser.put(b"frame-1")
        await channel.to_browser.put(b"frame-2")
        assert channel.to_browser.full()
        hub.close(session, "video", channel)

    asyncio.run(scenario())
