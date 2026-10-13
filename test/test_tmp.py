"""What the wrapper leaves behind in $TMPDIR: nothing, however it is stopped.

It used to leave a copy of its own supervisor, 300 KB of python, every time a
statusline refresh was cut short — and Claude Code cuts one short whenever the
next is due before the last has answered. `--cr-statusline` wrote the program
to a temp file, ran python on it and removed it afterwards; the abort is a
SIGTERM to the whole process group, the shell died where it stood, and
"afterwards" never came. One machine had 1600 of them after nine days.

So these tests stop the wrapper the way it is stopped in the field — as the
leader of its own group, by a signal sent to that group, at a moment when it
is known to be in the middle of things — and then look at the directory. Each
has a TMPDIR of its own: the machine's is shared with every live session on
it, and counting there is counting them.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helper import eventually, hermetic_env, load

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(ROOT, "agent-retrier.sh")
FAKE_SRC = os.path.join(ROOT, "test", "fake_claude.py")

cr = load()

DAY = 24 * 3600


def script(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)
    return path


class TmpTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-tmp-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.tmp = os.path.join(self.dir, "tmp")
        self.config = os.path.join(self.dir, "claude-config")
        os.mkdir(self.tmp)
        os.mkdir(self.config)
        self.marker = os.path.join(self.dir, "in-flight")
        self.status = os.path.join(self.dir, "status", "1.json")
        self.log = os.path.join(self.dir, "log")
        self.started = []
        self.addCleanup(self.reap)

    def env(self, via, **over):
        # `load()` above has filled this process's environment with the CR_*
        # of a unit test; a wrapper started from here begins without any.
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("CR_")
             and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}
        e.update(hermetic_env(os.path.join(self.dir, "state")))
        e.update({"TMPDIR": self.tmp, "CLAUDE_CONFIG_DIR": self.config,
                  "CR_PY_VIA": via, "CR_LOG": self.log, "CR_NOTIFY": "0"})
        e.update(over)
        return e

    def start(self, args, env, stdin=""):
        """The wrapper as Claude Code starts a statusline command: the leader
        of a session, and so of a process group, of its own."""
        proc = subprocess.Popen([WRAP, *args], env=env, cwd=self.dir,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        self.started.append(proc)
        proc.stdin.write(stdin.encode())
        proc.stdin.close()
        proc.stdin = None        # ...and `communicate` is not to flush it again
        return proc

    def stop(self, proc, sig):
        """...and as Claude Code stops one: the whole group, at once.

        Returns when the process we started is gone. Where a shell's trap is
        what removes the file, that shell IS the process we started, so there
        is no "a moment later" to wait for: what is in $TMPDIR now is what
        was left."""
        os.killpg(proc.pid, sig)
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()

    def reap(self):
        for proc in self.started:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for pipe in (proc.stdout, proc.stderr):
                if pipe and not pipe.closed:
                    pipe.close()

    def in_flight(self):
        return eventually(lambda: os.path.exists(self.marker), timeout=15)

    def left(self):
        return sorted(os.listdir(self.tmp))

    def users_statusline(self, command):
        """The user's own `statusLine`, which the proxy runs after its own
        work is done — so a command that hangs is a refresh held in flight."""
        with open(os.path.join(self.config, "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": command}}, fh)

    def a_refresh(self):
        return json.dumps({"session_id": "s", "workspace": {"current_dir": self.dir}})

    def hang(self):
        return ": > '%s'; exec sleep 30" % self.marker

    def an_interpreter_that_never_gets_going(self):
        """A python that answers `cr_find_python`'s two questions and then,
        handed the supervisor, says it has been started and does nothing —
        not even the unlink that is the supervisor's own first act. What is in
        $TMPDIR while this one hangs is the shell's to remove, or nobody's.
        """
        return script(os.path.join(self.dir, "python3"),
                      "#!/bin/sh\n"
                      'case "$1" in\n'
                      "  -c) case \"$2\" in *sys.executable*) printf '%%s' \"$0\" ;; esac\n"
                      "      exit 0 ;;\n"
                      "esac\n"
                      ": > '%s'\n"
                      "exec sleep 30\n" % self.marker)


class TestAStatuslineRefresh(TmpTestCase):
    """`agent-retrier.sh --cr-statusline <path>`, the command line Claude Code
    is given, through the shell and not only the python behind it."""

    def test_one_that_runs_its_course_still_does_its_job(self):
        self.users_statusline("echo theirs; exit 3")
        for via in ("fd", "tmp"):
            with self.subTest(via=via):
                proc = self.start(["--cr-statusline", self.status], self.env(via),
                                  stdin=self.a_refresh())
                out, err = proc.communicate(timeout=30)
                self.assertEqual(out.decode().strip(), "theirs", err)
                self.assertEqual(proc.returncode, 3)       # theirs too
                with open(self.status) as fh:
                    self.assertEqual(json.load(fh)["session_id"], "s")
                self.assertEqual(self.left(), [])
                os.unlink(self.status)

    def test_one_that_claude_code_cuts_short_leaves_nothing(self):
        # The leak itself. In flight means python is up and waiting on the
        # user's statusline: the widest part of the window, and the part most
        # of the 1600 were made in — whole copies, not half-written ones.
        self.users_statusline(self.hang())
        proc = self.start(["--cr-statusline", self.status], self.env("auto"),
                          stdin=self.a_refresh())
        self.assertTrue(self.in_flight())
        self.stop(proc, signal.SIGTERM)
        self.assertEqual(self.left(), [])

    def test_nor_does_one_that_is_killed_outright(self):
        # What follows the SIGTERM a second and a half later, for whatever is
        # still there. No trap runs for this one, which is why the answer is
        # not a trap: over a descriptor there is no file to begin with.
        self.users_statusline(self.hang())
        proc = self.start(["--cr-statusline", self.status], self.env("auto"),
                          stdin=self.a_refresh())
        self.assertTrue(self.in_flight())
        self.assertEqual(self.left(), [])
        self.stop(proc, signal.SIGKILL)
        self.assertEqual(self.left(), [])

    def test_the_temp_file_is_gone_before_the_proxy_does_anything(self):
        # No /dev/fd, so there is a file — and it is python's first act to
        # unlink it, in this mode as in a session. By the time the proxy is
        # running there is nothing for a SIGKILL to strand.
        self.users_statusline(self.hang())
        proc = self.start(["--cr-statusline", self.status], self.env("tmp"),
                          stdin=self.a_refresh())
        self.assertTrue(self.in_flight())
        self.assertEqual(self.left(), [])
        self.stop(proc, signal.SIGKILL)
        self.assertEqual(self.left(), [])


class TestWhatPythonNeverGotTo(TmpTestCase):
    """Stopped before python has run a line of the supervisor: nothing in
    python can have removed anything, so what is left is the shell's doing.
    Over a descriptor that is nothing at all; with a temp file it is a trap.
    """

    def stopped_early(self, args):
        python = self.an_interpreter_that_never_gets_going()
        for via in ("fd", "tmp"):
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                with self.subTest(via=via, sig=sig.name):
                    # Each on its own: what one leaves is not the next one's.
                    self.start_over()
                    proc = self.start(args, self.env(via, CR_PYTHON=python),
                                      stdin=self.a_refresh())
                    self.assertTrue(self.in_flight())
                    if via == "tmp":
                        self.assertEqual(len(self.left()), 1)   # ...so there is one
                    self.stop(proc, sig)
                    self.assertEqual(self.left(), [])
                    if via == "tmp":
                        # The shell outlived its python to do that, and says
                        # what a shell says of a death by signal.
                        self.assertEqual(proc.returncode, 128 + sig)

    def start_over(self):
        for name in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, name))
        if os.path.exists(self.marker):
            os.unlink(self.marker)

    def test_a_statusline_refresh(self):
        self.stopped_early(["--cr-statusline", self.status])

    def test_the_models_table(self):
        # ^C at `agent-retrier --cr-models` while it is asking the network:
        # the same shape, with a person in place of Claude Code.
        self.stopped_early(["--cr-models"])

    def test_the_models_table_still_prints(self):
        for via in ("fd", "tmp"):
            with self.subTest(via=via):
                proc = self.start(["--cr-models"], self.env(via))
                out, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, err)
                self.assertIn(b"restart_at", out)
                self.assertEqual(self.left(), [])


class TestAProbeOfTheUsersShell(TmpTestCase):
    """`cr_probe` asks an interactive shell what an alias is, and collects the
    answer in a file. A shell that hangs on a question from its rc file is the
    one that gets a ^C."""

    def test_stopped_while_the_shell_is_thinking_it_leaves_nothing(self):
        shell = script(os.path.join(self.dir, "zsh"),
                       "#!/bin/sh\n: > '%s'\nexec sleep 30\n" % self.marker)
        env = self.env("auto", CR_SHELL=shell, CR_CLAUDE_BIN="",
                       CR_PROBE_TIMEOUT_SEC="20")
        proc = self.start(["--cmd", "claude-work", "-p", "hi"], env)
        self.assertTrue(self.in_flight())
        # Not "removed in time": it has had no name since before the shell
        # was started, so there is no moment at which it could be left.
        self.assertEqual(self.left(), [])
        self.stop(proc, signal.SIGKILL)
        self.assertEqual(self.left(), [])


class TestWhatOlderVersionsLeft(TmpTestCase):
    """The sweep: a session removes the stale copies 3.0.3 and earlier left
    in $TMPDIR, and nothing else that happens to be lying there."""

    HEAD = b'"""agent-retrier PTY supervisor.'
    COPY = HEAD + b'\n\nRuns claude inside a pty...\n"""\nimport os\n'

    def put(self, name, content=None, age=2 * DAY, where=None):
        path = os.path.join(where or self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(self.COPY if content is None else content)
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def test_what_the_wrapper_writes_begins_the_way_the_sweep_expects(self):
        # The sweep knows its own by their first line. Reword the docstring at
        # the top of the supervisor and this is where it is noticed.
        src = subprocess.run([WRAP, "--cr-dump-python"], env=self.env("auto"),
                             capture_output=True, check=True).stdout
        self.assertTrue(src.startswith(self.HEAD + b"\n"))
        self.assertEqual(cr._OWN_TMP_HEAD, self.HEAD)

    def test_stale_copies_go_whole_cut_short_or_empty(self):
        whole = self.put("agent-retrier.aB3dE9")
        cut = self.put("agent-retrier.Zz0000", self.COPY[:40])
        empty = self.put("agent-retrier.EMPTY1", b"")
        count, freed = cr.sweep_stale_tmp(self.tmp)
        self.assertEqual((count, freed), (3, len(self.COPY) + 40))
        for path in (whole, cut, empty):
            self.assertFalse(os.path.exists(path), path)

    def test_a_copy_younger_than_a_day_is_left_alone(self):
        # Far more than the moment it takes python to read one — which is the
        # only time one is in use, by this version or a neighbour's older one.
        fresh = self.put("agent-retrier.fresh1", age=DAY - 600)
        self.assertEqual(cr.sweep_stale_tmp(self.tmp), (0, 0))
        self.assertTrue(os.path.exists(fresh))
        self.assertEqual(cr.sweep_stale_tmp(self.tmp, now=time.time() + 1200)[0], 1)

    def test_nothing_that_is_not_provably_ours_is_touched(self):
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.mkdir(elsewhere)
        target = self.put("somebody-elses.py", where=elsewhere)
        os.symlink(target, os.path.join(self.tmp, "agent-retrier.LiNk01"))
        os.mkdir(os.path.join(self.tmp, "agent-retrier.aDir01"))
        self.put("agent-retrier.notes1", b"my notes on agent-retrier\n")
        self.put("agent-retrier.log")
        self.put("agent-retrier.toolong")
        self.put("agent-retrier-test.log")
        self.put("tmp.aB3dE9")
        before = self.left()
        self.assertEqual(cr.sweep_stale_tmp(self.tmp), (0, 0))
        self.assertEqual(self.left(), before)
        self.assertTrue(os.path.exists(target))

    def test_nor_another_users_copy(self):
        theirs = self.put("agent-retrier.aB3dE9")
        with mock.patch.object(cr.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(cr.sweep_stale_tmp(self.tmp), (0, 0))
        self.assertTrue(os.path.exists(theirs))

    def test_a_tmpdir_that_is_not_there_is_not_a_crash(self):
        self.assertEqual(cr.sweep_stale_tmp(os.path.join(self.dir, "nope")), (0, 0))

    def session(self, sweep):
        """One whole session over a $TMPDIR that is ours: started, told to
        quit, gone. `sweep` is CR_TMP_SWEEP, or None for whatever ships."""
        claude = script(os.path.join(self.dir, "claude"),
                        '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_SRC))
        env = self.env("auto", CR_CLAUDE_BIN=claude)
        del env["CR_TMP_SWEEP"]
        if sweep is not None:
            env["CR_TMP_SWEEP"] = sweep
        return subprocess.run([WRAP], env=env, cwd=self.dir, input="quit\n",
                              capture_output=True, text=True, timeout=25)

    def test_a_session_sweeps_at_its_start_and_says_so(self):
        stale = self.put("agent-retrier.aB3dE9")
        other = self.put("agent-retrier.Zz0000")
        fresh = self.put("agent-retrier.fresh1", age=60)
        r = self.session(sweep=None)
        self.assertIn("fake-claude ready", r.stdout, r.stderr)
        self.assertEqual(self.left(), [os.path.basename(fresh)])
        self.assertFalse(os.path.exists(stale) or os.path.exists(other))
        with open(self.log) as fh:
            self.assertIn("removed 2 stale copies of this program", fh.read())

    def test_the_off_switch(self):
        stale = self.put("agent-retrier.aB3dE9")
        r = self.session(sweep="0")
        self.assertIn("fake-claude ready", r.stdout, r.stderr)
        self.assertEqual(self.left(), [os.path.basename(stale)])
        with open(self.log) as fh:
            self.assertNotIn("stale", fh.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
