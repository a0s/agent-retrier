"""End-to-end runs of agent-retrier.sh against a real pty.

Two things are being proven here. First, that the wrapper is invisible during
ordinary use — keystrokes, exit codes, terminal size and window resizes all pass
through — because a wrapper that degrades the everyday session is worse than no
wrapper. Second, that a limit really does get detected and answered, through
each of the two channels, without tmux anywhere in the picture.
"""
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import fcntl
import sys
import termios
import tempfile
import time
import unittest

from helper import LEFT_ALONE, hermetic_env, restart_timers, roundtrip
from screen import Screen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(ROOT, "agent-retrier.sh")


def _fake_launcher():
    """A single-exec launcher for the fake claude.

    Running fake_claude.py via its `#!/usr/bin/env python3` shebang would pick up
    whatever `python3` resolves to — often a pyenv/asdf shim, i.e. a shell script
    that becomes an extra process in the chain. `exec` here keeps the process
    tree flat so the exit-status assertions below measure the wrapper, not the
    shim.
    """
    path = os.path.join(tempfile.mkdtemp(prefix="cr-fake-"), "claude")
    with open(path, "w") as fh:
        fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n'
                 % (sys.executable, os.path.join(ROOT, "test", "fake_claude.py")))
    os.chmod(path, 0o755)
    return path


FAKE = _fake_launcher()

# The same Badge the supervisor paints with, for the one number of its that a
# test has to wait out.
Badge = sys.modules[Screen.__module__].Badge

FRAME_CLOSED = "\x1b[?2026l"     # how claude ends a repaint, and what the overlay keys on
SAVE_CURSOR = "\x1b7"            # how `annotation_bytes` begins every paint of ours

# Where a session logs when its test did not say. One file per test PROCESS, not
# one for the machine: `./test/run.sh` runs several of these side by side, and a
# single file would be all of them interleaved, on top of whatever every earlier
# run left in it. It is not cleaned up, on purpose — when a test fails, the
# newest of these is where the wrapper said why.
LOG = os.path.join(tempfile.gettempdir(), "cr-pty-test.%d.log" % os.getpid())


