# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Retention and deletion (DESIGN.md section 18). Standard library only.

The submissions of a lab are deleted `retention_days` days after the
later of the deadline and the last submission. The lab itself (task,
testbench) is kept.
"""

import calendar
import os
import shutil
from datetime import timedelta

from . import db, util

WARN_MONTHS = (
    9  # from this distance to the deletion date on, the admin pages warn
)
URGENT_DAYS = 30
LOGIN_CODE_KEEP = timedelta(
    hours=24
)  # codes including IP address; the rate limits only need one hour


def add_months(dt, months):
    month = dt.month - 1 + months
    year, month = dt.year + month // 12, month % 12 + 1
    return dt.replace(
        year=year,
        month=month,
        day=min(dt.day, calendar.monthrange(year, month)[1]),
    )


def delete_at(due_at, last_submission_at, retention_days):
    """Deletion date (UTC) for the submissions of a lab."""
    base = max(util.from_iso(due_at), util.from_iso(last_submission_at))
    return base + timedelta(days=retention_days)


def schedule(conn, retention_days, now=None):
    """Deletion dates of all labs with submissions: {name: {delete_at,
    days_left, warn, urgent, submissions}}."""
    now = now or util.utcnow()
    horizon = add_months(now, WARN_MONTHS)
    result = {}
    for row in conn.execute(
        "SELECT l.name, l.due_at, MAX(s.created_at) AS last, COUNT(*) AS n "
        "FROM lab l JOIN submission s ON s.lab = l.name "
        "GROUP BY l.name ORDER BY l.name"
    ):
        when = delete_at(row["due_at"], row["last"], retention_days)
        days_left = (when - now).days
        result[row["name"]] = {
            "delete_at": util.to_iso(when),
            "days_left": days_left,
            "submissions": row["n"],
            "warn": when <= horizon,
            "urgent": days_left <= URGENT_DAYS,
        }
    return result


def warnings(conn, retention_days, now=None):
    """Only the labs whose deletion is due within WARN_MONTHS, sorted by
    date."""
    items = schedule(conn, retention_days, now)
    return sorted(
        ((name, e) for name, e in items.items() if e["warn"]),
        key=lambda x: x[1]["delete_at"],
    )


def lab_dir(cfg, lab):
    return os.path.join(cfg["data_dir"], "submissions", lab)


def purge(conn, cfg, now=None, dry_run=False):
    """Delete due submissions (rows and files) and expired login data.

    Returns counters. Labs with a check still running are skipped (again
    on the next run).
    """
    now = now or util.utcnow()
    now_iso = util.to_iso(now)
    counts = {
        "labs": 0,
        "submissions": 0,
        "login_codes": 0,
        "remember_tokens": 0,
    }
    for name, entry in schedule(conn, cfg["retention_days"], now).items():
        if entry["delete_at"] > now_iso:
            continue
        if conn.execute(
            "SELECT 1 FROM submission WHERE lab = ? AND status "
            "IN ('queued', 'running')",
            (name,),
        ).fetchone():
            continue
        counts["labs"] += 1
        counts["submissions"] += entry["submissions"]
        if not dry_run:
            conn.execute("DELETE FROM submission WHERE lab = ?", (name,))
            shutil.rmtree(lab_dir(cfg, name), ignore_errors=True)
    codes_before = util.to_iso(now - LOGIN_CODE_KEEP)
    counts["login_codes"] = conn.execute(
        "SELECT COUNT(*) FROM login_code WHERE created_at < ?", (codes_before,)
    ).fetchone()[0]
    counts["remember_tokens"] = conn.execute(
        "SELECT COUNT(*) FROM remember_token WHERE expires_at <= ?", (now_iso,)
    ).fetchone()[0]
    if not dry_run:
        conn.execute(
            "DELETE FROM login_code WHERE created_at < ?", (codes_before,)
        )
        conn.execute(
            "DELETE FROM remember_token WHERE expires_at <= ?", (now_iso,)
        )
        conn.commit()
        db.meta_set(conn, "last_purge_at", now_iso)
    return counts
