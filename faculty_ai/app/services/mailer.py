"""Minimal transactional email sender.

If SMTP_HOST is not set (the default, out of the box), emails are not sent — the content is written
to the console/log instead, so the reset flow still works end-to-end during local development and
testing without needing a real mail server. Set SMTP_* in .env to send real emails (Gmail with an
App Password, Outlook, or any SMTP provider all work with smtplib as-is).
"""
import logging
import smtplib
from email.message import EmailMessage

from ..config import get_settings

logger = logging.getLogger("faculty_ai.mail")


def send_email(to: str, subject: str, body: str) -> bool:
    """Returns True if actually sent over SMTP, False if it fell back to the console log."""
    s = get_settings()
    if not s.smtp_host:
        logger.info("SMTP not configured — email NOT sent. To: %s | Subject: %s\n%s", to, subject, body)
        print(f"\n[dev email — SMTP not configured]\nTo: {to}\nSubject: {subject}\n{body}\n")
        return False

    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as server:
        if s.smtp_use_tls:
            server.starttls()
        if s.smtp_user:
            server.login(s.smtp_user, s.smtp_password)
        server.send_message(msg)
    return True
