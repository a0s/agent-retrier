#!/usr/bin/env python3
"""A stand-in for the claude binary, driven entirely by the environment.

Behaves like a line-oriented TUI on a pty: prints a greeting, echoes what it is
told, and can print a usage-limit banner and/or write one into a JSONL
transcript, so the wrapper can be exercised end to end without burning real
quota.

  FAKE_BANNER      text printed once at startup (the on-screen channel)
  FAKE_BANNER_PAINT  1 = paint FAKE_BANNER the way Claude Code paints text in
                   the default colour instead: no space ever written, every
                   gap a cursor move (see `paint_like_claude`)
  FAKE_TRANSCRIPT  banner text written as a rate_limit record (the structured one)
  FAKE_DELAY       seconds to wait before doing either
  FAKE_EXIT        exit code to use when told to quit
  FAKE_WORKING     print a streaming footer forever (session looks busy)

Typing `answer` writes an ordinary (non-limit) assistant record: a turn that was
served, which is how a session says its quota came back.

It also plays the other half of a context restart, which needs a session that
answers with numbers rather than words:

  FAKE_USAGE       context the startup record claims to have been sent with
  FAKE_USAGE_DELAY seconds to wait first, and between the two copies of it
  FAKE_MODEL       model slug on every record (default claude-opus-5)
  FAKE_HANDOFF     what to do when asked to fold up: ok | short | nomarker | none
  FAKE_HANDOFF_STOP  stop_reason for the folding turn (default end_turn)
  FAKE_USAGE_AFTER context the record written after the resume phrase claims

`/clear` moves the fake to a fresh transcript file, which is what Claude Code
does and what the wrapper's "did the context actually fall" check reads.

It also writes `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`, the way Claude Code
>=2.1.273 does, so identity-binding code (T02) has something real to read:

  FAKE_SCRIPT      comma-separated scenario directives (see `run_script`)
  FAKE_SESSION_DELAY  seconds before that file is first written (default 0).
                      Real Claude Code takes a second or two over it — long
                      enough for the wrapper to poll, find nothing under its
                      own pid, and have to decide what that means (T32). The
                      fake is instant unless told, which is why nothing here
                      ever had to decide. Everything after it waits too: a
                      session is named before it writes a row.
  FAKE_SESSION_FILE   0 = never write it at all: a Claude Code older than the
                      file

Two launches are not a session of the fake's own (T33), and it plays both the
way Claude Code 2.1.294 does:

  `attach <id>`     when `$CLAUDE_CONFIG_DIR/jobs/` has exactly one job whose
                    name starts with <id> (the test lays it out, as the daemon
                    would have): the fake is the attach client and that job's
                    background session at once. It writes NO record under its
                    own pid — the client never does — and keeps the job's own
                    up to date instead: `sessions/<pid>.json` with
                    `kind: "bg"`, `jobId`, written under a pid that is nobody's
                    child, and on `/clear` the job's `state.json`
                    (`resumeSessionId`, `linkScanPath`) too. Everything it
                    writes goes into the job's transcript, under the job's
                    cwd. With no such job it is the plain fake, so a test that
                    only looks at argv still can.
  FAKE_BG_RECORD    0 = leave the job's background record alone: a build
                    whose record has no `jobId`, so only `state.json` says
                    which session the job is
  `agents`          the roster: draws its header, writes no record and no
                    transcript of its own, and answers every line it is
                    typed with GOT: — in the real one, Enter in that box
                    dispatches a new session

For T27 (the subagent tree overlay) it can also draw two rows of Claude Code's
own subagent tree and back them with real `subagents/agent-*.{meta.json,jsonl}`
files, reproducing the geometry T27's evidence #5 captured (glyph in column 4,
label from column 6, a frame closed by ESC[?2026l):

  FAKE_AGENT_TREE  1 = draw the tree once at startup
                    the frame is redrawn (with an ESC[K that wipes whatever the
                    wrapper painted over it, the way claude's own repaint does)
                    whenever the line `repaint-tree` is typed

For T29 (the OTHER subagent view, opened by typing /tasks while agents are
still running -- no glyph, a "(state)" marker instead) it can draw that one
too, reproducing test/fixtures/agents-panel-2.1.273.bin:

  FAKE_AGENTS_PANEL  1 = draw the panel once at startup; `repaint-panel`
                    redraws it (state flips to "done", the same ESC[K erase
                    the real repaint does)

For T20 (claude's own statusline, proxied through `--cr-statusline`) it can
also play claude's half of that: if launched with a `--settings` naming a
statusLine command (exactly what `claude_launch_args` adds), it runs that
command the way Claude Code itself would -- once, stdin fed a payload built
from the knobs below, stdout printed straight onto the screen so a test can
see whatever the proxy chained through to the user's own statusline:

  FAKE_STATUSLINE_RUN     1 = actually run it (never on by default: most
                          tests neither need nor want the extra process)
  FAKE_STATUS_MODEL       model.id in the payload (default claude-sonnet-5)
  FAKE_STATUS_WINDOW      context_window.context_window_size (default 200000)
  FAKE_STATUSLINE_DELAY   seconds to wait first (default 0.5): the wrapper
                          seeds its own pollers before this runs, the same
                          reasoning as FAKE_USAGE_DELAY above

For T35 (the session budget) a line typed as `statusline <percent>` runs the
same command once more, with claude's `rate_limits` in the payload: the
five-hour window's used percentage, at a reset FAKE_STATUS_RESETS_IN seconds
after the fake started (default 18000), and a seven-day one.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

# The phrase the wrapper types is configurable, so the fake recognises it by the
# two things any wording of it must carry: where to write, and what to end with.
_MARKER = re.compile(r"must be exactly (\S+) and nothing else")
_TARGET = re.compile(r"handoff to `([^`]+)`")

SESSION = ["fake-session"]
RESETS_AT = [0]      # the five-hour window's reset, as the statusline states it (T35)
STARTED_AT = [None]
DROP_NEXT_FOLD = [False]
CWD = [None]          # whose project the transcript is under: ours, or an attached job's
ATTACHED = [None]     # the job `attach` named (T33)
ROSTER = [False]      # `agents`: no session of its own at all (T33)


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def project_dir():
    slug = re.sub(r"[^A-Za-z0-9]", "-", CWD[0] or os.getcwd())
    d = os.path.join(config_dir(), "projects", slug)
    os.makedirs(d, exist_ok=True)
    return d


def job_dir(job):
    return os.path.join(config_dir(), "jobs", job)


def read_job_state(job):
    with open(os.path.join(job_dir(job), "state.json")) as fh:
        return json.load(fh)


def write_job_state(job, state):
    path = os.path.join(job_dir(job), "state.json")
    with open(path + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(path + ".tmp", path)


def attach_to(argv):
    """`attach <id>` -> take on the job's session, or stay the plain fake.

    The id is resolved the way Claude Code resolves it: the one job whose
    directory name starts with it."""
    if len(argv) < 2 or argv[0] != "attach":
        return
    try:
        jobs = [n for n in os.listdir(os.path.join(config_dir(), "jobs"))
                if re.match(r"^[0-9a-f]{8}$", n) and n.startswith(argv[1])]
    except OSError:
        return
    if len(jobs) != 1:
        return
    state = read_job_state(jobs[0])
    ATTACHED[0] = jobs[0]
    SESSION[0] = state.get("resumeSessionId") or state["sessionId"]
    CWD[0] = state.get("cwd") or os.getcwd()


def bg_record_path():
    """The attached job's own record: the one already there if the test laid
    one out, or one under a pid nobody started — the daemon's worker is no
    child of ours either."""
    sessions = sessions_dir()
    for name in os.listdir(sessions):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions, name)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("kind") == "bg" and rec.get("jobId") == ATTACHED[0]:
            return os.path.join(sessions, name)
    return os.path.join(sessions, "%d.json" % (4000000 + os.getpid()))


def write_bg_record(status):
    if os.environ.get("FAKE_BG_RECORD", "1") == "0":
        return
    path = bg_record_path()
    rec = {"pid": int(os.path.basename(path)[:-5]), "sessionId": SESSION[0], "cwd": CWD[0],
           "startedAt": STARTED_AT[0], "version": "1.0.0-fake", "kind": "bg",
           "jobId": ATTACHED[0], "name": ATTACHED[0], "status": status,
           "statusUpdatedAt": int(time.time() * 1000)}
    try:
        with open(path) as fh:
            old = json.load(fh)
        rec["pid"], rec["startedAt"] = old.get("pid", rec["pid"]), old.get("startedAt", rec["startedAt"])
    except (OSError, ValueError):
        pass
    with open(path + ".tmp", "w") as fh:
        json.dump(rec, fh)
    os.replace(path + ".tmp", path)


def draw_roster(out):
    """The two things `CR_ROSTER_PATTERNS` know a roster by, and nothing
    that is anybody's session."""
    out.write("1 awaiting input · 2 working\r\n")
    out.write("> describe a task for a new session\r\n")
    out.flush()


