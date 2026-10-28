import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from al1s.components.host.bot_deployment import NAMES, SERVICES, BotDeployment
from al1s.components.host.bot_inventory import bot_inventory


def test_two_bot_inventory_batches_images_and_uses_actual_runtime_proofs():
    calls = []
    containers = [
        {
            "Id": alias,
            "Name": "/" + NAMES[alias],
            "Image": "sha256:" + char * 64,
            "Config": {
                "Image": f"al1s-plachta:{alias}",
                "Labels": {
                    "com.docker.compose.project": "al1s-bot",
                    "com.docker.compose.service": SERVICES[alias],
                },
            },
            "Mounts": [],
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        for alias, char in (("qq", "a"), ("discord", "b"))
    ]

    def run(args, timeout=30):
        calls.append(args)
        if args[1] == "inspect":
            return json.dumps(
                containers
                if len(args) > 3
                else [next(c for c in containers if c["Name"] == "/" + args[2])]
            )
        if args[1:3] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": c["Image"],
                        "Architecture": "amd64",
                        "Config": {"Labels": {"org.al1s.native-version": "wrong-label"}},
                    }
                    for c in containers
                ]
            )
        if args[1] == "exec":
            return json.dumps(
                {
                    "role": "qq" if args[2] == NAMES["qq"] else "discord",
                    "native_version": "8.1.9" if args[2] == NAMES["qq"] else "0.1.0",
                    "runtime_version": "1.0.0",
                    "render_proof": True,
                }
            )
        raise AssertionError(args)

    deployment = BotDeployment(SimpleNamespace(), SimpleNamespace(run=run))
    deployment.config = MagicMock(
        side_effect=lambda alias: {"services": {SERVICES[alias]: {"image": "image"}}}
    )
    result = bot_inventory(deployment)
    assert [row["version"] for row in result] == ["8.1.9", "0.1.0"]
    assert len([call for call in calls if call[1] == "inspect"]) == 1
    assert len([call for call in calls if call[1:3] == ["image", "inspect"]]) == 1
