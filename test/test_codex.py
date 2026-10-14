"""codex: the second agent, and the second reason a session stops.

Two things are new here and both come from the same file. codex writes a rollout
JSONL that states outright what Claude Code's transcript only implies — the turn
boundaries, the size of the context window, and the reason a turn ended — so the
first half of this suite is about reading it, and about not reading the rollouts
that belong to other people's sessions.

The second half is the stall: a turn refused by the server rather than by the
account. Nothing about it says when to come back, which is exactly what makes it
a different machine from the limit — the wait is one we pick, and it doubles for
as long as the refusals keep coming.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from helper import LEFT_ALONE, hermetic_env, load, restart_timers
from test_pty import PtyTestCase, Session, USAGE_DOWN

cr = load()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPACITY = "Selected model is at capacity. Please try a different model."
OUT_OF_QUOTA = ("You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
                "to purchase more credits or try again at Aug 20th, 2026 10:00 AM.")


def rollout(dirname, name="rollout-2026-08-28T10-00-00-abc.jsonl", cwd=None,
            subagent=False, rows=()):
    """Write a rollout file with a session_meta head and `rows` after it."""
    os.makedirs(dirname, exist_ok=True)
    path = os.path.join(dirname, name)
    meta = {"session_id": "abc", "cwd": cwd or os.getcwd(), "originator": "codex-tui",
            "cli_version": "0.150.1",
            "source": {"subagent": {"thread_spawn": {}}} if subagent else "vscode",
            "thread_source": "subagent" if subagent else "user"}
    with open(path, "w") as fh:
        fh.write(json.dumps({"timestamp": "2026-08-28T08:00:00.000Z", "ordinal": 0,
                             "type": "session_meta", "payload": meta}) + "\n")
        for i, (rtype, payload) in enumerate(rows):
            fh.write(json.dumps({"timestamp": "2026-08-28T08:00:0%d.000Z" % (i + 1),
                                 "ordinal": i + 1, "type": rtype,
                                 "payload": payload}) + "\n")
    return path


def task_complete(error=None, last="pong"):
    p = {"type": "task_complete", "turn_id": "t1", "last_agent_message": last,
         "started_at": 1, "completed_at": 2, "duration_ms": 1000}
    if error:
        p["error"] = error
    return ("event_msg", p)


def token_count(tokens, window=258400):
    usage = {"input_tokens": tokens, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": 0,
             "reasoning_output_tokens": 0, "total_tokens": tokens}
    return ("event_msg", {"type": "token_count",
                          "info": {"total_token_usage": usage, "last_token_usage": usage,
                                   "model_context_window": window},
                          "rate_limits": {"limit_id": "codex"}})


def message(role, text):
    key = "input_text" if role == "user" else "output_text"
    return ("response_item", {"type": "message", "id": "msg_1", "role": role,
                              "content": [{"type": key, "text": text}]})


class TestReadingARollout(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-codex-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def read(self, rows, echo=None):
        path = rollout(self.dir, rows=rows)
        return cr.codex_records(path, 0, echo)[1]

    def test_a_refused_turn_is_a_stall(self):
        got = self.read([task_complete(
            error={"message": CAPACITY, "codex_error_info": "server_overloaded"}, last=None)])
        self.assertEqual([r["kind"] for r in got], ["stall"])
        self.assertEqual(got[0]["text"], CAPACITY)

    def test_a_turn_the_account_ran_out_on_is_a_limit(self):
        got = self.read([task_complete(
            error={"message": OUT_OF_QUOTA, "codex_error_info": "usage_limit_exceeded"},
            last=None)])
        self.assertEqual([r["kind"] for r in got], ["limit"])
        # And the wording carries a time, which is the whole point of preferring
        # the message over anything we could guess. Read from the day before the
        # one it names, since a reset that has already passed is no wait at all.
        before = datetime.datetime(2026, 8, 19, 12, 0).astimezone()
        self.assertAlmostEqual(cr.parse_reset(got[0]["text"], before), 22 * 3600, delta=60)

    def test_an_error_with_no_kind_is_read_from_its_wording(self):
        got = self.read([task_complete(error={"message": OUT_OF_QUOTA}, last=None)])
        self.assertEqual([r["kind"] for r in got], ["limit"])

    def test_an_unrecognised_error_is_a_stall_rather_than_a_limit(self):
        # Guessing "limit" would park the session for five hours over a hiccup.
        got = self.read([task_complete(
            error={"message": "the connection went away", "codex_error_info": "io"},
            last=None)])
        self.assertEqual([r["kind"] for r in got], ["stall"])

    def test_a_turn_that_finished_says_so(self):
        got = self.read([task_complete()])
        self.assertEqual(got[0]["kind"], "alive")
        self.assertTrue(got[0]["clean"])

    def test_token_count_carries_the_context_and_the_window(self):
        got = self.read([token_count(120000, window=258400)])
        self.assertEqual(got[0]["tokens"], 120000)
        self.assertEqual(got[0]["window"], 258400)

    def test_the_prompt_we_typed_comes_back_as_an_echo(self):
        got = self.read([message("user", "continue")], echo="continue")
        self.assertEqual([r["kind"] for r in got], ["echo"])
        self.assertEqual(got[0]["text"], "continue")

    def test_a_prompt_we_did_not_type_is_not_an_echo(self):
        self.assertEqual(self.read([message("user", "do the thing")], echo="continue"), [])

    def test_an_answer_says_the_session_is_alive(self):
        got = self.read([message("assistant", "pong")])
        self.assertEqual([r["kind"] for r in got], ["alive"])
        self.assertFalse(got[0].get("clean"))

    def test_the_model_is_named_but_proves_nothing(self):
        got = self.read([("event_msg", {"type": "thread_settings_applied",
                                        "thread_settings": {"model": "gpt-5.6-sol"}})])
        self.assertEqual(got[0]["model"], "gpt-5.6-sol")
        # `quiet`: a row that names the model is not the session answering, and a
        # wait that ended on one would end on the user opening /model.
        self.assertTrue(got[0]["quiet"])

    def test_rows_are_not_collapsed(self):
        # Each row is a different statement about the turn. Merging them the way
        # claude's assistant rows are merged would let the one without figures
        # blank out the one that had them.
        got = self.read([token_count(9000), message("assistant", "pong"), task_complete()])
        self.assertEqual([r["kind"] for r in got], ["alive", "alive", "alive"])
        self.assertEqual(got[0]["tokens"], 9000)

    def test_reading_resumes_where_it_stopped(self):
        path = rollout(self.dir, rows=[task_complete()])
        offset, first = cr.codex_records(path, 0)
        self.assertEqual(len(first), 1)
        _, again = cr.codex_records(path, offset)
        self.assertEqual(again, [])

    def test_a_half_written_row_is_left_for_the_next_poll(self):
        path = rollout(self.dir, rows=[])
        with open(path, "a") as fh:
            fh.write('{"type":"event_msg","payload":{"type":"task_comp')
        offset, got = cr.codex_records(path, 0)
        self.assertEqual(got, [])
        with open(path, "a") as fh:
            fh.write('lete","turn_id":"t"}}\n')
        _, got = cr.codex_records(path, offset)
        self.assertEqual([r["kind"] for r in got], ["alive"])


class TestWhereATurnStartsAndEnds(unittest.TestCase):
    """What a context restart reads off a rollout, in the shape codex-cli 0.154
    writes it. Claude Code states how a turn ended in every answer; codex states
    it once, in the row that closes the turn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-codex-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def read(self, rows):
        return cr.codex_records(rollout(self.dir, rows=rows), 0)[1]

    def test_a_turn_opens_with_its_window(self):
        got = self.read([("event_msg", {"type": "task_started", "turn_id": "t1",
                                        "model_context_window": 258400})])
        self.assertEqual(got[0]["turn"], "open")
        self.assertEqual(got[0]["window"], 258400)
        # A turn the server is about to refuse starts exactly like one it serves.
        self.assertTrue(got[0]["quiet"])

    def test_a_turn_that_finished_closed_with_end_turn(self):
        got = self.read([task_complete()])
        self.assertEqual(got[0]["turn"], "closed")
        self.assertEqual(got[0]["stop_reason"], "end_turn")

    def test_esc_closes_a_turn_without_end_turn(self):
        got = self.read([("event_msg", {"type": "turn_aborted", "turn_id": "t1"})])
        self.assertEqual(got[0]["turn"], "closed")
        self.assertEqual(got[0]["stop_reason"], "aborted")
        self.assertTrue(got[0]["quiet"])

    def test_a_refused_turn_is_closed_too(self):
        got = self.read([task_complete(
            error={"message": CAPACITY, "codex_error_info": "server_overloaded"}, last=None)])
        self.assertEqual(got[0]["turn"], "closed")
        self.assertNotIn("stop_reason", got[0])

    def test_turn_context_names_the_model(self):
        got = self.read([("turn_context", {"turn_id": "t1", "model": "gpt-5.6-sol",
                                           "effort": "high"})])
        self.assertEqual(got[0]["model"], "gpt-5.6-sol")
        self.assertTrue(got[0]["quiet"])


class TestWhichRolloutsAreOurs(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cr-codex-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        t = time.localtime()
        self.today = os.path.join(self.home, "sessions", "%04d" % t.tm_year,
                                  "%02d" % t.tm_mon, "%02d" % t.tm_mday)

    def agent(self, cwd=None, now=None):
        return cr.CodexAgent(home=self.home, cwd=cwd or os.getcwd(), now=now)

    def test_todays_rollouts_are_found(self):
        path = rollout(self.today, rows=[])
        self.assertIn(path, self.agent().paths())

    def test_a_rollout_from_another_day_is_not_scanned(self):
        old = os.path.join(self.home, "sessions", "2001", "01", "01")
        path = rollout(old, rows=[])
        self.assertNotIn(path, self.agent().paths())

    def test_our_own_rollout_is_kept(self):
        path = rollout(self.today, rows=[])
        self.assertTrue(self.agent().keep(path))

    def test_a_subagents_rollout_is_not_ours(self):
        # It is a different session with a context of its own, and a limit on it
        # is not this terminal's news.
        path = rollout(self.today, name="rollout-sub.jsonl", subagent=True, rows=[])
        self.assertFalse(self.agent().keep(path))

    def test_a_rollout_from_another_directory_is_not_ours(self):
        path = rollout(self.today, name="rollout-elsewhere.jsonl", cwd="/somewhere/else",
                       rows=[])
        self.assertFalse(self.agent().keep(path))

    def test_a_file_with_no_head_yet_is_read_anyway(self):
        # Better to read a stranger's rollout for one poll than to write our own
        # session off for the rest of its life.
        os.makedirs(self.today, exist_ok=True)
        path = os.path.join(self.today, "rollout-empty.jsonl")
        open(path, "w").close()
        a = self.agent()
        self.assertTrue(a.keep(path))
        # ...and the verdict is not cached, so the real answer still lands.
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta",
                                 "payload": {"thread_source": "subagent"}}) + "\n")
        self.assertFalse(a.keep(path))

    def test_a_rollout_codex_resume_reopens_days_later_is_still_found(self):
        # `codex resume` writes into the rollout from the day it was CREATED,
        # which the three-day scan above has long since scrolled past (T03).
        old = os.path.join(self.home, "sessions", "2026", "08", "01")
        a = self.agent(now=time.time() - 5)
        path = rollout(old, rows=[])           # written "now": after `a` started
        self.assertIn(path, a.paths())

    def test_an_old_rollout_nobody_resumed_is_left_alone(self):
        old = os.path.join(self.home, "sessions", "2020", "01", "01")
        path = rollout(old, rows=[])
        os.utime(path, (1000, 1000))           # long before this agent existed
        self.assertNotIn(path, self.agent().paths())


