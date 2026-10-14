"""The structured channel: Claude Code's own JSONL transcript.

A limited turn is written as a record carrying `"error": "rate_limit"` and
`"apiErrorStatus": 429`. Keying on that field instead of on the rendered text
removes the entire "text about an error vs. a live error" ambiguity that the
screen-scraping design can only ever approximate.
"""
import inspect
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helper import load

cr = load()


def record(text, error="rate_limit", status=429, api_error=True):
    return {
        "type": "assistant",
        "timestamp": "2026-07-19T19:22:08.730Z",
        "message": {"role": "assistant", "model": "<synthetic>",
                    "content": [{"type": "text", "text": text}]},
        "error": error,
        "isApiErrorMessage": api_error,
        "apiErrorStatus": status,
        "sessionId": "s1",
    }


ORDINARY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}

# A "No response requested" / interrupted-request row: Claude Code writes it
# with a model that is not a real slug and usage every counter of which is 0.
SYNTHETIC = {"type": "assistant", "message": {
    "role": "assistant", "model": "<synthetic>",
    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "output_tokens": 0},
    "stop_reason": "stop_sequence",
    "content": [{"type": "text", "text": "No response requested."}]}}


def user_row(text):
    return {"type": "user", "timestamp": "2026-08-01T18:20:45.715Z",
            "message": {"role": "user", "content": text}, "sessionId": "s1"}


USER_ECHO = user_row("continue")

# Verbatim shape of a real limited turn, minus the fields we don't read.
REAL = record("You've hit your weekly limit · resets Jul 22 at 6am (Europe/Warsaw)")


class FakeRegistry:
    """A stand-in for ClaudeSessionRegistry: hands back whichever queued
    `(record, why)` answer is next, repeating the last one once exhausted —
    the same shape a real `sessions/<pid>.json` gives across polls once
    nothing about it has changed. With no answers at all it names nobody,
    which is what a claude that has not written its file yet looks like, and
    one that never will."""

    child_pid = 4242

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def lookup(self):
        if not self.answers:
            return None, None
        i = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        return self.answers[i]


class TestProjectDir(unittest.TestCase):
    def test_cwd_is_slugged_the_way_claude_code_does_it(self):
        got = cr.project_dir("/Users/a0s/a0s_github/node_editor", "/cfg")
        self.assertEqual(got, "/cfg/projects/-Users-a0s-a0s-github-node-editor")

    def test_dots_and_underscores_both_become_dashes(self):
        got = cr.project_dir("/Users/a0s/p/.claude/worktrees/x_y", "/cfg")
        self.assertEqual(got, "/cfg/projects/-Users-a0s-p--claude-worktrees-x-y")


