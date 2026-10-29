# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Octave/MATLAB (`.m` files). Standard library only.

The grader assembles one file `testbench.m` from the lab's body and
helper functions: the helpers and the two check functions `check` and
`check_close` are subfunctions of that file, so student code cannot call
them by name. The checks append one line each to a results file; the
verdict is computed from these lines, never from what Octave prints.

Provided functions: the lab stores them as one text. Every `function`
starts a new file `<name>.m` in the run folder, named after the
function. A student's file of the same name wins.
"""

import re

from . import Language, ProvidedError, TestbenchError, register


# without a display Octave uses gnuplot and warns at every plot
OCTAVE_COMMAND = "warning('off', 'Octave:gnuplot-graphics'); testbench"
FUNCTION_RE = re.compile(r"^\s*function\b", re.MULTILINE)
CHECK_RE = re.compile(r"\bcheck(_close)?\s*\(")

# Octave side of the testbench. @BODY@ and @HELPERS@ are the lab's text,
# @RESULTS@ the name of the results file and @TOKEN@ a random string
# that every recorded line has to carry. `testbench` records the end of
# the run itself, so `exit` or an error inside student code means "not
# finished".
TESTBENCH_TEMPLATE = """function testbench()
  emptydiff_body__();
  emptydiff_record__('done', '');
end

function emptydiff_body__()
@BODY@
end

@HELPERS@

function check(cond, ok_text, fail_text)
  passed = (isnumeric(cond) || islogical(cond)) && ~isempty(cond) ...
           && all(cond(:)) && ~any(isnan(double(cond(:))));
  emptydiff_verdict__(passed, ok_text, fail_text);
end

function check_close(a, b, tol, ok_text, fail_text)
  if ~(isnumeric(tol) && isscalar(tol) && tol >= 0)
    error('check_close: the tolerance must be a number that is not negative');
  end
  passed = (isnumeric(a) || islogical(a)) ...
           && (isnumeric(b) || islogical(b)) && isequal(size(a), size(b));
  if passed
    a = double(a(:));
    b = double(b(:));
    passed = all((a == b) | (abs(a - b) <= tol));
  end
  emptydiff_verdict__(passed, ok_text, fail_text);
end

function emptydiff_verdict__(passed, ok_text, fail_text)
  if passed
    emptydiff_record__('ok', ok_text);
  else
    emptydiff_record__('fail', fail_text);
  end
end

