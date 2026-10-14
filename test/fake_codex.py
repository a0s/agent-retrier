#!/usr/bin/env python3
"""A stand-in for the codex binary, driven entirely by the environment.

Behaves like a line-oriented TUI on a pty: prints a greeting, echoes what it
is told, and appends JSONL "rollout" records the way real codex does, so the
wrapper can be exercised end to end without burning real quota.

  FAKE_BANNER      text printed once at startup (the on-screen channel)
  FAKE_TRANSCRIPT  written as a usage-limit task_complete error (the structured one)
  FAKE_STALL       written as a transient task_complete error (a server hiccup,
                   not a usage limit)
  FAKE_STALL_INFO  the codex_error_info string for FAKE_STALL (default
                   "server_overloaded")
  FAKE_DELAY       seconds to wait before writing any of the above
  FAKE_EXIT        exit code to use when told to quit
  FAKE_WORKING     print codex's own streaming footer forever (session looks busy)

Typing `answer` writes an ordinary (non-error) task_complete: a turn that was
served, which is how a session says its quota came back. `usage N` serves a
turn that leaves N tokens in the context, and `refuse` a turn the server
refused (FAKE_STALL's wording, or codex's capacity line) — on cue, for the
tests that need a session to change only once its neighbour is watching.

It also plays the other half of a context restart, which needs a session that
answers with numbers rather than words:

  FAKE_USAGE        context the startup token_count records claim
                     (last_token_usage.total_tokens)
  FAKE_USAGE_DELAY   seconds to wait first, and between the two copies of it
  FAKE_WINDOW        model_context_window on every record (default 258400)
  FAKE_SUBAGENT      when set, session_meta claims thread_source "subagent"
                     (used to prove the wrapper ignores other agents' rollouts)
  FAKE_CWD           the cwd written into session_meta (default os.getcwd())
  FAKE_HANDOFF       what to do when asked to fold up: ok | short | nomarker | none
  FAKE_HANDOFF_STOP  how the folding turn ends: complete (task_complete, the
                     default) or aborted (turn_aborted — someone pressed Esc)
  FAKE_TURN_HOLD     seconds the folding turn stays open after the file is
                     written, writing nothing — a root waiting on its agents
  FAKE_USAGE_AFTER   context the record written after the resume phrase claims
  FAKE_MODEL         the model turn_context names (default gpt-5.6-sol)
  FAKE_LOG           when set, every token_count is also written the way codex
                     logs the count its compaction is decided on: a "post sampling
                     token usage" row in $CODEX_HOME/logs_2.sqlite, FAKE_LOG_EXTRA
                     tokens above the rollout's figure (default 18000), under
                     whichever compaction scope the -c arguments asked for
  FAKE_COMPACT       when set, `compact` writes a `compacted` row: codex compacting
                     the thread on its own

Who wrote what (T34). Like codex-cli 0.161, the fake logs a row into
$CODEX_HOME/logs_2.sqlite the moment it creates a thread — at startup and at
every `/clear` — tagged `process_uuid = pid:<its pid>:<uuid>`, which is how the
wrapper tells its own codex's thread from a neighbour's. Thread ids are
UUIDv7, as codex's are.

  FAKE_LOG_PID       "0": an older codex — the log table has no process_uuid
                     column and no thread is ever named in it (default "1")
  FAKE_LAZY_ROLLOUT  when set, a thread's rollout file is not made until
                     something is first written to it — real codex makes it at
                     the thread's first message, not when the thread is
                     created. Without it the file is made at once, as the
                     fake always has (the startup records above need it).

A turn it serves is framed the way codex-cli 0.154 frames one: task_started
(carrying the window), turn_context (carrying the model), the turn's rows, and
task_complete or turn_aborted.

`/clear` (or FAKE_CLEAR_CMD) moves the fake to a whole new rollout file, which
is what a new chat does and what the wrapper's "did the context actually
fall" check reads.
"""
import json
import os
import re
import sys
import time
import uuid

# The phrase the wrapper types is configurable, so the fake recognises it by the
# two things any wording of it must carry: where to write, and what to end with.
_MARKER = re.compile(r"must be exactly (\S+) and nothing else")
_TARGET = re.compile(r"handoff to `([^`]+)`")

SESSION = [None]   # [current rollout file path]
THREAD = [None]    # [current thread id]
ORDINAL = [0]      # resets to 0 whenever a new rollout file is started
PENDING = [None]   # [(path, session_meta, ts)] of a thread whose file is not made yet
PROCESS = "pid:%d:%s" % (os.getpid(), uuid.uuid4())   # codex's process_uuid


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def window():
    return int(os.environ.get("FAKE_WINDOW", "258400"))


