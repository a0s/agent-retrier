"""The shell layer, and every path where the wrapper must get out of the way.

Upstream's sharpest failure was not a missed retry: it was issue #65, where an
uninstalled wrapper left `claude` unable to start at all. A wrapper is only
acceptable if every failure mode degrades to plain claude.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(ROOT, "agent-retrier.sh")
FAKE_SRC = os.path.join(ROOT, "test", "fake_claude.py")


def launcher(dirpath, body=None):
    path = os.path.join(dirpath, "claude")
    with open(path, "w") as fh:
        fh.write(body or '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE_SRC))
    os.chmod(path, 0o755)
    return path


def clean_env():
    """os.environ minus anything a wrapped session would have exported.

    Running the suite from inside a agent-retrier session leaves
    AGENT_RETRIER_ACTIVE=1 in the environment, which is a legitimate instruction
    to the wrapper — degrade, do not stack a second supervisor. Every wrapper
    started here would obey it, and the tests would be measuring the caller's
    session instead of the code.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CR_")
            and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}


def run(args=(), env=None, stdin=subprocess.DEVNULL, timeout=30):
    full = clean_env()
    full.setdefault("CR_NOTIFY", "0")
    full.update(env or {})
    return subprocess.run([WRAP, *args], env=full, stdin=stdin,
                          capture_output=True, text=True, timeout=timeout)


class TestCli(unittest.TestCase):
    def test_version(self):
        self.assertIn("agent-retrier", run(["--cr-version"]).stdout)

    def test_dump_python_is_valid_python(self):
        src = run(["--cr-dump-python"]).stdout
        compile(src, "embedded", "exec")

    def test_dump_patterns_lists_every_array(self):
        out = run(["--cr-dump-patterns"]).stdout
        for name in ("LIMIT", "RESET", "WORKING", "MENU", "IGNORE", "STALL"):
            self.assertIn("### CR_PAT_%s" % name, out)
        self.assertIn("hit your", out)

    def test_cr_models_prints_the_profile_table(self):
        # T18. Needs no claude/codex installed at all — it never resolves a
        # command, so the fake-claude launcher this file's tests otherwise
        # rely on is beside the point here.
        out = run(["--cr-models"]).stdout
        self.assertIn("claude-opus-5", out)
        self.assertIn("gpt-5.6-sol", out)
        self.assertIn("restart_at", out)   # the header, so a truncated table fails loudly

    def test_shell_syntax_is_valid_under_bash(self):
        self.assertEqual(subprocess.run(["bash", "-n", WRAP]).returncode, 0)

    def test_the_version_matches_the_changelog(self):
        # The release workflow refuses a tag that disagrees with either of these,
        # so a mismatch is a release that cannot be cut — better found here.
        import re
        version = run(["--cr-version"]).stdout.split()[-1]
        with open(os.path.join(ROOT, "CHANGELOG.md")) as fh:
            newest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", fh.read(), re.M).group(1)
        self.assertEqual(version, newest)


