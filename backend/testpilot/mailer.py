"""Outgoing e-mail through the SMTP server configured in Platform Settings (password encrypted at rest)."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy.orm import Session

from . import settings_store

log = logging.getLogger("testpilot.mail")


class MailError(Exception):
    pass


def configured(db: Session) -> bool:
    c = settings_store.get(db, "smtp")
    return bool(c["enabled"] and c["host"] and c["from_address"])


def _connect(c: dict) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if c["security"] == "ssl":
        s = smtplib.SMTP_SSL(c["host"], int(c["port"]), timeout=15, context=ctx)
    else:
        s = smtplib.SMTP(c["host"], int(c["port"]), timeout=15)
        if c["security"] == "starttls":
            s.starttls(context=ctx)
    if c["username"]:
        s.login(c["username"], settings_store.unseal(c["password_enc"]))
    return s


def send(db: Session, to: str, subject: str, text: str, *, cfg: dict | None = None) -> None:
    c = cfg or settings_store.get(db, "smtp")
    if not (c["host"] and c["from_address"]):
        raise MailError("SMTP is not configured")
    msg = EmailMessage()
    msg["From"] = formataddr((c["from_name"] or "TestAgent", c["from_address"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=c["from_address"].split("@")[-1])
    msg.set_content(text)
    try:
        with _connect(c) as s:
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.warning("e-mail to %s failed: %s", to.split("@")[-1], type(e).__name__)
        raise MailError(f"{type(e).__name__}: {str(e)[:200]}") from e


def try_send(db: Session, to: str, subject: str, text: str) -> bool:
    """Send if SMTP is enabled; never raises (callers show the link on screen as a fallback)."""
    if not configured(db) or not to:
        return False
    try:
        send(db, to, subject, text)
        return True
    except MailError:
        return False
