import copy
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from al1s.execution.host_management import HostManagerConnection


def relocate_settings(
    source: dict[str, Any], engine_root: str, platform_url: str
) -> dict[str, Any]:
    """Register the same deployment directory in the executor and Docker engine."""
    destination = PurePosixPath(engine_root)
    if (
        not destination.is_absolute()
        or str(destination) == "/"
        or ".." in destination.parts
        or any(character in engine_root for character in ",\n\r\\")
    ):
        raise ValueError("component_registered_engine_root")
    original = str(source["deployment_root"])
    path_type = PureWindowsPath if PureWindowsPath(original).drive else PurePosixPath
    root = path_type(original)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("component_registered_source_root")

    def relocate(value: str) -> str:
        path = path_type(value)
        if ".." in path.parts or not path.is_relative_to(root):
            raise ValueError("component_registered_path_outside_root")
        return str(destination.joinpath(*path.relative_to(root).parts))

    result = copy.deepcopy(source)
    result["deployment_root"] = str(destination)
    for key in ("compose_files", "env_files"):
        result[key] = [relocate(value) for value in source[key]]
    for key in ("selectors_file", "state_dir", "certificate_file", "private_key_file"):
        result[key] = relocate(source[key])
    connection = result["platform"]
    connection["url"] = platform_url
    if connection.get("ca_file"):
        connection["ca_file"] = relocate(connection["ca_file"])
    HostManagerConnection.model_validate(connection)
    return result
