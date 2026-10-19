import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from al1s.app.service_state import service_state

router = APIRouter()
UUID_PART = r"[0-9a-fA-F-]{36}"
ADMIN_PATH = re.compile(
    rf"(?:templates(?:/{UUID_PART})?|settings/{UUID_PART}|assets(?:/{UUID_PART})?|preview|"
    r"rate-limits(?:/(?:discord|qq))?|runtime)\Z"
)
WORKER_PATH = re.compile(rf"(?:catalog|claim|render|{UUID_PART}/(?:send|result))\Z")


async def forward(request: Request, path: str, platform: str | None = None) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 56 * 1024 * 1024:
            raise HTTPException(413, "表情请求超过大小限制")
        body.extend(chunk)
    if any(key not in {"after_id", "limit"} for key in request.query_params):
        raise HTTPException(400, "invalid_meme_query")
    query = urlencode(list(request.query_params.multi_items()))
    suffix = f"?{query}" if query else ""
    headers = {
        name: value
        for name in ("content-type", "authorization", "if-match", "idempotency-key")
        if (value := request.headers.get(name)) is not None
    }
    try:
        services = service_state(request)
        backend = services.meme_backend if platform is None else services.meme_backends[platform]
        result = await run_in_threadpool(
            backend.forward,
            request.method,
            path + suffix,
            bytes(body),
            headers,
        )
    except ValueError as exc:
        message = (
            "表情包Bot后端尚未配置"
            if str(exc) == "meme_backend_not_configured"
            else "表情包Bot后端响应无效"
        )
        raise HTTPException(503, message) from exc
    except (OSError, TimeoutError) as exc:
        raise HTTPException(503, "表情包Bot后端暂时不可用, 请核对后重试") from exc
    return Response(
        result.body,
        status_code=result.status,
        media_type=result.media_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.api_route(
    "/bots/memes/{platform}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
)
async def platform_forward(platform: str, path: str, request: Request) -> Response:
    if platform not in {"qq", "discord"} or not ADMIN_PATH.fullmatch(path):
        raise HTTPException(404, "meme_endpoint_not_found")
    return await forward(request, f"/api/v1/bots/memes/{path}", platform)


@router.api_route(
    "/bots/memes/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False
)
async def admin_forward(path: str, request: Request) -> Response:
    if not ADMIN_PATH.fullmatch(path):
        raise HTTPException(404, "meme_endpoint_not_found")
    return await forward(request, f"/api/v1/bots/memes/{path}")


@router.api_route(
    "/bots/worker/memes/{path:path}", methods=["GET", "POST"], include_in_schema=False
)
async def worker_forward(path: str, request: Request) -> Response:
    if not WORKER_PATH.fullmatch(path):
        raise HTTPException(404, "meme_endpoint_not_found")
    header = request.headers.get("authorization", "")
    credential = header[7:] if header.startswith("Bearer ") else ""
    service_state(request).bot_service.worker_service_id(credential)
    return await forward(request, f"/api/v1/bots/worker/memes/{path}")
