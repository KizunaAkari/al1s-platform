import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.mqtt.mosquitto import MosquittoDynamicSecurityBroker
from al1s.adapters.postgres.artifact_unit_of_work import (
    ArtifactUploadSqlAlchemyUnitOfWork,
)
from al1s.adapters.postgres.blob_transfer_unit_of_work import (
    TerminalBlobSqlAlchemyUnitOfWork,
)
from al1s.adapters.postgres.bot_unit_of_work import BotSqlAlchemyUnitOfWork
from al1s.adapters.postgres.discord_forward_rules import DiscordForwardRuleRepository
from al1s.adapters.postgres.editor_repository import EditorSqlTransaction
from al1s.adapters.postgres.execution_unit_of_work import ExecutionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.failure_review import PostgresFailureReview
from al1s.adapters.postgres.lexicon_repository import PostgresLexiconRepository
from al1s.adapters.postgres.lineup_annotation_repository import LineupAnnotationRepository
from al1s.adapters.postgres.lineup_batch_commands import LineupBatchCommands
from al1s.adapters.postgres.lineup_repository import LineupRepository
from al1s.adapters.postgres.lineup_workspace_queries import LineupWorkspaceQueries
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.adapters.postgres.maintenance_repository import MaintenanceRepository
from al1s.adapters.postgres.mqtt_unit_of_work import MqttSessionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.adapters.postgres.quick_test_media import PostgresQuickTestMedia
from al1s.adapters.postgres.recording_reader import PostgresRecordingReader
from al1s.adapters.postgres.release_repository import ReleaseRepository
from al1s.adapters.postgres.repositories import PostgresKernelMaintenanceReader
from al1s.adapters.postgres.scheduling_unit_of_work import (
    SchedulingSqlAlchemyUnitOfWork,
)
from al1s.adapters.postgres.storage_gc_repository import PostgresStorageGcRepository
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.android_demo.definition_provider import AndroidDemoExecutionDefinitionProvider
from al1s.api.database_errors import install_database_error_handlers, internal_error_response
from al1s.api.router import api_router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings, get_settings
from al1s.app.frontend import mount_frontend
from al1s.app.logging import configure_logging
from al1s.app.modules import platform_modules
from al1s.app.service_state import PlatformServiceState, install_service_state
from al1s.appearance.service import AppearanceService
from al1s.bots.errors import BotDomainError
from al1s.bots.gateway_probe import GatewayProbe
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.runtime_connection import BotConnectionReader
from al1s.bots.runtime_status import BotRuntimeService
from al1s.bots.service import BotService
from al1s.execution.artifact_upload_ports import ArtifactUploadUnitOfWork
from al1s.execution.artifact_upload_service import ArtifactUploadService
from al1s.execution.blob_transfer import TerminalBlobTransferService
from al1s.execution.blob_transfer_ports import TerminalBlobUnitOfWork
from al1s.execution.definitions import ExecutionDefinitionRegistry
from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.editor_service import EditorSessionService
from al1s.execution.errors import ExecutionDomainError
from al1s.execution.failure_review_service import FailureReviewService
from al1s.execution.maintenance_service import MaintenanceService
from al1s.execution.mqtt_sessions import (
    MqttSessionPasswordSigner,
    TerminalMqttSessionService,
    TerminalMqttSessionUnitOfWork,
)
from al1s.execution.offline_permits import OfflinePermitSigner
from al1s.execution.recording_download import RecordingDownloadService
from al1s.execution.scheduling_service import (
    ExecutionSchedulingService,
    SchedulingUowFactory,
)
from al1s.execution.services import ExecutionResourceService, UowFactory
from al1s.infrastructure.database import (
    create_batch_session_factory,
    create_database_engine,
    create_session_factory,
)
from al1s.infrastructure.readiness import ReadinessService, build_readiness_service
from al1s.kernel.maintenance import KernelMaintenanceService
from al1s.kernel.storage_gc import StorageGcService
from al1s.lineup.batch_definition import LineupBatchDefinitionProvider
from al1s.lineup.definition import LineupDefinitionProvider
from al1s.lineup.images import LineupImages
from al1s.lineup.service import LineupService
from al1s.lineup.workspace_service import LineupWorkspaceService
from al1s.maa.applicability_service import MaaApplicabilityService
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.definition_provider import MaaExecutionDefinitionProvider, MaaUowFactory
from al1s.maa.editor_creation import MaaEditorCreationService
from al1s.maa.errors import MaaDomainError
from al1s.maa.export_service import MaaScriptExportService
from al1s.maa.image_resources import MaaImageResourceService
from al1s.maa.import_command_service import MaaArchiveImportCommandService
from al1s.maa.import_service import MaaArchiveImportService
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.script_command_service import MaaScriptCommandService
from al1s.maa.strategy_command_service import MaaStrategyCommandService
from al1s.maa.strategy_service import MaaStrategyService
from al1s.notifications.discord_forward_rules import DiscordForwardRuleService
from al1s.notifications.errors import NotificationDomainError
from al1s.notifications.lexicon_service import LexiconService
from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.query_service import NotificationQueryService
from al1s.notifications.review_service import ReviewService
from al1s.notifications.security import FernetSecretCipher
from al1s.notifications.service import NotificationService
from al1s.releases.service import ReleaseService


