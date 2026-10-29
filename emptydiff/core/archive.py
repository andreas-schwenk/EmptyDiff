# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Archive of a whole lab as a ZIP file (DESIGN.md section 18). Standard
library only.

Layout (everything below `<lab>/`):
  README.txt, lab.json (configuration), testbench<ext>, helpers<ext>,
  provided<ext>, overview.csv,
  submissions/<student>/<no>/<files>, reports/<student>/<no>.txt
"""

import csv
import io
import json
import re
import zipfile

from . import i18n, languages, report, submissions, util

README = """Archive of the lab {name} (EmptyDiff)

Created on {created} (UTC).

lab.json               configuration of the lab (language, deadline,
                       required files, ...)
testbench{ext}            the testbench (body)
helpers{ext}              helper functions of the testbench (if any)
provided{ext}             the provided functions (if any)
overview.csv           all submissions with time, state and result
submissions/           submitted files per student and submission
reports/               the report of every submission

The archive contains personal data (mail addresses, submissions). Please
treat it as confidential and delete it securely after use.
"""


def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


def write(conn, cfg, lab_name, fileobj, now=None):
    """Write the archive to `fileobj` (binary, seekable). Returns the
    number of submissions."""
    lab = conn.execute(
        "SELECT * FROM lab WHERE name = ?", (lab_name,)
    ).fetchone()
    if lab is None:
        raise KeyError(lab_name)
    now = now or util.utcnow()
    root = lab_name
    # an unknown language cannot be in the database: imports refuse it
    ext = languages.get(lab["language"]).extension
    dirs = {}  # address -> unique directory name

    def student_dir(email):
        if email not in dirs:
            base, n = _safe(email), 1
            while base in dirs.values():
                n += 1
                base = f"{_safe(email)}_{n}"
            dirs[email] = base
        return dirs[email]

    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            f"{root}/README.txt",
            README.format(name=lab_name, created=util.to_iso(now), ext=ext),
        )
        meta = {
            key: lab[key]
            for key in (
                "name",
                "course",
                "language",
                "description",
                "due_at",
                "show_report",
                "active",
            )
        }
        meta["max_attempts"] = cfg["limits"]["max_attempts"]
        meta["required_files"] = submissions.required_files(lab)
        z.writestr(
            f"{root}/lab.json",
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        )
        z.writestr(f"{root}/testbench{ext}", lab["testbench"])
        if lab["helpers"]:
            z.writestr(f"{root}/helpers{ext}", lab["helpers"])
        if lab["provided"]:
            z.writestr(f"{root}/provided{ext}", lab["provided"])

        table = io.StringIO()
        writer = csv.writer(table)
        writer.writerow(
            [
                "email",
                "attempt",
                "created_at",
                "late",
                "complete",
                "status",
                "passed",
                "flags",
                "missing_files",
            ]
        )
        count = 0
        for sub in conn.execute(
            "SELECT * FROM submission WHERE lab = ? ORDER BY email, attempt",
            (lab_name,),
        ):
            count += 1
            who = student_dir(sub["email"])
            writer.writerow(
                [
                    sub["email"],
                    sub["attempt"],
                    sub["created_at"],
                    sub["late"],
                    sub["complete"],
                    sub["status"],
                    "" if sub["passed"] is None else sub["passed"],
                    " ".join(sub["flags"].splitlines()),
                    " ".join(sub["missing_files"].splitlines()),
                ]
            )
            try:
                for name, content in submissions.load_files(cfg, sub).items():
                    z.writestr(
                        f"{root}/submissions/{who}/"
                        f"{sub['attempt']:03d}/{name}",
                        content,
                    )
            except OSError:
                # files are missing (e.g. already deleted): the row
                # stays in the overview
                pass
            if sub["status"] in ("done", "error"):
                z.writestr(
                    f"{root}/reports/{who}/{sub['attempt']:03d}.txt",
                    report.as_text(report.build(sub, i18n.OPERATOR_LANGUAGE))
                    + "\n",
                )
        z.writestr(f"{root}/overview.csv", table.getvalue())
    return count
