# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Worker: grade the submissions of the queue (table submission),
strictly one after another.

The queue is the database itself; a restart loses nothing (stuck
'running' jobs are set back to 'queued' on start).

There is exactly one worker thread and no interpreter ever runs twice at
the same time, regardless of the machine. The work is a pipeline over
batches: fetch the oldest waiting submissions (at most
`grader.batch_size`), then take them one by one through the stages load
(files, testbench) -> execute (the interpreter in the sandbox) -> store
(result). Each result is stored right away, and a submission is marked
'running' only when its own turn begins, so at most one submission is
'running' at any time.
"""

import collections
import json
import sys
import threading
import time
import traceback

from . import db, grader, provided, report, screen, submissions, util

MAX_STORED_CHARS = 256 * 1024
SANDBOX_TRIES = 3  # a sandbox that does not start is tried again
SANDBOX_RETRY_WAIT = 1.0  # seconds before the second try, then doubled
HEARTBEAT_EVERY = 30  # seconds between two heartbeat entries while idle

Job = collections.namedtuple(
    "Job", "language testbench helpers files provided"
)


def _clip(text):
    if len(text) <= MAX_STORED_CHARS:
        return text
    return text[:MAX_STORED_CHARS] + "\n" + report.CLIP_MARK


def reset_running(conn):
    conn.execute(
        "UPDATE submission SET status = 'queued' WHERE status = 'running'"
    )
    conn.commit()


# ---------- Batch ----------


def queued_batch(conn, size):
    """IDs of the oldest waiting submissions, oldest first (empty in
    maintenance mode)."""
    if db.meta_get(conn, "maintenance"):
        return []
    rows = conn.execute(
        "SELECT id FROM submission WHERE status = 'queued' "
        "ORDER BY id LIMIT ?",
        (size,),
    )
    return [r["id"] for r in rows]


def claim(conn, sub_id):
    """Mark one waiting submission as 'running'. False if it is gone, no
    longer waiting or in maintenance mode."""
    if db.meta_get(conn, "maintenance"):
        return False
    cur = conn.execute(
        "UPDATE submission SET status = 'running' WHERE id = "
        "? AND status = 'queued'",
        (sub_id,),
    )
    conn.commit()
    return cur.rowcount == 1


# ---------- Stages ----------


def load(conn, cfg, sub_id):
    """Stage 1: everything the interpreter needs (raises OSError,
    GraderError ... if something is missing)."""
    sub = submissions.get(conn, sub_id)
    lab = conn.execute(
        "SELECT * FROM lab WHERE name = ?", (sub["lab"],)
    ).fetchone()
    return Job(
        lab["language"],
        lab["testbench"],
        lab["helpers"],
        submissions.load_files(cfg, sub),
        provided.files(lab["language"], lab["provided"]),
    )


def execute(cfg, job):
    """Stage 2: run the testbench in the sandbox. The only stage that
    starts an interpreter.

    If the sandbox itself does not start (a hiccup of the machine, not
    a wrong submission), the run is repeated a few times; only then the
    submission is stored as an error and never as "not passed"."""
    wait = SANDBOX_RETRY_WAIT
    for attempt in range(SANDBOX_TRIES):
        try:
            return grader.grade(
                cfg,
                job.testbench,
                job.files,
                job.provided,
                helpers=job.helpers,
                language=job.language,
            )
        except grader.SandboxError as e:
            print(f"Sandbox did not start: {e}", file=sys.stderr)
            if attempt == SANDBOX_TRIES - 1:
                raise
            time.sleep(wait)
            wait *= 2


def store(conn, sub_id, result, flags=()):
    """Stage 3: record the result."""
    conn.execute(
        "UPDATE submission SET status = 'done', passed = ?, "
        "checks = ?, finished = ?, flags = ?, log = ?, "
        "error_log = ?, timed_out = ?, truncated = ? WHERE id = ?",
        (
            int(result.passed),
            json.dumps([[int(ok), text] for ok, text in result.checks]),
            int(result.finished),
            "\n".join(flags),
            _clip(result.log),
            _clip(result.error_log),
            int(result.timed_out),
            int(result.truncated),
            sub_id,
        ),
    )
    conn.commit()


def store_error(conn, sub_id, error):
    conn.execute(
        "UPDATE submission SET status = 'error', error_log = ? WHERE id = ?",
        (f"{type(error).__name__}: {error}", sub_id),
    )
    conn.commit()


def process(conn, cfg, sub_id):
    """Take one claimed submission through all stages."""
    try:
        job = load(conn, cfg, sub_id)
        result = execute(cfg, job)
    # GraderError, OSError, missing files: no result for the submission
    except Exception as e:
        store_error(conn, sub_id, e)
        return
    store(
        conn,
        sub_id,
        result,
        screen.scan(
            job.files, cfg["languages"][job.language]["suspicious_words"]
        ),
    )


def run_batch(conn, cfg, stop=None):
    """Fetch one batch and grade it one submission after the other.
    Returns the number graded.

    When `stop` is set, the running submission is finished and the rest
    stays queued.
    """
    done = 0
    for sub_id in queued_batch(conn, cfg["grader"]["batch_size"]):
        if stop is not None and stop.is_set():
            break
        if not claim(
            conn, sub_id
        ):  # deleted, regraded or paused in the meantime
            continue
        heartbeat(conn)
        process(conn, cfg, sub_id)
        done += 1
    return done


def heartbeat(conn, now=None):
    db.meta_set(conn, "worker_heartbeat", util.to_iso(now or util.utcnow()))


def _loop(cfg, stop, poll):
    conn = db.connect(db.path_for(cfg))
    last_beat = 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() - last_beat >= HEARTBEAT_EVERY:
                    heartbeat(conn)
                    last_beat = time.monotonic()
                worked = run_batch(conn, cfg, stop)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                worked = 0
            if not worked:
                stop.wait(poll)
    finally:
        conn.close()


def start(cfg, stop, poll=2.0):
    """Start the single worker thread and return it."""
    conn = db.connect(db.path_for(cfg))
    reset_running(conn)
    conn.close()
    grader.cleanup_run_dir(
        cfg
    )  # nothing is running yet: what is there was left by a worker that died
    thread = threading.Thread(
        target=_loop, args=(cfg, stop, poll), daemon=True
    )
    thread.start()
    return thread
