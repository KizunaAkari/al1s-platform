import os
import re
import tarfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from al1s.bots.container_control import UnixConnection
from al1s.bots.qq_identity.contracts import IdentityRecord, QqIdentitySummary
from al1s.bots.qq_identity.repository import RECORD_NAME
from al1s.bots.qq_identity.summary import stored_summary, summarize_record
from al1s.components.host.bot_deployment import BotDeployment
from al1s.components.host.bot_mounts import canonical_protection


def read_qq_identity_summary(deployment: BotDeployment) -> QqIdentitySummary:
    root = deployment.settings.deployment_root.resolve()
    mounts = deployment.config("qq")["services"]["llbot"].get("volumes", [])
    registered = [m for m in mounts if m.get("target") == "/app/llbot/data"]
    state = deployment.inspect("qq")
    actual = [m for m in state["Mounts"]
              if m.get("Destination") == "/app/llbot/data"]
    if (len(registered) != 1 or len(actual) != 1
            or registered[0].get("type") != "bind" or actual[0].get("Type") != "bind"):
        raise ValueError("qq_identity_mount_invalid")
    source = Path(registered[0]["source"])
    if source.is_symlink() or not source.resolve().is_relative_to(root) or not source.is_dir():
        raise ValueError("qq_identity_mount_invalid")
    expected = {"mounts": [{"Type": "bind", "Source": str(source)}]}
    observed = {"mounts": [{"Type": "bind", "Source": actual[0]["Source"]}]}
    if canonical_protection(expected, root) != canonical_protection(observed, root):
        raise ValueError("qq_identity_mount_changed")
    try:
        return stored_summary(source)
    except PermissionError:
        record = IdentityRecord.model_validate_json(archive_metadata_file(state["Id"], RECORD_NAME))
        guid = archive_metadata_file(state["Id"], "machine_guid.bin")
        return summarize_record(record, guid)


def archive_metadata_file(instance: str, name: str) -> bytes:
    maximum = {RECORD_NAME: 32768, "machine_guid.bin": 16}.get(name)
    if maximum is None or not re.fullmatch(r"[a-f0-9]{64}", instance):
        raise ValueError("qq_identity_metadata_path_invalid")
    version = os.environ.get("AL1S_CONTROL_DOCKER_VERSION", "1.45")
    if not re.fullmatch(r"1\.[0-9]{2,3}", version):
        raise ValueError("qq_identity_engine_version_invalid")
    connection = UnixConnection("/var/run/docker.sock")
    try:
        query = urlencode({"path": "/app/llbot/data/" + name})
        connection.request("GET", f"/v{version}/containers/{instance}/archive?{query}")
        response = connection.getresponse()
        raw = response.read(131073)
        if response.status != 200 or len(raw) > 131072:
            raise ValueError("qq_identity_metadata_archive_invalid")
        with tarfile.open(fileobj=BytesIO(raw), mode="r|") as archive:
            member = archive.next()
            if (member is None or member.name.removeprefix("./") != name
                    or not member.isfile() or member.size > maximum):
                raise ValueError("qq_identity_metadata_archive_invalid")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("qq_identity_metadata_archive_invalid")
            data = stream.read(maximum + 1)
            if len(data) != member.size or archive.next() is not None:
                raise ValueError("qq_identity_metadata_archive_invalid")
            return data
    finally:
        connection.close()
