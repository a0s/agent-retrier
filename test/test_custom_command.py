"""Whatever the user types to start Claude, the wrapper has to accept it.

Nobody runs stock `claude` for long: people end up with `claude-work` and
`claude-personal`, sometimes a binary, more often an alias or a shell function in
~/.zshrc that pins a model or a settings file. The wrapper cannot know which, so
it has to handle all of them — and an alias in particular exists nowhere except
inside an interactive shell that has read the rc file.

Every shape is exercised twice where it matters: once through the fast degrade
path (`-p`, no supervisor), and once through the pty supervisor, because the two
reach exec by different routes.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_pty import PtyTestCase, FAKE          # noqa: E402  (pty harness, reused)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(ROOT, "agent-retrier.sh")


def has(shell):
    return shutil.which(shell) is not None


def modern_bash():
    """bash 4+, i.e. one that can be asked for an alias body via BASH_ALIASES.

    macOS still ships 3.2 as /bin/bash, where the wrapper falls back to running
    the alias through an interactive shell — correct, but a different shape.
    """
    if not has("bash"):
        return False
    out = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"],
                         capture_output=True, text=True).stdout.strip()
    return out.isdigit() and int(out) >= 4


class Rig:
    """A HOME with rc files defining an alias and a function for both shells."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="cr-cmd-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        # The stand-in claude, reachable by path and by name on PATH.
        self.claude = os.path.join(self.bin, "my-claude")
        with open(self.claude, "w") as fh:
            fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n'
                     % (sys.executable, os.path.join(ROOT, "test", "fake_claude.py")))
        os.chmod(self.claude, 0o755)

        rc = ("alias claude-work='\"%s\" --model opus'\n"
              "claude-personal() { \"%s\" --fn \"$@\"; }\n" % (self.claude, self.claude))
        self.zdotdir = os.path.join(self.dir, "zdot")
        os.makedirs(self.zdotdir)
        for path in (os.path.join(self.dir, ".bashrc"),
                     os.path.join(self.zdotdir, ".zshrc")):
            with open(path, "w") as fh:
                fh.write(rc)

    def env(self, shell="bash", **over):
        e = {
            "HOME": self.dir,                   # bash -i reads $HOME/.bashrc
            "ZDOTDIR": self.zdotdir,            # zsh -i reads $ZDOTDIR/.zshrc
            "CR_SHELL": shell,
            "CR_CLAUDE_BIN": "",
            "CR_NOTIFY": "0",
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
        }
        e.update(over)
        return e

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run(args, env, timeout=30):
    # CR_* and AGENT_RETRIER_ACTIVE from a wrapped caller would configure the
    # very wrapper under test; start from a clean environment.
    full = {k: v for k, v in os.environ.items()
            if not k.startswith("CR_") and k != "AGENT_RETRIER_ACTIVE"}
    full["CR_TMP_SWEEP"] = "0"       # the developer's $TMPDIR is not ours to tidy
    full.update(env)
    return subprocess.run([WRAP, *args], env=full, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


class CommandShapes(unittest.TestCase):
    """`-p` degrades to a direct exec, which is the cheapest way to see argv."""

    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)

    def argv_of(self, out):
        """What the stand-in claude saw, as one string."""
        for line in out.splitlines():
            if line.startswith("fake-claude ready argv="):
                return line.split("argv=", 1)[1].strip()
        self.fail("claude never started; output was:\n%s" % out)

    # --- the shapes ---------------------------------------------------------

    def test_an_absolute_path(self):
        r = run(["--cmd", self.rig.claude, "-p", "hi"], self.rig.env())
        self.assertEqual(self.argv_of(r.stdout), "-p hi")

    def test_a_relative_path(self):
        rel = os.path.relpath(self.rig.claude, os.getcwd())
        r = run(["--cmd", rel, "-p", "hi"], self.rig.env())
        self.assertEqual(self.argv_of(r.stdout), "-p hi")

    def test_a_name_on_path(self):
        r = run(["--cmd", "my-claude", "-p", "hi"], self.rig.env())
        self.assertEqual(self.argv_of(r.stdout), "-p hi")

    def test_a_command_line_with_its_own_arguments(self):
        r = run(["--cmd", '%s --model sonnet' % self.rig.claude, "-p", "hi"],
                self.rig.env())
        self.assertEqual(self.argv_of(r.stdout), "--model sonnet -p hi")

    @unittest.skipUnless(has("bash"), "no bash")
    def test_a_bash_alias(self):
        r = run(["--cmd", "claude-work", "-p", "hi"], self.rig.env("bash"))
        self.assertEqual(self.argv_of(r.stdout), "--model opus -p hi")

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_a_zsh_alias(self):
        r = run(["--cmd", "claude-work", "-p", "hi"], self.rig.env("zsh"))
        self.assertEqual(self.argv_of(r.stdout), "--model opus -p hi")

    @unittest.skipUnless(has("bash"), "no bash")
    def test_a_bash_function(self):
        r = run(["--cmd", "claude-personal", "-p", "hi"], self.rig.env("bash"))
        self.assertEqual(self.argv_of(r.stdout), "--fn -p hi")

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_a_zsh_function(self):
        r = run(["--cmd", "claude-personal", "-p", "hi"], self.rig.env("zsh"))
        self.assertEqual(self.argv_of(r.stdout), "--fn -p hi")

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_an_alias_resolves_without_an_interactive_shell_on_stderr(self):
        # Resolving the alias to its body keeps `sh -i` out of the final exec, so
        # a scripted `claude -p …` gets a clean stderr rather than job-control
        # chatter from a shell that has no terminal.
        r = run(["--cmd", "claude-work", "-p", "hi"], self.rig.env("zsh"))
        self.assertEqual(r.stderr.strip(), "")

    # --- the shape of the launch vector -------------------------------------

    def argv_vector(self, spec, shell="bash"):
        r = run(["--cmd", spec, "--cr-dump-argv"], self.rig.env(shell))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_a_file_is_exec_ed_directly(self):
        self.assertEqual(self.argv_vector(self.rig.claude), [self.rig.claude])

    @unittest.skipUnless(has("zsh") and modern_bash(), "needs zsh and bash 4+")
    def test_no_interactive_shell_is_left_in_the_launch_vector(self):
        # An interactive shell inside the pty contends for the terminal it was
        # just handed: on a headless machine (CI, container, cron) it can hang
        # there, and the session never starts. Aliases and functions are lifted
        # out of the rc file during resolution precisely so that the thing we
        # exec is a plain `sh -c`.
        for shell in ("bash", "zsh"):
            for spec in ("claude-work", "claude-personal",
                         "%s --model opus" % self.rig.claude):
                vec = self.argv_vector(spec, shell)
                self.assertNotIn("-ic", vec,
                                 "%s via %s still needs an interactive shell" % (spec, shell))

    # --- how it is spelled --------------------------------------------------

    def test_the_equals_form(self):
        r = run(["--cmd=%s" % self.rig.claude, "-p", "hi"], self.rig.env())
        self.assertEqual(self.argv_of(r.stdout), "-p hi")

    def test_the_prefixed_spelling_still_works(self):
        # --cr-cmd is the unambiguous form, kept for the day claude grows a
        # --cmd of its own. Both spellings, both with and without the equals.
        for arg in (["--cr-cmd", self.rig.claude], ["--cr-cmd=%s" % self.rig.claude]):
            r = run([*arg, "-p", "hi"], self.rig.env())
            self.assertEqual(self.argv_of(r.stdout), "-p hi")

    def test_the_environment_variable(self):
        r = run(["-p", "hi"], self.rig.env(CR_CLAUDE_CMD=self.rig.claude))
        self.assertEqual(self.argv_of(r.stdout), "-p hi")

    def test_the_flag_wins_over_the_environment(self):
        env = self.rig.env(CR_CLAUDE_CMD="/nonexistent/claude")
        r = run(["--cmd", self.rig.claude, "-p", "hi"], env)
        self.assertEqual(self.argv_of(r.stdout), "-p hi")

    # --- what must NOT happen ----------------------------------------------

    def test_a_prompt_is_not_mistaken_for_a_command(self):
        # `claude "fix the bug"` is a legitimate invocation: claude takes a bare
        # prompt as its first argument. Guessing the command positionally would
        # eat it, so only the explicit flag may set the command.
        r = run(["-p", "fix the bug"], self.rig.env(CR_CLAUDE_CMD=self.rig.claude))
        self.assertEqual(self.argv_of(r.stdout), "-p fix the bug")

    def test_claude_flags_are_not_consumed(self):
        r = run(["--cmd", self.rig.claude, "-p", "--model", "opus", "x"],
                self.rig.env())
        self.assertEqual(self.argv_of(r.stdout), "-p --model opus x")

    def test_an_unknown_command_fails_loudly(self):
        # Not a silent fallback to some other claude: the user named a command,
        # and running a different one would be worse than not starting.
        r = run(["--cmd", "no-such-claude-anywhere", "-p", "hi"], self.rig.env())
        self.assertEqual(r.returncode, 127)
        self.assertIn("no-such-claude-anywhere", r.stderr)

    def test_a_path_that_is_not_executable_fails_loudly(self):
        dud = os.path.join(self.rig.dir, "not-executable")
        open(dud, "w").close()
        r = run(["--cmd", dud, "-p", "hi"], self.rig.env())
        self.assertEqual(r.returncode, 127)

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_an_rc_file_that_blocks_does_not_take_the_session_with_it(self):
        # zsh's compinit stops on "insecure directories … [y/n]?" and reads the
        # answer from /dev/tty — the terminal the user is sitting at. Waiting on
        # that forever is a session that never starts, which is the one failure
        # this wrapper is not allowed to have.
        with open(os.path.join(self.rig.zdotdir, ".zshrc"), "a") as fh:
            fh.write("sleep 60\n")
        start = time.monotonic()
        r = run(["--cmd", "claude-work", "-p", "hi"],
                self.rig.env("zsh", CR_PROBE_TIMEOUT_SEC="2"), timeout=45)
        elapsed = time.monotonic() - start

        self.assertEqual(r.returncode, 127)
        self.assertIn("did not answer", r.stderr)
        # One probe's worth of waiting, not one per question asked.
        self.assertLess(elapsed, 20, "took %.1fs: %s" % (elapsed, r.stderr))

    def test_the_flag_needs_an_argument(self):
        r = run(["--cmd"], self.rig.env())
        self.assertEqual(r.returncode, 2)

    def test_a_command_pointing_back_at_the_wrapper_is_stopped(self):
        # The nightmare case: CR_CLAUDE_CMD naming the wrapper itself. Unlike the
        # flag, the variable is inherited by every exec, so each level would start
        # another wrapper — forever. The depth counter has to stop it, quickly.
        env = self.rig.env(CR_CLAUDE_CMD=WRAP, CR_CLAUDE_FALLBACKS="")
        r = run(["-p", "hi"], env, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("recurse", r.stderr)

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_an_alias_pointing_back_at_the_wrapper_is_stopped(self):
        # The same loop by another route: `alias claude='agent-retrier.sh …'`,
        # which is exactly how someone would wire this into their rc file.
        with open(os.path.join(self.rig.zdotdir, ".zshrc"), "w") as fh:
            fh.write("alias claude='%s --cmd claude'\n" % WRAP)
        # A PATH without a real `claude` on it, or the name would resolve to that
        # instead and the loop this test is about would never form.
        env = self.rig.env("zsh", CR_CLAUDE_FALLBACKS="",
                           PATH=os.pathsep.join([self.rig.bin, "/usr/bin", "/bin"]))
        r = run(["--cmd", "claude", "-p", "hi"], env, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("recurse", r.stderr)


class SupervisedCustomCommand(PtyTestCase):
    """The same shapes, but under the pty supervisor — the path that matters."""

    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def env(self, shell="bash", **over):
        e = self.rig.env(shell)
        e.update({
            "CLAUDE_CONFIG_DIR": self.cfg,
            "CR_SCRAPE": "always",
            "CR_SCRAPE_CONFIRM_SEC": "0.5",   # 3s by default, and nothing here is about it
            "CR_WAIT_SCALE": "3600",     # an hour of waiting becomes a second
            "CR_MARGIN_SEC": "0",
            "CR_USER_IDLE_SEC": "0",
            "CR_BUSY_IDLE_SEC": "0.2",
            "CR_LOG": os.path.join(self.rig.dir, "log"),
        })
        e.update(over)
        return e

    def test_a_binary_is_supervised(self):
        s = self.session(env=self.env(), args=["--cmd", self.rig.claude],
                         cwd=self.work)
        self.assertTrue(s.read_until("fake-claude ready"))

    def seen(self, s):
        """What actually arrived on the pty — the only evidence when this fails.

        A supervised session that goes wrong usually goes silent, and 'False is
        not true' from a timed-out read says nothing about why.
        """
        return "\n--- pty buffer ---\n%s\n--- end ---" % (s.buf or "(nothing)")

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_an_alias_is_supervised_and_retried(self):
        # The whole point: a user whose claude is an alias still gets the retry.
        s = self.session(
            env=self.env("zsh", FAKE_BANNER="You've hit your session limit - resets in 1 hours"),
            args=["--cmd", "claude-work"], cwd=self.work)
        self.assertTrue(s.read_until("GOT:continue", timeout=25), self.seen(s))

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_a_function_is_supervised_and_retried(self):
        s = self.session(
            env=self.env("zsh", FAKE_BANNER="You've hit your session limit - resets in 1 hours"),
            args=["--cmd", "claude-personal"], cwd=self.work)
        self.assertTrue(s.read_until("GOT:continue", timeout=25), self.seen(s))

    @unittest.skipUnless(has("zsh"), "no zsh")
    def test_the_exit_code_survives_the_shell_in_between(self):
        # An alias runs through a shell, which is one more process to lose the
        # status in. `claude` exiting 3 must still be 3 to the caller.
        s = self.session(env=self.env("zsh", FAKE_EXIT="3"),
                         args=["--cmd", "claude-work"], cwd=self.work)
        self.assertTrue(s.read_until("fake-claude ready", timeout=20), self.seen(s))
        s.send("quit\n")
        try:
            rc = s.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("session never exited" + self.seen(s))
        self.assertEqual(rc, 3, self.seen(s))

    def test_arguments_still_reach_claude(self):
        s = self.session(env=self.env(),
                         args=["--cmd", self.rig.claude, "--verbose"], cwd=self.work)
        self.assertTrue(s.read_until("argv=--verbose"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
