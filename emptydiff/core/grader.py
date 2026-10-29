# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Running a testbench in the sandbox (DESIGN.md section 8). Standard
library only.

A testbench consists of a body (the statements of the testbench) and
helper functions. What the program that runs it looks like depends on
the language of the lab and is built by the language adapter
(`languages/`): it writes the testbench file into the run folder and
names the interpreter. This module is the same for every language: run
folder, sandbox, time and size limits, the results protocol and the
verdict. The checks append one line each to a results file; the verdict
is computed from these lines, never from what the program prints. The
student's own output is kept apart and only shown as information.

Sandbox 'bwrap': prlimit + bubblewrap, no network, only the run
directory is writable. Sandbox 'none': the interpreter directly, no
isolation, only with EMPTYDIFF_DEV=1 (see config.py).
"""

import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

from . import languages

# measured on real submission logs: median 131 bytes, 99 % below 48 KB,
# only 6 of 6022 above 256 KiB.
MAX_OUTPUT_BYTES = 1024 * 1024
FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
ADDRESS_SPACE_BYTES = 3 * 1024**3
MAX_FILE_BYTES = 10 * 1024**2
MAX_PROCESSES = 128
TMP_BYTES = 64 * 1024**2  # size of the RAM disk at /tmp inside the sandbox
MAX_CHECKS = 500  # recorded checks per run; further ones are ignored
MAX_CHECK_TEXT = 500  # characters per recorded text
MAX_RESULTS_BYTES = 1024 * 1024


class GraderError(Exception):
    """The grader could not be run (no result for the submission)."""


class SandboxError(GraderError):
    """The sandbox did not start (bubblewrap reported an error, so the
    interpreter never ran): a problem of the machine, for example too
    many processes at that moment, not of the submission."""


@dataclass
class GradeResult:
    passed: bool
    checks: list  # [(ok: bool, text: str)] in the order they were recorded
    finished: bool  # the testbench ran to its end (no abort, no exit)
    exit_code: int
    timed_out: bool
    truncated: bool
    log: str  # stdout of the interpreter: what the student's code printed
    error_log: str  # stderr of the interpreter
    duration: float


def parse_results(text, token):
    """Recorded lines -> (checks, finished). Lines without the token are
    ignored.

    `finished` means the last recorded line is 'done'.
    """
    checks = []
    finished = False
    for line in text.split("\n"):
        parts = line.split("\t", 2)
        if (
            len(parts) != 3
            or parts[0] != token
            or parts[1] not in ("ok", "fail", "done")
        ):
            continue
        kind = parts[1]
        finished = kind == "done"
        if not finished and len(checks) < MAX_CHECKS:
            checks.append((kind == "ok", parts[2].strip()[:MAX_CHECK_TEXT]))
    return checks, finished


def verdict(checks, finished, exit_code, timed_out=False, truncated=False):
    """Passed: clean end, exit code 0, at least one check and no failed
    one."""
    if timed_out or truncated or exit_code != 0 or not finished or not checks:
        return False
    return all(ok for ok, _ in checks)


def _command(cfg, rundir, language):
    g = cfg["grader"]
    interpreter = language.argv(cfg["languages"][language.id]["command"])
    if g["sandbox"] == "none":
        return interpreter
    for tool in ("bwrap", "prlimit"):
        if shutil.which(tool) is None:
            raise GraderError(
                f"'{tool}' not found (sandbox 'bwrap' not available)"
            )
    cpu = 2 * g["timeout_seconds"]
    return [
        "prlimit",
        f"--cpu={cpu}",
        f"--as={ADDRESS_SPACE_BYTES}",
        f"--fsize={MAX_FILE_BYTES}",
        f"--nproc={MAX_PROCESSES}",
        "--core=0",
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--ro-bind-try",
        "/etc/alternatives",
        "/etc/alternatives",
        "--ro-bind-try",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--ro-bind-try",
        "/etc/ld.so.conf.d",
        "/etc/ld.so.conf.d",
        *[
            arg
            for path in language.extra_ro_binds
            for arg in ("--ro-bind-try", path, path)
        ],
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(TMP_BYTES),
        "--tmpfs",
        "/tmp",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--bind",
        rundir,
        "/work",
        "--chdir",
        "/work",
        *interpreter,
    ]


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _run(cmd, cwd, timeout, env):
    """Start cmd, read stdout/stderr through pipes (bounded) and enforce
    the time limit.

    The output deliberately goes through pipes instead of files in the
    run directory, so that student code cannot tamper with the log
    through file access.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    sel = selectors.DefaultSelector()
    for stream in buffers:
        sel.register(stream, selectors.EVENT_READ)
    timed_out = truncated = False
    try:
        while sel.get_map() and not truncated:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in sel.select(timeout=remaining):
                data = os.read(key.fd, 65536)
                if not data:
                    sel.unregister(key.fileobj)
                    continue
                buf = buffers[key.fileobj]
                room = MAX_OUTPUT_BYTES - len(buf)
                buf.extend(data[:room])
                if len(data) > room:
                    truncated = True
        if not timed_out and not truncated:
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.01))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if timed_out or truncated or proc.poll() is None:
            _kill_group(proc)
        sel.close()
        proc.stdout.close()
        proc.stderr.close()
    out = (
        buffers[proc.stdout]
        .decode("utf-8", errors="replace")
        .replace("\x00", "")
    )
    err = (
        buffers[proc.stderr]
        .decode("utf-8", errors="replace")
        .replace("\x00", "")
    )
    return proc.returncode, out, err, timed_out, truncated


