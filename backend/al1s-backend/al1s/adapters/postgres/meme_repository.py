from al1s.adapters.postgres.meme_catalog import MemeCatalogRepository
from al1s.adapters.postgres.meme_rate_settings import MemeRateSettingsRepository
from al1s.adapters.postgres.meme_receipts import MemeReceiptRepository
from al1s.adapters.postgres.meme_resources import MemeResourceRepository


class MemeRepository(
    MemeRateSettingsRepository, MemeCatalogRepository, MemeReceiptRepository, MemeResourceRepository
):
    """Bounded catalog/configuration transactions and original-message send fencing."""
