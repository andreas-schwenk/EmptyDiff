# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import unittest
from datetime import datetime, timezone

from helpers import GAUSS_FUNCTION, GOOD, OTHER, STUDENT, DbTestCase, add_lab
from emptydiff.core import credit, labs, submissions, util

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

FORM = {
    "description": "Text",
    "due_at": "2027-03-01T23:59",
    "required_files": "a.m\nb.m",
    "show_report": "1",
    "testbench": "check(true, 'ok', 'falsch');\n",
    "helpers": "",
}


class DueTest(unittest.TestCase):
    def test_end_of_minute_in_berlin(self):
        # winter: UTC+1 -> 23:59:59 local = 22:59:59 UTC
        self.assertEqual(
            labs.parse_due("2027-01-15T23:59"), "2027-01-15T22:59:59Z"
        )
        # summer: UTC+2
        self.assertEqual(
            labs.parse_due("2027-07-15T23:59"), "2027-07-15T21:59:59Z"
        )

    def test_round_trip(self):
        self.assertEqual(
            labs.due_for_input(labs.parse_due("2027-07-15T08:30")),
            "2027-07-15T08:30",
        )

    def test_invalid(self):
        for bad in ("", "morgen", "2027-13-01T10:00", "2027-01-15"):
            with self.subTest(bad=bad), self.assertRaises(labs.LabError):
                labs.parse_due(bad)

    def test_shift_keeps_local_time_across_dst(self):
        # 23:59:59 Berlin in winter + 182 days (daylight saving time):
        # stays 23:59:59 local
        shifted = labs.shift_due("2027-01-15T22:59:59Z", 182)
        self.assertEqual(util.local_str(shifted).split()[1], "23:59")


class FieldsTest(unittest.TestCase):
    def test_valid(self):
        f = labs.clean_fields(FORM)
        self.assertEqual(f["required_files"], "a.m\nb.m")
        self.assertEqual(f["show_report"], 1)
        self.assertNotIn(
            "active", f
        )  # visibility is not a field of the lab form

    def test_checkboxes_default_to_off(self):
        f = labs.clean_fields(
            {k: v for k, v in FORM.items() if k != "show_report"}
        )
        self.assertEqual(f["show_report"], 0)
        self.assertNotIn(
            "active", labs.clean_fields({**FORM, "active": "1"})
        )  # a posted value is ignored

    def test_required_accepts_commas_and_dedupes(self):
        self.assertEqual(
            labs.parse_required("a.m, b.m\n\na.m"), ["a.m", "b.m"]
        )

    def test_invalid_inputs(self):
        for key, value in (
            ("required_files", "../x.m"),
            ("required_files", "a b.m"),
            ("testbench", "  \n"),
            ("due_at", ""),
        ):
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(labs.LabError),
            ):
                labs.clean_fields({**FORM, key: value})

    def test_crlf_is_normalized(self):
        f = labs.clean_fields(
            {**FORM, "testbench": "a\r\nb\r\n", "description": "x\r\ny"}
        )
        self.assertEqual(
            (f["testbench"], f["description"]), ("a\nb\n", "x\ny")
        )

    def test_warnings(self):
        self.assertEqual(labs.warnings_for(FORM["testbench"]), [])
        self.assertEqual(labs.warnings_for("disp('x')"), ["warn.tb_no_check"])
        self.assertEqual(
            labs.warnings_for(
                "x = h();",
                "function y = h()\n  check(true, 'a', 'b');\n  y = 1;\nend\n",
            ),
            [],
        )

    def test_old_style_testbench_is_refused(self):
        for old in (
            "function [] = testbench()\n  check(true, 'a', 'b');\nend\n",
            "  function x()\nend",
            "a = 1;\nfunction y = f()\nend",
        ):
            with (
                self.subTest(old=old),
                self.assertRaises(labs.LabError) as ctx,
            ):
                labs.clean_fields({**FORM, "testbench": old})
            self.assertEqual(ctx.exception.key, "ln.octave.err_tb_function")

    def test_helpers_are_kept_and_may_contain_functions(self):
        f = labs.clean_fields(
            {
                **FORM,
                "helpers": "\r\n function y = h()\r\n y = 1;\r\nend\r\n\r\n",
            }
        )
        self.assertEqual(f["helpers"], "function y = h()\n y = 1;\nend\n")
        self.assertEqual(labs.clean_fields(FORM)["helpers"], "")

    def test_at_least_one_required_file_and_no_reserved_name(self):
        for bad in ("", "  \n", "testbench.m"):
            with self.subTest(bad=bad), self.assertRaises(labs.LabError):
                labs.clean_fields({**FORM, "required_files": bad})


