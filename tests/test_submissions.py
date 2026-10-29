# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import unittest
from datetime import datetime, timezone

from helpers import (
    FUTURE,
    GAUSS_FUNCTION,
    GOOD,
    OTHER,
    PAST,
    STUDENT,
    DbTestCase,
    add_lab,
)
from emptydiff.core import labs, submissions

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class SubmitTest(DbTestCase):
    def submit(
        self, files=None, lab="am24-1a", email=STUDENT, now=NOW, preview=False
    ):
        files = {"addone.m": GOOD} if files is None else files
        return submissions.submit(
            self.conn, self.cfg, lab, email, files, now=now, preview=preview
        )

    def test_happy_path(self):
        add_lab(self.conn)
        sub = submissions.get(self.conn, self.submit())
        self.assertEqual(
            (sub["attempt"], sub["status"], sub["late"], sub["complete"]),
            (1, "queued", 0, 1),
        )
        self.assertEqual(
            submissions.load_files(self.cfg, sub), {"addone.m": GOOD}
        )

    def test_attempts_are_numbered(self):
        add_lab(self.conn)
        first = self.submit()
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (first,)
        )
        second = self.submit()
        self.assertEqual(submissions.get(self.conn, second)["attempt"], 2)

    def test_directory_uses_hash_not_address(self):
        add_lab(self.conn)
        sub = submissions.get(self.conn, self.submit())
        directory = submissions.submission_dir(
            self.cfg, "am24-1a", STUDENT, sub["attempt"]
        )
        self.assertTrue(os.path.isdir(directory))
        self.assertNotIn("max.muster", directory)
        self.assertNotIn("@", directory)

    def test_late_is_accepted_and_marked(self):
        add_lab(self.conn, due=PAST)
        self.assertEqual(submissions.get(self.conn, self.submit())["late"], 1)

    def test_refresh_late_follows_a_moved_deadline(self):
        add_lab(self.conn, due=PAST)
        sid = self.submit()
        other = self.submit(email=OTHER)
        self.assertEqual(submissions.get(self.conn, sid)["late"], 1)
        self.conn.execute("UPDATE lab SET due_at = ?", (FUTURE,))
        submissions.refresh_late(self.conn, sub_id=sid)
        self.assertEqual(submissions.get(self.conn, sid)["late"], 0)
        self.assertEqual(submissions.get(self.conn, other)["late"], 1)
        submissions.refresh_late(self.conn, lab_name="am24-1a")
        self.assertEqual(submissions.get(self.conn, other)["late"], 0)
        self.conn.execute("UPDATE lab SET due_at = ?", (PAST,))
        submissions.refresh_late(self.conn)
        self.assertEqual(
            [submissions.get(self.conn, i)["late"] for i in (sid, other)],
            [1, 1],
        )

    def test_on_time_exactly_at_deadline(self):
        add_lab(self.conn, due="2026-10-01T12:00:00Z")
        self.assertEqual(submissions.get(self.conn, self.submit())["late"], 0)

    def test_incomplete_lists_missing_files(self):
        add_lab(self.conn, required="addone.m\nplot.m\n")
        sub = submissions.get(self.conn, self.submit())
        self.assertEqual(
            (sub["complete"], sub["missing_files"]), (0, "plot.m")
        )

    def test_provided_functions_count_as_present(self):
        add_lab(
            self.conn, required="addone.m\ngauss.m\n", provided=GAUSS_FUNCTION
        )
        self.assertEqual(
            submissions.get(self.conn, self.submit())["complete"], 1
        )

    def test_unknown_or_inactive_lab(self):
        add_lab(self.conn, name="am24-1b", active=0)
        for lab in ("am99-9z", "am24-1b"):
            with self.subTest(lab=lab):
                with self.assertRaises(submissions.SubmissionError):
                    self.submit(lab=lab)

    def test_no_files_and_invalid_names(self):
        add_lab(self.conn)
        with self.assertRaises(submissions.SubmissionError):
            self.submit(files={})
        for name in ("../evil.m", "a/b.m", ".hidden", "x y.m", "ä.m", ""):
            with self.subTest(name=name):
                with self.assertRaises(submissions.SubmissionError):
                    self.submit(files={name: b"x"})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
            0,
        )

    def test_only_files_with_the_required_names_are_accepted(self):
        add_lab(
            self.conn, required="addone.m\nplot.png\n", provided=GAUSS_FUNCTION
        )
        for extra in (
            "other.m",
            "disp.m",
            "testbench.m",
            "gauss.m",
            "Addone.m",
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(submissions.SubmissionError) as ctx:
                    self.submit(files={"addone.m": GOOD, extra: b"x"})
                self.assertEqual(
                    ctx.exception.key, "err.submit.unexpected_files"
                )
                self.assertIn(extra, str(ctx.exception))
                self.assertIn("addone.m, plot.png", str(ctx.exception))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
            0,
        )
        self.assertEqual(
            submissions.get(
                self.conn,
                self.submit(files={"addone.m": GOOD, "plot.png": b"x"}),
            )["complete"],
            1,
        )

    def test_size_limit(self):
        add_lab(self.conn)
        self.cfg["limits"]["max_upload_bytes"] = 10
        with self.assertRaises(submissions.SubmissionError):
            self.submit(files={"addone.m": b"x" * 11})

    def test_max_attempts(self):
        add_lab(self.conn)
        self.cfg["limits"]["max_attempts"] = 2
        for _ in range(2):
            sid = self.submit()
            self.conn.execute(
                "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
            )
        with self.assertRaises(submissions.SubmissionError):
            self.submit()
        self.submit(email=OTHER)  # another student is not affected

    def test_hidden_lab_accepts_only_preview_submissions(self):
        add_lab(self.conn, active=0)
        with self.assertRaises(submissions.SubmissionError) as ctx:
            self.submit()
        self.assertEqual(ctx.exception.key, "err.submit.unavailable")
        self.assertEqual(
            submissions.get(self.conn, self.submit(preview=True))["status"],
            "queued",
        )

    def test_hidden_lab_has_the_same_attempt_limit_as_a_released_one(self):
        add_lab(self.conn, active=0)
        self.cfg["limits"]["max_attempts"] = 1
        sid = self.submit(preview=True)
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
        )
        with self.assertRaises(submissions.SubmissionError):
            self.submit(preview=True)

    def test_overview_lists_hidden_labs_only_when_asked_for(self):
        add_lab(self.conn, "am24-1a", active=1)
        add_lab(self.conn, "am24-1b", active=0)

        def names(**kw):
            rows = submissions.overview_for_user(self.conn, STUDENT, **kw)
            return [r["lab"]["name"] for r in rows]

        self.assertEqual(names(), ["am24-1a"])
        self.assertEqual(names(all_labs=True), ["am24-1a", "am24-1b"])

    def test_limit_message_points_to_the_supervisors(self):
        add_lab(self.conn)
        self.cfg["limits"]["max_attempts"] = 1
        sid = self.submit()
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
        )
        self.conn.commit()
        with self.assertRaises(submissions.SubmissionError) as ctx:
            self.submit()
        self.assertIn("1 submissions", str(ctx.exception))
        self.assertIn("contact your supervisors", str(ctx.exception))

    def test_deleting_a_submission_gives_an_attempt_back(self):
        add_lab(self.conn)
        self.cfg["limits"]["max_attempts"] = 3
        ids = []
        for _ in range(3):
            sid = self.submit()
            self.conn.execute(
                "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
            )
            self.conn.commit()
            ids.append(sid)
        with self.assertRaises(submissions.SubmissionError):
            self.submit()
        labs.delete_submission(
            self.conn, self.cfg, ids[1]
        )  # a middle submission: the highest number stays 3
        new = submissions.get(self.conn, self.submit())
        self.assertEqual(
            new["attempt"], 4
        )  # numbers are never reused: "last submission" stays unambiguous
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (new["id"],)
        )
        self.conn.commit()
        self.assertEqual(
            [
                s["attempt"]
                for s in submissions.list_for_user(
                    self.conn, STUDENT, "am24-1a"
                )
            ],
            [1, 3, 4],
        )
        with self.assertRaises(submissions.SubmissionError):
            self.submit()
        self.submit(email=OTHER)  # other students are not affected

    def test_deleting_the_latest_submission_makes_the_previous_one_count(self):
        add_lab(self.conn)
        self.cfg["limits"]["max_attempts"] = 2
        first = self.submit()
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (first,)
        )
        self.conn.commit()
        second = self.submit()
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (second,)
        )
        self.conn.commit()
        labs.delete_submission(self.conn, self.cfg, second)
        rows = submissions.overview_for_user(self.conn, STUDENT)
        self.assertEqual(
            (rows[0]["attempts"], rows[0]["last"]["id"]), (1, first)
        )
        self.assertEqual(
            submissions.get(self.conn, self.submit())["attempt"], 2
        )

    def test_pending_submission_blocks_next_one(self):
        add_lab(self.conn)
        self.submit()
        with self.assertRaises(submissions.SubmissionError):
            self.submit()

    def test_failed_validation_leaves_no_files_or_rows(self):
        add_lab(self.conn)
        self.cfg["limits"]["max_attempts"] = 1
        sid = self.submit()
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
        )
        with self.assertRaises(submissions.SubmissionError):
            self.submit()
        self.assertFalse(
            os.path.exists(
                submissions.submission_dir(self.cfg, "am24-1a", STUDENT, 2)
            )
        )


class OverviewTest(DbTestCase):
    def test_overview_includes_active_labs_and_labs_with_submissions(self):
        add_lab(self.conn, name="am24-1a")
        add_lab(self.conn, name="am23-1a", active=0)
        add_lab(self.conn, name="am22-1a", active=0)
        sid = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        self.conn.execute(
            "INSERT INTO submission (lab, email, attempt, "
            "created_at) VALUES ('am23-1a', ?, 1, ?)",
            (STUDENT, FUTURE),
        )
        rows = submissions.overview_for_user(self.conn, STUDENT)
        self.assertEqual(
            [r["lab"]["name"] for r in rows], ["am23-1a", "am24-1a"]
        )  # am22-1a: inactive, no submission
        self.assertEqual(rows[1]["attempts"], 1)
        self.assertEqual(rows[1]["last"]["id"], sid)
        other = submissions.overview_for_user(self.conn, OTHER)
        self.assertEqual([r["lab"]["name"] for r in other], ["am24-1a"])
        self.assertIsNone(other[0]["last"])


if __name__ == "__main__":
    unittest.main()
