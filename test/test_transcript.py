"""The structured channel: Claude Code's own JSONL transcript.

A limited turn is written as a record carrying `"error": "rate_limit"` and
`"apiErrorStatus": 429`. Keying on that field instead of on the rendered text
removes the entire "text about an error vs. a live error" ambiguity that the
screen-scraping design can only ever approximate.
"""
import json
import os
import shutil
import tempfile
import unittest

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
    nothing about it has changed."""

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


class TestSessionRegistry(unittest.TestCase):
    """`ClaudeSessionRegistry.lookup()` in isolation: which of
    `sessions/*.json` is ours, given only a child pid and a cwd."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-reg-")
        self.sessions = os.path.join(self.dir, "sessions")
        os.makedirs(self.sessions)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, pid, **fields):
        rec = {"pid": pid, "sessionId": "s-%d" % pid, "cwd": "/work",
               "startedAt": 1000, "version": "2.1.273", "kind": "interactive",
               "status": "idle", "statusUpdatedAt": 1000}
        rec.update(fields)
        with open(os.path.join(self.sessions, "%d.json" % pid), "w") as fh:
            json.dump(rec, fh)
        return rec

    def registry(self, child_pid, **kw):
        kw.setdefault("proc_table", lambda: {})
        return cr.ClaudeSessionRegistry(child_pid, cwd="/work", config_dir=self.dir, **kw)

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

    def test_cwd_and_startedAt_fallback_when_not_a_descendant(self):
        self.write(300, startedAt=5000)
        rec, why = self.registry(999, started_at=4000).lookup()
        self.assertEqual(rec["sessionId"], "s-300")
        self.assertEqual(why, "cwd+startedAt")

    def test_a_different_cwd_is_never_a_candidate(self):
        self.write(400, cwd="/elsewhere", startedAt=0)
        rec, why = self.registry(999, started_at=0).lookup()
        self.assertIsNone(rec)

    def test_ambiguous_candidates_do_not_bind(self):
        self.write(500, startedAt=5000)
        self.write(501, startedAt=5000)
        rec, why = self.registry(999, started_at=4000).lookup()
        self.assertIsNone(rec)

    def test_no_session_registry_falls_back_and_logs_exactly_once(self):
        logged = []
        reg = self.registry(999, started_at=0, log=logged.append)
        for _ in range(3):
            rec, why = reg.lookup()
            self.assertIsNone(rec)
        matches = [ln for ln in logged if "no session registry for pid 999" in ln]
        self.assertEqual(len(matches), 1)
        self.assertIn("falling back to newest transcript", matches[0])


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