def uuid7(at=None):
    """A UUIDv7, the kind codex mints: 48 bits of unix milliseconds first."""
    ms = int((time.time() if at is None else at) * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    n = ((ms << 80) | (0x7 << 76) | (((rand >> 62) & 0xFFF) << 64)
         | (0x2 << 62) | (rand & ((1 << 62) - 1)))
    return str(uuid.UUID(int=n))


def logs_db():
    """$CODEX_HOME/logs_2.sqlite, laid out the way codex-cli 0.161 lays it out
    (or, with FAKE_LOG_PID=0, the way a codex from before process_uuid did)."""
    import sqlite3
    db = sqlite3.connect(os.path.join(codex_home(), "logs_2.sqlite"), timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "ts INTEGER NOT NULL, ts_nanos INTEGER NOT NULL, level TEXT NOT NULL, "
               "target TEXT NOT NULL, feedback_log_body TEXT, thread_id TEXT, %s"
               "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
               % ("process_uuid TEXT, " if names_its_process() else ""))
    return db


def names_its_process():
    return os.environ.get("FAKE_LOG_PID", "1") != "0"


def write_log(body, thread, level="TRACE", target="codex_core"):
    db = logs_db()
    if names_its_process():
        db.execute("INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, thread_id, "
                   "process_uuid) VALUES (?, 0, ?, ?, ?, ?, ?)",
                   (int(time.time()), level, target, body, thread, PROCESS))
    else:
        db.execute("INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, thread_id) "
                   "VALUES (?, 0, ?, ?, ?, ?)", (int(time.time()), level, target, body, thread))
    db.commit()
    db.close()


def log_thread_start(thread):
    """The row codex logs the moment a TUI makes a thread, long before its
    rollout exists (copied in shape from a real logs_2.sqlite, codex 0.161)."""
    if not names_its_process():
        logs_db().close()        # the table is still there, as an older codex left it
        return
    write_log('app_server.request{otel.kind="server" otel.name="thread/start" '
              'rpc.system="jsonrpc" rpc.method="thread/start" rpc.transport="in-process"}: '
              'shell snapshot captured', thread, level="INFO", target="codex_core::shell_snapshot")


def write_record(rtype, payload, ts=None):
    if SESSION[0] is None and PENDING[0] is not None:
        # The thread's first message: codex makes its rollout now.
        path, meta, meta_ts = PENDING[0]
        PENDING[0] = None
        SESSION[0] = path
        write_record("session_meta", meta, ts=meta_ts)
    rec = {"timestamp": ts or now_iso(), "ordinal": ORDINAL[0], "type": rtype, "payload": payload}
    ORDINAL[0] += 1
    with open(SESSION[0], "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def new_rollout(age_days=0):
    """Start a brand new rollout file, the way a fresh codex session does.

    The date directories come from the session start time, UTC. `age_days`
    backdates that directory (and the timestamp in it), for tests exercising
    `codex resume`'s visibility into yesterday's sessions.
    """
    now = time.gmtime(time.time() - age_days * 86400)
    d = os.path.join(codex_home(), "sessions",
                      time.strftime("%Y", now), time.strftime("%m", now), time.strftime("%d", now))
    os.makedirs(d, exist_ok=True)
    sid = uuid7(time.time() - age_days * 86400)
    THREAD[0] = sid
    log_thread_start(sid)
    name = "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S", now), sid)
    SESSION[0] = os.path.join(d, name)
    ORDINAL[0] = 0

    ts = time.strftime("%Y-%m-%dT%H:%M:%S", now) + ".000Z"
    subagent = os.environ.get("FAKE_SUBAGENT")
    payload = {
        "session_id": sid,
        "id": sid,
        "timestamp": ts,
        "cwd": os.environ.get("FAKE_CWD") or os.getcwd(),
        "originator": "codex-tui",
        "cli_version": "0.150.1",
        "source": {"subagent": {"thread_spawn": {}}} if subagent else "vscode",
        "thread_source": "subagent" if subagent else "user",
        "model_provider": "openai",
    }
    if os.environ.get("FAKE_LAZY_ROLLOUT"):
        PENDING[0] = (SESSION[0], payload, ts)
        SESSION[0] = None
        return
    write_record("session_meta", payload, ts=ts)


def write_user_message(text):
    """The echo record: proof a typed line was really submitted."""
    write_record("response_item", {
        "type": "message",
        "id": "msg_%s" % uuid.uuid4(),
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    })