class TestBindingTwoLiveRollouts(unittest.TestCase):
    """T03: nothing about a rollout names the pid that writes it, so two user
    sessions in the same cwd look identical until the fold phrase's nonce
    lands in one of them. Until then, the wrapper reads both but does not bet
    a restart on a guess."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cr-codex-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        t = time.localtime()
        self.today = os.path.join(self.home, "sessions", "%04d" % t.tm_year,
                                  "%02d" % t.tm_mon, "%02d" % t.tm_mday)

    def watcher(self, cwd=None):
        agent = cr.CodexAgent(home=self.home, cwd=cwd or os.getcwd())
        return cr.TranscriptWatcher(agent=agent, poll=0)

    def test_both_are_candidates_until_one_is_confirmed(self):
        # Both files are created AFTER the watcher starts -- neither is
        # "preexisting", which is exactly what makes them indistinguishable.
        w = self.watcher()
        rollout(self.today, name="rollout-a.jsonl", rows=[])
        rollout(self.today, name="rollout-b.jsonl", rows=[])
        w.poll_now(now=time.time() + 1)
        self.assertEqual(len(w.candidates), 2)

    def test_the_echo_settles_it_and_the_others_growth_cannot_undo_that(self):
        w = self.watcher()
        a = rollout(self.today, name="rollout-a.jsonl", rows=[])
        b = rollout(self.today, name="rollout-b.jsonl", rows=[])
        w.poll_now(now=time.time() + 1)
        w.confirm(a)                      # our handoff phrase was echoed in `a`
        self.assertEqual(w.current, a)
        self.assertEqual(len(w.candidates), 1)
        with open(b, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {}}) + "\n")
        w.poll_now(now=time.time() + 2)
        self.assertEqual(w.current, a)

    def test_a_subagent_is_never_a_candidate_even_with_a_live_neighbour(self):
        w = self.watcher()
        a = rollout(self.today, name="rollout-a.jsonl", rows=[])
        rollout(self.today, name="rollout-sub.jsonl", subagent=True, rows=[])
        w.poll_now(now=time.time() + 1)
        self.assertEqual(w.current, a)
        self.assertEqual(w.candidates, [a])


# --------------------------------------------------------------------------- #
# T34: which thread is ours, from codex's own log
# --------------------------------------------------------------------------- #
def uuid7(at):
    """A thread id the way codex 0.16x mints one: UUIDv7, `at` in its first 48 bits."""
    import uuid
    ms = int(at * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return str(uuid.UUID(int=(ms << 80) | (0x7 << 76) | (((rand >> 62) & 0xFFF) << 64)
                             | (0x2 << 62) | (rand & ((1 << 62) - 1))))


def codex_logs(home, process_uuid=True):
    """`$CODEX_HOME/logs_2.sqlite` with codex-cli 0.161's columns (or, with
    `process_uuid=False`, those of a codex from before that column)."""
    import sqlite3
    db = sqlite3.connect(os.path.join(home, "logs_2.sqlite"))
    db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
               "ts_nanos INTEGER NOT NULL DEFAULT 0, level TEXT NOT NULL DEFAULT 'INFO', "
               "target TEXT NOT NULL DEFAULT 'codex_core', feedback_log_body TEXT, "
               "thread_id TEXT, %sestimated_bytes INTEGER NOT NULL DEFAULT 0)"
               % ("process_uuid TEXT, " if process_uuid else ""))
    db.commit()
    return db


def log_row(db, pid, thread, at, body='app_server.request{rpc.method="thread/start"}'):
    """One row of codex's log, written by `pid` about `thread` (None: threadless)
    -- with nothing to say who wrote it, in a table from before that column."""
    if "process_uuid" in {row[1] for row in db.execute("PRAGMA table_info(logs)")}:
        db.execute("INSERT INTO logs (ts, feedback_log_body, thread_id, process_uuid) "
                   "VALUES (?, ?, ?, ?)", (int(at), body, thread, "pid:%d:uuid-of-%d" % (pid, pid)))
    else:
        db.execute("INSERT INTO logs (ts, feedback_log_body, thread_id) VALUES (?, ?, ?)",
                   (int(at), body, thread))
    db.commit()


def codex_rollout(home, thread, created, cwd, subagent=False, rows=()):
    """The rollout codex writes for `thread`, named and filed for when it was
    created, the way real codex names and files one (local time)."""
    t = time.localtime(created)
    d = os.path.join(home, "sessions", "%04d" % t.tm_year, "%02d" % t.tm_mon, "%02d" % t.tm_mday)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S", t), thread))
    if not os.path.exists(path):
        meta = {"id": thread, "session_id": thread, "cwd": cwd, "originator": "codex-tui",
                "cli_version": "0.161.0", "thread_source": "subagent" if subagent else "user",
                "source": {"subagent": {"thread_spawn": {}}} if subagent else "cli"}
        with open(path, "w") as fh:
            fh.write(json.dumps({"timestamp": "x", "type": "session_meta", "payload": meta}) + "\n")
    with open(path, "a", encoding="utf-8") as fh:
        for rtype, payload in rows:
            # Raw UTF-8, as codex writes it: the default phrase has an em dash.
            fh.write(json.dumps({"timestamp": "x", "type": rtype, "payload": payload},
                                ensure_ascii=False) + "\n")
    return path


def a_turn(tokens):
    """One whole turn of a codex session: opened, the model named, a count, closed."""
    return [("event_msg", {"type": "task_started", "model_context_window": 258400}),
            ("turn_context", {"model": "gpt-5.6-sol"}), token_count(tokens), task_complete()]


class TestCodexSessionRegistry(unittest.TestCase):
    """`CodexSessionRegistry.lookup()` in isolation: which thread codex's log
    says the codex we forked (pid 111) is running."""

    CHILD = 111

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cr-codex-reg-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.work = os.path.join(self.home, "work")
        os.makedirs(self.work)
        self.t0 = time.time() - 600          # when this wrapper started
        self.db = codex_logs(self.home)
        self.logged = []

    def registry(self, table=None, resuming=False, child=CHILD):
        self.agent = cr.CodexAgent(home=self.home, cwd=self.work, now=self.t0,
                                   resuming=resuming)
        return cr.CodexSessionRegistry(child, self.agent, started_at=self.t0,
                                       log=self.logged.append,
                                       proc_table=lambda: dict(table or {}))

    def thread(self, pid, offset, rows=None, subagent=False):
        """A thread `pid` created `offset` seconds after we started, and logged."""
        tid = uuid7(self.t0 + offset)
        log_row(self.db, pid, tid, self.t0 + offset)
        path = None
        if rows is not None:
            path = codex_rollout(self.home, tid, self.t0 + offset, self.work,
                                 subagent=subagent, rows=rows)
        return tid, path

    def test_our_thread_is_named_before_it_has_a_rollout(self):
        # codex logs `thread/start` as it starts; the file waits for the
        # first message. Named, with nowhere to read: every rollout in the
        # directory is somebody else's.
        reg = self.registry()
        tid, _ = self.thread(self.CHILD, 1)
        rec, why = reg.lookup()
        self.assertEqual(rec, {"sessionId": tid, "path": None})
        self.assertEqual(why, "pid 111 in codex's log; no rollout yet")

    def test_once_written_its_rollout_is_the_session(self):
        reg = self.registry()
        tid, _ = self.thread(self.CHILD, 1)
        reg.lookup()
        path = codex_rollout(self.home, tid, self.t0 + 1, self.work, rows=a_turn(9000))
        rec, why = reg.lookup()
        self.assertEqual(rec, {"sessionId": tid, "path": path})
        self.assertEqual(why, "pid 111 in codex's log")

    def test_a_neighbours_thread_is_never_ours_even_alone(self):
        # The later wrapper of the pair, whose codex has said nothing: the
        # neighbour's rollout is the only one there is, and it is theirs.
        reg = self.registry(table={222: 1})
        _, theirs = self.thread(222, 1, rows=a_turn(250000))
        self.assertEqual(reg.lookup(), (None, None))
        self.assertEqual(reg.strangers([theirs]), {theirs})

    def test_a_codex_behind_a_shim_is_found_through_its_parent_chain(self):
        # The cmux shim, or npm's codex.js: what we fork is not codex itself.
        reg = self.registry(table={300: self.CHILD, self.CHILD: 1})
        tid, path = self.thread(300, 1, rows=a_turn(9000))
        rec, why = reg.lookup()
        self.assertEqual(rec["sessionId"], tid)
        self.assertEqual(why, "descendant of pid 111 in codex's log")

    def test_a_codex_the_session_ran_itself_does_not_displace_it(self):
        # `codex exec` run from inside the session's own shell: a codex below
        # ours, in our directory, writing a user rollout of its own.
        table = {self.CHILD: 1, 250: self.CHILD, 260: 250}
        reg = self.registry(table=table)
        ours, path = self.thread(self.CHILD, 1, rows=a_turn(9000))
        nested, nested_path = self.thread(260, 60, rows=a_turn(500))
        rec, _ = reg.lookup()
        self.assertEqual(rec["sessionId"], ours)
        self.assertEqual(reg.strangers([path, nested_path]), {nested_path})

    def test_a_subagent_logs_under_our_pid_and_is_not_the_session(self):
        reg = self.registry()
        ours, path = self.thread(self.CHILD, 1, rows=a_turn(9000))
        self.thread(self.CHILD, 30, rows=a_turn(500), subagent=True)
        log_row(self.db, self.CHILD, ours, self.t0 + 31)
        rec, _ = reg.lookup()
        self.assertEqual(rec, {"sessionId": ours, "path": path})

    def test_a_clear_moves_the_session_once_the_new_thread_is_written(self):
        # `/clear` makes the new thread at once and its file at the resume
        # phrase. Until then the session stays where the restart left it.
        reg = self.registry()
        old, old_path = self.thread(self.CHILD, 1, rows=a_turn(200000))
        new, _ = self.thread(self.CHILD, 120)
        self.assertEqual(reg.lookup()[0], {"sessionId": old, "path": old_path})
        new_path = codex_rollout(self.home, new, self.t0 + 120, self.work, rows=a_turn(5000))
        log_row(self.db, self.CHILD, new, self.t0 + 140)
        self.assertEqual(reg.lookup()[0], {"sessionId": new, "path": new_path})

    def test_rows_from_before_we_started_are_not_read(self):
        # A pid reused from a codex that exited before we were started.
        reg = self.registry()
        stale = uuid7(self.t0 - 3600)
        log_row(self.db, self.CHILD, stale, self.t0 - 3600)
        codex_rollout(self.home, stale, self.t0 - 3600, self.work, rows=a_turn(250000))
        self.assertEqual(reg.lookup(), (None, None))

    def test_a_log_that_cannot_say_who_wrote_a_row_is_unavailable(self):
        os.remove(os.path.join(self.home, "logs_2.sqlite"))
        codex_logs(self.home, process_uuid=False)
        reg = self.registry()
        self.assertEqual(reg.lookup(), (None, None))
        self.assertTrue(reg.unavailable)
        self.assertEqual(len([ln for ln in self.logged if "does not say which process" in ln]), 1)
        reg.lookup()
        self.assertEqual(len([ln for ln in self.logged if "does not say which process" in ln]), 1)

    def test_no_log_yet_is_only_not_yet(self):
        os.remove(os.path.join(self.home, "logs_2.sqlite"))
        reg = self.registry()
        self.assertEqual(reg.lookup(), (None, None))
        self.assertFalse(reg.unavailable)

    def test_a_resumed_thread_is_found_in_the_day_it_was_made(self):
        # `codex resume`: a thread a month old, in a directory the three-day
        # scan never looks at, named by our pid as it is reopened.
        reg = self.registry(resuming=True)
        made = self.t0 - 30 * 86400
        tid = uuid7(made)
        path = codex_rollout(self.home, tid, made, self.work, rows=a_turn(120000))
        os.utime(path, (made, made))
        log_row(self.db, self.CHILD, tid, self.t0 + 1)
        self.assertEqual(reg.lookup()[0], {"sessionId": tid, "path": path})
        # ...and its history is not news: it is read from where it stands.
        self.assertEqual(self.agent.seed_offset(path), os.path.getsize(path))
        self.assertEqual(reg.strangers([path]), set())

    def test_a_resumed_rollouts_history_is_not_read_as_news(self):
        # `codex resume` on a session that hit its limit last month. The file
        # turns up by its mtime the moment codex appends to it, and used to
        # be read from byte 0 like a new one: last month's limit, scheduled
        # as a wait today.
        made = self.t0 - 30 * 86400
        tid = uuid7(made)
        path = codex_rollout(self.home, tid, made, self.work, rows=a_turn(120000) + [
            task_complete(error={"message": OUT_OF_QUOTA,
                                 "codex_error_info": "usage_limit_exceeded"}, last=None)])
        os.utime(path, (made, made))
        agent = cr.CodexAgent(home=self.home, cwd=self.work, now=self.t0)
        w = cr.TranscriptWatcher(agent=agent, poll=0, now=self.t0)
        os.utime(path, None)                         # codex appended to it just now
        self.assertIn(path, agent.paths(self.t0 + 60))
        found = w.poll_now(self.t0 + 60)
        self.assertEqual([r for r in found if r["kind"] == "limit"], [])
        codex_rollout(self.home, tid, made, self.work, rows=a_turn(125000))
        found = w.poll_now(self.t0 + 62)
        self.assertIn(125000, [r.get("tokens") for r in found])

    def test_a_thread_made_before_we_started_is_a_stranger_unless_we_resume(self):
        old, path = self.thread(222, -120, rows=a_turn(250000))
        reg = self.registry()
        reg.lookup()
        self.assertEqual(reg.strangers([path]), {path})        # the log says so too
        os.remove(os.path.join(self.home, "logs_2.sqlite"))
        quiet = self.registry()                                  # ...and without a log
        self.assertEqual(quiet.strangers([path]), {path})
        self.assertEqual(self.registry(resuming=True).strangers([path]), set())
        self.assertEqual(self.agent.seed_offset(path), os.path.getsize(path))


class TestTwoCodexSessionsInOneDirectory(unittest.TestCase):
    """T34 from end to end, without a pty: the real registry, the real watcher
    and the real controller, handed rows the way `main()` hands them.

    Two codex sessions in one directory, as the owner's log has them on
    2026-10-08 in some_game: wrapper 9345 started at 19:38, its rollout bound
    at 19:39; wrapper 75256 started at 19:54 and was `context bound` to 9345's
    rollout ten seconds later; 9345, seeing two, held its restart from
    19:55:42 and was next heard from when codex had compacted the thread
    itself, at the cap. Both are reproduced here, and both were what the
    script did before `CodexSessionRegistry` (absent, this builds none)."""

    THRESHOLD = 194400
    OURS, THEIRS = 111, 222

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cr-codex-pair-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.work = os.path.join(self.home, "work")
        os.makedirs(self.work)
        self.db = codex_logs(self.home)
        self.typed = []
        self.logged = []

    def start(self, at, logs=True):
        """Our wrapper, started at `at`, its codex forked as pid OURS."""
        self.t0 = at
        cfg = dict(cr.CFG)
        cfg.update(RESTART)                  # the phrases: bash supplies them, not CFG
        cfg.update(codex_context_tokens=self.THRESHOLD, wait_scale=1, margin=0,
                   user_idle=0, root_idle=0, handoff_file=os.path.join(self.home, "handoff.md"),
                   codex_folds_file=os.path.join(self.home, "folds.json"))
        self.ctl = cr.Controller(cr.agent_cfg(cfg, "codex"), self.logged.append, now=at)
        agent = cr.CodexAgent(home=self.home, cwd=self.work, now=at)
        Registry = getattr(cr, "CodexSessionRegistry", None)
        registry = Registry(self.OURS, agent, started_at=at, log=self.logged.append,
                            proc_table=lambda: {self.OURS: 1, self.THEIRS: 1}) if Registry else None
        self.w = cr.TranscriptWatcher(agent=agent, poll=0, now=at, registry=registry,
                                      log=self.logged.append)
        self.watching = None

    def thread(self, pid, created, rows=None):
        tid = uuid7(created)
        log_row(self.db, pid, tid, created)
        return tid, (codex_rollout(self.home, tid, created, self.work, rows=rows)
                     if rows is not None else None)

    def turn(self, at):
        """One turn of the supervisor's loop at `at`: what `main()` does with a poll."""
        w, ctl = self.w, self.ctl
        recs = w.poll_now(at)
        if w.current and w.current != ctl.context_path:
            why = "session identity" if w.bound_session_id else "fallback heuristic"
            ctl.bind_transcript(w.current, why, at)
        ctl.on_candidates(w.guessing and len(w.candidates) > 1 and not w.confirmed
                          and not w.bound_session_id, at)
        for rec in recs:
            if rec["kind"] == "echo":
                if (ctl.handoff_text and rec["text"] == ctl.handoff_text
                        and ctl.on_handoff_echo(rec["path"], at)):
                    w.confirm(rec["path"])
                continue
            if rec["kind"] == "alive" and rec["path"] == w.current:
                ctl.on_context(rec, at)
                if rec.get("turn"):
                    ctl.on_turn(rec["turn"], at)
        ctl.note_growth(w.grown, at)
        action = ctl.tick(at)
        if ctl.handoff_text != self.watching:
            if self.watching:
                w.forget(self.watching)
            self.watching = ctl.handoff_text
            if self.watching:
                w.expect(self.watching)
        if action and action[0] == "inject":
            self.typed.append(action[1])
        return action

    def folded(self):
        return [t for t in self.typed if self.ctl.nonce and self.ctl.nonce in t] or \
               [ln for ln in self.logged if "folding up" in ln]

    def test_the_later_session_is_not_folded_on_its_neighbours_count(self):
        # The neighbour started ten minutes before us and has been talking
        # since; ours has said nothing, so it has no rollout at all.
        t0 = time.time() - 3600
        theirs, their_path = self.thread(self.THEIRS, t0 + 1, rows=a_turn(150000))
        self.start(t0 + 600)
        ours, _ = self.thread(self.OURS, t0 + 601)
        for i, at in enumerate((602, 604, 606, 640, 700)):
            codex_rollout(self.home, theirs, t0 + 1, self.work,
                          rows=a_turn(250000) if i == 1 else [("event_msg", {"type": "x"})])
            self.turn(t0 + at)
            self.assertIsNone(self.ctl.context_path)
        self.assertEqual(self.folded(), [])
        self.assertNotIn(theirs, "".join(self.logged))
        self.assertIn("session bound: %s (pid 111 in codex's log; no rollout yet)" % ours,
                      self.logged)
        # Then ours is spoken to, and it is ours that is followed from then on.
        path = codex_rollout(self.home, ours, t0 + 601, self.work, rows=a_turn(9000))
        self.turn(t0 + 702)
        self.assertEqual(self.ctl.context_path, path)
        self.assertEqual(self.ctl.context_tokens, 9000)

    def test_the_earlier_session_still_folds_once_a_neighbour_has_started(self):
        # We started first, and our rollout is the only one; then a neighbour
        # starts and speaks. Ours climbs past the threshold and goes quiet.
        t0 = time.time() - 40000
        self.start(t0)
        ours, path = self.thread(self.OURS, t0 + 1, rows=a_turn(150000))
        self.turn(t0 + 10)
        self.assertEqual(self.ctl.context_path, path)
        self.thread(self.THEIRS, t0 + 400, rows=a_turn(10000))
        self.turn(t0 + 402)
        codex_rollout(self.home, ours, t0 + 1, self.work, rows=a_turn(250000))
        for at in (404, 406, 410):
            self.turn(t0 + at)
        self.assertTrue(self.folded(), self.logged)
        self.assertFalse([ln for ln in self.logged if "candidate rollout" in ln])

    def test_without_a_log_that_names_processes_the_hold_still_gives_way(self):
        # A codex too old to record who wrote a row: no name for anyone, the
        # heuristic, and T03's hold over two candidates -- which is the
        # right thing to do for a while, and was the wrong thing to do for
        # ten hours (the probe that found it went on longer still).
        os.remove(os.path.join(self.home, "logs_2.sqlite"))
        self.db = codex_logs(self.home, process_uuid=False)
        t0 = time.time() - 40000
        self.start(t0)
        ours, path = self.thread(self.OURS, t0 + 1, rows=a_turn(150000))
        self.turn(t0 + 10)
        self.thread(self.THEIRS, t0 + 400, rows=a_turn(10000))
        self.turn(t0 + 402)
        codex_rollout(self.home, ours, t0 + 1, self.work, rows=a_turn(250000))
        self.turn(t0 + 404)
        self.assertIn("more than one candidate rollout in this directory; holding the restart "
                      "until the handoff phrase proves which is ours", self.logged)
        self.turn(t0 + 406)
        self.assertEqual(self.folded(), [])                 # held, as T03 holds it
        for at in range(410, 36000, 60):
            if self.folded():
                break
            self.turn(t0 + at)
        self.assertTrue(self.folded(), self.logged[-5:])
        held = [ln for ln in self.logged if "for want of a proof" in ln]
        self.assertEqual(len(held), 1)
        # The phrase lands in the rollout it was folded on: proven, and on it goes.
        codex_rollout(self.home, ours, t0 + 1, self.work,
                      rows=[message("user", self.ctl.handoff_text)])
        self.turn(t0 + at + 2)
        self.assertEqual(self.ctl.rstate, cr.HANDOFF_SENT)
        self.assertTrue(self.ctl.handoff_echoed)
        self.assertTrue(self.w.confirmed)

    def test_a_fold_on_a_wrong_guess_is_called_off_where_it_lands(self):
        # Same old codex. This time the guess is the neighbour's: it is the
        # one that kept growing, and it is the one past the threshold. When
        # the hold gives way the phrase goes into OUR session -- everything
        # typed does -- and its echo says the count was never ours.
        os.remove(os.path.join(self.home, "logs_2.sqlite"))
        self.db = codex_logs(self.home, process_uuid=False)
        t0 = time.time() - 40000
        self.start(t0)
        ours, path = self.thread(self.OURS, t0 + 1, rows=a_turn(9000))
        self.turn(t0 + 10)
        theirs, their_path = self.thread(self.THEIRS, t0 + 30, rows=a_turn(150000))
        self.turn(t0 + 32)
        codex_rollout(self.home, theirs, t0 + 30, self.work, rows=a_turn(250000))
        at = 34
        while not self.folded() and at < 36000:
            self.turn(t0 + at)
            at += 60
        self.assertTrue(self.folded())
        self.assertEqual(self.ctl.context_path, their_path)       # what it folded on
        codex_rollout(self.home, ours, t0 + 1, self.work,
                      rows=[message("user", self.ctl.handoff_text)])
        self.turn(t0 + at)
        self.assertEqual(self.ctl.context_path, path)
        self.assertEqual(self.ctl.rstate, cr.CANCEL_PENDING)
        self.assertIn("restart aborted at handoff_sent: the handoff phrase landed in another "
                      "rollout than the one whose count it was folded on", self.logged)
        self.assertTrue(self.w.confirmed)


