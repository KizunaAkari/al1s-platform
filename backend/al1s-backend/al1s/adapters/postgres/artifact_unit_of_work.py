from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.artifact_repositories import (
    PostgresArtifactUploadRepository,
)
from al1s.adapters.postgres.repositories import (
    PostgresBlobCatalogRepository,
    PostgresGcJobRepository,
)


class ArtifactUploadSqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> ArtifactUploadSqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.artifacts = PostgresArtifactUploadRepository(self._session)
        self.blobs = PostgresBlobCatalogRepository(self._session)
        self.gc_jobs = PostgresGcJobRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    def commit(self) -> None:
        assert self._session is not None
        self._session.commit()