function emptydiff_record__(kind, text)
  if ~ischar(text)
    text = '';
  end
  text = regexprep(text(:).', '[\\t\\r\\n]+', ' ');
  fid = fopen('@RESULTS@', 'a');
  fprintf(fid, '%s\\t%s\\t%s\\n', '@TOKEN@', kind, text);
  fclose(fid);
end
"""


# ---------- Provided functions ----------

RESERVED = ("testbench",)  # also every name that starts with 'emptydiff_'
_FUNCTION_LINE = re.compile(
    r"^[ \t]*function\b(?P<header>[^\n]*)", re.MULTILINE
)
_NAME = re.compile(
    r"^\s*(?:(?:\[[^\]\n]*\]|[A-Za-z_]\w*)\s*=\s*)?(?P<name>[A-Za-z_]\w*)"
)
_ONLY_COMMENTS = re.compile(r"^(?:\s*|\s*[%#][^\n]*\n?)*$")


def _split(text):
    """The text of the provided functions as [(function name, source)]
    in the order of the text.

    Comments in front of a function belong to the one before it (or to
    the first one). Anything else in front of the first function is an
    error, and so are duplicate or reserved names.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    starts = [m.start() for m in _FUNCTION_LINE.finditer(text)]
    if not text.strip():
        return []
    if not starts:
        raise ProvidedError("ln.octave.err_provided_no_function")
    if not _ONLY_COMMENTS.match(text[: starts[0]] + "\n"):
        raise ProvidedError("ln.octave.err_provided_outside")
    blocks, names = [], set()
    for i, start in enumerate(starts):
        source = (
            text[
                (0 if i == 0 else start) : (
                    starts[i + 1] if i + 1 < len(starts) else len(text)
                )
            ].strip()
            + "\n"
        )
        header = _FUNCTION_LINE.search(text, start).group("header")
        match = _NAME.match(header)
        name = match.group("name") if match else ""
        if (
            not name
            or len(name) > 60
            or name in RESERVED
            or name.startswith("emptydiff_")
        ):
            raise ProvidedError(
                "ln.octave.err_provided_name", name=name or header.strip()[:30]
            )
        if name in names:
            raise ProvidedError("ln.octave.err_provided_duplicate", name=name)
        names.add(name)
        blocks.append((name, source))
    return blocks


# ---------- Highlighting ----------

KEYWORDS = frozenset(
    """
break case catch classdef continue do else elseif end end_try_catch
end_unwind_protect endclassdef endfor endfunction endif endparfor endswitch
endwhile enumeration events for function global if methods otherwise parfor
persistent properties return switch try until unwind_protect
unwind_protect_cleanup while
""".split()
)

BUILTINS = frozenset(
    """
abs all any ceil clc clear close cos cumsum diag diff disp eig error exp eye
factorial figure find fix floor fprintf hold inv isempty isnumeric ischar
length linspace log log10 log2 max mean min mod nan norm numel ones plot pi
printf prod rand rank rem reshape round sin size sort sprintf sqrt sum tan
trace zeros inf true false
""".split()
)

_TOKEN = re.compile(
    r"""
    (?P<ws>[ \t]+)
  | (?P<comment>(?:[%\#]|\.\.\.)[^\n]*)
  | (?P<string>"(?:[^"\\\n]|\\.|"")*")
  | (?P<number>(?:0[xX][0-9A-Fa-f]+
      |(?:\d+(?:\.(?!\.)\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?[ij]?))
  | (?P<word>[A-Za-z_]\w*)
  | (?P<other>.)
""",
    re.VERBOSE,
)
_SQ_STRING = re.compile(r"'(?:[^'\n]|'')*'")
_BLOCK_START = re.compile(r"^\s*[%#]\{\s*$")
_BLOCK_END = re.compile(r"^\s*[%#]\}\s*$")
_TRANSPOSE_AFTER = frozenset("])}'.") | frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _tokenize_line(line):
    pos = 0
    while pos < len(line):
        m = _TOKEN.match(line, pos)
        kind = m.lastgroup
        text = m.group()
        if (
            kind == "other"
            and text == "'"
            and (line[pos - 1] if pos else "") not in _TRANSPOSE_AFTER
        ):
            sq = _SQ_STRING.match(
                line, pos
            )  # otherwise a transpose (a') and not the start of a string
            if sq:
                kind, text = "string", sq.group()
        if kind == "word":
            kind = (
                "keyword"
                if text in KEYWORDS
                else "builtin" if text in BUILTINS else None
            )
        elif kind in ("ws", "other"):
            kind = None
        yield kind, text
        pos += len(text)


def _tokens(text):
    """Split Octave source into (kind, text); kind is None for text
    without highlighting."""
    out = []

    def add(kind, chunk):
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + chunk)
        else:
            out.append((kind, chunk))

    in_block = False
    for i, line in enumerate(text.split("\n")):
        if i:
            add(None, "\n")
        if in_block:
            if _BLOCK_END.match(line):
                in_block = False
            add("comment", line)
        elif _BLOCK_START.match(line):
            in_block = True
            add("comment", line)
        else:
            for kind, chunk in _tokenize_line(line):
                add(kind, chunk)
    return out


# ---------- The adapter ----------

SUSPICIOUS_WORDS = (
    "exit",
    "quit",
    "atexit",
    "evalin",
    "assignin",
    "evalc",
    "system",
    "unix",
    "dos",
    "shell_cmd",
    "popen",
    "popen2",
    "fopen",
    "fileread",
    "fwrite",
    "fdisp",
    "fputs",
    "getenv",
    "setenv",
    "putenv",
    "builtin",
    "dbstack",
    "testbench",
    "check_close",
    "emptydiff",
    "[OK]",
    "[Fehler]",
)


class Octave(Language):
    id = "octave"
    name = "Octave/MATLAB"
    extension = ".m"
    default_command = "octave-cli"
    default_suspicious_words = SUSPICIOUS_WORDS
    packages = (
        "octave",
        "gnuplot-nox",
        "fonts-freefont-otf",
    )  # the last two: plots without a display
    extra_ro_binds = ("/etc/fonts",)
    testbench_file = "testbench.m"
    selftest_body = "check(1 + 1 == 2, 'selftest', 'selftest failed');\n"

    def files(self, body, helpers, results_file, token):
        head, rest = TESTBENCH_TEMPLATE.split("@BODY@")
        middle, tail = rest.split("@HELPERS@")

        def fill(part):
            return part.replace("@RESULTS@", results_file).replace(
                "@TOKEN@", token
            )

        text = (
            fill(head)
            + body.rstrip()
            + fill(middle)
            + helpers.strip()
            + fill(tail)
        )
        return {self.testbench_file: text.encode("utf-8")}

    def argv(self, command):
        return [command, "--norc", "--silent", "--eval", OCTAVE_COMMAND]

    def check_testbench(self, body):
        if FUNCTION_RE.search(
            body
        ):  # the wrapper `function testbench()` is added by the system
            raise TestbenchError("ln.octave.err_tb_function")

    def has_check(self, text):
        return bool(CHECK_RE.search(text))

    def split_provided(self, text):
        return _split(text)

    def tokens(self, text):
        return _tokens(text)


OCTAVE = register(Octave())