class LabCrudTest(DbTestCase):
    def test_create_update_delete(self):
        labs.create_course(self.conn, "am27")
        self.assertEqual(
            labs.create(self.conn, "am27", "1a", labs.clean_fields(FORM)),
            "am27-1a",
        )
        lab = labs.get(self.conn, "am27-1a")
        self.assertEqual(
            (lab["course"], lab["active"]), ("am27", 0)
        )  # new labs are hidden
        labs.set_active(self.conn, "am27-1a", True)
        labs.update(
            self.conn,
            "am27-1a",
            labs.clean_fields({**FORM, "description": "Neu"}),
        )
        lab = labs.get(self.conn, "am27-1a")
        self.assertEqual(
            (lab["description"], lab["active"]), ("Neu", 1)
        )  # saving does not change the visibility
        self.assertNotIn(
            "max_attempts", lab.keys()
        )  # the limit is in the configuration, not per lab
        self.assertEqual(labs.delete(self.conn, self.cfg, "am27-1a"), 0)
        self.assertIsNone(labs.get(self.conn, "am27-1a"))

    def test_invalid_and_duplicate_identifiers(self):
        labs.create_course(self.conn, "am27")
        for bad in ("", "1a/../x", "a b", "1-a", "x" * 17):
            with self.subTest(bad=bad), self.assertRaises(labs.LabError):
                labs.create(self.conn, "am27", bad, labs.clean_fields(FORM))
        with self.assertRaises(labs.LabError):  # unknown course
            labs.create(self.conn, "am99", "1a", labs.clean_fields(FORM))
        labs.create(self.conn, "am27", "1a", labs.clean_fields(FORM))
        with self.assertRaises(labs.LabError):
            labs.create(self.conn, "am27", "1a", labs.clean_fields(FORM))

    def test_courses_can_be_created_and_deleted_when_empty(self):
        labs.create_course(self.conn, "am27")
        self.assertTrue(labs.course_exists(self.conn, "am27"))
        for bad in ("", "am27", "AM28", "am", "am2027", "x/../y"):
            with (
                self.subTest(bad=bad),
                self.assertRaises(labs.LabError),
            ):  # "am27" exists already
                labs.create_course(self.conn, bad)
        self.assertIn(
            ("am27", []),
            [(c["course"], c["labs"]) for c in labs.overview(self.conn)],
        )  # empty courses are listed
        labs.create(self.conn, "am27", "1a", labs.clean_fields(FORM))
        with self.assertRaises(labs.LabError):
            labs.delete_course(self.conn, "am27")  # still has a lab
        labs.delete(self.conn, self.cfg, "am27-1a")
        self.assertTrue(
            labs.course_exists(self.conn, "am27")
        )  # the course stays when its last lab goes
        labs.delete_course(self.conn, "am27")
        self.assertFalse(labs.course_exists(self.conn, "am27"))
        with self.assertRaises(labs.LabError):
            labs.delete_course(self.conn, "am27")

    def test_inserting_a_lab_directly_keeps_its_course(self):
        add_lab(self.conn, "am31-1a")
        self.assertTrue(labs.course_exists(self.conn, "am31"))

    def test_delete_removes_lab_with_all_submissions_and_files(self):
        add_lab(self.conn)
        add_lab(self.conn, "am24-1b")
        first = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE id = ?", (first,)
        )  # otherwise "is being checked" blocks
        self.conn.commit()
        submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        submissions.submit(
            self.conn, self.cfg, "am24-1b", STUDENT, {"addone.m": GOOD}
        )
        folder = os.path.dirname(
            os.path.dirname(
                submissions.submission_dir(self.cfg, "am24-1a", STUDENT, 1)
            )
        )
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(labs.delete(self.conn, self.cfg, "am24-1a"), 2)
        self.assertIsNone(labs.get(self.conn, "am24-1a"))
        self.assertFalse(os.path.exists(folder))
        left = self.conn.execute(
            "SELECT DISTINCT lab FROM submission"
        ).fetchall()
        self.assertEqual(
            [r[0] for r in left], ["am24-1b"]
        )  # other labs stay untouched
        self.assertTrue(
            os.path.isdir(
                os.path.dirname(
                    os.path.dirname(
                        submissions.submission_dir(
                            self.cfg, "am24-1b", STUDENT, 1
                        )
                    )
                )
            )
        )

    def test_delete_refused_while_a_check_is_running(self):
        add_lab(self.conn)
        sub_id = submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        self.conn.execute(
            "UPDATE submission SET status = 'running' WHERE id = ?", (sub_id,)
        )
        self.conn.commit()
        with self.assertRaises(labs.LabError):
            labs.delete(self.conn, self.cfg, "am24-1a")
        self.assertIsNotNone(labs.get(self.conn, "am24-1a"))

    def test_provided_functions_are_stored_with_the_lab(self):
        labs.create_course(self.conn, "am27")
        labs.create(
            self.conn,
            "am27",
            "1a",
            labs.clean_fields(
                {**FORM, "provided": "\r\n" + GAUSS_FUNCTION + "\r\n"}
            ),
        )
        self.assertEqual(
            labs.get(self.conn, "am27-1a")["provided"], GAUSS_FUNCTION
        )
        labs.update(
            self.conn, "am27-1a", labs.clean_fields({**FORM, "provided": ""})
        )
        self.assertEqual(labs.get(self.conn, "am27-1a")["provided"], "")

    def test_provided_functions_are_checked_when_saving(self):
        for bad, key in (
            ("x = 1;\n", "ln.octave.err_provided_no_function"),
            ("x = 1;\n" + GAUSS_FUNCTION, "ln.octave.err_provided_outside"),
            (GAUSS_FUNCTION * 2, "ln.octave.err_provided_duplicate"),
            ("function testbench()\nend\n", "ln.octave.err_provided_name"),
            ("function emptydiff_x()\nend\n", "ln.octave.err_provided_name"),
            ("function = 3\n", "ln.octave.err_provided_name"),
            ("function f()\nend\n" + "% x" * 100_000, "err.lab.provided_long"),
        ):
            with (
                self.subTest(bad=bad[:30]),
                self.assertRaises(labs.LabError) as ctx,
            ):
                labs.clean_fields({**FORM, "provided": bad})
            self.assertEqual(ctx.exception.key, key)

    def test_delete_removes_the_provided_functions_with_the_lab(self):
        labs.create_course(self.conn, "am27")
        labs.create(
            self.conn,
            "am27",
            "1a",
            labs.clean_fields({**FORM, "provided": GAUSS_FUNCTION}),
        )
        labs.delete(self.conn, self.cfg, "am27-1a")
        self.assertIsNone(labs.get(self.conn, "am27-1a"))