def transcript_path():
    return os.path.join(project_dir(), SESSION[0] + ".jsonl")


def sessions_dir():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    d = os.path.join(cfg, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def pid_file_path():
    return os.path.join(sessions_dir(), "%d.json" % os.getpid())


def write_pid_file(status):
    """The identity record TranscriptWatcher (T02) binds on: pid -> sessionId.

    Field shapes copied from a real `sessions/<pid>.json` written by Claude
    Code 2.1.273 (see T02's "Verified" section): `startedAt`/`statusUpdatedAt`
    are epoch milliseconds, not ISO strings.
    """
    if ROSTER[0]:
        return                       # the roster is nobody's session (T33)
    if ATTACHED[0]:
        write_bg_record(status)      # ...and the attach client is not one either
        return
    if os.environ.get("FAKE_SESSION_FILE", "1") == "0":
        return
    rec = {"pid": os.getpid(), "sessionId": SESSION[0], "cwd": os.getcwd(),
           "startedAt": STARTED_AT[0], "version": "1.0.0-fake",
           "kind": "interactive", "status": status,
           "statusUpdatedAt": int(time.time() * 1000)}
    tmp = pid_file_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
    os.replace(tmp, pid_file_path())


def remove_pid_file():
    if ATTACHED[0]:
        return        # detaching leaves the session running: "The session keeps running either way"
    try:
        os.unlink(pid_file_path())
    except OSError:
        pass


def write_transcript(text, limited=True, tokens=None, stop_reason=None, model=None, synthetic=False):
    if ROSTER[0]:
        return                       # the roster has no transcript to write into
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    msg_model = "<synthetic>" if synthetic else (model or os.environ.get("FAKE_MODEL", "claude-opus-5"))
    if msg_model:
        msg["model"] = msg_model
    if stop_reason:
        msg["stop_reason"] = stop_reason
    if synthetic:
        msg["usage"] = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": 0}
    elif tokens is not None:
        # Split across the three counters the wrapper adds up, so the test
        # exercises the sum rather than a single field.
        msg["usage"] = {"input_tokens": 2,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": max(0, int(tokens) - 1002),
                        "output_tokens": 4000}
    rec = {"type": "assistant", "timestamp": "2026-07-19T19:22:08.730Z",
           "isSidechain": False, "message": msg}
    if limited:
        rec.update({"error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429})
    with open(transcript_path(), "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_user_message(text):
    """The echo record: proof a typed line was really submitted.

    Real Claude Code writes the user's own prompt into the transcript as a
    `type: "user"` row before the assistant answers it; that row is what the
    wrapper's echo detection (transcript_limit_records' `echoish`) reads, and
    what proves a phrase it typed (a retry, the resume phrase, or -- T06 -- the
    fold phrase) actually reached the session rather than being eaten by a
    popup or lost keystrokes.
    """
    rec = {"type": "user", "timestamp": "2026-07-19T19:22:08.730Z",
           "isSidechain": False,
           "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    with open(transcript_path(), "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_streaming_turn(tokens, final_stop_reason="end_turn"):
    """A turn as it looks mid-stream: a delta row with no stop_reason yet,
    then the row that closes it (T11 row-collapse)."""
    write_transcript("partial", limited=False, tokens=tokens, stop_reason=None)
    write_transcript("partial and final", limited=False, tokens=tokens,
                     stop_reason=final_stop_reason)


def _grow_loop(step, period):
    total = int(step)
    while True:
        time.sleep(period)
        write_transcript("growing", limited=False, tokens=total, stop_reason="end_turn")
        total += int(step)


def run_script(spec):
    """Apply `FAKE_SCRIPT` directives, comma-separated `name` or `name=value`.

      grow=N:M          append a full-looking turn worth N tokens every M
                         seconds, forever (a session that keeps climbing)
      dropfirstfold      swallow the first handoff request entirely: no file,
                         no transcript row, no GOT reply (the lost-message case)
      synthetic          write a `<synthetic>`, all-zero-usage row at startup
      delayclear=K       stall K seconds before acking FAKE_CLEAR_CMD/`/clear`
      stream=N           write a streaming turn at startup: a delta row with
                         no stop_reason, then the row that closes it at N tokens
    """
    if not spec:
        return
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        if name == "grow":
            step, _, period = value.partition(":")
            t = threading.Thread(target=_grow_loop, args=(step or "1000", float(period or "1")), daemon=True)
            t.start()
        elif name == "dropfirstfold":
            DROP_NEXT_FOLD[0] = True
        elif name == "synthetic":
            write_transcript("<synthetic>", limited=False, synthetic=True, stop_reason="stop_sequence")
        elif name == "delayclear":
            os.environ["FAKE_CLEAR_DELAY"] = value or "1"
        elif name == "stream":
            write_streaming_turn(int(value or "10000"))


AGENT_TREE_ROW1 = 10   # rows chosen well clear of both the badge (bottom row)
AGENT_TREE_ROW2 = 11   # and the terminal top, on any size test_pty.py uses.
AGENT_TREE_ROW3 = 12
AGENT_A, AGENT_B = "aaa111", "bbb222"
AGENT_A_LABEL, AGENT_B_LABEL = "Report first file in cwd", "Report second file in cwd"
AGENT_A_MODEL, AGENT_B_MODEL = "claude-sonnet-5", "claude-haiku-4-5-20251001"

# T29: the OTHER subagent view, opened by typing /tasks while agents are
# still running -- a different row shape from the tree above (no glyph, a
# "(state)" marker instead), reproducing test/fixtures/agents-panel-2.1.273.bin.
# Rows picked well clear of the tree rows above, the badge, and the top.
PANEL_ROW_HEADER = 30
PANEL_ROW1 = 31
PANEL_ROW2 = 32


def subagents_dir():
    d = os.path.join(project_dir(), SESSION[0], "subagents")
    os.makedirs(d, exist_ok=True)
    return d


def write_subagent(agent_id, description, model):
    d = subagents_dir()
    meta = {"agentType": "general-purpose", "description": description,
            "toolUseId": "toolu_fake_%s" % agent_id, "spawnDepth": 1,
            # T27 evidence: a fork spawned with this field has been seen to run
            # on a different model entirely — writing one here is deliberate,
            # so a test can catch the overlay if it ever starts trusting it.
            "requestedModel": "sonnet"}
    with open(os.path.join(d, "agent-%s.meta.json" % agent_id), "w") as fh:
        json.dump(meta, fh)
    rec = {"type": "assistant", "isSidechain": False, "message": {"model": model, "content": []}}
    with open(os.path.join(d, "agent-%s.jsonl" % agent_id), "w") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def draw_agent_tree(out, suffix="0 tool uses"):
    frame = (
        "\x1b[?2026h"
        "\x1b[%d;4H├\x1b[%d;6GReport first file in cwd · %s\x1b[K"
        "\x1b[%d;4H│\x1b[%d;6G⎿  Initializing…\x1b[K"
        "\x1b[%d;4H└\x1b[%d;6GReport second file in cwd · %s\x1b[K"
        "\x1b[?2026l"
    ) % (AGENT_TREE_ROW1, AGENT_TREE_ROW1, suffix,
         AGENT_TREE_ROW2, AGENT_TREE_ROW2,
         AGENT_TREE_ROW3, AGENT_TREE_ROW3, suffix)
    out.write(frame)
    out.flush()


def draw_agents_panel(out, state="running"):
    # CUP (row;colH) for every field, never CHA (colG) with a row number
    # smuggled into its params -- CHA takes exactly one, the column, and
    # leaves the row wherever it already was.
    frame = (
        "\x1b[?2026h"
        "\x1b[%d;4HLocal agents (2)\x1b[K"
        "\x1b[%d;6H%s (%s)\x1b[K"
        "\x1b[%d;4H❯\x1b[%d;6H%s (%s)\x1b[K"
        "\x1b[?2026l"
    ) % (PANEL_ROW_HEADER,
         PANEL_ROW1, AGENT_A_LABEL, state,
         PANEL_ROW2, PANEL_ROW2, AGENT_B_LABEL, state)
    out.write(frame)
    out.flush()


def paint_like_claude(text):
    """`text` as Claude Code >=2.1.22x draws default-coloured text on cells
    that were blank.

    Its renderer writes only the cells that differ from what is already on
    screen, comparing character and style, and an unstyled space IS a blank
    cell — so not one space is written. A word is reached with CHA
    (ESC[<col>G), the next row with "\\r ESC[1B", all inside a synchronized-
    update frame. That is the shape of every default-coloured row in
    test/fixtures/*-2.1.273.bin and docs/demo/claude-restart.cast:
    "⏺⎋[3GA⎋[5Gpty⎋[9G(pseudo-terminal)⎋[27Gis…". A row's first word, if it
    starts in column 1, goes out with no move in front of it, as the real
    ones do after their "\\r".

    Rows are split on "\\r\\n". The cursor is left at the start of a fresh row
    below, so whatever is printed next does not land on top of it.
    """
    out = ["\x1b[?2026h"]
    for n, row in enumerate(text.split("\r\n")):
        out.append("\r\x1b[1B" if n else "\r")
        for m in re.finditer(r"\S+", row):
            out.append(("\x1b[%dG" % (m.start() + 1) if m.start() else "") + m.group(0))
    out.append("\x1b[?2026l\r\n")
    return "".join(out)


def maybe_run_statusline():
    """T20: if `--settings` (as `claude_launch_args` builds it) named a
    statusLine command, run it the way Claude Code itself would -- stdin fed
    the JSON payload FAKE_STATUS_MODEL/FAKE_STATUS_WINDOW describe, stdout
    printed straight onto the screen. Off unless FAKE_STATUSLINE_RUN asks for
    it: most tests have no use for the extra process.
    """
    if not os.environ.get("FAKE_STATUSLINE_RUN"):
        return
    time.sleep(float(os.environ.get("FAKE_STATUSLINE_DELAY", "0.5")))
    run_statusline()


def run_statusline(percent=None):
    """One refresh of claude's statusline: the statusLine command named by
    `--settings`, run once, stdin fed the payload the FAKE_STATUS_* knobs
    describe, stdout printed onto the screen. With `percent` (T35) the payload
    also carries claude's `rate_limits`: the five-hour window's used percentage
    at a fixed `resets_at` (FAKE_STATUS_RESETS_IN seconds after this process
    started), and a seven-day one."""
    settings = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--settings" and i + 1 < len(argv):
            settings = argv[i + 1]
        elif a.startswith("--settings="):
            settings = a[len("--settings="):]
    if not settings:
        return
    try:
        data = json.loads(settings)
    except ValueError:
        return
    status_line = (data.get("statusLine") or {}) if isinstance(data, dict) else {}
    command = status_line.get("command")
    if not command or status_line.get("type", "command") != "command":
        return
    payload = {
        "model": {"id": os.environ.get("FAKE_STATUS_MODEL", "claude-sonnet-5")},
        "context_window": {
            "context_window_size": int(os.environ.get("FAKE_STATUS_WINDOW", "200000"))},
        "session_id": SESSION[0],
        "transcript_path": transcript_path(),
        "workspace": {"current_dir": os.getcwd(), "project_dir": os.getcwd()},
    }
    if percent is not None:
        payload["rate_limits"] = {
            "five_hour": {"used_percentage": percent, "resets_at": RESETS_AT[0]},
            "seven_day": {"used_percentage": 40.1, "resets_at": RESETS_AT[0] + 86400}}
    try:
        result = subprocess.run(command, shell=True, input=json.dumps(payload),
                                text=True, capture_output=True, timeout=10)
    except Exception as exc:
        sys.stdout.write("statusline-proxy-error: %s\r\n" % exc)
        sys.stdout.flush()
        return
    sys.stdout.write("STATUSLINE:%s\r\n" % result.stdout.replace("\n", "\\n"))
    sys.stdout.flush()


def fold_up(line):
    """Write (or fail to write) the handoff the wrapper just asked for."""
    mode = os.environ.get("FAKE_HANDOFF", "ok")
    m, t = _MARKER.search(line), _TARGET.search(line)
    marker = m.group(1) if m else "HANDOFF"
    target = t.group(1) if t else ".agent-retrier/handoff.md"
    body = "".join("state: step %d is done\n" % i for i in range(20))
    text = {"ok": body + marker + "\n",
            "short": marker + "\n",           # under the byte floor
            "nomarker": body,                 # complete-looking, no proof it is
            "none": None}.get(mode, body + marker + "\n")
    if text is not None:
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(target, "w") as fh:
            fh.write(text)
    write_transcript("handoff written", limited=False,
                     tokens=int(os.environ.get("FAKE_USAGE", "0") or 0) or None,
                     stop_reason=os.environ.get("FAKE_HANDOFF_STOP", "end_turn"))


def main():
    out = sys.stdout
    STARTED_AT[0] = int(time.time() * 1000)
    SESSION[0] = "fake-session-%d" % os.getpid()  # distinct per process, like a real sessionId
    RESETS_AT[0] = int(time.time()) + int(os.environ.get("FAKE_STATUS_RESETS_IN", "18000"))
    out.write("fake-claude ready argv=%s\r\n" % " ".join(sys.argv[1:]))
    try:
        cols, rows = os.get_terminal_size()   # os.terminal_size is (columns, lines)
        out.write("winsize %dx%d\r\n" % (cols, rows))
    except OSError:
        out.write("winsize unknown\r\n")
    out.flush()

    # `--settings` takes a value (the statusline proxy's JSON, T20), and a
    # roster started with one in front of it is still the roster.
    positional, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
        elif a == "--settings":
            skip = True
        elif not a.startswith("-"):
            positional.append(a)
    ROSTER[0] = bool(positional) and positional[0] == "agents"
    attach_to(positional)
    if ROSTER[0]:
        draw_roster(out)

    time.sleep(float(os.environ.get("FAKE_SESSION_DELAY", "0")))
    write_pid_file("idle")
    run_script(os.environ.get("FAKE_SCRIPT"))
    threading.Thread(target=maybe_run_statusline, daemon=True).start()

    if os.environ.get("FAKE_AGENT_TREE"):
        # A pause first: the wrapper seeds its transcript watcher at whatever
        # size the file already has (so `--continue` never replays yesterday's
        # banner) — writing before that seed happens would be invisible to it,
        # same reasoning as FAKE_USAGE_DELAY below. Then a second pause after
        # the transcript starts growing, before the tree is drawn: the overlay
        # can only resolve a row's model once the wrapper's watcher has bound
        # to THIS session's transcript (T27's subagents live next to it), and
        # a real session's transcript is always growing well before it spawns
        # subagents.
        time.sleep(float(os.environ.get("FAKE_AGENT_TREE_DELAY", "0.3")))
        write_transcript("agent tree started", limited=False)
        time.sleep(float(os.environ.get("FAKE_AGENT_TREE_SETTLE_SEC", "0.5")))
        write_subagent(AGENT_A, AGENT_A_LABEL, AGENT_A_MODEL)
        write_subagent(AGENT_B, AGENT_B_LABEL, AGENT_B_MODEL)
        draw_agent_tree(out)

    if os.environ.get("FAKE_AGENTS_PANEL"):
        # Same seeding reasoning as FAKE_AGENT_TREE above -- the overlay
        # cannot resolve a row until the watcher is bound to this session.
        time.sleep(float(os.environ.get("FAKE_AGENTS_PANEL_DELAY", "0.3")))
        write_transcript("agents panel started", limited=False)
        time.sleep(float(os.environ.get("FAKE_AGENTS_PANEL_SETTLE_SEC", "0.5")))
        write_subagent(AGENT_A, AGENT_A_LABEL, AGENT_A_MODEL)
        write_subagent(AGENT_B, AGENT_B_LABEL, AGENT_B_MODEL)
        draw_agents_panel(out)

    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))

    if os.environ.get("FAKE_TRANSCRIPT"):
        write_transcript(os.environ["FAKE_TRANSCRIPT"])
    if os.environ.get("FAKE_TRANSCRIPT_PLAIN"):
        write_transcript(os.environ["FAKE_TRANSCRIPT_PLAIN"], limited=False)
    if os.environ.get("FAKE_USAGE"):
        # Twice, after a pause. The wrapper seeds its watcher at the size every
        # transcript already has, so that `claude --continue` never replays
        # yesterday's banner — which means a record written before it gets there
        # is invisible. Real claude takes seconds to answer anything and never
        # runs into that; a fake that writes on its first line does, and did.
        gap = float(os.environ.get("FAKE_USAGE_DELAY", "0.6"))
        for _ in range(2):
            time.sleep(gap)
            write_transcript("a full-looking turn", limited=False,
                             tokens=int(os.environ["FAKE_USAGE"]), stop_reason="end_turn")
    if os.environ.get("FAKE_BANNER"):
        if os.environ.get("FAKE_BANNER_PAINT") == "1":
            out.write(paint_like_claude(os.environ["FAKE_BANNER"]))
        else:
            out.write(os.environ["FAKE_BANNER"] + "\r\n")
        out.flush()

    if os.environ.get("FAKE_WORKING"):
        while True:
            out.write("\r* Cogitating... (esc to interrupt)")
            out.flush()
            time.sleep(0.2)

    # readline(), not `for line in sys.stdin`: iterating a TextIOWrapper reads
    # ahead in blocks, so on a tty it sits on a complete line until the buffer
    # fills. A real TUI is character-driven and never has this problem.
    try:
        while True:
            write_pid_file("idle")             # waiting for input
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue                      # an empty box: Claude Code ignores it too
            write_pid_file("busy")
            if ROSTER[0]:
                # No session here to write it into: in the real roster, Enter
                # in that box dispatches a brand new session with this text.
                if line.startswith("quit"):
                    break
                out.write("GOT:%s\r\n" % line)
                out.flush()
                continue
            # The one line that may not get an echo at all: `dropfirstfold`
            # (DROP_NEXT_FOLD) stands in for a popup eating the keystrokes or a
            # write that never landed -- nothing reaches the session, so
            # nothing is written for it, echo included (T06).
            is_fold = bool(_MARKER.search(line) or "Write a complete handoff" in line)
            if not (is_fold and DROP_NEXT_FOLD[0]):
                write_user_message(line)
            if line.startswith("quit"):
                break
            if line == "answer":
                write_transcript("an ordinary answer", limited=False)
                out.write("GOT:answer\r\n")
                out.flush()
                continue
            if line.startswith("statusline "):
                # T35: one statusline refresh, as Claude Code draws one after a
                # turn, with the five-hour percentage the line names.
                run_statusline(float(line.split()[1]))
                out.write("GOT:statusline\r\n")
                out.flush()
                continue
            if line == "winsize":
                try:
                    cols, rows = os.get_terminal_size()   # os.terminal_size is (columns, lines)
                    out.write("winsize %dx%d\r\n" % (cols, rows))
                except OSError:
                    out.write("winsize unknown\r\n")
                out.flush()
                continue
            if line == "repaint-tree":
                # A repaint the way claude's own TUI does one: new content,
                # ESC[K wiping the row from wherever the label's cursor ends up
                # — which wipes any overlay annotation that was sitting to the
                # right of it, the exact flicker T27's per-frame repaint exists
                # to fix.
                draw_agent_tree(out, suffix="1 tool use")
                out.write("GOT:repaint-tree\r\n")
                out.flush()
                continue
            if line == "repaint-panel":
                draw_agents_panel(out, state="done")
                out.write("GOT:repaint-panel\r\n")
                out.flush()
                continue
            if is_fold:
                if DROP_NEXT_FOLD[0]:
                    # The lost-message case: the fold request is swallowed as if
                    # it never reached the session at all.
                    DROP_NEXT_FOLD[0] = False
                    continue
                fold_up(line)
                out.write("GOT:handoff\r\n")
                out.flush()
                continue
            if line == os.environ.get("FAKE_CLEAR_CMD", "/clear"):
                # Claude Code starts a new session in place, and a new session means
                # a new transcript. Nothing is written into it until someone speaks.
                time.sleep(float(os.environ.get("FAKE_CLEAR_DELAY", "0")))
                SESSION[0] = "fake-session-%d" % (int(time.time() * 1000) % 100000)
                write_pid_file("busy")         # sessionId changed under the same pid
                if ATTACHED[0]:
                    # A background session also tells its job where it went,
                    # which is what the daemon resumes it from next time.
                    state = read_job_state(ATTACHED[0])
                    state.update(resumeSessionId=SESSION[0], linkScanPath=transcript_path())
                    write_job_state(ATTACHED[0], state)
                out.write("GOT:/clear\r\n")
                out.flush()
                continue
            if os.environ.get("FAKE_RESUME_MATCH", "continue from it") in line:
                write_transcript("picked the handoff up", limited=False,
                                 tokens=int(os.environ.get("FAKE_USAGE_AFTER", "5000")),
                                 stop_reason="end_turn")
                out.write("GOT:resume\r\n")
                out.flush()
                continue
            out.write("GOT:%s\r\n" % line)
            out.flush()
        return int(os.environ.get("FAKE_EXIT", "0"))
    finally:
        remove_pid_file()


if __name__ == "__main__":
    sys.exit(main())
