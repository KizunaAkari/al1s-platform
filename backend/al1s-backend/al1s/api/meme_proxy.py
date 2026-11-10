import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from al1s.app.service_state import service_state
from al1s.infrastructure.direct_http import NoRedirect

router = APIRouter()
UUID_PART = r"[0-9a-fA-F-]{36}"
ADMIN_PATH = re.compile(
    rf"(?:templates(?:/{UUID_PART})?|settings/{UUID_PART}|assets(?:/{UUID_PART})?|preview|"
    r"rate-limits(?:/(?:discord|qq))?|runtime)\Z"
)
WORKER_PATH = re.compile(rf"(?:catalog|claim|render|{UUID_PART}/(?:send|result))\Z")


async def forward(request: Request, path: str, platform: str | None = None,
                  max_body_bytes: int = 56 * 1024 * 1024, *, timeout: float | None = None,
                  size_error: str = "表情请求超过大小限制", allow_drawing_view: bool = False,
                  allow_trading_page: bool = False) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_body_bytes:
            raise HTTPException(413, size_error)
        body.extend(chunk)
    allowed_query = ({"page"} if allow_trading_page else
                     {"after_id", "limit", "view"} if allow_drawing_view else {"after_id", "limit"})
    if any(key not in allowed_query for key in request.query_params):
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
        arguments = (request.method, path + suffix, bytes(body), headers)
        result = await run_in_threadpool(backend.forward, *arguments,
                                         **({"timeout": timeout} if timeout is not None else {}))
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
    "/bots/quant/settings/{service_id}", methods=["GET", "PUT"], include_in_schema=False
)
async def quant_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_quant_query")
    return await forward(request, f"/api/v1/bots/quant/settings/{service_id}", "qq", 65536)


@router.get("/bots/quant/status", include_in_schema=False)
async def quant_status_forward(request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_quant_query")
    return await forward(request, "/api/v1/bots/quant/status", "qq", 0)


@router.get("/bots/trading/commands/{service_id}/{engine}", include_in_schema=False)
async def trading_commands(service_id: UUID, engine: Literal["quant", "freqtrade"], request: Request):
    if request.query_params:
        raise HTTPException(400, "invalid_reply_query")
    return await forward(request, f"/api/v1/bots/trading/commands/{service_id}/{engine}", "qq", 0)


@router.put("/bots/trading/commands/{service_id}/{engine}/{action}", include_in_schema=False)
async def trading_command_save(service_id: UUID, engine: Literal["quant", "freqtrade"], action: str, request: Request):
    if request.query_params or not re.fullmatch(r"[a-z_]{1,16}", action):
        raise HTTPException(400, "invalid_reply_query")
    return await forward(request, f"/api/v1/bots/trading/commands/{service_id}/{engine}/{action}", "qq", 65536)


@router.post("/bots/trading/commands/{service_id}/{engine}/{action}/preview", include_in_schema=False)
async def trading_command_preview(service_id: UUID, engine: Literal["quant", "freqtrade"], action: str, request: Request):
    if request.query_params or not re.fullmatch(r"[a-z_]{1,16}", action):
        raise HTTPException(400, "invalid_reply_query")
    return await forward(request, f"/api/v1/bots/trading/commands/{service_id}/{engine}/{action}/preview", "qq", 65536)


@router.api_route('/bots/freqtrade/settings/{service_id}', methods=['GET', 'PUT'], include_in_schema=False)
async def freqtrade_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, 'invalid_freqtrade_query')
    return await forward(request, f'/api/v1/bots/freqtrade/settings/{service_id}', 'qq', 65536)


@router.get('/bots/freqtrade/status', include_in_schema=False)
async def freqtrade_status_forward(request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, 'invalid_freqtrade_query')
    return await forward(request, '/api/v1/bots/freqtrade/status', 'qq', 0)


