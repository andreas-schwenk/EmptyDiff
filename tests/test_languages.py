# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""The language layer: the adapters, the registry, the configuration and
what the rest of the system does with a language (labs, submissions,
backup). A toy language stands in for a second one.
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import GOOD, STUDENT, DbTestCase, add_lab
from emptydiff.core import (
    backup,
    config,
    db,
    grader,
    labs,
    languages,
    provided,
    submissions,
)


class ToyLanguage(languages.Language):
    """A second language that does nothing real: `.toy` files, words
    instead of statements."""

    id = "toy"
    name = "Toy"
    extension = ".toy"
    default_command = "toy-run"
    default_suspicious_words = ("forbidden",)
    testbench_file = "bench.toy"
    selftest_body = "check 1\n"

    def files(self, body, helpers, results_file, token):
        return {
            self.testbench_file: f"{body}\n{helpers}\n# "
            f"{results_file} {token}\n".encode("utf-8")
        }

    def argv(self, command):
        return [command, self.testbench_file]

    def check_testbench(self, body):
        if "def " in body:
            raise languages.TestbenchError("ln.toy.err_def")

    def split_provided(self, text):
        return [
            (line.split()[1], line)
            for line in text.splitlines()
            if line.startswith("def ")
        ]


def with_toy(test):
    """Register the toy language for the duration of a test and let the
    server offer it."""

    def run(self, *args, **kwargs):
        with mock.patch.dict(languages._REGISTRY, {"toy": ToyLanguage()}):
            self.cfg["languages"]["enabled"] = ["octave", "toy"]
            self.cfg["languages"]["toy"] = {
                "command": "toy-run",
                "suspicious_words": ["forbidden"],
            }
            return test(self, *args, **kwargs)

    run.__name__ = test.__name__
    return run


class RegistryTest(unittest.TestCase):
    def test_octave_is_registered_and_the_default(self):
        self.assertIn("octave", languages.ids())
        self.assertEqual(languages.DEFAULT, "octave")
        self.assertEqual(languages.get("octave").extension, ".m")

    def test_unknown_language(self):
        with self.assertRaises(languages.UnknownLanguage) as ctx:
            languages.get("nonsense")
        self.assertEqual(
            (ctx.exception.key, ctx.exception.params),
            ("err.lang.unknown", {"language": "nonsense"}),
        )

    def test_defaults_come_from_the_adapters(self):
        section = languages.defaults()
        self.assertEqual(section["enabled"], ["octave"])
        self.assertEqual(section["octave"]["command"], "octave-cli")
        self.assertIn("exit", section["octave"]["suspicious_words"])


class ContractTest(unittest.TestCase):
    """What the rest of the system relies on, for every registered
    language."""

    def test_every_adapter_fills_in_the_contract(self):
        for language_id in languages.ids():
            adapter = languages.get(language_id)
            with self.subTest(language=language_id):
                self.assertEqual(adapter.id, language_id)
                for attribute in (
                    "name",
                    "extension",
                    "default_command",
                    "testbench_file",
                    "selftest_body",
                ):
                    self.assertTrue(getattr(adapter, attribute), attribute)
                self.assertTrue(adapter.extension.startswith("."))
                self.assertTrue(
                    grader.FILENAME_RE.match(adapter.testbench_file)
                )

    def test_testbench_files_carry_the_results_file_and_the_token(self):
        for language_id in languages.ids():
            adapter = languages.get(language_id)
            with self.subTest(language=language_id):
                files = adapter.files(
                    adapter.selftest_body, "", "results-x.txt", "token-y"
                )
                self.assertIn(adapter.testbench_file, files)
                text = files[adapter.testbench_file].decode("utf-8")
                self.assertIn("results-x.txt", text)
                self.assertIn("token-y", text)
                self.assertEqual(adapter.argv("interpreter")[0], "interpreter")

    def test_the_selftest_body_and_an_empty_provided_text_are_fine(self):
        for language_id in languages.ids():
            adapter = languages.get(language_id)
            with self.subTest(language=language_id):
                adapter.check_testbench(adapter.selftest_body)
                self.assertTrue(adapter.has_check(adapter.selftest_body))
                self.assertEqual(adapter.split_provided(""), [])
                self.assertEqual(adapter.provided_files(""), {})

    def test_tokens_never_lose_text(self):
        for language_id in languages.ids():
            adapter = languages.get(language_id)
            with self.subTest(language=language_id):
                text = adapter.selftest_body + "x = 'a';\n% comment\n"
                self.assertEqual(
                    "".join(t for _, t in adapter.tokens(text)), text
                )


