import pytest

from al1s.components.host.layout import (
    S3_BUNDLE_LAYOUT,
    S3_LAYOUT_LABEL,
    component_services,
    validate_s3_image,
)


LEGACY = ("seaweed-master", "seaweed-volume", "seaweed-filer", "seaweed-s3")


def test_old_and_combined_storage_select_only_their_actual_services():
    old = {"services": {key: {} for key in LEGACY}}
    assert component_services(old, "s3") == LEGACY
    new = {"services": {"storage": {}}}
    assert component_services(new, "s3") == ("storage",)
    new["services"].update({key: {"profiles": ["legacy-storage"]} for key in LEGACY})
    assert component_services(new, "s3") == ("storage",)


@pytest.mark.parametrize("services", [
    {"storage": {}, "seaweed-master": {}},
    {"seaweed-master": {}},
    {},
])
def test_partial_or_simultaneously_active_layout_is_rejected(services):
    with pytest.raises(ValueError, match="layout"):
        component_services({"services": services}, "s3")


def test_combined_candidate_cannot_replace_legacy_and_plain_weed_cannot_replace_bundle():
    bundled = {"Config": {"Labels": {S3_LAYOUT_LABEL: S3_BUNDLE_LAYOUT}}}
    plain = {"Config": {"Labels": {}}}
    validate_s3_image("bundle", bundled)
    validate_s3_image("legacy", plain)
    for layout, image in [("bundle", plain), ("legacy", bundled)]:
        with pytest.raises(ValueError, match="layout"):
            validate_s3_image(layout, image)


def test_preflight_rejects_legacy_package_on_bundle_before_any_switch():
    from al1s.components.contracts import ComponentInventoryEntry, ComponentTarget
    from al1s.components.operations import compatible_release
    from tests.test_component_inventory import release

    current = ComponentInventoryEntry(
        component="s3", upgrade_unit="s3", target=ComponentTarget(kind="platform"),
        display_name="S3", version="4.43", architecture="amd64", status="healthy",
        source_identity="a" * 64, storage_layout="bundle",
    )
    old = release(component="s3", candidate_image="chrislusf/seaweedfs:4.44",
                  native_version="4.44", compatible_from="4")
    new = old.model_copy(update={"candidate_image": "al1s-platform-storage:4.44"})
    assert "component_storage_layout_mismatch" in compatible_release(current, old)
    assert compatible_release(current, new) == []


def test_ops_health_is_authenticated_and_independent_of_bot_target(tmp_path):
    from fastapi.testclient import TestClient
    from al1s.bots.control_proxy import create_proxy
    from al1s.bots.container_control import DockerControl

    control = DockerControl({"qq": "missing-qq"}, str(tmp_path / "missing.sock"))
    with TestClient(create_proxy("a" * 32, control)) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer " + "b" * 32}).status_code == 401
        result = client.get("/health", headers={"Authorization": "Bearer " + "a" * 32})
        assert result.status_code == 200 and result.json() == {"status": "ok"}
