from fastapi import APIRouter

from al1s.api.appearance import router as appearance_router
from al1s.api.bot_containers import router as bot_containers_router
from al1s.api.bots import router as bots_router
from al1s.api.discord_forward_rules import router as discord_forward_rules_router
from al1s.api.editor_sessions import router as editor_sessions_router
from al1s.api.executions import router as executions_router
from al1s.api.host_management import router as host_management_router
from al1s.api.lexicon import router as lexicon_router
from al1s.api.lineup import router as lineup_router
from al1s.api.linux_releases import router as linux_releases_router
from al1s.api.maa import router as maa_router
from al1s.api.maa_archive_upload import router as maa_archive_upload_router
from al1s.api.maa_catalog import router as maa_catalog_router
from al1s.api.maa_export import router as maa_export_router
from al1s.api.maa_images import router as maa_images_router
from al1s.api.maa_import_operations import router as maa_import_operations_router
from al1s.api.notifications import router as notifications_router
from al1s.api.reviews import router as reviews_router
from al1s.api.system import router as system_router
from al1s.api.target_devices import router as target_devices_router
from al1s.api.task_failure_details import router as task_failure_details_router
from al1s.api.task_recordings import router as task_recordings_router
from al1s.api.task_schedules import router as task_schedules_router
from al1s.api.tasks import router as tasks_router
from al1s.api.terminal_artifacts import router as terminal_artifacts_router
from al1s.api.terminal_blobs import router as terminal_blobs_router
from al1s.api.terminal_delivery import router as terminal_delivery_router
from al1s.api.terminal_mqtt import router as terminal_mqtt_router
from al1s.api.terminal_quick_tests import router as terminal_quick_tests_router
from al1s.api.terminals import router as terminals_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(lineup_router, tags=['lineup'])

api_router.include_router(appearance_router, tags=["appearance"])
api_router.include_router(linux_releases_router, tags=["linux-releases"])
api_router.include_router(host_management_router, tags=["host-management"])
api_router.include_router(bot_containers_router, tags=["bots"])
api_router.include_router(reviews_router, tags=["reviews"])
api_router.include_router(discord_forward_rules_router, tags=["discord-forward-rules"])
api_router.include_router(editor_sessions_router, tags=["editor-sessions"])
api_router.include_router(system_router, prefix="/system", tags=["system"])
api_router.include_router(terminals_router, prefix="/terminals", tags=["terminals"])
api_router.include_router(terminal_delivery_router, prefix="/terminal", tags=["terminal-delivery"])
api_router.include_router(terminal_mqtt_router, prefix="/terminal", tags=["terminal-mqtt"])
api_router.include_router(terminal_blobs_router, prefix="/terminal", tags=["terminal-blobs"])
api_router.include_router(
    terminal_artifacts_router,
    prefix="/terminal",
    tags=["terminal-artifacts"],
)
api_router.include_router(
    terminal_quick_tests_router,
    prefix="/terminal",
    tags=["terminal-quick-tests"],
)
api_router.include_router(target_devices_router, prefix="/target-devices", tags=["target-devices"])
api_router.include_router(executions_router, prefix="/executions", tags=["executions"])
api_router.include_router(maa_router, prefix="/maa", tags=["maa"])
api_router.include_router(maa_catalog_router, prefix="/maa", tags=["maa"])
api_router.include_router(maa_images_router, prefix="/maa", tags=["maa"])
api_router.include_router(maa_export_router, prefix="/maa", tags=["maa"])
api_router.include_router(maa_archive_upload_router, prefix="/maa", tags=["maa"])
api_router.include_router(maa_import_operations_router, prefix="/maa", tags=["maa"])
api_router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
api_router.include_router(lexicon_router, prefix="/notifications", tags=["notifications"])
api_router.include_router(bots_router, prefix="/bots", tags=["bots"])
api_router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
api_router.include_router(task_recordings_router, prefix="/tasks", tags=["task-recordings"])
api_router.include_router(task_failure_details_router, prefix="/tasks", tags=["task-details"])
api_router.include_router(
    task_schedules_router,
    prefix="/task-schedules",
    tags=["task-schedules"],
)