class TestPickingTheAgent(unittest.TestCase):
    def test_a_codex_binary_is_recognised(self):
        self.assertEqual(cr.pick_agent("auto", ["/Users/x/.local/bin/codex"]), "codex")

    def test_a_codex_command_line_behind_a_shell_is_recognised(self):
        self.assertEqual(
            cr.pick_agent("auto", ["/bin/zsh", "-c", 'codex --model x "$@"', "codex"]),
            "codex")

    def test_claude_is_the_default(self):
        self.assertEqual(cr.pick_agent("auto", ["/usr/local/bin/claude"]), "claude")

    def test_a_name_that_merely_contains_codex_is_not_one(self):
        self.assertEqual(cr.pick_agent("auto", ["/usr/local/bin/my-codex-helper"]),
                         "claude")

    def test_a_prompt_that_says_codex_does_not_pick_codex(self):
        # `agent-retrier "fix the codex build"` starts claude. Reading the
        # sentence would send the watcher after a file nobody is writing.
        self.assertEqual(cr.pick_agent("auto", ["/usr/local/bin/claude"]), "claude")

    def test_an_explicit_choice_wins(self):
        self.assertEqual(cr.pick_agent("codex", ["/usr/local/bin/claude"]), "codex")
        self.assertEqual(cr.pick_agent("claude", ["/usr/local/bin/codex"]), "claude")

    def test_the_directory_codex_was_pointed_at_is_the_one_we_follow(self):
        self.assertEqual(cr.codex_cwd(["--cd", "/tmp/x"]), "/tmp/x")
        self.assertEqual(cr.codex_cwd(["--cd=/tmp/y"]), "/tmp/y")
        self.assertIsNone(cr.codex_cwd(["--model", "gpt"]))


CFG = dict(message="continue", margin=0, max_attempts=3, fallback_wait=18000,
           max_wait=691200, user_idle=20, busy_idle=6, verify=60, wait_scale=1,
           draft_grace=600, resume=15, typing_max=900,
           stall_wait=60, stall_backoff=2, stall_max_wait=600, stall_max_attempts=8)


def controller(**over):
    cfg = dict(CFG)
    cfg.update(over)
    logs = []
    ctl = cr.Controller(cfg, logs.append, now=0)
    ctl.log_lines = logs
    return ctl


class TestStalling(unittest.TestCase):
    def test_a_stall_schedules_a_nudge(self):
        ctl = controller()
        self.assertTrue(ctl.on_stall(CAPACITY, now=1000, source="transcript"))
        self.assertEqual(ctl.state, cr.WAITING)
        self.assertAlmostEqual(ctl.wake_at, 1060, delta=1)

    def test_no_margin_is_added(self):
        # The margin exists to clear a stated reset time by a little. A stall
        # states nothing, so there is nothing to clear.
        ctl = controller(margin=45)
        ctl.on_stall(CAPACITY, now=0, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 60, delta=1)

    def test_the_wait_doubles_while_the_refusals_keep_coming(self):
        ctl = controller()
        waits = []
        for i in range(4):
            now = i * 1000
            ctl.state = cr.IDLE
            ctl.on_stall(CAPACITY, now=now, source="transcript")
            waits.append(round(ctl.wake_at - now))
        self.assertEqual(waits, [60, 120, 240, 480])

    def test_the_wait_is_capped(self):
        ctl = controller(stall_max_wait=100)
        for i in range(4):
            ctl.state = cr.IDLE
            ctl.on_stall(CAPACITY, now=i * 1000, source="transcript")
        self.assertAlmostEqual(ctl.wake_at - 3000, 100, delta=1)

    def test_a_turn_that_finishes_ends_the_streak(self):
        ctl = controller()
        ctl.on_stall(CAPACITY, now=0, source="transcript")
        ctl.state = cr.IDLE
        ctl.on_turn_done(now=100)
        ctl.on_stall(CAPACITY, now=200, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 260, delta=1)

    def test_the_same_line_again_is_the_same_stall(self):
        # A TUI repaints the line for as long as it is on screen, and the
        # transcript reports it besides.
        ctl = controller()
        self.assertTrue(ctl.on_stall(CAPACITY, now=0, source="screen"))
        ctl.state = cr.IDLE
        self.assertFalse(ctl.on_stall(CAPACITY, now=5, source="screen"))

    def test_a_stall_during_a_wait_is_ignored(self):
        ctl = controller()
        ctl.on_limit("You've hit your weekly limit · resets in 2 hours", now=0,
                     source="transcript")
        self.assertFalse(ctl.on_stall(CAPACITY, now=10, source="transcript"))
        self.assertAlmostEqual(ctl.wake_at, 7200, delta=1)

    def test_a_limit_during_a_stall_wait_takes_over(self):
        # The account running out outranks the server being busy: it is the
        # longer wait and the one with a time attached.
        ctl = controller()
        ctl.on_stall(CAPACITY, now=0, source="transcript")
        self.assertTrue(ctl.on_limit("You've hit your weekly limit · resets in 2 hours",
                                     now=10, source="transcript"))
        self.assertAlmostEqual(ctl.wake_at, 7210, delta=1)

    def test_it_gives_up_after_enough_of_them(self):
        ctl = controller(stall_max_attempts=2)
        for i in range(2):
            ctl.state = cr.IDLE
            self.assertTrue(ctl.on_stall(CAPACITY, now=i * 1000, source="transcript"))
        ctl.state = cr.IDLE
        self.assertFalse(ctl.on_stall(CAPACITY, now=9000, source="transcript"))
        self.assertTrue(any("leaving it alone" in l for l in ctl.log_lines))

    def test_zero_switches_it_off(self):
        ctl = controller(stall_wait=0)
        self.assertFalse(ctl.on_stall(CAPACITY, now=0, source="transcript"))
        self.assertEqual(ctl.state, cr.IDLE)

    def test_the_nudge_goes_out_when_the_wait_ends(self):
        ctl = controller(user_idle=0)
        ctl.on_stall(CAPACITY, now=0, source="transcript")
        self.assertIsNone(ctl.tick(30))
        self.assertEqual(ctl.tick(61), ("inject", "continue", False))

    def test_the_notice_says_which_of_the_two_it_is(self):
        ctl = controller(user_idle=0)
        ctl.on_stall(CAPACITY, now=0, source="transcript")
        ctl.tick(61)
        self.assertEqual(ctl.inject_note(), "asking the session to carry on")
        ctl = controller(user_idle=0)
        ctl.on_limit("resets in 1 hours", now=0, source="transcript")
        ctl.tick(3601)
        self.assertEqual(ctl.inject_note(), "limit lifted; resuming session")

    def test_a_working_session_holds_the_nudge_back(self):
        ctl = controller(user_idle=0)
        ctl.on_stall(CAPACITY, now=0, source="transcript")
        ctl.on_output("✳ Cogitating… (esc to interrupt)", now=60)
        self.assertIsNone(ctl.tick(61))


RESTART = dict(
    context_tokens=0, context_window="auto",
    context_env_max=0, context_no_1m=False,
    handoff_file="/proj/H.md", handoff_marker="HANDOFF", handoff_min_bytes=20,
    handoff_attempts=2,
    handoff_msg="Fold into `{file}` and end it with {marker}.",
    clear_cmd="/clear", resume_msg="Read `{file}` and continue from it.",
    root_idle=20, handoff_timeout=900, step_gap=3,
    context_cooldown=600, context_max_cycles=0,
)
WINDOW = 258400
ROLL = "/codex/sessions/rollout-mine.jsonl"


