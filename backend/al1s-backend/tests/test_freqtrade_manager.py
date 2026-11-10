import time
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Event, RLock
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from al1s.freqtrade.contracts import FreqtradeSettings
import pytest

from al1s.freqtrade.manager import FreqtradeManager, confirm_exit


def init_concurrency(manager):
    manager.lock = RLock()
    manager.condition = Condition(manager.lock)
    manager._busy = set()
    manager._closing = False


def test_idle_cleanup_preserves_held_accounts_and_batches_lifecycle_updates():
    manager = object.__new__(FreqtradeManager)
    empty_id, held_id = uuid4(), uuid4()
    empty = SimpleNamespace(profile=None, last_used=time.monotonic()-400, call=MagicMock(return_value=[]))
    held = SimpleNamespace(profile=None, last_used=time.monotonic()-400, call=MagicMock(return_value=[{'pair':'BTC/USDT'}]))
    manager.instances = {empty_id:empty, held_id:held}
    init_concurrency(manager)
    manager.repository = MagicMock()
    manager.repository.runtime_settings.return_value = {empty_id:FreqtradeSettings(), held_id:FreqtradeSettings()}
    manager._terminate = MagicMock()
    manager.idle_cleanup()
    assert set(manager.instances) == {held_id}
    manager.repository.runtime_settings.assert_called_once()
    manager.repository.mark_inactive.assert_called_once_with([empty_id])


def test_shutdown_terminates_children_even_if_notice_checkpoint_fails():
    manager = object.__new__(FreqtradeManager)
    identity = uuid4()
    native = SimpleNamespace(profile=SimpleNamespace(market="futures"),
                             process=MagicMock(), notice_lock=RLock())
    native.process.poll.return_value = None
    manager.instances = {identity: native}
    init_concurrency(manager)
    manager.repository, manager.database, manager.notices = MagicMock(), MagicMock(), MagicMock()
    manager.notices.catch_up.side_effect = OSError("database unavailable")
    manager._terminate = MagicMock()
    manager.close()
    manager._terminate.assert_called_once_with(native)
    manager.repository.mark_inactive.assert_not_called()
    assert manager.instances == {}


def test_concurrent_start_for_one_account_reuses_the_same_instance():
    manager = object.__new__(FreqtradeManager)
    init_concurrency(manager)
    manager.instances, manager.repository = {}, MagicMock()
    manager.capacity = 5
    profile = SimpleNamespace(id=uuid4(), market='spot')
    settings = FreqtradeSettings()
    native = SimpleNamespace(fee=str(settings.fee), last_used=0, process=MagicMock())
    native.process.poll.return_value = None
    started, release = Event(), Event()
    def start(_profile, _settings):
        started.set()
        assert release.wait(5)
        return native
    manager._start = MagicMock(side_effect=start)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.ensure, profile, settings)
        assert started.wait(5)
        second = pool.submit(manager.ensure, profile, settings)
        release.set()
        assert first.result(timeout=5) is native
        assert second.result(timeout=5) is native
    manager._start.assert_called_once()
    manager.repository.set_profile.assert_called_once_with(profile.id, market='spot', native_active=True)


def test_exit_requires_an_explicit_acknowledgement_for_the_correct_trade():
    confirm_exit({'result': 'Created exit order for trade 4.'}, 4)
    with pytest.raises(OSError, match='freqtrade_exit_result_unconfirmed'):
        confirm_exit({'result': 'Created exit order'}, 4)
    with pytest.raises(OSError, match='freqtrade_exit_result_unconfirmed'):
        confirm_exit({'result': 'Created exit order for trade 5.'}, 4)
    with pytest.raises(ValueError, match='未受理'):
        confirm_exit({'status': 'error'}, 4)
