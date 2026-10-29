#!/bin/sh
# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

# Checks that the bubblewrap sandbox for Octave works and has the expected
# properties (DESIGN.md section 8). Runs in the dev/ container or on the Debian VM.
# The bwrap command line in sandbox() is the template for the grader.
set -u

OCTAVE=octave-cli
fail=0
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

ok()   { echo "OK    $1"; }
nok()  { echo "ERROR $1"; fail=1; }

# sandbox <work directory> <seconds> <octave arguments...>
sandbox() {
  dir=$1; secs=$2; shift 2
  prlimit --cpu=60 --as=3221225472 --fsize=10485760 --nproc=128 --core=0 \
    timeout -s KILL "$secs" \
    bwrap --unshare-all --die-with-parent --new-session --clearenv \
      --ro-bind /usr /usr \
      --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/sbin /sbin \
      --ro-bind-try /etc/alternatives /etc/alternatives \
      --ro-bind-try /etc/ld.so.cache /etc/ld.so.cache \
      --ro-bind-try /etc/ld.so.conf.d /etc/ld.so.conf.d \
      --ro-bind-try /etc/fonts /etc/fonts \
      --proc /proc --dev /dev --tmpfs /tmp \
      --setenv HOME /tmp --setenv PATH /usr/bin \
      --bind "$dir" /work --chdir /work \
      "$OCTAVE" --norc --silent "$@"
}

command -v bwrap >/dev/null || { echo "bwrap is missing"; exit 2; }
command -v "$OCTAVE" >/dev/null || { echo "$OCTAVE is missing"; exit 2; }
echo "bwrap: $(bwrap --version)   octave: $($OCTAVE --version | head -1)"

# 1. Testbench runs and produces output
cat > "$work/testbench.m" <<'EOF'
function [] = testbench()
  disp('[OK] example');
  disp('[Verification ready]');
end
EOF
out=$(sandbox "$work" 30 --eval testbench 2>&1); rc=$?
if [ $rc -eq 0 ] && echo "$out" | tail -1 | grep -q '^\[Verification ready\]$'; then
  ok "Testbench runs (exit 0, marker as last line)"
else
  nok "Testbench run (rc=$rc): $out"
  echo "Sandbox unusable, further checks skipped."
  exit 1
fi

# 2. No network: no route, and a real connection attempt fails.
# (Inactive fallback devices like tunl0/gre0 are created by the kernel in every new network namespace.)
cat > "$work/net.m" <<'EOF'
[s, out] = system('wc -l < /proc/net/route');
printf('routelines=%s\n', strtrim(out));
s = system('bash -c "exec 3<>/dev/tcp/1.1.1.1/53" 2>/dev/null');
printf('connect=%d\n', s);
EOF
out=$(sandbox "$work" 30 --eval net 2>&1 | tr '\n' ' ')
[ "$out" = "routelines=1 connect=1 " ] && ok "No network (no route, connection fails)" || nok "Network test: '$out'"

# 3. Host file system not visible (repo under /app, the user's home)
cat > "$work/fs.m" <<'EOF'
disp(exist('/app', 'dir'));
disp(exist('/home', 'dir'));
disp(exist('/var/lib', 'dir'));
EOF
out=$(sandbox "$work" 30 --eval fs 2>&1 | tr '\n' ' ')
[ "$out" = "0 0 0 " ] && ok "/app, /home, /var/lib not visible" || nok "Host directories visible: $out"

# 4. Writing only in the work directory
cat > "$work/wr.m" <<'EOF'
fid = fopen('/usr/evil.txt', 'w'); disp(fid);
fid = fopen('/work/ok.txt', 'w'); disp(fid > 0);
EOF
out=$(sandbox "$work" 30 --eval wr 2>&1 | tr '\n' ' ')
[ "$out" = "-1 1 " ] && [ -f "$work/ok.txt" ] && ok "Writing only possible in /work" || nok "Write permissions: $out"

# 5. Infinite loop is aborted
cat > "$work/loop.m" <<'EOF'
fclose(fopen('/work/started', 'w'));
while true, end
EOF
start=$(date +%s)
sandbox "$work" 3 --eval loop >/dev/null 2>&1; rc=$?
elapsed=$(( $(date +%s) - start ))
# 137 = SIGKILL by timeout; the marker file proves that Octave really ran
[ $rc -eq 137 ] && [ -f "$work/started" ] && [ $elapsed -le 6 ] \
  && ok "Infinite loop ended after ${elapsed}s (exit 137)" || nok "Timeout (rc=$rc, ${elapsed}s)"

# 6. Memory limit applies (8 GB request with a 3 GB address space limit)
cat > "$work/mem.m" <<'EOF'
x = zeros(1e9, 1); disp(numel(x));
EOF
sandbox "$work" 30 --eval mem >"$work/mem.out" 2>&1; rc=$?
[ $rc -ne 0 ] && grep -qi 'out of memory' "$work/mem.out" \
  && ok "Memory limit applies (Octave: out of memory)" || nok "Memory limit (rc=$rc): $(head -c 200 "$work/mem.out")"

# 7. Process namespace: host processes not visible
cat > "$work/ps.m" <<'EOF'
[s, out] = system('ls /proc | grep -c "^[0-9]"');
disp(strtrim(out));
EOF
n=$(sandbox "$work" 30 --eval ps 2>&1 | tail -1)
case "$n" in
  ''|*[!0-9]*) nok "PID test: unexpected output '$n'" ;;
  *) [ "$n" -lt 20 ] && ok "Own PID namespace ($n processes visible)" || nok "Too many processes visible: $n" ;;
esac

# 8. Plots without a display (gnuplot toolkit, font): figure/plot must not fail
cat > "$work/plottest.m" <<'EOF'
warning('off', 'Octave:gnuplot-graphics');
figure; plot(1:3); hold on; plot(3:-1:1); title('t'); xlabel('x');
disp('plot ok');
EOF
out=$(sandbox "$work" 30 plottest.m 2>&1 | tr '\n' ' ')
[ "$out" = "plot ok " ] && ok "Plots without a display work" || nok "Plot test: '$(echo "$out" | head -c 200)'"

[ $fail -eq 0 ] && echo "All checks passed." || echo "There were errors."
exit $fail