def _install_notification_services(
    services: PlatformServiceState,
    settings: Settings,
    session_factory: sessionmaker[Session],
    notification_service: NotificationService | None,
    notification_query_service: NotificationQueryService | None,
    bot_service: BotService | None,
) -> None:
    services.discord_forward_rules = DiscordForwardRuleService(
        DiscordForwardRuleRepository(session_factory)
    )
    services.host_maintenance = MaintenanceService(
        MaintenanceRepository(session_factory),
        settings.host_managers,
    )
    services.lexicon = LexiconService(PostgresLexiconRepository(session_factory))
    notification_uow_factory = cast(
        Callable[[], NotificationUnitOfWork],
        lambda: NotificationSqlAlchemyUnitOfWork(session_factory),
    )
    platform_cipher = FernetSecretCipher(settings.notification_master_key.get_secret_value())
    services.reviews = ReviewService(
        notification_uow_factory,
        platform_cipher,
        services.lexicon,
    )
    bot_uow_factory = cast(
        Callable[[], BotUnitOfWork],
        lambda: BotSqlAlchemyUnitOfWork(session_factory),
    )
    services.gateway_probe = GatewayProbe(bot_uow_factory, platform_cipher)
    services.editor_sessions = EditorSessionService(
        lambda: EditorSqlTransaction(session_factory), platform_cipher
    )
    services.notification_commands = notification_service or NotificationService(
        notification_uow_factory,
        platform_cipher,
    )
    services.notification_queries = notification_query_service or NotificationQueryService(
        notification_uow_factory
    )
    services.bot_service = bot_service or BotService(
        bot_uow_factory,
        platform_cipher,
        platform_cipher,
    )
    services.bot_runtime = BotRuntimeService(
        services.bot_service, BotConnectionReader(bot_uow_factory, platform_cipher)
    )


def _install_execution_identity_services(
    services: PlatformServiceState,
    settings: Settings,
    session_factory: sessionmaker[Session],
    execution_resource_service: ExecutionResourceService | None,
    terminal_mqtt_session_service: TerminalMqttSessionService | None,
) -> None:
    services.execution_resources = execution_resource_service or ExecutionResourceService(
        cast(UowFactory, lambda: ExecutionSqlAlchemyUnitOfWork(session_factory))
    )
    services.terminal_mqtt_sessions = terminal_mqtt_session_service or TerminalMqttSessionService(
        cast(
            Callable[[], TerminalMqttSessionUnitOfWork],
            lambda: MqttSessionSqlAlchemyUnitOfWork(session_factory),
        ),
        MosquittoDynamicSecurityBroker(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            admin_username=settings.mqtt_username,
            admin_password=settings.mqtt_password.get_secret_value(),
            timeout_seconds=settings.probe_timeout_seconds,
            tls_enabled=settings.mqtt_tls_enabled,
        ),
        MqttSessionPasswordSigner(settings.mqtt_session_signing_key.get_secret_value()),
        public_host=settings.mqtt_public_host,
        public_port=settings.mqtt_public_port,
        tls_enabled=settings.mqtt_public_tls_enabled,
        ttl=timedelta(seconds=settings.mqtt_session_ttl_seconds),
    )


