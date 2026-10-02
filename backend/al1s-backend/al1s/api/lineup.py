import asyncio
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from al1s.app.service_state import service_state
from al1s.execution.errors import InvalidRequestError
from al1s.lineup.annotations import AnnotationDocument
from al1s.lineup.catalog import ASSETS, catalog
from al1s.maa.image_resources import MAX_IMAGE_BYTES

router = APIRouter(prefix="/lineup")


class RunRequest(BaseModel):
    terminal_id: UUID
    layout_hint: Literal["auto", "attack", "defense", "left_attack", "right_attack"] = "auto"
    recognition_mode: Literal["auto", "portrait", "text"] = "auto"


class ReviewRequest(BaseModel):
    expected_version: int = Field(ge=1)
    student_ids: list[StrictInt | None] = Field(min_length=12, max_length=12)


class BatchQueryRequest(BaseModel):
    record_ids: list[UUID] = Field(min_length=1, max_length=100)


class SubmitTaskRequest(RunRequest):
    record_ids: list[UUID] = Field(min_length=1, max_length=200)
    idempotency_key: UUID


class RetryTaskRequest(BaseModel):
    idempotency_key: UUID


class SaveAnnotationRequest(AnnotationDocument):
    expected_version: StrictInt = Field(ge=0)


@router.post("/tasks", status_code=201)
def submit_task(body: SubmitTaskRequest, request: Request) -> dict[str, Any]:
    return service_state(request).lineup_workspace.submit(
        body.record_ids,
        body.terminal_id,
        dict(layout_hint=body.layout_hint, recognition_mode=body.recognition_mode),
        str(body.idempotency_key),
    )


@router.get("/tasks/{task_id}")
def task_results(
    task_id: UUID,
    request: Request,
    after: int = Query(0, ge=0, le=200),
    category: Literal["all", "usable", "attention", "failure"] = "all",
) -> dict[str, Any]:
    return service_state(request).lineup_workspace.queries.task(
        task_id, after=after, category=category
    )


@router.post("/tasks/{task_id}/retry", status_code=201)
def retry_task(task_id: UUID, body: RetryTaskRequest, request: Request) -> dict[str, Any]:
    return service_state(request).lineup_workspace.retry(task_id, str(body.idempotency_key))


@router.get("/annotation-tasks")
def annotation_tasks(request: Request, cursor: UUID | None = None) -> dict[str, Any]:
    return service_state(request).lineup_workspace.queries.annotation_tasks(cursor)


@router.get("/records/{record_id}/annotation")
def get_annotation(
    record_id: UUID, request: Request, task_id: UUID | None = None
) -> dict[str, Any]:
    return service_state(request).lineup_workspace.annotation(record_id, task_id)


@router.put("/records/{record_id}/annotation")
def put_annotation(
    record_id: UUID, body: SaveAnnotationRequest, request: Request
) -> dict[str, Any]:
    return service_state(request).lineup_workspace.save_annotation(
        record_id, body.expected_version, body.model_dump(mode="json", exclude={"expected_version"})
    )


@router.get("/records/{record_id}/annotation/export")
def export_annotation(record_id: UUID, request: Request) -> Response:
    return Response(
        service_state(request).lineup_workspace.export_annotation(record_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="annotation-{record_id}.zip"'},
    )


@router.get("/catalog")
def get_catalog() -> dict[str, Any]:
    data = catalog()
    return dict(
        version=data["version"],
        students=[{k: s[k] for k in ("id", "name", "aliases")} for s in data["students"]],
    )


@router.get("/terminals")
def get_terminals(request: Request) -> dict[str, Any]:
    return dict(items=service_state(request).lineup.repository.terminals())


@router.get("/records")
def list_records(request: Request, cursor: UUID | None = None) -> dict[str, Any]:
    return service_state(request).lineup.page(cursor)


@router.post("/records", status_code=201)
async def upload_record(
    request: Request, name: str = Query(default="战报图片", max_length=160)
) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";")[0] not in ("image/png", "image/jpeg"):
        raise InvalidRequestError("lineup_image_type", "仅支持 PNG/JPEG 图片")
    slots = service_state(request).maa_image_slots
    if slots.locked():
        raise InvalidRequestError("lineup_upload_busy", "图片处理中。请稍后再试")
    async with slots:
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                        raise InvalidRequestError("lineup_image_size", "图片不得超过 16 MiB")
                    body.extend(chunk)
        except TimeoutError as exc:
            raise InvalidRequestError("lineup_upload_timeout", "图片上传超时。请重试") from exc
        return await run_in_threadpool(service_state(request).lineup.upload, name, bytes(body))


@router.post("/records/query")
def query_records(body: BatchQueryRequest, request: Request) -> dict[str, Any]:
    return service_state(request).lineup.batch_details(body.record_ids)


@router.get("/records/{record_id}")
def get_record(record_id: UUID, request: Request) -> dict[str, Any]:
    return service_state(request).lineup.detail(record_id)


@router.get("/records/{record_id}/image")
def get_image(record_id: UUID, request: Request) -> Response:
    service = service_state(request).lineup
    body = service.images.read(service.repository.get(record_id)["blob_id"])
    return Response(body, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/records/{record_id}/portrait/{student_id}")
def get_portrait(record_id: UUID, student_id: int, request: Request) -> Response:
    service_state(request).lineup.repository.get(record_id)
    if student_id not in {s["id"] for s in catalog()["students"]}:
        raise InvalidRequestError("lineup_student_invalid", "学生 ID 无效")
    return Response(
        (ASSETS / "portraits" / f"{student_id}.webp").read_bytes(),
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/records/{record_id}/run")
def run_record(record_id: UUID, body: RunRequest, request: Request) -> dict[str, Any]:
    state = service_state(request)
    return state.lineup.run(
        record_id,
        body.terminal_id,
        state.execution_scheduling,
        uuid4(),
        dict(layout_hint=body.layout_hint, recognition_mode=body.recognition_mode),
    )


@router.put("/records/{record_id}/review")
def save_review(record_id: UUID, body: ReviewRequest, request: Request) -> dict[str, Any]:
    return service_state(request).lineup.review(record_id, body.expected_version, body.student_ids)
