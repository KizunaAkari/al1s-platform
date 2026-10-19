import json
import os
from pathlib import Path
from uuid import uuid4

from al1s.bots.qq_headed.contracts import ClientProfile
from al1s.bots.qq_identity.repository import atomic_bytes, read_bytes, sync_directory
from al1s.components.host.journal import HostJournal

PROFILE_NAME = ".al1s-client-profile.json"


def validate_client_version(root: Path, expected: str) -> None:
    folder = root / "qq-client" / "versions"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("qq_client_version_path_invalid")
    selector = folder / "config.json"
    if not selector.exists() and not selector.is_symlink():
        return  # First startup has no vendor selector yet.
    config = json.loads(read_bytes(selector, 16384))
    if (not isinstance(config, dict) or config.get("baseVersion") != expected
            or config.get("curVersion") != expected):
        raise ValueError("qq_client_stored_version_mismatch")


def private_directory(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("qq_client_directory_invalid")
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    sync_directory(path.parent)
    return path


def read_client_profile(root: Path) -> ClientProfile:
    folder = root / "qq-client"
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("qq_client_directory_invalid")
    return ClientProfile.model_validate_json(read_bytes(folder / PROFILE_NAME, 4096))


def prepare_client_profile(root: Path, account: str) -> ClientProfile:
    requested = ClientProfile(instance_id=uuid4(), account=account)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("qq_client_root_invalid")
    folder = private_directory(root / "qq-client")
    lock = private_directory(root / ".al1s-qq-client-lock")
    with HostJournal(lock).exclusive("bot:qq"):
        record = folder / PROFILE_NAME
        if record.exists() or record.is_symlink():
            existing = read_client_profile(root)
            if existing.account != account:
                raise ValueError("qq_client_account_conflict")
            return existing
        # Never adopt a client directory from an unidentified prior instance.
        if any(folder.iterdir()):
            raise ValueError("qq_client_profile_missing")
        atomic_bytes(record, requested.model_dump_json().encode())
        return requested