class TestRecordParsing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-tr-")
        self.path = os.path.join(self.dir, "session.jsonl")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, *records):
        with open(self.path, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def limits(self, found):
        return [r for r in found if r["kind"] == "limit"]

    def test_finds_the_limit_record(self):
        self.write(ORDINARY, REAL, ORDINARY)
        offset, found = cr.transcript_limit_records(self.path, 0)
        limits = self.limits(found)
        self.assertEqual(len(limits), 1)
        self.assertIn("resets Jul 22 at 6am", limits[0]["text"])
        self.assertEqual(offset, os.path.getsize(self.path))

    def test_offset_makes_reads_incremental(self):
        self.write(ORDINARY)
        offset, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual(self.limits(found), [])
        self.write(REAL)
        offset, found = cr.transcript_limit_records(self.path, offset)
        self.assertEqual(len(self.limits(found)), 1)
        # Reading again from the new offset must not re-report it.
        _, again = cr.transcript_limit_records(self.path, offset)
        self.assertEqual(again, [])

    def test_a_half_written_line_is_not_parsed_yet(self):
        # The transcript is appended to while we read it, so the last line can be
        # incomplete. Consuming it would drop the record permanently.
        with open(self.path, "w") as fh:
            fh.write(json.dumps(ORDINARY) + "\n")
            fh.write(json.dumps(REAL)[:60])
        offset, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual(self.limits(found), [])
        with open(self.path, "a") as fh:
            fh.write(json.dumps(REAL)[60:] + "\n")
        _, found = cr.transcript_limit_records(self.path, offset)
        self.assertEqual(len(self.limits(found)), 1)

    def test_non_limit_api_errors_are_ignored(self):
        self.write(record("API Error: 529 overloaded", error="overloaded", status=529))
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual(found, [])

    def test_the_word_alone_is_not_enough(self):
        # A session that merely discusses rate limits writes ordinary assistant
        # records; only the structured error field makes one a limit.
        self.write({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "rate_limit and isApiErrorMessage are the fields to watch"}]}})
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual([r["kind"] for r in found], ["alive"])

    def test_malformed_json_does_not_raise(self):
        with open(self.path, "w") as fh:
            fh.write("{not json at all\n")
            fh.write(json.dumps(REAL) + "\n")
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual(len(self.limits(found)), 1)

    def test_an_answered_turn_is_reported_as_alive(self):
        # The account is serving requests again. It is what says so when a limit
        # ends early — a switched account, an upgraded plan — with no banner and
        # no reset to announce it.
        self.write(ORDINARY)
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual([r["kind"] for r in found], ["alive"])

    def test_synthetic_row_reports_nothing_but_stays_alive(self):
        # It still proves the account answered ("alive"), but carries no usable
        # tokens/model/stop_reason: reading them would reset the badge to 0 and
        # make the window lookup fail on the fake slug.
        row = cr.assistant_row(SYNTHETIC)
        self.assertEqual(row["kind"], "alive")
        self.assertIsNone(row["tokens"])
        self.assertIsNone(row["model"])
        self.assertIsNone(row["stop_reason"])

    def test_synthetic_row_through_transcript_limit_records(self):
        self.write(SYNTHETIC)
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual([r["kind"] for r in found], ["alive"])
        self.assertIsNone(found[0]["tokens"])
        self.assertIsNone(found[0]["model"])

    def test_a_run_of_assistant_rows_collapses_into_one(self):
        # One answer is many rows (a thought, three tool calls, a summary). The
        # caller only needs "the session answered".
        self.write(ORDINARY, ORDINARY, ORDINARY)
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual([r["kind"] for r in found], ["alive"])

    def test_alive_keeps_its_place_relative_to_a_limit(self):
        # Order is the whole meaning: alive-then-limit is a session that just
        # ran out, limit-then-alive is one that came back.
        self.write(ORDINARY, REAL, ORDINARY)
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual([r["kind"] for r in found], ["alive", "limit", "alive"])

    def test_a_limit_record_is_not_also_alive(self):
        self.write(REAL)
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual([r["kind"] for r in found], ["limit"])

    def test_a_user_row_quoting_the_type_field_is_not_alive(self):
        # Tool output lands in a user row verbatim; this file's own source has
        # been pasted into one more than once.
        self.write(user_row('grep found: "type":"assistant" in the parser'))
        _, found = cr.transcript_limit_records(self.path, 0)
        self.assertEqual(found, [])

    def human(self, text, **extra):
        row = user_row(text)
        row.update(turnOrigin="human", origin={"kind": "human"}, promptSource="typed")
        row.update(extra)
        return row

    def kinds(self, *records, echo=None):
        self.write(*records)
        _, found = cr.transcript_limit_records(self.path, 0, echo)
        return [(r["kind"], r.get("text")) for r in found]

    def test_a_prompt_a_person_typed_is_reported(self):
        # T30: the one sign that someone took the session over mid-restart.
        self.assertEqual(self.kinds(self.human("продолжи scratchpad/RESUME.md")),
                         [("prompt", "продолжи scratchpad/RESUME.md")])

    def test_a_peer_message_is_not_a_prompt(self):
        peer = user_row("Another Claude session sent a message")
        peer.update(turnOrigin="peer", origin={"kind": "peer"}, isMeta=True)
        self.assertEqual(self.kinds(peer), [])

    def test_the_clear_command_row_is_not_a_prompt(self):
        self.assertEqual(self.kinds(user_row(
            "<command-name>/clear</command-name>\n<command-message>clear</command-message>")), [])

    def test_a_skill_body_riding_along_is_not_a_second_prompt(self):
        self.assertEqual(self.kinds(self.human("Base directory for this skill", isMeta=True)), [])

    def test_our_own_phrase_is_an_echo_not_a_prompt(self):
        self.assertEqual(self.kinds(self.human("continue"), echo="continue"),
                         [("echo", "continue")])

    def test_missing_file(self):
        offset, found = cr.transcript_limit_records(os.path.join(self.dir, "nope.jsonl"), 0)
        self.assertEqual((offset, found), (0, []))


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-w-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def append(self, name, rec):
        with open(self.path(name), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_preexisting_content_is_not_replayed(self):
        # `claude --continue` reopens a transcript that already contains
        # yesterday's banner. Replaying it would park a fresh session for hours.
        self.append("old.jsonl", REAL)
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.assertEqual(w.poll_now(), [])

    def test_new_records_in_an_existing_file_are_seen(self):
        self.append("old.jsonl", ORDINARY)
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("old.jsonl", REAL)
        found = w.poll_now()
        self.assertEqual(len(found), 1)

    def test_a_file_created_after_start_is_read_whole(self):
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("fresh.jsonl", REAL)
        self.assertEqual(len(w.poll_now()), 1)

    def test_growth_is_what_marks_the_transcript_channel_as_live(self):
        # `seen_any` is what turns the screen-scraping fallback off; it must only
        # flip once this project's transcript is actually being written.
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.assertFalse(w.seen_any)
        self.append("fresh.jsonl", ORDINARY)
        w.poll_now()
        self.assertTrue(w.seen_any)

    def test_poll_interval_is_respected(self):
        w = cr.TranscriptWatcher(self.dir, poll=100)
        w.poll_now(now=0)                                # first poll always runs
        self.append("fresh.jsonl", REAL)
        self.assertEqual(w.poll_now(now=1), [])          # still inside the interval
        self.assertEqual(len(w.poll_now(now=1e9)), 1)

    def test_our_retry_coming_back_is_reported_as_an_echo(self):
        # The proof a retry was submitted rather than left in the input box.
        # Watching the footer for it broke silently when Claude Code reworded it;
        # this row is written by claude itself.
        w = cr.TranscriptWatcher(self.dir, poll=0, echo="continue")
        self.append("s.jsonl", USER_ECHO)
        found = w.poll_now()
        self.assertEqual([r["kind"] for r in found], ["echo"])

    def test_a_different_prompt_is_not_our_echo(self):
        w = cr.TranscriptWatcher(self.dir, poll=0, echo="continue")
        self.append("s.jsonl", user_row("continue the refactor yourself"))
        self.assertEqual(w.poll_now(), [])

    def test_echoes_are_ignored_when_no_message_is_configured(self):
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("s.jsonl", USER_ECHO)
        self.assertEqual(w.poll_now(), [])

    def test_limit_records_are_still_labelled(self):
        w = cr.TranscriptWatcher(self.dir, poll=0, echo="continue")
        self.append("s.jsonl", REAL)
        self.assertEqual([r["kind"] for r in w.poll_now()], ["limit"])

    def test_truncation_is_handled(self):
        self.append("s.jsonl", ORDINARY)
        w = cr.TranscriptWatcher(self.dir, poll=0)
        with open(self.path("s.jsonl"), "w") as fh:      # rewritten from scratch
            fh.write(json.dumps(REAL) + "\n")
        self.assertEqual(len(w.poll_now()), 1)

    # -- registry-backed identity (T02) -------------------------------------- #
    def test_registry_binding_ignores_a_foreign_file_that_grows(self):
        registry = FakeRegistry(
            ({"sessionId": "ours", "status": "idle", "statusUpdatedAt": 0}, "pid file"))
        w = cr.TranscriptWatcher(self.dir, poll=0, registry=registry)
        w.poll_now()
        self.assertEqual(w.current, self.path("ours.jsonl"))
        for _ in range(3):
            self.append("theirs.jsonl", ORDINARY)
            w.poll_now()
            self.assertEqual(w.current, self.path("ours.jsonl"))

    def test_registry_rebinds_when_sessionId_changes_and_stops_reading_the_old_file(self):
        registry = FakeRegistry(
            ({"sessionId": "old", "status": "idle", "statusUpdatedAt": 0}, "pid file"),
            ({"sessionId": "new", "status": "busy", "statusUpdatedAt": 0}, "pid file"))
        logged = []
        w = cr.TranscriptWatcher(self.dir, poll=0, registry=registry, log=logged.append)
        w.poll_now()
        self.assertEqual(w.current, self.path("old.jsonl"))
        w.poll_now()
        self.assertEqual(w.current, self.path("new.jsonl"))
        self.assertTrue(any("session rebound: old → new" in ln for ln in logged))
        # growth on the file we left behind no longer moves `current`
        self.append("old.jsonl", REAL)
        w.poll_now()
        self.assertEqual(w.current, self.path("new.jsonl"))

    def test_registry_status_feeds_agent_status(self):
        registry = FakeRegistry(
            ({"sessionId": "s", "status": "busy", "statusUpdatedAt": 12345}, "pid file"))
        w = cr.TranscriptWatcher(self.dir, poll=0, registry=registry)
        w.poll_now()
        self.assertEqual(w.agent_status, "busy")
        self.assertEqual(w.agent_status_at, 12.345)

    def test_no_registry_keeps_the_growth_heuristic(self):
        # codex, and claude with no registry passed in: unchanged behaviour.
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("a.jsonl", ORDINARY)
        w.poll_now()
        self.assertEqual(w.current, self.path("a.jsonl"))
        self.append("b.jsonl", ORDINARY)
        w.poll_now()
        self.assertEqual(w.current, self.path("b.jsonl"))


OURS = {"sessionId": "ours", "status": "idle", "statusUpdatedAt": 0}


class TestBeforeTheRegistryHasNamedAnyone(unittest.TestCase):
    """T32. Every launch begins with a registry that names nobody: Claude Code
    writes `sessions/<pid>.json` a second or two after it starts. Until it
    has, a transcript growing in the project is a neighbour's as readily as
    ours — two wrappers started together in one directory — and the watcher
    used to take it anyway, the first poll it saw one grow. It waits now."""

    START = 1000.0

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-w-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.logged = []

    def path(self, name):
        return os.path.join(self.dir, name)

    def append(self, name, rec):
        with open(self.path(name), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def watcher(self, registry, **kw):
        return cr.TranscriptWatcher(self.dir, poll=0, now=self.START, registry=registry,
                                    log=self.logged.append, **kw)

    def test_a_neighbours_transcript_is_not_taken_while_ours_is_unnamed(self):
        w = self.watcher(FakeRegistry())
        for t in (0.0, 0.2, 2.0, 4.0, 13.0):
            self.append("theirs.jsonl", ORDINARY)
            found = w.poll_now(now=self.START + t)
            self.assertTrue(found)                  # read...
            self.assertIsNone(w.current)            # ...and nobody's
            self.assertIsNone(w.bound_session_id)

    def test_a_limit_is_read_whoever_it_belongs_to(self):
        # The account's, not a session's: not having been named yet is no
        # reason to sit on one.
        w = self.watcher(FakeRegistry())
        self.append("theirs.jsonl", REAL)
        self.assertEqual([r["kind"] for r in w.poll_now(now=self.START + 0.2)], ["limit"])

    def test_once_named_the_session_is_ours_and_nothing_of_the_neighbours_is(self):
        registry = FakeRegistry()
        w = self.watcher(registry)
        theirs, ours = self.path("theirs.jsonl"), self.path("ours.jsonl")
        mine = []           # what main() would hand the controller as this session's
        for t in (0.2, 2.0):
            self.append("theirs.jsonl", ORDINARY)
            mine += [r for r in w.poll_now(now=self.START + t) if r["path"] == w.current]
        registry.answers = [(OURS, "pid file")]     # our claude's file lands
        self.append("ours.jsonl", ORDINARY)
        self.append("theirs.jsonl", ORDINARY)
        found = w.poll_now(now=self.START + 4.0)
        mine += [r for r in found if r["path"] == w.current]
        self.assertEqual(w.current, ours)
        self.assertEqual(w.bound_session_id, "ours")
        self.assertEqual(sorted(r["path"] for r in found), [ours, theirs])
        for t in (6.0, 60.0, 3600.0):               # and it stays ours
            self.append("theirs.jsonl", ORDINARY)
            mine += [r for r in w.poll_now(now=self.START + t) if r["path"] == w.current]
            self.assertEqual(w.current, ours)
        self.assertEqual([r["path"] for r in mine], [ours])
        self.assertEqual([ln for ln in self.logged if "session" in ln],
                         ["session bound: ours (pid file)"])

    def test_a_row_is_never_read_ahead_of_the_name_it_belongs_under(self):
        # The file and the session's first row land between the two steps of
        # one poll. Asked who we are BEFORE reading, the watcher read the row
        # with nobody named and handed it on as a stranger's — our own limit,
        # logged as a neighbour's, was how this was first noticed.
        registry = FakeRegistry()
        test = self

        class NamedMidPoll(cr.ClaudeAgent):
            polling = False

            def paths(self, now=None):
                if self.polling and not registry.answers:
                    registry.answers = [(OURS, "pid file")]
                    test.append("ours.jsonl", REAL)
                    test.append("theirs.jsonl", ORDINARY)
                    os.utime(test.path("ours.jsonl"), (1, 1))     # theirs is the newer
                return super().paths(now)

        agent = NamedMidPoll(self.dir)
        w = cr.TranscriptWatcher(poll=0, now=self.START, registry=registry, agent=agent)
        agent.polling = True                 # the scan that seeds the watcher is over
        found = w.poll_now(now=self.START + 0.2)
        self.assertEqual(w.current, self.path("ours.jsonl"))
        self.assertEqual([r["path"] for r in found if r["kind"] == "limit"], [w.current])

    def test_the_heuristic_gets_its_turn_when_the_wait_runs_out(self):
        # A claude that writes no session file at all: an older build. (Not
        # `claude attach` or the roster any more — T33, below.) Said once,
        # when it starts to matter.
        registry = FakeRegistry()
        registry.child_pid = 999
        w = self.watcher(registry)
        self.append("a.jsonl", ORDINARY)
        w.poll_now(now=self.START + 29.9)            # thirty seconds, unless told
        self.assertIsNone(w.current)
        self.assertEqual(self.logged, [])
        for t in (30, 32, 34):
            self.append("a.jsonl", ORDINARY)
            w.poll_now(now=self.START + t)
            self.assertEqual(w.current, self.path("a.jsonl"))
        said = [ln for ln in self.logged if "no session registry for pid 999" in ln]
        self.assertEqual(len(said), 1)
        self.assertIn("after 30s; falling back to newest transcript", said[0])

    def test_the_wait_is_as_long_as_it_is_told(self):
        w = self.watcher(FakeRegistry(), grace=5)    # CR_SESSION_GRACE_SEC
        self.append("a.jsonl", ORDINARY)
        w.poll_now(now=self.START + 4.9)
        self.assertIsNone(w.current)
        self.append("a.jsonl", ORDINARY)
        w.poll_now(now=self.START + 5)
        self.assertEqual(w.current, self.path("a.jsonl"))
        self.assertIn("after 5s", self.logged[-1])

    def test_a_file_that_comes_after_the_wait_still_decides(self):
        registry = FakeRegistry()
        w = self.watcher(registry)
        self.append("theirs.jsonl", ORDINARY)
        w.poll_now(now=self.START + 31)
        self.assertEqual(w.current, self.path("theirs.jsonl"))     # the guess
        registry.answers = [(OURS, "pid file")]
        w.poll_now(now=self.START + 33)
        self.assertEqual(w.current, self.path("ours.jsonl"))
        self.append("theirs.jsonl", ORDINARY)                       # ...which never returns
        w.poll_now(now=self.START + 35)
        self.assertEqual(w.current, self.path("ours.jsonl"))

    def test_a_session_whose_file_has_gone_is_still_the_session(self):
        # Claude Code removes the file as it exits, a poll or so before the
        # pty closes. That is not the moment to go looking for another.
        w = self.watcher(FakeRegistry((OURS, "pid file"), (None, None)))
        w.poll_now(now=self.START + 2)
        self.assertEqual(w.current, self.path("ours.jsonl"))
        for t in (4, 40, 4000):
            self.append("theirs.jsonl", ORDINARY)
            w.poll_now(now=self.START + t)
            self.assertEqual(w.current, self.path("ours.jsonl"))
            self.assertEqual(w.bound_session_id, "ours")
        self.assertFalse([ln for ln in self.logged if "falling back" in ln])

    def test_without_a_registry_there_is_nothing_to_wait_for(self):
        # codex: the heuristic is all there has ever been, from the first poll.
        w = cr.TranscriptWatcher(self.dir, poll=0, now=self.START)
        self.append("a.jsonl", ORDINARY)
        w.poll_now(now=self.START + 0.2)
        self.assertEqual(w.current, self.path("a.jsonl"))


STARTED = 4000      # this wrapper's own start, on the clock `startedAt` is written in


def session_registry(config_dir, child_pid, **kw):
    """A `ClaudeSessionRegistry` given what `main()` has to give one.

    Our own start time is part of that for any registry that asks for it. The
    one that shipped until T32 did, and bound a session on nothing more than
    its having started after us; this one takes no clock at all. Should one
    ask again, it gets `STARTED` here — and every neighbour in the tests below
    started after that, in our cwd, which is all that rule ever looked at.
    """
    kw.setdefault("proc_table", lambda: {})
    if "started_at" in inspect.signature(cr.ClaudeSessionRegistry).parameters:
        kw.setdefault("started_at", STARTED)
    return cr.ClaudeSessionRegistry(child_pid, cwd="/work", config_dir=config_dir, **kw)


def session_record(sessions, pid, **fields):
    rec = {"pid": pid, "sessionId": "s-%d" % pid, "cwd": "/work",
           "startedAt": 1000, "version": "2.1.273", "kind": "interactive",
           "status": "idle", "statusUpdatedAt": 1000}
    rec.update(fields)
    with open(os.path.join(sessions, "%d.json" % pid), "w") as fh:
        json.dump(rec, fh)
    return rec


class TestSessionRegistry(unittest.TestCase):
    """`ClaudeSessionRegistry.lookup()` in isolation: which of
    `sessions/*.json` is ours, given only a child pid and a cwd."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-reg-")
        self.sessions = os.path.join(self.dir, "sessions")
        os.makedirs(self.sessions)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, pid, **fields):
        return session_record(self.sessions, pid, **fields)

    def registry(self, child_pid, **kw):
        return session_registry(self.dir, child_pid, **kw)

    def test_direct_pid_file_is_used_first(self):
        self.write(111)
        rec, why = self.registry(111).lookup()
        self.assertEqual(rec["sessionId"], "s-111")
        self.assertEqual(why, "pid file")

    def test_a_grandchild_is_found_through_the_parent_chain(self):
        # An alias launch: we forked pid 100 (a shell), which forked 200 --
        # the real claude, which is who writes 200.json.
        self.write(200)
        table = {200: 100, 100: 1}
        rec, why = self.registry(100, proc_table=lambda: table).lookup()
        self.assertEqual(rec["sessionId"], "s-200")
        self.assertIn("descendant", why)

    # -- a record is ours by whose process wrote it, and by nothing else (T32) -- #
    def test_a_session_started_beside_us_is_not_ours(self):
        # One candidate, and it is somebody else: a second wrapper's claude,
        # started in this directory a moment after we were.
        self.write(300, startedAt=STARTED + 5)
        self.assertEqual(self.registry(999).lookup(), (None, None))

    def test_two_sessions_start_together_and_ours_is_the_later_file(self):
        # The race itself. Our claude (111) and a neighbour's (222) were
        # started in the same instant; theirs has written its file, ours has
        # not yet. Then ours does.
        reg = self.registry(111, proc_table=lambda: {111: 50, 222: 60, 50: 1, 60: 1})
        self.write(222, startedAt=STARTED + 5)
        for _ in range(3):
            self.assertEqual(reg.lookup(), (None, None))
        self.write(111, startedAt=STARTED + 9)
        rec, why = reg.lookup()
        self.assertEqual((rec["sessionId"], why), ("s-111", "pid file"))

    def test_our_file_going_away_does_not_hand_us_to_a_neighbour(self):
        # Hours in, with a neighbour that started since: claude removes its
        # file on the way out, and for one last poll only theirs is there.
        reg = self.registry(111)
        self.write(111)
        self.write(222, startedAt=STARTED + 3600000)
        self.assertEqual(reg.lookup()[0]["sessionId"], "s-111")
        os.unlink(os.path.join(self.sessions, "111.json"))
        self.assertEqual(reg.lookup(), (None, None))

    def test_a_different_cwd_is_never_a_candidate(self):
        self.write(400, cwd="/elsewhere")
        rec, why = self.registry(100, proc_table=lambda: {400: 100}).lookup()
        self.assertIsNone(rec)

    def test_ambiguous_candidates_do_not_bind(self):
        logged = []
        self.write(500)
        self.write(501)
        reg = self.registry(100, proc_table=lambda: {500: 100, 501: 100}, log=logged.append)
        for _ in range(3):
            self.assertEqual(reg.lookup(), (None, None))
        self.assertEqual(len([ln for ln in logged if "2 candidates for pid 100" in ln]), 1)

    def test_naming_nobody_is_said_in_silence(self):
        # Not yet written, just removed, or never coming: the registry cannot
        # tell which, so it does not say. `TranscriptWatcher` counts the wait
        # and is the one to announce the fallback (test above, in its class).
        logged = []
        reg = self.registry(999, log=logged.append)
        self.write(300, startedAt=STARTED + 5)
        for _ in range(3):
            self.assertEqual(reg.lookup(), (None, None))
        self.assertEqual(logged, [])

    def test_a_file_that_is_not_a_record_loses_the_candidate_and_nothing_else(self):
        # The directory is every Claude Code's on the machine, of any version.
        for pid, text in ((300, "[1, 2]"), (301, '"busy"'), (302, "{"), (303, "")):
            with open(os.path.join(self.sessions, "%d.json" % pid), "w") as fh:
                fh.write(text)
        self.assertEqual(self.registry(999).lookup(), (None, None))
        self.assertEqual(self.registry(300).lookup(), (None, None))
        self.write(200)
        rec, why = self.registry(100, proc_table=lambda: {200: 100}).lookup()
        self.assertEqual(rec["sessionId"], "s-200")

    # -- the parent chain, where there is no `ps` to ask ------------------- #
    def proc(self, **stat):
        root = os.path.join(self.dir, "proc")
        for name, text in stat.items():
            os.makedirs(os.path.join(root, name.lstrip("p")))
            if text is not None:
                with open(os.path.join(root, name.lstrip("p"), "stat"), "w") as fh:
                    fh.write(text)
        for name in ("self", "net"):                 # /proc is not only pids
            os.makedirs(os.path.join(root, name))
        return root

    def test_the_parent_chain_is_read_from_proc(self):
        root = self.proc(p100="100 (sh) S 1 100 100 0 -1 4194304 1",
                         p200="200 (node) R 100 200 100 0 -1 4194304 1",
                         p300="300 (my (odd) name) S 200 300 100 0 -1 4194304 1",
                         p400=None,                  # gone between listdir and open
                         p500="500 garbage")
        self.assertEqual(cr.ClaudeSessionRegistry._proc_table(root),
                         {100: 1, 200: 100, 300: 200})
        self.assertEqual(cr.ClaudeSessionRegistry._proc_table(os.path.join(root, "nope")), {})

    def test_proc_stands_in_when_ps_cannot_be_run(self):
        # A slim container: no procps. An alias launch there has only the
        # parent chain to be found by, now that nothing else binds.
        with mock.patch.object(cr.subprocess, "run", side_effect=FileNotFoundError("ps")), \
                mock.patch.object(cr.ClaudeSessionRegistry, "_proc_table",
                                  staticmethod(lambda root="/proc": {200: 100, 100: 1})):
            self.write(200)
            reg = cr.ClaudeSessionRegistry(100, cwd="/work", config_dir=self.dir)
            rec, why = reg.lookup()
        self.assertEqual(rec["sessionId"], "s-200")
        self.assertIn("descendant", why)


def answered(tokens, model):
    """An assistant row the size of a turn sent with `tokens` of context."""
    return {"type": "assistant", "isSidechain": False, "message": {
        "role": "assistant", "model": model, "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1000,
                  "cache_read_input_tokens": tokens - 1002, "output_tokens": 10},
        "content": [{"type": "text", "text": "done"}]}}


class TestTwoWrappersStartedTogether(unittest.TestCase):
    """T32 from end to end, without a pty: the real registry, the real watcher
    and the real controller, handed rows the way `main()` hands them.

    Two wrappers are started in one directory in the same instant. The
    neighbour's claude (pid 222) has written its session file and is already
    at work on a small-window model with 700k in it; ours (pid 111, a big
    one, 50k) has not written its own yet. That is a poll or two of every
    real launch — and it was the whole of the time this wrapper used to
    spend bound to the other one's session, reading its count, its model and
    its turns as its own."""

    THRESHOLD = 510000
    T0 = 1000.0         # when both were started

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cr-race-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.sessions = os.path.join(self.home, "sessions")
        self.project = os.path.join(self.home, "project")
        os.makedirs(self.sessions)
        os.makedirs(self.project)
        self.logged = []
        self.typed = []
        cfg = dict(cr.CFG, context_tokens=self.THRESHOLD, wait_scale=1, margin=0,
                   user_idle=0, handoff_file=os.path.join(self.home, "handoff.md"))
        self.ctl = cr.Controller(cfg, self.logged.append, now=self.T0)
        registry = session_registry(self.home, 111, log=self.logged.append,
                                    proc_table=lambda: {111: 50, 222: 60, 50: 1, 60: 1})
        self.w = cr.TranscriptWatcher(self.project, poll=0, now=self.T0, registry=registry,
                                      log=self.logged.append)

    def session(self, pid, name, **fields):
        session_record(self.sessions, pid, sessionId=name, **fields)

    def path(self, name):
        return os.path.join(self.project, name + ".jsonl")

    def row(self, name, rec):
        with open(self.path(name), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def turn(self, at):
        """One turn of the supervisor's loop, `at` seconds in: poll, and hand
        on what was read exactly as `main()` does — by path, to whichever
        transcript is ours."""
        w, ctl, now = self.w, self.ctl, self.T0 + at
        recs = w.poll_now(now)
        if w.current and w.current != ctl.context_path:
            why = "session identity" if w.bound_session_id else "fallback heuristic"
            ctl.bind_transcript(w.current, why, now)
        for rec in recs:
            ours = rec["path"] == w.current
            if rec["kind"] == "alive" and ours:
                ctl.on_context(rec, now)
                ctl.on_alive(now, "transcript", rec["path"])
            elif rec["kind"] == "limit":
                ctl.on_limit(rec["text"], now, "transcript" if ours else "neighbour")
        ctl.note_growth(w.grown, now)
        if w.agent_status is not None:
            ctl.on_agent_status(w.agent_status, w.agent_status_at)
        action = ctl.tick(now)
        if action and action[0] == "inject":
            self.typed.append(action[1])
        return action

    def assertKnowsNothing(self):
        self.assertIsNone(self.w.bound_session_id)
        self.assertIsNone(self.w.current)
        for name in ("context_path", "context_tokens", "context_model", "context_window",
                     "agent_status"):
            self.assertIsNone(getattr(self.ctl, name), name)

    def test_we_do_not_bind_to_the_neighbour_and_keep_nothing_of_theirs(self):
        self.session(222, "theirs", startedAt=STARTED + 5, status="busy")
        # Ours is not written yet: the poll at launch, the one two seconds
        # in, and — for one launch in eight — the one after that.
        for at in (0.0, 2.0, 4.0):
            self.row("theirs", answered(700000, "claude-haiku-4-5"))
            self.turn(at)
            self.assertKnowsNothing()
        # ...and, longer than any launch on record has gone unnamed, long
        # enough for b's turn to have gone quiet: which is when a wrapper
        # holding 700k for its own would fold.
        self.turn(28.0)
        self.assertKnowsNothing()
        self.assertEqual(self.logged, [])
        # Then ours is, and our claude answers its first turn.
        self.session(111, "ours", startedAt=STARTED + 9)
        self.row("ours", answered(50000, "claude-opus-5"))
        self.row("theirs", answered(700100, "claude-haiku-4-5"))
        self.turn(29.0)
        self.assertEqual(self.w.bound_session_id, "ours")
        self.assertEqual(self.ctl.context_path, self.path("ours"))
        self.assertEqual((self.ctl.context_tokens, self.ctl.context_model),
                         (50000, "claude-opus-5"))
        self.assertEqual(self.ctl.context_window, 1000000)
        self.assertEqual(self.ctl.agent_status, "idle")
        for at in (31.0, 60.0, 600.0):               # and b goes on being b
            self.row("theirs", answered(700200, "claude-haiku-4-5"))
            self.turn(at)
            self.assertEqual(self.ctl.context_tokens, 50000)
        self.assertEqual(self.typed, [])
        said = "\n".join(self.logged)
        self.assertIn("session bound: ours (pid file)", said)
        for theirs in ("theirs", "haiku", "700k", "rebound", "switched", "folding"):
            self.assertNotIn(theirs, said)

    def test_the_neighbours_answer_does_not_end_our_wait(self):
        # Ours takes thirteen seconds to be written — the slowest launch on
        # record — and in them our own session runs into the limit and the
        # neighbour, on another model, goes on answering.
        self.session(222, "theirs", startedAt=STARTED + 5)
        self.turn(0.0)
        self.row("ours", REAL)
        self.turn(2.0)
        self.assertEqual(self.ctl.state, cr.WAITING)
        self.row("theirs", answered(700000, "claude-haiku-4-5"))
        self.turn(12.0)                              # well past ALIVE_GRACE
        self.assertEqual(self.ctl.state, cr.WAITING)
        self.session(111, "ours", startedAt=STARTED + 9)
        self.row("theirs", answered(700100, "claude-haiku-4-5"))
        self.turn(14.0)
        self.assertEqual(self.w.bound_session_id, "ours")
        self.assertEqual(self.ctl.state, cr.WAITING)
        self.assertFalse([ln for ln in self.logged if "answering again" in ln])
        self.row("ours", answered(50000, "claude-opus-5"))     # our own does
        self.turn(16.0)
        self.assertEqual(self.ctl.state, cr.IDLE)

    def test_our_claude_leaving_does_not_hand_us_to_the_neighbour(self):
        # The same rule, at the other end of the session: hours in, with a
        # neighbour started since, claude removes its file on the way out.
        self.session(111, "ours", startedAt=STARTED + 9)
        self.row("ours", answered(50000, "claude-opus-5"))
        self.turn(2.0)
        self.session(222, "theirs", startedAt=STARTED + 3600000)
        os.unlink(os.path.join(self.sessions, "111.json"))
        for at in (7200.0, 7202.0):
            self.row("theirs", answered(700000, "claude-haiku-4-5"))
            self.turn(at)
            self.assertEqual(self.w.bound_session_id, "ours")
            self.assertEqual(self.ctl.context_path, self.path("ours"))
            self.assertEqual(self.ctl.context_tokens, 50000)
        self.assertFalse([ln for ln in self.logged if "rebound" in ln or "switched" in ln])


# --------------------------------------------------------------------------- #
# T33: `claude attach` and the roster are not a session of the child's
# --------------------------------------------------------------------------- #
JOB = "6cb059d2"                                  # the job of the field case, wrapper 45890
JOB_SESSION = "6cb059d2-1300-4191-8dd7-442e082370cd"   # what it was dispatched under
MOVED_ON = "f123d92c-b953-401e-92e6-925f41126da5"      # ...and the session it is after a /clear


def registry_for(argv, child_pid, config_dir, **kw):
    """The registry `main()` builds for a claude launched with `argv`.

    Until T33 that was the same registry whatever the launch, and the attach
    and the roster were named by its rules for a session of the child's own
    — nobody, and then the growth heuristic. Asked of a build that predates
    `claude_registry`, this builds exactly that, so a test below that fails
    there fails on what that build did, not on a name it lacks.
    """
    kw.setdefault("proc_table", lambda: {})
    if hasattr(cr, "claude_registry"):
        reg = cr.claude_registry(child_pid, argv, cwd="/work", config_dir=config_dir,
                                 log=kw.pop("log", None))
        reg.proc_table = kw["proc_table"]
        return reg
    return cr.ClaudeSessionRegistry(child_pid, cwd="/work", config_dir=config_dir, **kw)


class Jobs:
    """`$CLAUDE_CONFIG_DIR` the way Claude Code's daemon lays it out:
    `jobs/<8 hex>/state.json` per background session, and a background
    session's own `sessions/<pid>.json` with `kind: "bg"` and `jobId`."""

    def make_home(self):
        self.home = tempfile.mkdtemp(prefix="cr-attach-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.sessions = os.path.join(self.home, "sessions")
        os.makedirs(self.sessions)
        self.logged = []

    def project(self, cwd="/work"):
        d = cr.project_dir(cwd, self.home)
        os.makedirs(d, exist_ok=True)
        return d

    def job(self, short, **state):
        st = {"state": "working", "template": "bg", "sessionId": short + "-0000-4000-8000-000000000000",
              "cwd": "/work", "name": "", "intent": "", "createdAt": "2026-09-29T08:45:03.766Z"}
        st.update(state)
        d = os.path.join(self.home, "jobs", short)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "state.json"), "w") as fh:
            json.dump(st, fh)
        return st

    def bg(self, pid, short, session, **fields):
        return session_record(self.sessions, pid, sessionId=session, kind="bg", jobId=short,
                              **fields)


class TestWhatALaunchIsNamedBy(Jobs, unittest.TestCase):
    """`ClaudeSessionRegistry.lookup()` for the two launches that are not a
    session of the child's own: `claude attach <id|name>`, and the roster."""

    def setUp(self):
        self.make_home()

    def registry(self, argv, child_pid=111, **kw):
        return registry_for(argv, child_pid, self.home, log=self.logged.append, **kw)

    def test_an_attach_is_the_session_of_the_job_it_names(self):
        self.job(JOB, sessionId=JOB_SESSION)
        self.bg(333, JOB, JOB_SESSION)                    # the job's own, in the daemon
        self.write_neighbour()
        rec, why = self.registry(["attach", JOB]).lookup()
        self.assertEqual(rec["sessionId"], JOB_SESSION)
        self.assertEqual(why, "background job %s" % JOB)
        self.assertEqual(rec["transcriptPath"],
                         os.path.join(self.project(), JOB_SESSION + ".jsonl"))

    def write_neighbour(self):
        # Somebody else's session, started beside us after we were: the
        # first thing the old fallback (and the rule before it) took.
        session_record(self.sessions, 222, sessionId="1b900c55-theirs",
                       startedAt=STARTED + 60000)

    def test_the_attach_client_is_not_the_session_it_shows(self):
        # Were the client ever to write a record of its own (one build fed
        # `attach` a `--settings` and got a fresh session of its own out of
        # it), that record describes the client, not the session on screen.
        self.job(JOB, sessionId=JOB_SESSION)
        self.bg(333, JOB, JOB_SESSION)
        session_record(self.sessions, 111, sessionId="the-client-itself")
        session_record(self.sessions, 112, sessionId="its-child")
        reg = self.registry(["attach", JOB], proc_table=lambda: {112: 111})
        self.assertEqual(reg.lookup()[0]["sessionId"], JOB_SESSION)

    def test_nothing_else_ever_names_an_attach(self):
        # The field case before its job could be read: wrapper 45890's
        # session had no record naming its job, and a neighbour's did start.
        self.job("0badf00d", sessionId="0badf00d-0000")
        self.bg(444, "0badf00d", "another-job")          # somebody else's job
        self.write_neighbour()
        reg = self.registry(["attach", JOB])
        for _ in range(3):
            self.assertEqual(reg.lookup(), (None, None))
        said = [ln for ln in self.logged if "claude attach %s" % JOB in ln]
        self.assertEqual(len(said), 1, self.logged)
        self.assertIn("no background job matches", said[0])

    def test_any_unambiguous_start_of_the_id_will_do(self):
        self.job(JOB, sessionId=JOB_SESSION)
        self.job("6cf00000")
        self.bg(333, JOB, JOB_SESSION)
        self.assertEqual(self.registry(["attach", "6cb"]).lookup()[0]["sessionId"], JOB_SESSION)
        self.assertEqual(self.registry(["attach", "6c"]).lookup(), (None, None))

    def test_a_name_works_in_place_of_the_id(self):
        self.job(JOB, sessionId=JOB_SESSION, name="iPhone подключение и очистка диска")
        self.job("51759cbe", name="configure-model-and-effort")
        self.bg(333, JOB, JOB_SESSION)
        self.assertEqual(self.registry(["attach", "IPHONE"]).lookup()[0]["sessionId"],
                         JOB_SESSION)

    def test_a_name_two_jobs_share_names_neither(self):
        # Claude Code lists both and exits; so does the child, then.
        self.job(JOB, name="backlog task selection")
        self.job("911654fe", name="iOS backlog tasks selection")
        self.bg(333, JOB, JOB_SESSION)
        reg = self.registry(["attach", "selection"])
        self.assertEqual(reg.lookup(), (None, None))
        self.assertTrue([ln for ln in self.logged if "2 background jobs match" in ln])

    def test_text_that_could_start_an_id_is_never_read_as_a_name(self):
        self.job(JOB, name="abc")                      # Claude Code: "Use more of the name."
        self.bg(333, JOB, JOB_SESSION)
        self.assertEqual(self.registry(["attach", "abc"]).lookup(), (None, None))

    def test_the_job_says_which_session_it_is_when_no_record_does(self):
        # A build whose background record has no `jobId`, or a session the
        # attach is still waking: what the daemon resumes the job from.
        elsewhere = self.project("/elsewhere")
        scan = os.path.join(elsewhere, MOVED_ON + ".jsonl")
        self.job(JOB, sessionId=JOB_SESSION, resumeSessionId=MOVED_ON, linkScanPath=scan,
                 cwd="/elsewhere")
        session_record(self.sessions, 333, sessionId=MOVED_ON, kind="bg")   # no jobId
        rec, why = self.registry(["attach", JOB]).lookup()
        self.assertEqual((rec["sessionId"], rec["transcriptPath"]), (MOVED_ON, scan))
        self.assertIn("state.json", why)

    def test_a_job_never_cleared_is_the_session_it_was_dispatched_as(self):
        self.job(JOB, sessionId=JOB_SESSION)
        rec, _ = self.registry(["attach", JOB]).lookup()
        self.assertEqual(rec["sessionId"], JOB_SESSION)

    def test_the_sessions_own_word_outranks_its_jobs(self):
        self.job(JOB, sessionId=JOB_SESSION)              # not yet told of the /clear
        self.bg(333, JOB, MOVED_ON)
        self.assertEqual(self.registry(["attach", JOB]).lookup()[0]["sessionId"], MOVED_ON)

    def test_a_respawn_overlapping_the_worker_it_replaces_is_the_one(self):
        self.job(JOB)
        self.bg(333, JOB, "the-old-worker", startedAt=1000)
        self.bg(555, JOB, "the-respawn", startedAt=2000)
        self.assertEqual(self.registry(["attach", JOB]).lookup()[0]["sessionId"], "the-respawn")

    def test_the_transcript_is_where_the_session_writes_not_where_we_were_started(self):
        self.job(JOB, cwd="/elsewhere")
        self.bg(333, JOB, MOVED_ON, cwd="/elsewhere")
        rec, _ = self.registry(["attach", JOB]).lookup()
        self.assertEqual(rec["transcriptPath"],
                         os.path.join(self.project("/elsewhere"), MOVED_ON + ".jsonl"))
        # ...and a file the job's own linkScanPath points into wins over a
        # directory worked out from a cwd (a worktree, a session that moved).
        tree = self.project("/elsewhere/.claude/worktrees/x")
        with open(os.path.join(tree, MOVED_ON + ".jsonl"), "w") as fh:
            fh.write("{}\n")
        self.job(JOB, cwd="/elsewhere", linkScanPath=os.path.join(tree, "older.jsonl"))
        rec, _ = self.registry(["attach", JOB]).lookup()
        self.assertEqual(rec["transcriptPath"], os.path.join(tree, MOVED_ON + ".jsonl"))

    def test_the_roster_is_nobody_whatever_is_written(self):
        session_record(self.sessions, 111, sessionId="the-roster-itself")
        session_record(self.sessions, 112, sessionId="its-child")
        self.write_neighbour()
        reg = self.registry(["agents"], proc_table=lambda: {112: 111})
        for _ in range(3):
            self.assertEqual(reg.lookup(), (None, None))

    def test_only_a_session_of_the_childs_own_may_be_guessed_at(self):
        self.assertTrue(getattr(self.registry([]), "may_guess", True))
        self.assertTrue(getattr(self.registry(["--continue"]), "may_guess", True))
        for argv in (["attach", JOB], ["attach"], ["agents"], ["--debug", "agents"]):
            with self.subTest(argv=argv):
                self.assertFalse(getattr(self.registry(argv), "may_guess", True))

    def test_an_attach_with_nothing_to_attach_to_is_still_an_attach(self):
        session_record(self.sessions, 111, sessionId="usage-and-exit")
        self.assertEqual(self.registry(["attach"]).lookup(), (None, None))


class TestAnAttachIsFollowedByItsJob(Jobs, unittest.TestCase):
    """T33 from end to end, without a pty: the registry `main()` builds for
    `claude attach 6cb059d2`, the real watcher and the real controller.

    The field case, wrapper 45890 in voxik: the attached session's
    transcript is older than the wrapper; a neighbour's interactive session
    starts in the same directory while it runs. Its transcript is the fresh
    one, and fresh is what the growth heuristic prefers."""

    THRESHOLD = 510000
    T0 = 1000.0

    def setUp(self):
        self.make_home()
        self.dir = self.project()
        self.typed = []

    def start(self, argv=("attach", JOB), project=None):
        cfg = dict(cr.CFG, context_tokens=self.THRESHOLD, wait_scale=1, margin=0,
                   user_idle=0, root_idle=20, handoff_file=os.path.join(self.home, "handoff.md"))
        self.ctl = cr.Controller(cfg, self.logged.append, now=self.T0)
        self.reg = registry_for(list(argv), 111, self.home, log=self.logged.append)
        self.w = cr.TranscriptWatcher(project or self.dir, poll=0, now=self.T0,
                                      registry=self.reg, log=self.logged.append)

    def row(self, path, rec):
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def turn(self, at):
        """One turn of the supervisor's loop, as `main()` takes it."""
        w, ctl, now = self.w, self.ctl, self.T0 + at
        recs = w.poll_now(now)
        if w.current and w.current != ctl.context_path:
            ctl.bind_transcript(w.current, "session identity" if w.bound_session_id
                                else "fallback heuristic", now)
        ctl.on_candidates(w.guessing and len(w.candidates) > 1 and not w.confirmed
                          and not w.bound_session_id, now)
        for rec in recs:
            ours = rec["path"] == w.current
            if rec["kind"] == "alive" and ours:
                ctl.on_context(rec, now)
                ctl.on_alive(now, "transcript", rec["path"])
            elif rec["kind"] == "limit":
                ctl.on_limit(rec["text"], now, "transcript" if ours else "neighbour")
        ctl.note_growth(w.grown, now)
        if w.agent_status is not None:
            ctl.on_agent_status(w.agent_status, w.agent_status_at)
        action = ctl.tick(now)
        if action and action[0] == "inject":
            self.typed.append(action[1])
        return recs

    def test_a_session_started_beside_an_attach_is_never_its_session(self):
        ours = os.path.join(self.dir, MOVED_ON + ".jsonl")
        theirs = os.path.join(self.dir, "1b900c55-theirs.jsonl")
        self.row(ours, answered(400000, "claude-opus-5"))      # two days of history
        self.job(JOB, sessionId=JOB_SESSION, resumeSessionId=MOVED_ON, linkScanPath=ours)
        self.start()
        self.row(ours, answered(402000, "claude-opus-5"))
        self.turn(0.0)
        self.assertEqual(self.w.current, ours)
        session_record(self.sessions, 222, sessionId="1b900c55-theirs", startedAt=STARTED + 60000)
        # The neighbour gets going, at a size that is past our threshold, and
        # outlasts every wait there is; ours goes quiet in the meantime.
        for at in (2.0, 31.0, 33.0, 120.0, 3600.0, 7200.0):
            self.row(theirs, answered(700000, "claude-haiku-4-5"))
            self.turn(at)
            self.assertEqual(self.w.current, ours)
            self.assertEqual(self.ctl.context_path, ours)
            self.assertEqual(self.ctl.context_tokens, 402000)
        self.turn(7300.0)                    # theirs quiet too: a fold would be due now
        self.assertEqual(self.typed, [])
        said = "\n".join(self.logged)
        self.assertIn("session bound: %s (background job %s, from its state.json)"
                      % (MOVED_ON, JOB), said)
        for wrong in ("1b900c55", "falling back", "fallback heuristic", "700k", "folding"):
            self.assertNotIn(wrong, said)
        # ...and its own count, when it gets there, is the one that folds it.
        self.row(ours, answered(520000, "claude-opus-5"))
        self.turn(7310.0)
        self.turn(7340.0)
        self.assertEqual(len(self.typed), 1, self.logged)
        self.assertIn("folding up", "\n".join(self.logged))

    def test_a_clear_in_the_attached_session_moves_the_attach_with_it(self):
        self.job(JOB, sessionId=JOB_SESSION)
        self.bg(333, JOB, JOB_SESSION)
        self.start()
        self.turn(0.0)
        self.assertEqual(self.w.current, os.path.join(self.dir, JOB_SESSION + ".jsonl"))
        self.bg(333, JOB, MOVED_ON)                       # /clear: same process, new session
        self.row(os.path.join(self.dir, MOVED_ON + ".jsonl"), answered(5000, "claude-opus-5"))
        self.turn(2.0)
        self.assertEqual(self.w.current, os.path.join(self.dir, MOVED_ON + ".jsonl"))
        self.assertEqual(self.ctl.context_tokens, 5000)     # its first row, read
        self.assertIn("session rebound: %s → %s (background job %s)" % (JOB_SESSION, MOVED_ON, JOB),
                      "\n".join(self.logged))

    def test_an_attach_from_another_directory_does_not_replay_the_sessions_past(self):
        # `claude attach` run from home: the session writes under its own
        # cwd, which the watcher never scanned, so nothing there was seeded.
        elsewhere = self.project("/elsewhere")
        ours = os.path.join(elsewhere, MOVED_ON + ".jsonl")
        self.row(ours, REAL)                                  # yesterday's limit
        self.row(ours, answered(400000, "claude-opus-5"))
        self.job(JOB, cwd="/elsewhere")
        self.bg(333, JOB, MOVED_ON, cwd="/elsewhere")
        self.start()
        recs = self.turn(0.0)
        self.assertEqual(self.w.current, ours)
        self.assertEqual(recs, [])
        self.assertEqual(self.ctl.state, cr.IDLE)
        self.row(ours, answered(450000, "claude-opus-5"))     # today's, read
        self.turn(2.0)
        self.assertEqual(self.ctl.context_tokens, 450000)
        # After a /clear its new transcript is news from the first row — read
        # a poll after the rebind, being in no directory the scan covers.
        self.bg(333, JOB, "after-the-clear", cwd="/elsewhere")
        self.row(os.path.join(elsewhere, "after-the-clear.jsonl"), answered(6000, "claude-opus-5"))
        self.turn(4.0)
        self.turn(6.0)
        self.assertEqual(self.ctl.context_tokens, 6000)

    def test_the_roster_follows_nothing_and_never_says_it_is_guessing(self):
        self.start(argv=("agents",))
        theirs = os.path.join(self.dir, "1b900c55-theirs.jsonl")
        for at in (0.0, 2.0, 31.0, 33.0, 3600.0):
            self.row(theirs, answered(700000, "claude-haiku-4-5"))
            self.turn(at)
            self.assertIsNone(self.w.current)
            self.assertIsNone(self.ctl.context_path)
        self.row(theirs, REAL)
        self.assertEqual([r["kind"] for r in self.turn(3602.0)], ["limit"])   # still read
        self.assertEqual(self.ctl.state, cr.WAITING)
        self.assertEqual(self.typed, [])
        self.assertFalse([ln for ln in self.logged if "falling back" in ln or "bound" in ln])


class TestContextFigures(unittest.TestCase):
    """The context trigger does not count tokens; it reads the count Claude wrote.

    Every assistant row carries the API's own `usage`, and the three input
    counters in it are the prompt that was sent. Anything this layer gets wrong
    is a restart at the wrong moment — or, worse, never.
    """

    def test_the_three_input_counters_are_the_context(self):
        self.assertEqual(cr.usage_tokens(
            {"input_tokens": 2, "cache_creation_input_tokens": 1093,
             "cache_read_input_tokens": 611317, "output_tokens": 6858}), 612412)

    def test_output_tokens_are_not_context(self):
        # Not until the next turn quotes them back, and by then they are inside
        # cache_creation. Adding them here would count them twice.
        self.assertEqual(cr.usage_tokens({"input_tokens": 10, "output_tokens": 9999}), 10)

    def test_a_row_without_usage_says_nothing(self):
        self.assertIsNone(cr.usage_tokens(None))
        self.assertIsNone(cr.usage_tokens({}))
        self.assertIsNone(cr.usage_tokens("not a dict"))

    def test_missing_counters_do_not_zero_the_others(self):
        self.assertEqual(cr.usage_tokens({"cache_read_input_tokens": 500}), 500)

    def test_all_zero_counters_say_nothing(self):
        # "<synthetic>" rows ("No response requested", an interrupted request)
        # carry a real usage dict with every counter at 0. That is not a context
        # of zero tokens; it is a row with nothing to report, same as no usage
        # dict at all.
        self.assertIsNone(cr.usage_tokens(
            {"input_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "output_tokens": 0}))


class TestModelWindows(unittest.TestCase):
    def test_the_slug_the_transcript_actually_writes(self):
        self.assertEqual(cr.model_window("claude-opus-5"), 1000000)
        self.assertEqual(cr.model_window("claude-haiku-4-5"), 200000)

    def test_a_dated_slug_resolves_to_the_family(self):
        self.assertEqual(cr.model_window("claude-haiku-4-5-20251001"), 200000)

    def test_a_bracketed_suffix_is_not_part_of_the_slug(self):
        self.assertEqual(cr.model_window("claude-opus-5[1m]"), 1000000)

    def test_an_unknown_slug_is_not_guessed(self):
        # The caller leaves the window unset and goes and looks it up; the rest
        # of that story is in test_models.py.
        self.assertIsNone(cr.model_window("claude-something-9"))
        self.assertIsNone(cr.model_window(None))

    def test_window_sizes_are_written_the_way_people_write_them(self):
        self.assertEqual(cr.parse_tokens("200k"), 200000)
        self.assertEqual(cr.parse_tokens("1M"), 1000000)
        self.assertEqual(cr.parse_tokens("1_000_000"), 1000000)
        self.assertEqual(cr.parse_tokens("450000"), 450000)

    def test_auto_is_not_a_size(self):
        for text in ("auto", "", None, "lots", "0"):
            self.assertIsNone(cr.parse_tokens(text), text)


def assistant(text="ok", tokens=None, model="claude-opus-5", stop="end_turn",
              sidechain=False):
    msg = {"role": "assistant", "model": model, "stop_reason": stop,
           "content": [{"type": "text", "text": text}]}
    if tokens is not None:
        msg["usage"] = {"input_tokens": 2, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": tokens - 2, "output_tokens": 100}
    return {"type": "assistant", "isSidechain": sidechain, "message": msg}


class TestAssistantRows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-ar-")
        self.path = os.path.join(self.dir, "s.jsonl")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, *records):
        with open(self.path, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def rows(self):
        return cr.transcript_limit_records(self.path, 0)[1]

    def test_an_alive_row_carries_the_figures(self):
        self.write(assistant(tokens=612412))
        row = self.rows()[0]
        self.assertEqual(row["tokens"], 612412)
        self.assertEqual(row["model"], "claude-opus-5")
        self.assertEqual(row["stop_reason"], "end_turn")
        self.assertFalse(row["sidechain"])

    def test_a_collapse_keeps_the_newest_figures(self):
        # One answer is many rows. Collapsing them to the FIRST would freeze the
        # context reading at whatever it was when the answer started, which for a
        # long answer is a threshold that arrives an answer late.
        self.write(assistant(tokens=100, stop="tool_use"),
                   assistant(tokens=200, stop="tool_use"),
                   assistant(tokens=300, stop="end_turn"))
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["alive"])
        self.assertEqual(rows[0]["tokens"], 300)
        self.assertEqual(rows[0]["stop_reason"], "end_turn")

    def test_a_subagents_row_is_marked_as_one(self):
        # A subagent writes into the session's own transcript, and its usage is
        # its context, not the session's.
        self.write(assistant(tokens=40000, sidechain=True))
        self.assertTrue(self.rows()[0]["sidechain"])

    def test_a_collapse_does_not_overwrite_end_turn_with_none(self):
        # The row that closes the fold turn ("end_turn") can be followed in the
        # same poll by a streaming fragment of the NEXT turn, whose stop_reason
        # is still None because it hasn't finished. Losing "end_turn" here is
        # what makes _handoff_fault see stop_reason=tool_use and call the fold
        # failed when it actually landed.
        self.write(assistant(tokens=100, stop="end_turn"),
                   assistant(tokens=200, stop=None))
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["alive"])
        self.assertEqual(rows[0]["tokens"], 200)
        self.assertEqual(rows[0]["stop_reason"], "end_turn")

    def test_a_collapse_does_not_overwrite_end_turn_with_none_reversed(self):
        # Same guarantee in the other order: a real stop_reason still wins over
        # an earlier None once it arrives.
        self.write(assistant(tokens=100, stop=None),
                   assistant(tokens=200, stop="end_turn"))
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["alive"])
        self.assertEqual(rows[0]["tokens"], 200)
        self.assertEqual(rows[0]["stop_reason"], "end_turn")

    def test_a_sidechain_row_never_merges_with_a_root_row(self):
        # A root row immediately followed by a subagent's row must not collapse
        # into one: that would either hide the root's real figures behind the
        # subagent's small ones, or mark the root's tokens as a sidechain's and
        # make Controller.on_context skip the observation entirely.
        self.write(assistant(tokens=500000, sidechain=False),
                   assistant(tokens=20000, sidechain=True))
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["alive", "alive"])
        self.assertEqual(rows[0]["tokens"], 500000)
        self.assertFalse(rows[0]["sidechain"])
        self.assertEqual(rows[1]["tokens"], 20000)
        self.assertTrue(rows[1]["sidechain"])

    def test_a_row_with_no_usage_still_says_the_session_answered(self):
        self.write(assistant())
        row = self.rows()[0]
        self.assertEqual(row["kind"], "alive")
        self.assertIsNone(row["tokens"])


def compact_boundary(pre_tokens=190000):
    """claude's own row for compacting the context on its own (T19)."""
    return {"type": "system", "subtype": "compact_boundary",
            "timestamp": "2026-09-19T00:00:00.000Z",
            "compactMetadata": {"trigger": "auto", "preTokens": pre_tokens}}


class TestCompactBoundaryRows(unittest.TestCase):
    """T19: the one row that says outright how large the context actually was
    right before claude decided on its own that it was full."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-cb-")
        self.path = os.path.join(self.dir, "s.jsonl")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, *records):
        with open(self.path, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def rows(self):
        return cr.transcript_limit_records(self.path, 0)[1]

    def test_it_parses_into_an_alive_record_with_the_pre_compaction_count(self):
        self.write(compact_boundary(190000))
        row = self.rows()[0]
        self.assertEqual(row["kind"], "alive")
        self.assertTrue(row["quiet"])
        self.assertTrue(row["compacted"])
        self.assertEqual(row["pre_tokens"], 190000)

    def test_it_never_merges_with_a_neighbouring_assistant_row(self):
        self.write(assistant(tokens=100), compact_boundary(190000), assistant(tokens=50))
        rows = self.rows()
        self.assertEqual([r.get("compacted") for r in rows], [None, True, None])
        self.assertEqual(rows[0]["tokens"], 100)
        self.assertEqual(rows[2]["tokens"], 50)


class TestWhichTranscriptIsOurs(unittest.TestCase):
    """A project directory holds more than one live transcript. For a limit that
    never mattered — a limit is the account's. For the context trigger it is the
    whole question: another session's usage read as ours is a restart at the
    wrong moment, or none at all."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-cur-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def append(self, name, rec, mtime=None):
        p = os.path.join(self.dir, name)
        with open(p, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_every_record_says_which_file_it_came_from(self):
        w = cr.TranscriptWatcher(self.dir, poll=0)
        p = self.append("a.jsonl", assistant(tokens=5))
        self.assertEqual(w.poll_now()[0]["path"], p)

    def test_the_transcript_that_did_not_exist_before_us_is_ours(self):
        # `claude` writes its transcript within a second of starting; anything
        # else in the directory was there first.
        old = self.append("old.jsonl", assistant(tokens=1), mtime=1000)
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("old.jsonl", assistant(tokens=2), mtime=3000)
        mine = self.append("mine.jsonl", assistant(tokens=3), mtime=2000)
        w.poll_now()
        self.assertEqual(w.current, mine)
        self.assertEqual(sorted(w.grown), sorted([old, mine]))

    def test_it_stays_with_the_file_it_picked_while_that_file_grows(self):
        # Another session writing a burst in between must not steal the reading.
        w = cr.TranscriptWatcher(self.dir, poll=0)
        mine = self.append("mine.jsonl", assistant(tokens=1), mtime=1000)
        w.poll_now()
        self.assertEqual(w.current, mine)
        self.append("mine.jsonl", assistant(tokens=2), mtime=1000)
        self.append("other.jsonl", assistant(tokens=3), mtime=9000)
        w.poll_now()
        self.assertEqual(w.current, mine)

    def test_it_follows_the_session_into_a_new_file(self):
        # Which is what `/clear` looks like from here: the old transcript stops
        # and a new one appears.
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("before.jsonl", assistant(tokens=1), mtime=1000)
        w.poll_now()
        after = self.append("after.jsonl", assistant(tokens=2), mtime=2000)
        w.poll_now()
        self.assertEqual(w.current, after)

    def test_nothing_grew_means_nothing_grew(self):
        w = cr.TranscriptWatcher(self.dir, poll=0)
        self.append("a.jsonl", assistant(tokens=1))
        w.poll_now()
        w.poll_now()
        self.assertEqual(w.grown, [])


class TestSeveralEchoes(unittest.TestCase):
    """A restart types more than one thing, and each of them has to be
    recognisable coming back."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-ec-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def append(self, name, rec):
        with open(os.path.join(self.dir, name), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_any_of_the_messages_we_type_is_an_echo(self):
        w = cr.TranscriptWatcher(self.dir, poll=0,
                                 echo=["continue", "Read `h.md` and continue from it."])
        self.append("s.jsonl", user_row("Read `h.md` and continue from it."))
        found = w.poll_now()
        self.assertEqual([r["kind"] for r in found], ["echo"])
        self.assertEqual(found[0]["text"], "Read `h.md` and continue from it.")

    def test_a_message_with_characters_json_escapes_still_matches(self):
        # The prefilter builds its needle with json.dumps for exactly this: a
        # quote or a backslash is written escaped in the row.
        msg = 'continue with "the plan" \\ now'
        w = cr.TranscriptWatcher(self.dir, poll=0, echo=[msg])
        self.append("s.jsonl", user_row(msg))
        self.assertEqual([r["kind"] for r in w.poll_now()], ["echo"])

    def test_a_non_ascii_character_written_as_raw_utf8_still_matches(self):
        # Real Claude Code and codex write a non-ASCII character (an em dash
        # in the default handoff phrase, Cyrillic in a custom one) as its raw
        # UTF-8 bytes, never as a \uXXXX escape -- unlike json.dumps's own
        # default. The prefilter's needle has to be built the same way, or
        # the echo (and everything gated on it: /clear, the whole restart)
        # waits forever. This is exactly the encoding a live recording
        # against real Claude Code hit (docs/demo/record.py).
        msg = "continue — русский текст"
        w = cr.TranscriptWatcher(self.dir, poll=0, echo=[msg])
        with open(os.path.join(self.dir, "s.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(user_row(msg), ensure_ascii=False) + "\n")
        self.assertEqual([r["kind"] for r in w.poll_now()], ["echo"])

    # A phrase typed while a turn is still running, exactly as Claude Code
    # 2.1.280 wrote one into a live transcript: never a user row, only the
    # queue's own bookkeeping and then an attachment handed to the model.
    QUEUED = ("Wrap up now. Write a complete handoff — the very last line must "
              "be exactly HANDOFF-759560d5 and nothing else.")

    def write_raw(self, rec):
        with open(os.path.join(self.dir, "s.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_a_queued_phrase_is_reported_as_queued_then_echoed(self):
        w = cr.TranscriptWatcher(self.dir, poll=0, echo=[self.QUEUED])
        self.write_raw({"type": "queue-operation", "operation": "enqueue",
                        "timestamp": "2026-09-23T12:31:25.298Z", "content": self.QUEUED})
        found = w.poll_now()
        self.assertEqual([r["kind"] for r in found], ["queued"])
        self.assertEqual(found[0]["text"], self.QUEUED)
        self.write_raw({"type": "queue-operation", "operation": "remove",
                        "timestamp": "2026-09-23T12:31:29.731Z", "content": self.QUEUED,
                        "reason": "absorbed_mid_turn"})
        self.write_raw({"parentUuid": "ee8994fa", "isSidechain": False, "type": "attachment",
                        "attachment": {"type": "queued_command", "prompt": self.QUEUED,
                                       "commandMode": "prompt", "origin": {"kind": "human"},
                                       "humanTurn": True},
                        "timestamp": "2026-09-23T12:31:29.731Z"})
        found = w.poll_now()
        self.assertEqual([r["kind"] for r in found], ["echo"])
        self.assertEqual(found[0]["text"], self.QUEUED)

    def test_someone_elses_queued_message_is_not_ours(self):
        w = cr.TranscriptWatcher(self.dir, poll=0, echo=[self.QUEUED])
        self.write_raw({"type": "queue-operation", "operation": "enqueue",
                        "content": "something else entirely"})
        self.write_raw({"type": "attachment",
                        "attachment": {"type": "queued_command", "prompt": "something else"}})
        self.assertEqual(w.poll_now(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
