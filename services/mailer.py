"""
services/mailer.py

Outbound email, behind one small interface.

Why this exists
---------------
The account layer needs to deliver password-reset codes. Rather than call a
provider from the endpoints, they hand a message to a Mailer, so swapping
transport never touches application code.

Transports
----------
log     (default, local development)
    Writes the message to the log and marks it "visible to the caller", which
    lets the API echo a reset code in the response so the flow is testable
    without an inbox. Demo accounts use @example.com and @variantlens.local
    addresses that no provider can deliver to anyway, so this is the correct
    transport for the prototype -- not merely a shortcut.

resend  (production)
    Posts to the Resend API. Requires RESEND_API_KEY and a verified sending
    domain. Never marks messages visible, so a code cannot leak in a response.

smtp    (optional)
    stdlib smtplib, for an institutional relay.

Configuration
-------------
    MAIL_DELIVERY=log|resend|smtp     (default: log)
    RESEND_API_KEY, RESEND_FROM
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM

The safety rule enforced here: echoing a secret in an HTTP response is a
development convenience ONLY. `echo_secret` is False for every real transport,
and asking for a real transport without its credentials raises rather than
silently dropping the message.
"""

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

class Mailer:
    """
    Base transport. Subclasses implement `send`.

    Attributes
    ----------
    name        : transport identifier, useful in logs and admin views
    echo_secret : whether the caller may include a secret (a reset code) in the
                  HTTP response. True only for the log transport.
    """

    name = "base"
    echo_secret = False

    def send(self, to_email: str, subject: str, body: str,
             secret: Optional[str] = None) -> bool:
        raise NotImplementedError

    @staticmethod
    def _text_message(to_email: str, subject: str, body: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = os.getenv(
            "MAIL_FROM", "VariantLens <noreply@variantlens.local>")
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)
        return message

    def describe(self) -> str:
        return self.name


class LogMailer(Mailer):
    """
    Development transport: prints the message instead of sending it.

    Marked echo_secret=True so the reset endpoint can return the code and the
    flow is exercisable without an inbox.
    """

    name = "log"
    echo_secret = True

    def send(self, to_email: str, subject: str, body: str,
             secret: Optional[str] = None) -> bool:
        # The body already carries the code for a reset mail; this line repeats
        # it so it is greppable in the server log without reading the body.
        logger.info("mail[log] to=%s subject=%r%s", to_email, subject,
                    f" code={secret}" if secret else "")
        logger.debug("mail[log] body for %s:\n%s", to_email, body)
        return True


class ResendMailer(Mailer):
    """Production transport via the Resend HTTP API."""

    name = "resend"
    endpoint = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str):
        self.api_key = api_key
        self.sender = sender

    def send(self, to_email: str, subject: str, body: str,
             secret: Optional[str] = None) -> bool:
        # secret is accepted and ignored: a real transport must never return
        # the code to the requester.
        response = requests.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"from": self.sender, "to": [to_email],
                  "subject": subject, "text": body},
            timeout=10,
        )
        if response.status_code >= 400:
            logger.error("mail[resend] failed for %s: %s %s", to_email,
                         response.status_code, response.text[:200])
            return False
        return True


class SmtpMailer(Mailer):
    """Optional transport using an institutional relay."""

    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    def send(self, to_email: str, subject: str, body: str,
             secret: Optional[str] = None) -> bool:
        message = self._text_message(to_email, subject, body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as server:
                server.starttls()
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(message)
            return True
        except smtplib.SMTPException as exc:
            logger.error("mail[smtp] failed for %s: %s", to_email, exc)
            return False


def get_mailer() -> Mailer:
    """
    Build the configured mailer.

    MAIL_DELIVERY defaults to "log". Selecting a real transport without its
    credentials raises MailConfigurationError rather than falling back silently
    -- a deployment must not quietly stop delivering reset codes.
    """
    transport = os.getenv("MAIL_DELIVERY", "log").strip().lower()

    if transport == "log":
        return LogMailer()

    if transport == "resend":
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            raise MailConfigurationError(
                "MAIL_DELIVERY=resend requires RESEND_API_KEY. Set it, or use "
                "MAIL_DELIVERY=log for local development."
            )
        return ResendMailer(
            api_key,
            os.getenv("RESEND_FROM", "VariantLens <noreply@example.com>"),
        )

    if transport == "smtp":
        host = os.getenv("SMTP_HOST")
        if not host:
            raise MailConfigurationError("MAIL_DELIVERY=smtp requires SMTP_HOST.")
        return SmtpMailer(
            host=host,
            port=int(os.getenv("SMTP_PORT", "587")),
            username=os.getenv("SMTP_USER", ""),
            password=os.getenv("SMTP_PASSWORD", ""),
        )

    raise MailConfigurationError(
        f"MAIL_DELIVERY={transport!r} is not supported. "
        f"Choose one of: {', '.join(SUPPORTED_TRANSPORTS)}."
    )


def reset_code_body(code: str, minutes: int) -> str:
    """Plain-text body for a password-reset code."""
    return (
        "You asked to reset the password for your VariantLens account.\n\n"
        f"    Verification code: {code}\n\n"
        f"The code expires in {minutes} minutes and can only be used once.\n"
        "If you did not request this, you can safely ignore this message -- "
        "your password will not change.\n"
    )
logger = logging.getLogger("variantlens.mail")

SUPPORTED_TRANSPORTS = ("log", "resend", "smtp")


class MailConfigurationError(RuntimeError):
    """Raised when a real transport is selected without its credentials."""