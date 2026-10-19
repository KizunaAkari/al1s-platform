from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.component_repository import ComponentRepository
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import Settings
from al1s.components.host_client import ComponentHostClient
from al1s.components.inventory import ComponentInventoryService
from al1s.components.linux_adapter import CompositeUpgradeHost
from al1s.components.operations import ComponentUpgradeService
from al1s.components.releases import ComponentReleaseService
from al1s.components.status_capability import StatusCapability
from al1s.components.verification import DockerArchiveVerifier
from al1s.execution.maintenance_service import MaintenanceService


def component_services(
    settings: Settings,
    sessions: sessionmaker[Session],
    objects: S3BlobStore,
    maintenance: MaintenanceService,
) -> tuple[ComponentInventoryService, ComponentReleaseService, ComponentUpgradeService]:
    store = ComponentRepository(sessions)
    host = ComponentHostClient(settings.component_host)
    directory = Path(settings.component_verification_directory)
    inventory = ComponentInventoryService(store, host)
    key = (
        settings.component_host.token.get_secret_value()
        if settings.component_host
        else settings.mqtt_session_signing_key.get_secret_value()
    )
    bridge = CompositeUpgradeHost(store, host, maintenance)
    return (
        inventory,
        ComponentReleaseService(
            store,
            objects,
            DockerArchiveVerifier(lambda _path, release: host.verify_release(release.release_id)),
            directory,
            registry_prepare=host.prepare_qq_release,
        ),
        ComponentUpgradeService(
            store,
            inventory,
            bridge,
            StatusCapability(key),
            available=bridge.available,
            resolve_source=bridge.resolve_source,
        ),
    )
