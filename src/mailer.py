"""
Delivery for the Mag digest: Resend's HTTP API when RESEND_API_KEY is set,
otherwise SMTP.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

logger = logging.getLogger("mag.mailer")

RESEND_API_URL = "https://api.resend.com/emails"


def _send_via_resend(subject: str, html_body: str, text_body: str, to_addr: str, from_addr: str) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    resp = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        },
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Sent digest via Resend to %s", to_addr)


def _send_via_smtp(subject: str, html_body: str, text_body: str, to_addr: str, from_addr: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    logger.info("Sent digest via SMTP (%s) to %s", host, to_addr)


def send_email(subject: str, html_body: str, text_body: str) -> None:
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM", "Mag <mag@example.com>")

    if not to_addr:
        raise RuntimeError("EMAIL_TO is not set; cannot send the digest")

    if os.environ.get("RESEND_API_KEY"):
        _send_via_resend(subject, html_body, text_body, to_addr, from_addr)
    else:
        _send_via_smtp(subject, html_body, text_body, to_addr, from_addr)
