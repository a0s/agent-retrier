"""Loads the implementation straight out of agent-retrier.sh.

Both the code and the pattern arrays come from the shell script itself, so a test
can never pass against a copy that has drifted from what actually ships.
"""
import atexit
import fcntl
import importlib.util
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(ROOT, "agent-retrier.sh")


def _dump(flag):
    return subprocess.run([WRAP, flag], capture_output=True, text=True, check=True).stdout


def pattern_env():
    """The CR_PAT_* environment exactly as agent-retrier.sh builds it."""
    env = {}
    name = None
    for line in _dump("--cr-dump-patterns").split("\n"):
        if line.startswith("### "):
            name = line[4:].strip()
            env[name] = []
        elif name and line.strip():
            env[name].append(line)
    return {k: "\n".join(v) for k, v in env.items()}


def load(**overrides):
    """Import the embedded Python as a module, with config from the environment.

    Inherited CR_* settings are dropped first: a suite run from inside a wrapped
    session would otherwise be testing the caller's tuning (and its
    AGENT_RETRIER_ACTIVE) rather than the defaults that ship.
    """
    for k in [k for k in os.environ if k.startswith("CR_")]:
        del os.environ[k]
    os.environ.pop("AGENT_RETRIER_ACTIVE", None)
    for k, v in pattern_env().items():
        os.environ[k] = v
    for k, v in overrides.items():
        os.environ[k] = str(v)
    os.environ.setdefault("CR_LOG", os.path.join(tempfile.gettempdir(), "agent-retrier-test.log"))
    # And likewise everything else the implementation keeps under
    # ~/.agent-retrier. A controller built in a unit test is still a
    # controller: one that folds a codex session writes down what the fold
    # cost, and with nothing set that went into the developer's own
    # folds.json — a "fold" of 1000 tokens, dated 1970, for every run of the
    # suite — next to the real ones CR_CODEX_RESERVE_ADAPT learns from.
    for k, v in state_paths(_own_state()).items():
        os.environ.setdefault(k, v)

    src = _dump("--cr-dump-python")
    path = os.path.join(tempfile.mkdtemp(prefix="cr-"), "cr_impl.py")
    with open(path, "w") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location("cr_impl", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cr_impl"] = mod
    spec.loader.exec_module(mod)
    return mod


_STATE = []


def _own_state():
    """This process's stand-in for ~/.agent-retrier, for the implementation as
    `load()` imports it. Made once, gone when the process is."""
    if not _STATE:
        _STATE.append(tempfile.mkdtemp(prefix="cr-state-"))
        atexit.register(shutil.rmtree, _STATE[0], ignore_errors=True)
    return _STATE[0]


def state_paths(state):
    """Every file the wrapper keeps between one run and the next, by the
    setting that says where — all of them moved into `state`."""
    return {
        "CR_UPDATE_CACHE": os.path.join(state, "update.json"),
        "CR_MODEL_CACHE": os.path.join(state, "windows.json"),
        "CR_HANDOFF_REGISTRY_DIR": os.path.join(state, "sessions"),
        "CR_STATUS_DIR": os.path.join(state, "status"),
        "CR_CODEX_FOLDS_FILE": os.path.join(state, "folds.json"),
    }


def hermetic_env(state):
    """What a wrapper under test is started with so that it keeps to itself:
    everything it would otherwise read from, or leave behind in, the developer's
    own `~/.agent-retrier` is pointed into `state` instead.

    Left at their defaults these made every run a guest in somebody's real home.
    `sessions/` is where the LIVE wrappers on the machine claim their handoff
    paths, and a test wrapper both read those claims and pruned among them.
    `folds.json` is the history CR_CODEX_RESERVE_ADAPT learns from, and every
    codex fold in the suite was appended to it as though it had happened.
    `update.json` decides whether a launch stops for CR_UPDATE_NOTICE_SEC to say
    a newer release is out — two seconds on every session, on any machine whose
    checkout is behind — and a stale one sends the first session of the day to
    api.github.com. None of that is the suite's to read or to write — and two
    test processes side by side, which is what `./test/run.sh` now runs, would
    be doing it to each other as well.

    So: no update check and no model lookup (the only two things here that ask
    the network anything), and a directory of its own for the rest. The tests
    that are ABOUT the update notice, or the registry, switch them back on and
    name their own files, exactly as before; this is only the default.

    And no sweep of $TMPDIR. A session removes the stale copies of the
    supervisor that older versions left there, and the $TMPDIR a test wrapper
    inherits is the developer's: whether those go is theirs to decide, not
    something the first test of a run does on the way past. The few places that
    start a wrapper without coming through here set CR_TMP_SWEEP=0 themselves.

    `CR_PYTHON` is the interpreter the suite itself runs under. `python3` on a
    developer's PATH is as often as not a pyenv/asdf shim, and `cr_find_python`
    has to go through it to learn which interpreter it really is — a tenth of
    a second a launch here, a few hundred launches a run, to arrive at the
    answer `sys.executable` already is. The resolution itself still runs, on
    the candidate it is handed; test_degrade.py is where the default candidates
    are exercised, and it does not come through here.
    """
    env = state_paths(state)
    env.update({"CR_UPDATE_CHECK": "0", "CR_MODEL_LOOKUP": "0", "CR_TMP_SWEEP": "0"})
    if sys.executable:
        env["CR_PYTHON"] = sys.executable
    return env


# From the fake's last startup row to the fold, in a session that is going to
# be folded: the transcript quiet for CR_ROOT_IDLE_SEC, one poll to notice it,
# one turn of the supervisor's loop (0.25s) to act on it, and 0.6s for the
# phrase to be typed and answered — a second and a half. LEFT_ALONE is twice
# that, because the tests that wait this long are the ones asserting that
# NOTHING happened, and a window that is too short does not fail: it just
# stops proving anything.
ROOT_IDLE = 0.5
LEFT_ALONE = 3.0


def restart_timers(**over):
    """The clocks every context-restart test runs on, in the one place they are
    set. They were five copies of the same dict, and the copies had drifted.

    Each is as short as it can be while still meaning what it means:

      FAKE_USAGE_DELAY   the fake writes its usage row twice, this far apart,
                         because the first can land before the wrapper has
                         seeded its watcher and be invisible (see fake_claude).
                         Shorter than CR_ROOT_IDLE_SEC, so the fold is decided
                         on a transcript that has gone quiet after BOTH.
      CR_ROOT_IDLE_SEC   how long a transcript has to say nothing before the
                         turn is taken to be over. The line above is its only
                         floor here.
      CR_VERIFY_SEC      how long a typed phrase may go unanswered before it is
                         typed again. NOT shortened: nothing on the ordinary
                         path waits for it, and a `/clear` retyped because a
                         loaded runner was a few seconds late is a second
                         `/clear`. The two tests that do sit this out say so
                         and set their own.
      CR_STEP_GAP_SEC,   pauses for a TUI to settle. The fake is line-oriented
      CR_SLASH_*         and needs none; they only have to stay in order.
      CR_GATE_RETRY_SEC  how long a step held back by a session that is still
                         writing waits before it is looked at again. Every fold
                         is followed by at least one such hold before `/clear`
                         — the handoff is accepted on the very row that makes
                         the session look busy — and at the 5s default every
                         test that reaches `/clear` sat it out in full. Shorter
                         only means the gate is asked more often; what it
                         answers is unchanged, so a test that proves a step is
                         held proves it over more looks, not fewer.
    """
    e = {
        "CR_SCRAPE": "never",
        "CR_HANDOFF_FILE": "handoff.md",
        "CR_HANDOFF_TIMEOUT_SEC": "45",
        "CR_USER_IDLE_SEC": "0",
        "CR_ROOT_IDLE_SEC": str(ROOT_IDLE),
        "CR_POLL_SEC": "0.1",
        "CR_VERIFY_SEC": "8",
        "CR_STEP_GAP_SEC": "0.2",
        "CR_SLASH_GAP_SEC": "0.1",
        "CR_SLASH_ENTER_GAP_SEC": "0.1",
        "CR_GATE_RETRY_SEC": "0.3",
        "FAKE_USAGE_DELAY": "0.3",
    }
    e.update(over)
    return e


def eventually(probe, timeout=5.0, every=0.02):
    """Call `probe` until it answers with something true, and return that.

    For the things a test used to give "long enough" with a `time.sleep`: a row
    the fake is about to write, a pid file, a process on its way out. Sleeping
    for them is slow when the machine is idle and wrong when it is not.
    """
    deadline = time.time() + timeout
    while True:
        got = probe()
        if got or time.time() >= deadline:
            return got
        time.sleep(every)


def roundtrip(session, word="tick", timeout=10):
    """Type a line and wait for the fake to answer it — proof the wrapper has
    been all the way round its loop since the line was sent.

    This is what a test stands on to say something did NOT happen. The
    supervisor is one thread: the turn of the loop that reads our line off its
    stdin is the same turn that confirms a scraped banner, polls the
    transcript, ticks the controller and paints the badge, and it logs what it
    decides before it reads another byte. So an answer to a line sent after a
    deadline means whatever was due by that deadline has been decided and
    written down, however slow the machine is — which a sleep of any length
    only makes likely. And the fake answers lines in the order they were
    typed, so anything the wrapper typed ahead of ours has been answered too.

    It is typing, and the wrapper sees it as typing; the fake records it in
    its transcript like any other prompt. Not for a test whose point is that
    nobody touched the session.
    """
    session.trips = getattr(session, "trips", 0) + 1
    line = "%s-%d" % (word, session.trips)
    session.send(line + "\r")
    return session.read_until("GOT:" + line, timeout=timeout)


def _fake_bin(agent):
    """A single-exec launcher for fake_claude.py/fake_codex.py, named after the
    agent: agent-retrier's own auto-detection (`cr_looks_like_codex`) reads the
    resolved binary's path for a `codex` component, the same way it would for a
    real install, so the launcher has to be named exactly that.
    """
    script = "fake_claude.py" if agent == "claude" else "fake_codex.py"
    path = os.path.join(tempfile.mkdtemp(prefix="cr-fake-%s-" % agent), agent)
    with open(path, "w") as fh:
        fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n'
                 % (sys.executable, os.path.join(ROOT, "test", script)))
    os.chmod(path, 0o755)
    return path


