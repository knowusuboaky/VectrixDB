"""Sending the few emails this system sends: a set-up link, a password reset, a warning.

A sender is anything called with ``(to, subject, text)``. Three come with the
package. ``SmtpSender`` speaks to any mail server, which covers a company
relay, Amazon SES, SendGrid and the rest, since they all offer SMTP.
``LogSender`` writes the message to the server's log, and runs only when no
mail server is configured and the server's public address is this machine:
right for a laptop. On any other server a link in the log is a way in for
whoever reads the log, which is often a team and a log shipping service, so
there ``NoMailSender`` runs instead and says the message was not sent. The
link is then made on the server itself, with ``vectrixdb people reset``.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable
from urllib.parse import unquote, urlsplit

from ..exceptions import ConfigurationError

__all__ = [
    "LogSender",
    "NoMailSender",
    "Sender",
    "SmtpSender",
    "enrolment_email",
    "lock_email",
    "password_email",
]


# ============================================================================
# SETTINGS: the logger, and what a sender is
# ============================================================================
#
# Anything called with (to, subject, text) is a sender.

logger = logging.getLogger("vectrixdb.signin")

Sender = Callable[[str, str, str], None]


# ============================================================================
# THE SENDERS
# ============================================================================
#
# INPUT   (to, subject, text)
# OUTPUT  written to the log on a laptop; not sent, and said so without the
#         link, on any other server with no mail server; sent by SMTP with
#         STARTTLS or by SMTPS
#
# A link in a server's log is a way in for whoever reads the log.


class LogSender:
    """Writes the message to the log. For a laptop, where the log is on the screen of the person it is for."""

    configured = False

    def __call__(self, to: str, subject: str, text: str) -> None:
        logger.warning("no mail server is configured, so the sign-in email for %s is here instead:\n%s", to, text)


class NoMailSender:
    """Sends nothing, and says so without the link: a server reachable by others, with no mail server."""

    configured = False

    def __call__(self, to: str, subject: str, text: str) -> None:
        logger.warning(
            "no mail server is configured, so the email for %s (%s) was not sent. Set VECTRIXDB_SMTP_URL, "
            "or make a set-up link on the server with: vectrixdb people reset %s",
            to,
            subject,
            to,
        )


class SmtpSender:
    """``smtp://user:password@host:587`` with STARTTLS, or ``smtps://host:465``."""

    configured = True

    def __init__(self, url: str, sender: str, *, timeout: float = 15.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
            raise ConfigurationError("VECTRIXDB_SMTP_URL looks like smtp://user:password@host:587 or smtps://host:465")
        if not sender or "@" not in sender:
            raise ConfigurationError("VECTRIXDB_MAIL_FROM must be the address sign-in emails come from")
        self.implicit_tls = parts.scheme == "smtps"
        self.host = parts.hostname
        self.port = parts.port or (465 if self.implicit_tls else 587)
        self.username = unquote(parts.username) if parts.username else None
        self.password = unquote(parts.password) if parts.password else None
        self.sender = sender
        self.timeout = timeout

    def __call__(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(text)
        context = ssl.create_default_context()
        if self.implicit_tls:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with server:
            if not self.implicit_tls:
                # No fallback to plain text: a link that enrols an authenticator does not travel in the clear.
                server.starttls(context=context)
            if self.username:
                server.login(self.username, self.password or "")
            server.send_message(message)


# ============================================================================
# THE THREE EMAILS
# ============================================================================
#
# INPUT   a link, the minutes it lasts, and the product's name
# OUTPUT  the set-up email, the password reset, and the lock warning, as
#         subject and text
#
# Short, plain, and the link is the whole message.


def enrolment_email(link: str, minutes: int, product: str = "VectrixDB", passkeys: bool = True) -> tuple[str, str]:
    how = "choose how you will sign in: a passkey, or an authenticator app" if passkeys else "set up the authenticator app you will sign in with"
    subject = f"Your sign-in link for {product}"
    text = (
        f"Somebody, we hope you, asked to sign in to {product} with this address.\n\n"
        f"Open this link to {how}. "
        f"It works once, for {minutes} minutes:\n\n"
        f"{link}\n\n"
        "If this was not you, do nothing. Nobody can sign in without the link, and it expires on its own.\n"
    )
    return subject, text


def password_email(link: str, minutes: int, product: str = "VectrixDB") -> tuple[str, str]:
    subject = f"Choose a new password for {product}"
    text = (
        f"Somebody, we hope you, asked to reset the password for this address on {product}.\n\n"
        f"Open this link to choose a new one. You will need the code from your authenticator app as well. "
        f"It works once, for {minutes} minutes:\n\n"
        f"{link}\n\n"
        "If this was not you, do nothing. The link is useless without your authenticator, and it expires on its own.\n"
    )
    return subject, text


def lock_email(minutes: int, product: str = "VectrixDB") -> tuple[str, str]:
    subject = f"Sign-in to {product} is paused for your address"
    text = (
        f"Wrong codes were typed for your address on {product} five times, so signing in with a code is paused "
        f"for {minutes} minutes.\n\n"
        "If that was you, wait and try again. If it was not, nobody got in: that takes your authenticator app "
        f"or a passkey. If this keeps happening, tell whoever runs {product}.\n"
    )
    return subject, text