def write_assistant_message(text):
    write_record("response_item", {
        "type": "message",
        "id": "msg_%s" % uuid.uuid4(),
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
        "phase": "final_answer",
    })


def write_usage_log(tokens):
    """One row of codex's own accounting, in its log database."""
    total = int(tokens) + int(os.environ.get("FAKE_LOG_EXTRA", "18000"))
    full = window()
    if "body_after_prefix" in " ".join(sys.argv[1:]):
        scope, limit, scope_name = max(0, total - 5000), "Some(1000000000)", "BodyAfterPrefix"
    else:
        scope, limit, scope_name = total, "Some(%d)" % (full * 100 // 95 * 9 // 10), "Total"
    body = ("session_loop{thread_id=%s}:run_turn: post sampling token usage turn_id=t "
            "total_usage_tokens=%d auto_compact_scope_tokens=%d auto_compact_scope_limit=%s "
            "auto_compact_limit_scope=%s auto_compact_window_prefill_tokens=None "
            "full_context_window_limit=Some(%d) full_context_window_limit_reached=false "
            "token_limit_reached=false model_needs_follow_up=false"
            % (THREAD[0], total, scope, limit, scope_name, full))
    write_log(body, THREAD[0])


def write_token_count(tokens):
    if os.environ.get("FAKE_LOG"):
        write_usage_log(tokens)
    tokens = int(tokens)
    usage = {
        "input_tokens": tokens,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": tokens,
    }
    write_record("event_msg", {
        "type": "token_count",
        "info": {
            "total_token_usage": usage,
            "last_token_usage": usage,
            "model_context_window": window(),
        },
        "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": 4.0, "window_minutes": 10080,
                        "resets_at": int(time.time()) + 604800},
            "secondary": None,
            "rate_limit_reached_type": None,
        },
    })


def write_task_started():
    write_record("event_msg", {
        "type": "task_started",
        "turn_id": str(uuid.uuid4()),
        "started_at": int(time.time()),
        "model_context_window": window(),
        "collaboration_mode_kind": "default",
    })
    write_record("turn_context", {
        "turn_id": str(uuid.uuid4()),
        "cwd": os.environ.get("FAKE_CWD") or os.getcwd(),
        "model": os.environ.get("FAKE_MODEL", "gpt-5.6-sol"),
        "effort": "high",
    })


def write_turn_aborted():
    write_record("event_msg", {"type": "turn_aborted", "turn_id": str(uuid.uuid4()),
                               "reason": "interrupted"})


def write_task_complete(last_agent_message, error=None):
    now = int(time.time())
    payload = {
        "type": "task_complete",
        "turn_id": str(uuid.uuid4()),
        "last_agent_message": last_agent_message,
        "started_at": now,
        "completed_at": now,
        "duration_ms": 1,
    }
    if error is not None:
        payload["error"] = error
    write_record("event_msg", payload)


def fold_up(line):
    """Write (or fail to write) the handoff the wrapper just asked for.

    Mirrors fake_claude.py's fold_up: only the handoff-file part. The rollout
    records for the turn are written by the caller, since codex's turn shape
    differs from claude's.
    """
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


