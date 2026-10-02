"""Typed view of the services installed on FastAPI's runtime state."""

import asyncio
from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI, Request

from al1s.app.config import Settings
from al1s.app.modules import ModuleRegistry
from al1s.appearance.service import AppearanceService
from al1s.bots.gateway_probe import GatewayProbe
from al1s.bots.runtime_status import BotRuntimeService
from al1s.bots.service import BotService
from al1s.execution.artifact_upload_service import ArtifactUploadService
from al1s.execution.blob_transfer import TerminalBlobTransferService
from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.editor_service import EditorSessionService
from al1s.execution.failure_review_service import FailureReviewService
from al1s.execution.maintenance_service import MaintenanceService
from al1s.execution.mqtt_sessions import TerminalMqttSessionService
from al1s.execution.recording_download import RecordingDownloadService
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.services import ExecutionResourceService
from al1s.infrastructure.readiness import ReadinessService
from al1s.kernel.maintenance import KernelMaintenanceService
from al1s.kernel.storage_gc import StorageGcService
from al1s.lineup.service import LineupService
from al1s.lineup.workspace_service import LineupWorkspaceService
from al1s.maa.applicability_service import MaaApplicabilityService
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.editor_creation import MaaEditorCreationService
from al1s.maa.export_service import MaaScriptExportService
from al1s.maa.image_resources import MaaImageResourceService
from al1s.maa.import_command_service import MaaArchiveImportCommandService
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.script_command_service import MaaScriptCommandService
from al1s.maa.strategy_command_service import MaaStrategyCommandService
from al1s.maa.strategy_service import MaaStrategyService
from al1s.notifications.discord_forward_rules import DiscordForwardRuleService
from al1s.notifications.lexicon_service import LexiconService
from al1s.notifications.query_service import NotificationQueryService
from al1s.notifications.review_service import ReviewService
from al1s.notifications.service import NotificationService
from al1s.releases.service import ReleaseService


@dataclass(init=False, slots=True)
class PlatformServiceState:
    lineup: LineupService
    lineup_workspace: LineupWorkspaceService
    settings: Settings
    modules: ModuleRegistry
    readiness: ReadinessService
    maintenance: KernelMaintenanceService
    storage_gc: StorageGcService
    discord_forward_rules: DiscordForwardRuleService
    host_maintenance: MaintenanceService
    lexicon: LexiconService
    reviews: ReviewService
    gateway_probe: GatewayProbe
    editor_sessions: EditorSessionService
    notification_commands: NotificationService
    notification_queries: NotificationQueryService
    bot_service: BotService
    bot_runtime: BotRuntimeService
    execution_resources: ExecutionResourceService
    terminal_mqtt_sessions: TerminalMqttSessionService
    maa_publication: MaaScriptPublicationService
    maa_quick_tests: MaaQuickTestService
    maa_catalog: MaaCatalogQueryService
    maa_catalog_commands: MaaCatalogCommandService
    maa_editor_creation: MaaEditorCreationService
    maa_applicability: MaaApplicabilityService
    maa_script_commands: MaaScriptCommandService
    maa_strategies: MaaStrategyService
    maa_strategy_commands: MaaStrategyCommandService
    appearance: AppearanceService
    maa_images: MaaImageResourceService
    maa_image_slots: asyncio.Semaphore
    maa_exports: MaaScriptExportService
    maa_export_slots: asyncio.Semaphore
    linux_releases: ReleaseService
    failure_reviews: FailureReviewService
    recording_downloads: RecordingDownloadService
    maa_archive_upload_slots: asyncio.Semaphore
    quick_test_media: RecordingDownloadService
    maa_archive_import_commands: MaaArchiveImportCommandService
    terminal_blob_transfer: TerminalBlobTransferService
    artifact_uploads: ArtifactUploadService
    execution_scheduling: ExecutionSchedulingService
    terminal_delivery: TerminalDeliveryService


def service_state(request: Request) -> PlatformServiceState:
    services = getattr(request.app.state, "services", None)
    if isinstance(services, PlatformServiceState):
        return services
    # Independently mounted routers and existing test doubles retain their legacy state view.
    return cast(PlatformServiceState, request.app.state)


def validate_service_state(app: FastAPI) -> None:
    services = getattr(app.state, "services", app.state)
    missing = [name for name in PlatformServiceState.__annotations__ if not hasattr(services, name)]
    if missing:
        raise RuntimeError(f"Platform service assembly is incomplete: {', '.join(missing)}")


def install_service_state(app: FastAPI, services: PlatformServiceState) -> None:
    """Publish the typed assembly only after all required services are installed."""
    missing = [name for name in PlatformServiceState.__annotations__ if not hasattr(services, name)]
    if missing:
        raise RuntimeError(f"Platform service assembly is incomplete: {', '.join(missing)}")
    app.state.services = services
    # Retain existing infrastructure readers and dependency-injection entrypoints.
    for name in PlatformServiceState.__annotations__:
        setattr(app.state, name, getattr(services, name))