def _install_maa_services(
    services: PlatformServiceState,
    session_factory: sessionmaker[Session],
    maa_quick_test_service: MaaQuickTestService | None,
    execution_definition_registry: ExecutionDefinitionRegistry | None,
) -> tuple[MaaUowFactory, ExecutionDefinitionRegistry]:
    maa_uow_factory = cast(MaaUowFactory, lambda: MaaSqlAlchemyUnitOfWork(session_factory))
    maa_definitions = MaaExecutionDefinitionProvider(maa_uow_factory)
    maa_publication = MaaScriptPublicationService(maa_uow_factory)
    maa_strategies = MaaStrategyService(maa_uow_factory)
    definitions = execution_definition_registry or ExecutionDefinitionRegistry()
    if execution_definition_registry is None:
        definitions.register("maa", maa_definitions)
        definitions.register("android_demo", AndroidDemoExecutionDefinitionProvider())
        definitions.register("lineup", LineupDefinitionProvider(LineupRepository(session_factory)))
        definitions.register(
            "lineup_batch", LineupBatchDefinitionProvider(LineupRepository(session_factory))
        )
    services.maa_publication = maa_publication
    services.maa_quick_tests = maa_quick_test_service or MaaQuickTestService(
        maa_uow_factory,
        maa_definitions,
        maa_publication,
    )
    services.maa_catalog = MaaCatalogQueryService(maa_uow_factory)
    services.maa_catalog_commands = MaaCatalogCommandService(maa_uow_factory)
    services.maa_editor_creation = MaaEditorCreationService(maa_uow_factory)
    services.maa_applicability = MaaApplicabilityService(maa_uow_factory)
    services.maa_script_commands = MaaScriptCommandService(
        maa_uow_factory,
        publication_service=maa_publication,
    )
    services.maa_strategies = maa_strategies
    services.maa_strategy_commands = MaaStrategyCommandService(
        maa_uow_factory,
        strategy_service=maa_strategies,
    )
    return maa_uow_factory, definitions


