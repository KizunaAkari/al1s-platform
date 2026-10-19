from typing import Literal

from al1s.meme_workshop.qq_worker import QqWorker
from al1s.meme_workshop.service import MemeWorkshopService
from al1s.secrets.ports import SecretCipher

RuntimeRole = Literal["qq", "discord", "combined"]


def render_limit(role: RuntimeRole) -> int:
    if role not in {"qq", "discord", "combined"}:
        raise ValueError("invalid_meme_runtime_role")
    return 2 if role == "combined" else 1


def make_qq_worker(
    role: RuntimeRole, workshop: MemeWorkshopService, cipher: SecretCipher
) -> QqWorker | None:
    render_limit(role)
    return None if role == "discord" else QqWorker(workshop, cipher)
