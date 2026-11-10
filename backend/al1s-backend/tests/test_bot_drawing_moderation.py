import asyncio
import math
import threading
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
import pytest

from al1s.bot_drawing.moderation import MODEL_REVISION, POLICY_VERSION, WDAuditor, decide


def png():
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, "PNG")
    return output.getvalue()


def test_green_allows_general_or_sensitive_and_blocks_r18_or_ties():
    assert decide({"general": 0.95, "sensitive": 0.02, "questionable": 0.02, "explicit": 0.01}) == "allow"
    assert decide({"general": 0.70, "sensitive": 0.10, "questionable": 0.10, "explicit": 0.10}) == "allow"
    assert decide({"general": 0.812, "sensitive": 0.191, "questionable": 0.0028, "explicit": 0.00029}) == "allow"
    assert decide({"general": 0.30, "sensitive": 0.60, "questionable": 0.05, "explicit": 0.05}) == "allow"
    assert decide({"general": 0.478, "sensitive": 0.553, "questionable": 0.0047, "explicit": 0.0012}) == "allow"
    assert decide({"general": 0.30, "sensitive": 0.05, "questionable": 0.60, "explicit": 0.05}) == "block"
    assert decide({"general": 0.02, "sensitive": 0.03, "questionable": 0.05, "explicit": 0.90}) == "block"
    assert decide({"general": 0.95, "sensitive": 0.95, "questionable": 0.01, "explicit": 0.01}) == "allow"
    assert decide({"general": 0.2, "sensitive": 0.6, "questionable": 0.6, "explicit": 0.1}) == "block"
    assert decide({"general": math.nan, "sensitive": 0, "questionable": 0, "explicit": 0}) == "unknown"
    assert decide({"general": 1.2, "sensitive": 0, "questionable": 0, "explicit": 0}) == "unknown"
    assert decide({"general": 0.99}) == "unknown"


def test_stricter_v1_and_v2_approved_previews_remain_visible_under_new_policy():
    from datetime import UTC, datetime, timedelta
    from al1s.adapters.postgres.bot_drawing_repository import _preview_visible

    assert POLICY_VERSION == 'wd14-green-v3'
    expires = datetime.now(UTC) + timedelta(hours=1)
    digest = 'a' * 64
    assert _preview_visible('admin_preview', 'reference', expires, 'green', 'allow', digest, digest,
                            MODEL_REVISION, 'wd14-green-v1')
    assert _preview_visible('admin_preview', 'reference', expires, 'green', 'allow', digest, digest,
                            MODEL_REVISION, 'wd14-green-v2')
    assert not _preview_visible('admin_preview', 'reference', expires, 'green', 'unknown', digest, digest,
                                MODEL_REVISION, 'wd14-green-v1')
    assert not _preview_visible('admin_preview', 'reference', expires, 'green', 'allow', digest, digest,
                                'different-model', 'wd14-green-v1')


def test_preview_download_accepts_only_valid_old_approval():
    from datetime import UTC, datetime, timedelta
    from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository

    service, task_id = uuid4(), uuid4()
    digest = 'a' * 64
    row = SimpleNamespace(service_id=service, origin='admin_preview', deletion_state='visible', media_expires_at=datetime.now(UTC) + timedelta(hours=1),
        generation_status='succeeded', output_reference='reference', output_sha256=digest, audit_mode='green',
        audit_status='allow', audit_sha256=digest, audit_model_revision=MODEL_REVISION, audit_policy_version='wd14-green-v1')
    class Session:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, _model, identity): return row if identity == task_id else None
    repo = BotDrawingRepository(lambda: Session())
    assert repo.preview_media(service, task_id) == ('reference', digest)
    row.audit_policy_version = 'wd14-green-v2'
    assert repo.preview_media(service, task_id) == ('reference', digest)
    row.audit_status = 'unknown'
    with pytest.raises(ValueError, match='drawing_media_unavailable'):
        repo.preview_media(service, task_id)


def test_failed_audit_is_unknown_and_cannot_accidentally_allow(monkeypatch, tmp_path):
    async def run():
        auditor = WDAuditor(tmp_path, timeout=1)
        def fail(_body):
            raise ValueError("broken model")
        monkeypatch.setattr(auditor, "_infer", fail)
        result = await auditor.audit(png())
        assert result.status == "unknown" and len(result.image_sha256) == 64
        auditor.close()
    asyncio.run(run())


def test_timeout_does_not_start_a_second_native_inference(monkeypatch, tmp_path):
    async def run():
        auditor = WDAuditor(tmp_path, timeout=0.02)
        started, release, calls = threading.Event(), threading.Event(), []
        def slow(_body):
            calls.append(1)
            started.set()
            release.wait(2)
            return {"general": 0.99, "sensitive": 0, "questionable": 0, "explicit": 0}
        monkeypatch.setattr(auditor, "_infer", slow)
        first = await auditor.audit(png())
        assert started.is_set() and first.status == "unknown"
        second = await auditor.audit(png())
        assert second.status == "unknown" and len(calls) == 1
        release.set()
        await asyncio.sleep(0.05)
        auditor.close()
    asyncio.run(run())


def test_gif_audits_every_frame_and_blocks_when_any_frame_fails(monkeypatch, tmp_path):
    import numpy as np

    output = BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "green", "blue")]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    calls = []

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="input", shape=[1, 8, 8, 3])]
        def run(self, _outputs, inputs):
            calls.append(inputs["input"])
            return [np.array([[0.1, 0.0, 0.9, 0.0] if len(calls) == 2
                              else [0.9, 0.0, 0.1, 0.0]], dtype=np.float32)]

    async def run():
        auditor = WDAuditor(tmp_path, timeout=1)
        auditor._session = Session()
        auditor._rating_indices = {"general": 0, "sensitive": 1, "questionable": 2, "explicit": 3}
        decision = await auditor.audit(output.getvalue())
        assert decision.status == "block"
        assert len(calls) == 3
        auditor.close()

    asyncio.run(run())