def _install_blob_services(
    services: PlatformServiceState,
    settings: Settings,
    session_factory: sessionmaker[Session],
    owned: ExitStack,
    maa_uow_factory: MaaUowFactory,
    maa_archive_import_command_service: MaaArchiveImportCommandService | None,
    terminal_blob_transfer_service: TerminalBlobTransferService | None,
    artifact_upload_service: ArtifactUploadService | None,
    recording_download_service: RecordingDownloadService | None,
    failure_review_service: FailureReviewService | None,
) -> None:
    release_blob_store = S3BlobStore(settings)
    owned.callback(release_blob_store.close)
    services.appearance = AppearanceService(release_blob_store)
    services.maa_images = MaaImageResourceService(maa_uow_factory, release_blob_store)
    services.maa_image_slots = asyncio.Semaphore(2)
    assert session_factory.kw["bind"] is not None
    archive_sessions = create_batch_session_factory(session_factory.kw["bind"], settings)
    archive_uow_factory = cast(MaaUowFactory, lambda: MaaSqlAlchemyUnitOfWork(archive_sessions))
    services.maa_exports = MaaScriptExportService(archive_uow_factory, release_blob_store)
    services.maa_export_slots = asyncio.Semaphore(1)
    services.linux_releases = ReleaseService(
        ReleaseRepository(session_factory),
        release_blob_store,
    )
    owned_blob_store: S3BlobStore | None = None
    if (
        maa_archive_import_command_service is None
        or terminal_blob_transfer_service is None
        or artifact_upload_service is None
        or recording_download_service is None
        or failure_review_service is None
    ):
        owned_blob_store = S3BlobStore(settings)
        owned.callback(owned_blob_store.close)
    if failure_review_service is None:
        assert owned_blob_store is not None
        services.failure_reviews = FailureReviewService(
            PostgresFailureReview(session_factory),
            owned_blob_store,
        )
    else:
        services.failure_reviews = failure_review_service
    if recording_download_service is None:
        assert owned_blob_store is not None
        services.recording_downloads = RecordingDownloadService(
            PostgresRecordingReader(session_factory),
            owned_blob_store,
        )
    else:
        services.recording_downloads = recording_download_service
    services.maa_archive_upload_slots = asyncio.Semaphore(1)
    if owned_blob_store is None:
        owned_blob_store = S3BlobStore(settings)
        owned.callback(owned_blob_store.close)
    services.quick_test_media = RecordingDownloadService(
        PostgresQuickTestMedia(session_factory),
        owned_blob_store,
    )
    services.lineup = LineupService(
        LineupRepository(session_factory),
        LineupImages(maa_uow_factory, owned_blob_store),
    )
    services.lineup_workspace = LineupWorkspaceService(
        services.lineup,
        LineupBatchCommands(session_factory),
        LineupWorkspaceQueries(session_factory),
        LineupAnnotationRepository(session_factory),
    )
    if maa_archive_import_command_service is None:
        assert owned_blob_store is not None
        services.maa_archive_import_commands = MaaArchiveImportCommandService(
            archive_uow_factory,
            owned_blob_store,
            MaaArchiveImportService(archive_uow_factory, owned_blob_store),
        )
    else:
        services.maa_archive_import_commands = maa_archive_import_command_service
    if terminal_blob_transfer_service is None:
        assert owned_blob_store is not None
        services.terminal_blob_transfer = TerminalBlobTransferService(
            cast(
                Callable[[], TerminalBlobUnitOfWork],
                lambda: TerminalBlobSqlAlchemyUnitOfWork(session_factory),
            ),
            owned_blob_store,
        )
    else:
        services.terminal_blob_transfer = terminal_blob_transfer_service
    if artifact_upload_service is None:
        assert owned_blob_store is not None
        services.artifact_uploads = ArtifactUploadService(
            cast(
                Callable[[], ArtifactUploadUnitOfWork],
                lambda: ArtifactUploadSqlAlchemyUnitOfWork(session_factory),
            ),
            owned_blob_store,
        )
    else:
        services.artifact_uploads = artifact_upload_service


def _install_scheduling_services(
    services: PlatformServiceState,
    settings: Settings,
    session_factory: sessionmaker[Session],
    definitions: ExecutionDefinitionRegistry,
    execution_scheduling_service: ExecutionSchedulingService | None,
    terminal_delivery_service: TerminalDeliveryService | None,
) -> None:
    services.execution_scheduling = execution_scheduling_service or ExecutionSchedulingService(
        cast(
            SchedulingUowFactory,
            lambda: SchedulingSqlAlchemyUnitOfWork(session_factory),
        ),
        definitions,
    )
    services.terminal_delivery = terminal_delivery_service or TerminalDeliveryService(
        cast(
            SchedulingUowFactory,
            lambda: SchedulingSqlAlchemyUnitOfWork(session_factory),
        ),
        now=lambda: datetime.now(UTC),
        offline_permit_signer=OfflinePermitSigner(
            settings.offline_permit_signing_key.get_secret_value()
        ),
        offline_permit_ttl=timedelta(seconds=settings.offline_permit_ttl_seconds),
    )


