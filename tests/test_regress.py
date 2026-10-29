# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import unittest

from helpers import DbTestCase, add_lab
from emptydiff.core import regress
from test_grader import GOOD, MODES, make_cfg

WRONG = b"function y = addone(x)\n  y = x + 2;\nend\n"


def make_revision(
    root, lab, student, rev, student_code, old_passed, extra=None
):
    path = os.path.join(root, lab, student, rev)
    os.makedirs(path)
    files = {
        "addone.m": student_code,
        "testbench.m": b"old testbench",
        "log.txt": b"old log",
        "error.txt": b"",
        **(extra or {}),
    }
    for name, content in files.items():
        with open(os.path.join(path, name), "wb") as f:
            f.write(content)
    if old_passed:
        open(os.path.join(path, "__passed_lab"), "w").close()


class FindCasesTest(unittest.TestCase):
    def test_find_cases_anonymizes_and_filters(self):
        with tempfile.TemporaryDirectory() as root:
            make_revision(
                root,
                "am24-1a",
                "max.muster@student.example.edu",
                "Rev000",
                GOOD,
                True,
            )
            make_revision(
                root,
                "am24-1a",
                "max.muster@student.example.edu",
                "Rev001",
                GOOD,
                True,
            )
            os.makedirs(
                os.path.join(
                    root,
                    "am24-1a",
                    "max.muster@student.example.edu",
                    "kein-rev",
                )
            )
            os.makedirs(os.path.join(root, ".versteckt"))
            cases = list(regress.find_cases(root))
        self.assertEqual([c.rev for c in cases], ["Rev000", "Rev001"])
        self.assertEqual(len(cases[0].student), 8)
        self.assertNotIn("max", cases[0].student)

    def test_load_case_takes_only_required_files(self):
        with tempfile.TemporaryDirectory() as root:
            make_revision(
                root,
                "am24-1a",
                "s",
                "Rev000",
                GOOD,
                True,
                extra={"other.m": b"x = 1;\n"},
            )
            case = next(regress.find_cases(root))
            data, reason = regress.load_case(case, ["addone.m", "missing.m"])
        self.assertIsNone(reason)
        self.assertEqual(list(data["files"]), ["addone.m"])
        self.assertTrue(data["old_passed"])

    def test_load_case_skips_revisions_without_required_files(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(
                os.path.join(root, "am24-1a", "s", "Rev000")
            )  # empty (e.g. too many attempts)
            case = next(regress.find_cases(root))
            self.assertEqual(
                regress.load_case(case, ["addone.m"])[1], "no submission files"
            )


@unittest.skipUnless(
    MODES, "octave-cli nicht verfügbar (Tests im dev/-Container ausführen)"
)
class RunTest(DbTestCase):
    def test_run_compares_with_recorded_verdicts(self):
        add_lab(self.conn)
        with tempfile.TemporaryDirectory() as root:
            make_revision(
                root, "am24-1a", "a", "Rev000", GOOD, True
            )  # matches
            make_revision(
                root, "am24-1a", "b", "Rev000", WRONG, False
            )  # matches
            make_revision(
                root, "am24-1a", "c", "Rev000", WRONG, True
            )  # deviation
            make_revision(
                root, "am99-1a", "d", "Rev000", GOOD, True
            )  # lab not in the database
            report = regress.run(self.cfg, self.conn, root)
        self.assertEqual(len(report.outcomes), 3)
        self.assertEqual(report.skipped, {"lab not in database": 1})
        self.assertEqual(len(report.deviations), 1)
        self.assertEqual(report.deviations[0].old_passed, True)
        self.assertEqual(report.deviations[0].new_passed, False)

    def test_provided_functions_and_labs_filter(self):
        add_lab(self.conn, required="addone.m\n", provided=GOOD.decode())
        add_lab(self.conn, "am24-1b")
        with tempfile.TemporaryDirectory() as root:
            make_revision(root, "am24-1a", "a", "Rev000", GOOD, True)
            make_revision(root, "am24-1b", "a", "Rev000", GOOD, True)
            report = regress.run(self.cfg, self.conn, root, labs={"am24-1a"})
        self.assertEqual([o.case.lab for o in report.outcomes], ["am24-1a"])
        self.assertEqual(report.deviations, [])


if __name__ == "__main__":
    unittest.main()