class Session:
    """Runs agent-retrier.sh on a pty we control, the way a terminal emulator would."""

    def __init__(self, env=None, args=(), rows=40, cols=120, cwd=None, state=None):
        self.master, slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        # `state` stands in for ~/.agent-retrier (see helper.hermetic_env). A
        # test hands every session it starts the same one, so two of them still
        # see each other's claims the way two wrappers in one home do; a session
        # started on its own gets, and afterwards removes, its own.
        self._own_state = None
        if state is None:
            state = self._own_state = tempfile.mkdtemp(prefix="cr-pty-state-")
        if cwd is None:
            # Not wherever the suite happens to be standing. That was test/
            # itself, where a session with a restart armed quietly made a
            # `.agent-retrier/` for its handoff — in the checkout, which the
            # Linux run mounts read-only precisely so that nothing can.
            cwd = os.path.join(state, "cwd")
            os.makedirs(cwd, exist_ok=True)
        # A test run started from inside a wrapped session would inherit
        # AGENT_RETRIER_ACTIVE=1 (and the caller's tuning), and every wrapper
        # under test would dutifully degrade into a plain exec. Start clean.
        full = {k: v for k, v in os.environ.items()
                if not k.startswith("CR_")
                and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}
        full.update(hermetic_env(state))
        full.update({
            "CR_CLAUDE_BIN": FAKE,
            "CR_LOG": LOG,
            "CR_NOTIFY": "0",
            "CR_BADGE": "0",             # off unless a test is about the badge
            "PYTHONUNBUFFERED": "1",
            # Left unset, both the fake and the real ClaudeSessionRegistry (T02)
            # fall back to the developer's actual ~/.claude — reading (and
            # littering) whatever real sessions/transcripts happen to be there.
            "CLAUDE_CONFIG_DIR": tempfile.mkdtemp(prefix="cr-pty-cfg-"),
        })
        full.update(env or {})
        self.proc = subprocess.Popen(
            [WRAP, *args], stdin=slave, stdout=slave, stderr=slave,
            env=full, cwd=cwd, close_fds=True, start_new_session=True)
        os.close(slave)
        self.buf = ""

    def read_until(self, needle, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.buf:
                return True
            r, _, _ = select.select([self.master], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.buf += data.decode("utf-8", "replace")
        return needle in self.buf

    def drain(self, seconds=1.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.buf += data.decode("utf-8", "replace")

    def send(self, text):
        os.write(self.master, text.encode())

    def resize(self, rows, cols):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def wait(self, timeout=10):
        """Wait while still draining the pty.

        A session leader exiting with an unread output buffer is held in the
        kernel until that buffer is consumed, so a harness that stops reading
        would stall the process it is waiting on. Real terminals always read.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                self.drain(0.2)
                return rc
            self.drain(0.1)
        self.proc.kill()
        raise subprocess.TimeoutExpired(self.proc.args, timeout)

    def roundtrip(self, word="tick", timeout=10):
        return roundtrip(self, word, timeout)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            # Killed with output nobody read, the wrapper does not finish dying
            # until the terminal has taken that output off its hands (see
            # `wait` above), and a plain `proc.wait()` here sat out its whole
            # timeout and then raised — out of a cleanup, on a test that had
            # passed. Tests were reading on to the end of a restart they had
            # stopped caring about purely to keep this from happening.
            deadline = time.time() + 5
            while time.time() < deadline:
                self.drain(0.02)
                try:
                    self.proc.wait(timeout=0.02)
                    break
                except subprocess.TimeoutExpired:
                    pass
            self.proc.wait(timeout=5)
        try:
            os.close(self.master)
        except OSError:
            pass
        if self._own_state:
            shutil.rmtree(self._own_state, ignore_errors=True)
            self._own_state = None


class PtyTestCase(unittest.TestCase):
    def session(self, **kw):
        kw.setdefault("state", self.state())
        s = Session(**kw)
        self.addCleanup(s.close)
        return s

    def state(self):
        """This test's stand-in for ~/.agent-retrier, made the first time a
        session is started and shared by every session the test starts."""
        if getattr(self, "_state", None) is None:
            self._state = tempfile.mkdtemp(prefix="cr-pty-state-")
            self.addCleanup(shutil.rmtree, self._state, ignore_errors=True)
        return self._state

    # A class whose tests read the wrapper's log names the file in `self.log`
    # (and hands it to the wrapper as CR_LOG); these two are for those.
    def logged(self):
        try:
            with open(self.log) as fh:
                return fh.read()
        except OSError:
            return ""

    def wait_log(self, needle, session, timeout=25):
        """Wait for a line in the wrapper's log, draining the pty meanwhile.

        Waiting on the log rather than on a fixed number of seconds is what keeps
        these honest on a loaded runner: the assertion is about what the wrapper
        decided, not about how long it took to decide it.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.logged():
                return True
            session.drain(0.05)
        return needle in self.logged()

    # One whole paint of the badge, as `annotation_bytes` writes it: the mark,
    # whatever it says, and the cursor handed back.
    _BADGE = re.compile("[◆◇][^\x1b]*\x1b\\[0m\x1b8")

    def badge_painted(self, session, since=0, timeout=5):
        """Wait until the badge has been drawn, whole, at or after offset
        `since` of what the session has printed.

        The badge goes on once claude's output has been quiet for Badge.QUIET,
        and again after everything claude prints. A test that needs it on
        screen used to give it a second; this takes as long as it takes.
        """
        deadline = time.time() + timeout
        while not self._BADGE.search(session.buf, since) and time.time() < deadline:
            session.drain(0.05)
        self.assertTrue(self._BADGE.search(session.buf, since),
                        "no badge was painted: %r" % session.buf[since:][-300:])


class TestTransparency(PtyTestCase):
    def test_arguments_reach_claude(self):
        s = self.session(args=["--model", "opus", "--verbose"])
        self.assertTrue(s.read_until("argv=--model opus --verbose"))

    def test_attach_arguments_are_not_preceded_by_statusline_settings(self):
        # Claude parses a prepended --settings as the start of a normal launch,
        # not an `attach` subcommand.  Keep the user vector intact even while a
        # context restart would ordinarily enable the statusline proxy.
        s = self.session(env={"CR_CONTEXT_TOKENS": "135200"},
                         args=["attach", "session-id"])
        self.assertTrue(s.read_until("argv=attach session-id"), s.buf)

    def test_terminal_size_is_propagated(self):
        s = self.session(rows=40, cols=120)
        self.assertTrue(s.read_until("winsize 120x40"))

    def test_keystrokes_pass_through(self):
        s = self.session()
        s.read_until("ready")
        s.send("hello world\r")
        self.assertTrue(s.read_until("GOT:hello world"))

    def test_control_characters_pass_through_unmangled(self):
        # Shift-Enter and friends are just escape sequences; upstream broke them
        # by routing input through `tmux send-keys` (issue #59). Here nothing
        # rewrites the byte stream, so an arbitrary sequence survives intact.
        s = self.session()
        s.read_until("ready")
        s.send("a\x1b[200~b\x1b[201~c\r")
        self.assertTrue(s.read_until("GOT:a\x1b[200~b\x1b[201~c"))

    def test_exit_code_is_preserved(self):
        s = self.session(env={"FAKE_EXIT": "42"})
        s.read_until("ready")
        s.send("quit\r")
        self.assertEqual(s.wait(), 42)

    def test_zero_exit_is_preserved(self):
        s = self.session(env={"FAKE_EXIT": "0"})
        s.read_until("ready")
        s.send("quit\r")
        self.assertEqual(s.wait(), 0)

    def test_resize_reaches_the_child(self):
        # A pane resize has to be forwarded onto the inner pty, or Claude keeps
        # rendering at the old width for the rest of the session.
        s = self.session(rows=24, cols=80)
        self.assertTrue(s.read_until("winsize 80x24"))
        s.resize(50, 100)
        # The wrapper acts on a resize at the top of its loop, so a line typed
        # straight after one reaches claude a turn AHEAD of the new size. Once
        # a line has been answered, that turn has been taken.
        self.assertTrue(s.roundtrip())
        s.send("winsize\r")
        self.assertTrue(s.read_until("winsize 100x50"))

    def test_signals_reach_the_child(self):
        # pty.fork puts claude in its own session, so a SIGTERM aimed at the
        # wrapper would otherwise leave the child running as an orphan.
        s = self.session()
        s.read_until("ready")
        s.proc.send_signal(signal.SIGTERM)
        self.assertEqual(s.wait(timeout=10), 128 + signal.SIGTERM)

    def test_wrapper_is_bypassed_when_disabled(self):
        s = self.session(env={"CR_DISABLE": "1"})
        self.assertTrue(s.read_until("ready"))
        s.send("quit\r")
        s.wait()


class TestLogTag(PtyTestCase):
    """Two wrappers writing into one CR_LOG must stay untangled by pid."""

    def test_two_concurrent_wrappers_are_distinguishable_by_pid(self):
        log_path = os.path.join(tempfile.mkdtemp(prefix="cr-log-"), "shared.log")
        s1 = self.session(env={"CR_LOG": log_path})
        s2 = self.session(env={"CR_LOG": log_path})
        self.assertTrue(s1.read_until("ready"))
        self.assertTrue(s2.read_until("ready"))
        s1.send("quit\r")
        s2.send("quit\r")
        s1.wait()
        s2.wait()
        self.assertNotEqual(s1.proc.pid, s2.proc.pid)

        with open(log_path) as fh:
            lines = fh.read().splitlines()
        tag1, tag2 = "[cr %d claude]" % s1.proc.pid, "[cr %d claude]" % s2.proc.pid
        own1 = [l for l in lines if tag1 in l]
        own2 = [l for l in lines if tag2 in l]

        # grep by one pid gives a connected start -> ... -> exit sequence,
        # with nothing from the other process mixed in.
        self.assertTrue(any(" start: " in l for l in own1))
        self.assertTrue(any(" exit: " in l for l in own1))
        self.assertTrue(any(" start: " in l for l in own2))
        self.assertTrue(any(" exit: " in l for l in own2))
        self.assertFalse(any(tag2 in l for l in own1))
        self.assertFalse(any(tag1 in l for l in own2))
        start1 = next(l for l in own1 if " start: " in l)
        exit1 = next(l for l in own1 if " exit: " in l)
        self.assertLess(own1.index(start1), own1.index(exit1))

        self.assertIn("cwd=", start1)
        self.assertRegex(exit1, r"exit: \d+ after ")


class TestScrapeChannel(PtyTestCase):
    """The fallback channel: the banner is only ever seen on screen."""

    # A scraped banner is acted on once it has stood for CR_SCRAPE_CONFIRM_SEC.
    # At the 3s default that was most of what every test here spent, and three
    # of them were asserting "nothing happened" inside a window no longer than
    # the confirmation itself — which nothing could have failed.
    CONFIRM = 0.5

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.log = os.path.join(self.cfg, "log")

    def env(self, **over):
        e = {
            "CLAUDE_CONFIG_DIR": self.cfg,
            "CR_LOG": self.log,
            "CR_SCRAPE": "always",
            "CR_SCRAPE_CONFIRM_SEC": str(self.CONFIRM),
            "CR_WAIT_SCALE": "3600",     # an hour of waiting becomes a second
            "CR_MARGIN_SEC": "0",
            "CR_USER_IDLE_SEC": "0",
            "CR_BUSY_IDLE_SEC": "0.2",
        }
        e.update(over)
        return e

    def confirmed(self, s):
        """What is on screen now has stood for CR_SCRAPE_CONFIRM_SEC, and the
        wrapper has been round its loop since: had it taken any of it for a
        banner, it has said so in the log by now (see `helper.roundtrip`).

        Called once the text in question has been read off the pty, which is
        necessarily after the wrapper read it — so this sleep cannot end before
        the wrapper's own confirmation window does.
        """
        time.sleep(self.CONFIRM)
        self.assertTrue(s.roundtrip(), s.buf[-500:])

    def test_a_banner_on_screen_triggers_a_retry(self):
        s = self.session(
            env=self.env(FAKE_BANNER="You've hit your session limit - resets in 1 hours"),
            cwd=self.work)
        self.assertTrue(s.read_until("GOT:continue", timeout=20))
        # The two lines the tests below look for in the log and must not find.
        self.assertIn("limit detected (screen)", self.logged())
        self.assertIn("sending retry", self.logged())

    def test_the_message_is_configurable(self):
        s = self.session(
            env=self.env(FAKE_BANNER="You've hit your session limit - resets in 1 hours",
                         CR_MESSAGE="please resume"),
            cwd=self.work)
        self.assertTrue(s.read_until("GOT:please resume", timeout=20))

    def test_ordinary_output_triggers_nothing(self):
        s = self.session(
            env=self.env(FAKE_BANNER="all done, no limits here"), cwd=self.work)
        self.assertTrue(s.read_until("all done"))
        self.confirmed(s)
        self.assertNotIn(" detected (screen)", self.logged())   # limit or stall
        self.assertNotIn("GOT:continue", s.buf)

    def test_text_about_a_limit_triggers_nothing(self):
        # A tool render quoting banner text: upstream #63, a 22-hour bogus wait.
        #
        # The same call three times over, because what says "quotation" is the
        # call and not the bullet in front of it. "*" is what this fixture
        # opened with for a long time: `_TOOL_HEADER` knew only "●⏺∙", and the
        # wrapper took the line for a live banner. The test stayed green
        # because it stopped looking at 3s, which is when the confirmation
        # ends, and because a wait until "3pm (UTC)" types nothing for up to
        # 24 (CR_WAIT_SCALE'd) seconds after that. Nothing at all is how
        # Claude Code itself draws a call that is still running, every other
        # 600ms, while its bullet is blinked off. And "⏺" is the other 600.
        call = 'Bash(grep "hit your session limit - resets 3pm (UTC)" log)'
        s = self.session(
            env=self.env(FAKE_BANNER="\r\n".join(["* " + call, " " + call, "⏺ " + call])),
            cwd=self.work)
        self.assertTrue(s.read_until("⏺ Bash(grep"))
        self.confirmed(s)
        self.assertNotIn("limit detected", self.logged())
        self.assertNotIn("GOT:continue", s.buf)

    def test_the_agent_roster_is_never_scraped(self):
        # `claude agents` lists OTHER sessions, and opening a card scrolls that
        # session's history — banners and all — past us. A neighbour's card
        # reading "resets 2:10am" scheduled an 11h18m wait in a terminal whose
        # own limit lifted in seven minutes, and nothing could correct it.
        #
        # The log rather than the retry: a wait scheduled here is already the
        # bug, whether or not "continue" has been typed yet.
        log = os.path.join(self.work, "roster.log")
        s = self.session(
            env=self.env(CR_SCRAPE="auto", CR_LOG=log,
                         FAKE_BANNER="You've hit your session limit - resets in 1 hours"),
            args=["agents"], cwd=self.work)
        self.assertTrue(s.read_until("argv=agents"))
        self.assertTrue(s.read_until("session limit"))
        self.confirmed(s)
        self.assertNotIn("GOT:continue", s.buf)
        with open(log) as fh:
            written = fh.read()
        self.assertNotIn("limit detected", written)
        self.assertIn("wrapping the agent roster", written)

    def test_an_ordinary_session_still_is(self):
        # The same banner, the same settings, without the subcommand.
        log = os.path.join(self.work, "session.log")
        s = self.session(
            env=self.env(CR_SCRAPE="auto", CR_LOG=log,
                         FAKE_BANNER="You've hit your session limit - resets in 1 hours"),
            cwd=self.work)
        self.assertTrue(s.read_until("GOT:continue", timeout=20))
        with open(log) as fh:
            self.assertIn("limit detected (screen)", fh.read())

    def test_a_busy_session_is_not_typed_into(self):
        s = self.session(
            env=self.env(FAKE_BANNER="You've hit your session limit - resets in 1 hours",
                         FAKE_WORKING="1", CR_BUSY_IDLE_SEC="30"),
            cwd=self.work)
        self.assertTrue(s.read_until("Cogitating"))
        # The wrapper's own account of the moment the reset arrived. The fake
        # never reads a line while it is "working", so there is no GOT to look
        # for whatever the wrapper types — this used to assert the absence of
        # one, and could not have failed. What a keystroke would leave is its
        # echo: the pty prints back what is typed into it.
        self.assertTrue(self.wait_log("deferring retry: claude is working", s),
                        self.logged())
        self.assertNotIn("sending retry", self.logged())
        self.assertNotIn("continue", s.buf)

    def test_an_unsent_draft_defers_the_retry(self):
        # The limit lands a second into the session, on someone who is already
        # halfway through a sentence.
        s = self.session(
            env=self.env(FAKE_BANNER="You've hit your session limit - resets in 1 hours",
                         FAKE_DELAY="1", CR_USER_IDLE_SEC="0"),
            cwd=self.work)
        self.assertTrue(s.read_until("winsize"))
        s.send("half a thought")            # typed, never submitted
        # Not "nothing was typed for four seconds": that was the confirmation
        # and the wait, to the second, and the draft went out before the gate
        # was ever asked. This is the gate being asked, and answering.
        self.assertTrue(self.wait_log("deferring retry: unsent text in the prompt box", s),
                        self.logged())
        self.assertNotIn("sending retry", self.logged())
        self.assertNotIn("GOT:continue", s.buf)
        s.send("\r")                        # the human submits it themselves
        # A deferred retry is looked at again 15s later, and that number is
        # not ours to shorten (`wake_at = now + 15`, no knob).
        self.assertTrue(s.read_until("GOT:continue", timeout=30), self.logged())


class TestBadge(PtyTestCase):
    """The corner badge, judged the way a user judges it: by what is on screen.

    Every assertion here goes through a terminal emulator rather than the byte
    stream, because the whole risk of painting into someone else's TUI is about
    *where* the bytes land — a badge that greps fine but scrolls the screen or
    strands the cursor is a regression, not a feature.
    """

    ROWS, COLS = 40, 120

    def screen(self, session, rows=None, cols=None):
        s = Screen(rows or self.ROWS, cols or self.COLS)
        s.feed(session.buf)
        return s

    def running(self, **env):
        e = {"CR_BADGE": "1"}
        e.update(env)
        s = self.session(env=e, rows=self.ROWS, cols=self.COLS)
        self.assertTrue(s.read_until("winsize"))
        self.badge_painted(s)             # it goes on once the frame has gone quiet
        return s

    def test_it_is_painted_in_the_bottom_right_corner(self):
        sc = self.screen(self.running())
        self.assertEqual(sc.line(self.ROWS).strip(), "◆ cr")
        self.assertTrue(sc.line(self.ROWS).startswith(" " * 115))   # right-aligned
        self.assertEqual(sc.cells[self.ROWS - 1][self.COLS - 1], " ")   # spare column

    def test_it_never_scrolls_the_screen(self):
        # The bottom-right cell is the one place a printed character makes the
        # whole screen jump. If that ever happens, claude's output walks upward
        # a line at a time for the rest of the session.
        s = self.running()
        for _ in range(3):
            self.assertTrue(s.roundtrip())
            # ...and the badge again, over what that just printed: the repaint
            # is the write that would scroll the screen, if any did.
            self.badge_painted(s, since=s.buf.rindex("GOT:"))
        sc = self.screen(s)
        self.assertEqual(sc.scrolled, 0)
        self.assertTrue(sc.line(1).startswith("fake-claude ready"))

    def test_it_does_not_move_the_cursor_claude_is_using(self):
        # Claude keeps drawing where it left off; the badge saves and restores.
        s = self.running()
        s.send("hello\r")
        self.assertTrue(s.read_until("GOT:hello"))
        self.badge_painted(s, since=s.buf.index("GOT:hello"))
        sc = self.screen(s)
        # The reply lands at the start of its own line, where claude's cursor was
        # — not in the corner the badge jumped to.
        rows = [n for n in range(1, self.ROWS + 1) if sc.line(n) == "GOT:hello"]
        self.assertEqual(len(rows), 1, sc.text())
        self.assertLess(rows[0], self.ROWS)
        self.assertEqual(sc.line(self.ROWS).strip(), "◆ cr")

    def test_it_does_not_colour_what_claude_draws_next(self):
        s = self.running()
        s.send("plain\r")
        self.assertTrue(s.read_until("GOT:plain"))
        self.badge_painted(s, since=s.buf.index("GOT:plain"))
        sc = self.screen(s)
        self.assertEqual(sc.attr_at(3, 1), "")        # no dim leaking out of DECSC
        self.assertEqual(sc.attr_at(self.ROWS, 116), "2")

    def test_it_is_off_when_asked(self):
        s = self.session(env={"CR_BADGE": "0"}, rows=self.ROWS, cols=self.COLS)
        self.assertTrue(s.read_until("winsize"))
        # A badge goes on Badge.QUIET after claude stops printing. Once that
        # has passed, the next turn of the wrapper's loop is the one that would
        # paint it — and an answered line is proof that turn was taken.
        time.sleep(Badge.QUIET)
        self.assertTrue(s.roundtrip())
        self.assertNotIn("◆", s.buf)

    def test_the_corner_is_configurable(self):
        sc = self.screen(self.running(CR_BADGE_POS="top-left"))
        self.assertTrue(sc.line(1).startswith("◆ cr"))
        self.assertNotIn("◆", sc.line(self.ROWS))

    def test_the_label_is_configurable(self):
        sc = self.screen(self.running(CR_BADGE_LABEL="watching"))
        self.assertEqual(sc.line(self.ROWS).strip(), "◆ watching")

    def test_a_resize_moves_it(self):
        s = self.running()
        before = len(s.buf)
        s.resize(24, 60)
        self.badge_painted(s, since=before)
        sc = self.screen(s, rows=24, cols=60)
        self.assertIn("◆ cr", sc.line(24))
        self.assertEqual(sc.cells[23][59], " ")

    def test_it_shows_the_time_left_while_waiting(self):
        # A limit that "resets in 5 hours", compressed 60x, is a five-minute wait
        # — long enough to read the countdown off the screen.
        s = self.running(
            CLAUDE_CONFIG_DIR=tempfile.mkdtemp(prefix="cr-cfg-"),
            CR_SCRAPE="always", CR_WAIT_SCALE="60", CR_MARGIN_SEC="0",
            CR_SCRAPE_CONFIRM_SEC="0.5",
            FAKE_BANNER="You've hit your session limit - resets in 5 hours")
        self.assertTrue(s.read_until("session limit"))
        countdown = re.compile(r"◆ cr ([1-5])m$")
        deadline = time.time() + 10
        while time.time() < deadline:
            s.drain(0.1)
            sc = self.screen(s)
            if countdown.search(sc.line(self.ROWS)):
                break
        sc = self.screen(s)
        self.assertRegex(sc.line(self.ROWS), countdown)
        self.assertEqual(sc.attr_at(self.ROWS, sc.line(self.ROWS).index("◆") + 1), "2;33")

    def test_it_is_taken_off_the_screen_on_exit(self):
        s = self.running()
        s.send("quit\r")
        self.assertEqual(s.wait(), 0)
        self.assertNotIn("◆", self.screen(s).text())


class TestAgentOverlay(PtyTestCase):
    """T27: each row of Claude Code's own subagent tree annotated with the
    model that agent is really running on — judged through the same terminal
    emulator the badge is, since the risk is identical: where the bytes land,
    whether the screen scrolled, whether the cursor was left where claude
    expects it.
    """

    ROWS, COLS = 40, 120
    ROW_A, ROW_B = 10, 12     # test/fake_claude.py's AGENT_TREE_ROW1/ROW3

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def screen(self, session, rows=None, cols=None):
        s = Screen(rows or self.ROWS, cols or self.COLS)
        s.feed(session.buf)
        return s

    def running(self, **env):
        e = {"CR_AGENTS_OVERLAY": "1", "CLAUDE_CONFIG_DIR": self.cfg,
             "FAKE_AGENT_TREE": "1", "CR_POLL_SEC": "0.05"}
        e.update(env)
        s = self.session(env=e, cwd=self.work, rows=self.ROWS, cols=self.COLS)
        self.assertTrue(s.read_until("winsize"))
        return s

    def wait_for(self, s, needle, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s.drain(0.2)
            sc = self.screen(s)
            if needle in sc.line(self.ROW_A) or needle in sc.line(self.ROW_B):
                return sc
        self.fail("%r never appeared on row %d or %d; got %r"
                  % (needle, self.ROW_A, self.ROW_B, self.screen(s).text()))

    def test_the_model_is_annotated_at_the_agent_s_row(self):
        s = self.running()
        self.wait_for(s, "sonnet-5/?")
        sc = self.screen(s)
        self.assertIn("sonnet-5/?", sc.line(self.ROW_A))
        self.assertIn("haiku-4.5/?", sc.line(self.ROW_B))

    def test_it_never_touches_the_last_column(self):
        s = self.running()
        sc = self.wait_for(s, "sonnet-5/?")
        self.assertEqual(sc.cells[self.ROW_A - 1][self.COLS - 1], " ")
        self.assertEqual(sc.cells[self.ROW_B - 1][self.COLS - 1], " ")

    def test_it_never_scrolls_the_screen(self):
        s = self.running()
        sc = self.wait_for(s, "sonnet-5/?")
        self.assertEqual(sc.scrolled, 0)

    def test_it_is_off_by_default(self):
        s = self.session(env={"CLAUDE_CONFIG_DIR": self.cfg, "FAKE_AGENT_TREE": "1"},
                         cwd=self.work, rows=self.ROWS, cols=self.COLS)
        self.assertTrue(s.read_until("winsize"))
        # The overlay paints in the same turn of the loop that sees claude
        # close a frame, so the frame is what to wait for (it is drawn most of
        # a second in — the old 1.0s here cleared it by a fifth of one), and an
        # answered line after it is everything that turn wrote.
        self.assertTrue(s.read_until(FRAME_CLOSED), s.buf[-300:])
        self.assertTrue(s.roundtrip())
        self.assertNotIn("sonnet-5", s.buf)      # not one extra byte, model or otherwise
        self.assertNotIn(SAVE_CURSOR, s.buf)     # ...which is how either painter starts one

    def test_the_label_returns_after_claude_repaints_the_row(self):
        s = self.running()
        self.wait_for(s, "sonnet-5/?")
        s.send("repaint-tree\r")
        self.assertTrue(s.read_until("GOT:repaint-tree"))
        # claude's own repaint just wiped it with ESC[K — the overlay has to
        # notice the next closed frame and put it straight back.
        sc = self.wait_for(s, "sonnet-5/?")
        self.assertIn("haiku-4.5/?", sc.line(self.ROW_B))

    def test_a_tree_row_that_lands_on_the_badges_row_is_left_to_the_badge(self):
        # T28: a terminal small enough (12 rows) that the last agent tree row
        # (fake_claude's fixed AGENT_TREE_ROW3 = 12) IS the badge's own
        # bottom-right row. Coordination now goes through the shared
        # OccupiedRows registry, not a badge_row= computed once by name at
        # the call site -- this is the end-to-end proof that path still
        # avoids the collision.
        rows, cols = 12, 120
        e = {"CR_AGENTS_OVERLAY": "1", "CLAUDE_CONFIG_DIR": self.cfg,
             "FAKE_AGENT_TREE": "1", "CR_POLL_SEC": "0.05", "CR_BADGE": "1"}
        s = self.session(env=e, cwd=self.work, rows=rows, cols=cols)
        self.assertTrue(s.read_until("winsize"))
        deadline = time.time() + 10
        sc = None
        while time.time() < deadline:
            s.drain(0.2)
            sc = self.screen(s, rows=rows, cols=cols)
            if "sonnet-5/?" in sc.line(self.ROW_A):
                break
        # The tree's last row is drawn over the badge's corner and wipes it;
        # the badge comes back on its own due() timer, after the frame.
        self.badge_painted(s, since=s.buf.index(FRAME_CLOSED))
        sc = self.screen(s, rows=rows, cols=cols)
        self.assertIsNotNone(sc)
        self.assertIn("sonnet-5/?", sc.line(self.ROW_A))     # row 10: no collision
        self.assertNotIn("haiku-4.5/?", sc.line(rows))       # row 12: the badge's row
        self.assertIn("◆ cr", sc.line(rows))                 # the badge, undisturbed
        self.assertEqual(sc.scrolled, 0)


class TestAgentsPanelOverlay(PtyTestCase):
    """T29: the OTHER subagent view, opened by typing /tasks while at least
    one agent is still running -- a different row shape from T27's inline
    tree (no glyph, a "(state)" marker instead), reproduced by
    test/fixtures/agents-panel-2.1.273.bin and test/fake_claude.py's
    FAKE_AGENTS_PANEL. Judged through the same terminal emulator as the tree,
    for the same reason: what matters is where the bytes land, not that they
    were sent.
    """

    ROWS, COLS = 40, 120
    ROW_A, ROW_B = 31, 32     # test/fake_claude.py's PANEL_ROW1/PANEL_ROW2

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def screen(self, session, rows=None, cols=None):
        s = Screen(rows or self.ROWS, cols or self.COLS)
        s.feed(session.buf)
        return s

    def running(self, **env):
        e = {"CR_AGENTS_OVERLAY": "1", "CLAUDE_CONFIG_DIR": self.cfg,
             "FAKE_AGENTS_PANEL": "1", "CR_POLL_SEC": "0.05"}
        e.update(env)
        s = self.session(env=e, cwd=self.work, rows=self.ROWS, cols=self.COLS)
        self.assertTrue(s.read_until("winsize"))
        return s

    def wait_for(self, s, needle, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s.drain(0.2)
            sc = self.screen(s)
            if needle in sc.line(self.ROW_A) or needle in sc.line(self.ROW_B):
                return sc
        self.fail("%r never appeared on row %d or %d; got %r"
                  % (needle, self.ROW_A, self.ROW_B, self.screen(s).text()))

    def test_the_model_is_annotated_at_the_agent_s_row(self):
        s = self.running()
        self.wait_for(s, "sonnet-5/?")
        sc = self.screen(s)
        self.assertIn("sonnet-5/?", sc.line(self.ROW_A))
        self.assertIn("haiku-4.5/?", sc.line(self.ROW_B))

    def test_the_section_header_row_is_left_untouched(self):
        s = self.running()
        self.wait_for(s, "sonnet-5/?")
        sc = self.screen(s)
        self.assertIn("Local agents", sc.line(30))
        self.assertNotIn("sonnet", sc.line(30))
        self.assertNotIn("haiku", sc.line(30))

    def test_it_never_touches_the_last_column(self):
        s = self.running()
        sc = self.wait_for(s, "sonnet-5/?")
        self.assertEqual(sc.cells[self.ROW_A - 1][self.COLS - 1], " ")
        self.assertEqual(sc.cells[self.ROW_B - 1][self.COLS - 1], " ")

    def test_it_never_scrolls_the_screen(self):
        s = self.running()
        sc = self.wait_for(s, "sonnet-5/?")
        self.assertEqual(sc.scrolled, 0)

    def test_it_is_off_by_default(self):
        s = self.session(env={"CLAUDE_CONFIG_DIR": self.cfg, "FAKE_AGENTS_PANEL": "1"},
                         cwd=self.work, rows=self.ROWS, cols=self.COLS)
        self.assertTrue(s.read_until("winsize"))
        # As for the tree's test of the same name, above.
        self.assertTrue(s.read_until(FRAME_CLOSED), s.buf[-300:])
        self.assertTrue(s.roundtrip())
        self.assertNotIn("sonnet-5", s.buf)
        self.assertNotIn(SAVE_CURSOR, s.buf)

    def test_the_label_returns_after_claude_repaints_the_row(self):
        s = self.running()
        self.wait_for(s, "sonnet-5/?")
        s.send("repaint-panel\r")
        self.assertTrue(s.read_until("GOT:repaint-panel"))
        sc = self.wait_for(s, "sonnet-5/?")
        self.assertIn("haiku-4.5/?", sc.line(self.ROW_B))


class TestTheUpdateNotice(PtyTestCase):
    """What a user with an out-of-date copy actually sees, on a real terminal.

    The unit tests decide what the notice SAYS; this one is about whether it
    reaches the screen at all, before claude takes the terminal over.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-upd-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = os.path.join(self.dir, "update.json")
        # The version is baked into the script and deliberately not overridable
        # from the environment — a wrapper that can be told it is a different
        # version is a wrapper that can be told it is up to date.
        self.version = subprocess.run(
            [WRAP, "--cr-version"], capture_output=True, text=True,
            check=True).stdout.split()[-1]

    def cached(self, version):
        with open(self.cache, "w") as fh:
            fh.write('{"version": "%s", "at": %d}' % (version, int(time.time())))

    def env(self, **over):
        e = {"CR_UPDATE_CACHE": self.cache, "CR_UPDATE_NOTICE_SEC": "0",
             "CR_UPDATE_CHECK": "1"}
        e.update(over)
        return e

    def test_it_is_on_screen_before_claude_starts(self):
        self.cached("v9.9.9")
        s = self.session(env=self.env())
        self.assertTrue(s.read_until("9.9.9"), s.buf)
        self.assertIn(self.version, s.buf)          # both numbers, not just the new one
        self.assertIn("is out", s.buf)
        # ...and the session it was printed over is a normal one.
        self.assertTrue(s.read_until("fake-claude ready"), s.buf)
        notice = s.buf.index("9.9.9")
        self.assertLess(notice, s.buf.index("fake-claude ready"))

    def test_a_current_copy_is_told_nothing(self):
        self.cached("v" + self.version)
        s = self.session(env=self.env())
        self.assertTrue(s.read_until("fake-claude ready"), s.buf)
        self.assertNotIn("is out", s.buf)

    def test_the_switch_silences_it(self):
        self.cached("v9.9.9")
        s = self.session(env=self.env(CR_UPDATE_CHECK="0"))
        self.assertTrue(s.read_until("fake-claude ready"), s.buf)
        self.assertNotIn("is out", s.buf)

    def test_a_check_that_ran_leaves_the_cache_behind(self):
        # No cache at all: nothing is said, and the refresh that follows is what
        # the NEXT launch reads. The feed is a file:// url, so no network.
        feed = os.path.join(self.dir, "latest.json")
        with open(feed, "w") as fh:
            fh.write('{"tag_name": "v9.9.9"}')
        s = self.session(env=self.env(CR_UPDATE_URL="file://" + feed))
        self.assertTrue(s.read_until("fake-claude ready"), s.buf)
        self.assertNotIn("is out", s.buf)
        for _ in range(100):
            if os.path.exists(self.cache):
                break
            time.sleep(0.05)
        self.assertIn("9.9.9", open(self.cache).read())


class TestTranscriptChannel(PtyTestCase):
    """The primary channel: nothing is scraped, the JSONL record drives it."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.log = os.path.join(self.cfg, "log")

    def test_a_rate_limit_record_triggers_a_retry(self):
        s = self.session(env={
            "CLAUDE_CONFIG_DIR": self.cfg,
            "CR_LOG": self.log,
            "CR_SCRAPE": "never",            # prove the transcript alone is enough
            "CR_WAIT_SCALE": "3600",
            "CR_MARGIN_SEC": "0",
            "CR_USER_IDLE_SEC": "0",
            "CR_POLL_SEC": "0.2",
            "FAKE_TRANSCRIPT": "You've hit your weekly limit - resets in 2 hours",
        }, cwd=self.work)
        self.assertTrue(s.read_until("GOT:continue", timeout=25))
        # The line the two tests below look for in the log and must not find.
        self.assertIn("sending retry", self.logged())

    def test_an_answered_turn_ends_the_wait(self):
        # The account was switched mid-wait (or the plan upgraded, or the quota
        # came back early): nothing announces it, the session simply starts
        # answering. Left alone, the wrapper would count down its 40 hours over
        # a working session and eventually type into it.
        s = self.session(env={
            "CLAUDE_CONFIG_DIR": self.cfg,
            "CR_LOG": self.log,
            "CR_SCRAPE": "never",
            "CR_NOTIFY": "1",
            "CR_WAIT_SCALE": "3600",
            "CR_MARGIN_SEC": "0",
            "CR_POLL_SEC": "0.2",
            "FAKE_TRANSCRIPT": "You've hit your weekly limit - resets in 40 hours",
        }, cwd=self.work)
        self.assertTrue(s.read_until("usage limit detected", timeout=15))
        s.send("answer\r")
        self.assertTrue(s.read_until("wait cancelled", timeout=15))
        # The retry this wait was counting down to is forty (scaled) seconds
        # off; no test could sit here long enough to watch it not be typed,
        # and the two seconds this one used to sit proved nothing either way.
        # What can be seen is that the wait itself is gone, in the wrapper's
        # own words, and that a turn of its loop later nothing has gone out.
        self.assertIn("dropping the wait", self.logged())
        self.assertTrue(s.roundtrip())
        self.assertNotIn("sending retry", self.logged())
        self.assertNotIn("GOT:continue", s.buf)

    def test_a_live_transcript_turns_the_scraper_off(self):
        # Both channels active: the transcript is being written (so `seen_any`
        # is true), and the screen shows text that WOULD match the scraper. The
        # scraper must stand down rather than double-fire on the render.
        s = self.session(env={
            "CLAUDE_CONFIG_DIR": self.cfg,
            "CR_LOG": self.log,
            "CR_SCRAPE": "auto",
            "CR_SCRAPE_CONFIRM_SEC": "0.5",  # still several CR_POLL_SECs: time
                                             # for the transcript to be noticed
            "CR_WAIT_SCALE": "3600",
            "CR_MARGIN_SEC": "0",
            "CR_USER_IDLE_SEC": "0",
            "CR_POLL_SEC": "0.2",
            "FAKE_TRANSCRIPT_PLAIN": "ordinary turn",   # a non-limit record: just growth
            "FAKE_BANNER": "You've hit your session limit - resets in 1 hours",
        }, cwd=self.work)
        self.assertTrue(s.read_until("session limit"))
        # The banner has stood for its confirmation window and the wrapper has
        # been round its loop since (see TestScrapeChannel.confirmed): a
        # scraper that had not stood down has logged the limit by now.
        time.sleep(0.5)
        self.assertTrue(s.roundtrip())
        self.assertNotIn("limit detected", self.logged())
        self.assertNotIn("GOT:continue", s.buf)


# Both fakes print FAKE_BANNER after everything they write at startup, the two
# copies of FAKE_USAGE included — so on a session that scrapes nothing it is a
# free signal, on the pty, that the usage a test is about is now on file.
USAGE_DOWN = "usage is on file"


class TestContextRestart(PtyTestCase):
    """The second trigger, end to end: a session whose context is nearly full is
    folded into a file, cleared, and unfolded — with the wrapper reading the
    usage figures claude writes and typing all four steps itself.

    The fake plays the session's side: it writes the handoff when asked, moves to
    a new transcript on `/clear` (which is what Claude Code does, and what makes
    "did the context actually fall" answerable), and answers the resume phrase
    with a small turn.
    """

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.log = os.path.join(self.cfg, "log")

    def env(self, **over):
        e = restart_timers(
            CLAUDE_CONFIG_DIR=self.cfg,
            CR_LOG=self.log,
            CR_CONTEXT_TOKENS="510000",           # 510k of a 1M window
            FAKE_USAGE="700000",
            FAKE_MODEL="claude-opus-5")
        e.update(over)
        return e

    def test_a_full_context_is_folded_cleared_and_unfolded(self):
        s = self.session(env=self.env(), cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("context restarted", s), self.logged())

        # The file the whole thing turns on, with the nonce as its last line.
        with open(os.path.join(self.work, "handoff.md")) as fh:
            handoff = fh.read()
        self.assertTrue(handoff.rstrip().split("\n")[-1].startswith("HANDOFF-"))

    def test_a_slow_clear_still_finishes_the_restart(self):
        # T08: the resume phrase used to go out CR_STEP_GAP_SEC after `/clear`
        # was TYPED, not after it took hold. fake_claude here does not even
        # accept it for 1.5s -- several times the 0.2s step gap, so a resume
        # sent on the old schedule lands well inside the stall, and well short
        # of the two things that would make this a different test:
        # CR_VERIFY_SEC (8s), past which `/clear` is typed a second time, and
        # CR_CLEAR_SETTLE_SEC, past which a screen that has merely gone quiet
        # is taken for confirmation without the new session ever showing up.
        s = self.session(env=self.env(FAKE_SCRIPT="delayclear=1.5",
                                      CR_CLEAR_SETTLE_SEC="10"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("context restarted", s), self.logged())
        # The fake answers lines in order, so a resume typed too early is still
        # answered, after the clear, and all of the above would pass. What
        # tells the two apart is what the wrapper says released the resume:
        # the new session, and not a timer or a second `/clear`.
        log = self.logged()
        self.assertIn("the clear took hold (a new session replaced it)", log)
        self.assertNotIn("the clear command left no trace", log)

    def test_a_clear_rebinds_the_watcher_and_unfold_lands_in_the_new_file(self):
        # T02 AC: `/clear` moves the session's identity, and the watcher has to
        # follow it through the registry — not the old growth heuristic — for
        # the unfold phrase to ever reach the file claude is actually writing.
        s = self.session(env=self.env(), cwd=self.work)
        # getcwd() inside the child reports the resolved path (macOS: /var/folders
        # is a symlink to /private/var/folders) -- slug off that, not the raw
        # tempdir string, or this looks for a directory that is never created.
        proj_dir = os.path.join(self.cfg, "projects",
                                re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.work)))
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        before = {f for f in os.listdir(proj_dir) if f.endswith(".jsonl")}
        self.assertEqual(len(before), 1, before)

        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("session rebound", s), self.logged())
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("context restarted", s), self.logged())

        after = {f for f in os.listdir(proj_dir) if f.endswith(".jsonl")}
        new_files = after - before
        self.assertEqual(len(new_files), 1, after)
        with open(os.path.join(proj_dir, next(iter(new_files)))) as fh:
            self.assertIn("picked the handoff up", fh.read())
        with open(os.path.join(proj_dir, next(iter(before)))) as fh:
            self.assertNotIn("picked the handoff up", fh.read())

    def test_the_clear_is_typed_once_however_many_enters_it_takes(self):
        # Two Enters go out for a slash command, because the first may only
        # complete the entry the command list highlighted. The second lands in an
        # empty box when it does not, and an empty box submits nothing.
        s = self.session(env=self.env(), cwd=self.work)
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        # The restart is over, so the wrapper has typed all it is going to; and
        # the fake answers in the order it was typed to, so once it has
        # answered a line of ours, it has answered every Enter that came first.
        self.assertTrue(self.wait_log("context restarted", s), self.logged())
        self.assertTrue(s.roundtrip())
        self.assertEqual(s.buf.count("GOT:/clear"), 1)

    def called_off(self, s):
        """The wrapper has given the fold up, and said so to the session.

        That phrase is what it types INSTEAD of `/clear`, and it is the last
        thing it types: the restart is over the moment it goes out. The fake
        answers in the order it was typed to, so once this has its answer a
        `/clear` sent ahead of it would have had one too — which is the whole
        of what three seconds of watching the screen used to establish.
        """
        self.assertTrue(s.read_until("GOT:The context restart was cancelled", timeout=15),
                        self.logged())

    def test_a_handoff_that_never_arrives_never_clears_anything(self):
        s = self.session(env=self.env(FAKE_HANDOFF="none", CR_HANDOFF_ATTEMPTS="1"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("never written", s), self.logged())
        self.called_off(s)
        self.assertNotIn("GOT:/clear", s.buf)
        self.assertNotIn("clearing the context", self.logged())
        self.assertFalse(os.path.exists(os.path.join(self.work, "handoff.md")))

    def test_a_handoff_that_stops_before_the_marker_never_clears_anything(self):
        s = self.session(env=self.env(FAKE_HANDOFF="nomarker", CR_HANDOFF_ATTEMPTS="1"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("does not end with", s), self.logged())
        self.called_off(s)
        self.assertNotIn("GOT:/clear", s.buf)
        self.assertNotIn("clearing the context", self.logged())

    def test_a_lost_fold_phrase_is_retyped_and_the_restart_still_completes(self):
        # T06: the first attempt is swallowed as if it never reached the
        # session at all -- no file, no transcript row, no GOT reply. Nothing
        # but the wrapper's own no-echo timeout (CR_VERIFY_SEC) can notice that
        # and retype it; the second attempt is the one fake_claude answers.
        #
        # That timeout is the one thing this test sits out in full, so it is
        # the one place it is worth shortening: 3s is still several times the
        # 0.6s a typed phrase takes to be submitted and echoed, which is what
        # keeps the SECOND attempt from being called lost as well.
        s = self.session(env=self.env(FAKE_SCRIPT="dropfirstfold", CR_VERIFY_SEC="3"),
                         cwd=self.work)
        self.assertTrue(self.wait_log("left no trace", s, timeout=20), self.logged())
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("context restarted", s), self.logged())

    def test_a_session_under_the_threshold_is_left_alone(self):
        s = self.session(env=self.env(FAKE_USAGE="200000", FAKE_BANNER=USAGE_DOWN),
                         cwd=self.work)
        # Both copies of the usage row are on file, and the wrapper has read
        # the figure (it prints the threshold the moment it has a model to
        # hold it against). From here a session it meant to fold is LEFT_ALONE
        # away from being typed into.
        self.assertTrue(s.read_until(USAGE_DOWN), s.buf[-500:])
        self.assertTrue(self.wait_log("restarting at 510k", s), self.logged())
        s.drain(LEFT_ALONE)
        self.assertNotIn("folding up", self.logged())
        self.assertNotIn("GOT:handoff", s.buf)
        self.assertNotIn("GOT:/clear", s.buf)

    def test_the_feature_stays_off_until_it_is_switched_on(self):
        # The whole point of the default: nobody upgrading this package should
        # find their session being cleared for them.
        s = self.session(env=self.env(CR_CONTEXT_TOKENS="0", FAKE_BANNER=USAGE_DOWN),
                         cwd=self.work)
        # 700k of a 1M window is on file. Switched on, this is the session the
        # first test in this class folds; LEFT_ALONE later it would have been.
        self.assertTrue(s.read_until(USAGE_DOWN), s.buf[-500:])
        s.drain(LEFT_ALONE)
        self.assertNotIn("GOT:handoff", s.buf)
        self.assertNotIn("context restart armed", self.logged())
        self.assertNotIn("folding up", self.logged())

    def test_the_restart_flag_turns_it_on_at_the_default_percentage(self):
        # No CR_CONTEXT_TOKENS of its own — CR_CONTEXT_RESTART alone has to be
        # enough, and has to survive the round trip through bash's own `:=`
        # defaults (which is where an implementation reading only the Python
        # side would quietly do nothing).
        s = self.session(env=self.env(CR_CONTEXT_TOKENS="", CR_CONTEXT_RESTART="1"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertIn("restarting at 510k", self.logged())   # 51% of a 1M window
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])

    def test_a_per_model_override_is_read_straight_from_the_environment(self):
        s = self.session(env=self.env(CR_CONTEXT_TOKENS="", CR_CONTEXT_RESTART="1",
                                      CR_CLAUDE_TOKENS_CLAUDE_OPUS_5="300000"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertIn("restarting at 300k", self.logged())
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])

    def test_a_permanently_aborted_restart_shows_restart_off_in_the_badge(self):
        # T14: "the clear did nothing" is one of the two paths to a PERMANENT
        # abort (T13's frequency guard is the other) -- the trigger never fires
        # again this session, and that has to stay on screen for as long as the
        # session runs, not just for the one notify() line Claude's own repaint
        # erases within a frame. FAKE_USAGE_AFTER equal to FAKE_USAGE means the
        # context claude reports right after `/clear` never actually fell.
        #
        # The verdict comes CR_VERIFY_SEC after the resume phrase: that is how
        # long the wrapper gives the context to fall before saying it did not.
        # Nothing else in this test is waiting for anything, so that is 3s
        # here, not the 8 the other restarts keep in hand.
        s = self.session(env=self.env(CR_BADGE="1", FAKE_USAGE_AFTER="700000",
                                      CR_VERIFY_SEC="3"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("not attempting another this session", s),
                        self.logged())
        sc = Screen(40, 120)
        deadline = time.time() + 5
        while time.time() < deadline:
            sc = Screen(40, 120)
            sc.feed(s.buf)
            if "restart off" in sc.line(40):
                break
            s.drain(0.05)
        self.assertIn("restart off", sc.line(40))

    def test_an_at_file_mention_resume_phrase_is_delivered_whole(self):
        # T16: "@file" is plain text on claude too (no popup handling needed,
        # T15's finding for codex) -- the whole phrase, mention included, must
        # reach the transcript on the first attempt, same as any other phrase.
        s = self.session(env=self.env(
            CR_RESUME_MSG="@README.md summarize this and continue from `{file}`",
            FAKE_RESUME_MATCH="continue from `"), cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertIn("@README.md summarize this and continue from", s.buf)


class TestClaudesStatusLineProxy(PtyTestCase):
    """T20, end to end: claude's own statusline JSON, relayed through
    `--cr-statusline`, can correct the window the profile table alone would
    have guessed -- and the user's own statusline (chained through the
    proxy) still reaches the screen.
    """

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.log = os.path.join(self.cfg, "log")

    def env(self, **over):
        e = {
            "CLAUDE_CONFIG_DIR": self.cfg,
            "CR_LOG": self.log,
            "CR_SCRAPE": "never",
            "CR_CONTEXT_TOKENS": "102000",        # 102k of a 200k window
            "CR_POLL_SEC": "0.2",
            "CR_USER_IDLE_SEC": "0",
            "FAKE_USAGE": "10",
            "FAKE_USAGE_DELAY": "0.3",
            "FAKE_MODEL": "claude-opus-5",        # a 1M model in the profile table
        }
        e.update(over)
        return e

    def _user_statusline_script(self):
        """A stand-in for a person's own statusLine command: reads the same
        JSON any statusline command gets and prints one recognisable line,
        proof that whatever ran it did not swallow it.
        """
        path = os.path.join(self.cfg, "user_statusline.py")
        with open(path, "w") as fh:
            fh.write(
                "import json, sys\n"
                "d = json.loads(sys.stdin.read() or '{}')\n"
                "w = (d.get('context_window') or {}).get('context_window_size')\n"
                "print('USER-STATUS window=%s' % w)\n")
        return path

    def test_settings_names_the_proxy_once_a_restart_is_armed(self):
        # The fake prints its argv on the "ready" line and the window size on
        # the next: once "winsize" has arrived, the whole of argv has.
        s = self.session(env=self.env(), cwd=self.work)
        self.assertTrue(s.read_until("winsize"))
        self.assertIn('"statusLine"', s.buf)
        self.assertIn("--cr-statusline", s.buf)

    def test_without_a_restart_armed_nothing_is_added(self):
        s = self.session(env=self.env(CR_CONTEXT_TOKENS="0"), cwd=self.work)
        self.assertTrue(s.read_until("winsize"))
        self.assertNotIn("--settings", s.buf)

    def test_the_off_switch_stops_settings_being_added_at_all(self):
        s = self.session(env=self.env(CR_STATUSLINE_PROXY="0"), cwd=self.work)
        self.assertTrue(s.read_until("winsize"))
        self.assertNotIn("--settings", s.buf)

    def test_the_statusline_report_overrides_the_1m_model_table(self):
        # FAKE_MODEL is a 1M model by the profile table; the statusline says
        # this SESSION only actually got 200k (no [1m]) -- exactly the
        # invisible-self-compaction failure T20 exists to close.
        s = self.session(env=self.env(FAKE_STATUSLINE_RUN="1",
                                      FAKE_STATUS_MODEL="claude-opus-5",
                                      FAKE_STATUS_WINDOW="200000"),
                         cwd=self.work)
        self.assertTrue(self.wait_log("context window (claude's status line)", s),
                        self.logged())
        self.assertIn("200k context window", self.logged())
        self.assertIn("restarting at 102k", self.logged())   # 51% of 200k, not 510k

    def test_the_users_own_statusline_still_reaches_the_screen(self):
        d = os.path.join(self.work, ".claude")
        os.makedirs(d, exist_ok=True)
        script = self._user_statusline_script()
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command",
                                      "command": "%s %s" % (sys.executable, script)}}, fh)
        s = self.session(env=self.env(FAKE_STATUSLINE_RUN="1"), cwd=self.work)
        self.assertTrue(s.read_until("STATUSLINE:", timeout=15), s.buf)
        self.assertTrue(s.read_until("USER-STATUS window=200000", timeout=5), s.buf)
        sc = Screen(40, 120)
        sc.feed(s.buf)
        self.assertIn("USER-STATUS window=200000", sc.text())


class TestUniqueHandoffFile(PtyTestCase):
    """T05, single-session end: `{id}` resolves to a real file, and a custom
    phrase missing `{file}` is flagged once at startup."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="cr-cfg-")
        self.work = tempfile.mkdtemp(prefix="cr-work-")
        self.addCleanup(shutil.rmtree, self.cfg, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.log = os.path.join(self.cfg, "log")

    def env(self, **over):
        e = restart_timers(
            CLAUDE_CONFIG_DIR=self.cfg,
            CR_LOG=self.log,
            CR_CONTEXT_TOKENS="510000",
            FAKE_USAGE="700000",
            FAKE_MODEL="claude-opus-5")
        e.update(over)
        return e

    def test_the_id_placeholder_resolves_to_a_real_file_and_still_restarts(self):
        s = self.session(env=self.env(CR_HANDOFF_FILE="scratchpad/RESUME-{id}.md"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_log("context restarted", s), self.logged())
        names = os.listdir(os.path.join(self.work, "scratchpad"))
        self.assertEqual(len(names), 1, names)
        self.assertRegex(names[0], r"^RESUME-[0-9a-f]{8}\.md$")

    def test_a_resume_phrase_without_file_warns_once_at_startup(self):
        s = self.session(env=self.env(CR_RESUME_MSG="Read the notes and continue."),
                         cwd=self.work)
        self.assertTrue(self.wait_log(
            "CR_RESUME_MSG does not contain {file}; a per-session handoff "
            "path cannot be passed to it", s))
        # It is said before claude is started, and startup is the only place
        # it is said: with claude on screen, startup is behind us.
        self.assertTrue(s.read_until("winsize"))
        self.assertEqual(
            self.logged().count("CR_RESUME_MSG does not contain {file}"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
