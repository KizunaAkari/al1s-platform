import copy

import pytest
from fastapi.testclient import TestClient

from al1s.components.host.app import create_host
from al1s.components.host.container_config import relocate_settings
from tests.test_component_host_drivers import settings


def registration(root):
    return {
        "deployment_root": root,
        "compose_files": [root + "/compose/compose.yaml"],
        "env_files": [root + "/platform.env"],
        "selectors_file": root + "/components.env",
        "state_dir": root + "/runtime/state",
        "certificate_file": root + "/runtime/server.crt",
        "private_key_file": root + "/runtime/server.key",
        "platform": {
            "url": "https://127.0.0.1:8443",
            "token": "private-token" * 4,
            "ca_file": root + "/tls/ca.crt",
        },
    }


@pytest.mark.parametrize("root", ["C:/Users/YY/deployment", "/opt/al1s/deployment"])
def test_registration_preserves_engine_paths_identity_and_credentials(root):
    source = registration(root)
    before = copy.deepcopy(source)
    result = relocate_settings(source, "/registered/deployment", "https://platform.local:8443")
    assert result["state_dir"] == "/registered/deployment/runtime/state"
    assert result["compose_files"] == ["/registered/deployment/compose/compose.yaml"]
    assert result["platform"]["ca_file"] == "/registered/deployment/tls/ca.crt"
    assert result["platform"]["token"] == source["platform"]["token"]
    assert result["platform"]["url"] == "https://platform.local:8443"
    assert source == before


@pytest.mark.parametrize("key", ["state_dir", "selectors_file", "private_key_file"])
def test_registration_rejects_outside_root_and_parent_traversal(key):
    source = registration("C:/registered/deployment")
    source[key] = "C:/registered/deployment/../other/secret"
    with pytest.raises(ValueError, match="registered"):
        relocate_settings(source, "/registered/deployment", "https://platform.local")


@pytest.mark.parametrize("root", ["relative/path", "/", "/root/../other", "/root,other"])
def test_registration_rejects_ambiguous_engine_root(root):
    with pytest.raises(ValueError, match="registered"):
        relocate_settings(registration("C:/registered"), root, "https://platform.local")


def test_maintenance_health_requires_credential_but_not_running_platform(tmp_path):
    configured = settings(tmp_path)
    with TestClient(create_host(configured)) as client:
        assert client.get("/health").status_code == 401
        response = client.get("/health", headers={"Authorization": "Bearer " + "x" * 32})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