class ActiveTest(DbTestCase):
    def setUp(self):
        super().setUp()
        add_lab(self.conn, "am26-1a", active=1)
        add_lab(self.conn, "am26-1b", active=0)
        add_lab(self.conn, "am25-1a", active=0)

    def active(self):
        return {
            r["name"]
            for r in self.conn.execute("SELECT name FROM lab WHERE active = 1")
        }

    def test_set_active(self):
        labs.set_active(self.conn, "am26-1b", True)
        self.assertEqual(self.active(), {"am26-1a", "am26-1b"})
        labs.set_active(self.conn, "am26-1a", False)
        self.assertEqual(self.active(), {"am26-1b"})
        with self.assertRaises(labs.LabError):
            labs.set_active(self.conn, "am99-1a", True)

    def test_students_see_exactly_the_active_labs(self):
        def names():
            rows = submissions.overview_for_user(self.conn, STUDENT)
            return [r["lab"]["name"] for r in rows]

        self.assertEqual(names(), ["am26-1a"])
        labs.set_active(self.conn, "am25-1a", True)
        self.assertEqual(names(), ["am25-1a", "am26-1a"])
        labs.set_active(self.conn, "am26-1a", False)
        labs.set_active(self.conn, "am25-1a", False)
        self.assertEqual(names(), [])

    def test_overview_groups_courses_newest_first(self):
        result = labs.overview(self.conn)
        self.assertEqual([c["course"] for c in result], ["am26", "am25"])
        self.assertEqual(
            [(c["course"], c["active"], len(c["labs"])) for c in result],
            [("am26", 1, 2), ("am25", 0, 1)],
        )

    def test_overview_counts(self):
        submissions.submit(
            self.conn,
            self.cfg,
            "am26-1a",
            STUDENT,
            {"addone.m": GOOD},
            now=NOW,
        )
        submissions.submit(
            self.conn, self.cfg, "am26-1a", OTHER, {"addone.m": GOOD}, now=NOW
        )
        e = labs.overview(self.conn)[0]["labs"][0]
        self.assertEqual(
            (e["lab"]["name"], e["students"], e["submissions"]),
            ("am26-1a", 2, 2),
        )

    def test_overview_counts_students_of_the_lab_and_of_the_course(self):
        add_lab(self.conn, "am26-1c")
        for lab, email in (
            ("am26-1a", STUDENT),
            ("am26-1a", OTHER),
            ("am26-1a", STUDENT),  # a second attempt: still one student
            ("am26-1b", STUDENT),
            ("am26-1c", "dozent@example.edu"),  # a lecturer trying it out
        ):
            sid = submissions.submit(
                self.conn,
                self.cfg,
                lab,
                email,
                {"addone.m": GOOD},
                now=NOW,
                preview=True,  # also the hidden lab 1b
            )
            self.conn.execute(
                "UPDATE submission SET status = 'done' WHERE id = ?", (sid,)
            )
        self.conn.commit()
        course = labs.overview(self.conn, exclude=("dozent@example.edu",))[0]
        self.assertEqual(course["course"], "am26")
        self.assertEqual(course["students"], 2)  # not the lecturer
        by_lab = {e["lab"]["name"]: e for e in course["labs"]}
        self.assertEqual(
            [
                (n, by_lab[n]["students"], by_lab[n]["submissions"])
                for n in ("am26-1a", "am26-1b", "am26-1c")
            ],
            [("am26-1a", 2, 3), ("am26-1b", 1, 1), ("am26-1c", 0, 1)],
        )
        # without the exclusion the lecturer counts like anyone else
        everyone = labs.overview(self.conn)[0]
        self.assertEqual(everyone["students"], 3)


