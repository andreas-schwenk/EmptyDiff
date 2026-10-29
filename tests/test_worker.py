# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import threading
import unittest
from unittest import mock

from helpers import GOOD, MODES, STUDENT, DbTestCase, add_lab
from emptydiff.core import db, grader, submissions, worker


def ok_result(log=""):
    return grader.GradeResult(
        passed=True,
        checks=[(True, "x")],
        finished=True,
        exit_code=0,
        timed_out=False,
        truncated=False,
        log=log,
        error_log="",
        duration=0.0,
    )


class QueueTest(DbTestCase):
    def queue(self, n=1):
        add_lab(self.conn)
        ids = []
        for i in range(n):
            email = f"user{i}@example.edu"
            ids.append(
                submissions.submit(
                    self.conn, self.cfg, "am24-1a", email, {"addone.m": GOOD}
                )
            )
        return ids

    def test_batch_is_fifo_and_limited(self):
        ids = self.queue(5)
        self.assertEqual(worker.queued_batch(self.conn, 3), ids[:3])
        self.assertEqual(worker.queued_batch(self.conn, 10), ids)

    def test_claim_is_exclusive(self):
        (sid,) = self.queue()
        self.assertTrue(worker.claim(self.conn, sid))
        self.assertFalse(worker.claim(self.conn, sid))
        self.assertEqual(submissions.get(self.conn, sid)["status"], "running")
        self.assertEqual(worker.queued_batch(self.conn, 10), [])

    def test_no_batch_and_no_claim_in_maintenance_mode(self):
        (sid,) = self.queue()
        db.meta_set(self.conn, "maintenance", "1")
        self.assertEqual(worker.queued_batch(self.conn, 10), [])
        self.assertFalse(worker.claim(self.conn, sid))

    def test_reset_running_requeues_jobs(self):
        (sid,) = self.queue()
        worker.claim(self.conn, sid)
        worker.reset_running(self.conn)
        self.assertEqual(submissions.get(self.conn, sid)["status"], "queued")

    def test_batch_runs_strictly_one_after_another(self):
        ids = self.queue(4)
        seen = {"active": 0, "max_active": 0, "order": [], "running": []}

        def fake_grade(
            cfg, testbench, files, provided=None, helpers="", language=None
        ):
            seen["active"] += 1
            seen["max_active"] = max(seen["max_active"], seen["active"])
            running = self.conn.execute(
                "SELECT id FROM submission WHERE status = 'running'"
            ).fetchall()
            seen["running"].append([r["id"] for r in running])
            seen["order"].append(len(seen["order"]))
            seen["active"] -= 1
            return ok_result("[OK] x\n")

        with mock.patch.object(grader, "grade", fake_grade):
            self.assertEqual(worker.run_batch(self.conn, self.cfg), 4)
        self.assertEqual(seen["max_active"], 1)
        self.assertEqual(
            seen["running"], [[i] for i in ids]
        )  # exactly the own submission is 'running'
        self.assertEqual(
            [submissions.get(self.conn, i)["status"] for i in ids],
            ["done"] * 4,
        )

    def test_batch_size_comes_from_configuration(self):
        ids = self.queue(3)
        self.cfg["grader"]["batch_size"] = 2
        with mock.patch.object(
            grader, "grade", side_effect=grader.GraderError("x")
        ):
            self.assertEqual(worker.run_batch(self.conn, self.cfg), 2)
            self.assertEqual(worker.run_batch(self.conn, self.cfg), 1)
            self.assertEqual(worker.run_batch(self.conn, self.cfg), 0)
        self.assertEqual(
            [submissions.get(self.conn, i)["status"] for i in ids],
            ["error"] * 3,
        )

    def test_stop_finishes_current_submission_and_keeps_the_rest_queued(self):
        ids = self.queue(3)
        stop = threading.Event()

        def fake_grade(
            cfg, testbench, files, provided=None, helpers="", language=None
        ):
            stop.set()
            return ok_result()

        with mock.patch.object(grader, "grade", fake_grade):
            self.assertEqual(worker.run_batch(self.conn, self.cfg, stop), 1)
        self.assertEqual(
            [submissions.get(self.conn, i)["status"] for i in ids],
            ["done", "queued", "queued"],
        )

    def test_submission_deleted_after_fetching_is_skipped(self):
        first, second = self.queue(2)

        def fake_grade(
            cfg, testbench, files, provided=None, helpers="", language=None
        ):
            self.conn.execute("DELETE FROM submission WHERE id = ?", (second,))
            self.conn.commit()
            return ok_result()

        with mock.patch.object(grader, "grade", fake_grade):
            self.assertEqual(worker.run_batch(self.conn, self.cfg), 1)
        self.assertEqual(submissions.get(self.conn, first)["status"], "done")

    def test_conspicuous_words_are_stored_as_flags(self):
        add_lab(self.conn)
        sid = submissions.submit(
            self.conn,
            self.cfg,
            "am24-1a",
            STUDENT,
            {"addone.m": GOOD + b"% exit\nfopen('x');\n"},
        )
        with mock.patch.object(
            grader, "grade", side_effect=lambda *a, **k: ok_result()
        ):
            worker.run_batch(self.conn, self.cfg)
        self.assertEqual(
            submissions.get(self.conn, sid)["flags"], "exit\nfopen"
        )

    def test_lab_helpers_are_passed_to_the_grader(self):
        add_lab(self.conn, helpers="function y = h()\n  y = 1;\nend\n")
        submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        seen = {}

        def fake_grade(
            cfg, testbench, files, provided=None, helpers="", language=None
        ):
            seen["helpers"] = helpers
            return ok_result()

        with mock.patch.object(grader, "grade", fake_grade):
            worker.run_batch(self.conn, self.cfg)
        self.assertIn("function y = h()", seen["helpers"])

    def test_worker_start_removes_run_folders_of_a_dead_worker(self):
        import os

        stale = os.path.join(self.cfg["data_dir"], "run", "run-stale")
        os.makedirs(stale)
        stop = threading.Event()
        stop.set()
        worker.start(self.cfg, stop, poll=0.01).join(timeout=5)
        self.assertFalse(os.path.exists(stale))

    def test_batch_writes_heartbeat(self):
        self.queue()
        with mock.patch.object(
            grader, "grade", side_effect=grader.GraderError("x")
        ):
            worker.run_batch(self.conn, self.cfg)
        self.assertIsNotNone(db.meta_get(self.conn, "worker_heartbeat"))

    def test_grader_failure_marks_error_without_result(self):
        (sid,) = self.queue()
        with mock.patch.object(
            grader, "grade", side_effect=grader.GraderError("bwrap fehlt")
        ):
            self.assertEqual(worker.run_batch(self.conn, self.cfg), 1)
        sub = submissions.get(self.conn, sid)
        self.assertEqual(sub["status"], "error")
        self.assertIsNone(sub["passed"])
        self.assertIn("bwrap fehlt", sub["error_log"])

    def test_a_sandbox_that_does_not_start_is_tried_again(self):
        (sid,) = self.queue()
        ok = grader.GradeResult(
            passed=True,
            checks=[(True, "ok")],
            finished=True,
            exit_code=0,
            timed_out=False,
            truncated=False,
            log="",
            error_log="",
            duration=0.1,
        )
        fail = grader.SandboxError("bwrap: Creating new namespace failed")
        with (
            mock.patch.object(
                grader, "grade", side_effect=[fail, fail, ok]
            ) as fake,
            mock.patch.object(worker.time, "sleep") as sleep,
        ):
            worker.run_batch(self.conn, self.cfg)
        self.assertEqual(fake.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])
        sub = submissions.get(self.conn, sid)
        self.assertEqual(sub["status"], "done")
        self.assertEqual(sub["passed"], 1)

    def test_a_sandbox_that_never_starts_is_an_error_not_a_failure(self):
        (sid,) = self.queue()
        fail = grader.SandboxError("bwrap: Creating new namespace failed")
        with (
            mock.patch.object(grader, "grade", side_effect=fail) as fake,
            mock.patch.object(worker.time, "sleep"),
        ):
            worker.run_batch(self.conn, self.cfg)
        self.assertEqual(fake.call_count, worker.SANDBOX_TRIES)
        sub = submissions.get(self.conn, sid)
        self.assertEqual(sub["status"], "error")
        self.assertIsNone(sub["passed"])
        self.assertIn("Creating new namespace", sub["error_log"])

    def test_missing_files_marks_error(self):
        (sid,) = self.queue()
        import shutil

        shutil.rmtree(
            submissions.submission_dir(
                self.cfg, "am24-1a", "user0@example.edu", 1
            )
        )
        worker.run_batch(self.conn, self.cfg)
        self.assertEqual(submissions.get(self.conn, sid)["status"], "error")

    def test_run_once_without_jobs(self):
        self.assertFalse(worker.run_batch(self.conn, self.cfg))