class ConfigTest(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8"
        )
        self.addCleanup(os.unlink, handle.name)
        handle.write(text)
        handle.close()
        return handle.name

    def load(self, extra):
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": "1"}):
            return config.load(self.write(extra))

    def test_defaults(self):
        cfg = self.load("")
        self.assertEqual(cfg["languages"]["enabled"], ["octave"])
        self.assertEqual(cfg["languages"]["octave"]["command"], "octave-cli")

    def test_a_section_overrides_the_command(self):
        self.assertEqual(
            self.load('[languages.octave]\ncommand = "octave"\n')["languages"][
                "octave"
            ]["command"],
            "octave",
        )

    def test_enabled_must_be_a_list_of_known_languages(self):
        for bad in (
            '"octave"',
            "[]",
            "[1]",
            '["nonsense"]',
            '["octave", "nonsense"]',
        ):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                self.load(f"[languages]\nenabled = {bad}\n")

    def test_command_must_not_be_empty(self):
        with self.assertRaises(config.ConfigError):
            self.load('[languages.octave]\ncommand = " "\n')


class GraderLanguageTest(DbTestCase):
    def test_unknown_language_is_a_grader_error(self):
        with self.assertRaises(grader.GraderError):
            grader.grade(
                self.cfg, "check(true, 'a', 'b');\n", {}, language="nonsense"
            )

    @with_toy
    def test_a_language_the_server_does_not_offer_is_a_grader_error(self):
        self.cfg["languages"]["enabled"] = ["octave"]
        with self.assertRaises(grader.GraderError) as ctx:
            grader.grade(self.cfg, "check 1\n", {}, language="toy")
        self.assertIn("not offered", str(ctx.exception))


class LabLanguageTest(DbTestCase):
    FORM = {
        "description": "Text",
        "due_at": "2027-03-01T23:59",
        "required_files": "a.toy",
        "show_report": "1",
        "testbench": "check 1\n",
        "helpers": "",
    }

    def enabled(self):
        return languages.enabled(self.cfg)

    def test_a_new_lab_without_a_choice_gets_the_default(self):
        self.assertEqual(
            labs.clean_fields(self.FORM | {"required_files": "a.m"})[
                "language"
            ],
            "octave",
        )

    def test_the_language_must_be_offered_and_known(self):
        with self.assertRaises(labs.LabError) as ctx:
            labs.clean_fields(
                self.FORM | {"language": "nonsense"}, self.enabled()
            )
        self.assertEqual(ctx.exception.key, "err.lab.language")

    @with_toy
    def test_a_lab_keeps_its_language_even_if_the_server_stopped_offering_it(
        self,
    ):
        fields = labs.clean_fields(
            self.FORM | {"language": "toy"}, ["octave"], current="toy"
        )
        self.assertEqual(fields["language"], "toy")
        with self.assertRaises(labs.LabError):
            labs.clean_fields(
                self.FORM | {"language": "toy"}, ["octave"], current="octave"
            )

    @with_toy
    def test_testbench_and_provided_functions_are_checked_by_the_adapter(self):
        with self.assertRaises(labs.LabError) as ctx:
            labs.clean_fields(
                self.FORM | {"language": "toy", "testbench": "def x\n"},
                self.enabled(),
            )
        self.assertEqual(ctx.exception.key, "ln.toy.err_def")
        fields = labs.clean_fields(
            self.FORM | {"language": "toy", "provided": "def one\ndef two\n"},
            self.enabled(),
        )
        self.assertEqual(
            [n for n, _ in provided.split("toy", fields["provided"])],
            ["one", "two"],
        )
        self.assertEqual(
            sorted(provided.files("toy", fields["provided"])),
            ["one.toy", "two.toy"],
        )

    @with_toy
    def test_the_reserved_file_name_is_the_one_of_the_language(self):
        with self.assertRaises(labs.LabError):
            labs.parse_required("bench.toy", "toy")
        self.assertEqual(
            labs.parse_required("testbench.m", "toy"), ["testbench.m"]
        )  # only reserved for Octave
        with self.assertRaises(labs.LabError):
            labs.parse_required("testbench.m", "octave")

    @with_toy
    def test_create_update_and_clone_keep_the_language(self):
        labs.create_course(self.conn, "am27")
        fields = labs.clean_fields(
            self.FORM | {"language": "toy"}, self.enabled()
        )
        name = labs.create(self.conn, "am27", "1a", fields)
        self.assertEqual(labs.get(self.conn, name)["language"], "toy")
        labs.update(
            self.conn,
            name,
            labs.clean_fields(
                self.FORM | {"language": "toy", "description": "neu"},
                self.enabled(),
                "toy",
            ),
        )
        (cloned,) = labs.clone_course(self.conn, "am27", "am28")
        self.assertEqual(labs.get(self.conn, cloned)["language"], "toy")

    @with_toy
    def test_the_language_is_locked_once_there_are_submissions(self):
        labs.create_course(self.conn, "am27")
        name = labs.create(
            self.conn,
            "am27",
            "1a",
            labs.clean_fields(self.FORM | {"language": "toy"}, self.enabled()),
        )
        other = labs.clean_fields(
            self.FORM | {"language": "octave", "required_files": "a.m"},
            self.enabled(),
            "toy",
        )
        labs.update(self.conn, name, other)  # no submissions: may change
        self.assertEqual(labs.get(self.conn, name)["language"], "octave")
        labs.set_active(self.conn, name, True)
        submissions.submit(self.conn, self.cfg, name, STUDENT, {"a.m": GOOD})
        again = labs.clean_fields(
            self.FORM | {"language": "toy"}, self.enabled(), "octave"
        )
        with self.assertRaises(labs.LabError) as ctx:
            labs.update(self.conn, name, again)
        self.assertEqual(ctx.exception.key, "err.lab.language_locked")


