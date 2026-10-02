from __future__ import annotations

import smtplib
import ssl
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.adapters.notifications.smtp import SmtpNotificationAdapter
from al1s.notifications.errors import NotificationAdapterError
from al1s.notifications.types import NotificationKind, NotificationMessage


def _message(
    *, security: str = "plain", secret: str | None = "app-password"
) -> NotificationMessage:
    return NotificationMessage(
        delivery_id=uuid4(),
        kind=NotificationKind.SCRIPT_FAILURE,
        targets=("owner@example.test",),
        template_key="script_failure.v1",
        title="AL-1S failure",
        body="Open the task center for details.",
        payload={},
        channel_settings={
            "host": "smtp.example.test",
            "port": 465 if security == "ssl" else 587,
            "from_address": "al1s@example.test",
            "security": security,
            "username": "al1s@example.test",
            "timeout_seconds": 12,
        },
        bot_service_id=None,
        secret=secret,
    )


@pytest.mark.parametrize("security", ["plain", "starttls", "ssl"])
def test_adapter_uses_selected_transport_and_sends_message(security: str) -> None:
    smtp = MagicMock(spec=smtplib.SMTP)
    smtp.send_message.return_value = {}
    plain_factory = MagicMock(return_value=smtp)
    ssl_factory = MagicMock(return_value=smtp)
    adapter = SmtpNotificationAdapter(
        smtp_factory=plain_factory,
        smtp_ssl_factory=ssl_factory,
    )

    adapter.send(_message(security=security), idempotency_key="delivery-1")

    selected = ssl_factory if security == "ssl" else plain_factory
    other = plain_factory if security == "ssl" else ssl_factory
    assert selected.call_count == 1
    assert selected.call_args.args == ("smtp.example.test", 465 if security == "ssl" else 587)
    assert selected.call_args.kwargs["timeout"] == 12.0
    if security == "ssl":
        context = selected.call_args.kwargs["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    other.assert_not_called()
    assert smtp.ehlo.call_count == (2 if security == "starttls" else 0)
    assert smtp.starttls.call_count == (1 if security == "starttls" else 0)
    smtp.login.assert_called_once_with("al1s@example.test", "app-password")
    sent = smtp.send_message.call_args.args[0]
    assert sent["To"] == "owner@example.test"
    assert sent["Subject"] == "AL-1S failure"
    smtp.quit.assert_called_once_with()


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            smtplib.SMTPAuthenticationError(535, b"private response"),
            "smtp_authentication_failed",
            False,
        ),
        (smtplib.SMTPDataError(451, b"private response"), "smtp_provider_rejected", True),
        (smtplib.SMTPDataError(550, b"private response"), "smtp_provider_rejected", False),
        (smtplib.SMTPRecipientsRefused({}), "smtp_recipient_rejected", False),
        (
            smtplib.SMTPRecipientsRefused({"owner@example.test": (450, b"busy")}),
            "smtp_recipient_rejected",
            True,
        ),
        (TimeoutError("private response"), "smtp_delivery_unknown", True),
    ],
)
def test_adapter_classifies_provider_errors_without_exposing_response(
    error: Exception, code: str, retryable: bool
) -> None:
    smtp = MagicMock(spec=smtplib.SMTP)
    smtp.send_message.side_effect = error
    adapter = SmtpNotificationAdapter(smtp_factory=MagicMock(return_value=smtp))

    with pytest.raises(NotificationAdapterError) as raised:
        adapter.send(_message(), idempotency_key="delivery-1")

    assert raised.value.code == code
    assert raised.value.retryable is retryable
    assert "private response" not in str(raised.value)


def test_adapter_refuses_username_without_password_before_connecting() -> None:
    factory = MagicMock()
    adapter = SmtpNotificationAdapter(smtp_factory=factory)

    with pytest.raises(NotificationAdapterError) as raised:
        adapter.send(_message(secret=None), idempotency_key="delivery-1")

    assert raised.value.code == "smtp_password_missing"
    assert raised.value.retryable is False
    factory.assert_not_called()


def test_connect_timeout_is_not_marked_as_possible_delivery():
    adapter = SmtpNotificationAdapter(smtp_factory=MagicMock(side_effect=TimeoutError()))
    with pytest.raises(NotificationAdapterError) as error:
        adapter.send(_message(), idempotency_key="delivery")
    assert error.value.code == "smtp_connection_failed"


def test_message_id_is_stable_across_retries_but_not_a_deduplication_claim():
    smtp = MagicMock(spec=smtplib.SMTP)
    smtp.send_message.return_value = {}
    adapter = SmtpNotificationAdapter(smtp_factory=MagicMock(return_value=smtp))
    message = _message()
    first = adapter.send(message, idempotency_key="first")
    second = adapter.send(message, idempotency_key="retry")
    assert first.provider_message_id == second.provider_message_id
    assert smtp.send_message.call_count == 2


def test_cleanup_failure_does_not_discard_successful_acceptance():
    smtp = MagicMock(spec=smtplib.SMTP)
    smtp.send_message.return_value = {}
    smtp.quit.side_effect = OSError()
    smtp.close.side_effect = OSError()
    adapter = SmtpNotificationAdapter(smtp_factory=MagicMock(return_value=smtp))
    assert adapter.send(_message(), idempotency_key="delivery").provider_message_id
