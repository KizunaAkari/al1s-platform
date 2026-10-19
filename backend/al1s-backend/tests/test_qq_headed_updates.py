from types import SimpleNamespace

import pytest

from al1s.bots.qq_headed.profile import prepare_client_profile
from al1s.components.host.qq_headed_identity import observe_headed_identity
from al1s.components.operations import compatible_release
from al1s.meme_workshop.operation_identity import stored_identity


def test_headed_identity_uses_client_instance_and_checks_account(tmp_path):
    profile = prepare_client_profile(tmp_path, "123456")
    identity = stored_identity("qq", {"AUTO_LOGIN_QQ": "123456", "PROTOCOL_MODE": "pmhq"}, tmp_path)
    assert identity.binding == {"account": "123456", "mode": "pmhq",
                                "client_instance_id": str(profile.instance_id)}
    with pytest.raises(ValueError, match="account"):
        observe_headed_identity(tmp_path, "654321")


def test_headed_preflight_rejects_direct_runtime_and_ignores_direct_pending():
    current = SimpleNamespace(qq_protocol_mode="pmhq", qq_identity=None, upgrade_unit="qq-bot",
        status="healthy", source_identity="a"*64, target=SimpleNamespace(kind="bot"),
        architecture="amd64", version="8.3.2")
    release = SimpleNamespace(component="qq-bot", runtime_version="1.1.0", architecture="amd64",
        native_version="8.3.2", compatible_from="8.3.2")
    assert "qq_headed_runtime_required" in compatible_release(current, release)
    release.runtime_version = "2.0.0"
    assert compatible_release(current, release) == []