class Handoff:
    def __init__(self):
        self.state = None

    def write(self, ctl, at):
        text = "everything you need to know\n" * 4 + ctl.nonce + "\n"
        self.state = dict(size=len(text), mtime=at, tail=text[-512:])

    def __call__(self, _path):
        return self.state


def codex_controller(agent="codex", **over):
    cfg = dict(CFG)
    cfg.update(RESTART)
    cfg.update(over)
    # A codex controller that completes a fold writes down what it cost, and
    # one whose config names no file for that falls back to the developer's
    # real ~/.agent-retrier/folds.json — whatever the environment says. Every
    # run of this file used to leave a few "folds" there, 1000 tokens each and
    # dated 1970. The file `load()` gave the implementation instead will do.
    cfg.setdefault("codex_folds_file", cr.CFG["codex_folds_file"])
    logs = []
    hand = Handoff()
    ctl = cr.Controller(cr.agent_cfg(cfg, agent), logs.append, now=0, probe=hand)
    ctl.log_lines = logs
    ctl.handoff = hand
    return ctl


def feed(ctl, at, path=ROLL, **rec):
    """One rollout record, the way the main loop hands it over."""
    rec = dict(kind="alive", path=path, sidechain=False, **rec)
    ctl.on_context(rec, at)
    if rec.get("turn"):
        ctl.on_turn(rec["turn"], at)


def moved(ctl, path, at, why="new rollout"):
    """T04: `bind_transcript` is the only thing allowed to move `context_path`
    — what `main()` does once it notices a new rollout file, before handing
    its first row over to `feed()`."""
    ctl.bind_transcript(path, why, at)


class TestCandidateModeHoldsTheFoldBack(unittest.TestCase):
    """T03: what `main()` reports through `on_candidates` when the directory
    holds more than one unconfirmed rollout. A number that might be a
    neighbour's climbing count is read same as any other, but it is not one
    to fold a session on."""

    def ctl(self, **over):
        # root_idle/user_idle=0: nothing here is about whether a person or the
        # session is busy, and the defaults would hold every tick back on
        # that alone.
        return codex_controller(codex_context_tokens=135200, root_idle=0, user_idle=0, **over)

    def test_an_ambiguous_reading_is_never_folded(self):
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        ctl.on_candidates(True, 1)
        self.assertIsNone(ctl.tick(2))
        self.assertIsNone(ctl.rstate)

    def test_narrowing_to_one_candidate_lets_it_through(self):
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        ctl.on_candidates(True, 1)
        self.assertIsNone(ctl.tick(2))
        ctl.on_candidates(False, 3)
        self.assertEqual(ctl.tick(4)[0], "inject")

    def test_the_hold_and_release_are_each_logged_once(self):
        ctl = self.ctl()
        ctl.on_candidates(True, 1)
        ctl.on_candidates(True, 2)                 # no second line for the same state
        ctl.on_candidates(False, 3)
        held = [l for l in ctl.log_lines if "holding the restart" in l]
        released = [l for l in ctl.log_lines if "no longer held" in l]
        self.assertEqual(len(held), 1)
        self.assertEqual(len(released), 1)

    def test_an_unambiguous_reading_was_never_held_back_at_all(self):
        # The common case -- one session, one rollout -- never calls
        # `on_candidates(True, ...)`, so nothing about this feature is in its
        # way.
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        self.assertEqual(ctl.tick(2)[0], "inject")

    # -- T34: the hold is not for good --------------------------------------- #
    def test_the_hold_gives_way_and_says_so_once(self):
        # The proof the hold waits for is the fold phrase's echo, and only a
        # fold types the phrase. Held for good, a session never folded again.
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        ctl.on_candidates(True, 1)
        for at in (2, 60, ctl.PROOF_WAIT):
            self.assertIsNone(ctl.tick(at))
        self.assertEqual(ctl.tick(ctl.PROOF_WAIT + 2)[0], "inject")
        said = [l for l in ctl.log_lines if "for want of a proof" in l]
        self.assertEqual(len(said), 1)

    def test_the_wait_is_for_a_fold_that_is_due_not_for_the_ambiguity(self):
        # Ambiguous for an hour below the threshold is no wait at all: the
        # hold starts holding when there is a fold to hold.
        ctl = self.ctl()
        ctl.on_candidates(True, 1)
        feed(ctl, 2, window=WINDOW, tokens=20000)
        self.assertIsNone(ctl.tick(3600))
        feed(ctl, 3601, window=WINDOW, tokens=WINDOW)
        self.assertIsNone(ctl.tick(3602))
        self.assertIsNone(ctl.tick(3601 + ctl.PROOF_WAIT - 1))
        self.assertEqual(ctl.tick(3602 + ctl.PROOF_WAIT)[0], "inject")

    def test_a_fold_the_hold_let_through_never_interrupts_a_turn(self):
        # Esc mid-turn is not done to a session on a count that may be a
        # neighbour's. Past the interrupt line, with a turn running, a proven
        # reading interrupts; an unproven one waits for the turn to end.
        ctl = self.ctl(codex_interrupt=True)
        feed(ctl, 1, window=WINDOW, tokens=WINDOW - 1000, turn="open")
        ctl.on_candidates(True, 1)
        self.assertIsNone(ctl.tick(2))                       # held: the wait starts
        self.assertIsNone(ctl.tick(2 + ctl.PROOF_WAIT))      # ...and is over, but not by Esc
        self.assertTrue([l for l in ctl.log_lines if "for want of a proof" in l])
        self.assertIn("restart step held: a turn is still running", ctl.log_lines[-1])
        self.assertIsNone(ctl.tick(4 + ctl.PROOF_WAIT))
        feed(ctl, 5 + ctl.PROOF_WAIT, turn="closed", stop_reason="end_turn")
        self.assertEqual(ctl.tick(6 + ctl.PROOF_WAIT)[0], "inject")

    def test_an_unproven_fold_whose_echo_lands_where_it_was_read_goes_on(self):
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        ctl.on_candidates(True, 1)
        ctl.tick(2)
        self.assertEqual(ctl.tick(3 + ctl.PROOF_WAIT)[0], "inject")
        self.assertTrue(ctl.on_handoff_echo(ROLL, 4 + ctl.PROOF_WAIT))
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)

    def test_an_unproven_fold_whose_echo_lands_elsewhere_is_called_off(self):
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        ctl.on_candidates(True, 1)
        ctl.tick(2)
        self.assertEqual(ctl.tick(3 + ctl.PROOF_WAIT)[0], "inject")
        mine = "/codex/sessions/rollout-really-mine.jsonl"
        self.assertTrue(ctl.on_handoff_echo(mine, 4 + ctl.PROOF_WAIT))
        self.assertEqual(ctl.context_path, mine)
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)
        # ...and what goes out next is the phrase that calls it off.
        action = ctl.tick(5 + ctl.PROOF_WAIT)
        self.assertEqual(action[0], "inject")
        self.assertIn("cancelled", action[1])

    def test_a_proven_fold_whose_echo_lands_elsewhere_is_not_called_off(self):
        # The echo landing elsewhere after an ordinary fold is T06's old case:
        # the binding is corrected and the restart goes on, as it always did.
        ctl = self.ctl()
        feed(ctl, 1, window=WINDOW, tokens=WINDOW)
        self.assertEqual(ctl.tick(2)[0], "inject")
        self.assertTrue(ctl.on_handoff_echo("/codex/sessions/rollout-other.jsonl", 3))
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)


class TestTheCodexThreshold(unittest.TestCase):
    """The percentage is the one on codex's status line, and the setting is its
    own — defaulting to claude's, so one export still covers both."""

    def test_the_percentage_is_the_one_the_status_line_shows(self):
        # Measured on codex-cli 0.154: 13,711 of a 258,400 window reads
        # "Context 1% used". Without the baseline it would be 5%.
        ctl = codex_controller(codex_context_tokens=135200)   # armed; 50% of WINDOW
        feed(ctl, 1, window=WINDOW, tokens=13711)
        self.assertEqual(round(ctl.context_pct()), 1)
        claude = codex_controller(agent="claude", context_tokens=129200,
                                  context_window="258400")
        claude.on_context(dict(kind="alive", path=ROLL, tokens=13711,
                               model="claude-opus-5"), 1)
        self.assertEqual(round(claude.context_pct()), 5)

    def test_the_corner_rounds_the_way_the_status_line_does(self):
        # 23,440 of 258,400 is 4.64%: codex says "Context 5% used".
        ctl = codex_controller(codex_context_tokens=24320)   # armed; 5% of WINDOW
        feed(ctl, 1, window=WINDOW, tokens=23440)
        badge = cr.Badge(dict(badge=1, badge_pos="bottom-right", badge_label="cr"))
        text, _ = badge.frame(cr.IDLE, 0, 0, 3, now=0, context=ctl.badge_context())
        self.assertIn("5%", text)

    def test_claudes_absolute_count_alone_leaves_codex_off(self):
        ctl = codex_controller(context_tokens=500000)
        self.assertFalse(ctl.context_enabled)

    def test_codex_may_have_an_absolute_count_of_its_own(self):
        ctl = codex_controller(codex_context_tokens=200000)
        feed(ctl, 1, window=WINDOW)
        self.assertEqual(ctl.context_limit, 200000)

    def test_claude_codes_switches_are_not_codexs(self):
        ctl = codex_controller(codex_context_tokens=135200, context_env_max=100000)
        feed(ctl, 1, window=WINDOW, model="gpt-5.6-sol")
        self.assertEqual(ctl.context_window, WINDOW)

    def test_a_codex_model_is_never_looked_up(self):
        # The model can be named before any turn has stated its window. The
        # lookup reads Anthropic's models table, which has no GPT in it.
        ctl = codex_controller(codex_context_tokens=135200)
        feed(ctl, 1, model="gpt-5.6-sol")
        self.assertIsNone(ctl.window_unknown)
        feed(ctl, 2, window=WINDOW)
        self.assertEqual(ctl.context_window, WINDOW)

    def test_a_per_model_override_reaches_a_dotted_codex_slug(self):
        env = "CR_CODEX_TOKENS_GPT_5_6_SOL"
        os.environ[env] = "140000"
        self.addCleanup(os.environ.pop, env, None)
        ctl = codex_controller(codex_context_tokens=135200)
        feed(ctl, 1, model="gpt-5.6-sol", window=WINDOW)
        self.assertEqual(ctl.context_limit, 140000)

    def test_a_claude_override_does_not_reach_codex(self):
        env = "CR_CLAUDE_TOKENS_GPT_5_6_SOL"
        os.environ[env] = "140000"
        self.addCleanup(os.environ.pop, env, None)
        ctl = codex_controller(codex_context_tokens=135200)
        feed(ctl, 1, model="gpt-5.6-sol", window=WINDOW)
        self.assertNotEqual(ctl.context_limit, 140000)

    def test_the_bare_flag_arms_both_agents_without_a_number(self):
        # The flag alone hands neither agent a percentage anymore — it just
        # arms context_restart_on, and model_restart_at answers with each
        # agent's own profile instead (see
        # test_the_bare_flag_reaches_the_profile_threshold below).
        from helper import load as reload_impl
        mod = reload_impl(CR_CONTEXT_RESTART="1")
        claude_cfg = mod.agent_cfg(mod.CFG, "claude")
        codex_cfg = mod.agent_cfg(mod.CFG, "codex")
        self.assertEqual(claude_cfg["context_tokens"], 0)
        self.assertEqual(codex_cfg["context_tokens"], 0)
        self.assertTrue(claude_cfg["context_restart_on"])
        self.assertTrue(codex_cfg["context_restart_on"])

    def test_the_bare_flag_reaches_the_profile_threshold(self):
        # AC: no CR_CONTEXT_TOKENS of any kind, just the flag — the rollout
        # reporting the profile's own window (258,400) is what arms
        # gpt-5.6-sol's 194,400 (= cap - the default 64k reserve).
        ctl = codex_controller(context_tokens=0, context_restart_on=True)
        feed(ctl, 1, model="gpt-5.6-sol", window=258400)
        self.assertEqual(ctl.context_limit, 194400)
        self.assertEqual(ctl.trigger_limit(), 194400)

    def test_a_raised_window_leaves_the_profile_sitting_out(self):
        # AC: model_context_window=872000 in the rollout is not the window the
        # profile was written for, so the profile does not answer at all —
        # stage 4's default_restart_at decides instead, at the REPORTED window
        # (the usable window minus the reserve the fold needs).
        ctl = codex_controller(context_tokens=0, context_restart_on=True)
        feed(ctl, 1, model="gpt-5.6-sol", window=872000)
        self.assertEqual(ctl.context_limit, 872000 - 64000)


class TestPerAgentMessages(unittest.TestCase):
    """A phrase that names a command cannot travel between agents — claude and
    codex do not recognize the same ones. CR_CLAUDE_<X>/CR_CODEX_<X>, unset by
    default, override CR_<X> for one agent only."""

    def test_unset_both_agents_keep_the_shared_phrase(self):
        mod = load(CR_RESUME_MSG="Read `{file}` and continue.")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "Read `{file}` and continue.")
        self.assertEqual(mod.agent_cfg(mod.CFG, "claude")["resume_msg"],
                         "Read `{file}` and continue.")

    def test_codex_override_does_not_touch_claude(self):
        mod = load(CR_RESUME_MSG="Read `{file}` and continue.",
                  CR_CODEX_RESUME_MSG="@supervisor continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "@supervisor continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "claude")["resume_msg"],
                         "Read `{file}` and continue.")

    def test_claude_override_does_not_touch_codex(self):
        mod = load(CR_RESUME_MSG="Read `{file}` and continue.",
                  CR_CLAUDE_RESUME_MSG="/supervisor continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "claude")["resume_msg"],
                         "/supervisor continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "Read `{file}` and continue.")

    def test_handoff_msg_and_clear_cmd_follow_the_same_rule(self):
        mod = load(CR_CODEX_HANDOFF_MSG="fold it, codex-style: {file} / {marker}",
                  CR_CLAUDE_CLEAR_CMD="/clear-please",
                  CR_CODEX_CANCEL_MSG="never mind, codex")
        codex_cfg = mod.agent_cfg(mod.CFG, "codex")
        claude_cfg = mod.agent_cfg(mod.CFG, "claude")
        self.assertEqual(codex_cfg["handoff_msg"], "fold it, codex-style: {file} / {marker}")
        self.assertEqual(claude_cfg["handoff_msg"], mod.CFG["handoff_msg"])
        self.assertEqual(claude_cfg["clear_cmd"], "/clear-please")
        self.assertEqual(codex_cfg["clear_cmd"], mod.CFG["clear_cmd"])
        self.assertEqual(codex_cfg["cancel_msg"], "never mind, codex")
        self.assertEqual(claude_cfg["cancel_msg"], mod.CFG["cancel_msg"])

    def test_an_empty_override_is_the_same_as_unset(self):
        # The shell's ${VAR:=default} treats an explicitly empty value as unset
        # too, so an override left blank in an rc file must read the same way.
        mod = load(CR_RESUME_MSG="Read `{file}` and continue.", CR_CODEX_RESUME_MSG="")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "Read `{file}` and continue.")

    def test_codex_clear_cmd_default_matches_claudes(self):
        # T15 (live codex-cli 0.155.1): /clear behaves identically on both
        # agents -- same banner, same single-Enter recipe -- so there is no
        # reason for the defaults to diverge.
        mod = load()
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["clear_cmd"], "/clear")
        self.assertEqual(mod.agent_cfg(mod.CFG, "claude")["clear_cmd"], "/clear")


