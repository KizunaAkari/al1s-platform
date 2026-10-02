"""Public image read and administrator-only appearance upload."""


from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from al1s.adapters.s3.blob_store import BlobNotFoundError
from al1s.app.service_state import service_state
from al1s.appearance.service import MAX_BYTES, AppearanceService

router = APIRouter(prefix="/appearance")


def _service(request: Request) -> AppearanceService:
    return service_state(request).appearance


@router.get("/login-background")
def background(request: Request) -> Response:
    try:
        body, media_type = _service(request).read()
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="appearance_image_missing") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="appearance_image_unavailable") from exc
    return Response(body, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.put("/login-background")
async def upload(request: Request) -> dict[str, str]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BYTES:
            raise HTTPException(status_code=413, detail="appearance_image_size")
        content.extend(chunk)
    try:
        saved_type = await run_in_threadpool(_service(request).upload, bytes(content), media_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="appearance_upload_unavailable") from exc
    return {"media_type": saved_type}
