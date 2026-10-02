"""Authenticated, bounded editor crop OCR through the registered terminal."""

import asyncio
import json
import os
import ssl
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from al1s.execution.editor_sessions import EditorStatus
from al1s.infrastructure.editor_relay import DirectConnect, allowed_target

router = APIRouter()
MAX_BYTES = 4 * 1024 * 1024


@router.post("/editor-sessions/{session_id}/ocr")
async def test_ocr(session_id: UUID, request: Request) -> dict[str, object]:
    if request.headers.get("authorization"):
        raise HTTPException(403, "Operator context required")
    session, connection = await run_in_threadpool(
        request.app.state.editor_sessions.detail,
        session_id,
    )
    target = connection.get("ocr_ws_url") if connection else None
    if session.status is not EditorStatus.ACTIVE or not target or not allowed_target(target):
        raise HTTPException(409, "当前终端未提供 OCR 测试。请重新连接手机")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "OCR 选区过大。请缩小区域")
        body.extend(chunk)
    if len(body) < 33 or body[:8] != b"\x89PNG\r\n\x1a\n":
        raise HTTPException(422, "OCR 测试需要 PNG 选区")
    try:
        context = ssl.create_default_context(cafile=os.environ["AL1S_EDITOR_RELAY_CA_FILE"])
        async with DirectConnect(
            target,
            ssl=context,
            proxy=None,
            open_timeout=8,
            close_timeout=2,
            max_size=1024 * 1024,
            compression=None,
        ) as upstream:
            await asyncio.wait_for(upstream.send(bytes(body)), 5)
            payload = await asyncio.wait_for(upstream.recv(), 30)
        result = json.loads(payload)
        if not isinstance(result, dict) or not isinstance(result.get("texts"), list):
            raise ValueError("Invalid OCR result")
        return {"texts": [str(text)[:1000] for text in result["texts"][:200]]}
    except Exception as exc:
        raise HTTPException(502, "OCR 测试失败。请检查终端模型后重试") from exc
