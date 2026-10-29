# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock

from emptydiff.core import config, grader, languages

TESTBENCH = """check(addone(1) == 2, 'addone ok', 'addone falsch');
"""

GOOD = b"function y = addone(x)\n  y = x + 1;\nend\n"


class ParseTest(unittest.TestCase):
    TOKEN = "abc123"

    def lines(self, *rows):
        return (
            "\n".join(f"{self.TOKEN}\t{kind}\t{text}" for kind, text in rows)
            + "\n"
        )

    def test_checks_and_end(self):
        checks, finished = grader.parse_results(
            self.lines(("ok", "a"), ("fail", "b"), ("done", "")), self.TOKEN
        )
        self.assertEqual(checks, [(True, "a"), (False, "b")])
        self.assertTrue(finished)

    def test_lines_without_the_token_are_ignored(self):
        text = "forged\tok\tx\nforged\tdone\t\n" + self.lines(("fail", "real"))
        self.assertEqual(
            grader.parse_results(text, self.TOKEN), ([(False, "real")], False)
        )

    def test_something_after_done_is_not_a_clean_end(self):
        checks, finished = grader.parse_results(
            self.lines(("done", ""), ("ok", "late")), self.TOKEN
        )
        self.assertFalse(finished)

    def test_unknown_kinds_and_broken_lines_are_ignored(self):
        text = (
            self.lines(("maybe", "x"))
            + "no tabs at all\n"
            + self.lines(("ok", "a"))
        )
        self.assertEqual(
            grader.parse_results(text, self.TOKEN), ([(True, "a")], False)
        )

    def test_texts_and_number_of_checks_are_limited(self):
        rows = [("ok", "x" * 5000)] * (grader.MAX_CHECKS + 10)
        checks, _ = grader.parse_results(self.lines(*rows), self.TOKEN)
        self.assertEqual(len(checks), grader.MAX_CHECKS)
        self.assertEqual(len(checks[0][1]), grader.MAX_CHECK_TEXT)