class SubmissionLanguageTest(DbTestCase):
    @with_toy
    def test_a_language_that_is_not_offered_takes_no_submissions(
        self,
    ):
        add_lab(self.conn, "am24-1a", required="a.toy\n")
        self.conn.execute(
            "UPDATE lab SET language = 'toy' WHERE name = 'am24-1a'"
        )
        self.conn.commit()
        self.cfg["languages"]["enabled"] = ["octave"]
        with self.assertRaises(submissions.SubmissionError) as ctx:
            submissions.submit(
                self.conn, self.cfg, "am24-1a", STUDENT, {"a.toy": b"x"}
            )
        self.assertEqual(ctx.exception.key, "err.submit.language")
        self.cfg["languages"]["enabled"] = ["octave", "toy"]
        self.assertEqual(
            submissions.get(
                self.conn,
                submissions.submit(
                    self.conn, self.cfg, "am24-1a", STUDENT, {"a.toy": b"x"}
                ),
            )["status"],
            "queued",
        )


class BackupLanguageTest(DbTestCase):
    RAW = {
        "name": "am24-1a",
        "language": "octave",
        "due_at": "2027-01-01T10:00:00Z",
        "required_files": "a.m",
        "testbench": "check(true, 'a', 'b');\n",
    }

    def test_the_language_is_part_of_a_lab(self):
        self.assertIn("language", backup.LAB_FIELDS)
        self.assertEqual(backup._clean_lab(self.RAW)["language"], "octave")

    def test_a_language_this_system_does_not_know_is_refused(self):
        with self.assertRaises(backup.BackupError) as ctx:
            backup._clean_lab(self.RAW | {"language": "nonsense"})
        self.assertEqual(
            (ctx.exception.key, ctx.exception.params["language"]),
            ("err.backup.language", "nonsense"),
        )

    def test_a_lab_without_a_language_is_refused(self):
        raw = dict(self.RAW)
        del raw["language"]
        with self.assertRaises(backup.BackupError):
            backup._clean_lab(raw)

    @with_toy
    def test_a_known_language_that_the_server_does_not_offer_is_taken_over(
        self,
    ):
        self.cfg["languages"]["enabled"] = ["octave"]
        self.assertEqual(
            backup._clean_lab(
                self.RAW | {"language": "toy", "required_files": "a.toy"}
            )["language"],
            "toy",
        )


class MigrationTest(DbTestCase):
    def test_existing_labs_become_octave_labs(self):
        path = os.path.join(self.cfg["data_dir"], "old.sqlite")
        conn = db.connect(path)
        for i, migration in enumerate(db.MIGRATIONS[:12], start=1):
            migration(conn)
        conn.execute("PRAGMA user_version = 12")
        conn.execute(
            "INSERT INTO lab (name, course, due_at, "
            "required_files, testbench) "
            "VALUES ('am24-1a', 'am24', '2027-01-01T10:00:00Z', 'a.m', 'x')"
        )
        conn.commit()
        conn.close()
        conn = db.init_db(path)
        self.assertEqual(
            conn.execute("SELECT language FROM lab").fetchone()[0], "octave"
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
