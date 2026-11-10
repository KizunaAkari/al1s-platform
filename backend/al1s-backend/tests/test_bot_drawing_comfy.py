import asyncio
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from al1s.bot_drawing.comfy import DrawingComfyClient, owned_output
from al1s.bot_drawing.workflow import MAX_GRAPH_BYTES, _hash
from al1s.bot_drawing.media import MAX_IMAGE_DATA

PROMPT = UUID("00000000-0000-4000-8000-000000000123")
SERVICE = UUID("00000000-0000-4000-8000-000000000456")
OWNER = "al1s-drawing-test"
GRAPH = {"1": {"class_type": "fixture", "inputs": {}}}
DIGEST = _hash(GRAPH, limit=MAX_GRAPH_BYTES + MAX_IMAGE_DATA)


def job(status="completed", *, owner=OWNER, node="9", outputs=None):
    return {"id": str(PROMPT), "status": status, "workflow": {"extra_data": {"client_id": owner}},
        "outputs": outputs or {node: {"images": [{"filename": "output_00001_.png",
            "subfolder": f"al1s-drawing/{PROMPT}", "type": "output"}]}}}


def registration(owner=OWNER):
    return {"prompt_id": str(PROMPT), "service_id": str(SERVICE), "owner": owner,
            "operation": "draw", "execution_hash": DIGEST, "state": "registered"}


def image_body(metadata=False):
    result = BytesIO()
    info = PngInfo()
    if metadata:
        info.add_text("prompt", "private reference image data")
    Image.new("RGB", (8, 8), "blue").save(result, "PNG", pnginfo=info)
    return result.getvalue()


def test_output_must_belong_to_exact_prompt_and_single_output_node():
    assert owned_output(job(), PROMPT, "9") == "output_00001_.png"
    for result in (job(owner="other"), job(outputs={"9": {"images": [
            {"filename": "../private.png", "subfolder": f"al1s-drawing/{PROMPT}", "type": "output"}]}}),
            job(outputs={"9": {"images": [{"filename": "output_00001_.png", "subfolder": "other", "type": "output"}]}}),
            job(outputs={"9": {"images": [1, 2]}})):
        with pytest.raises(ValueError, match="comfy_output_invalid"):
            owned_output(result, PROMPT, "9", owner=OWNER)


def test_windows_output_directory_is_accepted_without_relaxing_ownership():
    result = job()
    image = result["outputs"]["9"]["images"][0]
    image["subfolder"] = f"al1s-drawing\\{PROMPT}"
    assert owned_output(result, PROMPT, "9", owner=OWNER) == "output_00001_.png"
    for invalid in (f"al1s-drawing\\..\\{PROMPT}", f"al1s-drawing\\{PROMPT}\\other"):
        image["subfolder"] = invalid
        with pytest.raises(ValueError, match="comfy_output_invalid"):
            owned_output(result, PROMPT, "9", owner=OWNER)


def test_lost_submission_receipt_is_unknown_and_never_resubmitted():
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(200, json=registration())
            if request.url.path.endswith("/begin"):
                return httpx.Response(200, json={"started": True})
            raise httpx.ReadTimeout("response lost", request=request)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        result = await client.submit(PROMPT, SERVICE, "draw", DIGEST, GRAPH)
        assert result == "unknown" and calls.count("/prompt") == 1
        await http.aclose()
    asyncio.run(run())


def test_begin_is_one_time_and_changed_graph_is_rejected():
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(200, json=registration())
            return httpx.Response(200, json={"started": False})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        assert await client.submit(PROMPT, SERVICE, "draw", DIGEST, GRAPH) == "unknown"
        assert "/prompt" not in calls
        with pytest.raises(ValueError, match="comfy_execution_hash_mismatch"):
            await client.submit(PROMPT, SERVICE, "draw", DIGEST, {"different": {}})
        await http.aclose()
    asyncio.run(run())


def test_foreign_registration_cannot_submit_or_cancel():
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=registration(owner="other"))
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        with pytest.raises(ValueError, match="comfy_resource_owner_mismatch"):
            await client.cancel(PROMPT, SERVICE, "draw", DIGEST)
        assert all(not path.endswith("/cancel") for path in calls)
        await http.aclose()
    asyncio.run(run())


def test_cancel_distinguishes_queued_cancel_from_unknown():
    async def run():
        def handler(request):
            return httpx.Response(200, json={"status": "cancelled"} if request.method == "POST" else registration())
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        assert await client.cancel(PROMPT, SERVICE, "draw", DIGEST) == "cancelled"
        await http.aclose()
    asyncio.run(run())


def test_resource_registration_binds_boot_owner_and_uses_authenticated_requests():
    async def run():
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path, request.headers.get("authorization")))
            if request.url.path == "/al1s/resources/v1/state":
                return httpx.Response(200, json={"protocol": 1, "adapter": "test-v1", "boot_id": "boot-1"})
            return httpx.Response(200, json={**registration(), "boot_id": "boot-1"})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        state = await client.state()
        assert state["boot_id"] == "boot-1"
        result = await client.register(PROMPT, SERVICE, "draw", DIGEST, "boot-1", [])
        assert result["state"] == "registered"
        assert all(value == "Bearer secret" for _, _, value in calls)
        await http.aclose()
    asyncio.run(run())


def test_automatic_policy_and_switch_use_authenticated_resource_api():
    async def run():
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path, request.headers.get("authorization")))
            return httpx.Response(200, json={"status": "queued"})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        await client.set_automatic(SERVICE, 3, True)
        assert await client.switch("draw", "preset-1") == "queued"
        assert calls == [("PUT", f"/al1s/resources/v1/automatic/{SERVICE}", "Bearer secret"),
                         ("POST", "/al1s/resources/v1/switch", "Bearer secret")]
        await http.aclose()
    asyncio.run(run())


def test_node_info_reads_only_requested_node_definitions():
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={name: {"name": name}})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        result = await client.node_info(["SaveImage", "SaveImage", "KSampler"])
        assert set(result) == {"SaveImage", "KSampler"} and len(paths) == 2
        await http.aclose()
    asyncio.run(run())


def test_completed_job_downloads_only_owned_image_through_resource_extension():
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/output"):
                return httpx.Response(200, content=image_body(metadata=True), headers={"content-type": "image/png"})
            if request.url.path.startswith("/api/jobs/"):
                return httpx.Response(200, json=job())
            return httpx.Response(200, json=registration())
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost:8188")
        client = DrawingComfyClient("http://localhost:8188", OWNER, "secret", client=http)
        body = await client.download(PROMPT, SERVICE, "draw", DIGEST, "9")
        with Image.open(BytesIO(body)) as image:
            assert image.info == {} and image.getpixel((0, 0)) == (0, 0, 255)
        assert f"/al1s/resources/v1/jobs/{PROMPT}/output" in calls
        assert not any(path == "/view" for path in calls)
        await http.aclose()
    asyncio.run(run())
