# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared test helpers: dev configuration, temporary database, sample
lab."""

import copy
import os
import tempfile
import unittest
from unittest import mock

from emptydiff.core import config, db, mail
from test_grader import (
    GOOD,
    MODES,
    TESTBENCH,
)  # noqa: F401  (imported by other tests)

STUDENT = "max.muster@student.example.edu"
OTHER = "erika.beispiel@student.example.edu"
ADMIN = "dozent@example.edu"
# text of a provided function
GAUSS_FUNCTION = "function [x] = gauss(A, b)\n  x = A \\ b;\nend\n"
FUTURE = "2099-01-01T22:59:59Z"
PAST = "2020-01-01T22:59:59Z"


def dev_cfg(data_dir):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["data_dir"] = data_dir
    cfg["mail"]["backend"] = "simulate"
    cfg["grader"]["sandbox"] = MODES[-1] if MODES else "none"
    cfg["grader"]["timeout_seconds"] = 20
    cfg["ui"]["default_language"] = "DE"  # the tests check the German texts
    cfg["auth"]["admins"] = [ADMIN]
    cfg["auth"]["code_cooldown_seconds"] = 0  # tests log in again and again
    cfg["auth"]["min_form_seconds"] = 0
    cfg["auth"]["allowed_domains"] = [
        "student.example.edu",
        "example.edu",
        "student.example.org",
        "example.org",
    ]
    return cfg


def add_lab(
    conn,
    name="am24-1a",
    due=FUTURE,
    required="addone.m\n",
    testbench=TESTBENCH,
    active=1,
    show_report=1,
    provided="",
    helpers="",
):
    """`provided` is the text of the provided functions (every function
    becomes a file)."""
    conn.execute(
        "INSERT INTO lab (name, course, description, due_at, "
        "required_files, show_report, "
        "testbench, helpers, provided, active) VALUES (?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            name.split("-")[0],
            f"Beschreibung {name}",
            due,
            required,
            show_report,
            testbench,
            helpers,
            provided,
            active,
        ),
    )
    conn.commit()


class DbTestCase(unittest.TestCase):
    """Temporary database and data directory in dev mode
    (EMPTYDIFF_DEV=1)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        mail.SIMULATED.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cfg = dev_cfg(self.tmp)
        self.conn = db.init_db(db.path_for(self.cfg))
        self.addCleanup(self.conn.close)
