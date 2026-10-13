#!/usr/bin/env python3
"""Scans for live processes carrying CR_CLAUDE_ARGV in their environment —
proof a wrapper's supervisor (or the agent under it: env is inherited by every
descendant unless a test explicitly unsets it) is still running after a suite
that should have reaped it. Left behind, it keeps writing into the shared
CR_LOG and confuses the next run (memory: live-codex-test-orphans).

Linux only: `/proc/<pid>/environ` is the one place a process's environment is
reliably readable from outside it for the same user. There is no portable
equivalent on macOS, so elsewhere this is a documented no-op rather than a
false pass — the accurate check runs where CI actually runs: `./test/run.sh
--docker` (test/run-docker.sh), a Linux container.

    check-orphans.py [SECONDS]

SECONDS is how long something found alive is given to finish dying before it
is called an orphan. A fake agent is not killed by the test that started it:
it dies of the hangup its supervisor's death sends it, a moment AFTER the test
is over. run.sh used to run two more test files between the last pty test and
this check, which was all the moment it ever needed; now the check comes
straight after, so it asks for the moment by name. A real orphan is still
there when it is up.
"""
import os
import sys
import time


def find_orphans():
    if not os.path.isdir("/proc"):
        return None  # not checkable on this platform
    orphans = []
    self_pid = os.getpid()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == self_pid:
            continue
        try:
            with open("/proc/%d/environ" % pid, "rb") as fh:
                data = fh.read()
        except (OSError, PermissionError):
            continue
        if b"CR_CLAUDE_ARGV=" in data:
            orphans.append(pid)
    return orphans


def main(settle=0.0):
    orphans = find_orphans()
    deadline = time.time() + settle
    while orphans and time.time() < deadline:
        time.sleep(0.1)
        orphans = find_orphans()
    if orphans is None:
        print("orphan check: skipped (no /proc on this platform)")
        return 0
    if orphans:
        print("FAILURES: orphaned wrapper process(es) still running: %s" % orphans)
        return 1
    print("orphan check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.0))
