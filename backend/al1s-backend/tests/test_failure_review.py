from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from al1s.execution.failure_details import definitely_no_screenshot, failure_details
from al1s.execution.failure_review_service import FailureReviewService
from al1s.execution.recording_download import RecordingFile


def test_projection_excludes_arbitrary_fields_and_does_not_invent_no_image():
    image = uuid4()
    data = {"modules": [{"module_index": 3, "definition_key": "script",
        "result": {"failed_step": {"number": 2}, "failure_diagnosis": {
            "title": "recognition", "message": "not matched", "secret": "never returned"},
            "failure_screenshot": {"artifact_id": str(image), "path": "/secret/path"}}}]}
    detail, = failure_details(data)
    assert detail.module_number == 3 and detail.step_number == 2
    assert detail.screenshot_id == image
    assert "secret" not in str(detail)
    assert not definitely_no_screenshot(data)
    assert not definitely_no_screenshot(None)
    assert definitely_no_screenshot({"failure_screenshot_error": "capture failed"})


class FakeReview:
    def __init__(self, body):
        self.body = body
        self.receipts = 0
        self.item = RecordingFile("test.png", sha256(body).hexdigest(), len(body), "internal")

    def metadata(self, *_):
        return self.item

    def downloaded(self, *_):
        self.receipts += 1

    def get_range(self, key, start, end):
        return self.body[start:end + 1]


def test_stream_complete_only_download_receipt_and_preview_unchanged():
    fake = FakeReview(b"original-image")
    service = FailureReviewService(fake, fake)
    ids = (uuid4(), uuid4(), uuid4())
    assert b"".join(service.stream(*ids, fake.item, preview=True)) == fake.body
    assert fake.receipts == 0
    stream = service.stream(*ids, fake.item)
    next(stream)
    stream.close()
    assert fake.receipts == 0
    assert b"".join(service.stream(*ids, fake.item)) == fake.body
    assert fake.receipts == 1


def test_corruption_never_unlocks_confirmation():
    fake = FakeReview(b"original-image")
    fake.body = b"corrupted-data"
    service = FailureReviewService(fake, fake, lambda: datetime.now(UTC))
    with pytest.raises(RuntimeError):
        list(service.stream(uuid4(), uuid4(), uuid4(), fake.item))
    assert fake.receipts == 0
