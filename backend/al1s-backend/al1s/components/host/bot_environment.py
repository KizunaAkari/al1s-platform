"""Normalize frozen version metadata, while retaining exact deployment configuration checks."""

import copy
import json

from al1s.components.host.bot_deployment import NAMES, SERVICES, BotDeployment
from al1s.components.host.contracts import JsonObject


def normalize_image_metadata(
    deployment: BotDeployment, state: JsonObject, snapshot: JsonObject, current: JsonObject
) -> JsonObject:
    alias = snapshot["alias"]
    if alias not in {"qq", "discord"}:
        return current
    before = dict(value.split("=", 1) for value in snapshot["protected"]["env"])
    after = dict(value.split("=", 1) for value in current["env"])
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    allowed = {"NODE_VERSION", "AL1S_QQ_BOOTSTRAP_VERSION"} if alias == "qq" else {"NODE_VERSION"}
    if not changed or not changed <= allowed:
        return current
    try:
        environment = deployment.config(alias)["services"][SERVICES[alias]].get("environment", {})
        if changed & environment.keys():
            return current
        images = json.loads(deployment.runner.run(
            ["docker", "image", "inspect", snapshot["before_id"], state["Image"]]
        ))
        defaults = {row["Id"]: dict(value.split("=", 1) for value in row["Config"].get("Env", []))
                    for row in images}
        for key in changed:
            if (before.get(key) != defaults[snapshot["before_id"]].get(key)
                    or after.get(key) != defaults[state["Image"]].get(key)):
                return current
        if "NODE_VERSION" in changed and state["State"]["Running"]:
            actual = deployment.runner.run(["docker", "exec", NAMES[alias], "node", "--version"])
            if actual.strip().removeprefix("v") != after.get("NODE_VERSION"):
                return current
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return current
    result = copy.deepcopy(current)
    for key in changed:
        after.pop(key, None)
        if key in before:
            after[key] = before[key]
    result["env"] = sorted(f"{key}={value}" for key, value in after.items())
    return result
