"""Read the last applied connection without exposing secrets to API callers."""

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from al1s.bots.ports import BotUnitOfWork
from al1s.bots.types import BotServiceKind
from al1s.notifications.errors import NotificationAdapterError
from al1s.secrets.ports import SecretCipher


@dataclass(frozen=True)
class BotConnection:
    settings: dict[str, object]
    secret: str | None = field(repr=False)


class BotConnectionReader:
    def __init__(self, uow_factory: Callable[[], BotUnitOfWork], cipher: SecretCipher):
        self._uow_factory = uow_factory
        self._cipher = cipher

    def read(self, service_id: UUID, kind: BotServiceKind) -> BotConnection:
        # Three bounded primary-key reads; transaction is closed before external I/O.
        with self._uow_factory() as uow:
            service = uow.services.get(service_id)
            if service is None or not service.enabled or service.deleted_at is not None:
                raise NotificationAdapterError("bot_unavailable", retryable=False)
            if service.kind != kind:
                raise NotificationAdapterError("bot_kind_mismatch", retryable=False)
            if service.applied_config_version_id is None:
                raise NotificationAdapterError("bot_configuration_not_applied", retryable=True)
            version = uow.configs.get_version(service.applied_config_version_id)
            if version is None or version.service_id != service_id:
                raise NotificationAdapterError("bot_configuration_invalid", retryable=False)
            secret = uow.secrets.get(version.secret_id) if version.secret_id else None
            if version.secret_id and secret is None:
                raise NotificationAdapterError("bot_secret_unavailable", retryable=False)
            return BotConnection(
                settings=dict(version.settings),
                secret=self._cipher.decrypt(secret.ciphertext, secret.key_id) if secret else None,
            )