def _write_files(rundir, files):
    for name, content in files.items():
        if not FILENAME_RE.match(name):
            raise GraderError(f"invalid file name {name!r}")
        with open(os.path.join(rundir, name), "wb") as f:
            f.write(content)


def cleanup_run_dir(cfg):
    """Remove run folders that a crashed or killed worker left behind.
    Call it only while no check is running."""
    run_root = os.path.join(cfg["data_dir"], "run")
    try:
        names = os.listdir(run_root)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(run_root, name)
        if (
            name.startswith("run-")
            and os.path.isdir(path)
            and not os.path.islink(path)
        ):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def grade(
    cfg,
    testbench,
    files,
    provided=None,
    helpers="",
    language=languages.DEFAULT,
):
    """Run the testbench (body and helper functions as text) against the
    student files.

    files: {file name: bytes} of the submission. provided: {file name:
    bytes} of the lab's provided functions; they are only added if the
    student did not submit a file of that name. A submitted file with
    the name of the testbench file is always replaced by the one of the
    lab. `language` is the identifier of the lab's language.
    """
    try:
        adapter = languages.get(language)
    except languages.UnknownLanguage as e:
        raise GraderError(str(e)) from None
    if language not in languages.enabled(cfg):
        raise GraderError(
            f"language {language!r} is not offered on this "
            "server (languages.enabled)"
        )
    g = cfg["grader"]
    run_root = os.path.join(cfg["data_dir"], "run")
    os.makedirs(run_root, exist_ok=True)
    rundir = tempfile.mkdtemp(prefix="run-", dir=run_root)
    token = secrets.token_hex(8)
    results_file = f"emptydiff-{secrets.token_hex(6)}.txt"
    try:
        merged = {
            **(provided or {}),
            **{k: v for k, v in files.items() if k != adapter.testbench_file},
        }
        _write_files(rundir, merged)
        _write_files(
            rundir, adapter.files(testbench, helpers, results_file, token)
        )
        cmd = _command(cfg, rundir, adapter)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": rundir}
        start = time.monotonic()
        rc, out, err, timed_out, truncated = _run(
            cmd, rundir, g["timeout_seconds"], env
        )
        duration = time.monotonic() - start
        checks, finished = parse_results(
            _read_results(os.path.join(rundir, results_file)), token
        )
    finally:
        shutil.rmtree(rundir, ignore_errors=True)
    if (
        g["sandbox"] == "bwrap"
        and not checks
        and not finished
        and err.startswith("bwrap: ")
    ):
        raise SandboxError(err.strip().split("\n")[0])
    return GradeResult(
        passed=verdict(checks, finished, rc, timed_out, truncated),
        checks=checks,
        finished=finished,
        exit_code=rc,
        timed_out=timed_out,
        truncated=truncated,
        log=out,
        error_log=err,
        duration=duration,
    )


def _read_results(path):
    try:
        with open(path, "rb") as f:
            return (
                f.read(MAX_RESULTS_BYTES)
                .decode("utf-8", errors="replace")
                .replace("\r", "")
            )
    except OSError:
        return ""


def selftest(cfg, language=None):
    """Check whether the interpreters run in the configured sandbox: the
    given language, otherwise every language the server offers. Returns
    (ok, message)."""
    messages = []
    for language_id in [language] if language else languages.enabled(cfg):
        adapter = languages.get(language_id)
        try:
            result = grade(
                cfg, adapter.selftest_body, {}, language=language_id
            )
        except (GraderError, OSError) as e:
            return False, f"Grader cannot run ({adapter.name}): {e}"
        if not result.passed:
            detail = (result.error_log or result.log).strip()[:300]
            return (
                False,
                f"Self-test failed ({adapter.name}, exit "
                f"{result.exit_code}): {detail}",
            )
        messages.append(f"{adapter.name} {result.duration:.1f}s")
    return (
        True,
        "Self-test passed (sandbox "
        f"'{cfg['grader']['sandbox']}'; {', '.join(messages)})",
    )