def create_app(
    settings: Settings | None = None,
    readiness_service: ReadinessService | None = None,
    maintenance_service: KernelMaintenanceService | None = None,
    storage_gc_service: StorageGcService | None = None,
    execution_resource_service: ExecutionResourceService | None = None,
    execution_scheduling_service: ExecutionSchedulingService | None = None,
    execution_definition_registry: ExecutionDefinitionRegistry | None = None,
    terminal_delivery_service: TerminalDeliveryService | None = None,
    terminal_blob_transfer_service: TerminalBlobTransferService | None = None,
    artifact_upload_service: ArtifactUploadService | None = None,
    maa_quick_test_service: MaaQuickTestService | None = None,
    maa_archive_import_command_service: MaaArchiveImportCommandService | None = None,
    terminal_mqtt_session_service: TerminalMqttSessionService | None = None,
    notification_service: NotificationService | None = None,
    notification_query_service: NotificationQueryService | None = None,
    bot_service: BotService | None = None,
    recording_download_service: RecordingDownloadService | None = None,
    failure_review_service: FailureReviewService | None = None,
) -> FastAPI:
    configure_logging()
    resolved_settings = settings or get_settings()
    services = PlatformServiceState()
    services.settings = resolved_settings
    services.modules = platform_modules()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with ExitStack() as owned:
            database_engine = create_database_engine(resolved_settings)
            owned.callback(database_engine.dispose)
            session_factory = create_session_factory(database_engine)
            service = readiness_service or build_readiness_service(
                resolved_settings,
                database_engine=database_engine,
            )
            if readiness_service is None:
                owned.callback(service.close)
            services.readiness = service
            services.maintenance = maintenance_service or KernelMaintenanceService(
                PostgresKernelMaintenanceReader(session_factory)
            )
            services.storage_gc = storage_gc_service or StorageGcService(
                PostgresStorageGcRepository(session_factory)
            )
            app.state.session_factory = session_factory
            _install_notification_services(
                services,
                resolved_settings,
                session_factory,
                notification_service,
                notification_query_service,
                bot_service,
            )
            _install_execution_identity_services(
                services,
                resolved_settings,
                session_factory,
                execution_resource_service,
                terminal_mqtt_session_service,
            )
            maa_uow_factory, definitions = _install_maa_services(
                services,
                session_factory,
                maa_quick_test_service,
                execution_definition_registry,
            )
            _install_blob_services(
                services,
                resolved_settings,
                session_factory,
                owned,
                maa_uow_factory,
                maa_archive_import_command_service,
                terminal_blob_transfer_service,
                artifact_upload_service,
                recording_download_service,
                failure_review_service,
            )
            _install_scheduling_services(
                services,
                resolved_settings,
                session_factory,
                definitions,
                execution_scheduling_service,
                terminal_delivery_service,
            )
            install_service_state(app, services)
            yield

    app = FastAPI(
        title="AL-1S Platform API",
        version=resolved_settings.service_version,
        lifespan=lifespan,
    )
    app.state.settings = services.settings
    install_admin_auth(app, resolved_settings)
    app.state.modules = services.modules
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Request-ID",
            "X-AL1S-CSRF",
        ],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # SQL exceptions can include bound message bodies or secrets; never log their text.
        return internal_error_response(request, exc)

    @app.exception_handler(ExecutionDomainError)
    async def execution_domain_error(request: Request, exc: ExecutionDomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
        )

    @app.exception_handler(MaaDomainError)
    async def maa_domain_error(request: Request, exc: MaaDomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "context": exc.context,
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
        )

    @app.exception_handler(NotificationDomainError)
    async def notification_domain_error(
        request: Request, exc: NotificationDomainError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
        )

    @app.exception_handler(BotDomainError)
    async def bot_domain_error(request: Request, exc: BotDomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
            headers={"Cache-Control": "no-store"} if exc.status_code == 401 else None,
        )

    install_database_error_handlers(app)
    app.include_router(api_router)
    mount_frontend(app, resolved_settings.frontend_directory)
    return app
