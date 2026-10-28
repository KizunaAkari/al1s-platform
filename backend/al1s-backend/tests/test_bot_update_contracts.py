from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.components.contracts import ComponentRelease, ComponentTarget


def registry(**values):
    return dict(
        release_id=uuid4(),
        component="qq-bot",
        version="8.3.2+1.0.0",
        native_version="8.3.2",
        architecture="amd64",
        compatible_from="8",
        source_kind="registry",
        registry_digest="sha256:" + "a" * 64,
        platform_manifest_digest="sha256:" + "b" * 64,
        runtime_artifact_digest="sha256:" + "c" * 64,
        runtime_version="1.0.0",
        state="draft",
        row_version=1,
        created_at=datetime.now(UTC),
        **values,
    )


def test_bot_targets_have_independent_scopes():
    assert ComponentTarget(kind="bot", bot_alias="qq").scope == "bot:qq"
    assert ComponentTarget(kind="bot", bot_alias="discord").scope == "bot:discord"
    with pytest.raises(ValueError):
        ComponentTarget(kind="bot")


def test_registry_draft_has_no_archive_and_is_not_an_installable_image():
    value = ComponentRelease(**registry())
    assert value.blob_id is None and value.expected_image_id is None
    assert value.source_kind == "registry"


def test_registry_cannot_be_published_without_a_full_candidate():
    value = registry()
    value.update(state="published", published_at=datetime.now(UTC))
    with pytest.raises(ValueError):
        ComponentRelease(**value)


def test_registry_cannot_be_used_for_discord():
    value = registry()
    value["component"] = "discord-bot"
    with pytest.raises(ValueError):
        ComponentRelease(**value)


@pytest.mark.parametrize(
    "extra",
    [
        {"expected_image_id": "sha256:" + "d" * 64},
        {"schema_to": "20261013_0057"},
        {"maa_version": "5.0.0"},
        {"blob_id": uuid4()},
    ],
)
def test_registry_draft_rejects_partial_image_and_unrelated_archive_fields(extra):
    with pytest.raises(ValueError):
        ComponentRelease(**{**registry(), **extra})
