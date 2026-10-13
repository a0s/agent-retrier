#!/usr/bin/env python3
"""T26 item 4: the T15 checklist as a script — real codex-cli, real quota.

`./test/run.sh --live-codex` runs this. It is never part of the default suite:
it talks to the paid API, so it asks first (an interactive y/N, or
`CR_LIVE_CONFIRM=1` for CI) and refuses to start otherwise.

What it re-verifies, as assertions rather than as a printed transcript (that
was `test/fixtures/capture-codex-input-grammar.py`, the driver that produced
T15's Results table in the first place):

  * `/clear` is accepted by the Enters the shipped recipe sends, leaves a
    ready composer, and creates NO new rollout file of its own;
  * an ordinary phrase after `/clear` goes through and is what finally opens
    the new rollout;
  * `$skill …`, `@file …` and Cyrillic are delivered verbatim as plain text —
    no popup eats the Enter;
  * a bare `/name` codex does not know is dropped inline and never sent (the
    one real hazard T15 found, and the reason T17 expands `{skill:NAME}` to
    `$NAME` on codex).

Two live sessions, a handful of three-word turns on the cheapest model, is
the whole spend.

Differences from the T15 capture driver, both deliberate:

  * codex runs UNDER the wrapper (`agent-retrier.sh --agent codex`), not
    bare, so what is proven is that the supervisor's pty relay leaves every
    one of those recipes intact;
  * the keystrokes come from the shipped `typing_plan()` itself, so this
    cannot pass against a recipe the wrapper no longer sends.

Orphans: a killed pty driver otherwise leaves the supervisor behind, still
writing to the same log (memory `live-codex-test-orphans`). Every session is
registered before it is started and torn down process-group by process-group
from a `finally` and from SIGINT/SIGTERM handlers, and the run ends by
calling `check-orphans.py` whatever else happened.

Exit codes: 0 all checks passed (or codex is not installed — a documented
skip), 1 a check failed, 2 refused for want of confirmation, 3 codex is
installed but not logged in.
"""
import fcntl
import glob
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(ROOT, "agent-retrier.sh")
HERE = os.path.join(ROOT, "test")

OK, FAILED, REFUSED, LOGGED_OUT = 0, 1, 2, 3
ROWS, COLS = 50, 120

# Read before `helper.load()` runs: it deletes every CR_* name from the
# environment (so a suite run inside a wrapped session tests the defaults),
# and CR_LIVE_* would go with them.
MODEL = os.environ.get("CR_LIVE_MODEL") or "gpt-5.6-luna"
CODEX_BIN_OVERRIDE = os.environ.get("CR_LIVE_CODEX_BIN") or ""
CONFIRM_ENV = os.environ.get("CR_LIVE_CONFIRM") or ""
PROJ = os.environ.get("CR_LIVE_PROJ") or "/tmp/codex-live-proj"

NUDGE = ("For this whole session, reply to every message in 3 words or fewer "
         "and never run a shell command or tool call.")
SKILL_PHRASE = "$supervisor continue scratchpad/RESUME.md"
AT_PHRASE = "@README.md summarize this file in 3 words"
CYRILLIC = "Ответь одним словом — привет!"
UNKNOWN_CMD = "/supervisor continue scratchpad/RESUME.md"

CODEX_BIN = ""    # filled in by main(), once the preflight has approved one
LOG = ""          # the CR_LOG both sessions share, under PROJ

_CR = []          # the wrapper's own python, loaded once


def cr():
    if not _CR:
        sys.path.insert(0, HERE)
        from helper import load
        _CR.append(load())
    return _CR[0]


# ----------------------------------------------------------------- the gates

def resolve_codex(env, which=shutil.which):
    """The codex binary this run would drive, or None. An explicit
    CR_LIVE_CODEX_BIN that is not runnable is a configuration mistake, not a
    reason to quietly pick some other codex off PATH (same judgement as the
    wrapper's own CR_CLAUDE_BIN)."""
    override = env.get("CR_LIVE_CODEX_BIN") or ""
    if override:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None
    return which("codex")


