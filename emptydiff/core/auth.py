# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Login with a one-time code (DESIGN.md section 6). Standard library
only.

The code is sent by mail to an address in the allowed domains and proves
access to the mailbox. Only a SHA-256 hash is stored. Optionally ("stay
logged in") a random remember-me token is added; it lives in the cookie,
is stored on the server only as a hash and is deleted on logout.
"""

import hashlib
import hmac
import math
import re
import secrets
from datetime import timedelta

from . import i18n, util

# Crockford base32 without I, L, O, U
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
MAX_ATTEMPTS = 5
# Defaults of the limits; the configuration ([auth]) overrides them (see
# config.py).
MAX_CODES_PER_EMAIL_HOUR = 8
MAX_CODES_PER_IP_HOUR = 200
REMEMBER_SLIDE = timedelta(
    days=1
)  # extend the expiry at most once per day (saves writes)
EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+'-]{0,63}@([a-z0-9.-]{1,190})$")
_CODE_TRANSLATION = str.maketrans({"O": "0", "I": "1", "L": "1"})


class AuthError(i18n.LocalizedError):
    """Error with a message that students can read (and that can be
    translated)."""


def normalize_email(raw, allowed_domains):
    """The address in lower case (the only form that is stored and
    compared), or AuthError."""
    email = (raw or "").strip().lower()
    match = EMAIL_RE.match(email)
    if not match or match.group(1) not in {d.lower() for d in allowed_domains}:
        domains = ", ".join("@" + d for d in allowed_domains)
        raise AuthError("err.auth.domain", domains=domains)
    return email


def normalize_code(raw):
    return re.sub(r"[\s-]", "", raw or "").upper().translate(_CODE_TRANSLATION)


def _hash(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def issue_code(
    conn,
    email,
    ip,
    ttl_minutes=15,
    now=None,
    *,
    per_email=MAX_CODES_PER_EMAIL_HOUR,
    per_ip=MAX_CODES_PER_IP_HOUR,
    cooldown_seconds=0,
):
    """Create a new code (invalidates older codes of the same address).

    Requests for a code are limited: at most `cooldown_seconds` apart
    per address, at most `per_email` per address and `per_ip` per IP
    address and hour. A refused request is not counted.
    """
    now = now or util.utcnow()
    since = util.to_iso(now - timedelta(hours=1))
    if cooldown_seconds:
        last = conn.execute(
            "SELECT MAX(created_at) FROM login_code WHERE email = ?", (email,)
        ).fetchone()[0]
        if last:
            wait = (
                cooldown_seconds - (now - util.from_iso(last)).total_seconds()
            )
            if wait > 0:
                raise AuthError("err.auth.cooldown", seconds=math.ceil(wait))
    count = conn.execute(
        "SELECT COUNT(*) FROM login_code WHERE email = ? AND created_at > ?",
        (email, since),
    ).fetchone()[0]
    if count >= per_email:
        raise AuthError("err.auth.too_many_email")
    if ip:
        count = conn.execute(
            "SELECT COUNT(*) FROM login_code WHERE ip = ? AND created_at > ?",
            (ip, since),
        ).fetchone()[0]
        if count >= per_ip:
            raise AuthError("err.auth.too_many_ip")
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    conn.execute(
        "INSERT INTO login_code (email, code_hash, "
        "expires_at, ip, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            email,
            _hash(code),
            util.to_iso(now + timedelta(minutes=ttl_minutes)),
            ip,
            util.to_iso(now),
        ),
    )
    conn.commit()
    return code


def verify_code(conn, email, code, now=None):
    """Check the most recently issued code. A code can be used once."""
    now_iso = util.to_iso(now or util.utcnow())
    row = conn.execute(
        "SELECT rowid, code_hash, expires_at, attempts FROM "
        "login_code WHERE email = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (email,),
    ).fetchone()
    if (
        row is None
        or row["expires_at"] <= now_iso
        or row["attempts"] >= MAX_ATTEMPTS
    ):
        return False
    conn.execute(
        "UPDATE login_code SET attempts = attempts + 1 WHERE rowid = ?",
        (row["rowid"],),
    )
    ok = hmac.compare_digest(row["code_hash"], _hash(normalize_code(code)))
    if ok:
        # used up: the row is kept for the rate limits
        conn.execute(
            "UPDATE login_code SET expires_at = ? WHERE rowid = ?",
            ("1970-01-01T00:00:00Z", row["rowid"]),
        )
    conn.commit()
    return ok


def issue_remember_token(conn, email, days, now=None):
    """Create a remember-me token for `email` (valid `days` days from
    last use) and clean up expired ones."""
    now = now or util.utcnow()
    conn.execute(
        "DELETE FROM remember_token WHERE expires_at <= ?", (util.to_iso(now),)
    )
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO remember_token (token_hash, email, "
        "created_at, expires_at) VALUES (?, ?, ?, ?)",
        (
            _hash(token),
            email,
            util.to_iso(now),
            util.to_iso(now + timedelta(days=days)),
        ),
    )
    conn.commit()
    return token


def check_remember_token(conn, token, days, now=None):
    """Return the address for the token (and extend its validity) or
    None."""
    if not token or len(token) > 200:
        return None
    now = now or util.utcnow()
    row = conn.execute(
        "SELECT email, expires_at FROM remember_token WHERE token_hash = ?",
        (_hash(token),),
    ).fetchone()
    if row is None or row["expires_at"] <= util.to_iso(now):
        return None
    renewed = util.to_iso(now + timedelta(days=days))
    if (
        util.from_iso(renewed) - util.from_iso(row["expires_at"])
        >= REMEMBER_SLIDE
    ):
        conn.execute(
            "UPDATE remember_token SET expires_at = ? WHERE token_hash = ?",
            (renewed, _hash(token)),
        )
        conn.commit()
    return row["email"]


def revoke_remember_token(conn, token):
    """Invalidate a token (logout)."""
    if token:
        conn.execute(
            "DELETE FROM remember_token WHERE token_hash = ?", (_hash(token),)
        )
        conn.commit()