class TestSkillPlaceholder(unittest.TestCase):
    """{skill:NAME} -> the syntax this agent actually sends (T17): "/NAME" for
    claude, "$NAME" for codex. Never the other way around -- codex silently
    drops an unrecognized "/NAME" and never sends it as text at all, while
    "$NAME" is always delivered as plain text (T15)."""

    def test_skill_placeholder_expands_per_agent(self):
        mod = load(CR_RESUME_MSG="{skill:supervisor} continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "claude")["resume_msg"],
                         "/supervisor continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "$supervisor continue from `{file}`")

    def test_skill_placeholder_in_handoff_and_cancel_too(self):
        mod = load(CR_HANDOFF_MSG="Use {skill:wrapup} to fold: {file}/{marker}",
                  CR_CANCEL_MSG="Never mind -- {skill:supervisor} keep going")
        codex_cfg = mod.agent_cfg(mod.CFG, "codex")
        claude_cfg = mod.agent_cfg(mod.CFG, "claude")
        self.assertEqual(codex_cfg["handoff_msg"], "Use $wrapup to fold: {file}/{marker}")
        self.assertEqual(claude_cfg["handoff_msg"], "Use /wrapup to fold: {file}/{marker}")
        self.assertEqual(codex_cfg["cancel_msg"], "Never mind -- $supervisor keep going")
        self.assertEqual(claude_cfg["cancel_msg"], "Never mind -- /supervisor keep going")

    def test_skill_placeholder_does_not_touch_clear_cmd(self):
        # clear_cmd is a literal built-in command, not a phrase template.
        mod = load(CR_CLEAR_CMD="{skill:not-a-thing}")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["clear_cmd"],
                         "{skill:not-a-thing}")

    def test_a_per_agent_override_can_use_the_placeholder_too(self):
        mod = load(CR_RESUME_MSG="Read `{file}` and continue.",
                  CR_CODEX_RESUME_MSG="{skill:supervisor} continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "$supervisor continue from `{file}`")
        self.assertEqual(mod.agent_cfg(mod.CFG, "claude")["resume_msg"],
                         "Read `{file}` and continue.")

    def test_text_without_the_placeholder_is_unchanged(self):
        mod = load(CR_RESUME_MSG="Read `{file}` and continue.")
        self.assertEqual(mod.agent_cfg(mod.CFG, "codex")["resume_msg"],
                         "Read `{file}` and continue.")


class TestACodexRestart(unittest.TestCase):
    """The machine itself is shared with claude. What differs is where its facts
    come from, and the one that was missing was how the folding turn ended."""

    def full(self, ctl, at=1):
        feed(ctl, at, turn="open", window=WINDOW)
        feed(ctl, at, model="gpt-5.6-sol")
        feed(ctl, at, tokens=200000)
        feed(ctl, at, turn="closed", stop_reason="end_turn")

    def test_a_clean_task_complete_lets_the_clear_go_out(self):
        ctl = codex_controller(codex_context_tokens=135200)
        self.full(ctl)
        action = ctl.tick(30)
        self.assertEqual(action[0], "inject")
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        # The folding turn.
        feed(ctl, 31, turn="open", window=WINDOW)
        ctl.handoff.write(ctl, at=40)
        feed(ctl, 40, tokens=201000)
        feed(ctl, 41, turn="closed", stop_reason="end_turn")
        ctl.on_handoff_echo(ROLL, 41)               # T06: claude wrote the phrase back
        self.assertEqual(ctl.tick(70), ("inject", "/clear", False))
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        # The new chat is a new rollout, which confirms the clear took (T08).
        moved(ctl, "/codex/sessions/rollout-new.jsonl", 71)
        self.assertIsNone(ctl.tick(71))
        self.assertEqual(ctl.rstate, cr.CLEARED)

        # ...and it answers small.
        self.assertEqual(ctl.tick(74)[1], "Read `/proj/H.md` and continue from it.")
        feed(ctl, 80, path="/codex/sessions/rollout-new.jsonl", turn="open", window=WINDOW)
        feed(ctl, 82, path="/codex/sessions/rollout-new.jsonl", tokens=14000)
        action = ctl.tick(83)
        self.assertEqual(action[0], "notify")
        self.assertIn("restarted", action[1])

    def test_resume_waits_for_the_screen_to_settle_after_clear(self):
        # No rollout ever appears here, so the only signal available is the
        # screen going quiet -- exactly the case codex hits until "a new
        # rollout" (T08's other confirmation path) is wired up for it too.
        ctl = codex_controller(codex_context_tokens=135200)
        self.full(ctl)
        ctl.tick(30)
        feed(ctl, 31, turn="open", window=WINDOW)
        ctl.handoff.write(ctl, at=40)
        feed(ctl, 40, tokens=201000)
        feed(ctl, 41, turn="closed", stop_reason="end_turn")
        ctl.on_handoff_echo(ROLL, 41)
        self.assertEqual(ctl.tick(70), ("inject", "/clear", False))
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        ctl.on_output("repainting", now=71)      # the TUI is still drawing the new chat
        self.assertIsNone(ctl.tick(72))           # 1s quiet: nowhere near CR_CLEAR_SETTLE_SEC
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)
        self.assertIsNone(ctl.tick(75))           # 4s quiet: still short
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        self.assertIsNone(ctl.tick(76))           # 5s quiet: settled
        self.assertEqual(ctl.rstate, cr.CLEARED)
        self.assertIsNone(ctl.tick(78))           # step_gap(3) since settling not up yet
        self.assertEqual(ctl.tick(79)[1], "Read `/proj/H.md` and continue from it.")

    def test_an_open_turn_holds_the_fold_back_however_quiet_it_is(self):
        # A root waiting on its agents writes nothing for minutes. The byte
        # count would call that idle and type into a running turn.
        ctl = codex_controller(codex_context_tokens=135200)
        feed(ctl, 1, turn="open", window=WINDOW)
        feed(ctl, 1, tokens=200000)
        self.assertIsNone(ctl.tick(600))
        self.assertIn("a turn is still running", " ".join(ctl.log_lines))
        feed(ctl, 601, turn="closed", stop_reason="end_turn")
        self.assertEqual(ctl.tick(630)[0], "inject")

    def test_an_open_folding_turn_is_not_cleared_under(self):
        ctl = codex_controller(codex_context_tokens=135200)
        self.full(ctl)
        ctl.tick(30)
        feed(ctl, 31, turn="open", window=WINDOW)
        ctl.handoff.write(ctl, at=40)          # the file is done, the turn is not
        self.assertIsNone(ctl.tick(200))
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)

    def test_an_aborted_folding_turn_is_not_a_handoff(self):
        ctl = codex_controller(codex_context_tokens=135200, handoff_attempts=1)
        self.full(ctl)
        ctl.tick(30)
        feed(ctl, 31, turn="open", window=WINDOW)
        ctl.handoff.write(ctl, at=40)
        feed(ctl, 41, turn="closed", stop_reason="aborted")
        # The file carries this attempt's marker, so the phrase was delivered
        # and the abort owes the model a cancel (T09), not a plain notify.
        self.assertIsNone(ctl.tick(70))
        self.assertIn("stop_reason=aborted", ctl.log_lines[-1])
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)

    def test_a_new_rollout_forgets_the_old_turn(self):
        ctl = codex_controller(codex_context_tokens=135200)
        feed(ctl, 1, turn="open", window=WINDOW)
        moved(ctl, "/codex/sessions/rollout-new.jsonl", 2)
        feed(ctl, 2, path="/codex/sessions/rollout-new.jsonl", tokens=1000)
        self.assertIsNone(ctl.turn_open)


REAL_ROW = ("session_loop{thread_id=01a09fb0}:run_turn: post sampling token usage "
            "turn_id=01a09fb9 total_usage_tokens=251023 auto_compact_scope_tokens=251023 "
            "auto_compact_scope_limit=Some(244800) auto_compact_limit_scope=Total "
            "auto_compact_window_prefill_tokens=None full_context_window_limit=Some(258400) "
            "full_context_window_limit_reached=false token_limit_reached=true "
            "model_needs_follow_up=true")
HELD_ROW = ("post sampling token usage turn_id=01a09feb total_usage_tokens=13486 "
            "auto_compact_scope_tokens=5 auto_compact_scope_limit=Some(1000000000) "
            "auto_compact_limit_scope=BodyAfterPrefix auto_compact_window_prefill_tokens=Some(13481) "
            "full_context_window_limit=Some(258400) full_context_window_limit_reached=false")


