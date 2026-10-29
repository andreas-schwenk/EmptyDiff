# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import io
import json
import os
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

from helpers import GAUSS_FUNCTION, GOOD, OTHER, STUDENT, DbTestCase, add_lab
from emptydiff.core import archive, auth, labs, retention, submissions, util

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DAYS = 730


def iso(dt):
    return util.to_iso(dt)


class DatesTest(unittest.TestCase):
    def test_add_months_clamps_day_and_wraps_year(self):
        self.assertEqual(
            retention.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1)
            .date()
            .isoformat(),
            "2026-02-28",
        )
        self.assertEqual(
            retention.add_months(
                datetime(2026, 12, 15, tzinfo=timezone.utc), 2
            )
            .date()
            .isoformat(),
            "2027-02-15",
        )
        self.assertEqual(
            retention.add_months(datetime(2026, 10, 1, tzinfo=timezone.utc), 9)
            .date()
            .isoformat(),
            "2027-07-01",
        )

    def test_delete_at_uses_the_later_of_due_and_last_submission(self):
        due, late = "2025-01-01T22:59:59Z", "2025-03-01T10:00:00Z"
        self.assertEqual(
            retention.delete_at(due, "2024-12-01T10:00:00Z", DAYS),
            util.from_iso(due) + timedelta(days=DAYS),
        )
        self.assertEqual(
            retention.delete_at(due, late, DAYS),
            util.from_iso(late) + timedelta(days=DAYS),
        )


class ScheduleTest(DbTestCase):
    def put(self, lab, due, last, email=STUDENT):
        add_lab(self.conn, lab, due=iso(due))
        self.conn.execute(
            "INSERT INTO submission (lab, email, attempt, "
            "created_at, status, passed) VALUES (?, ?, 1, ?, "
            "'done', 1)",
            (lab, email, iso(last)),
        )
        self.conn.commit()

    def test_warning_window_is_nine_months(self):
        self.put(
            "am24-1a",
            NOW - timedelta(days=DAYS - 100),
            NOW - timedelta(days=DAYS - 100),
        )  # in 100 days
        self.put(
            "am24-1b",
            NOW - timedelta(days=DAYS - 270),
            NOW - timedelta(days=DAYS - 270),
        )  # in 270 days (< 9 months)
        self.put(
            "am24-1c",
            NOW - timedelta(days=DAYS - 400),
            NOW - timedelta(days=DAYS - 400),
        )  # in 400 days
        plan = retention.schedule(self.conn, DAYS, NOW)
        self.assertEqual(
            {n: e["warn"] for n, e in plan.items()},
            {"am24-1a": True, "am24-1b": True, "am24-1c": False},
        )
        self.assertEqual(
            {n: e["urgent"] for n, e in plan.items()},
            {"am24-1a": False, "am24-1b": False, "am24-1c": False},
        )
        self.assertEqual(
            [n for n, _ in retention.warnings(self.conn, DAYS, NOW)],
            ["am24-1a", "am24-1b"],
        )

    def test_urgent_and_overdue(self):
        self.put(
            "am24-1a",
            NOW - timedelta(days=DAYS - 10),
            NOW - timedelta(days=DAYS - 10),
        )
        self.put(
            "am24-1b",
            NOW - timedelta(days=DAYS + 50),
            NOW - timedelta(days=DAYS + 50),
        )
        plan = retention.schedule(self.conn, DAYS, NOW)
        self.assertTrue(plan["am24-1a"]["urgent"])
        self.assertLess(plan["am24-1b"]["days_left"], 0)
        self.assertTrue(plan["am24-1b"]["warn"])

    def test_labs_without_submissions_are_not_listed(self):
        add_lab(self.conn, "am24-1a", due=iso(NOW - timedelta(days=5000)))
        self.assertEqual(retention.schedule(self.conn, DAYS, NOW), {})

    def test_recent_late_submission_postpones_deletion(self):
        self.put("am24-1a", NOW - timedelta(days=900), NOW - timedelta(days=3))
        self.assertFalse(
            retention.schedule(self.conn, DAYS, NOW)["am24-1a"]["warn"]
        )


