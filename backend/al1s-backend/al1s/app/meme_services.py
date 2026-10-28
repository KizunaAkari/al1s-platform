from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.meme_repository import MemeRepository
from al1s.kernel.ports import BlobStore
from al1s.meme_workshop.assets import MemeAssets
from al1s.meme_workshop.service import MemeWorkshopService


def meme_services(
    sessions: sessionmaker[Session], objects: BlobStore, *, render_limit: int = 2
) -> MemeWorkshopService:
    repository = MemeRepository(sessions)
    return MemeWorkshopService(
        repository, MemeAssets(repository, objects), render_limit=render_limit
    )
