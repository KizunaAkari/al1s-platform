from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from al1s.bots.errors import BotDomainError
from al1s.bots.gateway_probe import GatewayProbe
from al1s.bots.types import BotConfigApplicationStatus, BotServiceKind


def setup_probe():
    identity, version = uuid4(), uuid4()
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.services.get.return_value = SimpleNamespace(
        enabled=True,
        kind=BotServiceKind.ONEBOT_GATEWAY,
        desired_config_version_id=version,
    )
    candidate = SimpleNamespace(
        application_id=uuid4(),
        config_version_id=version,
        status=BotConfigApplicationStatus.PENDING,
        row_version=1,
    )
    uow.configs.get_pending_for_service.return_value = candidate
    uow.configs.get_application.return_value = candidate
    uow.configs.get_version.return_value = SimpleNamespace(
        secret_id=None,
        settings={"ONEBOT_BASE_URL": "http://gateway:3000"},
    )
    return GatewayProbe(lambda: uow, MagicMock()), uow, identity


@pytest.mark.parametrize("user_id,expected", [(12345, True), (True, False), (0, False)])
def test_probe_accepts_only_valid_login_receipt(user_id, expected):
    probe, uow, identity = setup_probe()
    with patch(
        "al1s.bots.gateway_probe.post_json_direct",
        return_value={
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": user_id},
        },
    ) as post:
        assert probe.apply(identity)["applied"] is expected
        assert post.call_args.args[0].endswith("/get_login_info")
    assert uow.services.set_applied_if_desired.called is expected
    assert uow.audit.add.called


def test_probe_never_applies_obsolete_candidate():
    probe, uow, identity = setup_probe()
    initial = uow.services.get.return_value
    uow.services.get.side_effect = [
        initial,
        SimpleNamespace(
            enabled=True,
            desired_config_version_id=uuid4(),
        ),
    ]
    with (
        patch(
            "al1s.bots.gateway_probe.post_json_direct",
            return_value={
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 12345},
            },
        ),
        pytest.raises(BotDomainError),
    ):
        probe.apply(identity)
    uow.configs.settle_application.assert_not_called()
    uow.commit.assert_not_called()
