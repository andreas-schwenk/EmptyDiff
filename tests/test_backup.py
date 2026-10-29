# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from unittest import mock

from helpers import (
    FUTURE,
    GAUSS_FUNCTION,
    GOOD,
    OTHER,
    STUDENT,
    DbTestCase,
    add_lab,
    dev_cfg,
)
from emptydiff.core import backup, db, submissions, worker

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def read_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def write_zip(entries, rehash=False):
    entries = dict(entries)
    if rehash:
        manifest = json.loads(entries["manifest.json"])
        manifest["files"] = {
            n: hashlib.sha256(
                c if isinstance(c, bytes) else c.encode()
            ).hexdigest()
            for n, c in entries.items()
            if n != "manifest.json"
        }
        entries["manifest.json"] = json.dumps(manifest)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


class BackupTestCase(DbTestCase):
    def fill(self):
        add_lab(
            self.conn,
            "am24-1a",
            required="addone.m\nplot.png\n",
            provided=GAUSS_FUNCTION,
            helpers="function y = twice(x)\n  y = 2 * x;\nend\n",
        )
        add_lab(self.conn, "am24-1b", active=0, show_report=0)
        ids = {}
        for email, attempts in ((STUDENT, 2), (OTHER, 1)):
            for n in range(attempts):
                sid = submissions.submit(
                    self.conn,
                    self.cfg,
                    "am24-1a",
                    email,
                    {
                        "addone.m": GOOD + f"% {n}\n".encode(),
                        "plot.png": b"\x89PNG\x00\x01",
                    },
                    now=NOW,
                )
                self.conn.execute(
                    "UPDATE submission SET status = 'done', passed = ?, "
                    "log = ?, error_log = '', finished = 1, "
                    "checks = ?, flags = ? WHERE id = ?",
                    (
                        n % 2,
                        f"{n}\n",
                        f'[[{n % 2}, "check {n}"]]',
                        "exit" if n else "",
                        sid,
                    ),
                )
                self.conn.commit()
                ids[(email, n + 1)] = sid
        return ids

    def export(self, kind="full"):
        buf = io.BytesIO()
        counts = backup.export(self.conn, self.cfg, buf, kind, now=NOW)
        return buf.getvalue(), counts

    def archive_of(self, data):
        path = os.path.join(self.tmp, "in.zip")
        with open(path, "wb") as f:
            f.write(data)
        return backup.inspect(path)

    def second_system(self):
        """A second, empty system (different database and different data
        directory)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = dev_cfg(tmp.name)
        conn = db.init_db(db.path_for(cfg))
        self.addCleanup(conn.close)
        return cfg, conn

    def rows(self, conn, table, order):
        return [
            dict(r)
            for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]


class ExportImportTest(BackupTestCase):
    def test_full_roundtrip_restores_everything(self):
        self.fill()
        data, counts = self.export()
        self.assertEqual(
            counts, {"labs": 2, "submissions": 3, "submission_files": 6}
        )
        cfg2, conn2 = self.second_system()
        archive = self.archive_of(data)
        self.assertEqual(
            (archive.kind, archive.counts["submissions"]), ("full", 3)
        )
        backup.restore_full(conn2, cfg2, archive)

        self.assertEqual(
            self.rows(conn2, "lab", "name"),
            self.rows(self.conn, "lab", "name"),
        )
        self.assertEqual(
            conn2.execute(
                "SELECT provided FROM lab WHERE name = 'am24-1a'"
            ).fetchone()[0],
            GAUSS_FUNCTION,
        )
        self.assertEqual(
            self.rows(conn2, "submission", "id"),
            self.rows(self.conn, "submission", "id"),
        )
        for sub in conn2.execute("SELECT * FROM submission"):
            self.assertEqual(
                submissions.load_files(cfg2, sub),
                submissions.load_files(self.cfg, sub),
            )

    def test_archive_contains_no_secrets_or_sessions(self):
        self.fill()
        from emptydiff.core import auth

        auth.issue_code(self.conn, STUDENT, "203.0.113.9")
        auth.issue_remember_token(self.conn, STUDENT, 180)
        with open(os.path.join(self.cfg["data_dir"], "secret_key"), "w") as f:
            f.write("geheim")
        names = read_zip(self.export()[0]).keys()
        self.assertEqual(
            {n.split("/")[0] for n in names},
            {"manifest.json", "labs.json", "submissions.json", "submissions"},
        )
        blob = b"".join(read_zip(self.export()[0]).values())
        self.assertNotIn(b"203.0.113.9", blob)
        self.assertNotIn(b"geheim", blob)

    def test_submission_files_are_stored_under_pseudonymized_paths(self):
        self.fill()
        names = read_zip(self.export()[0]).keys()
        h = submissions.user_hash(STUDENT)
        self.assertIn(f"submissions/am24-1a/{h}/001/addone.m", names)
        self.assertFalse(any(STUDENT in n for n in names))

    def test_restore_replaces_existing_data_completely(self):
        self.fill()
        data, _ = self.export()
        cfg2, conn2 = self.second_system()
        add_lab(conn2, "am20-9z")
        submissions.submit(
            conn2, cfg2, "am20-9z", OTHER, {"addone.m": GOOD}, now=NOW
        )
        old_dir = os.path.join(cfg2["data_dir"], "submissions", "am20-9z")
        self.assertTrue(os.path.isdir(old_dir))
        backup.restore_full(conn2, cfg2, self.archive_of(data))
        self.assertEqual(
            [
                r["name"]
                for r in conn2.execute("SELECT name FROM lab ORDER BY name")
            ],
            ["am24-1a", "am24-1b"],
        )
        self.assertFalse(os.path.exists(old_dir))
        self.assertEqual(
            [
                n
                for n in os.listdir(cfg2["data_dir"])
                if n.startswith(("import-", "submissions.old"))
            ],
            [],
        )

    def test_restoring_into_an_empty_directory_without_submissions_folder(
        self,
    ):
        add_lab(self.conn)
        data, _ = self.export()
        cfg2, conn2 = self.second_system()
        shutil.rmtree(
            os.path.join(cfg2["data_dir"], "submissions"), ignore_errors=True
        )
        backup.restore_full(conn2, cfg2, self.archive_of(data))
        self.assertTrue(
            os.path.isdir(os.path.join(cfg2["data_dir"], "submissions"))
        )

    def test_running_jobs_become_queued_and_ids_survive(self):
        ids = self.fill()
        self.conn.execute(
            "UPDATE submission SET status = 'running' WHERE id = ?",
            (ids[(OTHER, 1)],),
        )
        self.conn.commit()
        cfg2, conn2 = self.second_system()
        backup.restore_full(conn2, cfg2, self.archive_of(self.export()[0]))
        row = submissions.get(conn2, ids[(OTHER, 1)])
        self.assertEqual((row["status"], row["email"]), ("queued", OTHER))

    def test_labs_export_has_no_personal_data(self):
        self.fill()
        data, counts = self.export("labs")
        self.assertEqual(counts["submissions"], 0)
        names = read_zip(data).keys()
        self.assertFalse(any(n.startswith("submissions") for n in names))
        blob = b"".join(read_zip(data).values())
        self.assertNotIn(STUDENT.encode(), blob)
        self.assertEqual(self.archive_of(data).kind, "labs")

    def test_manifest(self):
        self.fill()
        manifest = json.loads(read_zip(self.export()[0])["manifest.json"])
        self.assertEqual(
            (
                manifest["format"],
                manifest["format_version"],
                manifest["kind"],
                manifest["created_at"],
            ),
            ("emptydiff-backup", 4, "full", "2026-10-01T12:00:00Z"),
        )
        self.assertEqual(manifest["schema_version"], len(db.MIGRATIONS))
        self.assertIn("labs.json", manifest["files"])


class RestoreSafetyTest(BackupTestCase):
    def setUp(self):
        super().setUp()
        self.fill()
        self.data, _ = self.export()
        self.cfg2, self.conn2 = self.second_system()
        add_lab(self.conn2, "am20-9z")
        submissions.submit(
            self.conn2,
            self.cfg2,
            "am20-9z",
            OTHER,
            {"addone.m": GOOD},
            now=NOW,
        )

    def untouched(self):
        self.assertEqual(
            [r["name"] for r in self.conn2.execute("SELECT name FROM lab")],
            ["am20-9z"],
        )
        self.assertEqual(
            self.conn2.execute("SELECT COUNT(*) FROM submission").fetchone()[
                0
            ],
            1,
        )
        self.assertTrue(
            os.path.isdir(
                os.path.join(self.cfg2["data_dir"], "submissions", "am20-9z")
            )
        )
        self.assertFalse(db.meta_get(self.conn2, "maintenance"))
        self.assertEqual(
            [
                n
                for n in os.listdir(self.cfg2["data_dir"])
                if n.startswith(("import-", "submissions.old"))
            ],
            [],
        )

    def test_failure_while_inserting_keeps_the_old_data(self):
        archive = self.archive_of(self.data)
        with mock.patch.object(
            backup, "_insert_all", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                backup.restore_full(self.conn2, self.cfg2, archive)
        self.untouched()

    def test_failure_during_the_directory_swap_restores_the_old_directory(
        self,
    ):
        archive = self.archive_of(self.data)
        real = os.rename
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if (
                len(calls) == 2
            ):  # second rename: put the new directory in place
                raise OSError("Platte voll")
            return real(src, dst)

        with mock.patch("os.rename", flaky):
            with self.assertRaises(OSError):
                backup.restore_full(self.conn2, self.cfg2, archive)
        self.untouched()

    def test_running_evaluation_blocks_the_restore(self):
        sid = self.conn2.execute("SELECT id FROM submission").fetchone()[0]
        self.conn2.execute(
            "UPDATE submission SET status = 'running' WHERE id = ?", (sid,)
        )
        self.conn2.commit()
        with self.assertRaises(backup.BackupError) as ctx:
            backup.restore_full(
                self.conn2,
                self.cfg2,
                self.archive_of(self.data),
                wait_seconds=0,
            )
        self.assertIn("evaluated", str(ctx.exception))
        self.assertFalse(db.meta_get(self.conn2, "maintenance"))

    def test_maintenance_mode_pauses_worker_and_blocks_submissions(
        self,
    ):
        seen = {}
        real = backup._insert_all

        def spy(conn, archive):
            seen["maintenance"] = db.meta_get(conn, "maintenance")
            seen["batch"] = worker.queued_batch(self.conn2, 10)
            with self.assertRaises(submissions.SubmissionError) as ctx:
                submissions.submit(
                    self.conn2,
                    self.cfg2,
                    "am20-9z",
                    STUDENT,
                    {"addone.m": GOOD},
                )
            seen["message"] = str(ctx.exception)
            return real(conn, archive)

        with mock.patch.object(backup, "_insert_all", spy):
            backup.restore_full(
                self.conn2, self.cfg2, self.archive_of(self.data)
            )
        self.assertTrue(seen["maintenance"])
        self.assertEqual(seen["batch"], [])
        self.assertIn("maintenance", seen["message"])
        self.assertFalse(db.meta_get(self.conn2, "maintenance"))
        self.assertIsNotNone(
            submissions.submit(
                self.conn2, self.cfg2, "am24-1a", STUDENT, {"addone.m": GOOD}
            )
        )

    def test_wrong_kind_is_refused(self):
        labs_archive = self.archive_of(self.export("labs")[0])
        with self.assertRaises(backup.BackupError):
            backup.restore_full(self.conn2, self.cfg2, labs_archive)
        full = self.archive_of(self.data)
        with self.assertRaises(backup.BackupError):
            backup.import_labs(self.conn2, full)
        self.untouched()


class ImportLabsTest(BackupTestCase):
    def test_new_labs_are_inactive_existing_ones_are_overwritten(
        self,
    ):
        self.fill()
        data, _ = self.export("labs")
        cfg2, conn2 = self.second_system()
        add_lab(conn2, "am24-1a", active=1)
        conn2.execute(
            "UPDATE lab SET description = 'alt', testbench = "
            "'alt' WHERE name = 'am24-1a'"
        )
        conn2.execute(
            "UPDATE lab SET provided = 'function weg()\nend\n' "
            "WHERE name = 'am24-1a'"
        )
        conn2.commit()
        submissions.submit(
            conn2, cfg2, "am24-1a", OTHER, {"addone.m": GOOD}, now=NOW
        )

        archive = self.archive_of(data)
        self.assertEqual(
            backup.classify_labs(conn2, archive), (["am24-1b"], ["am24-1a"])
        )
        new, updated = backup.import_labs(conn2, archive)
        self.assertEqual((new, updated), (["am24-1b"], ["am24-1a"]))
        lab = dict(
            conn2.execute(
                "SELECT * FROM lab WHERE name = 'am24-1a'"
            ).fetchone()
        )
        self.assertEqual(
            (lab["description"], lab["active"]), ("Beschreibung am24-1a", 1)
        )  # active state is kept
        self.assertNotEqual(lab["testbench"], "alt")
        self.assertEqual(
            lab["provided"], GAUSS_FUNCTION
        )  # the provided functions are overwritten too
        self.assertEqual(
            conn2.execute(
                "SELECT active FROM lab WHERE name = 'am24-1b'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn2.execute("SELECT COUNT(*) FROM submission").fetchone()[0], 1
        )


class ValidationTest(BackupTestCase):
    def setUp(self):
        super().setUp()
        self.fill()
        self.entries = read_zip(self.export()[0])

    def reject(self, entries, text=None, rehash=True):
        with self.assertRaises(backup.BackupError) as ctx:
            self.archive_of(write_zip(entries, rehash=rehash))
        if text:
            self.assertIn(text, str(ctx.exception))

    def test_good_archive_passes(self):
        self.assertEqual(
            self.archive_of(write_zip(self.entries)).counts["submissions"], 3
        )

    def test_not_a_zip_and_not_a_emptydiff_archive(self):
        with self.assertRaises(backup.BackupError):
            self.archive_of(b"das ist kein zip")
        self.reject({"x.txt": "a"}, "manifest.json is missing", rehash=False)
        self.reject(
            {"manifest.json": json.dumps({"format": "etwas-anderes"})},
            "not a EmptyDiff archive",
            rehash=False,
        )

    def test_tampered_file_is_detected_by_checksum(self):
        self.entries[
            f"submissions/am24-1a/{submissions.user_hash(STUDENT)}"
            "/001/addone.m"
        ] = b"disp('manipuliert')"
        self.reject(self.entries, "Checksum", rehash=False)

    def test_file_list_must_match_manifest(self):
        extra = dict(self.entries, **{"submissions.txt": b"x"})
        self.reject(extra, "do not match the manifest", rehash=False)
        missing = {
            k: v for k, v in self.entries.items() if k != "submissions.json"
        }
        self.reject(missing, "do not match the manifest", rehash=False)

    def test_newer_format_and_bad_kind(self):
        manifest = json.loads(self.entries["manifest.json"])
        self.reject(
            dict(
                self.entries,
                **{
                    "manifest.json": json.dumps(
                        {**manifest, "format_version": 5}
                    )
                },
            ),
            "newer",
            rehash=False,
        )
        # format 3 had no language of the labs, format 2 still had
        # provided files as extra entries
        for old in (
            1,
            2,
            3,
        ):
            self.reject(
                dict(
                    self.entries,
                    **{
                        "manifest.json": json.dumps(
                            {**manifest, "format_version": old}
                        )
                    },
                ),
                "older version",
                rehash=False,
            )
        self.reject(
            dict(
                self.entries,
                **{"manifest.json": json.dumps({**manifest, "kind": "alles"})},
            ),
            "Unknown kind",
            rehash=False,
        )

    def test_path_tricks_are_rejected(self):
        for bad in (
            "../evil.m",
            "/etc/passwd",
            "submissions/../../x",
            "submissions\\x",
            "a//b",
        ):
            with self.subTest(bad=bad):
                self.reject(
                    dict(self.entries, **{bad: b"x"}),
                    "Invalid path",
                    rehash=False,
                )

    def test_unexpected_and_orphan_entries(self):
        self.reject(
            dict(self.entries, **{"sonstiges/datei.txt": b"x"}), "Unexpected"
        )
        h = submissions.user_hash("jemand.anderes@student.example.edu")
        self.reject(
            dict(
                self.entries, **{f"submissions/am24-1a/{h}/001/addone.m": b"x"}
            ),
            "without a matching submission",
        )
        self.reject(
            dict(self.entries, **{"lab_files/am24-1a/gauss.m": b"x"}),
            "Unexpected",
        )  # format 2 layout

    def test_invalid_file_names(self):
        h = submissions.user_hash(STUDENT)
        self.reject(
            dict(
                self.entries,
                **{f"submissions/am24-1a/{h}/001/bad name.m": b"x"},
            ),
            "Invalid submission file",
        )
        self.reject(
            self.edit_json(
                "labs.json", lambda d: d[0].update(provided="x = 1;\n")
            ),
            "Invalid value",
        )

    def edit_json(self, name, fn):
        data = json.loads(self.entries[name])
        fn(data)
        return dict(self.entries, **{name: json.dumps(data)})

    def test_invalid_lab_and_submission_data(self):
        self.reject(
            self.edit_json("labs.json", lambda d: d[0].update(name="../x")),
            "lab name",
        )
        self.reject(
            self.edit_json("labs.json", lambda d: d.append(dict(d[0]))),
            "twice",
        )
        self.reject(
            self.edit_json(
                "labs.json", lambda d: d[0].update(due_at="gestern")
            ),
            "timestamp",
        )
        self.reject(
            self.edit_json(
                "labs.json", lambda d: d[0].update(required_files="a b.m")
            ),
            "required file",
        )
        self.reject(
            self.edit_json(
                "submissions.json", lambda d: d[0].update(lab="am99-9z")
            ),
            "unknown lab",
        )
        self.reject(
            self.edit_json(
                "submissions.json", lambda d: d[0].update(email="a b")
            ),
            "address",
        )
        self.reject(
            self.edit_json(
                "submissions.json",
                lambda d: d[0].update(email=d[0]["email"].upper()),
            ),
            "address",
        )  # lower case only
        self.reject(
            self.edit_json(
                "submissions.json", lambda d: d[0].update(status="kaputt")
            ),
            "status",
        )
        self.reject(
            self.edit_json(
                "submissions.json", lambda d: d[1].update(id=d[0]["id"])
            ),
            "IDs",
        )
        self.reject(
            self.edit_json(
                "submissions.json",
                lambda d: d[1].update(
                    attempt=d[0]["attempt"], email=d[0]["email"]
                ),
            ),
            "twice",
        )
        self.reject(
            self.edit_json(
                "submissions.json", lambda d: d[0].update(attempt="1")
            ),
            "attempt",
        )
        self.reject(
            self.edit_json(
                "submissions.json", lambda d: d[0].update(passed=2)
            ),
            "passed",
        )

    def test_labs_archive_must_not_contain_submissions(self):
        entries = read_zip(self.export("labs")[0])
        manifest = json.loads(entries["manifest.json"])
        entries["submissions.json"] = "[]"
        self.reject(entries, "must not contain submissions")
        self.assertEqual(manifest["kind"], "labs")

    def test_size_limit_for_unpacked_content(self):
        with mock.patch.object(backup, "MAX_UNCOMPRESSED", 100):
            with self.assertRaises(backup.BackupError) as ctx:
                self.archive_of(write_zip(self.entries))
        self.assertIn("too large", str(ctx.exception))

    def test_checksum_is_checked_again_when_extracting(self):
        archive = self.archive_of(write_zip(self.entries))
        h = submissions.user_hash(STUDENT)
        tampered = dict(
            self.entries,
            **{f"submissions/am24-1a/{h}/001/addone.m": b"anders"},
        )
        # file swapped after the validation (e.g. in the temporary
        # storage)
        with open(archive.path, "wb") as f:
            f.write(write_zip(tampered))
        cfg2, conn2 = self.second_system()
        with self.assertRaises(backup.BackupError):
            backup.restore_full(conn2, cfg2, archive)
        self.assertEqual(
            conn2.execute("SELECT COUNT(*) FROM lab").fetchone()[0], 0
        )
        self.assertFalse(db.meta_get(conn2, "maintenance"))


if __name__ == "__main__":
    unittest.main()
