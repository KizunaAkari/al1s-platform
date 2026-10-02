from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from al1s.api.editor_sessions import EditorReport
from al1s.execution.editor_sessions import (
    EditorSession,
    EditorStatus,
    open_connection,
    report_session,
    request_close,
    validate_connection,
)
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.secrets.security import FernetSecretCipher, SecretUnavailableError


def sample():
    now = datetime.now(UTC)
    record = EditorSession(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        EditorStatus.PENDING,
        now,
        now + timedelta(seconds=30),
        now,
    )
    token = "a" * 40
    connection = {
        "transport": "scrcpy-managed-v1",
        "scrcpy_version": "3.3.4",
        "session_token": token,
        "video_ws_url": f"wss://terminal/scrcpy/{token}/video",
        "control_ws_url": f"wss://terminal/scrcpy/{token}/control",
    }
    return record, connection, FernetSecretCipher("test-key-" * 8), uuid4(), now


def test_native_screenshot_capability_is_optional_and_encrypted():
    record, connection, cipher, instance, now = sample()
    validate_connection(connection)  # Existing terminals remain compatible.
    connection["screenshot_ws_url"] = (
        connection["video_ws_url"].removesuffix("video") + "screenshot"
    )
    active = report_session(
        record, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
    )
    assert open_connection(active, cipher) == connection
    report = EditorReport(instance_id=instance, status="active", connection=connection)
    assert report.connection == connection


def test_editor_report_accepts_all_eight_connection_fields():
    record, connection, cipher, instance, now = sample()
    base = connection["video_ws_url"].removesuffix("video")
    connection.update({
        "screenshot_ws_url": base + "screenshot",
        "foreground_ws_url": base + "foreground",
        "app_icon_ws_url": base + "app-icon",
    })
    assert len(connection) == 8
    report = EditorReport(instance_id=instance, status="active", connection=connection)
    assert report.connection == connection
    active = report_session(
        record, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
    )
    assert open_connection(active, cipher) == connection


@pytest.mark.parametrize("url", [
    "wss://other/scrcpy/" + "a" * 40 + "/screenshot",
    "wss://terminal/scrcpy/wrong-token/screenshot",
    "wss://terminal/scrcpy/" + "a" * 40 + "/control",
])
def test_native_screenshot_rejects_mismatched_origin_identity_and_path(url):
    _, connection, _, _, _ = sample()
    connection["screenshot_ws_url"] = url
    with pytest.raises(InvalidRequestError):
        validate_connection(connection)


def test_activation_encrypts_and_replays_without_token_replacement():
    record, connection, cipher, instance, now = sample()
    active = report_session(
        record, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
    )
    assert active.ciphertext and connection["session_token"] not in active.ciphertext
    assert open_connection(active, cipher) == connection
    assert (
        report_session(
            active, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
        )
        == active
    )
    with pytest.raises(ConflictError):
        report_session(
            active, record.terminal_id, uuid4(), EditorStatus.ACTIVE, now, cipher, connection
        )


def test_deadline_and_cancellation_never_reactivate():
    record, connection, cipher, instance, now = sample()
    expired = report_session(
        record,
        record.terminal_id,
        instance,
        EditorStatus.ACTIVE,
        record.create_deadline,
        cipher,
        connection,
    )
    assert expired.status is EditorStatus.EXPIRED and expired.ciphertext is None
    closed = request_close(record, now)
    assert (
        report_session(
            closed, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
        )
        == closed
    )


def test_close_hides_credentials_immediately_then_erases_on_confirmation():
    record, connection, cipher, instance, now = sample()
    active = report_session(
        record, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
    )
    closing = request_close(active, now)
    assert closing.status is EditorStatus.CLOSING and open_connection(closing, cipher) is None
    assert (
        report_session(
            closing, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
        )
        == closing
    )
    closed = report_session(closing, record.terminal_id, instance, EditorStatus.CLOSED, now, cipher)
    assert closed.ciphertext is None and closed.key_id is None


def test_other_terminal_and_ciphertext_misassociation_are_rejected():
    record, connection, cipher, instance, now = sample()
    with pytest.raises(NotFoundError):
        report_session(record, uuid4(), instance, EditorStatus.ACTIVE, now, cipher, connection)
    active = report_session(
        record, record.terminal_id, instance, EditorStatus.ACTIVE, now, cipher, connection
    )
    with pytest.raises(SecretUnavailableError):
        open_connection(replace(active, device_id=uuid4()), cipher)


@pytest.mark.parametrize(
    "field,value",
    [
        ("video_ws_url", "file:///secret"),
        ("video_ws_url", "ws://user:pass@terminal/video"),
        ("control_ws_url", "wss://another/scrcpy/" + "a" * 40 + "/control"),
        ("session_token", "short"),
        ("transport", "adb"),
        ("scrcpy_version", "unknown"),
    ],
)
def test_untrusted_connection_fields_are_rejected(field, value):
    _, connection, _, _, _ = sample()
    connection[field] = value
    with pytest.raises(InvalidRequestError):
        validate_connection(connection)
