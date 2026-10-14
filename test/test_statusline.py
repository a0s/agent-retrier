"""The `--cr-statusline` proxy (T20): claude's own statusline JSON relayed to
the supervisor without ever hiding the user's own statusline from the screen.

`claude_launch_args` decides whether `--settings` is added to the launch at
all; `run_statusline_proxy` is what actually runs once Claude Code invokes
the command it names; `StatusPoller` is how the supervisor reads the file
that proxy wrote. `Controller.on_status` (the fourth piece) has its own tests
in test_controller.py, next to `on_context`.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from helper import load

cr = load()


class TheStatusFileIsWrittenAtomically(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-statusline-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.status_path = os.path.join(self.dir, "status", "4242.json")

    def proxy(self, payload_text):
        stdin = io.StringIO(payload_text)
        stdout = io.StringIO()
        rc = cr.run_statusline_proxy(self.status_path, stdin=stdin, stdout=stdout)
        return rc, stdout.getvalue()

    def test_the_fields_the_supervisor_wants_are_recorded(self):
        self.proxy(json.dumps(dict(
            model=dict(id="claude-sonnet-5"),
            context_window=dict(context_window_size=200000),
            session_id="sess-1", transcript_path="/x/y.jsonl")))
        with open(self.status_path) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["model_id"], "claude-sonnet-5")
        self.assertEqual(rec["context_window_size"], 200000)
        self.assertEqual(rec["session_id"], "sess-1")
        self.assertEqual(rec["transcript_path"], "/x/y.jsonl")

    def test_the_directory_is_created_and_no_tmp_file_is_left_behind(self):
        self.proxy(json.dumps(dict(model=dict(id="claude-opus-5"),
                                   context_window=dict(context_window_size=1000000))))
        self.assertTrue(os.path.isdir(os.path.dirname(self.status_path)))
        leftovers = [f for f in os.listdir(os.path.dirname(self.status_path))
                    if f != os.path.basename(self.status_path)]
        self.assertEqual(leftovers, [])

    def test_a_second_write_replaces_the_first(self):
        self.proxy(json.dumps(dict(context_window=dict(context_window_size=200000))))
        self.proxy(json.dumps(dict(context_window=dict(context_window_size=1000000))))
        with open(self.status_path) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["context_window_size"], 1000000)

    def test_garbage_on_stdin_still_writes_something_sane(self):
        self.proxy("not json")
        with open(self.status_path) as fh:
            rec = json.load(fh)
        self.assertIsNone(rec["model_id"])
        self.assertIsNone(rec["context_window_size"])


class TheUsersOwnStatuslineStillRuns(unittest.TestCase):
    """The whole point of the proxy: someone with their own statusLine must
    still see it, chained through ours rather than replaced by it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-statusline-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.status_path = os.path.join(self.dir, "status", "1.json")
        self.cwd = os.path.join(self.dir, "work")
        os.makedirs(self.cwd)
        # Isolate from whatever real ~/.claude/settings.json this machine
        # happens to have -- the whole point of these tests is what happens
        # with NO user settings in the way, and the fallback in
        # `_user_statusline_command` would otherwise read the developer's own.
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.dir, "claude-config")
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)

    def write_settings(self, command, local=False):
        d = os.path.join(self.cwd, ".claude")
        os.makedirs(d, exist_ok=True)
        name = "settings.local.json" if local else "settings.json"
        with open(os.path.join(d, name), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": command}}, fh)

    def payload(self, **over):
        base = dict(model=dict(id="claude-sonnet-5"),
                   context_window=dict(context_window_size=200000),
                   session_id="sess-1", transcript_path="/x/y.jsonl",
                   workspace=dict(current_dir=self.cwd, project_dir=self.cwd))
        base.update(over)
        return base

    def proxy(self, payload):
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        rc = cr.run_statusline_proxy(self.status_path, stdin=stdin, stdout=stdout)
        return rc, stdout.getvalue()

    def test_the_users_command_runs_with_the_same_stdin_and_its_output_wins(self):
        script = os.path.join(self.dir, "user-status.py")
        with open(script, "w") as fh:
            fh.write(
                "import json, sys\n"
                "d = json.loads(sys.stdin.read())\n"
                "print('USER window=%s' % d['context_window']['context_window_size'])\n")
        self.write_settings("%s %s" % (sys.executable, script))
        rc, out = self.proxy(self.payload())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "USER window=200000")

    def test_no_statusline_configured_is_silence_not_a_fabrication(self):
        rc, out = self.proxy(self.payload())
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_a_local_override_wins_over_the_shared_one(self):
        self.write_settings("echo shared", local=False)
        self.write_settings("echo local", local=True)
        rc, out = self.proxy(self.payload())
        self.assertEqual(out.strip(), "local")

    def test_the_exit_code_is_forwarded_too(self):
        script = os.path.join(self.dir, "fail.py")
        with open(script, "w") as fh:
            fh.write("import sys\nsys.exit(3)\n")
        self.write_settings("%s %s" % (sys.executable, script))
        rc, _out = self.proxy(self.payload())
        self.assertEqual(rc, 3)

    def test_the_status_file_is_still_written_even_with_a_users_statusline(self):
        script = os.path.join(self.dir, "user-status.py")
        with open(script, "w") as fh:
            fh.write("import sys\nsys.stdin.read()\nprint('ok')\n")
        self.write_settings("%s %s" % (sys.executable, script))
        self.proxy(self.payload())
        with open(self.status_path) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["context_window_size"], 200000)