class PurgeTest(DbTestCase):
    def submit(self, lab, email, when):
        sid = submissions.submit(
            self.conn, self.cfg, lab, email, {"addone.m": GOOD}, now=when
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
        )
        self.conn.commit()
        return sid

    def setUp(self):
        super().setUp()
        self.cfg["retention_days"] = DAYS
        old = NOW - timedelta(days=DAYS + 10)
        add_lab(self.conn, "am22-1a", due=iso(old))
        add_lab(self.conn, "am26-1a", due=iso(NOW + timedelta(days=30)))
        self.submit("am22-1a", STUDENT, old - timedelta(days=1))
        self.submit("am26-1a", STUDENT, NOW - timedelta(days=1))

    def count(self, table, where=""):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} {where}"
        ).fetchone()[0]

    def test_dry_run_changes_nothing(self):
        counts = retention.purge(self.conn, self.cfg, now=NOW, dry_run=True)
        self.assertEqual((counts["labs"], counts["submissions"]), (1, 1))
        self.assertEqual(self.count("submission"), 2)
        self.assertTrue(os.path.isdir(retention.lab_dir(self.cfg, "am22-1a")))

    def test_purge_removes_expired_labs_data_only(self):
        counts = retention.purge(self.conn, self.cfg, now=NOW)
        self.assertEqual((counts["labs"], counts["submissions"]), (1, 1))
        self.assertEqual(self.count("submission", "WHERE lab = 'am22-1a'"), 0)
        self.assertFalse(
            os.path.exists(retention.lab_dir(self.cfg, "am22-1a"))
        )
        self.assertEqual(self.count("submission", "WHERE lab = 'am26-1a'"), 1)
        self.assertTrue(os.path.isdir(retention.lab_dir(self.cfg, "am26-1a")))
        self.assertIsNotNone(
            labs.get(self.conn, "am22-1a")
        )  # the task itself stays
        self.assertEqual(
            retention.purge(self.conn, self.cfg, now=NOW)["labs"], 0
        )  # idempotent

    def test_running_check_is_skipped_until_finished(self):
        self.conn.execute(
            "UPDATE submission SET status = 'running' WHERE lab = 'am22-1a'"
        )
        self.conn.commit()
        self.assertEqual(
            retention.purge(self.conn, self.cfg, now=NOW)["labs"], 0
        )
        self.assertEqual(self.count("submission"), 2)

    def test_old_login_codes_and_tokens_are_removed(self):
        auth.issue_code(
            self.conn, STUDENT, "203.0.113.5", now=NOW - timedelta(hours=30)
        )
        auth.issue_code(
            self.conn, STUDENT, "203.0.113.5", now=NOW - timedelta(hours=2)
        )
        auth.issue_remember_token(
            self.conn, STUDENT, 1, now=NOW - timedelta(days=3)
        )  # expired
        keep = auth.issue_remember_token(self.conn, OTHER, 180, now=NOW)
        counts = retention.purge(self.conn, self.cfg, now=NOW)
        self.assertEqual(
            (counts["login_codes"], counts["remember_tokens"]), (1, 0)
        )  # token already cleaned up on issue
        self.assertEqual(self.count("login_code"), 1)
        self.assertEqual(
            auth.check_remember_token(self.conn, keep, 180, now=NOW), OTHER
        )


class ArchiveTest(DbTestCase):
    def build(self, lab="am24-1a"):
        buf = io.BytesIO()
        count = archive.write(self.conn, self.cfg, lab, buf, now=NOW)
        return count, zipfile.ZipFile(io.BytesIO(buf.getvalue()))

    def finish(self, sid, passed=1):
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = ?, "
            "log = ? WHERE id = ?",
            (passed, "[Verification ready]\n", sid),
        )
        self.conn.commit()

    def test_contains_config_all_attempts_reports_and_overview(self):
        add_lab(self.conn, provided=GAUSS_FUNCTION)
        a1 = submissions.submit(
            self.conn,
            self.cfg,
            "am24-1a",
            STUDENT,
            {"addone.m": b"% v1\n"},
            now=NOW,
        )
        self.finish(a1, 0)
        a2 = submissions.submit(
            self.conn,
            self.cfg,
            "am24-1a",
            STUDENT,
            {"addone.m": GOOD},
            now=NOW,
        )
        self.finish(a2)
        o1 = submissions.submit(
            self.conn, self.cfg, "am24-1a", OTHER, {"addone.m": GOOD}, now=NOW
        )  # still being checked
        count, z = self.build()
        self.assertEqual(count, 3)
        names = set(z.namelist())
        who = STUDENT.replace("@", "_")
        for expected in (
            "am24-1a/README.txt",
            "am24-1a/lab.json",
            "am24-1a/testbench.m",
            "am24-1a/provided.m",
            "am24-1a/overview.csv",
            f"am24-1a/submissions/{who}/001/addone.m",
            f"am24-1a/submissions/{who}/002/addone.m",
            f"am24-1a/reports/{who}/001.txt",
            f"am24-1a/reports/{who}/002.txt",
        ):
            self.assertIn(expected, names)
        self.assertFalse(
            any("reports" in n and OTHER.replace("@", "_") in n for n in names)
        )  # without a result no report
        self.assertEqual(
            z.read(f"am24-1a/submissions/{who}/001/addone.m"), b"% v1\n"
        )
        meta = json.loads(z.read("am24-1a/lab.json"))
        self.assertEqual(
            (meta["name"], meta["required_files"]), ("am24-1a", ["addone.m"])
        )
        rows = z.read("am24-1a/overview.csv").decode().strip().splitlines()
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[0].startswith("email,attempt,created_at"))
        self.assertIsNotNone(o1)

    def test_students_with_same_safe_name_get_separate_folders(self):
        add_lab(self.conn)
        submissions.submit(
            self.conn, self.cfg, "am24-1a", "a@b", {"addone.m": b"1"}, now=NOW
        )
        submissions.submit(
            self.conn, self.cfg, "am24-1a", "a_b", {"addone.m": b"2"}, now=NOW
        )
        _, z = self.build()
        files = sorted(n for n in z.namelist() if "/submissions/" in n)
        self.assertEqual(
            files,
            [
                "am24-1a/submissions/a_b/001/addone.m",
                "am24-1a/submissions/a_b_2/001/addone.m",
            ],
        )

    def test_missing_files_on_disk_do_not_break_the_archive(self):
        add_lab(self.conn)
        sid = submissions.submit(
            self.conn,
            self.cfg,
            "am24-1a",
            STUDENT,
            {"addone.m": GOOD},
            now=NOW,
        )
        import shutil

        shutil.rmtree(retention.lab_dir(self.cfg, "am24-1a"))
        count, z = self.build()
        self.assertEqual(count, 1)
        self.assertIn("am24-1a/overview.csv", z.namelist())
        self.assertIsNotNone(sid)

    def test_unknown_lab(self):
        with self.assertRaises(KeyError):
            self.build("am99-1a")


if __name__ == "__main__":
    unittest.main()