def _is_zombie(pid):
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                              capture_output=True, text=True)
    except OSError:
        return False
    return out.stdout.strip().startswith("Z")


def _descendant_pids(pid):
    """Every live pid under `pid`, however many `setsid()` calls put distance
    between them — `pgrep -P` walks one generation at a time regardless.

    Zombies are excluded: the wrapper's own hand-off to python (`exec ... 3<
    <(printf ...)`, at the end of `agent-retrier.sh`) forks a bash to feed that
    process substitution, which becomes a direct, unreaped child of the
    supervisor the moment it exits — a lower pid than the real agent, since it
    forked first, and `pgrep` lists it right alongside. Left in, it is what
    `agent_pid()` finds instead of the agent (reproduced on Linux, where it
    lingers long enough to be seen; not exercised the same way on macOS).
    """
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    except OSError:
        return []
    children = [int(p) for p in out.stdout.split() if p.strip()]
    live = [c for c in children if not _is_zombie(c)]
    pids = list(live)
    for child in live:
        pids.extend(_descendant_pids(child))
    return pids


class WrapperSession:
    """One agent-retrier.sh instance on its own pty.

    Independent of test_pty.py's `Session`: this one has to run either agent,
    to be one of a pair sharing a project dir and a `CR_LOG`, and to guarantee
    its whole process group dies on close — a pty driver killed with a plain
    SIGTERM/`Popen.kill()` can leave its supervisor (and, in a live-codex run,
    codex itself) running as an orphan that keeps writing into that shared log
    (memory: live-codex-test-orphans). Built for `two_wrappers()`.
    """
    def __init__(self, cwd, log, env):
        env = dict(env or {})
        agent = env.pop("agent", "claude")
        self.agent = agent
        self.master, slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        full = {k: v for k, v in os.environ.items()
                if not k.startswith("CR_") and k not in ("AGENT_RETRIER_ACTIVE",)}
        # Next to the log, because the log is the one thing a pair is already
        # required to share: two wrappers over one project dir have to see each
        # other's handoff claims (T05) the way two real ones in one home would.
        full.update(hermetic_env(os.path.dirname(log)))
        full.update({
            "CR_CLAUDE_BIN": _fake_bin(agent),
            "CR_LOG": log,
            "CR_NOTIFY": "0",
            "CR_BADGE": "0",
            "PYTHONUNBUFFERED": "1",
        })
        full.update(env)
        self.log = log
        self.proc = subprocess.Popen(
            [WRAP], stdin=slave, stdout=slave, stderr=slave,
            env=full, cwd=cwd, close_fds=True, start_new_session=True)
        os.close(slave)
        self.buf = ""

    @property
    def pid(self):
        return self.proc.pid

    def agent_pid(self):
        """The pid of the fake claude/codex under us — what its own
        `sessions/<pid>.json` (T02) is named after, distinct from `.pid`."""
        children = _descendant_pids(self.proc.pid)
        return children[0] if children else None

    def _drain_once(self):
        r, _, _ = select.select([self.master], [], [], 0.1)
        if not r:
            return
        try:
            data = os.read(self.master, 65536)
        except OSError:
            return
        if data:
            self.buf += data.decode("utf-8", "replace")

    def read_until(self, needle, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.buf:
                return True
            self._drain_once()
        return needle in self.buf

    def send(self, text):
        os.write(self.master, text.encode())

    def roundtrip(self, word="tick", timeout=10):
        return roundtrip(self, word, timeout)

    def wait(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                return rc
            self._drain_once()
        self.close()
        raise subprocess.TimeoutExpired(self.proc.args, timeout)

    def log_lines(self):
        """This wrapper's own lines out of the shared `CR_LOG`, by its T01 pid tag."""
        tag = "[cr %d " % self.pid
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln for ln in fh if tag in ln]

    def close(self):
        if self.proc.poll() is None:
            # The wrapper puts the agent it launches in its OWN session
            # (`os.setsid()`, agent-retrier.sh's forkpty-alike, so a signal
            # aimed at us never lands mid-keystroke on it) — which means
            # `killpg` on our pid alone leaves it running. Kill every pid in
            # the tree individually, group by group, each a leader of its own.
            for pid in [self.proc.pid] + _descendant_pids(self.proc.pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            # A killed process holding the pty slave can sit "trying to exit"
            # until the master side is drained (unread output wedges teardown
            # on macOS) — plain `proc.wait()` alone can hang the full timeout.
            deadline = time.time() + 5
            while time.time() < deadline and self.proc.poll() is None:
                self._drain_once()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def two_wrappers(project_dir, cfg_a, cfg_b):
    """Two agent-retrier.sh wrappers sharing one project dir and one `CR_LOG`.

    The bug class this exists for (T02/T04/T05: one session reading another's
    transcript) only reproduces with both wrappers alive over the same
    project-dir at once — a single `Session` (test_pty.py) can never show it.

    `cfg_a`/`cfg_b` are env overrides for each wrapper; `agent` in either
    (default `"claude"`) picks `fake_claude.py` or `fake_codex.py`. Returns
    `(a, b)`, each a `WrapperSession`. Callers should register both for
    cleanup (e.g. `self.addCleanup(a.close)`) so a failed test still cannot
    leave a supervisor running.
    """
    os.makedirs(project_dir, exist_ok=True)
    log, base = _shared_home()
    a_env = dict(base)
    a_env.update(cfg_a or {})
    b_env = dict(base)
    b_env.update(cfg_b or {})
    a = WrapperSession(cwd=project_dir, log=log, env=a_env)
    b = WrapperSession(cwd=project_dir, log=log, env=b_env)
    return a, b


def _shared_home():
    """`(log, env)`: one fresh directory holding the `CR_LOG` and the agents'
    own config dirs, which is what makes two wrappers started from it a pair."""
    shared_dir = tempfile.mkdtemp(prefix="cr-two-")
    return os.path.join(shared_dir, "shared.log"), {
        "CLAUDE_CONFIG_DIR": os.path.join(shared_dir, "claude-config"),
        "CODEX_HOME": os.path.join(shared_dir, "codex-home"),
    }


def one_wrapper(project_dir, cfg):
    """One wrapper, laid out exactly as `two_wrappers()` lays out each of a
    pair — `claude-config` and `codex-home` next to the log — for the tests
    that are about what a single fake does and have no use for a neighbour.

    test_fake_agents.py used to start the pair anyway and throw one away: a
    second supervisor, a second fake and a second teardown per test, to prove
    nothing about either.
    """
    os.makedirs(project_dir, exist_ok=True)
    log, env = _shared_home()
    env.update(cfg or {})
    return WrapperSession(cwd=project_dir, log=log, env=env)