class TestTheRemovedPercentSettingsRefuseToStart(unittest.TestCase):
    """2.0: CR_CONTEXT_PCT/CR_CODEX_CONTEXT_PCT are gone. A session that still
    has one set in its environment gets told so and refused, rather than
    silently running with no restart armed at all."""

    def test_claude_pct_is_refused(self):
        r = run(["--cr-version"], env={"CR_CONTEXT_PCT": "51"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("was removed in 2.0", r.stderr)
        self.assertIn("CR_CONTEXT_RESTART", r.stderr)

    def test_codex_pct_is_refused(self):
        r = run(["--cr-version"], env={"CR_CODEX_CONTEXT_PCT": "60"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("was removed in 2.0", r.stderr)
        self.assertIn("CR_CONTEXT_RESTART", r.stderr)

    def test_an_empty_value_is_not_considered_set(self):
        r = run(["--cr-version"], env={"CR_CONTEXT_PCT": ""})
        self.assertEqual(r.returncode, 0)
        self.assertIn("agent-retrier", r.stdout)


class TestDegradation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-deg-")
        self.claude = launcher(self.dir)
        self.log = os.path.join(self.dir, "log")

    def env(self, **over):
        # No update check: the tests below that do start a supervisor would
        # otherwise be reading the developer's own ~/.agent-retrier/update.json
        # — and, on the first run of the day, asking api.github.com.
        e = {"CR_CLAUDE_BIN": self.claude, "CR_LOG": self.log, "CR_UPDATE_CHECK": "0"}
        e.update(over)
        return e

    def supervised(self):
        """Did the pty supervisor actually run? It is the only thing that logs."""
        return os.path.exists(self.log) and "start:" in open(self.log).read()

    def test_print_mode_runs_claude_directly(self):
        # `claude -p` is a batch run with no TUI to type into; supervising it
        # would add a pty for nothing and break piping.
        r = run(["-p", "hello"], env=self.env(), timeout=20)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_long_form_print_flag_too(self):
        run(["--print"], env=self.env(), timeout=20)
        self.assertFalse(self.supervised())

    def test_disable_flag_runs_claude_directly(self):
        r = run(env=self.env(CR_DISABLE="1"), timeout=20)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_recursion_is_refused(self):
        # If a wrapped session somehow invokes `claude` again, the inner call
        # must not stack a second pty supervisor on top of the first.
        r = run(env=self.env(AGENT_RETRIER_ACTIVE="1"), timeout=20)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_missing_claude_reports_clearly(self):
        empty = tempfile.mkdtemp(prefix="cr-empty-")
        r = run(env={"PATH": "%s:/usr/bin:/bin" % empty, "HOME": empty,
                     "CR_CLAUDE_BIN": "", "CR_CLAUDE_FALLBACKS": empty})
        self.assertEqual(r.returncode, 127)
        self.assertIn("not found", r.stderr)

    def test_an_unrunnable_override_is_reported_not_ignored(self):
        r = run(env=self.env(CR_CLAUDE_BIN=os.path.join(self.dir, "nope")))
        self.assertEqual(r.returncode, 127)

    def test_unusable_python_still_runs_claude(self):
        # No interpreter with the modules we need => the session must still start,
        # unsupervised. This is the single most important degradation path: a
        # missing dependency of the WRAPPER must never cost the user their claude.
        r = run(env=self.env(CR_PYTHON="", CR_PYTHON_CANDIDATES="/nonexistent/python"),
                timeout=20)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_a_claude_that_fails_still_reports_its_status(self):
        broken = launcher(self.dir, '#!/bin/sh\necho "boom" >&2\nexit 3\n')
        r = run(env=self.env(CR_CLAUDE_BIN=broken), timeout=20)
        self.assertEqual(r.returncode, 3)
        self.assertIn("boom", r.stdout + r.stderr)

    def test_it_works_without_a_tty_at_all(self):
        # Piped stdin/stdout: the supervisor still runs, just with no raw mode.
        r = subprocess.run([WRAP], env={**clean_env(), **self.env(), "CR_NOTIFY": "0"},
                           input="quit\n", capture_output=True, text=True, timeout=25)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertTrue(self.supervised())

    def wrapped(self, args):
        """Launch with args, feed `quit` so a real session ends on its own,
        and report whether the pty supervisor actually ran."""
        r = subprocess.run([WRAP, *args], env={**clean_env(), **self.env(), "CR_NOTIFY": "0"},
                           input="quit\n", capture_output=True, text=True, timeout=25)
        return r, self.supervised()

    def test_stop_subcommand_runs_directly(self):
        # T07: `claude stop <id>` is a one-shot control command, not a session
        # -- no TUI to type into, and the pty supervisor would sleep for
        # CR_UPDATE_NOTICE_SEC and write start:/exit: to the log for nothing.
        r = run(["stop", "abc"], env=self.env(), timeout=20)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_mcp_subcommand_runs_directly(self):
        r = run(["mcp", "list"], env=self.env(), timeout=20)
        self.assertIn("fake-claude ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_attach_is_still_wrapped(self):
        # `attach` opens a real session, unlike the control commands above --
        # it must stay wrapped.
        r, started = self.wrapped(["attach", "abc"])
        self.assertIn("fake-claude ready", r.stdout)
        self.assertTrue(started)

    def test_agents_roster_is_still_wrapped(self):
        # `agents` is the TUI roster of OTHER sessions, handled inside the
        # supervisor itself (T29) -- it must stay wrapped, not exec'd directly.
        r, started = self.wrapped(["agents"])
        self.assertIn("fake-claude ready", r.stdout)
        self.assertTrue(started)

    def test_a_prompt_that_names_a_subcommand_is_still_wrapped(self):
        # The first positional word decides, and it must match exactly: a
        # plain prompt is one argument, so it never equals "stop" outright
        # even though it contains the word.
        r, started = self.wrapped(["fix the stop command"])
        self.assertIn("fake-claude ready", r.stdout)
        self.assertTrue(started)


class TestHowTheSupervisorIsHandedOver(unittest.TestCase):
    """The embedded Python is ~140KB, and Linux caps a single argument at 128KB.

    `python3 -c "$CR_PY"` therefore works on macOS and fails on Linux with
    "Argument list too long" — which degrades every wrapped session to nothing at
    all. Both routes that replace it are exercised here, on whichever platform
    the suite is running.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-via-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.claude = launcher(self.dir)
        self.log = os.path.join(self.dir, "log")
        self.tmp = os.path.join(self.dir, "tmp")
        os.mkdir(self.tmp)

    def supervised(self, via):
        # "quit" rather than a closed stdin: the supervised claude sits on a pty
        # of its own, which does not end just because ours did.
        #
        # A TMPDIR of its own, because the last test here counts what the
        # wrapper leaves in it. Counted in the machine's, that was a count of
        # every wrapper on the machine: the statusline proxy drops (and
        # removes) a file of the same name on every refresh of every live
        # session, the other tests in this file do it too, and `./test/run.sh`
        # now runs them at the same moment.
        env = {**clean_env(), "CR_CLAUDE_BIN": self.claude, "CR_LOG": self.log,
               "CR_PY_VIA": via, "CR_UPDATE_CHECK": "0", "CR_NOTIFY": "0",
               "TMPDIR": self.tmp}
        r = subprocess.run([WRAP], env=env, input="quit\n",
                           capture_output=True, text=True, timeout=25)
        started = os.path.exists(self.log) and "start:" in open(self.log).read()
        return r, started

    def test_a_descriptor_carries_it(self):
        r, started = self.supervised("fd")
        self.assertIn("fake-claude ready", r.stdout)
        self.assertTrue(started, r.stderr)

    def test_and_so_does_a_temp_file(self):
        r, started = self.supervised("tmp")
        self.assertIn("fake-claude ready", r.stdout)
        self.assertTrue(started, r.stderr)

    def test_the_temp_file_does_not_outlive_the_exec(self):
        r, started = self.supervised("tmp")
        self.assertTrue(started, r.stderr)      # ...so there was a file to outlive it
        self.assertEqual(glob.glob(os.path.join(self.tmp, "agent-retrier.*")), [])


class TestPatternRobustness(unittest.TestCase):
    def test_a_broken_user_pattern_is_skipped_not_fatal(self):
        from helper import load
        mod = load(CR_PAT_LIMIT="you've hit your session limit\n[unclosed(")
        self.assertEqual(len(mod.PAT["limit"]), 1)
        self.assertTrue(mod.is_limit_line("You've hit your session limit"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
