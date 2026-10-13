"""Consumer tests for the fake_claude.py / fake_codex.py scenario support
added for T26: each `FAKE_SCRIPT` directive and each codex-specific knob gets
at least one test here, so a scenario nobody exercises cannot silently rot.
"""
import json
import os
import tempfile
import time
import unittest

import helper
from test_two_wrappers import TwoWrappersTestCase


def _transcript_path(session):
    cfg_dir = os.path.join(os.path.dirname(session.log), "claude-config")
    for root, _dirs, files in os.walk(os.path.join(cfg_dir, "projects")):
        for f in files:
            if f.endswith(".jsonl"):
                return os.path.join(root, f)
    return None


def _transcript_rows(session):
    path = _transcript_path(session)
    if not path:
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestFakeClaudeScript(TwoWrappersTestCase):
    def test_grow_appends_a_climbing_turn_on_a_timer(self):
        a = self.one({"agent": "claude", "FAKE_SCRIPT": "grow=1000:0.15"})
        self.assertTrue(a.read_until("ready"))

        def three_turns():
            rows = _transcript_rows(a)
            return rows if len(rows) >= 3 else None

        rows = helper.eventually(three_turns)
        self.assertTrue(rows, "the transcript never reached three turns")
        tokens = [r["message"]["usage"]["input_tokens"] +
                  r["message"]["usage"]["cache_creation_input_tokens"] +
                  r["message"]["usage"]["cache_read_input_tokens"] +
                  r["message"]["usage"]["output_tokens"] for r in rows]
        self.assertEqual(tokens, sorted(tokens))
        self.assertLess(tokens[0], tokens[-1])

    def test_dropfirstfold_swallows_exactly_one_attempt(self):
        a = self.one({"agent": "claude", "FAKE_SCRIPT": "dropfirstfold"})
        self.assertTrue(a.read_until("ready"))
        fold = ("handoff to `.agent-retrier/handoff.md` — the reply must be "
                "exactly HANDOFF and nothing else")
        a.send(fold + "\r")
        # The fake answers in the order it was typed to, so once the line
        # typed AFTER the fold has its answer, the fold has had its turn —
        # and got none. (Sleeping here instead proved nothing: `buf` only
        # grows when it is read, so a GOT that did arrive was never in it.)
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        self.assertNotIn("GOT:handoff", a.buf)          # first attempt: lost
        a.send(fold + "\r")
        self.assertTrue(a.read_until("GOT:handoff", timeout=5))  # second: served

    def test_synthetic_row_has_zero_usage_and_synthetic_model(self):
        a = self.one({"agent": "claude", "FAKE_SCRIPT": "synthetic"})
        self.assertTrue(a.read_until("ready"))
        # Written at startup, before the fake reads a line — so once it has
        # answered one, the transcript is the startup rows and then that line,
        # and there is nothing more on its way.
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        rows = _transcript_rows(a)
        self.assertEqual([r["type"] for r in rows], ["assistant", "user"])
        msg = rows[0]["message"]
        self.assertEqual(msg["model"], "<synthetic>")
        self.assertEqual(sum(msg["usage"].values()), 0)

    def test_delayclear_stalls_the_clear_ack(self):
        a = self.one({"agent": "claude", "FAKE_SCRIPT": "delayclear=0.6"})
        self.assertTrue(a.read_until("ready"))
        start = time.time()
        a.send("/clear\r")
        self.assertTrue(a.read_until("GOT:/clear", timeout=5))
        self.assertGreaterEqual(time.time() - start, 0.55)

    def test_stream_writes_a_delta_row_then_the_closing_one(self):
        a = self.one({"agent": "claude", "FAKE_SCRIPT": "stream=8000"})
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(a.roundtrip(), a.buf[-500:])    # as for `synthetic` above
        rows = _transcript_rows(a)
        self.assertEqual([r["type"] for r in rows], ["assistant", "assistant", "user"])
        self.assertNotIn("stop_reason", rows[0]["message"])
        self.assertEqual(rows[1]["message"]["stop_reason"], "end_turn")

    def test_pid_file_status_follows_the_turn_and_clear_rebinds_sessionid(self):
        a = self.one({"agent": "claude"})
        self.assertTrue(a.read_until("ready"))
        agent_pid = a.agent_pid()
        self.assertIsNotNone(agent_pid)
        sessions_dir = os.path.join(os.path.dirname(a.log), "claude-config", "sessions")

        def entry():
            with open(os.path.join(sessions_dir, "%d.json" % agent_pid)) as fh:
                return json.load(fh)

        # "ready" is printed a moment before the file is first written.
        self.assertTrue(helper.eventually(
            lambda: os.path.exists(os.path.join(sessions_dir, "%d.json" % agent_pid))))
        self.assertEqual(entry()["status"], "idle")
        before_id = entry()["sessionId"]
        a.send("/clear\r")
        self.assertTrue(a.read_until("GOT:/clear"))
        after_id = entry()["sessionId"]
        self.assertNotEqual(before_id, after_id)


class TestFakeCodexScenarios(TwoWrappersTestCase):
    def test_dollar_and_slash_open_a_popup_that_eats_the_first_enter(self):
        a = self.one({"agent": "codex", "FAKE_POPUP": "1"})
        self.assertTrue(a.read_until("ready"))
        a.send("$supervisor continue from it\r")
        self.assertTrue(a.read_until("POPUP:$supervisor continue from it", timeout=5))
        self.assertNotIn("GOT:resume", a.buf)
        a.send("$supervisor continue from it\r")
        self.assertTrue(a.read_until("GOT:resume", timeout=5))

    def test_old_day_rollout_lands_under_a_backdated_directory(self):
        a = self.one({"agent": "codex", "FAKE_ROLLOUT_AGE_DAYS": "3"})
        self.assertTrue(a.read_until("ready"))
        # The rollout is started right after "ready" is printed, and before
        # the fake reads a line: an answer to one means it is there.
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        home = os.path.join(os.path.dirname(a.log), "codex-home", "sessions")
        today = time.strftime("%Y/%m/%d", time.gmtime())
        found_old, found_today = False, False
        for root, _dirs, files in os.walk(home):
            for f in files:
                rel = os.path.relpath(root, home)
                if rel == today:
                    found_today = True
                elif files:
                    found_old = True
        self.assertTrue(found_old)
        self.assertFalse(found_today)

    def test_subagent_rollout_stays_in_the_same_days_directory(self):
        a = self.one({"agent": "codex", "FAKE_SUBAGENT": "1"})
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(a.roundtrip(), a.buf[-500:])    # as for the backdated one above
        home = os.path.join(os.path.dirname(a.log), "codex-home", "sessions")
        today = time.strftime("%Y/%m/%d", time.gmtime())
        rows = []
        for root, _dirs, files in os.walk(home):
            for f in files:
                if f.endswith(".jsonl"):
                    rel = os.path.relpath(root, home)
                    self.assertEqual(rel, today)
                    with open(os.path.join(root, f)) as fh:
                        rows.append(json.loads(fh.readline()))
        self.assertEqual(rows[0]["payload"]["thread_source"], "subagent")


if __name__ == "__main__":
    unittest.main()
