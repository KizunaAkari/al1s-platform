from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.lexicon_models import LexiconVersionRow
from al1s.notifications.lexicon import LexiconData


class PostgresLexiconRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self._sessions = sessions

    def active(self) -> LexiconVersionRow | None:
        with self._sessions() as session:
            return session.scalar(select(LexiconVersionRow).where(LexiconVersionRow.active))

    def activate(self, candidate: LexiconData) -> bool:
        with self._sessions.begin() as session:
            # Serialize infrequent admin updates across API processes, including first install.
            session.execute(text("SELECT pg_advisory_xact_lock(7142028)"))
            current = session.scalar(select(LexiconVersionRow).where(
                LexiconVersionRow.active,
            ))
            if current and (current.commit, current.normalization_version) == (
                candidate.commit, candidate.normalization_version,
            ):
                if current.sha256 != candidate.sha256:
                    raise ValueError("lexicon_same_version_hash_changed")
                return False
            existing = session.scalar(select(LexiconVersionRow).where(
                LexiconVersionRow.commit == candidate.commit,
                LexiconVersionRow.normalization_version == candidate.normalization_version,
            ))
            if existing and existing.sha256 != candidate.sha256:
                raise ValueError("lexicon_same_version_hash_changed")
            session.execute(update(LexiconVersionRow).where(LexiconVersionRow.active)
                            .values(active=False))
            if existing:
                existing.active = True
            else:
                session.add(LexiconVersionRow(
                    id=uuid4(), commit=candidate.commit, sha256=candidate.sha256,
                    normalization_version=candidate.normalization_version,
                    license_text=candidate.license_text, raw=candidate.raw,
                    active=True, created_at=datetime.now(UTC),
                ))
        return True
