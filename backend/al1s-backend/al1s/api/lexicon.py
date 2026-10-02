from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from al1s.app.service_state import service_state
from al1s.notifications.lexicon import REPOSITORY, SOURCE_PATH

router = APIRouter(prefix="/lexicon")


@router.get("")
def current(request: Request) -> JSONResponse:
    try:
        version = service_state(request).lexicon.current()
    except Exception:
        return JSONResponse({"code": "lexicon_unavailable"}, status_code=503)
    return JSONResponse({
        "available": version is not None,
        "source": f"https://github.com/{REPOSITORY}/blob/main/{SOURCE_PATH}",
        "commit": version.commit if version else None,
        "sha256": version.sha256 if version else None,
        "normalization_version": version.normalization_version if version else None,
        "word_count": len(version.words) if version else 0,
        "updated_at": version.created_at.isoformat() if version and version.created_at else None,
    })


@router.post("/update")
def update(request: Request) -> JSONResponse:
    try:
        changed = service_state(request).lexicon.update()
    except Exception:
        # Do not replace the last valid version or serialize upstream response bodies.
        return JSONResponse({"code": "lexicon_update_failed",
                             "message": "更新失败; 保留之前有效的词库。"}, status_code=502)
    return JSONResponse({"changed": changed})
