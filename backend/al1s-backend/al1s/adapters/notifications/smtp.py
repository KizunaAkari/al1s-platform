from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from contextlib import suppress
from email.message import EmailMessage
from typing import Any

from al1s.notifications.errors import NotificationAdapterError
from al1s.notifications.types import AdapterReceipt, NotificationMessage

SmtpFactory = Callable[..., smtplib.SMTP]


class SmtpNotificationAdapter:
    def __init__(
        self,
        *,
        smtp_factory: SmtpFactory = smtplib.SMTP,
        smtp_ssl_factory: SmtpFactory = smtplib.SMTP_SSL,
    ) -> None:
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory

    def send(self, message: NotificationMessage, *, idempotency_key: str) -> AdapterReceipt:
        del idempotency_key  # SMTP has no provider idempotency primitive.
        settings = message.channel_settings
        host = _required_string(settings, "host")
        port = _required_int(settings, "port", minimum=1, maximum=65535)
        sender = _required_string(settings, "from_address")
        security = _required_string(settings, "security")
        timeout = _optional_number(settings, "timeout_seconds", default=20.0)
        if not 1 <= timeout <= 60:
            raise NotificationAdapterError("smtp_timeout_invalid", retryable=False)
        if security not in {"plain", "starttls", "ssl"}:
            raise NotificationAdapterError("smtp_security_invalid", retryable=False)
        recipients = tuple(target.strip() for target in message.targets if "@" in target)
        if len(recipients) != len(message.targets) or not recipients:
            raise NotificationAdapterError("smtp_recipient_invalid", retryable=False)
        username = settings.get("username")
        if username is not None and not isinstance(username, str):
            raise NotificationAdapterError("smtp_username_invalid", retryable=False)
        normalized_username = username.strip() if isinstance(username, str) else ""
        if normalized_username and not message.secret:
            raise NotificationAdapterError("smtp_password_missing", retryable=False)

        email = EmailMessage()
        email["Subject"] = message.title
        email["From"] = sender
        email["To"] = ", ".join(recipients)
        email["Message-ID"] = f"<{message.delivery_id}@al1s.local>"
        email.set_content(message.body)

        smtp: smtplib.SMTP | None = None
        sending = False
        try:
            factory = self._smtp_ssl_factory if security == "ssl" else self._smtp_factory
            smtp = (
                factory(host, port, timeout=timeout, context=ssl.create_default_context())
                if security == "ssl"
                else factory(host, port, timeout=timeout)
            )
            if security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if normalized_username:
                smtp.login(normalized_username, message.secret or "")
            sending = True
            rejected = smtp.send_message(email)
            if rejected:
                raise NotificationAdapterError("smtp_recipient_rejected", retryable=False)
            return AdapterReceipt(provider_message_id=str(email["Message-ID"]))
        except NotificationAdapterError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise NotificationAdapterError("smtp_authentication_failed", retryable=False) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            temporary = bool(exc.recipients) and all(
                isinstance(value, tuple)
                and len(value) == 2
                and isinstance(value[0], int)
                and 400 <= value[0] < 500
                for value in exc.recipients.values()
            )
            raise NotificationAdapterError("smtp_recipient_rejected", retryable=temporary) from exc
        except smtplib.SMTPSenderRefused as exc:
            raise NotificationAdapterError("smtp_sender_rejected", retryable=False) from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise NotificationAdapterError("smtp_feature_unsupported", retryable=False) from exc
        except smtplib.SMTPResponseException as exc:
            retryable = 400 <= exc.smtp_code < 500
            raise NotificationAdapterError("smtp_provider_rejected", retryable=retryable) from exc
        except ssl.SSLCertVerificationError as exc:
            raise NotificationAdapterError("smtp_certificate_invalid", retryable=False) from exc
        except (TimeoutError, OSError, smtplib.SMTPServerDisconnected) as exc:
            code = "smtp_delivery_unknown" if sending else "smtp_connection_failed"
            raise NotificationAdapterError(code, retryable=True) from exc
        except smtplib.SMTPException as exc:
            raise NotificationAdapterError("smtp_protocol_failed", retryable=True) from exc
        finally:
            if smtp is not None:
                _close_quietly(smtp)


def _required_string(settings: dict[str, Any], key: str) -> str:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NotificationAdapterError(f"smtp_{key}_invalid", retryable=False)
    return value.strip()


def _required_int(settings: dict[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    value = settings.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise NotificationAdapterError(f"smtp_{key}_invalid", retryable=False)
    return value


def _optional_number(settings: dict[str, Any], key: str, *, default: float) -> float:
    value = settings.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise NotificationAdapterError(f"smtp_{key}_invalid", retryable=False)
    return float(value)


def _close_quietly(smtp: smtplib.SMTP) -> None:
    try:
        smtp.quit()
    except (OSError, smtplib.SMTPException):
        # Failure closing an already accepted email must not trigger another send.
        with suppress(OSError, smtplib.SMTPException):
            smtp.close()