class CloneTest(DbTestCase):
    def test_clone(self):
        add_lab(
            self.conn,
            "am26-1a",
            due="2027-01-15T22:59:59Z",
            provided=GAUSS_FUNCTION,
            helpers="function y = h()\nend\n",
        )
        add_lab(self.conn, "am26-1b", due="2027-02-15T22:59:59Z")
        names = labs.clone_course(self.conn, "am26", "am27", 365)
        self.assertEqual(names, ["am27-1a", "am27-1b"])
        new = labs.get(self.conn, "am27-1a")
        self.assertEqual(
            (new["course"], new["active"], new["due_at"]),
            ("am27", 0, "2028-01-15T22:59:59Z"),
        )
        self.assertEqual(
            new["testbench"], labs.get(self.conn, "am26-1a")["testbench"]
        )
        self.assertEqual(
            (new["provided"], new["helpers"]),
            (GAUSS_FUNCTION, "function y = h()\nend\n"),
        )
        self.assertEqual(labs.get(self.conn, "am27-1b")["provided"], "")
        self.assertEqual(
            labs.get(self.conn, "am26-1a")["active"], 1
        )  # source stays unchanged

    def test_clone_errors_leave_nothing_behind(self):
        add_lab(self.conn, "am26-1a")
        add_lab(self.conn, "am27-1a")
        for src, dst in (
            ("am26", "am27"),
            ("am26", "AM28"),
            ("am26", "x"),
            ("am99", "am28"),
        ):
            with self.subTest(dst=dst), self.assertRaises(labs.LabError):
                labs.clone_course(self.conn, src, dst)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM lab").fetchone()[0], 2
        )


class SubmissionAdminTest(DbTestCase):
    def setUp(self):
        super().setUp()
        add_lab(self.conn)

    def make(self, email=STUDENT, status="done"):
        sub_id = submissions.submit(
            self.conn, self.cfg, "am24-1a", email, {"addone.m": GOOD}, now=NOW
        )
        self.conn.execute(
            "UPDATE submission SET status = ?, passed = 1, log = "
            "'x' WHERE id = ?",
            (status, sub_id),
        )
        self.conn.commit()
        return sub_id

    def test_delete_removes_row_and_files(self):
        sub_id = self.make()
        sub = submissions.get(self.conn, sub_id)
        directory = submissions.submission_dir(
            self.cfg, "am24-1a", STUDENT, sub["attempt"]
        )
        labs.delete_submission(self.conn, self.cfg, sub_id)
        self.assertIsNone(submissions.get(self.conn, sub_id))
        self.assertFalse(os.path.exists(directory))

    def test_delete_refused_while_running(self):
        sub_id = self.make(status="running")
        with self.assertRaises(labs.LabError):
            labs.delete_submission(self.conn, self.cfg, sub_id)
        self.assertIsNotNone(submissions.get(self.conn, sub_id))

    def test_deleting_last_attempt_frees_the_attempt(self):
        self.make()
        second = self.make()
        labs.delete_submission(self.conn, self.cfg, second)
        third = self.make()
        self.assertEqual(submissions.get(self.conn, third)["attempt"], 2)

    def test_regrade_requeues_and_clears_result(self):
        sub_id = self.make()
        labs.regrade_submission(self.conn, sub_id)
        sub = submissions.get(self.conn, sub_id)
        self.assertEqual(
            (sub["status"], sub["passed"], sub["log"]), ("queued", None, None)
        )
        with self.assertRaises(labs.LabError):  # already in the queue
            labs.regrade_submission(self.conn, sub_id)

    def test_saving_the_lab_with_a_moved_deadline_updates_late(self):
        sub_id = self.make()
        self.conn.execute("UPDATE submission SET late = 1")
        self.conn.commit()
        fields = labs.clean_fields(FORM)  # deadline in 2027: not late
        labs.update(self.conn, "am24-1a", fields)
        self.assertEqual(submissions.get(self.conn, sub_id)["late"], 0)
        labs.update(
            self.conn,
            "am24-1a",
            {**fields, "due_at": "2020-01-01T10:00:00Z"},
        )
        self.assertEqual(submissions.get(self.conn, sub_id)["late"], 1)

    def test_regrade_takes_the_current_deadline(self):
        sub_id = self.make()
        self.conn.execute("UPDATE submission SET late = 1")
        self.conn.commit()
        labs.regrade_submission(self.conn, sub_id)
        self.assertEqual(submissions.get(self.conn, sub_id)["late"], 0)


