"""Normalize only Docker Desktop aliases inside the registered deployment root."""

import copy
import re
from pathlib import Path, PurePosixPath

from al1s.components.host.contracts import JsonObject


def canonical_protection(value: JsonObject, deployment_root: Path) -> JsonObject:
    root = PurePosixPath(str(deployment_root).replace("\\", "/"))
    if not re.match(r"^/run/desktop/mnt/host/[a-z]/", str(root)) or ".." in root.parts:
        return value
    result = copy.deepcopy(value)
    for mount in result["mounts"]:
        source = mount.get("Source", "")
        if mount.get("Type") != "bind" or not re.match(r"^[A-Za-z]:[\\/]", source):
            continue
        translated = PurePosixPath(
            "/run/desktop/mnt/host/" + source[0].lower() + source[2:].replace("\\", "/")
        )
        if ".." not in translated.parts and translated.is_relative_to(root):
            mount["Source"] = str(translated)
    return result


def same_protection(current: JsonObject, before: JsonObject, root: Path) -> bool:
    return canonical_protection(current, root) == canonical_protection(before, root)
