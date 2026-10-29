# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Backup: export and import as ZIP (DESIGN.md section 19). Standard
library only.

Two kinds of archives (`manifest.json`, `kind`):
  full  all labs (language, configuration, testbench, helper and
        provided functions) and all submissions (rows, files, reports,
        results)
  labs  only the labs, without any student data

Not included: login codes, remember-me tokens, session key,
configuration, operational data (`meta`).

Layout:
  manifest.json     format, version, kind, time, counters, SHA-256 of
                    all other files
  labs.json         list of labs
  submissions.json  list of submissions (full only)
  submissions/<lab>/<hash>/<no>/<file>
                    submission files (full only), same layout as on
                    the server

On import, no path from the archive is ever used for writing: target
paths result from the validated rows (lab, address, number) and from
file names that satisfy `grader.FILENAME_RE`.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import time
import zipfile

from . import db, grader, i18n, labs, languages, provided, submissions, util

FORMAT = "emptydiff-backup"
# 4: every lab has a language; 3: provided functions are a text of the
# lab (no files)
FORMAT_VERSION = 4
MIN_FORMAT_VERSION = (
    4  # older archives have another layout or no language and are refused
)
KINDS = ("full", "labs")
MAX_UNCOMPRESSED = 2 * 1024**3  # sum of all unpacked files
MAX_ENTRIES = 500_000
MAX_JSON_BYTES = 256 * 1024**2
MAX_FIELD_CHARS = 2_000_000  # log, error log of a submission
CHUNK = 1024 * 1024
STATUSES = ("queued", "running", "done", "error")

LAB_FIELDS = (
    "name",
    "course",
    "language",
    "description",
    "due_at",
    "required_files",
    "show_report",
    "testbench",
    "helpers",
    "provided",
    "active",
)
SUBMISSION_FIELDS = (
    "id",
    "lab",
    "email",
    "attempt",
    "created_at",
    "late",
    "complete",
    "status",
    "passed",
    "log",
    "error_log",
    "missing_files",
    "timed_out",
    "truncated",
    "checks",
    "finished",
    "flags",
)
EMAIL_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{3,254}$")
ATTEMPT_DIR_RE = re.compile(r"^[0-9]{3,6}$")
HASH_RE = re.compile(r"^[0-9a-f]{16}$")


class BackupError(i18n.LocalizedError):
    """Error with a message that admins can read (and that can be
    translated)."""


# ---------- Export ----------


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def export(conn, cfg, fileobj, kind="full", now=None):
    """Write an archive to `fileobj` (binary, seekable). Returns the
    counters."""
    if kind not in KINDS:
        raise ValueError(kind)
    now = now or util.utcnow()
    hashes = {}
    counts = {"labs": 0, "submissions": 0, "submission_files": 0}

    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as z:

        def add(name, data):
            data = data.encode("utf-8") if isinstance(data, str) else data
            z.writestr(name, data)
            hashes[name] = _sha256(data)

        conn.commit()
        conn.execute("BEGIN")  # one read snapshot for labs and submissions
        try:
            lab_rows = [
                dict(r)
                for r in conn.execute(
                    f"SELECT {', '.join(LAB_FIELDS)} FROM lab ORDER BY name"
                )
            ]
            sub_rows = (
                [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT {', '.join(SUBMISSION_FIELDS)} FROM "
                        "submission ORDER BY id"
                    )
                ]
                if kind == "full"
                else []
            )
        finally:
            conn.rollback()

        add("labs.json", json.dumps(lab_rows, ensure_ascii=False, indent=1))
        counts["labs"] = len(lab_rows)
        if kind == "full":
            add(
                "submissions.json",
                json.dumps(sub_rows, ensure_ascii=False, indent=1),
            )
            counts["submissions"] = len(sub_rows)
            for row in sub_rows:
                directory = submissions.submission_dir(
                    cfg, row["lab"], row["email"], row["attempt"]
                )
                try:
                    names = sorted(os.listdir(directory))
                except OSError:
                    # files are missing (e.g. removed by hand): the row
                    # is kept
                    continue
                rel = (
                    f"submissions/{row['lab']}/"
                    f"{submissions.user_hash(row['email'])}/"
                    f"{row['attempt']:03d}"
                )
                for name in names:
                    path = os.path.join(directory, name)
                    if grader.FILENAME_RE.match(name) and os.path.isfile(path):
                        with open(path, "rb") as f:
                            add(f"{rel}/{name}", f.read())
                        counts["submission_files"] += 1

        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "kind": kind,
            "created_at": util.to_iso(now),
            "emptydiff_version": _version(),
            "schema_version": len(db.MIGRATIONS),
            "counts": counts,
            "files": hashes,
        }
        z.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1)
        )
    return counts


