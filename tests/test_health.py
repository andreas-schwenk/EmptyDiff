# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import GOOD, STUDENT, DbTestCase, add_lab
from emptydiff.core import db, health, retention, submissions, util, worker

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return util.to_iso(dt)


class HealthTest(DbTestCase):
    def setUp(self):
        super().setUp()
        db.meta_set(
            self.conn, "created_at", iso(NOW - timedelta(days=30))
        )  # no grace period

    def beat(self, ago=timedelta(seconds=10)):
        db.meta_set(self.conn, "worker_heartbeat", iso(NOW - ago))

    def purged(self, ago=timedelta(hours=5)):
        db.meta_set(self.conn, "last_purge_at", iso(NOW - ago))

    def test_all_good(self):
        self.beat()
        self.purged()
        self.assertEqual(health.check(self.conn, self.cfg, NOW), [])

    def test_worker_missing_or_stale(self):
        self.purged()
        self.assertEqual(health.check(self.conn, self.cfg, NOW), ["worker"])
        self.beat(timedelta(minutes=10))
        self.assertEqual(health.check(self.conn, self.cfg, NOW), ["worker"])
        self.beat(timedelta(seconds=60))
        self.assertEqual(health.check(self.conn, self.cfg, NOW), [])

    def test_stale_threshold_follows_the_grader_timeout(self):
        self.purged()
        self.cfg["grader"]["timeout_seconds"] = 300
        self.beat(
            timedelta(minutes=9)
        )  # a running job may take up to the time limit
        self.assertEqual(health.check(self.conn, self.cfg, NOW), [])

    def test_old_queued_submission_is_reported(self):
        self.beat()
        self.purged()
        add_lab(self.conn)
        sid = submissions.submit(
            self.conn,
            self.cfg,
            "am24-1a",
            STUDENT,
            {"addone.m": GOOD},
            now=NOW - timedelta(minutes=30),
        )
        self.assertEqual(health.check(self.conn, self.cfg, NOW), ["queue"])
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
        )
        self.conn.commit()
        self.assertEqual(health.check(self.conn, self.cfg, NOW), [])

    def test_purge_overdue(self):
        self.beat()
        self.purged(timedelta(hours=40))
        self.assertEqual(health.check(self.conn, self.cfg, NOW), ["purge"])
        db.meta_set(self.conn, "last_purge_at", iso(NOW - timedelta(hours=35)))
        self.assertEqual(health.check(self.conn, self.cfg, NOW), [])

    def test_grace_period_after_a_fresh_install(self):
        db.meta_set(self.conn, "created_at", iso(NOW - timedelta(minutes=3)))
        self.assertEqual(
            health.check(self.conn, self.cfg, NOW), []
        )  # worker and purge may still be missing
        db.meta_set(self.conn, "created_at", iso(NOW - timedelta(hours=1)))
        self.assertEqual(health.check(self.conn, self.cfg, NOW), ["worker"])
        db.meta_set(self.conn, "created_at", iso(NOW - timedelta(hours=60)))
        self.assertEqual(
            health.check(self.conn, self.cfg, NOW), ["worker", "purge"]
        )

    def test_broken_database(self):
        self.conn.execute("DROP TABLE meta")
        self.assertEqual(health.check(self.conn, self.cfg, NOW), ["db"])

    def test_worker_writes_heartbeat_and_purge_writes_stamp(self):
        self.assertIsNone(db.meta_get(self.conn, "worker_heartbeat"))
        worker.heartbeat(self.conn, NOW)
        self.assertEqual(db.meta_get(self.conn, "worker_heartbeat"), iso(NOW))
        self.assertIsNone(db.meta_get(self.conn, "last_purge_at"))
        retention.purge(self.conn, self.cfg, now=NOW, dry_run=True)
        self.assertIsNone(
            db.meta_get(self.conn, "last_purge_at")
        )  # a dry run does not count
        retention.purge(self.conn, self.cfg, now=NOW)
        self.assertEqual(db.meta_get(self.conn, "last_purge_at"), iso(NOW))

    def test_worker_thread_beats(self):
        import threading

        stop = threading.Event()
        thread = worker.start(self.cfg, stop, poll=0.05)
        for _ in range(100):
            if db.meta_get(self.conn, "worker_heartbeat"):
                break
            stop.wait(0.05)
        stop.set()
        thread.join(timeout=5)
        self.assertIsNotNone(db.meta_get(self.conn, "worker_heartbeat"))

    def test_meta_is_created_by_migration_with_created_at(self):
        fresh = db.init_db(os.path.join(self.tmp, "fresh.sqlite"))
        self.assertIsNotNone(db.meta_get(fresh, "created_at"))
        fresh.close()

    def test_healthcheck_command(self):
        from emptydiff import __main__ as cli

        path = os.path.join(self.tmp, "c.toml")
        with open(path, "w") as f:
            f.write(f'data_dir = "{self.tmp}"\n[mail]\nbackend = "simulate"\n')
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as ctx,
        ):
            cli.main(
                ["--config", path, "healthcheck"]
            )  # no worker heartbeat, database old enough
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("worker", out.getvalue())
        self.beat()
        self.purged(timedelta(hours=1))
        db.meta_set(self.conn, "worker_heartbeat", iso(util.utcnow()))
        db.meta_set(self.conn, "last_purge_at", iso(util.utcnow()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", path, "healthcheck"])
        self.assertEqual(out.getvalue().strip(), "ok")


@unittest.skipIf(
    flask is None,
    "Flask nicht installiert (Tests im dev/-Container ausführen)",
)
class HealthzRouteTest(DbTestCase):
    def setUp(self):
        super().setUp()
        from emptydiff.web import create_app

        self.client = create_app(self.cfg).test_client()

    def test_ok_without_login_and_without_personal_data(self):
        db.meta_set(self.conn, "worker_heartbeat", iso(util.utcnow()))
        db.meta_set(self.conn, "last_purge_at", iso(util.utcnow()))
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.data), {"status": "ok"})
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.mimetype, "application/json")

    def test_fail_returns_503_with_problem_codes_only(self):
        db.meta_set(
            self.conn, "created_at", iso(util.utcnow() - timedelta(days=30))
        )
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(
            json.loads(resp.data),
            {"status": "fail", "problems": ["worker", "purge"]},
        )

    def test_post_is_not_allowed(self):
        self.assertEqual(
            self.client.post("/healthz").status_code, 400
        )  # without CSRF token (like any POST request)


if __name__ == "__main__":
    unittest.main()
