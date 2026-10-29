# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Conspicuous submissions: student files that contain words which have
no business in a solution.

This is a hint for admins only (nothing is blocked and students are not
told), because the words also appear in harmless places such as
comments. Standard library only.
"""

import re

MAX_SCAN_BYTES = 1024 * 1024


def _pattern(word):
    # a word counts only as a whole word: 'exit' matches 'exit(0)' but
    # not 'exited'
    return re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])"
    )


def scan(files, words):
    """Words (in the order of `words`) that occur in at least one text
    file of `files` {name: bytes}."""
    texts = [
        content[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
        for content in files.values()
        if b"\x00" not in content[:MAX_SCAN_BYTES]
    ]
    return [
        w for w in words if any(_pattern(w).search(text) for text in texts)
    ]