def _version():
    from .. import __version__

    return __version__


# ---------- Validation ----------


class Archive:
    """A validated archive (contents checked, checksums match)."""

    def __init__(self, path, manifest, labs_, subs, sub_files, total_bytes):
        self.path = path
        self.manifest = manifest
        self.kind = manifest["kind"]
        self.created_at = manifest["created_at"]
        self.labs = labs_
        self.submissions = subs
        self.submission_files = (
            sub_files  # {(lab, hash, attempt): [name, ...]}
        )
        self.total_bytes = total_bytes

    @property
    def counts(self):
        return {
            "labs": len(self.labs),
            "submissions": len(self.submissions),
            "submission_files": sum(
                len(v) for v in self.submission_files.values()
            ),
        }


def _safe_name(name):
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise BackupError("err.backup.path", name=name)
    return parts


def _check_str(value, field, limit=MAX_FIELD_CHARS, allow_none=False):
    if value is None and allow_none:
        return value
    if not isinstance(value, str) or len(value) > limit:
        raise BackupError("err.backup.value", field=field)
    return value


def _check_int(value, field, low=0, high=2**31, allow_none=False):
    if value is None and allow_none:
        return value
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise BackupError("err.backup.value", field=field)
    return value


def _check_time(value, field, allow_none=False):
    if value is None and allow_none:
        return value
    try:
        util.from_iso(value)
    except (TypeError, ValueError):
        raise BackupError("err.backup.time", field=field) from None
    return value


