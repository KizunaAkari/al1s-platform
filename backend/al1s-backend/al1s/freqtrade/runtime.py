import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path
from uuid import UUID

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.freqtrade_repository import FreqtradeRepository
from al1s.freqtrade.charts import Charts
from al1s.freqtrade.contracts import FreqtradeChartRequest, FreqtradeCommand, FreqtradeReadRequest, FreqtradeRecord
from al1s.freqtrade.manager import FreqtradeManager
from al1s.freqtrade.service import FreqtradeService, authorized_ai_source
from al1s.quant.socket_runtime import secure_socket


def create_app(repository, manager, token: str) -> FastAPI:
    if len(token.encode()) < 32:
        raise ValueError("freqtrade_control_configuration_invalid")
    records: dict[UUID, FreqtradeRecord] = {}
    service = FreqtradeService(repository, manager)
    charts = Charts()
    observation = [time.monotonic(), None]

    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(repository.recover)
        active = await asyncio.to_thread(repository.active_profiles)
        settings_by_id = await asyncio.to_thread(
            repository.runtime_settings, [row.id for row in active]
        )
        for profile in active:
            settings = settings_by_id.get(profile.id)
            if settings is not None:
                with suppress(Exception):
                    await asyncio.to_thread(
                        manager.ensure,
                        profile,
                        settings.model_copy(update={"max_active_accounts": 50}),
                    )
        stop = asyncio.Event()

        async def cleanup():
            while not stop.is_set():
                with suppress(Exception):
                    await asyncio.to_thread(manager.idle_cleanup)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=30)

        task = asyncio.create_task(cleanup())
        observer = asyncio.create_task(manager.notices.run(manager, stop))
        try:
            yield
        finally:
            stop.set()
            await task
            await observer
            await asyncio.to_thread(manager.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if not hmac.compare_digest(
            request.headers.get("x-freqtrade-control", "").encode(), token.encode()
        ):
            return JSONResponse({"detail": "freqtrade_control_required"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/config/{service_id}")
    def read_config(service_id: UUID):
        return {"row_version": records[service_id].row_version if service_id in records else 0}

    @app.put("/config/{service_id}")
    def activate(service_id: UUID, record: FreqtradeRecord):
        from al1s.freqtrade.commands import validate_words

        validate_words(record.commands, set())
        current = records.get(service_id)
        if current is not None and current.row_version > record.row_version:
            raise HTTPException(409, "freqtrade_version_conflict")
        records[service_id] = record.model_copy(deep=True)
        manager.capacity = record.settings.max_active_accounts
        return {"row_version": record.row_version}

    @app.post("/command/{service_id}")
    def execute(service_id: UUID, command: FreqtradeCommand):
        current = records.get(service_id)
        if current is None or command.source.scope_kind == "admin":
            raise HTTPException(503, "freqtrade_configuration_missing")
        # The trusted QQ entry freezes an authorized snapshot before configuration changes.
        if command.action in {"chart", "f_chart"}:
            return charts.execute(command)
        return {"content": service.execute(service_id, command)}

    @app.post("/query/{service_id}")
    def query_for_ai(service_id: UUID, query: FreqtradeReadRequest):
        current = records.get(service_id)
        if current is None or repository.get(service_id).row_version != current.row_version:
            raise HTTPException(503, "freqtrade_configuration_missing")
        return service.query_for_ai(service_id, query)

    @app.post("/admin/command/{service_id}")
    def execute_admin(service_id: UUID, command: FreqtradeCommand):
        current = records.get(service_id)
        if (current is None or command.source.scope_kind != "admin"
                or command.action not in {"open", "buy", "sell", "deposit", "f_open", "long", "short", "close", "f_deposit", "f_margin"}
                or not command.settings.enabled or current.row_version < command.settings_version):
            raise HTTPException(503, "freqtrade_configuration_missing")
        return {"content": service.execute(service_id, command)}

    @app.post("/admin/query/{service_id}")
    def query_admin(service_id: UUID, query: FreqtradeReadRequest):
        current = records.get(service_id)
        if (current is None or query.source.scope_kind != "admin"
                or repository.get(service_id).row_version != current.row_version):
            raise HTTPException(503, "freqtrade_configuration_missing")
        return service.query_for_ai(service_id, query, admin=True)

    @app.post("/chart/{service_id}")
    def chart_for_ai(service_id: UUID, query: FreqtradeChartRequest):
        current = records.get(service_id)
        record = repository.get(service_id)
        if current is None or record.row_version != current.row_version:
            raise HTTPException(503, "freqtrade_configuration_missing")
        if not authorized_ai_source(record.settings, query.source, query.market):
            raise HTTPException(403, "freqtrade_read_denied")
        return charts.execute_for_ai((query.symbol, query.period), query.market)

    @app.get("/status")
    def status():
        processes = [psutil.Process()]
        with manager.lock:
            for instance in manager.instances.values():
                if instance.process.poll() is None:
                    processes.append(psutil.Process(instance.process.pid))
            active = len(processes) - 1
        wall = time.monotonic()
        cpu = sum(p.cpu_times().user + p.cpu_times().system for p in processes)
        elapsed = wall - observation[0]
        cpu_percent = (
            100 * (cpu - observation[1]) / elapsed
            if observation[1] is not None and elapsed > 0 and cpu >= observation[1]
            else 0
        )
        observation[:] = [wall, cpu]
        return {
            "state": "ready",
            "version": version("freqtrade"),
            "active_accounts": active,
            "total_accounts": repository.total_accounts(),
            "max_active_accounts": manager.capacity,
            "rss_bytes": sum(p.memory_info().rss for p in processes),
            "cpu_percent": round(cpu_percent, 1),
            "market": {
                "state": "available" if manager.market_success else "unverified",
                "last_success": manager.market_success,
                "error": None,
            },
        }

    return app


def run():
    token = os.environ["AL1S_FREQTRADE_CONTROL_TOKEN"]
    database = create_engine(
        os.environ["AL1S_FREQTRADE_PLATFORM_DATABASE_URL"], pool_size=2, max_overflow=2
    )
    repository = FreqtradeRepository(sessionmaker(database, expire_on_commit=False))
    manager = FreqtradeManager(
        repository,
        os.environ["AL1S_FREQTRADE_NATIVE_DATABASE_URL"],
        Path("/var/lib/al1s-freqtrade"),
    )
    app = create_app(repository, manager, token)
    try:
        with secure_socket(Path("/run/al1s-freqtrade")) as sock:
            uvicorn.Server(
                uvicorn.Config(
                    app, access_log=False, log_level="warning", timeout_graceful_shutdown=25
                )
            ).run(sockets=[sock])
    finally:
        database.dispose()


if __name__ == "__main__":
    run()
