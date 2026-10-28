import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.components.host.qq_candidate_builder import QqCandidateBuilder


def test_candidate_builder_rejects_wrong_runtime_role(tmp_path):
    client = MagicMock()
    client.release.return_value = SimpleNamespace(
        release_id=uuid4(),
        source_kind="registry",
        component="qq-bot",
        state="verifying",
        native_version="8.3.2",
        runtime_version="1.0.0",
        registry_digest="sha256:0b9e20fdda15db223eceff87b0afd51c82dee156ce3c49424f1eb7f417786cdd",
        platform_manifest_digest="sha256:" + "b" * 64,
        runtime_artifact_digest="sha256:" + "c" * 64,
        architecture="amd64",
    )
    recipe = tmp_path / "bots"
    recipe.mkdir()
    (recipe / "Dockerfile.qq-candidate").write_text("FROM scratch")
    runner = MagicMock()
    runner.run.side_effect = lambda args, *rest: (
        json.dumps(
            [
                {
                    "Id": "sha256:" + "d" * 64,
                    "Architecture": "amd64",
                    "Config": {"Labels": {"org.al1s.bot-role": "discord"}},
                }
            ]
        )
        if args[:3] == ["docker", "image", "inspect"]
        else ""
    )
    builder = QqCandidateBuilder(
        SimpleNamespace(
            state_dir=tmp_path,
            deployment_root=tmp_path,
            qq_runtime_artifact_digest="sha256:" + "c" * 64,
        ),
        client,
        runner,
    )
    with pytest.raises(ValueError, match="candidate"):
        builder.prepare(client.release.return_value.release_id)
