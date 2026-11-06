# TODO

Open points and ideas. Details of a single institution (rollout, servers, legal contacts) belong into the untracked `LOCAL.md`, not here.

## Features

- Credit rule "best submission" as an alternative to "last submission" (today the last one counts).
- Plagiarism detection.
- Search and paging in long lists of submissions.
- Appoint admins in the web interface (today `auth.admins` in the configuration).
- Test submissions of admins are submissions like all others and show up in the list of submissions and possibly in the credit list: mark or hide them.

## Sandbox and checks

- Seccomp filter for the sandbox (there is none today).
- Separate processes for student code and verdict before a second language is added: the student code only delivers data, a second process judges. For Octave the protection by helper functions and flagging is enough today; Python code can read and change the modules, frames and variables of a testbench easily.
- Python as a second language with the same `check` and `check_close`: interpreter from `/usr`, stricter sandbox, distribution packages only (`numpy` would be one more package). Stay with two languages; further ones only as a documented extension point (C, C++ and Java need a compiler in the sandbox, R and Julia have large runtimes).

## To verify

- A full `install.sh` run with a `mod_md` certificate on a real server (https mode). Tested so far: a Raspberry Pi in plain-HTTP mode and behind a TLS reverse proxy.
- Ubuntu: only Debian 13 is tested; on Ubuntu 24.04 AppArmor can restrict the unprivileged user namespaces that `bubblewrap` needs.
- Labs with plots on a small machine (Raspberry Pi).
- Logging in again with the "stay logged in" cookie in a real browser.
- A legal review of the privacy statement (German and English) by the operating institution.

## Ideas

- Measure what "light" means: installation time, memory use, lines of code.