class TestStatusPoller(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-statuspoll-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "1.json")

    def write(self, **rec):
        with open(self.path, "w") as fh:
            json.dump(rec, fh)

    def test_nothing_before_the_file_exists(self):
        poller = cr.StatusPoller(self.path, poll=0)
        self.assertIsNone(poller.poll_now(0))

    def test_a_change_is_returned_once(self):
        poller = cr.StatusPoller(self.path, poll=0)
        self.write(context_window_size=200000)
        got = poller.poll_now(1)
        self.assertEqual(got["context_window_size"], 200000)
        self.assertIsNone(poller.poll_now(2))     # unchanged: nothing new to report

    def test_the_poll_interval_is_honoured(self):
        poller = cr.StatusPoller(self.path, poll=10)
        self.write(context_window_size=200000)
        self.assertIsNotNone(poller.poll_now(0))
        # mtime resolution can be coarse; force the file to look newer.
        self.write(context_window_size=1000000)
        os.utime(self.path, (5, 5))
        self.assertIsNone(poller.poll_now(5))     # too soon: next_poll not reached yet
        self.assertIsNotNone(poller.poll_now(11))

    def test_a_corrupt_file_is_simply_not_an_answer(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        poller = cr.StatusPoller(self.path, poll=0)
        self.assertIsNone(poller.poll_now(0))


class TestClaudeLaunchArgsAddTheStatuslineProxy(unittest.TestCase):
    def setUp(self):
        os.environ["CR_SELF"] = "/opt/bin/agent-retrier.sh"
        self.addCleanup(os.environ.pop, "CR_SELF", None)

    def cfg(self, **over):
        base = dict(context_tokens=135200, context_restart_on=False,
                   statusline_proxy=True, status_dir="/tmp/cr-status-test")
        base.update(over)
        return base

    def test_settings_names_our_own_proxy(self):
        args = cr.claude_launch_args(self.cfg(), "claude", [])
        self.assertEqual(args[0], "--settings")
        payload = json.loads(args[1])
        self.assertEqual(payload["statusLine"]["type"], "command")
        self.assertIn("--cr-statusline", payload["statusLine"]["command"])
        self.assertIn("/opt/bin/agent-retrier.sh", payload["statusLine"]["command"])

    def test_never_for_codex(self):
        self.assertEqual(cr.claude_launch_args(self.cfg(), "codex", []), [])

    def test_not_without_a_restart_to_inform(self):
        self.assertEqual(
            cr.claude_launch_args(self.cfg(context_tokens=0), "claude", []), [])

    def test_the_bare_flag_is_enough_too(self):
        args = cr.claude_launch_args(
            self.cfg(context_tokens=0, context_restart_on=True), "claude", [])
        self.assertEqual(args[0], "--settings")

    def test_the_off_switch(self):
        self.assertEqual(
            cr.claude_launch_args(self.cfg(statusline_proxy=False), "claude", []), [])

    def test_the_users_own_settings_flag_always_wins(self):
        self.assertEqual(
            cr.claude_launch_args(self.cfg(), "claude", ["--settings", "{}"]), [])
        self.assertEqual(
            cr.claude_launch_args(self.cfg(), "claude", ["--settings={}"]), [])

    def test_attach_keeps_its_command_position(self):
        # In current Claude Code, `claude --settings '{}' attach ID` starts a
        # prompt instead of the attach command.  The proxy flag cannot go after
        # the subcommand either, so do not inject it for this launch shape.
        self.assertEqual(
            cr.claude_launch_args(self.cfg(), "claude", ["attach", "session-id"]), [])

    def test_the_roster_relays_nobodys_status_line(self):
        # T33. `claude agents` applies `--settings` to "the agent view and
        # dispatched sessions": every session started from it would report
        # its model and window into this wrapper's status file as ours, and
        # go on doing so from the daemon, under this pid, after we exit.
        for argv in (["agents"], ["--debug", "agents"]):
            with self.subTest(argv=argv):
                self.assertEqual(cr.claude_launch_args(self.cfg(), "claude", argv), [])

    def test_the_path_is_named_after_the_given_pid(self):
        args = cr.claude_launch_args(self.cfg(), "claude", [], pid=999)
        payload = json.loads(args[1])
        self.assertIn("999.json", payload["statusLine"]["command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
