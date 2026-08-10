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
    try:
        host = os.environ["SMTP_HOST"]
    except KeyError:
        raise RuntimeError(
            "SMTP_HOST is not set and RESEND_API_KEY is absent, so there is no way "
            "to send the digest. For Gmail set SMTP_HOST=smtp.gmail.com, SMTP_PORT=465, "
            "SMTP_USER=<your gmail address> and SMTP_PASS=<a Google App Password>."
        ) from None

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    # Plain text must come first: mail clients render the *last* part they
    # can display, so HTML has to be attached second to win.
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        # Port 465 is implicit TLS (SMTPS); 587 is plaintext + STARTTLS.
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=60)
        else:
            server = smtplib.SMTP(host, port, timeout=60)
        with server:
            if port != 465:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            f"SMTP login rejected for {user} on {host}:{port} ({exc.smtp_code}). "
            "For Gmail this almost always means SMTP_PASS is a normal account "
            "password rather than a 16-character App Password — generate one at "
            "https://myaccount.google.com/apppasswords (2-Step Verification must "
            "be on first)."
        ) from exc

    logger.info("Sent digest via SMTP (%s:%d) to %s", host, port, to_addr)


def send_email(subject: str, html_body: str, text_body: str) -> None:
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get("EMAIL_FROM")

    if not to_addr:
        raise RuntimeError("EMAIL_TO is not set; cannot send the digest")

    if not from_addr:
        # Gmail rejects a From address that isn't the authenticated account,
        # so default to the SMTP user rather than a placeholder domain.
        smtp_user = os.environ.get("SMTP_USER")
        from_addr = f"Mag <{smtp_user}>" if smtp_user else "Mag <mag@example.com>"

    if os.environ.get("RESEND_API_KEY"):
        _send_via_resend(subject, html_body, text_body, to_addr, from_addr)
    else:
        _send_via_smtp(subject, html_body, text_body, to_addr, from_addr)
