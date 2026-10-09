import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import RLock
from typing import cast
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from al1s.components.host.backup import private_json
from al1s.components.host.commands import CommandRunner
from al1s.components.host.compose_driver import ComposeDeployment
from al1s.components.host.config import HostSettings
from al1s.components.host.contracts import JsonObject
from al1s.components.host.deployment_driver import DeploymentDriver
from al1s.components.host.executor import HostComponentExecutor
from al1s.components.host.journal import HostJournal
from al1s.components.host.platform_client import PlatformComponentClient
from al1s.components.host.probes import inventory
from al1s.components.status_capability import StatusCapability
from al1s.execution.errors import ExecutionDomainError


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: UUID


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


def create_host(settings: HostSettings) -> FastAPI:
    deployment = ComposeDeployment(settings, CommandRunner())
    client = PlatformComponentClient(settings.platform)
    driver = DeploymentDriver(deployment, client)
    executor = HostComponentExecutor(HostJournal(settings.state_dir / "operations"), driver)
    signer = StatusCapability(settings.platform.token.get_secret_value())
    verification_root = settings.state_dir / "verifications"
    verification_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="component-upgrade")
    probes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="component-verification")
    queue_lock = RLock()
    physical_lock = RLock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        pool.shutdown(wait=False, cancel_futures=True)
        probes.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(ExecutionDomainError)
    def domain_error(_request: Request, error: ExecutionDomainError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=error.status_code)

    def authorize(value: str | None) -> None:
        if not hmac.compare_digest(
            (value or "").encode(),
            ("Bearer " + settings.platform.token.get_secret_value()).encode(),
        ):
            raise HTTPException(401, detail="component_host_unauthorized")

    @app.get("/health")
    def health(authorization: str | None = Header(None)) -> JsonObject:
        authorize(authorization)
        return {"status": "ok"}

    @app.middleware("http")
    async def bounded_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith("/status/"):
            try:
                authorize(request.headers.get("authorization"))
            except HTTPException:
                return JSONResponse({"code": "component_host_unauthorized"}, status_code=401)
        if request.method == "POST":
            size = request.headers.get("content-length", "")
            if (
                not size.isdecimal()
                or not 0 < int(size) <= 8192
                or request.headers.get("transfer-encoding")
            ):
                return JSONResponse({"code": "component_request_size"}, status_code=413)
        return await call_next(request)

    @app.get("/inventory")
    def observation(authorization: str | None = Header(None)) -> JsonObject:
        authorize(authorization)
        return {"items": inventory(deployment)}

    def verify_package(identity: UUID, generation: int) -> None:
        directory = verification_root / f"{identity}-{generation}"
        directory.mkdir(mode=0o700, exist_ok=True)
        record = {"state": "running", "generation": generation}
        path = directory / "result.json"
        private_json(path, record)
        try:
            release = client.release(identity)
            if release.row_version != generation or release.state != "verifying":
                raise ValueError("component_verification_changed")
            archive = directory / "candidate.tar"
            client.download(release, archive)
            with physical_lock:
                driver.verify_archive(archive, release)
            record.update(state="completed", native_version=release.native_version)
        except Exception:
            record.update(state="failed", error_code="component_native_probe_failed")
        private_json(path, record)

    @app.post("/verify-release")
    def verify_request(
        body: ReleaseRequest, authorization: str | None = Header(None)
    ) -> JsonObject:
        authorize(authorization)
        release = client.release(body.release_id)
        if release.state != "verifying":
            raise HTTPException(409, detail="component_release_not_verifying")
        path = verification_root / f"{body.release_id}-{release.row_version}" / "result.json"
        with queue_lock:
            if path.exists():
                return cast(JsonObject, json.loads(path.read_text()))
            path.parent.mkdir(mode=0o700, exist_ok=True)
            private_json(path, {"state": "accepted", "generation": release.row_version})
            probes.submit(verify_package, body.release_id, release.row_version)
        return {"state": "accepted", "generation": release.row_version}

    @app.get("/verifications/{identity}")
    def verification(
        identity: UUID, generation: int = 1, authorization: str | None = Header(None)
    ) -> JsonObject:
        authorize(authorization)
        if not 1 <= generation <= 2147483647:
            raise HTTPException(400, detail="invalid_verification_generation")
        path = verification_root / f"{identity}-{generation}" / "result.json"
        return (
            cast(JsonObject, json.loads(path.read_text()))
            if path.exists()
            else {"state": "accepted", "generation": generation}
        )

    @app.post("/operations", status_code=202)
    def submit(body: OperationRequest, authorization: str | None = Header(None)) -> JsonObject:
        authorize(authorization)
        prior = executor.journal.load(body.operation_id)
        if prior is not None:
            return executor.status(body.operation_id)
        payload = client.operation(body.operation_id)
        if payload.get("operation", {}).get("state") != "accepted":
            raise HTTPException(409, detail="component_operation_not_accepted")
        with queue_lock:
            if executor.journal.load(body.operation_id) is not None:
                return executor.status(body.operation_id)
            receipt = executor.reserve(body.operation_id, payload)
            pool.submit(execute, body.operation_id, payload)
        return receipt

    def execute(identity: UUID, payload: JsonObject) -> JsonObject:
        with physical_lock:
            return executor.execute(identity, payload)

    @app.get("/operations/{identity}")
    def operation(identity: UUID, authorization: str | None = Header(None)) -> JsonObject:
        authorize(authorization)
        return executor.status(identity)

    @app.post("/operations/{identity}/reconcile")
    def reconcile(identity: UUID, authorization: str | None = Header(None)) -> JsonObject:
        authorize(authorization)
        with physical_lock:
            return executor.reconcile(identity)

    @app.get("/status/{identity}")
    def readonly_status(
        identity: UUID, request: Request, authorization: str | None = Header(None)
    ) -> JsonObject:
        if request.headers.get("origin") not in {None, settings.allowed_origin}:
            raise HTTPException(403, detail="component_status_origin")
        value = authorization.removeprefix("Bearer ") if authorization else ""
        signer.verify(value, identity)
        return executor.status(identity)

    return app
