"""The T26 test infrastructure itself: two agent-retrier.sh wrappers alive
over one project dir at once — the shape every "wrong session's numbers" bug
(T02, T04, T05) needs to reproduce, and that no single-`Session` pty test
(test_pty.py) can show.
"""
import json
import os
import shutil
import tempfile
import time
import unittest

import helper


def _read_sessions(sessions_dir):
    """pid -> record. Safe to call while the fakes are still writing: they
    write `<pid>.json.tmp` and rename it, so a name that is not `<pid>.json`
    yet, or a file gone by the time it is opened, is simply not an entry."""
    entries = {}
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return entries
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, name)) as fh:
                entries[int(name[:-5])] = json.load(fh)
        except (OSError, ValueError):
            continue
    return entries


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


JOB = "6cb059d2"
JOB_SESSION = "f123d92c-b953-401e-92e6-925f41126da5"


def lay_out_job(cfg_dir, cwd, history_tokens=400000, record=True):
    """A background job, the way Claude Code's daemon leaves one before
    anybody attaches to it (T33): `jobs/<id>/state.json`, the session's
    transcript under the job's cwd — older than any wrapper started after
    it, with the history to show for it — and the session's own
    `sessions/<pid>.json` (`kind: "bg"`, `jobId`), written by a daemon
    worker that is no process of ours."""
    slug = "".join(c if c.isalnum() else "-" for c in os.path.realpath(cwd))
    project = os.path.join(cfg_dir, "projects", slug)
    os.makedirs(project, exist_ok=True)
    transcript = os.path.join(project, JOB_SESSION + ".jsonl")
    with open(transcript, "w") as fh:
        fh.write(json.dumps({
            "type": "assistant", "isSidechain": False, "message": {
                "role": "assistant", "model": "claude-opus-5", "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1000,
                          "cache_read_input_tokens": history_tokens - 1002,
                          "output_tokens": 10},
                "content": [{"type": "text", "text": "two days of work"}]}}) + "\n")
    job = os.path.join(cfg_dir, "jobs", JOB)
    os.makedirs(job, exist_ok=True)
    with open(os.path.join(job, "state.json"), "w") as fh:
        json.dump({"state": "working", "template": "bg", "cwd": os.path.realpath(cwd),
                   "sessionId": JOB + "-1300-4191-8dd7-442e082370cd",
                   "resumeSessionId": JOB_SESSION, "linkScanPath": transcript,
                   "name": "iPhone and the disk"}, fh)
    if record:
        sessions = os.path.join(cfg_dir, "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "3999999.json"), "w") as fh:
            json.dump({"pid": 3999999, "sessionId": JOB_SESSION, "cwd": os.path.realpath(cwd),
                       "startedAt": int(time.time() * 1000) - 86400000, "kind": "bg",
                       "jobId": JOB, "status": "idle",
                       "statusUpdatedAt": int(time.time() * 1000)}, fh)
    return transcript


class TwoWrappersTestCase(unittest.TestCase):
    def two(self, cfg_a=None, cfg_b=None):
        d = tempfile.mkdtemp(prefix="cr-two-test-")
        a, b = helper.two_wrappers(d, cfg_a or {"agent": "claude"}, cfg_b or {"agent": "claude"})
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def one(self, cfg=None):
        d = tempfile.mkdtemp(prefix="cr-two-test-")
        a = helper.one_wrapper(d, cfg or {"agent": "claude"})
        self.addCleanup(a.close)
        return a

    def over_a_job(self, cfg_a, cfg_b=None, **job):
        """`a` started on a background job laid out beforehand (T33), and —
        given `cfg_b` — `b`, an ordinary session started beside it after
        it, in the same directory and the same `$CLAUDE_CONFIG_DIR`."""
        d = tempfile.mkdtemp(prefix="cr-two-test-")
        log, home = helper._shared_home()
        self.job_transcript = lay_out_job(home["CLAUDE_CONFIG_DIR"], d, **job)
        a = helper.WrapperSession(cwd=d, log=log, env=dict(home, **cfg_a))
        self.addCleanup(a.close)
        if cfg_b is None:
            return a, None
        self.assertTrue(a.read_until("ready"))
        b = helper.WrapperSession(cwd=d, log=log, env=dict(home, **cfg_b))
        self.addCleanup(b.close)
        return a, b


class TestTwoWrappers(TwoWrappersTestCase):
    def test_logs_stay_untangled_by_pid(self):
        a, b = self.two()
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        a.send("quit\r")
        b.send("quit\r")
        a.wait()
        b.wait()
        a_lines, b_lines = a.log_lines(), b.log_lines()
        self.assertTrue(a_lines)
        self.assertTrue(b_lines)
        other_tag = "[cr %d " % b.pid
        self.assertFalse(any(other_tag in line for line in a_lines))
        own_tag = "[cr %d " % a.pid
        self.assertFalse(any(own_tag in line for line in b_lines))

    def test_each_wrapper_gets_its_own_session_registry_entry(self):
        a, b = self.two()
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        a_pid, b_pid = a.agent_pid(), b.agent_pid()
        self.assertIsNotNone(a_pid)
        self.assertIsNotNone(b_pid)
        self.assertNotEqual(a_pid, b_pid)
        sessions_dir = os.path.join(os.path.dirname(a.log), "claude-config", "sessions")
        # "ready" is printed a moment before sessions/<pid>.json is written.
        helper.eventually(lambda: {a_pid, b_pid} <= set(_read_sessions(sessions_dir)))
        entries = _read_sessions(sessions_dir)
        self.assertIn(a_pid, entries)
        self.assertIn(b_pid, entries)
        self.assertNotEqual(entries[a_pid]["sessionId"], "")
        self.assertNotEqual(entries[a_pid]["sessionId"], entries[b_pid]["sessionId"])
        a.send("quit\r")
        b.send("quit\r")
        a.wait()
        b.wait()
        # gone on exit: a stale entry would tell a later wrapper a dead pid is live
        self.assertEqual(os.listdir(sessions_dir), [])

    def test_ten_runs_do_not_flap_and_finish_quickly(self):
        start = time.time()
        for _ in range(10):
            d = tempfile.mkdtemp(prefix="cr-two-flap-")
            a, b = helper.two_wrappers(d, {"agent": "claude"}, {"agent": "claude"})
            try:
                self.assertTrue(a.read_until("ready", timeout=10))
                self.assertTrue(b.read_until("ready", timeout=10))
                a.send("quit\r")
                b.send("quit\r")
                self.assertEqual(a.wait(timeout=10), 0)
                self.assertEqual(b.wait(timeout=10), 0)
            finally:
                a.close()
                b.close()
        self.assertLess(time.time() - start, 60)

    def test_close_kills_the_whole_process_group(self):
        # FAKE_WORKING never exits on its own — proof close() does not depend
        # on the fake cooperating (the live-codex-test-orphans case: a killed
        # pty driver leaving its supervisor behind).
        a, b = self.two({"agent": "claude", "FAKE_WORKING": "1"}, {"agent": "claude"})
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        wrapper_pid, agent_pid = a.pid, a.agent_pid()
        self.assertIsNotNone(agent_pid)
        a.close()
        # The wrapper was ours to reap and close() has; the agent was the
        # wrapper's, so it is init that reaps it, in its own time.
        helper.eventually(lambda: _gone(wrapper_pid) and _gone(agent_pid))
        with self.assertRaises(ProcessLookupError):
            os.kill(wrapper_pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(agent_pid, 0)   # setsid()'d out of our process group — the
                                    # orphan `close()` has to hunt down explicitly

    def test_a_session_growing_never_leaks_into_the_others_context(self):
        # The concrete bug class: one session's transcript keeps growing while
        # the other sits idle. Their sessions/<pid>.json entries — what T02's
        # identity binding reads — must stay distinguishable throughout.
        a, b = self.two({"agent": "claude", "FAKE_SCRIPT": "grow=50000:0.2"},
                        {"agent": "claude"})
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        a_pid, b_pid = a.agent_pid(), b.agent_pid()
        cfg_dir = os.path.join(os.path.dirname(a.log), "claude-config")
        sessions_dir = os.path.join(cfg_dir, "sessions")

        def grown():
            # Only a writes a transcript at all; three rows in is "kept growing".
            for root, _dirs, files in os.walk(os.path.join(cfg_dir, "projects")):
                for f in files:
                    if f.endswith(".jsonl"):
                        with open(os.path.join(root, f)) as fh:
                            return len(fh.readlines()) >= 3
            return False

        self.assertTrue(helper.eventually(grown))
        entries = _read_sessions(sessions_dir)
        self.assertEqual(entries[a_pid]["cwd"], entries[b_pid]["cwd"])
        self.assertNotEqual(entries[a_pid]["sessionId"], entries[b_pid]["sessionId"])

    def test_a_neighbours_answer_does_not_cancel_our_wait(self):
        # T04: `on_alive("transcript")` used to fire for ANY growing file in the
        # project dir, not just the one this wrapper owns — so b answering
        # cleared a's wait, which was waiting out a's own limit.
        base = {"agent": "claude", "CR_SCRAPE": "never", "CR_WAIT_SCALE": "3600",
                "CR_MARGIN_SEC": "0", "CR_USER_IDLE_SEC": "0", "CR_POLL_SEC": "0.2",
                "CR_NOTIFY": "1"}
        a, b = self.two(dict(base, FAKE_TRANSCRIPT="You've hit your weekly limit - resets in 40 hours"),
                        dict(base))
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        self.assertTrue(a.read_until("usage limit detected", timeout=15))
        b.send("answer\r")
        self.assertTrue(b.read_until("GOT:answer", timeout=15))
        # b's answer is in b's transcript (the fake writes the row before it
        # says GOT), and a reads every file in the project. One poll interval
        # so that a's next turn of the loop has to poll, then a line a can only
        # answer by taking that turn: whatever a made of b's row, it has made
        # it and said so. The line itself is a prompt, not an answer, and a
        # prompt ends no wait.
        time.sleep(0.2)                     # CR_POLL_SEC, as `base` sets it
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        self.assertNotIn("wait cancelled", a.buf)
        self.assertNotIn("session is answering again", "".join(a.log_lines()))
        # ...and it is still the wait it was, and still its own: a limit a
        # wrapper reads off the transcript it is bound to. Until T32 this
        # could say "(neighbour)" — started in the same instant as b, an a
        # whose sessions/<pid>.json was the later of the two to be written
        # was bound to b's for a poll (about every other run of this test),
        # and a limit row read in that poll was read as theirs.
        self.assertIn("limit detected (transcript)", "".join(a.log_lines()))
        self.assertNotIn("fake-session-%d" % b.agent_pid(), "".join(a.log_lines()))

    # -- T32: a session file that is late, the way a real one always is ------- #
    def bound(self, s):
        """The session `s` says it is bound to, once it says so."""
        said = helper.eventually(
            lambda: [ln for ln in s.log_lines() if "session bound: " in ln], timeout=15)
        self.assertTrue(said, "".join(s.log_lines()))
        return said[0].split("session bound: ", 1)[1].strip()

    def test_a_late_session_file_is_waited_for_not_guessed_at(self):
        # The race, held open. Real Claude Code writes sessions/<pid>.json a
        # second or two after it starts; the fake writes it at once, which is
        # why two fakes started together only ever raced by a few
        # milliseconds. Here a's takes a second — ten polls — and b's is there
        # from the first: in a's cwd, started after a was, the only candidate
        # there is. It is still b's.
        base = {"agent": "claude", "CR_SCRAPE": "never", "CR_POLL_SEC": "0.1"}
        a, b = self.two(dict(base, FAKE_SESSION_DELAY="1.0"), dict(base))
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        b_said, a_said = self.bound(b), self.bound(a)
        # (Asked for only now: a wrapper still looking for its session runs
        # `ps` every poll, and that is a child of its own for a moment.)
        a_pid, b_pid = a.agent_pid(), b.agent_pid()
        self.assertEqual(b_said, "fake-session-%d (pid file)" % b_pid)
        self.assertEqual(a_said, "fake-session-%d (pid file)" % a_pid)
        a_lines = "".join(a.log_lines())
        self.assertNotIn("fake-session-%d" % b_pid, a_lines)
        self.assertNotIn("session rebound", a_lines)
        self.assertNotIn("fallback heuristic", a_lines)

    def test_a_late_session_file_does_not_fold_us_on_the_neighbours_count(self):
        # What that binding could do while it lasted. b holds 700k and a
        # 50k, and only a folds at 510k; for three seconds a has no session
        # file of its own, and b's transcript is the one growing beside it.
        # Bound to b's, a read 700k as its own and — the moment b's turn went
        # quiet — typed the folding phrase into a session a tenth that size.
        # Then, told whose session it really was, it dropped the restart
        # without a word to the model it had just told to stop.
        base = helper.restart_timers(agent="claude", FAKE_MODEL="claude-opus-5")
        a, b = self.two(dict(base, CR_CONTEXT_TOKENS="510000", FAKE_USAGE="50000",
                             FAKE_SESSION_DELAY="3.0"),
                        dict(base, FAKE_USAGE="700000"))
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        self.bound(a)                    # to somebody; whom is the last thing checked
        own = "fake-session-%d" % a.agent_pid()
        self.assertTrue(helper.eventually(
            lambda: any(own in ln for ln in a.log_lines()), timeout=15))
        # a's fake answers lines in the order they were typed: anything a's
        # wrapper typed while it had no session of its own is answered before
        # this is.
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        self.assertNotIn("GOT:handoff", a.buf)
        a_lines = "".join(a.log_lines())
        self.assertNotIn("folding up", a_lines)
        self.assertNotIn("700k", a_lines)
        self.assertEqual(self.bound(a), own + " (pid file)")

    def test_a_claude_that_writes_no_session_file_is_still_followed(self):
        # The case the wait must not strand: a Claude Code older than the
        # file (`claude attach` is named by its job now, T33, below). Nobody
        # is ever named, so once the wait is
        # over the growth heuristic takes it from there, as it always did —
        # and the session is folded on its own count.
        base = helper.restart_timers(agent="claude", CR_CONTEXT_TOKENS="510000",
                                     FAKE_MODEL="claude-opus-5", FAKE_USAGE="700000",
                                     FAKE_SESSION_FILE="0", CR_SESSION_GRACE_SEC="1",
                                     FAKE_USAGE_DELAY="1.2")
        a = self.one(base)
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(a.read_until("GOT:handoff", timeout=20), a.buf[-500:])
        lines = a.log_lines()
        said = [ln for ln in lines if "no session registry for pid %d" % a.agent_pid() in ln]
        self.assertEqual(len(said), 1, "".join(lines))
        self.assertIn("after 1s; falling back to newest transcript", said[0])
        self.assertTrue([ln for ln in lines if "context bound: " in ln
                         and "(fallback heuristic)" in ln], "".join(lines))
        self.assertFalse([ln for ln in lines if "session bound" in ln])

    def test_a_limit_is_not_kept_waiting_for_a_name(self):
        # The wait for a name holds back what is read by path. A limit is
        # not read that way: it is the account's, and one that lands in those
        # thirty seconds is scheduled when it lands — not when somebody is
        # finally named, or given up on.
        a = self.one({"agent": "claude", "CR_SCRAPE": "never", "CR_WAIT_SCALE": "3600",
                      "CR_MARGIN_SEC": "0", "CR_POLL_SEC": "0.2", "CR_NOTIFY": "1",
                      "FAKE_SESSION_FILE": "0",
                      "FAKE_TRANSCRIPT": "You've hit your weekly limit - resets in 40 hours"})
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(a.read_until("usage limit detected", timeout=15))
        lines = "".join(a.log_lines())
        self.assertIn("limit detected (", lines)
        self.assertNotIn("falling back", lines)      # still inside the thirty seconds
        self.assertNotIn("context bound", lines)

    # -- T33: `claude attach` and the roster are no session of the child's ---- #
    def test_an_attach_follows_its_job_and_never_folds_on_a_neighbour(self):
        # Wrapper 45890, voxik, with the clock turned down. a is `claude
        # attach 6cb059d2`: its session is the job's, at 50k, in a transcript
        # older than a. b is somebody else's session, started beside it after
        # it, at 700k. The attach client writes no record of its own, so
        # until T33 a waited for one, gave up, and let the heuristic choose —
        # and the heuristic prefers the transcript that did not exist when a
        # started, which is b's: a folded its own session on b's count.
        base = helper.restart_timers(agent="claude", CR_CONTEXT_TOKENS="510000",
                                     FAKE_MODEL="claude-opus-5", CR_SESSION_GRACE_SEC="1")
        a, b = self.over_a_job(dict(base, args=["attach", JOB], FAKE_USAGE="50000"),
                               dict(base, FAKE_USAGE="700000", FAKE_USAGE_DELAY="1.5"))
        self.assertTrue(b.read_until("ready"))
        b_own = "fake-session-%d" % b.agent_pid()
        self.assertTrue(helper.eventually(lambda: self.rows(b, b_own) >= 2, timeout=15))
        time.sleep(helper.LEFT_ALONE)       # b quiet: when a bound to b would fold
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        self.assertNotIn("GOT:handoff", a.buf)
        lines = "".join(a.log_lines())
        self.assertIn("session bound: %s (background job %s)" % (JOB_SESSION, JOB), lines)
        for wrong in (b_own, "fallback heuristic", "falling back", "folding up", "700k"):
            self.assertNotIn(wrong, lines)

    def test_an_attach_folds_its_own_session_and_goes_through_the_clear_with_it(self):
        # What must go on working: an attach above its threshold is folded on
        # its own count, and the `/clear` moves it to the session's new
        # transcript the way it moves any session — its record says so.
        base = helper.restart_timers(agent="claude", CR_CONTEXT_TOKENS="510000",
                                     FAKE_MODEL="claude-opus-5")
        a, _ = self.over_a_job(dict(base, args=["attach", JOB], FAKE_USAGE="700000"))
        self.assertTrue(a.read_until("GOT:handoff", timeout=20), a.buf[-500:])
        self.assertTrue(a.read_until("GOT:/clear", timeout=30), a.buf[-500:])
        self.assertTrue(a.read_until("GOT:resume", timeout=30), a.buf[-500:])
        lines = "".join(a.log_lines())
        self.assertIn("session bound: %s (background job %s)" % (JOB_SESSION, JOB), lines)
        self.assertIn("session rebound: %s → fake-session-" % JOB_SESSION, lines)
        self.assertIn("(background job %s)" % JOB, lines.split("session rebound: ", 1)[1])
        self.assertTrue(helper.eventually(
            lambda: "context restarted" in "".join(a.log_lines()), timeout=15))

    def test_an_attach_whose_job_has_no_record_is_named_by_the_jobs_state(self):
        # A build whose background record carries no `jobId`: the job's own
        # state.json, which the session keeps up to date across a /clear.
        base = helper.restart_timers(agent="claude", CR_CONTEXT_TOKENS="510000",
                                     FAKE_MODEL="claude-opus-5", FAKE_BG_RECORD="0")
        a, _ = self.over_a_job(dict(base, args=["attach", JOB], FAKE_USAGE="700000"),
                               record=False)
        self.assertTrue(a.read_until("GOT:handoff", timeout=20), a.buf[-500:])
        self.assertTrue(a.read_until("GOT:resume", timeout=45), a.buf[-500:])
        lines = "".join(a.log_lines())
        self.assertIn("session bound: %s (background job %s, from its state.json)"
                      % (JOB_SESSION, JOB), lines)
        self.assertIn("session rebound: %s → fake-session-" % JOB_SESSION, lines)

    def rows(self, s, session):
        """How many rows `session`'s transcript has, wherever `s`'s claude
        config keeps it (0 before it exists)."""
        projects = os.path.join(os.path.dirname(s.log), "claude-config", "projects")
        for root, _dirs, files in os.walk(projects):
            if session + ".jsonl" in files:
                with open(os.path.join(root, session + ".jsonl")) as fh:
                    return len(fh.readlines())
        return 0

    def typed_into(self, s):
        """Every line `s`'s fake was handed that the test did not type itself."""
        return [ln for ln in s.buf.replace("\r", "\n").split("\n")
                if ln.startswith("GOT:") and not ln.startswith("GOT:tick-")]

    def test_the_roster_follows_no_transcript_and_folds_into_nothing(self):
        # a is `claude agents`: no session of its own and no transcript. Until
        # T33 it waited for a session file it would never get and handed the
        # directory to the heuristic, which took b's — and folded it. The fold
        # phrase went into the roster's one input box, which in the real
        # roster starts a new session with it. (b's own threshold is out of
        # reach, so that b's transcript goes quiet after its two rows.)
        base = helper.restart_timers(agent="claude", FAKE_MODEL="claude-opus-5",
                                     CR_SESSION_GRACE_SEC="1")
        a, b = self.two(dict(base, args=["agents"], CR_CONTEXT_TOKENS="510000"),
                        dict(base, CR_CONTEXT_TOKENS="900000", FAKE_USAGE="700000",
                             FAKE_USAGE_DELAY="1.5"))
        self.assertTrue(a.read_until("describe a task for a new session"))
        self.assertTrue(b.read_until("ready"))
        b_own = "fake-session-%d" % b.agent_pid()
        self.assertTrue(helper.eventually(lambda: self.rows(b, b_own) >= 2, timeout=15))
        time.sleep(helper.LEFT_ALONE)
        a.read_until("(nothing)", timeout=0.5)        # whatever it was typed by now
        self.assertEqual(self.typed_into(a), [])
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        self.assertEqual(self.typed_into(a), [])
        lines = "".join(a.log_lines())
        self.assertIn("the agent roster is no session of ours", lines)
        for wrong in ("context bound", "session bound", "falling back", "folding up", "700k"):
            self.assertNotIn(wrong, lines)

    def test_the_roster_types_nothing_when_a_limit_lifts(self):
        # b runs into the limit; it is the account's, and the roster reads it
        # and shows it. When it lifts, "continue" used to be typed into the
        # roster's box — a new session — and twice more when nothing came of
        # it.
        base = {"agent": "claude", "CR_SCRAPE": "never", "CR_WAIT_SCALE": "3600",
                "CR_MARGIN_SEC": "0", "CR_USER_IDLE_SEC": "0", "CR_POLL_SEC": "0.2",
                "CR_VERIFY_SEC": "1"}
        a, b = self.two(dict(base, args=["agents"]),
                        dict(base, FAKE_TRANSCRIPT="You've hit your session limit - resets in 1 hours"))
        self.assertTrue(a.read_until("describe a task for a new session"))
        self.assertTrue(helper.eventually(
            lambda: "limit detected (neighbour)" in "".join(a.log_lines()), timeout=15),
            "".join(a.log_lines()))
        # One second (an hour, scaled), then the moment a retry would go out,
        # and then the two more it would have taken.
        self.assertTrue(helper.eventually(
            lambda: "nothing typed" in "".join(a.log_lines())
            or "sending retry" in "".join(a.log_lines()), timeout=15))
        time.sleep(2.5)
        a.read_until("(nothing)", timeout=0.5)        # whatever it was typed by now
        self.assertEqual(self.typed_into(a), [])
        self.assertTrue(a.roundtrip(), a.buf[-500:])
        self.assertEqual(self.typed_into(a), [])
        lines = "".join(a.log_lines())
        self.assertIn("the limit is over; nothing typed", lines)
        self.assertNotIn("sending retry", lines)

    def test_only_the_session_above_the_threshold_is_folded(self):
        # The bug T02 removes: without registry-based identity, a's watcher
        # could pick up b's growth and read b's numbers as its own (or vice
        # versa), folding the wrong session or both.
        base = helper.restart_timers(agent="claude", CR_CONTEXT_TOKENS="510000",
                                     FAKE_MODEL="claude-opus-5")
        a, b = self.two(dict(base, FAKE_USAGE="700000"), dict(base, FAKE_USAGE="50000"))
        self.assertTrue(a.read_until("ready"))
        self.assertTrue(b.read_until("ready"))
        self.assertTrue(a.read_until("GOT:handoff", timeout=20), a.buf[-500:])
        # b gets every chance it would need to (wrongly) fold too. Started
        # together and on the same clocks, a b that had taken a's count for
        # its own would have decided to fold when a did — and a's phrase has
        # since taken 0.6s to be typed and answered. What is left to wait out
        # is how far apart two loops can reach the same decision (a poll and a
        # turn, 0.35s), several times.
        self.assertFalse(b.read_until("GOT:handoff", timeout=1.5))
        self.assertIn("folding up", "".join(a.log_lines()))
        self.assertNotIn("folding up", "".join(b.log_lines()))

    def test_two_sessions_sharing_a_handoff_file_get_two_different_ones(self):
        # T05: without this, the second wrapper to reach the fold overwrites
        # the first one's handoff.md mid-write, and the fold that reads it
        # back (or the one that never gets written at all) is a coin flip.
        #
        # Started one after the other's "ready" (its registry claim is made
        # before that point, well before the pty is even forked) rather than
        # via `two_wrappers()`'s simultaneous start: the registry has no lock,
        # by design (same "two sessions can race" tradeoff the model cache
        # makes), so two claims within the same instant can both see no
        # conflict — exactly the gap the evidence itself was not testing (its
        # two nonces landed two seconds apart, not the same millisecond).
        d = tempfile.mkdtemp(prefix="cr-two-handoff-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        shared = tempfile.mkdtemp(prefix="cr-two-handoff-cfg-")
        self.addCleanup(shutil.rmtree, shared, ignore_errors=True)
        log = os.path.join(shared, "shared.log")
        base = helper.restart_timers(
            CLAUDE_CONFIG_DIR=os.path.join(shared, "claude-config"),
            CODEX_HOME=os.path.join(shared, "codex-home"),
            agent="claude",
            CR_CONTEXT_TOKENS="510000",
            CR_HANDOFF_REGISTRY_DIR=os.path.join(shared, "handoff-registry"),
            FAKE_MODEL="claude-opus-5",
            FAKE_USAGE="700000")
        a = helper.WrapperSession(cwd=d, log=log, env=dict(base))
        self.addCleanup(a.close)
        self.assertTrue(a.read_until("ready"))

        b = helper.WrapperSession(cwd=d, log=log, env=dict(base))
        self.addCleanup(b.close)
        self.assertTrue(b.read_until("ready"))

        self.assertTrue(a.read_until("GOT:handoff", timeout=20), a.buf[-500:])
        self.assertTrue(b.read_until("GOT:handoff", timeout=20), b.buf[-500:])

        files = sorted(f for f in os.listdir(d) if f.startswith("handoff") and f.endswith(".md"))
        self.assertEqual(len(files), 2, files)
        self.assertIn("handoff.md", files)

        combined = "".join(a.log_lines()) + "".join(b.log_lines())
        self.assertIn("is taken by pid", combined)


if __name__ == "__main__":
    unittest.main()