def _read_json(z, name, info):
    if info.file_size > MAX_JSON_BYTES:
        raise BackupError("err.backup.too_big", name=name)
    try:
        return json.loads(z.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("err.backup.damaged", name=name) from None


def _check_language(value, lab):
    """The identifier of a language that this program has an adapter for
    (it need not be offered on this server)."""
    language = _check_str(value, f"{lab}.language", 32)
    try:
        languages.get(language)
    except languages.UnknownLanguage:
        raise BackupError(
            "err.backup.language", lab=lab, language=language
        ) from None
    return language


def _check_provided(value, lab, language):
    text = _check_str(value, f"{lab}.provided", provided.MAX_CHARS)
    try:
        provided.split(language, text)
    except provided.ProvidedError:
        raise BackupError(
            "err.backup.value", field=f"{lab}.provided"
        ) from None
    return text


def _clean_lab(raw):
    if not isinstance(raw, dict):
        raise BackupError("err.backup.not_object", name="labs.json")
    name = _check_str(raw.get("name"), "lab.name", 64)
    if not labs.NAME_RE.match(name):
        raise BackupError("err.backup.lab_name", name=name)
    required = _check_str(
        raw.get("required_files", ""), f"{name}.required_files", 10_000
    )
    if any(
        line and not grader.FILENAME_RE.match(line)
        for line in required.split("\n")
    ):
        raise BackupError("err.backup.required", name=name)
    language = _check_language(raw.get("language"), name)
    return {
        "name": name,
        "course": labs.course_of(name),
        "language": language,
        "description": _check_str(
            raw.get("description", ""),
            f"{name}.description",
            labs.MAX_DESCRIPTION_CHARS,
        ),
        "due_at": _check_time(raw.get("due_at"), f"{name}.due_at"),
        "required_files": required,
        "show_report": _check_int(
            raw.get("show_report", 1), f"{name}.show_report", 0, 1
        ),
        "testbench": _check_str(
            raw.get("testbench"), f"{name}.testbench", labs.MAX_TESTBENCH_CHARS
        ),
        "helpers": _check_str(
            raw.get("helpers", ""), f"{name}.helpers", labs.MAX_HELPERS_CHARS
        ),
        "provided": _check_provided(raw.get("provided", ""), name, language),
        "active": _check_int(raw.get("active", 1), f"{name}.active", 0, 1),
    }


def _check_checks(value):
    """The recorded checks are stored as a JSON text: a list of [ok,
    text] pairs."""
    text = _check_str(value, "submission.checks", MAX_FIELD_CHARS)
    try:
        items = json.loads(text)
    except ValueError:
        items = None
    if not isinstance(items, list) or not all(
        isinstance(i, list)
        and len(i) == 2
        and isinstance(i[0], int)
        and isinstance(i[1], str)
        for i in items
    ):
        raise BackupError("err.backup.value", field="submission.checks")
    return text


def _clean_submission(raw, lab_names):
    if not isinstance(raw, dict):
        raise BackupError("err.backup.not_object", name="submissions.json")
    lab = _check_str(raw.get("lab"), "submission.lab", 64)
    if lab not in lab_names:
        raise BackupError("err.backup.unknown_lab", name=lab)
    email = raw.get("email")
    if (
        not isinstance(email, str)
        or not EMAIL_RE.match(email)
        or email != email.lower()
    ):  # stored in lower case only
        raise BackupError("err.backup.bad_email")
    status = raw.get("status", "queued")
    if status not in STATUSES:
        raise BackupError("err.backup.bad_status", status=status)
    return {
        "id": _check_int(raw.get("id"), "submission.id", 1),
        "lab": lab,
        "email": email,
        "attempt": _check_int(
            raw.get("attempt"), "submission.attempt", 1, 999_999
        ),
        "created_at": _check_time(
            raw.get("created_at"), "submission.created_at"
        ),
        "late": _check_int(raw.get("late", 0), "submission.late", 0, 1),
        "complete": _check_int(
            raw.get("complete", 1), "submission.complete", 0, 1
        ),
        "status": status,
        "passed": _check_int(
            raw.get("passed"), "submission.passed", 0, 1, allow_none=True
        ),
        "log": _check_str(raw.get("log"), "submission.log", allow_none=True),
        "error_log": _check_str(
            raw.get("error_log"), "submission.error_log", allow_none=True
        ),
        "missing_files": _check_str(
            raw.get("missing_files", ""), "submission.missing_files", 10_000
        ),
        "timed_out": _check_int(
            raw.get("timed_out", 0), "submission.timed_out", 0, 1
        ),
        "truncated": _check_int(
            raw.get("truncated", 0), "submission.truncated", 0, 1
        ),
        "checks": _check_checks(raw.get("checks", "[]")),
        "finished": _check_int(
            raw.get("finished", 0), "submission.finished", 0, 1
        ),
        "flags": _check_str(raw.get("flags", ""), "submission.flags", 10_000),
    }


def inspect(path):
    """Validate an archive completely (layout, contents, checksums) and
    return an `Archive`."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise BackupError("err.backup.not_zip") from None
    with zf as z:
        infos = {}
        total = 0
        for info in z.infolist():
            if info.is_dir():
                continue
            _safe_name(info.filename)
            if info.filename in infos:
                raise BackupError(
                    "err.backup.duplicate_entry", name=info.filename
                )
            infos[info.filename] = info
            total += info.file_size
            if len(infos) > MAX_ENTRIES or total > MAX_UNCOMPRESSED:
                raise BackupError("err.backup.too_large")
        if "manifest.json" not in infos:
            raise BackupError("err.backup.no_manifest")
        manifest = _read_json(z, "manifest.json", infos["manifest.json"])
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BackupError("err.backup.not_emptydiff")
        version = manifest.get("format_version")
        if not isinstance(version, int) or version < 1:
            raise BackupError("err.backup.bad_version")
        if version < MIN_FORMAT_VERSION:
            raise BackupError("err.backup.too_old")
        if version > FORMAT_VERSION:
            raise BackupError("err.backup.newer", version=version)
        if manifest.get("kind") not in KINDS:
            raise BackupError("err.backup.bad_kind")
        _check_time(manifest.get("created_at"), "manifest.created_at")
        declared = manifest.get("files")
        if not isinstance(declared, dict):
            raise BackupError("err.backup.manifest_damaged")
        actual = set(infos) - {"manifest.json"}
        if actual != set(declared):
            raise BackupError("err.backup.manifest_mismatch")

        # checksums of all files (streamed; the size must not exceed the
        # size stated in the ZIP)
        for name in sorted(actual):
            info, digest, seen = infos[name], hashlib.sha256(), 0
            with z.open(info) as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    seen += len(chunk)
                    if seen > info.file_size:
                        raise BackupError(
                            "err.backup.entry_damaged", name=name
                        )
                    digest.update(chunk)
            if digest.hexdigest() != declared[name]:
                raise BackupError("err.backup.checksum", name=name)

        if "labs.json" not in infos:
            raise BackupError("err.backup.missing", name="labs.json")
        raw_labs = _read_json(z, "labs.json", infos["labs.json"])
        if not isinstance(raw_labs, list):
            raise BackupError("err.backup.damaged", name="labs.json")
        lab_list = [_clean_lab(r) for r in raw_labs]
        lab_names = [lab["name"] for lab in lab_list]
        if len(set(lab_names)) != len(lab_names):
            raise BackupError("err.backup.lab_twice")

        sub_entries = []
        for name in sorted(actual):
            parts = name.split("/")
            if name in ("labs.json", "submissions.json"):
                continue
            if parts[0] == "submissions" and len(parts) == 5:
                sub_entries.append(parts)
            else:
                raise BackupError("err.backup.unexpected", name=name)

        subs, sub_files = [], {}
        if manifest["kind"] == "labs":
            if sub_entries or "submissions.json" in infos:
                raise BackupError("err.backup.labs_with_subs")
        else:
            if "submissions.json" not in infos:
                raise BackupError(
                    "err.backup.missing", name="submissions.json"
                )
            raw_subs = _read_json(
                z, "submissions.json", infos["submissions.json"]
            )
            if not isinstance(raw_subs, list):
                raise BackupError(
                    "err.backup.damaged", name="submissions.json"
                )
            subs = [_clean_submission(r, set(lab_names)) for r in raw_subs]
            if len({s["id"] for s in subs}) != len(subs):
                raise BackupError("err.backup.dup_ids")
            if len(
                {(s["lab"], s["email"], s["attempt"]) for s in subs}
            ) != len(subs):
                raise BackupError("err.backup.dup_sub")
            known = {
                (s["lab"], submissions.user_hash(s["email"]), s["attempt"])
                for s in subs
            }
            for _, lab, who, nr, fname in sub_entries:
                if not (
                    HASH_RE.match(who)
                    and ATTEMPT_DIR_RE.match(nr)
                    and grader.FILENAME_RE.match(fname)
                ):
                    raise BackupError(
                        "err.backup.bad_sub_file",
                        name=f"submissions/{lab}/{who}/{nr}/{fname}",
                    )
                key = (lab, who, int(nr))
                if key not in known:
                    raise BackupError(
                        "err.backup.orphan_file",
                        name=f"submissions/{lab}/{who}/{nr}/{fname}",
                    )
                sub_files.setdefault(key, []).append(fname)
    return Archive(path, manifest, lab_list, subs, sub_files, total)


def classify_labs(conn, archive):
    """Which labs of the archive are new, which overwrite existing
    ones?"""
    existing = {r["name"] for r in conn.execute("SELECT name FROM lab")}
    names = [lab["name"] for lab in archive.labs]
    return [n for n in names if n not in existing], [
        n for n in names if n in existing
    ]


# ---------- Maintenance mode ----------


def _set_maintenance(conn, on):
    if on:
        db.meta_set(conn, "maintenance", "1")
    else:
        conn.execute("DELETE FROM meta WHERE key = 'maintenance'")
        conn.commit()


# ---------- Import ----------


def import_labs(conn, archive):
    """Take over the labs of an archive. New labs are created inactive,
    existing ones are overwritten (their active state is kept).
    Submissions are never touched. Returns (new, overwritten).
    """
    if archive.kind != "labs":
        raise BackupError("err.backup.need_labs")
    new, updated = classify_labs(conn, archive)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for lab in archive.labs:
            if lab["name"] in updated:
                language = conn.execute(
                    "SELECT language FROM lab WHERE name = ?", (lab["name"],)
                ).fetchone()[0]
                if (
                    language != lab["language"]
                    and conn.execute(
                        "SELECT 1 FROM submission WHERE lab = ?",
                        (lab["name"],),
                    ).fetchone()
                ):
                    raise BackupError(
                        "err.backup.language_locked", lab=lab["name"]
                    )  # as in the lab form
                conn.execute(
                    "UPDATE lab SET language = ?, description = ?, "
                    "due_at = ?, required_files = ?, show_report = ?, "
                    "testbench = ?, helpers = ?, provided = ? WHERE name = ?",
                    (
                        lab["language"],
                        lab["description"],
                        lab["due_at"],
                        lab["required_files"],
                        lab["show_report"],
                        lab["testbench"],
                        lab["helpers"],
                        lab["provided"],
                        lab["name"],
                    ),
                )
                submissions.refresh_late(conn, lab_name=lab["name"])
            else:
                conn.execute(
                    "INSERT INTO lab (name, course, language, "
                    "description, due_at, required_files, show_report, "
                    "testbench, helpers, provided, active) VALUES (?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        lab["name"],
                        lab["course"],
                        lab["language"],
                        lab["description"],
                        lab["due_at"],
                        lab["required_files"],
                        lab["show_report"],
                        lab["testbench"],
                        lab["helpers"],
                        lab["provided"],
                    ),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return new, updated


def _extract_submission_files(archive, stage_root):
    """Unpack the submission files to `stage_root/submissions/...`;
    paths come from rows, never from the archive."""
    by_key = {
        (s["lab"], submissions.user_hash(s["email"]), s["attempt"]): s
        for s in archive.submissions
    }
    cfg = {"data_dir": stage_root}
    with zipfile.ZipFile(archive.path) as z:
        for (lab, who, attempt), names in archive.submission_files.items():
            sub = by_key[(lab, who, attempt)]
            directory = submissions.submission_dir(
                cfg, lab, sub["email"], attempt
            )
            os.makedirs(directory, exist_ok=True)
            for fname in names:
                digest = hashlib.sha256()
                with (
                    z.open(
                        f"submissions/{lab}/{who}/{attempt:03d}/{fname}"
                    ) as src,
                    open(os.path.join(directory, fname), "wb") as dst,
                ):
                    for chunk in iter(lambda: src.read(CHUNK), b""):
                        digest.update(chunk)
                        dst.write(chunk)
                declared = archive.manifest["files"][
                    f"submissions/{lab}/{who}/{attempt:03d}/{fname}"
                ]
                if digest.hexdigest() != declared:
                    raise BackupError("err.backup.changed")


def _insert_all(conn, archive):
    conn.execute("DELETE FROM submission")
    conn.execute("DELETE FROM lab")
    conn.execute(
        "DELETE FROM course"
    )  # the trigger recreates the courses of the restored labs
    for lab in archive.labs:
        conn.execute(
            f"INSERT INTO lab ({', '.join(LAB_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(LAB_FIELDS))})",
            tuple(lab[k] for k in LAB_FIELDS),
        )
    for sub in archive.submissions:
        values = dict(
            sub,
            status="queued" if sub["status"] == "running" else sub["status"],
        )
        conn.execute(
            "INSERT INTO submission "
            f"({', '.join(SUBMISSION_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(SUBMISSION_FIELDS))})",
            tuple(values[k] for k in SUBMISSION_FIELDS),
        )


def restore_full(conn, cfg, archive, wait_seconds=60, sleep=time.sleep):
    """Replace the entire set of labs and submissions with the contents
    of the archive.

    During the restore the system is in maintenance mode (no new
    submissions, the worker pauses). The files are first unpacked into a
    sibling directory; then the database and the `submissions/`
    directory are switched over in one step. On an error the old data is
    kept.
    """
    if archive.kind != "full":
        raise BackupError("err.backup.need_full")
    data_dir = cfg["data_dir"]
    token = secrets.token_hex(8)
    stage = os.path.join(data_dir, f"import-{token}")
    live = os.path.join(data_dir, "submissions")
    old = os.path.join(data_dir, f"submissions.old-{token}")
    conn.commit()
    _set_maintenance(conn, True)
    try:
        deadline = time.monotonic() + wait_seconds
        while conn.execute(
            "SELECT 1 FROM submission WHERE status = 'running'"
        ).fetchone():
            if time.monotonic() > deadline:
                raise BackupError("err.backup.running")
            sleep(1)
            conn.commit()
        os.makedirs(os.path.join(stage, "submissions"), exist_ok=True)
        _extract_submission_files(archive, stage)

        conn.execute("BEGIN IMMEDIATE")
        swapped = False
        try:
            _insert_all(conn, archive)
            if os.path.exists(live):
                os.rename(live, old)
            swapped = True
            os.rename(os.path.join(stage, "submissions"), live)
            conn.commit()
        except BaseException:
            conn.rollback()
            if swapped:  # restore the directories
                if os.path.exists(live):
                    os.rename(live, os.path.join(stage, "submissions"))
                if os.path.exists(old):
                    os.rename(old, live)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        _set_maintenance(conn, False)
    return archive.counts
