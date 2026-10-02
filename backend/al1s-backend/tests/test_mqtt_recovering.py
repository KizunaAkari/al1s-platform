from unittest.mock import Mock

import pytest

from al1s.adapters.mqtt.recovering import RecoveringPublisher


def test_broker_unavailable_is_lazy_bounded_and_recovers():
    clock = [0.0]
    broker = Mock()
    factory = Mock(side_effect=[ConnectionError("offline"), broker])
    publisher = RecoveringPublisher(factory, clock=lambda: clock[0])
    factory.assert_not_called()
    with pytest.raises(ConnectionError):
        publisher.publish(Mock())
    with pytest.raises(ConnectionError):
        publisher.publish(Mock())
    assert factory.call_count == 1
    clock[0] = 5
    event = Mock()
    publisher.publish(event)
    broker.publish.assert_called_once_with(event)
    publisher.close()
    broker.close.assert_called_once()
