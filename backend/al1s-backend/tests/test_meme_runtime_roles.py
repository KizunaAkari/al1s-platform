from unittest.mock import MagicMock

import pytest

from al1s.meme_workshop.runtime_roles import make_qq_worker, render_limit


@pytest.mark.parametrize(
    "role,worker,limit", [("qq", True, 1), ("discord", False, 1), ("combined", True, 2)]
)
def test_role_selects_worker_and_budget(role, worker, limit):
    assert render_limit(role) == limit
    assert (make_qq_worker(role, MagicMock(), MagicMock()) is not None) == worker


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="role"):
        render_limit("other")


def test_service_enforces_its_local_budget():
    from al1s.meme_workshop.service import MemeWorkshopService

    service = MemeWorkshopService(MagicMock(), MagicMock(), render_limit=1)
    with pytest.raises(ValueError, match="render_busy"):
        service.pool.run(lambda: service.pool.run(lambda: None))
    assert service.pool.active == 0
    assert service.pool.run(lambda: 7) == 7
