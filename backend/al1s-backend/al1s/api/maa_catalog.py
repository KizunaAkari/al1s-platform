"""Compatibility route and import surface for Maa catalog endpoints."""

from fastapi import APIRouter

from al1s.api.maa_catalog_applications import router as application_router
from al1s.api.maa_catalog_devices import router as device_router
from al1s.api.maa_catalog_imports import import_archive as import_archive
from al1s.api.maa_catalog_imports import router as import_router
from al1s.api.maa_catalog_schemas import ImportArchiveRequest as ImportArchiveRequest
from al1s.api.maa_catalog_schemas import StaticCheckRequest as StaticCheckRequest
from al1s.api.maa_catalog_scripts import check_script_candidate as check_script_candidate
from al1s.api.maa_catalog_scripts import router as script_router
from al1s.api.maa_catalog_strategies import router as strategy_router

router = APIRouter()
for child_router in (
    device_router,
    application_router,
    script_router,
    strategy_router,
    import_router,
):
    router.include_router(child_router)
