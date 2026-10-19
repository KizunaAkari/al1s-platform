from pathlib import Path

from al1s.bots.qq_headed.profile import read_client_profile
from al1s.meme_workshop.operation_identity import OperationIdentity


def observe_headed_identity(root: Path, account: str) -> OperationIdentity:
    profile = read_client_profile(root)
    if profile.account != account:
        raise ValueError("qq_client_account_conflict")
    return OperationIdentity(role="qq", account_state="offline", binding={
        "account": account, "mode": "pmhq", "client_instance_id": str(profile.instance_id)})
