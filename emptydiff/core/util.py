# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Time functions. Timestamps are UTC everywhere in the format
'YYYY-MM-DDTHH:MM:SSZ'."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ISO = "%Y-%m-%dT%H:%M:%SZ"
BERLIN = ZoneInfo("Europe/Berlin")


def utcnow():
    return datetime.now(timezone.utc)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime(ISO)


def from_iso(text):
    return datetime.strptime(text, ISO).replace(tzinfo=timezone.utc)


def local_date_str(text, fmt="%d.%m.%Y"):
    """UTC timestamp as a local date, e.g. '16.12.2024'."""
    return from_iso(text).astimezone(BERLIN).strftime(fmt) if text else ""


def local_str(text, fmt="%d.%m.%Y %H:%M"):
    """UTC timestamp as local time for display, e.g. '16.12.2024
    23:59'."""
    if not text:
        return ""
    return from_iso(text).astimezone(BERLIN).strftime(fmt)
