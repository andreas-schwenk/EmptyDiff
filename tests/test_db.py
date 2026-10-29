# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import unittest

from emptydiff.core import db


class DbTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.sqlite")

    def tearDown(self):
        self.dir.cleanup()

    def tables(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}

    def test_init_creates_schema_and_is_idempotent(self):
        conn = db.init_db(self.path)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            len(db.MIGRATIONS),
        )
        self.assertTrue(
            {"lab", "submission", "login_code"} <= self.tables(conn)
        )
        conn.close()
        db.init_db(self.path).close()  # second run without errors

    def test_v1_database_is_migrated_to_current_version(self):
        conn = db.connect(self.path)
        db.MIGRATIONS[0](conn)  # state of version 1
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        conn = db.init_db(self.path)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            len(db.MIGRATIONS),
        )
        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(submission)")
        }
        self.assertTrue({"missing_files", "timed_out", "truncated"} <= columns)
        conn.close()

    def test_database_from_the_dropped_extra_attempts_feature_is_cleaned_up(
        self,
    ):
        conn = db.connect(self.path)
        for migration in db.MIGRATIONS[
            :4
        ]:  # local state with table attempt_grant (v3) and remember_token (v4)
            migration(conn)
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        self.assertIn("attempt_grant", self.tables(conn))
        conn.close()
        conn = db.init_db(self.path)
        self.assertNotIn("attempt_grant", self.tables(conn))
        self.assertIn("remember_token", self.tables(conn))
        conn.close()

    def test_v9_database_gets_its_courses(self):
        conn = db.connect(self.path)
        for migration in db.MIGRATIONS[:9]:
            migration(conn)
        for name in ("am24-1a", "am24-1b", "am25-1a"):
            conn.execute(
                "INSERT INTO lab (name, course, description, due_at, "
                "required_files, show_report, testbench, active) "
                "VALUES (?, ?, '', '2030-01-01T00:00:00Z', '', 1, 'x', 0)",
                (name, name.split("-")[0]),
            )
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()
        conn = db.init_db(self.path)
        self.assertEqual(
            [
                r[0]
                for r in conn.execute("SELECT name FROM course ORDER BY name")
            ],
            ["am24", "am25"],
        )
        conn.execute(
            "INSERT INTO lab (name, course, description, due_at, "
            "required_files, show_report, testbench, active) "
            "VALUES ('am26-1a', 'am26', '', "
            "'2030-01-01T00:00:00Z', '', 1, 'x', 0)"
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM course WHERE name = 'am26'"
            ).fetchone()[0],
            1,
        )  # trigger
        conn.close()

    def test_v10_database_gets_helpers_checks_and_flags(self):
        conn = db.connect(self.path)
        for migration in db.MIGRATIONS[:10]:
            migration(conn)
        conn.execute(
            "INSERT INTO lab (name, course, description, due_at, "
            "required_files, show_report, testbench, active) "
            "VALUES ('am24-1a', 'am24', '', "
            "'2030-01-01T00:00:00Z', 'a.m', 1, 'x', 0)"
        )
        for n, status in enumerate(("done", "queued"), start=1):
            conn.execute(
                "INSERT INTO submission (lab, email, attempt, "
                "created_at, status) "
                "VALUES ('am24-1a', 'a@example.edu', ?, "
                "'2030-01-01T00:00:00Z', ?)",
                (n, status),
            )
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        conn.close()
        conn = db.init_db(self.path)
        self.assertEqual(
            conn.execute("SELECT helpers FROM lab").fetchone()[0], ""
        )
        rows = conn.execute(
            "SELECT status, checks, finished, flags FROM "
            "submission ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in rows],
            [("done", "[]", 1, ""), ("queued", "[]", 0, "")],
        )
        conn.close()

    def test_v11_database_turns_provided_files_into_provided_functions(self):
        conn = db.connect(self.path)
        for migration in db.MIGRATIONS[:11]:
            migration(conn)
        for name in ("am24-2b", "am24-1a"):
            conn.execute(
                "INSERT INTO lab (name, course, description, due_at, "
                "required_files, show_report, testbench, active) "
                "VALUES (?, 'am24', '', '2030-01-01T00:00:00Z', "
                "'a.m', 1, 'x', 0)",
                (name,),
            )
        files = {
            (
                "am24-2b",
                "gauss.m",
            ): b"function [x]=gauss(A,b)\r\n    x = A \\ b;\r\nend",
            ("am24-2b", "extra.m"): b"function y = extra(x)\n  y = x;\nend\n",
            ("am24-2b", "data.csv"): b"1,2,3\n",  # not a function: dropped
            ("am24-2b", "script.m"): b"x = 1;\n",  # a script: dropped
            # name differs from the file: dropped
            (
                "am24-2b",
                "wrongname.m",
            ): b"function y = other(x)\nend\n",
            ("am24-2b", "two.m"): b"function a()\nend\nfunction b()\nend\n",
        }  # two functions: dropped
        for (lab, name), content in files.items():
            conn.execute(
                "INSERT INTO lab_file (lab, name, content) VALUES (?, ?, ?)",
                (lab, name, content),
            )
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
        conn.close()
        conn = db.init_db(self.path)
        self.assertEqual(
            conn.execute(
                "SELECT provided FROM lab WHERE name = 'am24-2b'"
            ).fetchone()[0],
            "function y = extra(x)\n  y = x;\nend\n\nfunction "
            "[x]=gauss(A,b)\n    x = A \\ b;\nend\n",
        )
        self.assertEqual(
            conn.execute(
                "SELECT provided FROM lab WHERE name = 'am24-1a'"
            ).fetchone()[0],
            "",
        )
        self.assertNotIn("lab_file", self.tables(conn))
        from emptydiff.core import provided

        self.assertEqual(
            provided.names(
                "octave",
                conn.execute(
                    "SELECT provided FROM lab WHERE name = 'am24-2b'"
                ).fetchone()[0],
            ),
            ["extra.m", "gauss.m"],
        )
        conn.close()

    def test_v7_database_loses_outbox_and_write_only_columns(self):
        conn = db.connect(self.path)
        for migration in db.MIGRATIONS[:7]:
            migration(conn)
        conn.execute(
            "CREATE TABLE outbox (id INTEGER PRIMARY KEY, body TEXT)"
        )  # earlier dev databases
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        self.assertIn(
            "exit_code",
            {r["name"] for r in conn.execute("PRAGMA table_info(submission)")},
        )
        conn.close()
        conn = db.init_db(self.path)
        self.assertNotIn("outbox", self.tables(conn))
        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(submission)")
        }
        self.assertFalse({"exit_code", "duration", "finished_at"} & columns)
        self.assertTrue({"timed_out", "truncated", "missing_files"} <= columns)
        self.assertNotIn(
            "note", {r["name"] for r in conn.execute("PRAGMA table_info(lab)")}
        )
        conn.close()
