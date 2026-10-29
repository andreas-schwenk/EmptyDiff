# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Burst load test for a running installation: many students upload at
once (as shortly before a deadline) and the checks run one after the
other. Standard library plus the application itself.

Run it on the server as the service user, so that it can read the secret
key and the database (it logs the test students in by signing session
cookies, which goes through Apache, Flask, the database and the worker
exactly like real uploads):

    cd /opt/emptydiff && sudo -u emptydiff PYTHONPATH=/opt/emptydiff \\
        python3 dev/loadtest.py --config /etc/emptydiff/config.toml \\
        --lab am27-1a --dir /tmp/solution --students 40

--dir is a folder with the required files of the lab (a correct
solution). The lab must be visible to students.

Keep --concurrency below about 60: the script runs as the service user
and its threads count against the process limit of the sandbox
(prlimit --nproc); too many make checks fail with "bwrap: Creating new
namespace failed". The test students are `loadNN@<first allowed
domain>`; --cleanup deletes their submissions afterwards (--purge alone
removes them if a run was interrupted).
"""

import argparse
import http.client
import os
import secrets
import sqlite3
import statistics
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from emptydiff import web  # noqa: E402
from emptydiff.core import config, db, labs  # noqa: E402


def multipart(fields, files):
    """Body and content type of a multipart/form-data request."""
    boundary = "----load" + secrets.token_hex(8)
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    for name, content in files.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="files"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload(url, cookie, csrf, lab, files):
    """POST one submission; returns (seconds, submission id or None,
    HTTP status)."""
    parts = urlsplit(url)
    body, ctype = multipart({"csrf_token": csrf}, files)
    start = time.monotonic()
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80)
    try:
        conn.request(
            "POST",
            f"/lab/{lab}",
            body,
            {"Content-Type": ctype, "Cookie": f"session={cookie}"},
        )
        resp = conn.getresponse()
        resp.read()
        where = resp.getheader("Location") or ""
    finally:
        conn.close()
    sub_id = None
    if resp.status == 302 and "/submission/" in where:
        sub_id = int(where.rsplit("/", 1)[1])
    return time.monotonic() - start, sub_id, resp.status


def cpu_temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def purge(cfg, lab, domain):
    """Delete all submissions of the test students (load..) of a lab."""
    conn = db.init_db(db.path_for(cfg))
    rows = conn.execute(
        "SELECT id FROM submission WHERE lab = ? AND email LIKE ?",
        (lab, f"load%@{domain}"),
    ).fetchall()
    for (sub_id,) in rows:
        try:
            labs.delete_submission(conn, cfg, sub_id)
        except labs.LabError as e:
            print(f"  not deleted: {sub_id}: {e}")
    conn.close()
    print(f"deleted {len(rows)} test submissions")


def percentile(values, share):
    values = sorted(values)
    return values[min(int(share * len(values)), len(values) - 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--url", default="http://127.0.0.1")
    ap.add_argument("--lab", required=True)
    ap.add_argument("--dir", help="the files to upload")
    ap.add_argument("--students", type=int, default=30)
    ap.add_argument("--per-student", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument(
        "--purge",
        action="store_true",
        help="only delete the submissions of all test students",
    )
    args = ap.parse_args()

    cfg = config.load(args.config)
    app = web.create_app(cfg)
    signer = app.session_interface.get_signing_serializer(app)
    domain = cfg["auth"]["allowed_domains"][0]
    if args.purge:
        purge(cfg, args.lab, domain)
        return
    if not args.dir:
        sys.exit("--dir is required")
    directory = os.path.expanduser(args.dir)
    files = {}
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), "rb") as f:
            files[name] = f.read()
    path = db.path_for(cfg)
    watch = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    queue_before = watch.execute(
        "SELECT COUNT(*) FROM submission WHERE status IN "
        "('queued', 'running')"
    ).fetchone()[0]

    results = []  # (student, seconds, sub_id, status)
    lock = threading.Lock()
    waiting = list(range(args.students, 0, -1))

    def student():
        while True:
            with lock:
                if not waiting:
                    return
                number = waiting.pop()
            email = f"load{number:02d}@{domain}"
            csrf = secrets.token_urlsafe(24)
            cookie = signer.dumps({"email": email, "csrf": csrf, "lang": "DE"})
            for _ in range(args.per_student):
                row = upload(args.url, cookie, csrf, args.lab, files)
                with lock:
                    results.append((email, *row))

    print(
        f"{args.students} students x {args.per_student} upload(s) of "
        f"{', '.join(files)} to {args.lab}, {args.concurrency} at a time"
        + (f" ({queue_before} already waiting)" if queue_before else "")
    )
    base = watch.execute(
        "SELECT COALESCE(MAX(id), 0) FROM submission"
    ).fetchone()[0]
    start = time.monotonic()
    threads = [
        threading.Thread(target=student)
        for _ in range(min(args.concurrency, args.students))
    ]
    for thread in threads:
        thread.start()

    done_at = {}  # id -> (seconds after the start, status, passed)
    peak_temp = cpu_temperature()
    uploaded = None
    accepted = []
    while True:
        now = time.monotonic() - start
        if uploaded is None and not any(t.is_alive() for t in threads):
            uploaded = now
            accepted = [r for r in results if r[2] is not None]
        for sub_id, status, passed in watch.execute(
            "SELECT id, status, passed FROM submission WHERE lab = ? "
            "AND id > ? AND email LIKE 'load%'",
            (args.lab, base),
        ).fetchall():
            if status in ("done", "error") and sub_id not in done_at:
                done_at[sub_id] = (now, status, passed)
        temp = cpu_temperature()
        if temp and (not peak_temp or temp > peak_temp):
            peak_temp = temp
        if uploaded is not None and len(done_at) >= len(accepted):
            break
        if now > args.timeout:
            break
        time.sleep(0.1)

    ids = [r[2] for r in accepted]
    failed = [r for r in results if r[2] is None]
    for email, seconds, _, status in failed[:5]:
        print(f"  upload failed: {email}: HTTP {status}")
    if failed and not ids:
        sys.exit(
            "No upload was accepted (is the lab visible, is the session "
            "valid, is the attempt limit reached?)."
        )
    total = max((t for t, _, _ in done_at.values()), default=uploaded)

    uploads = [r[1] for r in results]
    finished = sorted(t for t, _, _ in done_at.values())
    gaps = [b - a for a, b in zip(finished, finished[1:])]
    passed = sum(1 for _, s, p in done_at.values() if s == "done" and p)
    print()
    print(f"uploads accepted      {len(ids)} of {len(results)}")
    print(
        f"upload time (s)       median {statistics.median(uploads):.2f}, "
        f"95% {percentile(uploads, 0.95):.2f}, max {max(uploads):.2f}"
    )
    print(f"all uploaded after    {uploaded:.1f} s")
    print(
        f"checks finished       {len(done_at)} of {len(ids)}, "
        f"passed {passed}"
    )
    if finished:
        print(
            f"first / median / last {finished[0]:.1f} / "
            f"{statistics.median(finished):.1f} / {finished[-1]:.1f} s "
            "after the start"
        )
        print(f"throughput            {len(done_at) / total * 60:.0f} per min")
    if gaps:
        print(f"time per check (s)    mean {statistics.mean(gaps):.2f}")
    if peak_temp:
        print(f"highest CPU temp      {peak_temp:.1f} C")
    with open("/proc/loadavg") as f:
        print("load average          " + f.read().split()[0])
    shown = 0
    for bad, (when, status, ok) in sorted(done_at.items()):
        if status == "done" and ok:
            continue
        row = watch.execute(
            "SELECT email, checks, error_log, log FROM submission "
            "WHERE id = ?",
            (bad,),
        ).fetchone()
        print(f"  not passed: submission {bad} ({status}) {row[0]}")
        if shown < 3:
            for text in row[1:]:
                tail = "\n".join((text or "").strip().split("\n")[-6:])
                print("    | " + tail.replace("\n", "\n    | "))
        shown += 1

    if args.cleanup:
        purge(cfg, args.lab, domain)


if __name__ == "__main__":
    main()
