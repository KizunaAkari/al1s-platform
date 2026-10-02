from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status
from pydantic import BaseModel, Field

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state
from al1s.execution.artifact_upload_service import ArtifactUploadService
from al1s.execution.artifact_upload_types import (
    ArtifactKind,
    ArtifactOwnerKind,
    ArtifactStatus,
    ArtifactUploadRecord,
    PresignedArtifactUpload,
)

router = APIRouter()


class CreateArtifactUploadRequest(BaseModel):
    owner_kind: ArtifactOwnerKind
    owner_id: UUID
    artifact_kind: ArtifactKind
    file_name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=5 * 1024**3)
    media_type: str = Field(min_length=1, max_length=255)


class ArtifactUploadInstructionsResponse(BaseModel):
    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime


class ArtifactUploadResponse(BaseModel):
    artifact_id: UUID
    owner_kind: ArtifactOwnerKind
    owner_id: UUID
    artifact_kind: ArtifactKind
    file_name: str
    sha256: str
    size_bytes: int
    media_type: str
    status: ArtifactStatus
    expires_at: datetime
    completed_at: datetime | None
    blob_id: UUID | None
    row_version: int
    upload: ArtifactUploadInstructionsResponse | None = None
    processing: bool = False


def _service(request: Request) -> ArtifactUploadService:
    return service_state(request).artifact_uploads


@router.post(
    "/artifacts/uploads",
    response_model=ArtifactUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_artifact_upload(
    body: CreateArtifactUploadRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    authorization: str | None = Header(default=None),
) -> ArtifactUploadResponse:
    created = _service(request).create(
        terminal_id=authenticated_terminal_id(request, authorization),
        owner_kind=body.owner_kind,
        owner_id=body.owner_id,
        artifact_kind=body.artifact_kind,
        file_name=body.file_name,
        sha256=body.sha256,
        size_bytes=body.size_bytes,
        media_type=body.media_type,
        idempotency_key=idempotency_key,
    )
    result = _response(created.artifact, created.upload)
    result.processing = _service(request).is_completing(created.artifact.artifact_id)
    if result.processing:
        result.upload = None
    return result


@router.post(
    "/artifacts/uploads/{artifact_id}/complete",
    response_model=ArtifactUploadResponse,
)
def complete_artifact_upload(
    artifact_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    response: Response,
    authorization: str | None = Header(default=None),
    prefer: str | None = Header(default=None),
) -> ArtifactUploadResponse:
    terminal_id = authenticated_terminal_id(request, authorization)
    service = _service(request)
    response.headers["Cache-Control"] = "no-store"
    if prefer == "respond-async":
        artifact = service.get(terminal_id=terminal_id, artifact_id=artifact_id)
        if artifact.status is ArtifactStatus.READY:
            return _response(artifact, None)
        if service.reserve_completion(artifact_id):
            background_tasks.add_task(_finish_completion, service, terminal_id, artifact_id)
        response.status_code = 202
        response.headers["Location"] = f"/api/v1/terminal/artifacts/uploads/{artifact_id}"
        result = _response(artifact, None)
        result.processing = True
        return result
    completed = _service(request).complete(
        terminal_id=terminal_id,
        artifact_id=artifact_id,
    )
    return _response(completed, None)


def _finish_completion(
    service: ArtifactUploadService, terminal_id: UUID, artifact_id: UUID
) -> None:
    try:
        service.complete(terminal_id=terminal_id, artifact_id=artifact_id)
    except Exception as exc:
        structlog.get_logger().warning(
            "artifact_completion_retry_required", error_type=type(exc).__name__
        )
    finally:
        service.release_completion(artifact_id)


@router.get("/artifacts/uploads/{artifact_id}", response_model=ArtifactUploadResponse)
def get_artifact_upload(
    artifact_id: UUID,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> ArtifactUploadResponse:
    service = _service(request)
    artifact = service.get(
        terminal_id=authenticated_terminal_id(request, authorization), artifact_id=artifact_id
    )
    response.headers["Cache-Control"] = "no-store"
    result = _response(artifact, None)
    result.processing = service.is_completing(artifact_id)
    return result


def _response(
    artifact: ArtifactUploadRecord,
    upload: PresignedArtifactUpload | None,
) -> ArtifactUploadResponse:
    return ArtifactUploadResponse(
        artifact_id=artifact.artifact_id,
        owner_kind=artifact.owner_kind,
        owner_id=artifact.owner_id,
        artifact_kind=artifact.artifact_kind,
        file_name=artifact.file_name,
        sha256=artifact.expected_sha256,
        size_bytes=artifact.expected_size_bytes,
        media_type=artifact.media_type,
        status=artifact.status,
        expires_at=artifact.expires_at,
        completed_at=artifact.completed_at,
        blob_id=artifact.blob_id,
        row_version=artifact.row_version,
        upload=(
            ArtifactUploadInstructionsResponse(
                method=upload.method,
                url=upload.url,
                headers=upload.headers,
                expires_at=upload.expires_at,
            )
            if upload is not None
            else None
        ),
    )
