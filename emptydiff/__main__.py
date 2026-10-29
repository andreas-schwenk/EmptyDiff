# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line: python3 -m emptydiff <command>. See DESIGN.md, section
4."""

import argparse
import os
import signal
import sys
import threading

from . import __version__
from .core import (
    auth,
    config,
    db,
    deploy,
    grader,
    health,
    languages,
    mail,
    provided,
    regress,
    retention,
    worker,
)


def cmd_config(args):
    cfg = config.load(args.config)
    cfg["mail"]["password"] = "***" if cfg["mail"]["password"] else ""
    for section, value in cfg.items():
        print(f"{section} = {value}")


def cmd_initdb(args):
    cfg = config.load(args.config)
    os.makedirs(cfg["data_dir"], exist_ok=True)
    path = os.path.join(cfg["data_dir"], "emptydiff.sqlite")
    db.init_db(path).close()
    print(f"Database ready: {path}")


def _open_db(cfg):
    os.makedirs(cfg["data_dir"], exist_ok=True)
    return db.init_db(os.path.join(cfg["data_dir"], "emptydiff.sqlite"))


def cmd_grade(args):
    """Run a directory of student files against a testbench (local
    development)."""
    cfg = config.load(args.config)
    supplied = None
    helpers = ""
    language = args.language
    if args.testbench:
        with open(args.testbench, encoding="utf-8") as f:
            testbench = f.read()
        if args.helpers:
            with open(args.helpers, encoding="utf-8") as f:
                helpers = f.read()
        if args.provided:
            with open(args.provided, encoding="utf-8") as f:
                supplied = provided.files(language, f.read())
    else:
        conn = _open_db(cfg)
        row = conn.execute(
            "SELECT language, testbench, helpers, provided FROM "
            "lab WHERE name = ?",
            (args.lab,),
        ).fetchone()
        if row is None:
            sys.exit(f"Lab {args.lab!r} not in the database")
        language = row["language"]
        testbench, helpers = row["testbench"], row["helpers"]
        supplied = provided.files(language, row["provided"])
        conn.close()

    files = {}
    for name in sorted(os.listdir(args.directory)):
        path = os.path.join(args.directory, name)
        if os.path.isfile(path) and grader.FILENAME_RE.match(name):
            with open(path, "rb") as f:
                files[name] = f.read()
    try:
        result = grader.grade(
            cfg, testbench, files, supplied, helpers=helpers, language=language
        )
    except grader.GraderError as e:
        sys.exit(f"Grader error: {e}")

    for ok, text in result.checks:
        print(f"[{'OK' if ok else 'FAIL'}] {text}")
    if not result.finished:
        print("(the testbench did not run to its end)")
    if result.log.strip():
        print("--- output of the program ---")
        print(result.log, end="" if result.log.endswith("\n") else "\n")
    if result.error_log.strip():
        print("--- stderr ---")
        print(
            result.error_log,
            end="" if result.error_log.endswith("\n") else "\n",
        )
    flags = [
        f
        for f, on in (
            ("time limit exceeded", result.timed_out),
            ("output truncated", result.truncated),
        )
        if on
    ]
    print(
        f"--- {'PASSED' if result.passed else 'NOT PASSED'} "
        f"(exit {result.exit_code}, "
        f"{result.duration:.1f}s{''.join(', ' + f for f in flags)}) ---"
    )
    sys.exit(0 if result.passed else 1)


def cmd_regress(args):
    """Grade recorded student files against the labs in the database and
    compare with the recorded verdicts."""
    cfg = config.load(args.config)
    ok, message = grader.selftest(cfg)
    print(message)
    if not ok:
        sys.exit(2)
    conn = _open_db(cfg)
    try:
        report = regress.run(
            cfg,
            conn,
            args.directory,
            labs=set(args.lab) or None,
            limit=args.limit,
            progress=lambda done, total: print(
                f"  {done}/{total}", file=sys.stderr
            ),
        )
    finally:
        conn.close()
    total = len(report.outcomes)
    deviations = report.deviations
    same = total - len(deviations)
    print(
        f"\nChecked: {total} revisions; skipped: "
        f"{', '.join(f'{n} {k}' for k, n in report.skipped.items()) or 'none'}"
    )
    if total:
        print(f"Passed/not passed agrees: {same} ({100 * same / total:.2f} %)")
    lost = [o for o in deviations if o.old_passed]
    gained = [o for o in deviations if not o.old_passed]
    print(
        "Deviations: recorded passed -> graded not passed: "
        f"{len(lost)}; recorded not passed -> graded "
        f"passed: {len(gained)}"
    )

    per_lab = {}
    for o in deviations:
        per_lab.setdefault(o.case.lab, []).append(o)
    for lab, items in sorted(per_lab.items()):
        print(f"  {lab}: {len(items)} deviations")
    for o in deviations[: args.show]:
        print(
            f"    {o.case.lab}/{o.case.student}/{o.case.rev}: "
            f"recorded {'passed' if o.old_passed else 'not passed'} -> "
            f"graded {'passed' if o.new_passed else 'not passed'} {o.reason}"
        )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("lab,student,rev,old_passed,new_passed,reason\n")
            for o in deviations:
                f.write(
                    f"{o.case.lab},{o.case.student},{o.case.rev},"
                    f"{int(o.old_passed)},{int(o.new_passed)},{o.reason}\n"
                )
        print(f"Deviations as CSV: {args.output}")
    sys.exit(1 if deviations else 0)


def cmd_packages(args):
    """The Debian packages of the programming languages that the server
    offers (one per line, for install.sh)."""
    if args.defaults:
        enabled = config.DEFAULTS["languages"]["enabled"]
    else:
        enabled = languages.enabled(config.load(args.config))
    for package in dict.fromkeys(
        p for language in enabled for p in languages.get(language).packages
    ):
        print(package)


def cmd_selftest(args):
    ok, message = grader.selftest(config.load(args.config))
    print(message)
    sys.exit(0 if ok else 1)


def cmd_worker(args):
    """Grade submissions from the queue (systemd service)."""
    cfg = config.load(args.config)
    ok, message = grader.selftest(cfg)
    print(message, flush=True)
    if not ok:
        sys.exit(2)
    _open_db(cfg).close()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    thread = worker.start(cfg, stop)
    print(
        "Worker running (one submission at a time, batches "
        f"of {cfg['grader']['batch_size']}). "
        "Stop with Ctrl+C.",
        flush=True,
    )
    stop.wait()
    thread.join(timeout=cfg["grader"]["timeout_seconds"] + 5)


def cmd_issue_code(args):
    """Emergency route: print a login code in case mail delivery
    fails."""
    cfg = config.load(args.config)
    conn = _open_db(cfg)
    try:
        email = auth.normalize_email(
            args.email, cfg["auth"]["allowed_domains"]
        )
        print(
            auth.issue_code(conn, email, None, cfg["auth"]["code_ttl_minutes"])
        )
    except auth.AuthError as e:
        sys.exit(str(e))
    finally:
        conn.close()


def cmd_purge(args):
    """Delete submissions after the retention period has expired (daily,
    via systemd timer)."""
    cfg = config.load(args.config)
    conn = _open_db(cfg)
    counts = retention.purge(conn, cfg, dry_run=args.dry_run)
    conn.close()
    prefix = "[dry run] " if args.dry_run else ""
    print(
        f"{prefix}labs past their retention period: "
        f"{counts['labs']} ({counts['submissions']} "
        "submissions), "
        f"old login codes: {counts['login_codes']}, expired "
        f"remember-me tokens: {counts['remember_tokens']}"
    )


def cmd_mail_test(args):
    """Send a test mail through the configured backend (checks SMTP
    settings, e.g. on the server)."""
    cfg = config.load(args.config)
    m = cfg["mail"]
    print(
        f"Backend {m['backend']}"
        + (
            f", {m['host']}:{m['port']} ({m['security']}), "
            f"sender {m['sender']}"
            if m["backend"] == "smtp"
            else ""
        )
    )
    try:
        mail.send(
            cfg,
            args.email,
            "EmptyDiff: test mail",
            "This is a test mail from EmptyDiff. Mail delivery "
            "is set up correctly.\n",
        )
    except mail.MailError as e:
        sys.exit(f"Error: {e}")
    for (
        simulated
    ) in mail.pop_simulated():  # Dev: nothing is sent, the mail is shown here
        print(
            "Simulation, nothing sent. To: "
            f"{simulated['recipient']}\nSubject: {simulated['subject']}\n"
            f"{simulated['body']}"
        )
        return
    print(f"Test mail to {args.email} handed over.")


def cmd_healthcheck(args):
    """Check database, worker, queue and purge. Exit 0 = OK (for scripts
    and monitoring)."""
    cfg = config.load(args.config)
    try:
        conn = db.connect(db.path_for(cfg))
        problems = health.check(conn, cfg)
        conn.close()
    except Exception:
        problems = ["db"]
    if problems:
        print("ERROR: " + ", ".join(problems))
        sys.exit(1)
    print("ok")


def cmd_deploy_render(args):
    """Render Apache, systemd and logrotate files from deploy/ (called
    by install.sh)."""
    cfg = config.load(args.config)
    try:
        files = deploy.render(
            cfg,
            os.path.abspath(args.app_dir),
            args.out,
            accept_acme_terms=args.accept_acme_terms,
        )
    except deploy.DeployError as e:
        sys.exit(f"Error: {e}")
    for name in files:
        print(name)


def cmd_web(args):
    """Development server. In production the app runs under
    Apache/mod_wsgi and the worker service."""
    if not config.is_dev():
        sys.exit(
            "The development server only runs with "
            "EMPTYDIFF_DEV=1; in production use Apache/mod_wsgi."
        )
    from .web import create_app

    cfg = config.load(args.config)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: development server listens on {args.host}",
            file=sys.stderr,
        )
    app = create_app(cfg)  # also creates the database
    if (
        not args.no_worker
    ):  # for convenience in dev mode: worker in the same process
        ok, message = grader.selftest(cfg)
        print(f"Worker: {message}", flush=True)
        if ok:
            worker.start(cfg, threading.Event())
        else:
            print(
                "Worker not started: submissions stay in the queue.",
                file=sys.stderr,
            )
    app.run(host=args.host, port=args.port, use_reloader=False)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="emptydiff")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "config", help="show the effective configuration"
    ).set_defaults(func=cmd_config)
    sub.add_parser("initdb", help="create/migrate the database").set_defaults(
        func=cmd_initdb
    )
    p_grade = sub.add_parser("grade", help="test a directory of student files")
    p_grade.add_argument("directory", help="directory with the student files")
    src = p_grade.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--testbench", help="file with the testbench body (statements only)"
    )
    src.add_argument(
        "--lab",
        help="name of a lab in the database (testbench and "
        "helpers from the DB)",
    )
    p_grade.add_argument(
        "--language",
        default=languages.DEFAULT,
        choices=languages.ids(),
        help="language of the testbench (with --testbench; a lab "
        "from the database brings its own)",
    )
    p_grade.add_argument(
        "--helpers", help="file with the helper functions (with --testbench)"
    )
    p_grade.add_argument(
        "--provided",
        help="file with the provided functions (with --testbench)",
    )
    p_grade.set_defaults(func=cmd_grade)
    p_reg = sub.add_parser(
        "regress",
        help="grade recorded student files (ReceivedLabs/) "
        "against the labs in the database",
    )
    p_reg.add_argument(
        "directory",
        help="directory with the recorded submissions (ReceivedLabs/)",
    )
    p_reg.add_argument(
        "--lab",
        action="append",
        default=[],
        help="only this lab (may be repeated)",
    )
    p_reg.add_argument("--limit", type=int, help="at most this many revisions")
    p_reg.add_argument(
        "--show", type=int, default=40, help="list this many deviations"
    )
    p_reg.add_argument("--output", help="write deviations as CSV")
    p_reg.set_defaults(func=cmd_regress)
    p_packages = sub.add_parser(
        "packages",
        help="Debian packages of the offered programming "
        "languages (for install.sh)",
    )
    p_packages.add_argument(
        "--defaults",
        action="store_true",
        help="the default languages, without a configuration file",
    )
    p_packages.set_defaults(func=cmd_packages)
    sub.add_parser(
        "selftest",
        help="check the sandbox and the interpreters of the offered languages",
    ).set_defaults(func=cmd_selftest)
    p_web = sub.add_parser("web", help="development server (dev mode only)")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=5000)
    p_web.add_argument(
        "--no-worker",
        action="store_true",
        help="do not start the worker in the same process",
    )
    p_web.set_defaults(func=cmd_web)
    sub.add_parser("worker", help="grade submissions (service)").set_defaults(
        func=cmd_worker
    )
    sub.add_parser(
        "healthcheck", help="check the operational state (exit 1 on problems)"
    ).set_defaults(func=cmd_healthcheck)
    p_dep = sub.add_parser(
        "deploy-render",
        help="generate operational files (Apache, systemd, logrotate)",
    )
    p_dep.add_argument(
        "--app-dir",
        required=True,
        help="installation directory (e.g. /opt/emptydiff)",
    )
    p_dep.add_argument("--out", required=True, help="output directory")
    p_dep.add_argument(
        "--accept-acme-terms",
        action="store_true",
        help="accept the terms of service of the ACME certificate authority",
    )
    p_dep.set_defaults(func=cmd_deploy_render)
    p_purge = sub.add_parser(
        "purge", help="delete submissions after the retention period"
    )
    p_purge.add_argument(
        "--dry-run", action="store_true", help="only count, delete nothing"
    )
    p_purge.set_defaults(func=cmd_purge)
    p_mail = sub.add_parser(
        "mail-test", help="send a test mail through the configured backend"
    )
    p_mail.add_argument("email")
    p_mail.set_defaults(func=cmd_mail_test)
    p_code = sub.add_parser(
        "issue-code", help="emergency route: print a login code"
    )
    p_code.add_argument("email")
    p_code.set_defaults(func=cmd_issue_code)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except config.ConfigError as e:
        sys.exit(f"Configuration error: {e}")


if __name__ == "__main__":
    main()