class TestCodexsOwnCount(unittest.TestCase):
    """codex decides to compact on a count of its own, larger than anything the
    rollout shows, and writes it to its log database. Both rows below are copied
    from a real ~/.codex/logs_2.sqlite on codex-cli 0.154."""

    def test_the_default_scope_compacts_at_ninety_percent_of_the_raw_window(self):
        self.assertEqual(cr.parse_usage_row(REAL_ROW), (251023, 244800))

    def test_with_the_threshold_held_back_only_the_hard_cap_is_left(self):
        self.assertEqual(cr.parse_usage_row(HELD_ROW), (13486, 258400))

    def test_other_rows_are_not_usage(self):
        self.assertIsNone(cr.parse_usage_row("turn{model=gpt-5.6-sol}: sampling request sent"))
        self.assertIsNone(cr.parse_usage_row(None))

    def test_the_database_is_followed_one_thread_at_a_time(self):
        import sqlite3
        home = tempfile.mkdtemp(prefix="cr-codex-logs-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        db = sqlite3.connect(os.path.join(home, "logs_2.sqlite"))
        db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, "
                   "feedback_log_body TEXT, thread_id TEXT)")
        add = lambda body, thread: (db.execute(
            "INSERT INTO logs (ts, feedback_log_body, thread_id) VALUES (0, ?, ?)",
            (body, thread)), db.commit())
        add(HELD_ROW.replace("13486", "9000"), "mine")
        add(HELD_ROW, "mine")
        add(REAL_ROW, "someone-else")
        logs = []
        follow = cr.CodexUsageLog(dict(poll=0), logs.append, home=home)
        # First look: only the newest row, not a resumed thread's whole history.
        self.assertEqual(follow.poll("mine", 1), [(13486, 258400)])
        self.assertEqual(follow.poll("mine", 2), [])
        add(HELD_ROW.replace("13486", "20000"), "mine")
        self.assertEqual(follow.poll("mine", 3), [(20000, 258400)])

    def test_no_database_is_no_reading(self):
        follow = cr.CodexUsageLog(dict(poll=0), lambda *_: None, home="/nonexistent")
        self.assertEqual(follow.poll("mine", 1), [])

    def test_a_compacted_row_is_reported(self):
        d = tempfile.mkdtemp(prefix="cr-codex-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        got = cr.codex_records(rollout(d, rows=[("compacted", {"message": ""})]), 0)[1]
        self.assertTrue(got[0]["compacted"])
        self.assertTrue(got[0]["quiet"])


class TestHoldingCodexsCompactionBack(unittest.TestCase):
    def test_codex_is_started_with_its_threshold_moved(self):
        args = cr.codex_launch_args(dict(agent="codex", context_tokens=159840,
                                         codex_hold_compact=True))
        self.assertIn('model_auto_compact_token_limit_scope="body_after_prefix"', args)
        self.assertEqual(args[0], "-c")

    def test_not_without_a_restart_to_make_room_for(self):
        self.assertEqual(cr.codex_launch_args(dict(agent="codex",
                                                   context_tokens=0)), [])

    def test_the_bare_flag_makes_room_too(self):
        # T18: CR_CONTEXT_RESTART=1 alone arms the model's profile, not a
        # percentage — codex's own compaction still has to be moved out of the
        # way for that threshold to mean anything.
        args = cr.codex_launch_args(dict(agent="codex", context_tokens=0,
                                         context_restart_on=True))
        self.assertIn('model_auto_compact_token_limit_scope="body_after_prefix"', args)

    def test_not_when_told_not_to(self):
        self.assertEqual(cr.codex_launch_args(dict(agent="codex", context_tokens=159840,
                                                   codex_hold_compact=False)), [])

    def test_never_for_claude(self):
        self.assertEqual(cr.codex_launch_args(dict(agent="claude", context_tokens=159840)), [])


def logged_usage(ctl, at, tokens, cap=258400, path=ROLL):
    ctl.on_context(dict(kind="alive", source="log", path=path, sidechain=False,
                        tokens=tokens, cap=cap), at)


class TestStayingAheadOfCodex(unittest.TestCase):
    def ctl(self, **over):
        cfg = dict(codex_context_tokens=159840, codex_reserve=32000, codex_interrupt=True)
        cfg.update(over)
        ctl = codex_controller(**cfg)
        feed(ctl, 0, window=WINDOW, model="gpt-5.6-sol")
        return ctl

    def test_codexs_count_replaces_the_rollouts(self):
        ctl = self.ctl()
        logged_usage(ctl, 1, 150000)
        feed(ctl, 2, tokens=132000)             # the rollout's smaller figure, later
        self.assertEqual(ctl.context_tokens, 150000)

    def test_the_threshold_never_sits_past_the_interrupt_line(self):
        ctl = self.ctl(codex_context_tokens=246080)
        logged_usage(ctl, 1, 1000)
        self.assertEqual(ctl.interrupt_line(), 258400 - 32000)
        self.assertEqual(ctl.trigger_limit(), 258400 - 32000)
        self.assertIn("past the point codex would compact first", " ".join(ctl.log_lines))

    def test_a_running_turn_is_left_alone_under_the_line(self):
        ctl = self.ctl()
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 200000)            # past 60%, short of 226k
        self.assertIsNone(ctl.tick(100))

    def test_a_running_turn_past_the_line_is_interrupted(self):
        ctl = self.ctl()
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 230000)
        action = ctl.tick(100)
        self.assertEqual(action[0], "interrupt")
        self.assertIsNone(ctl.tick(105))        # one Esc, then time for it to land
        # The Esc lands: turn_aborted. The fold goes out once the rollout is quiet.
        feed(ctl, 106, turn="closed", stop_reason="aborted")
        self.assertIsNone(ctl.tick(110))
        action = ctl.tick(130)
        self.assertEqual(action[0], "inject")
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)

    def test_nobody_is_interrupted_mid_sentence(self):
        ctl = self.ctl()
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 230000)
        ctl.on_user_bytes(b"wait", 99)
        self.assertIsNone(ctl.tick(100))

    def test_interrupting_can_be_switched_off(self):
        ctl = self.ctl(codex_interrupt=False)
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 230000)
        self.assertIsNone(ctl.tick(100))

    def test_codex_getting_there_first_is_said_and_ends_the_restart(self):
        ctl = self.ctl()
        logged_usage(ctl, 1, 230000)
        ctl.tick(30)
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        feed(ctl, 40, compacted=True)
        self.assertIsNone(ctl.rstate)
        self.assertEqual(ctl.self_compactions, 1)
        self.assertIn("compacted the thread on its own", " ".join(ctl.log_lines))

    def test_codex_getting_there_first_does_not_lose_a_landed_handoff(self):
        ctl = self.ctl()
        logged_usage(ctl, 1, 230000)
        ctl.tick(30)
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        ctl.handoff.write(ctl, 35)               # the file is already valid...
        feed(ctl, 36, turn="closed", stop_reason="end_turn")
        feed(ctl, 40, compacted=True)            # ...when codex compacts first
        self.assertEqual(ctl.rstate, cr.CLEARED)
        self.assertEqual(ctl.self_compactions, 1)
        self.assertIn("skipping straight to unfold", " ".join(ctl.log_lines))
        action = ctl.tick(65)                    # past root_idle, session quiet
        self.assertEqual(action[0], "inject")
        self.assertEqual(ctl.rstate, cr.RESUME_SENT)

    def test_codex_getting_there_first_with_no_handoff_still_aborts(self):
        ctl = self.ctl()
        logged_usage(ctl, 1, 230000)
        ctl.tick(30)
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        feed(ctl, 40, compacted=True)            # no file was ever written
        self.assertIsNone(ctl.rstate)
        self.assertEqual(ctl.self_compactions, 1)
        self.assertIn("compacted the thread on its own", " ".join(ctl.log_lines))

    def test_codex_getting_there_first_after_a_delivered_fold_asks_it_to_carry_on(self):
        # T09: unlike the two cases above, the phrase DID reach the session --
        # codex just compacted its own context before a usable file followed
        # it -- so the abort has to say the ask is off, not just walk away.
        ctl = self.ctl()
        logged_usage(ctl, 1, 230000)
        ctl.tick(30)
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        ctl.on_handoff_echo(ROLL, 35)
        feed(ctl, 40, compacted=True)            # still no usable file when it compacted
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)
        self.assertEqual(ctl.self_compactions, 1)
        self.assertIn("restart aborted at handoff_sent", " ".join(ctl.log_lines))

        action = ctl.tick(65)                    # past root_idle, session quiet
        self.assertEqual(action[0], "inject")
        self.assertIn("cancelled", action[1])
        self.assertIn("asking the session to carry on", " ".join(ctl.log_lines))

    def test_without_the_log_the_rollout_still_counts(self):
        ctl = self.ctl()
        feed(ctl, 1, tokens=200000)
        self.assertEqual(ctl.context_tokens, 200000)
        self.assertIsNone(ctl.interrupt_line())
        self.assertEqual(ctl.trigger_limit(), ctl.context_limit)


class TestInterruptingAStuckTurnByTime(unittest.TestCase):
    """T22: the threshold between turns is nearly irrelevant on its own — a
    codex orchestrator turn can run for hours, and without this the only real
    restart is the cap-reserve line in TestStayingAheadOfCodex above.
    CR_CODEX_INTERRUPT_AFTER_SEC interrupts a turn that has simply sat past the
    ordinary threshold (trigger_limit) too long, whether or not it ever reaches
    that harder line."""

    def ctl(self, **over):
        cfg = dict(codex_context_tokens=159840, codex_reserve=32000, codex_interrupt=True)
        cfg.update(over)
        ctl = codex_controller(**cfg)
        feed(ctl, 0, window=WINDOW, model="gpt-5.6-sol")
        return ctl

    def test_a_turn_stuck_over_the_threshold_is_interrupted_after_the_timeout(self):
        ctl = self.ctl(codex_interrupt_after_sec=600)
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 200000)            # past the 159840 threshold, short of 226400
        self.assertIsNone(ctl.tick(2))          # just crossed; nowhere near 600s yet
        self.assertIsNone(ctl.tick(500))        # under 600s since crossing
        action = ctl.tick(2 + 700)
        self.assertEqual(action[0], "interrupt")

    def test_the_default_of_zero_never_interrupts_by_time_alone(self):
        # Regression: without CR_CODEX_INTERRUPT_AFTER_SEC, behavior is
        # unchanged from before T22 — only the cap-reserve line interrupts,
        # however long the turn sits above the ordinary threshold.
        ctl = self.ctl()
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 200000)
        self.assertIsNone(ctl.tick(2))
        self.assertIsNone(ctl.tick(100000))

    def test_a_new_turn_resets_the_clock(self):
        ctl = self.ctl(codex_interrupt_after_sec=600)
        feed(ctl, 1, turn="open")
        logged_usage(ctl, 2, 200000)
        self.assertIsNone(ctl.tick(2))
        feed(ctl, 400, turn="closed", stop_reason="end_turn")
        feed(ctl, 401, turn="open")
        # 700s since the FIRST crossing, but this turn only just opened.
        self.assertIsNone(ctl.tick(702))


