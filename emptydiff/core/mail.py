# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sending mail with two backends. Standard library only (smtplib).

'smtp': real delivery through an SMTP server (host, port, security
none/starttls/ssl, optional login). 'simulate': nothing is sent; send()
returns the mail and the web interface shows it once to the person who
asked for it, as a modal (no database, no history). Only with
EMPTYDIFF_DEV=1 or, for test installations, mail.simulate_insecure.
"""

import smtplib
import ssl
from collections import deque
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config, i18n


class MailError(Exception):
    pass


SIMULATED = deque(
    maxlen=20
)  # dev mode only: the mails sent so far (recipient, subject, text)


def pop_simulated():
    """Return the simulated mails of dev mode and empty the queue."""
    mails = []
    while SIMULATED:
        mails.append(SIMULATED.popleft())
    return mails


def simulates(cfg):
    """True if no mail is sent and the page shows it instead."""
    return cfg["mail"]["backend"] == "simulate"


def send(cfg, to, subject, body):
    """Send a mail. A simulated mail is returned (None otherwise)."""
    backend = cfg["mail"]["backend"]
    if backend == "simulate":
        if not (config.is_dev() or cfg["mail"]["simulate_insecure"]):
            raise MailError(
                "Mail simulation is only allowed with EMPTYDIFF_DEV=1 "
                "or mail.simulate_insecure = true"
            )
        simulated = {"recipient": to, "subject": subject, "body": body}
        if config.is_dev():
            SIMULATED.append(simulated)
        return simulated
    if backend != "smtp":
        raise MailError(f"unbekanntes Mail-Backend {backend!r}")

    m = cfg["mail"]
    if not str(m["host"]).strip() or not str(m["sender"]).strip():
        raise MailError("mail.host and mail.sender are not configured")
    message = EmailMessage()
    message["From"] = m["sender"]
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(
        domain=m["sender"].rpartition("@")[2] or None
    )
    message["Auto-Submitted"] = (
        "auto-generated"  # no out-of-office replies to system mails
    )
    message.set_content(body)
    try:
        if m["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(
                m["host"],
                m["port"],
                timeout=m["timeout_seconds"],
                context=ssl.create_default_context(),
            )
        else:
            smtp = smtplib.SMTP(
                m["host"], m["port"], timeout=m["timeout_seconds"]
            )
        with smtp:
            if m["security"] == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if m["user"]:
                smtp.login(m["user"], m["password"])
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"Mailversand fehlgeschlagen: {e}") from e


def login_code_mail(code, ttl_minutes, lang=i18n.OPERATOR_LANGUAGE):
    """Subject and text of the login mail in the language `lang`."""
    return (
        i18n.translate(lang, "mail.login_subject"),
        i18n.translate(
            lang, "mail.login_body", code=code, minutes=ttl_minutes
        ),
    )