@router.api_route("/bots/trading/account-owner", methods=["GET", "PUT"], include_in_schema=False)
async def trading_owner_forward(request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_trading_query")
    return await forward(request, "/api/v1/bots/trading/account-owner", "qq", 1024)


@router.get("/bots/trading/{service_id}/{market}/{view}", include_in_schema=False)
async def trading_account_forward(service_id: UUID, market: Literal["a-share", "spot", "futures"],
                                  view: Literal["account", "positions", "orders"], request: Request) -> Response:
    return await forward(request, f"/api/v1/bots/trading/{service_id}/{market}/{view}", "qq", 0,
                         timeout=130, allow_trading_page=True)


@router.post("/bots/trading/{service_id}/{market}/actions", include_in_schema=False)
async def trading_action_forward(service_id: UUID, market: Literal["a-share", "spot", "futures"], request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_trading_query")
    return await forward(request, f"/api/v1/bots/trading/{service_id}/{market}/actions", "qq", 4096, timeout=160)


@router.get("/bots/trading/{service_id}/{market}/actions/{operation_id}", include_in_schema=False)
async def trading_operation_forward(service_id: UUID, market: Literal["a-share", "spot", "futures"],
                                    operation_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_trading_query")
    return await forward(request, f"/api/v1/bots/trading/{service_id}/{market}/actions/{operation_id}", "qq", 0)


def probe_local_vision() -> Literal["running", "stopped", "incompatible", "unknown"]:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    root = "http://host.docker.internal:8188"
    nodes = {
        "/object_info/AILab_QwenVL_Advanced": "AILab_QwenVL_Advanced",
        "/object_info/ImageReceiver": "ImageReceiver",
        "/object_info/ShowText%7Cpysssss": "ShowText|pysssss",
    }
    try:
        def read(path: str):
            with opener.open(root + path, timeout=2) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("response_too_large")
            return json.loads(raw)

        stats = read("/system_stats")
        version = stats["system"]["comfyui_version"]
        match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
        if not match or tuple(map(int, match.groups())) < (0, 43):
            return "incompatible"
        for path, node in nodes.items():
            info = read(path)
            if not isinstance(info, dict) or not isinstance(info.get(node), dict):
                return "incompatible"
        return "running"
    except HTTPError as exc:
        return "unknown" if exc.code >= 500 else "incompatible"
    except URLError as exc:
        return "stopped" if isinstance(exc.reason, ConnectionRefusedError) else "unknown"
    except ConnectionRefusedError:
        return "stopped"
    except (TimeoutError, OSError):
        return "unknown"
    except (ValueError, TypeError, KeyError, AttributeError):
        return "incompatible"


@router.get("/bots/ai/local-vision/status", include_in_schema=False)
def local_vision_status(request: Request) -> JSONResponse:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return JSONResponse({"state": probe_local_vision()}, headers={"Cache-Control": "no-store"})


_DRAWING_ROUTES = (
    (re.compile(r"prompt-models\Z"), {"GET", "POST"}, 8192),
    (re.compile(r"prompt-models/active\Z"), {"PUT"}, 4096),
    (re.compile(rf"prompt-models/{UUID_PART}\Z"), {"PUT", "DELETE"}, 8192),
    (re.compile(rf"prompt-models/{UUID_PART}/test\Z"), {"POST"}, 4096),
    (re.compile(r"settings\Z"), {"GET", "PUT"}, 320 * 1024),
    (re.compile(r"group-modes\Z"), {"GET"}, 0),
    (re.compile(r"group-modes/[1-9][0-9]{0,19}\Z"), {"PUT"}, 4096),
    (re.compile(r"presets\Z"), {"GET", "POST"}, 320 * 1024),
    (re.compile(r"online-presets\Z"), {"POST"}, 8192),
    (re.compile(rf"online-presets/{UUID_PART}\Z"), {"PUT"}, 8192),
    (re.compile(rf"presets/{UUID_PART}\Z"), {"GET", "PUT"}, 320 * 1024),
    (re.compile(rf"presets/{UUID_PART}/(?:check|preview)\Z"), {"POST"}, 9 * 1024 * 1024),
    (re.compile(r"tasks\Z"), {"GET"}, 0),
    (re.compile(r"tasks/batch-delete\Z"), {"POST"}, 8192),
    (re.compile(rf"tasks/{UUID_PART}(?:/image)?\Z"), {"GET"}, 0),
    (re.compile(rf"tasks/{UUID_PART}/(?:cancel|retry-send)\Z"), {"POST"}, 4096),
    (re.compile(r"resources\Z"), {"GET"}, 0),
    (re.compile(r"resources/(?:warmup|release)\Z"), {"POST"}, 4096),
)


@router.api_route("/bots/ai/drawing/{service_id}/{path:path}", methods=["GET", "PUT", "POST", "DELETE"], include_in_schema=False)
async def drawing_forward(service_id: UUID, path: str, request: Request) -> Response:
    route = next((entry for entry in _DRAWING_ROUTES if entry[0].fullmatch(path) and request.method in entry[1]), None)
    if route is None:
        raise HTTPException(404, "drawing_route_not_found")
    if request.query_params and not (request.method == "GET" and path == "tasks"):
        raise HTTPException(400, "invalid_drawing_query")
    limit = route[2] if request.method != "GET" else 0
    timeout = 120.0 if path.endswith(("/check", "/warmup")) else 25.0 if path.endswith("/test") else None
    return await forward(request, f"/api/v1/bots/ai/drawing/{service_id}/{path}", "qq", limit,
                         timeout=timeout, size_error="绘图请求超过大小限制",
                         allow_drawing_view=request.method == "GET" and path == "tasks")


@router.api_route("/bots/ai/settings/{service_id}", methods=["GET", "PUT"], include_in_schema=False)
async def ai_settings_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/settings/{service_id}", "qq", 65536)


@router.get("/bots/ai/status/{service_id}", include_in_schema=False)
async def ai_status_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/status/{service_id}", "qq", 0)


@router.post("/bots/ai/stickers/{service_id}", include_in_schema=False)
async def ai_stickers_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/stickers/{service_id}", "qq", 16384)


@router.post("/bots/ai/stickers/{service_id}/import/{group_id}/{offset}", include_in_schema=False)
async def ai_sticker_import_forward(service_id: UUID, group_id: str, offset: int, request: Request) -> Response:
    if request.query_params or not re.fullmatch(r"[1-9][0-9]{0,19}", group_id) or not 0 <= offset <= 100:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/stickers/{service_id}/import/{group_id}/{offset}",
                         "qq", 32 * 1024 * 1024)


@router.get("/bots/ai/chat-profiles/{service_id}", include_in_schema=False)
async def ai_chat_profiles_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/chat-profiles/{service_id}", "qq", 0)


@router.api_route("/bots/ai/chat-profiles/{service_id}/{profile_id}",
                  methods=["PUT", "DELETE"], include_in_schema=False)
async def ai_chat_profile_forward(service_id: UUID, profile_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/chat-profiles/{service_id}/{profile_id}",
                         "qq", 65536 if request.method == "PUT" else 0)


@router.post("/bots/ai/chat-profiles/{service_id}/{profile_id}/test", include_in_schema=False)
async def ai_chat_profile_test_forward(service_id: UUID, profile_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/chat-profiles/{service_id}/{profile_id}/test", "qq", 0)


@router.get("/bots/ai/requests/{service_id}", include_in_schema=False)
async def ai_requests_forward(service_id: UUID, request: Request) -> Response:
    return await forward(request, f"/api/v1/bots/ai/requests/{service_id}", "qq", 0)


@router.post("/bots/ai/providers/test/{service_id}", include_in_schema=False)
async def ai_provider_test_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/providers/test/{service_id}", "qq", 4096)


@router.post("/bots/ai/sessions/{service_id}/clear", include_in_schema=False)
async def ai_clear_forward(service_id: UUID, request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_ai_query")
    return await forward(request, f"/api/v1/bots/ai/sessions/{service_id}/clear", "qq", 4096)


@router.get("/bots/quant/credentials", include_in_schema=False)
async def quant_credentials_status(request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_quant_query")
    return await forward(request, "/api/v1/bots/quant/credentials", "qq", 0)


@router.put("/bots/quant/credentials/{provider}", include_in_schema=False)
async def quant_credentials_save(provider: Literal["tushare", "gugudata"], request: Request) -> Response:
    if request.query_params:
        raise HTTPException(400, "invalid_quant_query")
    return await forward(request, f"/api/v1/bots/quant/credentials/{provider}", "qq", 4096)


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