def login_ok(binary, run=subprocess.run):
    """`codex login status` — cheap, offline-ish, and the one check that tells
    an expired login apart from a missing install."""
    try:
        done = run([binary, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def confirmed(env, isatty, ask):
    """True only on an explicit yes.

    `CR_LIVE_CONFIRM=1` is the non-interactive escape hatch (CI, a scripted
    run). Otherwise a tty is asked, and anything that is not a "y" is a no. A
    pipe with no CR_LIVE_CONFIRM gets neither: it is refused rather than
    defaulted into spending money nobody agreed to.
    """
    if (env.get("CR_LIVE_CONFIRM") or "").strip().lower() in ("1", "y", "yes", "true"):
        return True
    if not isatty:
        return False
    return (ask() or "").strip().lower() in ("y", "yes")


def prompt_text(model):
    return ("This runs REAL codex-cli against the live API on your account.\n"
            "  model:    %s (cheapest, low reasoning effort)\n"
            "  spend:    2 sessions, ~6 turns of <=3 words each\n"
            "  writes:   a throwaway project dir (%s) and new rollout files\n"
            "Set CR_LIVE_CONFIRM=1 to skip this prompt.\n"
            "Start the live run? [y/N] " % (model, PROJ))


# -------------------------------------------------------------- the sessions

LIVE = []         # every Session ever started, cleaned up whatever happens


def _descendants(pid):
    """Every live pid under `pid` — helper's own walker, zombies excluded."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from helper import _descendant_pids
    return _descendant_pids(pid)


class Session(object):
    """One `agent-retrier.sh --agent codex` on a pty, driving real codex."""

    def __init__(self, label):
        self.label = label
        LIVE.append(self)
        self.master, slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CR_") and k != "AGENT_RETRIER_ACTIVE"}
        env.update({
            "TERM": "xterm-256color",
            "COLUMNS": str(COLS), "LINES": str(ROWS),
            "CR_CLAUDE_BIN": CODEX_BIN,
            "CR_LOG": LOG,
            "CR_BADGE": "0",            # nothing drawn over the screen we read
            "CR_NOTIFY": "0",
            "CR_AGENTS_OVERLAY": "0",
            "CR_UPDATE_CHECK": "0",
            "CR_TMP_SWEEP": "0",        # the developer's $TMPDIR is not ours to tidy
            "CR_CONTEXT_RESTART": "0",  # no fold of its own mid-checklist
            "PYTHONUNBUFFERED": "1",
        })
        argv = [WRAP, "--agent", "codex", "-m", MODEL,
                "-c", 'model_reasoning_effort="low"', "-a", "never", "-s", "read-only"]
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                     env=env, cwd=PROJ, close_fds=True,
                                     start_new_session=True)
        os.close(slave)
        self.buf = bytearray()

    # -- reading ---------------------------------------------------------
    def drain(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            r, _, _ = select.select([self.master], [], [], 0.15)
            if not r:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.buf.extend(data)

    def screen(self):
        scr = cr().Screen(ROWS, COLS)
        scr.feed(bytes(self.buf).decode("utf-8", "replace"))
        return scr.text()

    def wait_for_composer(self, timeout=60):
        """Block until codex's input box is live — never a fixed sleep.

        Two traps, both fatal (the keystrokes land on a menu, it answers "No,
        quit", and the next write dies with EIO): the one-time "Do you trust
        the contents of this directory?" gate is NOT first (the model catalog
        loads ahead of it), and the splash already reads "Ask Codex to do
        anything" while the header still says "model: loading". So this waits
        for the status line codex paints only once the session is live —
        "… · Context N% used" — on the CURRENT render, accepting the trust
        gate on the way (T15's driver learned this the same way).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.drain(0.5)
            if self.proc.poll() is not None:
                raise RuntimeError("%s: codex exited during startup (rc=%s); screen:\n%s"
                                   % (self.label, self.proc.poll(), self.screen()))
            screen = self.screen()
            if "trust the contents" in screen:
                self.write(b"\r")       # "Yes, continue", preselected
                self.drain(1.5)
                continue
            if re.search(r"Context \d+% used", screen):
                return
        raise RuntimeError("%s: composer never became ready in %ss; screen:\n%s"
                           % (self.label, timeout, self.screen()))

    # -- writing ---------------------------------------------------------
    def write(self, data):
        os.write(self.master, data if isinstance(data, bytes) else data.encode())

    def send(self, text, settle=0.0):
        """Type and submit `text` exactly the way the supervisor would.

        The keystrokes are `typing_plan()`'s, straight out of the shipped
        script — the recipe under test, not a copy of it — and the delays are
        offsets from the moment the text itself is typed, so the wait between
        writes doubles as a read of whatever codex drew in between.
        """
        plan = cr().typing_plan("codex", text, cr().CFG)
        t0 = time.time()
        for offset, data in plan:
            gap = offset - (time.time() - t0)
            if gap > 0:
                self.drain(gap)
            self.write(data)
        if settle:
            self.drain(settle)

    # -- teardown --------------------------------------------------------
    def close(self):
        if self.proc.poll() is None:
            # The wrapper puts codex in its OWN session (`os.setsid()`), so a
            # killpg on our pid alone leaves the agent — and whatever it
            # spawned — running. Kill every pid in the tree, each as the
            # leader of its own group.
            for pid in [self.proc.pid] + _descendants(self.proc.pid):
                for kill in (os.killpg, os.kill):
                    try:
                        kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            # A killed process holding the pty slave can sit "trying to exit"
            # until the master side is drained; a plain wait() would hang.
            deadline = time.time() + 5
            while time.time() < deadline and self.proc.poll() is None:
                self.drain(0.2)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def cleanup_all():
    for sess in LIVE:
        try:
            sess.close()
        except Exception as exc:                     # teardown never raises
            print("cleanup: %s: %s" % (sess.label, exc))


# -------------------------------------------------------------- the rollouts

def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def rollouts():
    return set(glob.glob(os.path.join(codex_home(), "sessions", "*", "*", "*", "rollout-*.jsonl")))


def new_rollouts(before):
    return sorted(rollouts() - before, key=os.path.getmtime)


def user_texts(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload") or {}
                if payload.get("type") == "message" and payload.get("role") == "user":
                    for part in payload.get("content") or []:
                        if part.get("type") == "input_text":
                            out.append(part.get("text") or "")
    except OSError:
        pass
    return out


def all_user_texts(paths):
    return [t for p in paths for t in user_texts(p)]


def wait_for_text(before, needle, timeout=40):
    """Poll the rollouts for a user message carrying `needle`.

    Polling, not one look after a fixed sleep: codex flushes the user row when
    the turn starts, and how long that takes is the model's business.
    """
    deadline = time.time() + timeout
    while True:
        files = new_rollouts(before)
        if any(needle in t for t in all_user_texts(files)):
            return True, files
        if time.time() >= deadline:
            return False, files
        time.sleep(1.0)


# --------------------------------------------------------------- the results

def log_lines(pid):
    """One wrapper's own lines out of the shared CR_LOG, by its T01 pid tag."""
    if not os.path.exists(LOG):
        return []
    tag = "[cr %d " % pid
    with open(LOG) as fh:
        return [ln for ln in fh if tag in ln]


RESULTS = []


def check(name, ok, detail=""):
    """One assertion. `detail` is what a failure needs to be diagnosed from
    the printout alone — this is a run nobody will repeat casually — so it is
    printed only when the check fails."""
    RESULTS.append((bool(ok), name, detail))
    print("  %s  %s%s" % ("pass" if ok else "FAIL", name,
                          ("  — %s" % detail) if detail and not ok else ""))
    return bool(ok)


# -------------------------------------------------------------- the scenarios

def scenario_clear_and_resume():
    """T15 rows `/clear` and "plain phrase 3s after /clear"."""
    print("[1/2] /clear and the phrase after it")
    sess = Session("clear")
    sess.wait_for_composer()
    before = rollouts()

    sess.send(NUDGE + " Say ready.")
    got, first = wait_for_text(before, "Say ready.")
    check("a plain phrase reaches codex through the wrapper", got,
          "no user row in %d new rollout(s)" % len(first))
    sess.drain(4)

    after_first = rollouts()
    sess.send("/clear", settle=4)
    screen = sess.screen()
    check("/clear is accepted by the shipped Enters",
          "codex resume" in screen or "Token usage" in screen,
          "no post-clear banner on screen")
    check("/clear itself creates no rollout file",
          not new_rollouts(after_first),
          "unexpected: %s" % [os.path.basename(p) for p in new_rollouts(after_first)])

    sess.drain(3)
    sess.send("Say hello in one word.")
    got, files = wait_for_text(after_first, "Say hello in one word.")
    check("a plain phrase right after /clear gets through", got)
    check("that phrase is what opens the new rollout", got and len(files) >= 1,
          "new rollout files: %d" % len(files))
    sess.close()

    lines = log_lines(sess.proc.pid)              # T01: our own pid's lines only
    check("the wrapper supervised the session it was driving",
          any("(agent: codex)" in ln for ln in lines),
          "%d log line(s) for pid %d" % (len(lines), sess.proc.pid))


def scenario_input_grammar():
    """T15 rows `$skill …`, `@file …`, Cyrillic, and the unrecognized `/name`."""
    print("[2/2] $skill, @file, Cyrillic, and an unknown /command")
    sess = Session("grammar")
    sess.wait_for_composer()
    before = rollouts()

    sess.send(NUDGE + " " + SKILL_PHRASE)
    got, _ = wait_for_text(before, SKILL_PHRASE)
    check("$skill … is delivered verbatim, no popup eats the Enter", got)
    sess.drain(4)

    sess.send(AT_PHRASE)
    got, _ = wait_for_text(before, AT_PHRASE)
    check("@file … is delivered verbatim, no popup eats the Enter", got)
    sess.drain(4)

    sess.send(CYRILLIC)
    got, _ = wait_for_text(before, CYRILLIC)
    check("Cyrillic round-trips (no paste-burst mangling)", got)
    sess.drain(4)

    # Last, on purpose: codex leaves the rejected text sitting in the composer.
    sent_before = len(all_user_texts(new_rollouts(before)))
    files_before = rollouts()
    sess.send(UNKNOWN_CMD, settle=5)
    screen = sess.screen()
    check("an unknown /command is rejected inline",
          "Unrecognized command" in screen, "no rejection on screen")
    check("an unknown /command is never sent as a message",
          len(all_user_texts(new_rollouts(before))) == sent_before
          and not new_rollouts(files_before))
    sess.close()


# --------------------------------------------------------------------- main

def orphan_check():
    print("=== orphan check")
    try:
        return subprocess.run([sys.executable, os.path.join(HERE, "check-orphans.py")]).returncode
    except OSError as exc:
        print("orphan check could not run: %s" % exc)
        return 1


def main():
    global CODEX_BIN, LOG

    # Line buffering, so a live run's progress is on screen as it happens and
    # in the right order relative to check-orphans.py's own output — this is a
    # script someone sits and watches for a couple of minutes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    binary = resolve_codex(os.environ)
    if not binary:
        print("live-codex: no codex binary found (PATH or CR_LIVE_CODEX_BIN) — skipped")
        return OK
    if not login_ok(binary):
        print("live-codex: %s is installed but not logged in — run `codex login` first" % binary)
        return LOGGED_OUT
    if not confirmed(os.environ, sys.stdin.isatty(), lambda: input(prompt_text(MODEL))):
        print("live-codex: refused — no confirmation. Answer y at the prompt, "
              "or set CR_LIVE_CONFIRM=1 for a non-interactive run.")
        return REFUSED

    CODEX_BIN = binary
    os.makedirs(os.path.join(PROJ, "scratchpad"), exist_ok=True)
    readme = os.path.join(PROJ, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w") as fh:
            fh.write("Throwaway project for agent-retrier's live codex checklist.\n")
    cr()                                      # load before the CR_* env is filtered
    LOG = os.path.join(PROJ, "live-codex.log")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(FAILED))   # unwinds into the finally

    print("live-codex: %s, model %s, project %s" % (binary, MODEL, PROJ))
    try:
        scenario_clear_and_resume()
        scenario_input_grammar()
    except Exception as exc:
        check("the run finished without an error", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        cleanup_all()
        orphans = orphan_check()

    failed = [name for ok, name, _ in RESULTS if not ok]
    print("=== live-codex: %d checks, %d failed" % (len(RESULTS), len(failed)))
    for name in failed:
        print("  FAIL %s" % name)
    if failed or orphans or not RESULTS:
        return FAILED
    print("LIVE CODEX PASS")
    return OK


if __name__ == "__main__":
    sys.exit(main())