class TestFoldCostAndReserve(unittest.TestCase):
    """T22: the wrapper already knows what a fold turn costs -- log it, keep a
    durable history in codex_folds_file, recommend a bigger reserve once a
    fold burns through most of it, and (CR_CODEX_RESERVE_ADAPT=1) raise the
    effective reserve itself once that happens."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-codex-folds-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.folds_file = os.path.join(self.dir, "folds.json")

    def ctl(self, **over):
        cfg = dict(codex_context_tokens=135200, codex_reserve=64000, codex_folds_file=self.folds_file)
        cfg.update(over)
        ctl = codex_controller(**cfg)
        feed(ctl, 0, window=WINDOW, model="gpt-5.6-sol")
        return ctl

    def _fold(self, ctl, before, after):
        """Drive one full codex fold cycle through to the handoff being
        accepted -- the same steps as TestACodexRestart.full/
        test_a_clean_task_complete_lets_the_clear_go_out, with `logged_usage`
        standing in for the plain rollout tokens so codex_cap is wired up too.
        """
        feed(ctl, 1, turn="open", window=WINDOW)
        logged_usage(ctl, 1, before, cap=WINDOW)
        feed(ctl, 1, turn="closed", stop_reason="end_turn")
        self.assertEqual(ctl.tick(30)[0], "inject")
        feed(ctl, 31, turn="open", window=WINDOW)
        ctl.handoff.write(ctl, at=40)
        logged_usage(ctl, 40, after, cap=WINDOW)
        feed(ctl, 41, turn="closed", stop_reason="end_turn")
        ctl.on_handoff_echo(ROLL, 41)
        self.assertEqual(ctl.tick(70), ("inject", "/clear", False))

    def test_the_fold_cost_is_logged_and_recorded(self):
        ctl = self.ctl()
        self._fold(ctl, 200000, 231000)         # 31k, the backlog's own example
        self.assertIn("the fold cost 31k tokens (reserve 64k)", ctl.log_lines)
        with open(self.folds_file) as fh:
            records = json.load(fh)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["agent"], "codex")
        self.assertEqual(records[0]["cost"], 31000)
        self.assertIn("cwd", records[0])
        self.assertIn("date", records[0])

    def test_a_missing_or_corrupt_file_is_started_fresh(self):
        os.makedirs(os.path.dirname(self.folds_file), exist_ok=True)
        with open(self.folds_file, "w") as fh:
            fh.write("{not valid json")
        ctl = self.ctl()
        self._fold(ctl, 200000, 210000)
        with open(self.folds_file) as fh:
            records = json.load(fh)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["cost"], 10000)

    def test_a_heavy_fold_recommends_raising_the_reserve(self):
        ctl = self.ctl()
        self._fold(ctl, 200000, 255000)         # 55k against a 64k reserve
        self.assertIn("the fold cost more than 80% of the 64k reserve; "
                      "CR_CODEX_RESERVE_TOKENS could stand to be raised",
                      ctl.log_lines)

    def test_a_light_fold_recommends_nothing(self):
        ctl = self.ctl()
        self._fold(ctl, 200000, 210000)         # 10k, well under 80% of 64k
        self.assertFalse(any("could stand to be raised" in l for l in ctl.log_lines))

    def test_reserve_adapt_raises_the_effective_reserve(self):
        # 246080 (95% of WINDOW): high enough that trigger_limit is pinned to
        # the interrupt line itself, so both reflect the adapted reserve.
        ctl = self.ctl(codex_reserve_adapt=True, codex_context_tokens=246080)
        self._fold(ctl, 200000, 255000)         # 55k cost
        expected_reserve = max(64000, int(1.25 * 55000))
        self.assertEqual(ctl.interrupt_line(), WINDOW - expected_reserve)
        self.assertEqual(ctl.trigger_limit(), WINDOW - expected_reserve)

    def test_without_the_flag_the_reserve_never_moves(self):
        ctl = self.ctl()
        self._fold(ctl, 200000, 255000)         # same heavy fold, no adapt
        self.assertEqual(ctl.interrupt_line(), WINDOW - 64000)

    def test_a_fresh_session_benefits_from_an_earlier_ones_history(self):
        # The reserve this instance never folded to earn is still there once a
        # PREVIOUS session already wrote it to codex_folds_file (T22: "durably").
        cr._append_fold_record(self.folds_file, "codex", "/some/project", 90000, 1)
        ctl = self.ctl(codex_reserve_adapt=True)
        logged_usage(ctl, 1, 1000, cap=WINDOW)  # only to set codex_cap
        expected_reserve = int(1.25 * 90000)
        self.assertEqual(ctl.interrupt_line(), WINDOW - expected_reserve)


class TestModelSwitchResetsTheCodexCap(unittest.TestCase):
    """T21: `codex_cap` is read live off the rollout's own usage log and is
    specific to whichever model wrote it. A model switch — named through
    `turn_context`/`thread_settings_applied`, on the very next turn — has to
    blank it until a fresh usage line for the NEW model arrives, or
    `interrupt_line()` goes on answering for a model that is no longer
    running."""

    def ctl(self, **over):
        cfg = dict(codex_context_tokens=159840, codex_reserve=32000)
        cfg.update(over)
        ctl = codex_controller(**cfg)
        feed(ctl, 0, window=WINDOW, model="gpt-5.6-sol")
        logged_usage(ctl, 1, 150000)
        return ctl

    def test_turn_context_with_a_new_model_blanks_the_cap_until_a_fresh_line(self):
        ctl = self.ctl()
        self.assertEqual(ctl.codex_cap, 258400)
        self.assertEqual(ctl.interrupt_line(), 258400 - 32000)

        logged_before = len(ctl.log_lines)
        feed(ctl, 2, model="gpt-5.6-terra")      # the next turn's model
        self.assertIsNone(ctl.codex_cap)
        self.assertIsNone(ctl.interrupt_line())
        switched = ctl.log_lines[logged_before:]
        self.assertTrue(any("model switched: gpt-5.6-sol" in l
                            and "gpt-5.6-terra" in l and "[codex log]" in l
                            for l in switched), switched)

        logged_usage(ctl, 3, 50000, cap=258400)
        self.assertEqual(ctl.codex_cap, 258400)
        self.assertEqual(ctl.interrupt_line(), 258400 - 32000)

    def test_thread_settings_applied_reaches_on_model_the_same_way(self):
        ctl = self.ctl()
        path = rollout(tempfile.mkdtemp(prefix="cr-t21-"),
                       rows=[("event_msg", {"type": "thread_settings_applied",
                                           "thread_settings": {"model": "gpt-5.6-luna"}})])
        _, recs = cr.codex_records(path, 0)
        hits = [r for r in recs if r.get("model") == "gpt-5.6-luna"]
        self.assertEqual(len(hits), 1)
        feed(ctl, 2, model="gpt-5.6-luna")
        self.assertIsNone(ctl.codex_cap)


class TestFindingAStallOnScreen(unittest.TestCase):
    def test_the_capacity_line_is_found(self):
        self.assertEqual(cr.find_stall("⚠ " + CAPACITY), "⚠ " + CAPACITY)

    def test_ordinary_output_is_not_a_stall(self):
        self.assertIsNone(cr.find_stall("• Ran 3 commands · ctrl + t to view transcript"))

    def test_a_tool_call_quoting_the_words_is_not_one(self):
        screen = "● Bash(grep -r 'model is at capacity' .)\n  ⎿  selected model is at capacity"
        self.assertIsNone(cr.find_stall(screen))

    def test_our_own_documentation_is_not_one(self):
        self.assertIsNone(cr.find_stall("  CR_STALL_PATTERNS  selected model is at capacity"))


def _fake_codex_launcher():
    """A single-exec launcher, named codex so the auto-detection has something
    to work from — which is the same thing it has in a real install."""
    path = os.path.join(tempfile.mkdtemp(prefix="cr-fake-codex-"), "codex")
    with open(path, "w") as fh:
        fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n'
                 % (sys.executable, os.path.join(ROOT, "test", "fake_codex.py")))
    os.chmod(path, 0o755)
    return path


FAKE_CODEX = _fake_codex_launcher()


class TestCodexSubcommands(unittest.TestCase):
    """Most of what `codex` can be asked to do is not a session at all, and a pty
    supervisor around one of those is a pty for nothing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-codex-sub-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = os.path.join(self.dir, "log")

    def run_wrapper(self, args):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CR_")
               and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}
        env.update(hermetic_env(self.dir))
        env.update({"CR_CLAUDE_BIN": FAKE_CODEX, "CR_LOG": self.log,
                    "CODEX_HOME": self.dir, "CR_NOTIFY": "0"})
        return subprocess.run([os.path.join(ROOT, "agent-retrier.sh"), *args],
                              env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def supervised(self):
        """Did the pty supervisor run? It is the only thing that logs."""
        if not os.path.exists(self.log):
            return False
        with open(self.log) as fh:
            return "start:" in fh.read()

    def test_exec_runs_codex_directly(self):
        r = self.run_wrapper(["exec", "do the thing"])
        self.assertIn("fake-codex ready", r.stdout)
        self.assertFalse(self.supervised())

    def test_login_runs_codex_directly(self):
        self.run_wrapper(["login"])
        self.assertFalse(self.supervised())

    def test_a_flag_before_the_subcommand_is_stepped_over(self):
        self.run_wrapper(["--cd", self.dir, "exec"])
        self.assertFalse(self.supervised())

    def wrapped(self, args, timeout=15):
        """Start the wrapper and wait for the supervisor to announce itself.

        A wrapped session does not end on its own — it is sitting at a prompt on
        a pty, which is the whole point — so this asks the question and then
        stops it.
        """
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CR_")
               and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}
        env.update(hermetic_env(self.dir))
        env.update({"CR_CLAUDE_BIN": FAKE_CODEX, "CR_LOG": self.log,
                    "CODEX_HOME": self.dir, "CR_NOTIFY": "0"})
        proc = subprocess.Popen([os.path.join(ROOT, "agent-retrier.sh"), *args],
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            deadline = time.time() + timeout
            while time.time() < deadline and not self.supervised():
                time.sleep(0.1)
            return self.supervised()
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_resume_is_a_session_and_is_wrapped(self):
        self.assertTrue(self.wrapped(["resume"]))

    def test_a_bare_prompt_is_a_session_and_is_wrapped(self):
        # And a prompt that happens to name a subcommand is still a prompt.
        self.assertTrue(self.wrapped(["exec the plan and report back"]))


class TestCalledAsCodex(unittest.TestCase):
    """`agent-retrier-codex` is this same file under another name, with codex as
    the default. The install puts the name there whether or not codex is, so the
    name has to cope with a machine that has only claude."""

    NAME = "agent-retrier-codex"
    FLAGS = []

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-codex-name-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = os.path.join(self.dir, "log")
        self.prog = os.path.join(self.dir, self.NAME)
        os.symlink(os.path.join(ROOT, "agent-retrier.sh"), self.prog)
        self.codex_dir = os.path.dirname(FAKE_CODEX)     # holds an executable `codex`

    def call(self, args, path=None, **over):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CR_")
               and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}
        env.update({"PATH": path or (self.codex_dir + ":/usr/bin:/bin"),
                    "SHELL": "/bin/sh", "CR_LOG": self.log, "CODEX_HOME": self.dir,
                    "CR_NOTIFY": "0", "CR_UPDATE_CHECK": "0", "CR_TMP_SWEEP": "0"})
        env.update(over)
        return subprocess.run([self.prog, *self.FLAGS, *args], env=env,
                              stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def test_it_runs_codex(self):
        r = self.call(["--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), FAKE_CODEX)

    def test_claudes_command_is_not_its_command(self):
        # CR_CLAUDE_CMD lives in people's rc files, for claude.
        r = self.call(["--cr-dump-argv"], CR_CLAUDE_CMD="claude-work")
        self.assertEqual(r.stdout.strip(), FAKE_CODEX)

    def test_cr_codex_cmd_names_yours(self):
        mine = os.path.join(self.dir, "codex-work")
        shutil.copy(FAKE_CODEX, mine)
        r = self.call(["--cr-dump-argv"], CR_CODEX_CMD=mine)
        self.assertEqual(r.stdout.strip(), mine)

    def test_cmd_still_wins(self):
        mine = os.path.join(self.dir, "codex-work")
        shutil.copy(FAKE_CODEX, mine)
        r = self.call(["--cmd", mine, "--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), mine)

    def test_a_machine_with_no_codex_is_told_so(self):
        r = self.call([], path="/usr/bin:/bin")
        self.assertEqual(r.returncode, 127)
        self.assertIn("agent-retrier-codex: codex not found on PATH", r.stderr)

    def test_the_agent_is_codex_whatever_the_command_is_called(self):
        # Nothing in `agent-work` says codex. The name the wrapper was called by
        # does, which is what sends `exec` straight through unwrapped.
        mine = os.path.join(self.dir, "agent-work")
        shutil.copy(FAKE_CODEX, mine)
        r = self.call(["exec", "do the thing"], CR_CODEX_CMD=mine)
        self.assertIn("fake-codex ready", r.stdout)
        self.assertFalse(os.path.exists(self.log))


class TestCodexFlag(TestCalledAsCodex):
    """`agent-retrier --codex` is the same thing as the `agent-retrier-codex`
    name."""

    NAME = "agent-retrier"
    FLAGS = ["--codex"]

    def test_cmd_before_the_flag_still_wins(self):
        mine = os.path.join(self.dir, "codex-work")
        shutil.copy(FAKE_CODEX, mine)
        env = {"PATH": self.codex_dir + ":/usr/bin:/bin"}
        r = subprocess.run([self.prog, "--cmd", mine, "--codex", "--cr-dump-argv"],
                           env={**os.environ, **env, "CR_UPDATE_CHECK": "0"},
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.stdout.strip(), mine)


class TestClaudeName(unittest.TestCase):
    """`agent-retrier-claude` and `--claude` pick claude, the pair to the codex
    name and flag; the last flag given wins."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-claude-name-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.claude = os.path.join(self.bin, "claude")
        with open(self.claude, "w") as f:
            f.write("#!/bin/sh\necho claude\n")
        os.chmod(self.claude, 0o755)
        shutil.copy(FAKE_CODEX, os.path.join(self.bin, "codex"))

    def call(self, name, args):
        prog = os.path.join(self.dir, name)
        if not os.path.exists(prog):
            os.symlink(os.path.join(ROOT, "agent-retrier.sh"), prog)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CR_")
               and k not in ("AGENT_RETRIER_ACTIVE", "CLAUDE_CONFIG_DIR")}
        env.update({"PATH": self.bin + ":/usr/bin:/bin", "SHELL": "/bin/sh",
                    "CR_LOG": os.path.join(self.dir, "log"), "CR_NOTIFY": "0",
                    "CR_UPDATE_CHECK": "0", "CR_TMP_SWEEP": "0"})
        return subprocess.run([prog, *args], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def test_the_claude_name_runs_claude(self):
        r = self.call("agent-retrier-claude", ["--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), self.claude)

    def test_the_claude_flag_runs_claude(self):
        r = self.call("agent-retrier", ["--claude", "--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), self.claude)

    def test_the_last_flag_wins(self):
        r = self.call("agent-retrier", ["--codex", "--claude", "--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), self.claude)
        r = self.call("agent-retrier-codex", ["--claude", "--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), self.claude)
        r = self.call("agent-retrier-claude", ["--codex", "--cr-dump-argv"])
        self.assertEqual(r.stdout.strip(), os.path.join(self.bin, "codex"))


class TestCodexEndToEnd(PtyTestCase):
    """The whole thing on a pty: a rollout file the wrapper follows, a turn that
    dies in it, and `continue` typed into the session that was left sitting."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cr-codex-home-")
        self.work = tempfile.mkdtemp(prefix="cr-codex-work-")
        self.log = os.path.join(self.home, "log")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def env(self, **over):
        e = {"CR_CLAUDE_BIN": FAKE_CODEX,
             "CODEX_HOME": self.home,
             "CR_LOG": self.log,
             "CR_SCRAPE": "never",           # prove the rollout alone is enough
             "CR_WAIT_SCALE": "60",          # a minute of waiting becomes a second
             "CR_USER_IDLE_SEC": "0",
             "CR_POLL_SEC": "0.2"}
        e.update(over)
        return e

    def logged(self):
        try:
            with open(self.log) as fh:
                return fh.read()
        except OSError:
            return ""

    def wait_for_log(self, needle, session, timeout=20):
        """The log is the durable record; with the notice line off it is also the
        only place a detection shows up."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.logged():
                return True
            session.drain(0.05)
        return False

    def logged_by(self, session):
        """One wrapper's own lines, when two of them share this test's log."""
        tag = "[cr %d " % session.proc.pid
        return "\n".join(l for l in self.logged().splitlines() if tag in l)

    def caught_up(self, s):
        """Whatever the fake has written to its rollout, the wrapper has now
        read: one poll interval, so that the next turn of its loop has to
        poll, and then a line it can only answer by taking that turn (see
        `Session.roundtrip`). Whatever it made of what it read is in the log.
        """
        time.sleep(0.2)                     # CR_POLL_SEC, as `env` sets it
        self.assertTrue(s.roundtrip(), s.buf[-500:])

    def test_a_capacity_error_is_answered_with_the_message(self):
        s = self.session(env=self.env(FAKE_STALL=CAPACITY), cwd=self.work)
        self.assertTrue(s.read_until("GOT:continue", timeout=25))
        # The two lines the "left alone" tests below look for and must not find.
        self.assertIn("stall detected (transcript)", self.logged())
        self.assertIn("sending retry", self.logged())

    def test_the_agent_is_worked_out_from_the_command(self):
        s = self.session(env=self.env(FAKE_STALL=CAPACITY), cwd=self.work)
        s.read_until("GOT:continue", timeout=25)
        self.assertIn("agent: codex", self.logged())

    def test_a_usage_limit_in_the_rollout_is_waited_out(self):
        s = self.session(
            env=self.env(FAKE_TRANSCRIPT="You've hit your usage limit. Try again at "
                                         "10:00 AM.",
                         CR_MARGIN_SEC="0"),
            cwd=self.work)
        self.assertTrue(self.wait_for_log("limit detected (transcript)", s))
        self.assertIn("resets in", self.logged())

    def test_a_subagents_rollout_is_left_alone(self):
        # Same error, written by a rollout that belongs to a subagent. Acting on
        # it would type into a session that never stopped.
        s = self.session(env=self.env(FAKE_STALL=CAPACITY, FAKE_SUBAGENT="1"),
                         cwd=self.work)
        self.assertTrue(s.read_until("ready", timeout=10))
        # The fake writes the refused turn before it reads its first line, so
        # an answer to anything means the row is in the rollout — and the
        # second answer, a poll later, that the wrapper has been past it. A
        # wrapper that took it for its own has said so by now; the nudge it
        # would then type is still a second away, which is why this looks at
        # the decision and not at the keystrokes.
        self.assertTrue(s.roundtrip(), s.buf[-500:])
        self.caught_up(s)
        self.assertNotIn("stall detected", self.logged())
        self.assertNotIn("GOT:continue", s.buf)

    def test_an_ordinary_session_is_left_alone(self):
        s = self.session(env=self.env(), cwd=self.work)
        self.assertTrue(s.read_until("ready", timeout=10))
        s.send("hello there\r")
        self.assertTrue(s.read_until("GOT:hello there", timeout=10))
        self.caught_up(s)
        for decision in ("stall detected", "limit detected", "sending retry"):
            self.assertNotIn(decision, self.logged())
        self.assertNotIn("GOT:continue", s.buf)

    def restart_env(self, **over):
        # Codex never inherits CR_CONTEXT_TOKENS, so its own knob is what
        # actually arms every test in this class (they all run codex, never
        # claude) — 135200 is 50% of the 258,400 default FAKE_WINDOW, by the
        # same base+(window-base)*pct/100 formula the old percentage used.
        #
        # CR_CLEAR_SETTLE_SEC is codex's alone, and is the one clock here with
        # no claude counterpart: nothing names codex a new session after
        # `/clear`, so the screen going quiet for this long is the only
        # confirmation the wrapper ever gets, and at the 5s default every
        # restart in this class sat it out in full. It has to stay longer than
        # the two Enters a slash command takes to go out (CR_SLASH_GAP_SEC +
        # CR_SLASH_ENTER_GAP_SEC, 0.2s), or the screen would be called quiet
        # before the command was submitted at all.
        e = restart_timers(CR_CODEX_CONTEXT_TOKENS="135200",
                           CR_CLEAR_SETTLE_SEC="0.6",
                           FAKE_USAGE="200000")
        e.update(over)
        return self.env(**e)

    def test_a_full_context_is_folded_cleared_and_unfolded(self):
        s = self.session(env=self.restart_env(), cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_for_log("context restarted", s), self.logged())
        self.assertIn("handoff accepted", self.logged())
        with open(os.path.join(self.work, "handoff.md")) as fh:
            self.assertTrue(fh.read().rstrip().split("\n")[-1].startswith("HANDOFF-"))

    def test_codex_has_a_threshold_of_its_own(self):
        # claude's is off (CR_CONTEXT_TOKENS unset); codex's own knob alone
        # arms it — CR_CONTEXT_TOKENS is never carried over to codex anyway.
        s = self.session(env=self.restart_env(), cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertIn("context restart armed", self.logged())
        self.assertIn("135200 tokens", self.logged())

    def test_codex_resume_msg_can_be_overridden_for_codex_alone(self):
        # The env var has to survive the round trip through the bash wrapper —
        # declared, exported, and read back out of os.environ by the python it
        # hands the pty to — not just be understood by agent_cfg() in isolation.
        s = self.session(env=self.restart_env(
            CR_CODEX_RESUME_MSG="@supervisor continue from `{file}`",
            FAKE_RESUME_MATCH="continue from `"),
            cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertIn("@supervisor continue from", s.buf)

    def test_a_skill_placeholder_reaches_codex_as_a_dollar_prefix(self):
        # T17 end to end, through bash's `:=` defaults and agent_cfg both: one
        # SHARED CR_RESUME_MSG (no per-agent override at all) has to arrive at
        # codex as "$supervisor", the only prefix it actually delivers -- an
        # unrecognized "/supervisor" would be dropped inline and never sent
        # (T15, live codex-cli 0.155.1). Braces in the value must also survive
        # the round trip untouched.
        s = self.session(env=self.restart_env(
            CR_RESUME_MSG="{skill:supervisor} continue from `{file}`",
            FAKE_RESUME_MATCH="continue from `"),
            cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertIn("$supervisor continue from", s.buf)
        self.assertNotIn("{skill:supervisor}", s.buf)

    def test_the_restart_flag_reaches_codex_through_bash_too(self):
        # Same round trip as the claude version: CR_CONTEXT_RESTART has to
        # survive bash's own `:=` defaults, not just agent_cfg()'s in-process
        # fallback. Codex does NOT inherit claude's 51% here, though — the
        # flag arms gpt-5.6-sol's own profile row instead (T18), which answers
        # 194,400 (= the 258,400 cap minus the default 64k reserve) directly,
        # matching what the log-based cap/reserve mechanism would have clamped
        # a percentage down to anyway. With the profile already at that number,
        # `_note_cap`'s "using X instead" only logs when something ABOVE it
        # needed clamping — nothing does here, so it is silent, which is
        # itself confirmation nothing is racing past the reserve line.
        s = self.session(env=self.restart_env(CR_CODEX_CONTEXT_TOKENS="",
                                              CR_CONTEXT_RESTART="1",
                                              FAKE_LOG="1"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertIn("restarting at 194k", self.logged())   # cap(258k) - reserve(64k)
        self.assertNotIn("using 194k instead", self.logged())   # nothing needed clamping

    # A per-model override's own round trip through bash is covered by the
    # claude version above (test_a_per_model_override_is_read_straight_from_the_
    # environment in test_pty.py) — the mechanism is agent-agnostic. codex's own
    # slug-matching is covered directly against the controller in
    # TestTheCodexThreshold (test_a_per_model_override_reaches_a_dotted_codex_
    # slug).

    def test_and_it_can_be_off_while_claudes_is_on(self):
        # CR_CONTEXT_TOKENS never carries over to codex on its own (by
        # design), so setting it alongside an empty CR_CODEX_CONTEXT_TOKENS is
        # what "on for claude, off for codex" looks like now that there is no
        # shared percentage to inherit — this session runs codex, and codex
        # alone is what must stay off.
        s = self.session(env=self.restart_env(CR_CONTEXT_TOKENS="135200",
                                              CR_CODEX_CONTEXT_TOKENS="",
                                              FAKE_BANNER=USAGE_DOWN),
                         cwd=self.work)
        # 200k of usage is on file, well past claude's 135200 — and from the
        # moment the second copy of it lands, a wrapper that had armed itself
        # is LEFT_ALONE away from having typed the fold.
        self.assertTrue(s.read_until(USAGE_DOWN, timeout=10), s.buf[-500:])
        s.drain(LEFT_ALONE)
        self.assertNotIn("GOT:handoff", s.buf)
        self.assertNotIn("context restart armed", self.logged())
        self.assertNotIn("folding up", self.logged())

    def test_nothing_is_cleared_while_the_folding_turn_is_still_open(self):
        # The handoff file is whole and the rollout then says nothing for three
        # times CR_ROOT_IDLE_SEC — everything but the turn's own ending says
        # the fold is done. That ratio is what the hold is for; its length is
        # not, and at 5s it was mostly a wait.
        s = self.session(env=self.restart_env(FAKE_TURN_HOLD="1.5"), cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:turn-closed", timeout=15), s.buf[-500:])
        before = s.buf.index("GOT:turn-closed")
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertGreater(s.buf.index("GOT:/clear"), before)

    def test_an_interrupted_fold_never_clears_anything(self):
        s = self.session(env=self.restart_env(FAKE_HANDOFF_STOP="aborted",
                                              CR_HANDOFF_ATTEMPTS="1"),
                         cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_for_log("stop_reason=aborted", s), self.logged())
        # What the wrapper does INSTEAD of clearing: it calls the fold off, in
        # words, and that is the last thing it types. The fake answers lines in
        # the order they were typed, so by the time it has answered this one a
        # `/clear` sent ahead of it would be on screen too.
        self.assertTrue(s.read_until("GOT:The context restart was cancelled", timeout=15),
                        s.buf[-500:])
        self.assertNotIn("GOT:/clear", s.buf)
        self.assertNotIn("clearing the context", self.logged())

    def test_codex_is_started_with_its_own_compaction_held_back(self):
        s = self.session(env=self.restart_env(FAKE_USAGE="1000"), cwd=self.work)
        self.assertTrue(s.read_until("ready", timeout=10))
        self.assertIn("model_auto_compact_token_limit_scope", s.buf)
        self.assertIn("holding codex's own compaction back", self.logged())

    def test_codexs_own_count_and_cap_are_read_from_its_log(self):
        s = self.session(env=self.restart_env(FAKE_USAGE="1000", FAKE_LOG="1"), cwd=self.work)
        self.assertTrue(self.wait_for_log("codex compacts this thread at 258k", s),
                        self.logged())

    def test_codex_getting_there_first_is_logged(self):
        s = self.session(env=self.restart_env(FAKE_USAGE="1000", FAKE_COMPACT="1",
                                              FAKE_BANNER=USAGE_DOWN),
                         cwd=self.work)
        # The count first, so that there is a thread for codex to compact: the
        # fake has written it by the time it says so.
        self.assertTrue(s.read_until(USAGE_DOWN, timeout=10), s.buf[-500:])
        s.send("compact\r")
        self.assertTrue(self.wait_for_log("compacted the thread on its own", s),
                        self.logged())

    def test_the_context_window_comes_from_the_rollout(self):
        s = self.session(
            env=self.env(CR_CODEX_CONTEXT_TOKENS="135200", FAKE_USAGE="1000",
                        FAKE_WINDOW="400000"),
            cwd=self.work)
        self.assertTrue(s.read_until("ready", timeout=10))
        self.assertTrue(self.wait_for_log("400k context window (the transcript)", s),
                        self.logged())

    def test_only_the_session_above_threshold_gets_a_fold(self):
        # Two wrapped codex processes, same cwd, same CODEX_HOME: the quiet
        # one's rollout and the busy one's both pass `keep` -- cwd is all a
        # rollout has to go on. codex's log is what tells them apart (T34):
        # each wrapper is told its own thread by its own child's pid, and only
        # the session that is actually over the threshold may ever see the
        # handoff phrase.
        quiet = self.session(env=self.restart_env(FAKE_USAGE="1000"), cwd=self.work)
        self.assertTrue(quiet.read_until("ready", timeout=10))
        busy = self.session(env=self.restart_env(), cwd=self.work)
        self.assertTrue(busy.read_until("GOT:handoff", timeout=30), busy.buf[-500:])
        # Both wrappers were watching the same climbing count go quiet, on the
        # same clocks, so the moment busy decided to fold is the moment quiet
        # would have — and busy's phrase has since taken 0.6s to be typed and
        # answered. What is left to wait out is how far apart two loops can
        # reach the same decision (a poll and a turn, 0.35s), several times.
        quiet.drain(1.5)
        self.assertNotIn("GOT:handoff", quiet.buf)
        # Not that quiet happened to be looking at its own rollout: that it
        # was told which one is its own, and never had to guess.
        self.assertIn("session bound: ", self.logged_by(quiet))
        self.assertIn("in codex's log)", self.logged_by(quiet))
        self.assertNotIn("candidate rollout", self.logged_by(quiet))
        self.assertNotIn("fallback heuristic", self.logged_by(quiet))
        self.assertNotIn("folding up", self.logged_by(quiet))
        self.assertIn("folding up", self.logged_by(busy))

    def test_without_a_log_that_names_processes_the_quiet_one_still_holds(self):
        # An older codex, whose log does not say who wrote a row: the same
        # pair is told apart the way T03 told it apart, and the quiet one
        # holds its own restart back rather than guess.
        quiet = self.session(env=self.restart_env(FAKE_USAGE="1000", FAKE_LOG_PID="0"),
                             cwd=self.work)
        self.assertTrue(quiet.read_until("ready", timeout=10))
        busy = self.session(env=self.restart_env(FAKE_LOG_PID="0"), cwd=self.work)
        self.assertTrue(busy.read_until("GOT:handoff", timeout=30), busy.buf[-500:])
        quiet.drain(1.5)
        self.assertNotIn("GOT:handoff", quiet.buf)
        self.assertIn("does not say which process wrote a row", self.logged_by(quiet))
        self.assertIn("more than one candidate rollout", self.logged_by(quiet))
        self.assertNotIn("folding up", self.logged_by(quiet))
        self.assertIn("folding up", self.logged_by(busy))

    # -- T34: two codex sessions in one directory, as the log found them ------ #
    def test_a_session_that_has_said_nothing_is_not_folded_on_its_neighbours_count(self):
        # some_game, 2026-10-08: wrapper 75256 was `context bound` to its
        # neighbour's rollout ten seconds after starting. Its own codex had
        # said nothing yet, so it had no rollout at all -- codex makes one at
        # a thread's first message -- and the neighbour's was the only one.
        busy = self.session(env=self.restart_env(FAKE_USAGE=""), cwd=self.work)
        self.assertTrue(busy.read_until("ready", timeout=10))
        later = self.session(env=self.restart_env(FAKE_USAGE="", FAKE_LAZY_ROLLOUT="1"),
                             cwd=self.work)
        self.assertTrue(later.read_until("ready", timeout=10))
        # Watching before the neighbour's count climbs: told whose thread it
        # is, with no file to read yet. (Asserted below rather than here, so
        # that a wrapper that is told nothing still shows what it does next.)
        deadline = time.time() + 10
        while "no rollout yet)" not in self.logged_by(later) and time.time() < deadline:
            later.drain(0.05)
        busy.send("usage 200000\r")
        self.assertTrue(busy.read_until("GOT:handoff", timeout=30), busy.buf[-500:])
        # As in the test above: what is left is how far apart two loops can
        # reach the same decision about the same count.
        later.drain(1.5)
        self.assertNotIn("GOT:handoff", later.buf)
        mine = self.logged_by(later)
        self.assertNotIn("folding up", mine)
        self.assertNotIn("context bound", mine)
        self.assertNotIn("200k", mine)
        self.assertIn("in codex's log; no rollout yet)", mine)

    def test_the_earlier_session_still_folds_once_a_neighbour_has_started(self):
        # The other half of the same evening: wrapper 9345, which had been
        # following its own rollout for a quarter of an hour, saw a second
        # one appear, held its restart "until the handoff phrase proves which
        # is ours", and never typed that phrase again.
        first = self.session(env=self.restart_env(FAKE_USAGE=""), cwd=self.work)
        self.assertTrue(first.read_until("ready", timeout=10))
        self.assertTrue(self.wait_for_log("context bound", first), self.logged_by(first))
        second = self.session(env=self.restart_env(FAKE_USAGE=""), cwd=self.work)
        self.assertTrue(second.read_until("ready", timeout=10))
        self.assertTrue(second.roundtrip(), second.buf[-500:])    # it has a rollout, and it grew
        first.send("usage 200000\r")
        self.assertTrue(first.read_until("GOT:handoff", timeout=30), first.buf[-500:])
        self.assertNotIn("candidate rollout", self.logged_by(first))
        second.drain(1.5)
        self.assertNotIn("GOT:handoff", second.buf)

    def test_a_neighbours_refused_turn_is_not_nudged_in_this_session(self):
        # A refused turn is the session's, not the account's: the neighbour's
        # wrapper types `continue` into the neighbour. Ours never stopped.
        env = dict(CR_WAIT_SCALE="60", FAKE_USAGE="")
        ours = self.session(env=self.restart_env(**env), cwd=self.work)
        self.assertTrue(ours.read_until("ready", timeout=10))
        theirs = self.session(env=self.restart_env(**env), cwd=self.work)
        self.assertTrue(theirs.read_until("ready", timeout=10))
        self.assertTrue(self.wait_for_log("context bound", ours), self.logged_by(ours))
        self.assertTrue(self.wait_for_log("context bound", theirs), self.logged_by(theirs))
        theirs.send("refuse\r")
        self.assertTrue(theirs.read_until("GOT:continue", timeout=25), theirs.buf[-500:])
        self.caught_up(ours)
        self.assertNotIn("stall detected", self.logged_by(ours))
        self.assertNotIn("GOT:continue", ours.buf)

    def test_a_restart_unfolds_into_a_thread_that_has_no_rollout_yet(self):
        # Real codex makes the thread at `/clear` and its rollout only when
        # the resume phrase is typed into it. The log names the new thread at
        # once; the restart stays on the old rollout until the new one exists,
        # and then follows it there.
        s = self.session(env=self.restart_env(FAKE_LAZY_ROLLOUT="1"), cwd=self.work)
        self.assertTrue(s.read_until("GOT:handoff", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:/clear", timeout=30), s.buf[-500:])
        self.assertTrue(s.read_until("GOT:resume", timeout=30), s.buf[-500:])
        self.assertTrue(self.wait_for_log("context restarted", s), self.logged())
        self.assertIn("the clear took hold (the screen went quiet)", self.logged())
        self.assertIn("session rebound: ", self.logged())


if __name__ == "__main__":
    unittest.main(verbosity=2)
