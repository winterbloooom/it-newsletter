"""SMTP delivery.

`verify_auth` exists so the daily run can fail on a bad password before it
spends any Gemini quota. On the free tier a wasted run is a wasted day.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from it_newsletter.models import EmailConfig

logger = logging.getLogger(__name__)


def _connect(config: EmailConfig, password: str) -> smtplib.SMTP:
    server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
    server.starttls()
    server.login(config.sender_address, password)
    return server


def verify_auth(config: EmailConfig, password: str) -> None:
    """Authenticate and disconnect, without sending anything."""
    missing = [
        name for name, value in
        (("MAIL_SENDER", config.sender_address), ("MAIL_RECIPIENTS", config.recipients))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} not set. Copy .env.example to .env and fill "
            f"them in, or set them as repository secrets in CI."
        )
    logger.info("Verifying SMTP auth on %s:%d as %s",
                config.smtp_host, config.smtp_port, config.sender_address)
    with _connect(config, password):
        pass
    logger.info("SMTP auth OK")


def send(
    config: EmailConfig,
    password: str,
    *,
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    """Send one multipart message. The plain-text part is not a courtesy: it is
    what a reader sees when images and styles are blocked."""
    message = MIMEMultipart("alternative")
    message["From"] = f"{config.sender_name} <{config.sender_address}>"
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = subject
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Sending to %s", config.recipients)
    with _connect(config, password) as server:
        server.sendmail(config.sender_address, config.recipients, message.as_string())
    logger.info("Email sent")
