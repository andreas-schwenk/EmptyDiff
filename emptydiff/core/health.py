# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Health check for operations (DESIGN.md section 13). Standard library
only.

Returns a list of problem identifiers (empty = all fine):
  db      database not readable
  worker  no recent heartbeat of the grading service
  queue   a submission has been waiting too long for grading
  purge   the nightly deletion is overdue
The identifiers contain no personal data; they may be publicly visible.
"""

import sqlite3
from datetime import timedelta

from . import db, util

STARTUP_GRACE = timedelta(
    minutes=10
)  # after the database has been created the worker may still be missing
QUEUE_MAX_AGE = timedelta(minutes=10)
PURGE_MAX_AGE = timedelta(hours=36)
PURGE_GRACE = timedelta(hours=48)  # first deletion after the installation


def worker_stale_after(cfg):
    """A run can take up to the time limit; only after that a missing
    heartbeat counts as a failure."""
    return timedelta(seconds=2 * cfg["grader"]["timeout_seconds"] + 60)


def check(conn, cfg, now=None):
    now = now or util.utcnow()
    try:
        created = db.meta_get(conn, "created_at")
        beat = db.meta_get(conn, "worker_heartbeat")
        purged = db.meta_get(conn, "last_purge_at")
        oldest = conn.execute(
            "SELECT MIN(created_at) FROM submission WHERE status = 'queued'"
        ).fetchone()[0]
    except sqlite3.Error:
        return ["db"]
    age_of_db = now - util.from_iso(created) if created else timedelta(0)
    problems = []
    if beat is None:
        if age_of_db > STARTUP_GRACE:
            problems.append("worker")
    elif now - util.from_iso(beat) > worker_stale_after(cfg):
        problems.append("worker")
    if oldest and now - util.from_iso(oldest) > QUEUE_MAX_AGE:
        problems.append("queue")
    if purged is None:
        if age_of_db > PURGE_GRACE:
            problems.append("purge")
    elif now - util.from_iso(purged) > PURGE_MAX_AGE:
        problems.append("purge")
    return problems
