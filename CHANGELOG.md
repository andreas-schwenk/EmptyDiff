# Changelog

## 0.1.0 (alpha, unreleased)

First version, a rewrite of an older mail-based submission system.

- **Students:** login by one-time code sent by mail, upload of the required files (other file names are refused), report with a tick or cross per check, a configurable number of attempts per lab, late submissions accepted and marked, German and English interface.
- **Lecturers:** courses and labs managed in the browser (new labs start hidden, visibility is a switch in the lab list; lecturers see all labs in the student overview and can submit like a student, hidden labs are clearly marked), submissions with source view, download, regrade and delete, credit list with CSV, course cloning, per-lab archive, backup and restore of the whole data set or of the labs only.
- **Checks:** testbench = body plus helper functions using `check` and `check_close`; the verdict comes from a results file written by the checks, not from printed text. Octave runs in a `bubblewrap` sandbox with resource limits (size-limited `/tmp`, run folders on a fixed-size RAM disk, cgroup limits for the whole worker service), strictly one submission at a time. Labs can provide Octave functions (for example the solution of an earlier task) that every submission may call, written as text in the lab form; each function becomes its own file. Submissions containing suspicious words (for example `exit`, `evalin`, `fopen`) are flagged for admins.
- **Operation:** Apache with `mod_wsgi` and `mod_md` (or, for closed test networks, plain http with `base_url = "http://…"`), systemd worker and daily deletion run, `install.sh` for installation and update, `/healthz`, automatic deletion of submissions after the retention period, privacy statement from the configuration.
- **Programming languages:** every lab has a language (`lab.language`); each language is an adapter in `emptydiff/core/languages/` (today: Octave/MATLAB). The server offers the languages of `languages.enabled`, `install.sh` installs their packages, backups (format 4) and archives carry the language, the lab form has a language field, and general texts no longer name a language.
- **Operation tools:** `dev/loadtest.py` simulates many students uploading at once on a running installation and reports upload times, throughput and failures. If the mail with a login code cannot be sent, the login page still opens the page for the code, so that a code printed with `issue-code` can be entered.
- **Robust checks:** if bubblewrap itself does not start (for example because no process can be created at that moment), the worker repeats the check up to three times and otherwise stores an error; a submission is never rated "not passed" because of a sandbox that did not start.
- **Code style:** the Python source follows PEP 8 (79 columns, comments and docstrings 72), formatted with black and checked with pycodestyle (`dev/docker.sh lint` and `format`).
- **Accessibility and design:** high contrast, square flat design, skip link, live announcement of results, no external resources, `noindex`.

Known limits: student code and testbench run in the same Octave process, so a deliberate attack on the check is hindered but not impossible (see `DESIGN.md`, section 8); only a clean install on Debian 13 is tested, and the full `install.sh` run on a real server is still pending.