class VerdictTest(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(grader.verdict([(True, "a")], True, 0))

    def test_failed_check_fails(self):
        self.assertFalse(grader.verdict([(True, "a"), (False, "b")], True, 0))

    def test_no_checks_fails(self):
        self.assertFalse(grader.verdict([], True, 0))

    def test_unfinished_fails(self):
        self.assertFalse(grader.verdict([(True, "a")], False, 0))

    def test_nonzero_exit_fails_even_when_finished(self):
        self.assertFalse(grader.verdict([(True, "a")], True, 1))

    def test_timeout_and_truncation_fail(self):
        self.assertFalse(
            grader.verdict([(True, "a")], True, 0, timed_out=True)
        )
        self.assertFalse(
            grader.verdict([(True, "a")], True, 0, truncated=True)
        )


class AssembleTest(unittest.TestCase):
    def test_body_and_helpers_are_inserted_untouched(self):
        files = languages.get("octave").files(
            "x = @RESULTS@;\n", "function h()\nend\n", "r.txt", "tok"
        )
        self.assertEqual(list(files), ["testbench.m"])
        text = files["testbench.m"].decode("utf-8")
        self.assertIn(
            "x = @RESULTS@;", text
        )  # the lab's text is never taken for a placeholder
        self.assertIn("fopen('r.txt', 'a')", text)
        self.assertIn("'tok'", text)
        self.assertTrue(text.startswith("function testbench()"))
        self.assertIn("function h()", text)


class CleanupTest(unittest.TestCase):
    def test_left_over_run_folders_are_removed_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as data:
            run = os.path.join(data, "run")
            for name in ("run-abc", "run-def"):
                os.makedirs(os.path.join(run, name))
                with open(os.path.join(run, name, "x.m"), "w") as f:
                    f.write("x")
            os.makedirs(os.path.join(run, "other"))
            os.symlink(
                data, os.path.join(run, "run-link")
            )  # a link is never followed or removed as a folder
            cfg = {"data_dir": data}
            self.assertEqual(grader.cleanup_run_dir(cfg), 2)
            self.assertEqual(sorted(os.listdir(run)), ["other", "run-link"])
            self.assertTrue(os.path.isdir(data))

    def test_a_missing_run_folder_is_fine(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(grader.cleanup_run_dir({"data_dir": data}), 0)


class CommandTest(unittest.TestCase):
    def test_bwrap_mode_fails_closed_without_tools(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(grader.GraderError):
                grader._command(cfg, "/tmp/x", languages.get("octave"))

    def test_bwrap_command_is_isolated(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        with mock.patch("shutil.which", return_value="/usr/bin/x"):
            cmd = grader._command(cfg, "/tmp/run", languages.get("octave"))
        self.assertIn("--unshare-all", cmd)  # including network
        self.assertIn("--die-with-parent", cmd)
        self.assertEqual(
            cmd[cmd.index("--bind") + 1 : cmd.index("--bind") + 3],
            ["/tmp/run", "/work"],
        )
        # /tmp has a fixed size (the option comes right before the tmpfs
        # it applies to)
        i = cmd.index("--tmpfs")
        self.assertEqual(
            (cmd[i - 2 : i], cmd[i + 1]),
            (["--size", str(grader.TMP_BYTES)], "/tmp"),
        )
        self.assertEqual(cmd[-4:-1], ["--norc", "--silent", "--eval"])
        self.assertTrue(cmd[-1].endswith("testbench"))


def available_modes():
    """Sandbox modes that can really be tested in this environment."""
    modes = []
    if shutil.which("octave-cli"):
        modes.append("none")
        if shutil.which("bwrap") and shutil.which("prlimit"):
            tmp = tempfile.mkdtemp()
            try:
                cfg = make_cfg(tmp, "bwrap")
                if grader.selftest(cfg)[0]:
                    modes.append("bwrap")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return modes


def make_cfg(data_dir, sandbox, timeout=20):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["data_dir"] = data_dir
    cfg["grader"]["sandbox"] = sandbox
    cfg["grader"]["timeout_seconds"] = timeout
    return cfg


MODES = available_modes()


class SandboxStartTest(unittest.TestCase):
    """bubblewrap that does not start (for example no process left) is
    an error of the machine, never a wrong submission."""

    BWRAP = "bwrap: Creating new namespace failed: Resource temporarily "
    BWRAP += "unavailable\n"

    def grade(self, sandbox, rc, err):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = make_cfg(tmp.name, sandbox)
        with (
            mock.patch.object(grader, "_command", return_value=["unused"]),
            mock.patch.object(
                grader, "_run", return_value=(rc, "", err, False, False)
            ),
        ):
            return grader.grade(cfg, "check(1 == 1, 'a', 'b');", {})

    def test_bwrap_error_without_any_result_raises_sandbox_error(self):
        with self.assertRaises(grader.SandboxError) as ctx:
            self.grade("bwrap", 1, self.BWRAP)
        self.assertIn("Creating new namespace failed", str(ctx.exception))
        self.assertIsInstance(ctx.exception, grader.GraderError)

    def test_other_errors_are_still_results_of_the_submission(self):
        for err in (
            "error: 'addone' undefined\n",
            "warning: bwrap: not at the start\n",
            "",
        ):
            with self.subTest(err=err):
                result = self.grade("bwrap", 1, err)
                self.assertFalse(result.passed)

    def test_without_the_sandbox_nothing_is_reinterpreted(self):
        result = self.grade("none", 1, self.BWRAP)
        self.assertFalse(result.passed)


@unittest.skipUnless(
    MODES, "octave-cli nicht verfügbar (Tests im dev/-Container ausführen)"
)
class OctaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def grade(
        self, mode, files, provided=None, testbench=TESTBENCH, timeout=20
    ):
        cfg = make_cfg(self.tmp.name, mode, timeout)
        return grader.grade(cfg, testbench, files, provided)

    def test_pass(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": GOOD})
                self.assertTrue(r.passed, r.log + r.error_log)
                self.assertEqual(r.exit_code, 0)
                self.assertEqual(r.checks, [(True, "addone ok")])
                self.assertTrue(r.finished)

    def test_wrong_result_fails(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(
                    mode,
                    {
                        "addone.m": b"function y = addone(x)\n  y = "
                        b"x + 2;\nend\n"
                    },
                )
                self.assertFalse(r.passed)
                self.assertEqual(r.checks, [(False, "addone falsch")])

    def test_syntax_error_fails_with_message(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(
                    mode,
                    {"addone.m": b"function y = addone(x\n  y = x + 1;\n"},
                )
                self.assertFalse(r.passed)
                self.assertNotEqual(r.exit_code, 0)
                self.assertTrue(r.error_log.strip())

    def test_printed_verdict_lines_do_not_count(self):
        student = (
            b"function y = addone(x)\n  disp('[OK] addone ok');\n "
            b" disp('[Verification ready]');\n"
            b"  y = x + 2;\nend\n"
        )
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": student})
                self.assertFalse(r.passed)
                self.assertEqual(r.checks, [(False, "addone falsch")])
                self.assertIn(
                    "[OK] addone ok", r.log
                )  # kept apart as the student's own output

    def test_exit_in_student_code_is_not_a_finished_run(self):
        student = b"function y = addone(x)\n  exit(0);\nend\n"
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": student})
                self.assertEqual(r.exit_code, 0)
                self.assertFalse(r.finished)
                self.assertFalse(r.passed)

    def test_error_in_student_code_aborts_the_run(self):
        student = b"function y = addone(x)\n  error('abbruch');\nend\n"
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": student})
                self.assertFalse(r.passed)
                self.assertFalse(r.finished)
                self.assertIn("abbruch", r.error_log)

    def test_student_code_cannot_call_the_check_functions(self):
        student = (
            b"function y = addone(x)\n  check(true, 'gefaelscht', "
            b"'x');\n  y = x + 2;\nend\n"
        )
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": student})
                self.assertFalse(r.passed)
                self.assertNotIn((True, "gefaelscht"), r.checks)

    def test_helpers_are_available_to_the_testbench_only(self):
        helpers = "function y = twice(x)\n  y = 2 * x;\nend\n"
        body = "check(twice(addone(1)) == 4, 'ok', 'falsch');\n"
        for mode in MODES:
            with self.subTest(mode=mode):
                cfg = make_cfg(self.tmp.name, mode)
                r = grader.grade(
                    cfg, body, {"addone.m": GOOD}, helpers=helpers
                )
                self.assertTrue(r.passed, r.error_log)
                student = b"function y = addone(x)\n  y = twice(x);\nend\n"
                self.assertFalse(
                    grader.grade(
                        cfg, body, {"addone.m": student}, helpers=helpers
                    ).passed
                )

    def test_check_close_compares_size_and_values(self):
        body = (
            "check_close([1 2 3], [1 2 3 + 1e-9], 1e-6, 'a', 'A');\n"
            "check_close([1 2 3], [1 2 4], 1e-6, 'b', 'B');\n"
            "check_close([1 2 3], [1 2 3]', 1e-6, 'c', 'C');\n"
            "check_close(NaN, NaN, 1, 'd', 'D');\n"
            "check_close(Inf, Inf, 0, 'e', 'E');\n"
            "check_close('x', 1, 1, 'f', 'F');\n"
            "check([1 1 1], 'g', 'G');\n"
            "check([1 0 1], 'h', 'H');\n"
            "check([], 'i', 'I');\n"
            "check(NaN, 'j', 'J');\n"
        )
        r = grader.grade(make_cfg(self.tmp.name, MODES[0]), body, {})
        self.assertTrue(r.finished, r.error_log)
        self.assertEqual(
            [text for _, text in r.checks],
            ["a", "B", "C", "D", "e", "F", "g", "H", "I", "J"],
        )

    def test_texts_with_special_characters_stay_on_one_line(self):
        body = (
            "check(false, sprintf('zeile1\\nzeile2\\tTab %d%% "
            "\\\\ \u00e4', 5), 'x');\n"
        )
        r = grader.grade(make_cfg(self.tmp.name, MODES[0]), body, {})
        self.assertEqual(r.checks, [(False, "x")])
        body = (
            "check(true, sprintf('zeile1\\nzeile2\\tTab %d%% "
            "\u00e4', 5), 'x');\n"
        )
        r = grader.grade(make_cfg(self.tmp.name, MODES[0]), body, {})
        self.assertEqual(r.checks, [(True, "zeile1 zeile2 Tab 5% \u00e4")])

    def test_submitted_testbench_is_replaced(self):
        fake = b"function testbench()\n  disp('nothing checked');\nend\n"
        wrong = b"function y = addone(x)\n  y = 0;\nend\n"
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": wrong, "testbench.m": fake})
                self.assertFalse(r.passed)

    def test_provided_file_fills_in_but_student_wins(self):
        wrong = b"function y = addone(x)\n  y = 0;\nend\n"
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertTrue(
                    self.grade(mode, {}, provided={"addone.m": GOOD}).passed
                )
                r = self.grade(
                    mode, {"addone.m": GOOD}, provided={"addone.m": wrong}
                )
                self.assertTrue(r.passed)

    def test_infinite_loop_times_out(self):
        loop = b"function y = addone(x)\n  while true, end\nend\n"
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": loop}, timeout=2)
                self.assertTrue(r.timed_out)
                self.assertFalse(r.passed)
                self.assertLess(r.duration, 8)

    def test_output_flood_is_truncated_quickly(self):
        flood = (
            b"function y = addone(x)\n  while true, "
            b"disp(repmat('x', 1, 1000)); end\nend\n"
        )
        for mode in MODES:
            with self.subTest(mode=mode):
                r = self.grade(mode, {"addone.m": flood}, timeout=15)
                self.assertTrue(r.truncated)
                self.assertFalse(r.passed)
                self.assertLessEqual(len(r.log), grader.MAX_OUTPUT_BYTES)
                self.assertLess(r.duration, 12)

    def test_invalid_filename_rejected(self):
        for name in ("../evil.m", "a/b.m", ".hidden.m", "", "x" * 100 + ".m"):
            with self.subTest(name=name):
                with self.assertRaises(grader.GraderError):
                    self.grade(MODES[0], {name: b""})

    def test_run_directory_is_removed(self):
        self.grade(MODES[0], {"addone.m": GOOD})
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "run")), [])

    def test_selftest(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                ok, message = grader.selftest(make_cfg(self.tmp.name, mode))
                self.assertTrue(ok, message)


@unittest.skipUnless("bwrap" in MODES, "bwrap-Sandbox nicht verfügbar")
class SandboxIsolationTest(unittest.TestCase):
    """Properties that only hold in the bwrap sandbox."""

    def test_plotting_works_headless_and_is_silent(self):
        student = b"""function y = addone(x)
  figure; subplot(2, 1, 1); plot(1:3); hold on; plot(3:-1:1);
  title('t'); xlabel('x'); legend('a', 'b');
  y = x + 1;
end
"""
        with tempfile.TemporaryDirectory() as tmp:
            r = grader.grade(
                make_cfg(tmp, "bwrap"), TESTBENCH, {"addone.m": student}
            )
        self.assertTrue(r.passed, r.log + r.error_log)
        self.assertEqual(
            r.error_log.strip(), ""
        )  # no toolkit warning in the student report

    def test_tmp_is_a_small_ram_disk_and_cannot_be_filled(self):
        student = b"""function y = addone(x)
  cmd = ['n=0; while [ $n -lt 100 ] && ', ...
         'head -c 10485760 /dev/zero > /tmp/f$n 2>/dev/null; ', ...
         'do n=$((n+1)); done; echo $n'];
  [~, out] = system(cmd);
  disp(strtrim(out));
  y = x + 1;
end
"""
        with tempfile.TemporaryDirectory() as tmp:
            r = grader.grade(
                make_cfg(tmp, "bwrap"), TESTBENCH, {"addone.m": student}
            )
        self.assertTrue(r.passed, r.log + r.error_log)
        written = int(
            r.log.strip().splitlines()[-1]
        )  # files of 10 MB that fit
        self.assertLessEqual(written, grader.TMP_BYTES // (10 * 1024**2) + 1)
        self.assertGreaterEqual(written, 1)

    def test_student_cannot_see_or_write_host(self):
        student = b"""function y = addone(x)
  if exist('/home', 'dir') || exist('/var/lib', 'dir') || exist('/app', 'dir')
    error('Host sichtbar');
  end
  if fopen('/usr/pwned', 'w') > 0
    error('schreiben ausserhalb /work');
  end
  y = x + 1;
end
"""
        with tempfile.TemporaryDirectory() as tmp:
            r = grader.grade(
                make_cfg(tmp, "bwrap"), TESTBENCH, {"addone.m": student}
            )
        self.assertTrue(r.passed, r.log + r.error_log)


if __name__ == "__main__":
    unittest.main()
