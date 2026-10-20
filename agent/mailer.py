"""Password-reset email delivery -- plain SMTP via aiosmtplib, against
whatever provider SMTP_HOST/SMTP_USER/SMTP_PASS point at (see .env.example).
Nothing here is provider-specific: it's standard SMTP over STARTTLS.

Code-based reset (a short numeric code typed back into the form) rather
than a emailed link -- one less thing to build and host, since a link-based
flow needs its own frontend route and single-use token plumbing on top of
the same underlying reset record this already keeps.
"""

import logging

import aiosmtplib
from email.message import EmailMessage

from agent.config import Config

logger = logging.getLogger("tektonix")

RESET_CODE_TTL_MINUTES = 30


async def send_password_reset_email(config: Config, to_email: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Your Persei password reset code"
    msg["From"] = config.smtp_from
    msg["To"] = to_email
    msg.set_content(
        f"Use this code to reset your Persei password: {code}\n\n"
        f"It expires in {RESET_CODE_TTL_MINUTES} minutes. If you didn't request this, ignore this email."
    )
    await aiosmtplib.send(
        msg,
        hostname=config.smtp_host,
        port=config.smtp_port,
        username=config.smtp_user,
        password=config.smtp_pass,
        start_tls=True,
    )


async def send_plain_email(config: Config, to_email: str, subject: str, body: str) -> None:
    """One plain-text email over the same SMTP settings. Used by the GitHub
    inbox for approve links; raises on failure so the caller can log it."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.smtp_from
    msg["To"] = to_email
    msg.set_content(body)
    await aiosmtplib.send(
        msg,
        hostname=config.smtp_host,
        port=config.smtp_port,
        username=config.smtp_user,
        password=config.smtp_pass,
        start_tls=True,
    )
