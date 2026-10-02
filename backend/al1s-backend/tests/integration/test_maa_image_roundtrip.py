import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_models import TargetDeviceRow, TerminalRow
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.infrastructure.readiness import ReadinessResult
from al1s.kernel.gc_worker import GcWorker

pytestmark = pytest.mark.integration


def test_image_upload_save_reopen_and_gc_protection(monkeypatch):
    dsn = os.getenv("AL1S_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("isolated PostgreSQL required")
    url = make_url(dsn)
    if (
        url.database != "release_test" or url.username != "release_test"
        or url.host != "127.0.0.1" or os.getenv("AL1S_DATABASE_URL") != dsn
    ):
        pytest.skip("requires the verified release_test container and matching app DSN")
    engine = create_engine(dsn)
    # Run only inside the verified network-none/tmpfs integration container.
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE maa_applications, blob_objects CASCADE"))
    terminal_id, phone_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            TerminalRow(
                id=terminal_id,
                installation_id=uuid4(),
                terminal_type="linux",
                display_name="image-test",
                service_status="online",
                acceptance_status="accepting",
                agent_version="test",
                row_version=1,
            )
        )
        session.flush()
        session.add(
            TargetDeviceRow(
                id=phone_id,
                display_name="phone",
                platform="android",
                mode="mounted",
                managing_terminal_id=terminal_id,
                row_version=1,
            )
        )
    objects = {}
    request_prefix = str(uuid4())

    class Store:
        def put(self, key, body, media_type):
            objects[key] = body

        def head(self, key):
            return SimpleNamespace(size_bytes=len(objects[key]), media_type="image/png")

        def get_range(self, key, start, end):
            return objects[key][start : end + 1]

        def delete(self, key):
            objects.pop(key, None)

    class Ready:
        def check(self):
            return ReadinessResult(ready=True, dependencies={})

        def close(self):
            pass

    app = create_app(Settings(admin_password="image-test-password"), readiness_service=Ready())

    async def exercise():
        async with app.router.lifespan_context(app):
            async def foreground_package(*_args):
                return "com.example.image"

            monkeypatch.setattr(
                "al1s.api.maa_catalog_devices.probe_foreground_package", foreground_package
            )
            monkeypatch.setattr(app.state.maa_editor_creation, "needs_icon", lambda *_args: False)
            app.state.maa_images._store = Store()
            app.state.maa_exports._store = Store()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://test",
                headers={"X-AL1S-CSRF": "1"},
            ) as client:
                login = await client.post(
                    "/api/v1/auth/login", json={"password": "image-test-password"}
                )
                assert login.status_code == 200
                start = await client.post(
                    "/api/v1/maa/editor/scripts",
                    headers={"Idempotency-Key": request_prefix + "-start"},
                    json={"device_id": str(phone_id), "name": "Image start"},
                )
                assert start.status_code == 201, start.text
                start_record = start.json()
                assert start_record["package_name"] == "com.example.image"
                assert start_record["script_type"] == "module_start"
                application_id = start_record["application_id"]
                process = await client.post(
                    "/api/v1/maa/editor/scripts",
                    headers={"Idempotency-Key": request_prefix + "-process"},
                    json={"device_id": str(phone_id), "name": "Image process"},
                )
                assert process.status_code == 201, process.text
                process_record = process.json()
                assert process_record["script_type"] == "module_process"
                script_id = process_record["script_id"]
                script = await client.get(f"/api/v1/maa/scripts/{script_id}")
                assert script.status_code == 200, script.text
                record = script.json()
                assert record["script_id"] == script_id
                manifest = {
                    "version": 2,
                    "script_type": "module_process",
                    "cleanup_on_finish": False,
                    "steps": [{"action": "wait", "seconds": 1}],
                    "target": {"application_package": "com.example.image"},
                }
                output = BytesIO()
                Image.new("RGB", (8, 6), "blue").save(output, format="PNG")
                upload = await client.post(
                    f"/api/v1/maa/scripts/{script_id}/images",
                    content=output.getvalue(),
                    headers={"Content-Type": "image/png"},
                )
                assert upload.status_code == 200, upload.text
                resource = upload.json()["resource"]
                again = await client.post(
                    f"/api/v1/maa/scripts/{script_id}/images",
                    content=output.getvalue(),
                    headers={"Content-Type": "image/png"},
                )
                assert again.json() == upload.json()
                manifest["target"]["screen_size"] = {"width": 1080, "height": 2400}
                manifest["steps"] = [{"action": "wait_image", "template_base64": resource}]
                saved = await client.put(
                    f"/api/v1/maa/scripts/{script_id}/document",
                    headers={
                        "Idempotency-Key": request_prefix + "-version",
                        "If-Match": str(record["row_version"]),
                    },
                    json={"manifest": manifest, "device_id": str(phone_id)},
                )
                assert saved.status_code == 200, saved.text
                saved_record = saved.json()
                version_id = saved_record["current_version_id"]
                assert version_id is not None
                reopened = await client.get(
                    f"/api/v1/maa/scripts/{script_id}/versions/{version_id}"
                )
                assert reopened.status_code == 200, reopened.text
                assert reopened.json()["manifest"] == manifest
                image_url = (
                    f"/api/v1/maa/scripts/{script_id}/versions/{version_id}"
                    f"/images/{resource['$blob']}"
                )
                preview = await client.get(image_url)
                assert preview.status_code == 200, preview.text
                assert preview.content == output.getvalue()
                assert preview.headers["cache-control"] == "no-store"
                check_url = f"/api/v1/maa/scripts/{script_id}/static-check"
                checked = await client.post(
                    check_url,
                    headers={"Idempotency-Key": request_prefix + "-static"},
                    json={"candidate_version_id": version_id},
                )
                assert checked.status_code == 200, checked.text
                assert checked.json()["status"] == "passed"
                export_url = f"/api/v1/maa/scripts/{script_id}/versions/{version_id}/archive"
                exported = await client.get(export_url)
                assert exported.status_code == 200, exported.text
                assert exported.headers["cache-control"] == "no-store"
                from al1s.maa.archive import parse_script_archive

                portable = parse_script_archive(exported.content)
                assert portable.schema == "al1s-script-archive/v2"
                assert portable.scripts[0].source_script_id == script_id
                assert portable.scripts[0].document["target"]["screen_size"] == {
                    "width": 1080,
                    "height": 2400,
                }
                assert next(iter(portable.resources.values())).body == output.getvalue()
                missing = await client.get(image_url.rsplit("/", 1)[0] + f"/{uuid4()}")
                assert missing.status_code == 404
                from tests.maa_archive_fixture import FixtureScript, build_archive

                archive_body = build_archive(
                    FixtureScript(
                        "Browser import " + request_prefix,
                        {
                            "version": 2,
                            "script_type": "module_process",
                            "cleanup_on_finish": False,
                            "steps": [{"action": "wait", "seconds": 1}],
                        },
                        package_name="com.example.image",
                    )
                )
                selection = {
                    "X-AL1S-Import-Selection": json.dumps(
                        {"target_applications": {"com.example.image": application_id}}
                    )
                }
                imported = await client.post(
                    "/api/v1/maa/imports/upload",
                    content=archive_body,
                    headers={"Content-Type": "application/zip", **selection},
                )
                assert imported.status_code == 201, imported.text
                replay = await client.post(
                    "/api/v1/maa/imports/upload",
                    content=archive_body,
                    headers={"Content-Type": "application/zip", **selection},
                )
                assert replay.status_code == 200, replay.text
                assert replay.json()["batch_id"] == imported.json()["batch_id"]
                items = await client.get(f"/api/v1/maa/imports/{imported.json()['batch_id']}/items")
                assert items.status_code == 200, items.text
                assert items.json()["items"][0]["script_id"] is not None
                client.cookies.clear()
                assert (await client.get(export_url)).status_code == 401
                assert (
                    await client.post(check_url, json={"candidate_version_id": version_id})
                ).status_code == 401
                assert (await client.get(image_url)).status_code == 401
                assert (
                    await client.post(
                        "/api/v1/maa/imports/upload",
                        content=archive_body,
                        headers={"Content-Type": "application/zip"},
                    )
                ).status_code == 401
                return UUID(resource["$blob"])

    try:
        blob_id = asyncio.run(exercise())
        worker = GcWorker(
            uow_factory=lambda: SqlAlchemyUnitOfWork(sessionmaker(engine)),
            blob_store=Store(),
            worker_id="image-test",
            now=lambda: datetime.now(UTC) + timedelta(days=2),
        )
        assert worker.run_once().claimed == 0
        assert objects
        with Session(engine) as session:
            assert (
                session.scalar(select(BlobObjectRow.status).where(BlobObjectRow.id == blob_id))
                == "ready"
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE maa_applications, blob_objects CASCADE"))
            connection.execute(
                text("DELETE FROM target_devices WHERE id = :device_id"),
                {"device_id": phone_id},
            )
            connection.execute(
                text("DELETE FROM terminals WHERE id = :terminal_id"),
                {"terminal_id": terminal_id},
            )
        engine.dispose()