class CreditTest(DbTestCase):
    def setUp(self):
        super().setUp()
        add_lab(self.conn, "am26-1a")
        add_lab(self.conn, "am26-1b", show_report=0)
        add_lab(self.conn, "am25-1a")

    def sub(self, lab, email, status="done", passed=1, **kw):
        sub_id = submissions.submit(
            self.conn, self.cfg, lab, email, {"addone.m": GOOD}, now=NOW
        )
        self.conn.execute(
            "UPDATE submission SET status = ?, passed = ? WHERE id = ?",
            (status, passed, sub_id),
        )
        for key, value in kw.items():
            self.conn.execute(
                f"UPDATE submission SET {key} = ? WHERE id = ?",
                (value, sub_id),
            )
        self.conn.commit()
        return sub_id

    def test_last_submission_counts(self):
        self.sub("am26-1a", STUDENT, passed=1)
        self.sub("am26-1a", STUDENT, passed=0)
        _, rows = credit.matrix(self.conn, "am26")
        self.assertEqual(
            [
                (r["email"], r["cells"][0]["state"], r["cells"][0]["attempts"])
                for r in rows
            ],
            [(STUDENT, "fail", 2)],
        )

    def test_states(self):
        self.sub("am26-1a", STUDENT, passed=1)
        self.sub("am26-1b", STUDENT, passed=1)
        self.sub("am26-1a", OTHER, status="queued", passed=None)
        self.sub("am26-1b", OTHER, status="error", passed=None)
        labs_, rows = credit.matrix(self.conn, "am26")
        by = {r["email"]: [c["state"] for c in r["cells"]] for r in rows}
        self.assertEqual(by[STUDENT], ["pass", "received"])
        self.assertEqual(by[OTHER], ["pending", "error"])
        self.assertEqual(
            [lab["name"] for lab in labs_], ["am26-1a", "am26-1b"]
        )

    def test_incomplete_does_not_pass(self):
        self.sub("am26-1a", STUDENT, passed=1, complete=0)
        _, rows = credit.matrix(self.conn, "am26")
        self.assertEqual(rows[0]["cells"][0]["state"], "fail")
        self.assertEqual(rows[0]["passed"], 0)

    def test_only_students_of_the_course(self):
        self.sub("am25-1a", STUDENT)
        _, rows = credit.matrix(self.conn, "am26")
        self.assertEqual(rows, [])
        _, rows = credit.matrix(self.conn, "am25")
        self.assertEqual([r["email"] for r in rows], [STUDENT])

    def test_late_flag_and_csv(self):
        self.sub("am26-1a", STUDENT, passed=1, late=1)
        self.sub("am26-1a", OTHER, passed=0)
        text = credit.to_csv(*credit.matrix(self.conn, "am26"), lang="DE")
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "email,am26-1a,am26-1b,bestanden")
        self.assertEqual(lines[1], f"{OTHER},nicht bestanden,,0")
        self.assertEqual(lines[2], f"{STUDENT},bestanden (verspätet),,1")

    def test_unknown_course_is_empty(self):
        self.assertEqual(credit.matrix(self.conn, "am99")[1], [])


if __name__ == "__main__":
    unittest.main()
