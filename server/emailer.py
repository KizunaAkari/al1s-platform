import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from .config import settings


@dataclass(frozen=True)
class SMTPConfiguration:
    host: str
    port: int
    user: str
    password: str
    from_address: str
    starttls: bool
    ssl: bool
    failure_recipients: tuple[str, ...]
    failure_enabled: bool
    public_base_url: str
    source: str = "environment"
    updated_at: str | None = None

    @property
    def security(self) -> str:
        if self.ssl:
            return "ssl"
        if self.starttls:
            return "starttls"
        return "none"


def environment_configuration() -> SMTPConfiguration:
    return SMTPConfiguration(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        password=settings.smtp_password,
        from_address=settings.smtp_from,
        starttls=settings.smtp_starttls,
        ssl=settings.smtp_ssl,
        failure_recipients=settings.failure_email_to,
        failure_enabled=bool(settings.failure_email_to),
        public_base_url=settings.public_base_url,
    )


def smtp_configured(configuration: SMTPConfiguration | None = None) -> bool:
    active = configuration or environment_configuration()
    return bool(active.host and active.from_address)


def failure_notifications_configured(configuration: SMTPConfiguration | None = None) -> bool:
    active = configuration or environment_configuration()
    return active.failure_enabled and smtp_configured(active) and bool(active.failure_recipients)


def send_message(
    recipients: Iterable[str],
    subject: str,
    body: str,
    *,
    html: bool = False,
    attachment: Path | None = None,
    attachment_mime: str = "image/png",
    configuration: SMTPConfiguration | None = None,
) -> list[str]:
    active = configuration or environment_configuration()
    target = [item.strip() for item in recipients if item.strip()]
    if not smtp_configured(active):
        raise RuntimeError("未配置 SMTP 服务器和发件地址")
    if not target:
        raise RuntimeError("邮件收件人为空")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = active.from_address
    message["To"] = ", ".join(target)
    if html:
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)

    if attachment and attachment.is_file():
        main_type, sub_type = (attachment_mime.split("/", 1) + ["octet-stream"])[:2]
        message.add_attachment(
            attachment.read_bytes(),
            maintype=main_type,
            subtype=sub_type,
            filename=attachment.name,
        )

    smtp_type = smtplib.SMTP_SSL if active.ssl else smtplib.SMTP
    with smtp_type(active.host, active.port, timeout=20) as smtp:
        if active.starttls and not active.ssl:
            smtp.starttls()
        if active.user:
            smtp.login(active.user, active.password)
        smtp.send_message(message)
    return target


def send_failure_notification(
    failure: dict,
    screenshot: Path | None = None,
    configuration: SMTPConfiguration | None = None,
) -> list[str]:
    active = configuration or environment_configuration()
    failed_number = failure.get("failed_step_index")
    step_label = "初始化/未知位置" if failed_number is None else (
        f"第 {int(failed_number) + 1} 步（{failure.get('failed_step_action') or 'unknown'}）"
    )
    failure_url = f"{active.public_base_url}/?failure={failure['id']}"
    body = "\n".join([
        "MAA 自动化脚本执行失败。",
        "",
        f"脚本：{failure.get('script_name') or '未命名脚本'}",
        f"终端：{failure.get('agent_id')}",
        f"失败位置：{step_label}",
        f"错误：{failure.get('error')}",
        f"发生时间：{failure.get('created_at')}",
        f"失败记录：{failure_url}",
        "",
        "如有现场截图，已作为附件随邮件发送。",
    ])
    return send_message(
        active.failure_recipients,
        f"[MAA] 脚本失败：{failure.get('script_name') or failure.get('agent_id')}",
        body,
        attachment=screenshot,
        attachment_mime=str(failure.get("screenshot_mime") or "image/png"),
        configuration=active,
    )