@unittest.skipUnless(
    MODES, "octave-cli nicht verfügbar (Tests im dev/-Container ausführen)"
)
class ProcessTest(DbTestCase):
    def test_passing_submission(self):
        add_lab(self.conn)
        sid = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        self.assertEqual(worker.run_batch(self.conn, self.cfg), 1)
        sub = submissions.get(self.conn, sid)
        self.assertEqual(
            (sub["status"], sub["passed"], sub["timed_out"]), ("done", 1, 0)
        )
        self.assertEqual(json.loads(sub["checks"]), [[1, "addone ok"]])
        self.assertEqual(sub["finished"], 1)

    def test_failing_submission(self):
        add_lab(self.conn)
        wrong = b"function y = addone(x)\n  y = x + 2;\nend\n"
        sid = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": wrong}
        )
        worker.run_batch(self.conn, self.cfg)
        sub = submissions.get(self.conn, sid)
        self.assertEqual((sub["status"], sub["passed"]), ("done", 0))

    def test_provided_function_is_written_to_a_file_and_used(self):
        add_lab(
            self.conn, required="addone.m\nanderes.m\n", provided=GOOD.decode()
        )
        sid = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"anderes.m": b"x = 1;\n"}
        )
        worker.run_batch(self.conn, self.cfg)
        self.assertEqual(submissions.get(self.conn, sid)["passed"], 1)

    def test_timeout_is_recorded(self):
        add_lab(self.conn)
        self.cfg["grader"]["timeout_seconds"] = 2
        loop = b"function y = addone(x)\n  while true, end\nend\n"
        sid = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": loop}
        )
        worker.run_batch(self.conn, self.cfg)
        sub = submissions.get(self.conn, sid)
        self.assertEqual(
            (sub["status"], sub["passed"], sub["timed_out"]), ("done", 0, 1)
        )

    def test_worker_thread_processes_queue_and_stops(self):
        add_lab(self.conn)
        ids = [
            submissions.submit(
                self.conn,
                self.cfg,
                "am24-1a",
                f"u{i}@example.edu",
                {"addone.m": GOOD},
            )
            for i in range(3)
        ]
        stop = threading.Event()
        thread = worker.start(self.cfg, stop, poll=0.1)
        try:
            for _ in range(200):
                done = [submissions.get(self.conn, i)["status"] for i in ids]
                if done == ["done"] * 3:
                    break
                stop.wait(0.1)
            self.assertEqual(done, ["done"] * 3)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
