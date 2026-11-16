"""Outgoing email over SMTP. Failures are logged and reported as False, never raised."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import settings
from .config import EmailConfig

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 10
SMTP_SSL_PORT = 465
TEMPLATE_DIR = Path(__file__).resolve().parent / "mail_templates"

# Agent-supplied text ends up in these templates, so HTML is always escaped.
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]),
                   trim_blocks=True, lstrip_blocks=True)


def is_configured(email: EmailConfig) -> bool:
    return bool(email.smtp_server and email.to_addrs)


def render(template: str, **context) -> tuple:
    """Render mail_templates/<template>.txt and .html; returns (text, html)."""
    return (_env.get_template(f"{template}.txt").render(**context),
            _env.get_template(f"{template}.html").render(**context))


def send(email: EmailConfig, subject: str, text: str, html: str) -> bool:
    """Send one multipart message to email.to_addrs; True if the server accepted it."""
    if not is_configured(email):
        logger.debug("email is not configured (smtp_server and to_addrs); not sending '%s'", subject)
        return False
    user, password = settings.smtp_login()
    sender = email.from_addr or user
    if not sender:
        logger.error("no sender address: set email.from_addr or JABS_SMTP_USERNAME")
        return False
    if settings.env_mode() == "development":
        subject = f"[DEV] {subject}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(email.to_addrs)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    try:
        context = ssl.create_default_context()
        implicit_tls = email.smtp_port == SMTP_SSL_PORT
        if implicit_tls:
            server: smtplib.SMTP = smtplib.SMTP_SSL(email.smtp_server, email.smtp_port,
                                                    timeout=SMTP_TIMEOUT_S, context=context)
        else:
            server = smtplib.SMTP(email.smtp_server, email.smtp_port, timeout=SMTP_TIMEOUT_S)
        with server:
            if email.use_tls and not implicit_tls:
                server.starttls(context=context)
            if user and password:
                server.login(user, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        logger.error("failed to send email '%s': %s", subject, exc)
        return False
    logger.info("email sent: '%s' to %s", subject, ", ".join(email.to_addrs))
    return True


def smtp_login_hint(email: EmailConfig) -> Optional[str]:
    """A warning for the test command when credentials are missing, else None."""
    if email.smtp_server and not all(settings.smtp_login()):
        return "JABS_SMTP_USERNAME / JABS_SMTP_PASSWORD are not set; sending without logging in"
    return None