def main():
    out = sys.stdout
    out.write("fake-codex ready argv=%s\r\n" % " ".join(sys.argv[1:]))
    try:
        cols, rows = os.get_terminal_size()   # os.terminal_size is (columns, lines)
        out.write("winsize %dx%d\r\n" % (cols, rows))
    except OSError:
        out.write("winsize unknown\r\n")
    out.flush()

    new_rollout(age_days=int(os.environ.get("FAKE_ROLLOUT_AGE_DAYS", "0")))

    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))

    if os.environ.get("FAKE_TRANSCRIPT"):
        write_task_complete(None, error={
            "message": os.environ["FAKE_TRANSCRIPT"],
            "codex_error_info": "usage_limit_exceeded",
        })
    if os.environ.get("FAKE_STALL"):
        write_task_complete(None, error={
            "message": os.environ["FAKE_STALL"],
            "codex_error_info": os.environ.get("FAKE_STALL_INFO", "server_overloaded"),
        })
    if os.environ.get("FAKE_USAGE"):
        # Real codex never sends a token_count outside a turn — turn_context
        # (the model) always comes first, and a model-keyed threshold (T18's
        # MODEL_PROFILES) cannot answer at all until one has been. Writing it
        # alone, without task_started, announces the model without opening a
        # turn — everything below still behaves as if nothing has happened yet.
        write_record("turn_context", {
            "turn_id": str(uuid.uuid4()),
            "cwd": os.environ.get("FAKE_CWD") or os.getcwd(),
            "model": os.environ.get("FAKE_MODEL", "gpt-5.6-sol"),
            "effort": "high",
        })
        # Twice, after a pause. The wrapper seeds its watcher at the size every
        # rollout file already has, so a resumed session never replays
        # yesterday's numbers — which means a record written before the
        # watcher gets there is invisible. Real codex takes seconds to answer
        # anything and never runs into that; a fake that writes on its first
        # line does, and did.
        gap = float(os.environ.get("FAKE_USAGE_DELAY", "0.6"))
        for _ in range(2):
            time.sleep(gap)
            write_token_count(os.environ["FAKE_USAGE"])
    if os.environ.get("FAKE_BANNER"):
        out.write(os.environ["FAKE_BANNER"] + "\r\n")
        out.flush()

    if os.environ.get("FAKE_WORKING"):
        start = time.time()
        while True:
            out.write("\r• Working (%ds • esc to interrupt)" % int(time.time() - start))
            out.flush()
            time.sleep(0.2)

    # readline(), not `for line in sys.stdin`: iterating a TextIOWrapper reads
    # ahead in blocks, so on a tty it sits on a complete line until the buffer
    # fills. A real TUI is character-driven and never has this problem.
    pending_popup = [None]
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue                          # an empty box: codex ignores it too
        if os.environ.get("FAKE_POPUP") and (line.startswith("$") or line.startswith("/")):
            # `$` and `/` both open a selector popup (T15); the first Enter is
            # spent closing it, not submitting — only a retyped, identical line
            # gets through. Nothing is written for the swallowed attempt.
            if pending_popup[0] != line:
                pending_popup[0] = line
                out.write("POPUP:%s\r\n" % line)
                out.flush()
                continue
            pending_popup[0] = None
        write_user_message(line)
        if line.startswith("quit"):
            break
        if line == "answer":
            write_task_started()
            write_assistant_message("an ordinary answer")
            write_token_count(100)
            write_task_complete("an ordinary answer")
            out.write("GOT:answer\r\n")
            out.flush()
            continue
        m = re.match(r"usage (\d+)$", line)
        if m:
            write_task_started()
            write_assistant_message("an answer")
            write_token_count(m.group(1))
            write_task_complete("an answer")
            out.write("GOT:%s\r\n" % line)
            out.flush()
            continue
        if line == "refuse":
            write_task_started()
            write_task_complete(None, error={
                "message": os.environ.get("FAKE_STALL") or
                "Selected model is at capacity. Please try a different model.",
                "codex_error_info": os.environ.get("FAKE_STALL_INFO", "server_overloaded"),
            })
            out.write("GOT:refuse\r\n")
            out.flush()
            continue
        if line == "compact" and os.environ.get("FAKE_COMPACT"):
            write_record("compacted", {"message": "", "window_number": 2})
            write_token_count(20000)
            out.write("GOT:compact\r\n")
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
        if _MARKER.search(line) or "Write a complete handoff" in line:
            write_task_started()
            fold_up(line)
            write_assistant_message("handoff written")
            if os.environ.get("FAKE_USAGE"):
                # Folding up does not shrink the context — it is still the same
                # full session, which is what the wrapper compares against once
                # the new chat has started.
                write_token_count(os.environ["FAKE_USAGE"])
            out.write("GOT:handoff\r\n")
            out.flush()
            time.sleep(float(os.environ.get("FAKE_TURN_HOLD", "0")))
            if os.environ.get("FAKE_HANDOFF_STOP") == "aborted":
                write_turn_aborted()
            else:
                write_task_complete("handoff written")
            out.write("GOT:turn-closed\r\n")
            out.flush()
            continue
        if line == os.environ.get("FAKE_CLEAR_CMD", "/clear"):
            # codex starts a new session in place, and a new session means a
            # new rollout file. Nothing is written into it until someone speaks.
            new_rollout()
            out.write("GOT:/clear\r\n")
            out.flush()
            continue
        if os.environ.get("FAKE_RESUME_MATCH", "continue from it") in line:
            write_task_started()
            write_assistant_message("picked the handoff up")
            write_token_count(os.environ.get("FAKE_USAGE_AFTER", "5000"))
            write_task_complete("picked the handoff up")
            out.write("GOT:resume\r\n")
            out.flush()
            continue
        out.write("GOT:%s\r\n" % line)
        out.flush()
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
