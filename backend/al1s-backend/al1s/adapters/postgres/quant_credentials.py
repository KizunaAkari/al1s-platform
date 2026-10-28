"""Store only source-specific encrypted credentials in the quant schema."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.quant_models import QuantProviderCredentialRow
from al1s.quant.credential_key import derive_quant_credential_key as derive_quant_credential_key
from al1s.secrets.security import FernetSecretCipher, SecretUnavailableError

PRIVATE_PROVIDERS = frozenset({"tushare", "gugudata"})


class QuantCredentials:
    def __init__(self, sessions: sessionmaker[Session], cipher: FernetSecretCipher):
        self.sessions, self.cipher = sessions, cipher

    def configured(self) -> set[str]:
        with self.sessions() as session:
            return set(session.scalars(select(QuantProviderCredentialRow.provider)).all())

    def snapshot(self) -> dict[str, str]:
        with self.sessions() as session:
            rows = session.scalars(select(QuantProviderCredentialRow)).all()
            result = {}
            for row in rows:
                try:
                    result[row.provider] = self.cipher.decrypt(row.ciphertext, row.key_id)
                except SecretUnavailableError:
                    continue
            return result

    def get(self, provider: str) -> str | None:
        if provider not in PRIVATE_PROVIDERS:
            raise ValueError("quant_provider_invalid")
        with self.sessions() as session:
            row = session.get(QuantProviderCredentialRow, provider)
            if row is None:
                return None
            try:
                return self.cipher.decrypt(row.ciphertext, row.key_id)
            except SecretUnavailableError as error:
                raise ValueError("quant_source_unavailable") from error

    def save(self, provider: str, secret: str | None) -> None:
        if provider not in PRIVATE_PROVIDERS:
            raise ValueError("quant_provider_invalid")
        if secret is not None and (
            not secret or len(secret) > 1024 or any(ord(c) < 32 for c in secret)
        ):
            raise ValueError("quant_credential_invalid")
        with self.sessions.begin() as session:
            row = session.get(QuantProviderCredentialRow, provider, with_for_update=True)
            if secret is None:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(
                    QuantProviderCredentialRow(
                        provider=provider,
                        ciphertext=self.cipher.encrypt(secret),
                        key_id=self.cipher.key_id,
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                row.ciphertext, row.key_id, row.updated_at = (
                    self.cipher.encrypt(secret),
                    self.cipher.key_id,
                    datetime.now(UTC),
                )
