"""The session budget (T35): pace the 5-hour limit instead of hitting it.

With CR_SESSION_BUDGET set, claude's own statusline says how much of the 5-hour
limit is used. From those samples the wrapper fits a pace and forecasts where
the window stands at its reset. Nearing the target it asks the session to start
no more agents than the pace allows (slow). At the target it freezes the session
the way a context restart does: the handoff is written, nothing is cleared, and
the session is unfolded once the window resets (freeze).

The arithmetic is tested on SessionBudget; the rules on a Controller with a fake
clock; the whole thing once through fake_claude, which runs its statusline on a
typed `statusline <percent>` line.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import helper
import test_controller as tc
from helper import load

cr = load()

RESET = 1000.0          # the window's reset, in the clock the tests run on


def paced(target=97.0, **over):
    """A controller paced at `target`, built the way test_controller builds its
    restart controllers, with no context restart and nobody typing."""
    cfg = dict(session_budget=target, budget_freeze_min=0.0, budget_window_sec=600.0,
               budget_slow_msg=cr.BUDGET_SLOW_MSG_DEFAULT,
               budget_freeze_msg=cr.BUDGET_FREEZE_MSG_DEFAULT,
               budget_clear_msg=cr.BUDGET_CLEAR_MSG_DEFAULT,
               forecast_file="", poll=0, context_tokens=0, context_restart_on=False)
    cfg.update(over)
    ctl = tc.restart_controller(**cfg)
    ctl.last_user_input = -1000.0
    return ctl


def sample(ctl, t, percent, resets=RESET):
    """One statusline report, as the proxy hands it over (`at` is when it was drawn)."""
    ctl.on_status(dict(five_hour_percent=percent, five_hour_resets_at=resets, at=t), t)


def tracker(target=97.0, freeze_min=5.0, window=900.0):
    return cr.SessionBudget(target, window_sec=window, freeze_min=freeze_min)


def pace(percent, rate, minutes_to_reset, target=97.0, freeze_min=5.0, active=None):
    """The forecast of a tracker standing at `percent` at t=600, with a pace of
    `rate` percent a minute over the 600 s before, its window resetting
    `minutes_to_reset` later."""
    b = tracker(target, freeze_min, window=600.0)
    reset = 600 + minutes_to_reset * 60
    b.on_sample(0, percent - 10 * rate, reset)
    b.on_sample(600, percent, reset)
    if active is not None:
        b.active_agents = active
    return b.snapshot(600)


class BudgetCase(unittest.TestCase):
    """Every action the controller returns is recorded: half of what matters here
    is what was NOT typed."""

    def setUp(self):
        self.acts = []

    def step(self, ctl, t):
        """One pass of main(): the usage is read, then the controller ticks."""
        ctl.budget_poll(t)
        action = ctl.tick(t)
        if action:
            self.acts.append(action)
        return action

    def tick_only(self, ctl, t):
        """A tick on its own, for the tests that set the forecast by hand: a read
        would put back the one the samples say."""
        action = ctl.tick(t)
        if action:
            self.acts.append(action)
        return action

    def injected(self):
        return [a[1] for a in self.acts if a[0] == "inject"]

    def notices(self):
        return [a[1] for a in self.acts if a[0] == "notify"]

    def to_slow(self, ctl, t=10):
        sample(ctl, 0, 50.0)
        sample(ctl, t, 80.0)
        return self.step(ctl, t)

    def to_freeze(self, ctl, t=20):
        sample(ctl, t, 97.5)
        return self.step(ctl, t)

    def frozen(self, ctl, t=20):
        """Through the freeze message, the handoff written and accepted, and the
        session held until the reset."""
        self.to_slow(ctl)
        self.to_freeze(ctl, t)
        ctl.on_handoff_echo(tc.MINE, t + 10)
        ctl.handoff.write(ctl, at=t + 10)
        tc.usage(ctl, t + 10, 1000)           # the folding turn ends: end_turn
        self.step(ctl, t + 10)
        self.step(ctl, t + 40)                # quiet for the root idle, so /clear's gate is open
        self.assertEqual(ctl.rstate, cr.FROZEN, ctl.log_lines)
        return ctl


# --------------------------------------------------------------------------- #
# the arithmetic
# --------------------------------------------------------------------------- #
class TestThePaceIsHowFarThePercentMoved(unittest.TestCase):
    """The pace is the move of the reported percent over the last window_sec, read
    off straight lines between samples, 0% at the window's start (T38)."""

    def test_the_move_over_the_horizon_in_percent_a_minute(self):
        b = tracker(window=600.0)
        b.on_sample(0, 50.0, 5000)
        b.on_sample(600, 60.0, 5000)
        self.assertAlmostEqual(b.snapshot(600)["rate"], 1.0)

    def test_between_samples_the_percent_is_a_straight_line(self):
        # 600 s back from t=900 is t=300, halfway from 50 at 0 to 56 at 600.
        b = tracker(window=600.0)
        b.on_sample(0, 50.0, 5000)
        b.on_sample(600, 56.0, 5000)
        b.on_sample(900, 60.0, 5000)
        self.assertAlmostEqual(b.snapshot(900)["rate"], (60.0 - 53.0) / 10)

    def test_before_its_first_sample_the_window_is_read_from_its_start(self):
        # The window began two hours before t=0 at 0%; the wrapper's first sample
        # says 30% at t=0: 0.25 a minute, the window's own average.
        reset = 3 * 3600.0
        b = tracker(window=600.0)
        b.on_sample(0, 30.0, reset)
        self.assertAlmostEqual(b.snapshot(0)["rate"], 0.25)

    def test_after_the_newest_sample_the_percent_stands(self):
        b = tracker(window=600.0)
        b.on_sample(0, 50.0, 5000)
        b.on_sample(60, 51.0, 5000)
        self.assertAlmostEqual(b.snapshot(1000)["rate"], 0.0)

    def test_before_the_window_began_nothing_was_used(self):
        reset = 5 * 3600.0                       # the window began at 0
        b = tracker(window=900.0)
        b.on_sample(30, 1.0, reset)              # a step to 1 half a minute in
        self.assertAlmostEqual(b.snapshot(30)["rate"], 60.0 / 900)

    def test_no_sample_has_no_forecast(self):
        self.assertIsNone(tracker().snapshot(0))

    def test_a_freshly_started_wrapper_reads_no_pace_off_its_first_minute(self):
        # T38, as seen live: a wrapper started three hours into the window at
        # 11%, the whole number moved to 12 a minute later, and a fit through
        # that minute read 0.98% a minute: 0 agents allowed.
        reset = 5 * 3600.0
        b = tracker()
        b.on_sample(3 * 3600.0, 11.0, reset)
        b.on_sample(3 * 3600.0 + 85, 12.0, reset)
        snap = b.snapshot(3 * 3600.0 + 85)
        # 12 against the 10.2 it stood at fifteen minutes back on the line from
        # the window's start: one whole-number step over the horizon at most.
        self.assertLess(snap["rate"], 0.15)
        self.assertEqual(snap["state"], "ok")

    def test_a_pause_does_not_leave_a_few_seconds_to_decide_the_pace(self):
        # T36: 1% of the limit, ten quiet minutes, then the next turn's reports a
        # couple of seconds apart, the whole number stepping by one.
        reset = 5 * 3600.0
        b = tracker()
        for t in range(600, 1201, 30):
            b.on_sample(t, 1.0, reset)
        b.on_sample(1900, 1.0, reset)
        b.on_sample(1902, 2.0, reset)
        snap = b.snapshot(1902)
        self.assertLess(snap["rate"], 0.1)
        self.assertEqual(snap["state"], "ok")

    def test_the_token_estimate_is_no_pace(self):
        # Between two reports the estimate moves the percent shown, not the pace.
        reset = 5 * 3600.0
        b = tracker()
        b.on_sample(0, 0.0, reset)
        b.add_usage(10, 1000)
        b.on_sample(60, 1.0, reset)              # calibrates: 1% per 1000 tokens
        b.add_usage(1400, 500)
        snap = b.snapshot(1400)
        self.assertAlmostEqual(snap["percent"], 1.5)
        self.assertAlmostEqual(snap["rate"], 0.0)           # the reports have not moved

    def test_the_estimate_stops_short_of_the_next_whole_number(self):
        reset = 5 * 3600.0
        b = tracker()
        b.on_sample(0, 0.0, reset)
        b.add_usage(10, 10)
        b.on_sample(20, 1.0, reset)              # 1% for 10 tokens: a step, not a ratio
        b.add_usage(30, 10000)
        self.assertEqual(b.snapshot(30)["percent"], 2.0)

    def test_a_real_burst_is_read_in_full_once_it_spans_the_horizon(self):
        reset = 5 * 3600.0
        b = tracker()
        for t in range(0, 1801, 60):
            b.on_sample(t, 10.0, reset)
        for t in range(1860, 2701, 60):
            b.on_sample(t, 10.0 + (t - 1800) / 60.0, reset)
        self.assertAlmostEqual(b.snapshot(2700)["rate"], 1.0)

    def test_the_forecast_is_where_the_pace_stands_at_the_reset(self):
        snap = pace(91.0, 1.0, 6.5)
        self.assertAlmostEqual(snap["forecast_at_reset"], 97.5)
        self.assertAlmostEqual(snap["minutes_to_target"], 6.0)


class TestTheSamplesOfOneWindow(unittest.TestCase):
    def test_a_sample_of_an_older_window_is_dropped(self):
        b = tracker()
        b.on_sample(0, 50.0, 2000)
        b.on_sample(10, 51.0, 2000)
        self.assertFalse(b.on_sample(20, 40.0, 1000))
        self.assertEqual(b.snapshot(20)["percent"], 51.0)
        self.assertEqual(len(b.points), 2)

    def test_a_newer_window_starts_afresh(self):
        b = tracker()
        b.on_sample(0, 50.0, 2000)
        b.on_sample(10, 51.0, 2000)
        self.assertTrue(b.on_sample(20, 10.0, 3000))
        snap = b.snapshot(20)
        self.assertEqual(snap["percent"], 10.0)
        self.assertEqual(b.points, [(20, 10.0)])

    def test_a_window_already_over_has_no_forecast(self):
        b = tracker()
        b.on_sample(0, 50.0, 1000)
        self.assertIsNone(b.snapshot(1000))

    def test_a_report_without_numbers_is_no_sample(self):
        b = tracker()
        self.assertFalse(b.on_sample(0, None, 1000))
        self.assertFalse(b.on_sample(0, 50.0, "tomorrow"))
        self.assertIsNone(b.snapshot(0))


class TestTokensFillInBetweenSamples(unittest.TestCase):
    def test_the_percent_moves_on_by_the_calibrated_ratio(self):
        b = tracker()
        b.on_sample(0, 50.0, 6000)
        b.add_usage(5, 1000)
        self.assertEqual(b.snapshot(5)["percent"], 50.0)   # no ratio yet: the sample stands
        b.on_sample(10, 52.0, 6000)                         # 2 points for 1000 tokens
        self.assertAlmostEqual(b.ratio, 0.002)
        b.add_usage(20, 500)
        self.assertAlmostEqual(b.snapshot(20)["percent"], 53.0)

    def test_a_sample_that_does_not_move_keeps_the_last_good_ratio(self):
        b = tracker()
        b.on_sample(0, 50.0, 6000)
        b.add_usage(5, 1000)
        b.on_sample(10, 52.0, 6000)
        b.add_usage(15, 500)
        b.on_sample(20, 53.0, 6000)                         # no move: not a usable pair
        self.assertAlmostEqual(b.ratio, 0.002)

    def test_nothing_read_is_nothing_counted(self):
        b = tracker()
        b.on_sample(0, 50.0, 6000)
        b.add_usage(5, 0)
        self.assertEqual(b.weighted, 0.0)
        self.assertEqual(len(b.points), 1)


class TestTheWeightsOfATranscript(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-budget-read-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "session.jsonl")

    def write(self, *rows, partial=None):
        with open(self.path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            if partial:
                fh.write(partial)

    @staticmethod
    def row(mid, usage, model="claude-opus-5"):
        return {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}}

    USAGE = dict(input_tokens=10, cache_creation_input_tokens=200,
                 cache_read_input_tokens=1000, output_tokens=100)

    def test_each_kind_of_token_has_its_weight(self):
        # 5*100 output + 1*10 input + 1.25*200 cache writes + 0.1*1000 cache reads
        self.assertAlmostEqual(cr.weighted_usage(self.USAGE), 860.0)
        self.assertEqual(cr.weighted_usage(None), 0.0)
        self.assertEqual(cr.weighted_usage({"output_tokens": True}), 0.0)

    def test_a_message_written_as_several_rows_counts_once(self):
        self.write(self.row("m1", self.USAGE), self.row("m1", self.USAGE))
        _offset, total = cr.budget_read(self.path, 0, set())
        self.assertAlmostEqual(total, 860.0)

    def test_synthetic_rows_and_other_records_count_nothing(self):
        self.write(self.row("m2", self.USAGE, model="<synthetic>"),
                   {"type": "user", "message": {"id": "u", "content": []}})
        _offset, total = cr.budget_read(self.path, 0, set())
        self.assertEqual(total, 0.0)

    def test_only_what_is_new_is_read_and_a_half_row_waits(self):
        self.write(self.row("m1", self.USAGE))
        offset, total = cr.budget_read(self.path, 0, set())
        self.assertAlmostEqual(total, 860.0)
        self.write(self.row("m3", dict(output_tokens=10)), partial='{"type": "assis')
        offset, total = cr.budget_read(self.path, offset, set())
        self.assertAlmostEqual(total, 50.0)              # the half row is not counted yet
        _offset, total = cr.budget_read(self.path, offset, set())
        self.assertEqual(total, 0.0)

    def test_the_first_read_skips_history_and_a_subagent_counts_from_its_start(self):
        base = os.path.join(self.dir, "session")
        self.write(self.row("old", self.USAGE))
        b = tracker()
        b.read(100, self.path, base + os.sep + "subagents")
        self.assertEqual(b.weighted, 0.0)
        self.write(self.row("new", self.USAGE))
        os.makedirs(base + os.sep + "subagents")
        with open(os.path.join(base, "subagents", "agent-a1.jsonl"), "w") as fh:
            fh.write(json.dumps(self.row("sub1", dict(output_tokens=20))) + "\n")
        b.read(120, self.path, base + os.sep + "subagents")
        self.assertAlmostEqual(b.weighted, 860.0 + 100.0)
        self.assertEqual(b.active_agents, 1)


class TestMaxAgents(unittest.TestCase):
    def test_the_agents_the_pace_allows(self):
        # 5.58 points are left to 97% over 30.5 minutes: 0.183 a minute. Seven
        # active agents share that as 0.183 / 0.42 = 0.44 of their current pace
        # each, and 7 * 0.44 = 3.05, so 3 agents may run.
        snap = pace(91.42, 0.42, 30.5, active=7)
        self.assertAlmostEqual(snap["rate"], 0.42)
        self.assertEqual(snap["max_agents"], 3)
        self.assertEqual(snap["state"], "slow")

    def test_no_active_agent_still_counts_as_one(self):
        self.assertEqual(pace(90.0, 0.1, 20, active=0)["max_agents"], 3)
        self.assertEqual(pace(90.0, 0.1, 20, active=3)["max_agents"], 10)   # 10.5

    def test_the_agents_are_those_that_made_the_pace_not_those_left(self):
        # T37, as seen live: six agents made the pace, the slow message stopped
        # them, and two minutes later none was counted while their pace was still
        # in the fit -- 6, 3, 1, 0 allowed at 9% of the limit.
        b = tracker(window=300.0)
        reset = 3 * 3600.0
        for t in range(0, 601, 30):
            b.on_sample(t, 5.0 + t / 150.0, reset)          # 0.4% a minute
            b.agent_points.append((t, 6))
        b.agent_points.append((630, 0))                     # all stopped
        b.active_agents = 0
        snap = b.snapshot(630)
        self.assertEqual(snap["active_agents"], 0)
        self.assertGreater(b._pacing_agents(630), 5.0)
        self.assertGreaterEqual(snap["max_agents"], 3)

    def test_at_the_target_no_agent_is_allowed(self):
        self.assertEqual(pace(97.0, 1.0, 10)["max_agents"], 0)

    def test_a_pace_that_is_not_rising_limits_nothing(self):
        snap = pace(90.0, -0.5, 10)
        self.assertIsNone(snap["max_agents"])
        self.assertEqual(snap["state"], "ok")


class TestTheStatesAtTheBoundaries(unittest.TestCase):
    def test_at_the_target_it_is_a_freeze(self):
        self.assertEqual(pace(97.0, 1.0, 10)["state"], "freeze")     # exactly the target
        # With no freeze threshold of its own the percent alone decides it.
        self.assertEqual(pace(97.0, 1.0, 10, freeze_min=0.0)["state"], "freeze")

    def test_just_under_the_target_it_is_not(self):
        self.assertNotEqual(pace(95.9, 0.1, 1)["state"], "freeze")

    def test_fewer_minutes_than_the_freeze_threshold_is_a_freeze(self):
        # 6 minutes to the target, a 6.5 minute threshold: freeze. Exactly the
        # threshold is not under it.
        self.assertEqual(pace(91.0, 1.0, 2.0, freeze_min=6.5)["state"], "freeze")
        self.assertEqual(pace(91.0, 1.0, 2.0, freeze_min=6.0)["state"], "ok")

    def test_a_forecast_past_the_target_is_slow(self):
        self.assertEqual(pace(91.0, 1.0, 6.5)["state"], "slow")     # 97.5 at the reset
        self.assertEqual(pace(91.0, 1.0, 6.0)["state"], "ok")       # exactly 97.0 is not past it

    def test_the_freeze_threshold_wins_over_slow(self):
        self.assertEqual(pace(91.0, 1.0, 6.5, freeze_min=6.5)["state"], "freeze")


class TestTheStatuslineProxyKeepsTheLimits(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-budget-proxy-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        saved = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.dir, "claude")
        self.addCleanup(self.restore_config_dir, saved)
        self.status = os.path.join(self.dir, "status", "1.json")

    @staticmethod
    def restore_config_dir(saved):
        if saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved

    def proxy(self, payload):
        cr.run_statusline_proxy(self.status, stdin=io.StringIO(json.dumps(payload)),
                                stdout=io.StringIO())
        with open(self.status) as fh:
            return json.load(fh)

    def test_the_five_hour_and_seven_day_figures_are_recorded(self):
        rec = self.proxy(dict(rate_limits=dict(
            five_hour=dict(used_percentage=91.4, resets_at=1789000000),
            seven_day=dict(used_percentage=40.1, resets_at=1789500000))))
        self.assertEqual(rec["five_hour_percent"], 91.4)
        self.assertEqual(rec["five_hour_resets_at"], 1789000000)
        self.assertEqual(rec["seven_day_percent"], 40.1)

    def test_no_limits_in_the_payload_are_no_figures(self):
        rec = self.proxy(dict(model=dict(id="claude-sonnet-5")))
        self.assertIsNone(rec["five_hour_percent"])
        self.assertIsNone(rec["seven_day_percent"])

    def test_a_malformed_block_is_no_figure_and_no_crash(self):
        rec = self.proxy(dict(rate_limits="91%"))
        self.assertIsNone(rec["five_hour_percent"])
        rec = self.proxy(dict(rate_limits=dict(five_hour="high")))
        self.assertIsNone(rec["five_hour_resets_at"])


class TestTheLaunchAndTheSettings(unittest.TestCase):
    def cfg(self, **over):
        base = dict(context_tokens=0, context_restart_on=False, statusline_proxy=True,
                    status_dir="/tmp/cr-budget-status")
        base.update(over)
        return base

    def setUp(self):
        os.environ["CR_SELF"] = "/opt/bin/agent-retrier.sh"
        self.addCleanup(os.environ.pop, "CR_SELF", None)

    def test_the_proxy_is_wired_in_for_a_budget_alone(self):
        args = cr.claude_launch_args(self.cfg(session_budget=97.0), "claude", [])
        self.assertEqual(args[0], "--settings")

    def test_without_a_budget_or_a_restart_nothing_is_added(self):
        self.assertEqual(cr.claude_launch_args(self.cfg(), "claude", []), [])

    def test_codex_is_never_proxied_for_a_budget(self):
        self.assertEqual(cr.claude_launch_args(self.cfg(session_budget=97.0), "codex", []), [])

    def test_the_target_is_a_percentage_or_off(self):
        self.assertEqual(cr._budget_target("97"), 97.0)
        self.assertEqual(cr._budget_target("97.5"), 97.5)
        self.assertEqual(cr._budget_target("100"), 100.0)
        for text in (None, "", "0", "abc", "-5", "150", "nan", "inf"):
            self.assertIsNone(cr._budget_target(text), text)

    def test_the_settings_are_read_from_the_environment(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        mod = helper.load(CR_SESSION_BUDGET="95", CR_BUDGET_FREEZE_MIN="0",
                          CR_BUDGET_WINDOW_SEC="300", CR_FORECAST_FILE="/x/f.json")
        self.assertEqual(mod.CFG["session_budget"], 95.0)
        self.assertEqual(mod.CFG["budget_freeze_min"], 0.0)
        self.assertEqual(mod.CFG["budget_window_sec"], 300.0)
        self.assertEqual(mod.CFG["forecast_file"], "/x/f.json")

    def test_off_by_default(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        mod = helper.load()
        self.assertIsNone(mod.CFG["session_budget"])
        self.assertEqual(mod.CFG["forecast_file"], "")
        self.assertEqual(mod.CFG["budget_freeze_min"], 5.0)


class TestTheForecastFileSchema(unittest.TestCase):
    def fc(self):
        return dict(state="slow", percent=91.4, rate=0.42, forecast_at_reset=104.2,
                    minutes_to_target=13.3, minutes_to_reset=30.5, max_agents=3,
                    active_agents=7, resets_at=1789000000, target=97.0, seven_day_percent=40.1)

    def test_every_key_of_schema_one_is_there_and_nothing_else(self):
        rec = cr.forecast_record(self.fc(), 1789000000 - 1830, "claude", "sess-1")
        self.assertEqual(set(rec), set(cr.FORECAST_KEYS))
        self.assertEqual(rec["v"], 1)
        self.assertEqual(rec["session"], "sess-1")
        self.assertEqual(rec["updated_at"], 1789000000 - 1830)
        self.assertEqual(rec["rate_per_min"], 0.42)

    def test_it_is_written_whole_and_leaves_no_temporary_file(self):
        d = tempfile.mkdtemp(prefix="cr-budget-file-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "sub", "forecast.json")
        self.assertTrue(cr.write_forecast(path, cr.forecast_record(self.fc(), 5, "claude", "s")))
        with open(path) as fh:
            self.assertEqual(json.load(fh)["state"], "slow")
        self.assertEqual(os.listdir(os.path.dirname(path)), ["forecast.json"])

    def test_the_rename_is_what_puts_it_in_place(self):
        # Nothing is at the name until the whole file is: the rename is the one step
        # a reader can see, and it is taken from a finished temporary file.
        d = tempfile.mkdtemp(prefix="cr-budget-file-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "forecast.json")
        real_rename, seen = os.rename, []

        def rename(src, dst):
            seen.append((os.path.exists(dst), os.path.exists(src), src != dst))
            real_rename(src, dst)

        with mock.patch("os.rename", rename):
            self.assertTrue(cr.write_forecast(path, cr.forecast_record(self.fc(), 5, "claude", "s")))
        self.assertEqual(seen, [(False, True, True)])

    def test_a_path_that_cannot_be_written_is_reported_not_raised(self):
        d = tempfile.mkdtemp(prefix="cr-budget-file-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        blocker = os.path.join(d, "a-file")
        with open(blocker, "w") as fh:
            fh.write("x")
        self.assertFalse(cr.write_forecast(os.path.join(blocker, "f.json"), {}))


# --------------------------------------------------------------------------- #
# the rules, on a controller with a fake clock
# --------------------------------------------------------------------------- #
class TestTheSlowMessage(BudgetCase):
    def test_it_is_typed_once_with_the_placeholders_filled(self):
        ctl = paced()
        self.to_slow(ctl, 10)
        self.assertEqual(len(self.injected()), 1)
        text = self.injected()[0]
        self.assertIn("Session budget: 80.0% of the 5-hour limit used", text)
        self.assertIn("target 97%", text)
        self.assertIn("at most 0 agents", text)
        self.assertNotIn("{", text)
        self.step(ctl, 11)
        self.step(ctl, 12)
        self.assertEqual(len(self.injected()), 1)

    def test_the_transition_is_logged(self):
        ctl = paced()
        self.to_slow(ctl)
        self.assertTrue(any(line.startswith("budget: ok→slow 80.0% 3.17%/min")
                            for line in ctl.log_lines), ctl.log_lines)

    def test_again_when_the_agents_it_allows_have_halved(self):
        ctl = paced()
        self.to_slow(ctl)
        ctl._budget_slow_sent = 4
        ctl.budget_forecast = dict(ctl.budget_forecast, max_agents=3)
        self.assertIsNone(self.tick_only(ctl, 11))
        ctl.budget_forecast = dict(ctl.budget_forecast, max_agents=2)
        self.tick_only(ctl, 12)
        self.assertEqual(len(self.injected()), 2)

    def test_again_after_the_pace_was_cleared(self):
        ctl = paced()
        self.to_slow(ctl)
        ctl.budget_forecast = dict(ctl.budget_forecast, state="ok", forecast_at_reset=50.0)
        self.tick_only(ctl, 11)
        ctl.budget_forecast = dict(ctl.budget_forecast, state="slow", max_agents=0)
        self.tick_only(ctl, 12)
        self.assertEqual(len(self.injected()), 3)          # slow, clear, slow

    def test_the_clear_message_lifts_a_slow_one_once_the_forecast_is_well_under(self):
        # T37: told to wait "until the pace drops", the session cannot see it drop.
        ctl = paced(budget_clear_msg="CLEAR {percent}% forecast {forecast}%")
        self.to_slow(ctl)
        ctl.budget_forecast = dict(ctl.budget_forecast, state="ok", forecast_at_reset=95.0)
        self.tick_only(ctl, 11)                             # under, but not by the margin
        self.assertEqual(len(self.injected()), 1)
        ctl.budget_forecast = dict(ctl.budget_forecast, state="ok", forecast_at_reset=40.0)
        self.tick_only(ctl, 12)
        self.tick_only(ctl, 13)
        self.assertEqual(len(self.injected()), 2)
        self.assertTrue(self.injected()[1].startswith("CLEAR "), self.injected())
        self.assertIn("forecast 40%", self.injected()[1])
        self.assertIn("budget: clear message typed (forecast 40%)", "\n".join(ctl.log_lines))

    def test_no_clear_without_a_slow_message_before_it(self):
        ctl = paced()
        sample(ctl, 0, 10.0)
        sample(ctl, 60, 10.1)
        self.step(ctl, 60)
        self.assertEqual(self.injected(), [])

    def test_nothing_is_typed_for_a_roster_or_a_limit_in_flight(self):
        ctl = paced()
        ctl.hands_off = "this is the agent roster, not a session of ours"
        self.assertIsNone(self.to_slow(ctl))
        self.assertEqual(self.injected(), [])
        ctl = paced()
        ctl.on_limit(tc.BANNER, 0, "transcript")          # a wait of two hours
        sample(ctl, 0, 50.0)
        sample(ctl, 10, 80.0)
        self.assertIsNone(self.step(ctl, 10))
        self.assertEqual(self.injected(), [])

    def test_codex_is_never_paced(self):
        ctl = tc.restart_controller(agent="codex", session_budget=97.0, context_tokens=0,
                                    context_restart_on=False)
        self.assertIsNone(ctl.budget)


class TestTheDraftIsNotTypedOver(BudgetCase):
    def test_a_draft_holds_the_message_and_the_message_follows_it(self):
        ctl = paced()
        sample(ctl, 0, 50.0)
        sample(ctl, 10, 80.0)
        ctl.pending_input_chars = 3
        ctl.last_draft_change = 10
        self.assertIsNone(self.step(ctl, 11))
        self.assertIsNone(self.step(ctl, 20))
        self.assertEqual(self.injected(), [])
        ctl.pending_input_chars = 0           # the draft was sent or cleared
        self.step(ctl, 31)
        self.assertEqual(len(self.injected()), 1)
        self.assertTrue(any("unsent text in the prompt box" in line for line in ctl.log_lines))


class TestTheFreezeIsTheRestartsOwn(BudgetCase):
    def test_the_freeze_message_comes_first_and_then_the_handoff_phrase(self):
        ctl = paced()
        self.to_slow(ctl)
        self.to_freeze(ctl)
        (text,) = self.injected()[1:]
        self.assertTrue(text.startswith("Session budget: the 97% target will be reached"), text)
        self.assertIn("Start no new agents and stop the running ones at a safe point. "
                      "Fold into `/proj/H.md`", text)
        self.assertIn("`/proj/H.md`", text)
        self.assertIn(ctl.nonce, text)
        self.assertEqual(ctl.handoff_text, text)

    def test_the_session_is_held_until_the_reset_and_nothing_is_cleared(self):
        ctl = self.frozen(paced())
        self.assertIsNone(self.step(ctl, 500))
        self.assertIsNone(self.step(ctl, RESET - 1))
        self.assertNotIn("/clear", self.injected())
        self.assertEqual(len(self.injected()), 2)

    def test_the_unfold_is_typed_once_at_the_reset_and_its_echo_ends_the_freeze(self):
        ctl = self.frozen(paced())
        self.step(ctl, RESET)
        self.assertEqual(self.injected()[-1], ctl.resume_text)
        ctl.on_resume_echo(RESET + 1)
        self.assertIsNone(self.step(ctl, RESET + 2))
        self.assertIsNone(ctl.rstate)
        self.assertEqual(self.injected().count(ctl.resume_text), 1)
        self.assertEqual(len(self.injected()), 3)
        self.assertFalse(ctl._budget_freeze)
        self.assertFalse(ctl.context_off)             # the unfold is not a failed restart
        self.assertEqual(ctl.cooldown_until, 0.0)     # a freeze is no restart: no cooldown

    def test_the_unfold_waits_the_margin_a_usage_limit_waits_past_the_reset(self):
        ctl = self.frozen(paced(margin=45))
        self.step(ctl, RESET)
        self.assertNotIn(ctl.resume_text, self.injected())
        self.step(ctl, RESET + 44.9)
        self.assertNotIn(ctl.resume_text, self.injected())
        self.step(ctl, RESET + 45)
        self.assertEqual(self.injected()[-1], ctl.resume_text)

    def test_an_aborted_freeze_types_its_own_cancel_phrase(self):
        # The session has seen the freeze phrase, so an abort must call it off, and
        # in the budget's words: a freeze is no context restart (T35).
        ctl = paced()
        self.to_slow(ctl)
        self.to_freeze(ctl)                    # the freeze phrase goes out at t=20
        ctl.on_handoff_echo(tc.MINE, 21)       # ...and the session has seen it
        for t in (500, 920, 921):              # 900 s for the handoff, then the abort
            self.step(ctl, t)
        self.assertEqual(self.injected().count(ctl.budget_cancel_text), 1, self.injected())
        self.assertEqual(ctl.budget_cancel_text, cr.BUDGET_CANCEL_MSG_DEFAULT)
        self.assertNotIn(ctl.cancel_text, self.injected())
        self.assertEqual(ctl.inject_note(),
                         "session budget: freeze cancelled; asking the session to carry on")
        self.step(ctl, 922)                    # the cancel is closed out one tick later
        self.assertIsNone(ctl.rstate)

    def test_a_handoff_that_never_lands_aborts_the_freeze_and_says_so(self):
        ctl = paced()
        self.to_slow(ctl)
        self.to_freeze(ctl)
        for t in (21, 81, 141, 201):
            self.step(ctl, t)
        self.assertIn("budget freeze aborted: the handoff phrase never reached the session",
                      self.notices())
        self.assertIsNone(ctl.rstate)
        self.assertFalse(ctl.context_off)
        self.assertEqual(ctl.cooldown_until, 0.0)
        resent = [t for t in self.injected() if t.startswith("Session budget: the 97%")]
        self.assertEqual(len(resent), 3)               # the freeze phrase again, twice
        self.step(ctl, 202)
        self.assertEqual(len([t for t in self.injected()
                              if t.startswith("Session budget: the 97%")]), 3)

    def test_a_limit_that_lifts_before_the_reset_holds_the_unfold(self):
        ctl = self.frozen(paced())
        ctl.on_limit("You've hit your session limit · resets in 2 minutes", 500, "transcript")
        self.step(ctl, 620)
        self.assertIsNone(self.step(ctl, 700))
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertEqual(self.injected()[2:], [])
        self.step(ctl, RESET)
        self.assertEqual(self.injected()[-1], ctl.resume_text)
        self.assertNotIn("continue", self.injected())

    def test_a_limit_that_lifts_after_the_reset_unfolds_with_the_resume_not_continue(self):
        ctl = self.frozen(paced())
        ctl.on_limit("You've hit your session limit · resets in 30 minutes", 500, "transcript")
        self.step(ctl, 2300)
        self.assertEqual(self.injected()[-1], ctl.resume_text)
        self.assertNotIn("continue", self.injected())
        self.assertEqual(self.injected().count(ctl.resume_text), 1)


class TestAnAbortedRestartSaysWhichItWas(BudgetCase):
    def test_a_context_restart_that_aborts_still_types_the_restart_cancel(self):
        ctl = tc.restart_controller(budget_cancel_msg="BUDGET CANCEL")
        tc.usage(ctl, 0, tc.FULL)
        action = self.step(ctl, 30)
        self.assertEqual(action[0], "inject")  # the fold phrase
        ctl.on_handoff_echo(tc.MINE, 30)       # the session has seen it
        for t in (60, 500, 929, 930, 931):     # 900 s for the handoff, then the abort
            self.step(ctl, t)
        self.assertIn(ctl.cancel_text, self.injected())
        self.assertNotIn("BUDGET CANCEL", self.injected())


class TestTheOverridesReplaceTheDefaults(BudgetCase):
    def test_the_bash_defaults_say_what_the_python_defaults_say(self):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "agent-retrier.sh")
        out = subprocess.run(
            ["bash", "-c",
             'set -u; eval "$(sed -n "/^CR_BUDGET_[A-Z_]*_DEFAULT=/p" "$1")"; '
             'printf "%s\\0%s\\0%s\\0%s" "$CR_BUDGET_SLOW_MSG_DEFAULT" '
             '"$CR_BUDGET_FREEZE_MSG_DEFAULT" "$CR_BUDGET_CANCEL_MSG_DEFAULT" '
             '"$CR_BUDGET_CLEAR_MSG_DEFAULT"',
             "_", script],
            capture_output=True, text=True, check=True).stdout
        slow, freeze, cancel, clear = out.split("\0")
        self.assertEqual(clear, cr.BUDGET_CLEAR_MSG_DEFAULT)
        self.assertEqual(slow, cr.BUDGET_SLOW_MSG_DEFAULT)
        self.assertEqual(freeze, cr.BUDGET_FREEZE_MSG_DEFAULT)
        self.assertEqual(cancel, cr.BUDGET_CANCEL_MSG_DEFAULT)

    def test_the_cancel_wording_is_read_from_the_environment(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        mod = helper.load(CR_BUDGET_CANCEL_MSG="Budget freeze off.")
        self.assertEqual(mod.CFG["budget_cancel_msg"], "Budget freeze off.")
        self.assertEqual(helper.load().CFG["budget_cancel_msg"], helper.load().BUDGET_CANCEL_MSG_DEFAULT)

    def test_the_environment_wording_is_what_gets_typed(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        mod = helper.load(CR_SESSION_BUDGET="97", CR_BUDGET_FREEZE_MIN="0",
                          CR_BUDGET_SLOW_MSG="SLOW {percent}% with {agents} at most",
                          CR_BUDGET_FREEZE_MSG="FREEZE at {target}%.")
        ctl = mod.Controller(dict(mod.CFG, agent="claude", context_tokens=0,
                                  handoff_msg="Fold into `{file}` and end it with {marker}."),
                             lambda *_: None, now=0)
        ctl.last_user_input = -1000.0
        sample(ctl, 0, 50.0)
        sample(ctl, 10, 80.0)
        ctl.budget_poll(10)
        self.assertEqual(ctl.tick(10), ("inject", "SLOW 80.0% with 0 at most", False))
        sample(ctl, 20, 97.5)
        ctl.budget_poll(20)
        action = ctl.tick(20)
        self.assertEqual(action[0], "inject")
        self.assertTrue(action[1].startswith("FREEZE at 97%. Fold into `"), action[1])
        self.assertIn(ctl.nonce, action[1])

    def test_the_default_wording_has_no_placeholder_left(self):
        ctl = paced()
        self.to_slow(ctl)
        self.to_freeze(ctl)
        self.assertEqual(len(self.injected()), 2)
        for text in self.injected():
            self.assertNotIn("{percent}", text)
            self.assertNotIn("{minutes}", text)
            self.assertNotIn("{file}", text)


class TestTheNotesOnTheStatusLine(BudgetCase):
    def test_the_slow_message_is_named_once(self):
        ctl = paced()
        self.to_slow(ctl)
        self.assertEqual(ctl.inject_note(),
                         "session budget: the pace is over the target; asking for fewer agents")
        self.assertNotIn("session budget", ctl.inject_note())

    def test_the_freeze_is_named_while_its_handoff_goes_out(self):
        ctl = paced()
        self.to_slow(ctl)
        self.to_freeze(ctl)
        self.assertEqual(ctl.inject_note(), "session budget: freezing; asking for a handoff")
        self.assertEqual(cr.RESTART_LABELS[cr.FROZEN], "frozen to reset")


class TestNothingIsWrittenWithoutTheBudget(BudgetCase):
    def test_a_session_without_the_budget_gets_no_budget_at_all(self):
        ctl = tc.restart_controller(context_tokens=0, context_restart_on=False)
        self.assertIsNone(ctl.budget)
        sample(ctl, 0, 99.0)
        sample(ctl, 10, 99.5)
        self.assertIsNone(self.step(ctl, 10))
        self.assertIsNone(ctl.budget_forecast)
        self.assertEqual([l for l in ctl.log_lines if "budget" in l], [])

    def test_the_forecast_file_is_not_written_without_its_name(self):
        # The budget is on and forecasts are computed; only the name is missing. So
        # nothing may be written at all, anywhere: not through write_forecast, not
        # by opening a file for writing, not by a rename or a replace.
        work = tempfile.mkdtemp(prefix="cr-budget-cwd-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        here = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, here)
        ctl = paced(forecast_file="")
        real_open, writes = open, []

        def guarded_open(path, mode="r", *args, **kwargs):
            if any(c in mode for c in "wax+"):
                writes.append(path)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", guarded_open), \
                mock.patch.object(tc.cr, "write_forecast") as write, \
                mock.patch("os.rename") as rename, \
                mock.patch("os.replace") as replace:
            self.to_slow(ctl)
            self.to_freeze(ctl)
        self.assertIsNotNone(ctl.budget_forecast)     # the forecasts were computed
        write.assert_not_called()
        rename.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(writes, [])
        self.assertEqual(os.listdir(work), [])

    def test_the_forecast_file_is_written_when_it_is_named(self):
        work = tempfile.mkdtemp(prefix="cr-budget-cwd-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        path = os.path.join(work, "forecast.json")
        ctl = paced(forecast_file=path)
        self.to_slow(ctl)
        with open(path) as fh:
            rec = json.load(fh)
        self.assertEqual(set(rec), set(cr.FORECAST_KEYS))
        self.assertEqual(rec["state"], "slow")
        self.assertEqual(rec["target"], 97.0)
        self.assertEqual(rec["session"], ctl.session_id)


# --------------------------------------------------------------------------- #
# the whole of it, through fake_claude on a pty
# --------------------------------------------------------------------------- #
def transcript_texts(session):
    """What the user typed into the session, as its transcript holds it."""
    cfg_dir = os.path.join(os.path.dirname(session.log), "claude-config")
    texts = []
    for root, _dirs, files in os.walk(os.path.join(cfg_dir, "projects")):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(root, name)) as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") == "user":
                        texts.append(rec["message"]["content"][0].get("text", ""))
    return texts


def last_line(path):
    """The last line of a file, or "" while there is no such file."""
    try:
        with open(path) as fh:
            lines = fh.read().rstrip().split("\n")
    except OSError:
        return ""
    return lines[-1]


def until(session, probe, timeout=20):
    """Waits for `probe`, draining the pty meanwhile: the wrapper blocks on a full
    one, and everything it is waiting on with it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = probe()
        if value:
            return value
        session._drain_once()
    return probe()


class TestTheBudgetInAFakeSession(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="cr-budget-session-")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def session(self, **env):
        # A one-second horizon: the 50 → 80 the test types a moment apart is the
        # pace, as fifteen minutes of it would be in a real session.
        e = helper.restart_timers(CR_SESSION_BUDGET="97", CR_BUDGET_FREEZE_MIN="0",
                                  CR_MARGIN_SEC="0", FAKE_STATUS_RESETS_IN="12",
                                  CR_BUDGET_WINDOW_SEC="1")
        e.update(env)
        s = helper.one_wrapper(self.work, e)
        self.addCleanup(s.close)
        return s

    def test_slow_then_freeze_then_one_unfold_at_the_reset(self):
        forecast = os.path.join(self.work, "forecast.json")
        s = self.session(CR_FORECAST_FILE=forecast)
        self.assertTrue(s.read_until("ready"))
        s.send("statusline 50\r")
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        time.sleep(0.4)
        s.send("statusline 80\r")
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        slow = until(s, lambda: [t for t in transcript_texts(s)
                                 if t.startswith("Session budget: 80.0%")])
        self.assertTrue(slow, s.buf[-800:])

        s.send("statusline 97.5\r")
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        frozen = until(s, lambda: [t for t in transcript_texts(s)
                                   if t.startswith("Session budget: the 97% target")])
        self.assertTrue(frozen, s.buf[-800:])
        self.assertIn("Wrap up now.", frozen[0])
        handoff = re.search(r"Write a complete handoff to `([^`]+)`", frozen[0]).group(1)
        self.assertTrue(until(s, lambda: last_line(handoff).startswith("HANDOFF-")),
                        s.buf[-800:])

        resume = "Read `%s` and continue from it." % handoff
        landed = until(s, lambda: [t for t in transcript_texts(s) if t == resume])
        self.assertTrue(landed, s.buf[-800:])
        accepted = until(s, lambda: "budget: the resume phrase was accepted" in "".join(s.log_lines()))
        self.assertTrue(accepted, "".join(s.log_lines())[-800:])
        texts = transcript_texts(s)
        self.assertEqual(len([t for t in texts if t.startswith("Session budget: 80.0%")]), 1)
        self.assertEqual(len([t for t in texts if t.startswith("Session budget: the 97%")]), 1)
        self.assertEqual(texts.count(resume), 1)
        self.assertNotIn("/clear", texts)
        log = "".join(s.log_lines())
        self.assertIn("budget: ok→slow", log)
        self.assertIn("budget: freezing at 97.5%", log)
        self.assertIn("budget: handoff written; holding the session until the reset", log)
        self.assertIn("budget: the resume phrase was accepted", log)

        with open(forecast) as fh:
            rec = json.load(fh)
        self.assertEqual(set(rec), set(cr.FORECAST_KEYS))
        self.assertEqual(rec["v"], 1)
        self.assertEqual(rec["target"], 97.0)
        self.assertEqual([f for f in os.listdir(self.work) if f.endswith(".tmp")], [])

    def test_unset_nothing_is_paced_and_no_file_is_written(self):
        forecast = os.path.join(self.work, "forecast.json")
        e = helper.restart_timers(CR_FORECAST_FILE=forecast, FAKE_STATUS_RESETS_IN="12")
        s = helper.one_wrapper(self.work, e)
        self.addCleanup(s.close)
        self.assertTrue(s.read_until("ready"))
        s.send("statusline 50\r")
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        time.sleep(0.4)
        s.send("statusline 97.5\r")
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        self.assertFalse(os.path.exists(forecast))
        self.assertEqual([t for t in transcript_texts(s) if t.startswith("Session budget")], [])
        self.assertEqual([line for line in s.log_lines() if "] budget" in line
                          or "session budget" in line], [])


    def test_codex_is_not_paced_and_the_log_says_so(self):
        e = helper.restart_timers(CR_SESSION_BUDGET="97")
        e["agent"] = "codex"
        s = helper.one_wrapper(self.work, e)
        self.addCleanup(s.close)
        self.assertTrue(s.read_until("ready"))
        self.assertTrue(until(s, lambda: "claude-only" in "".join(s.log_lines())),
                        "".join(s.log_lines())[-600:])
        self.assertNotIn("session budget armed", "".join(s.log_lines()))

    def test_a_budget_that_is_no_percentage_is_said_so_and_is_off(self):
        forecast = os.path.join(self.work, "forecast.json")
        s = self.session(CR_SESSION_BUDGET="150", CR_FORECAST_FILE=forecast)
        self.assertTrue(s.read_until("ready"))
        self.assertTrue(until(s, lambda: "is not a percentage" in "".join(s.log_lines())),
                        "".join(s.log_lines())[-600:])
        s.send("statusline 97.5\r")
        self.assertTrue(s.roundtrip(), s.buf[-400:])
        self.assertFalse(os.path.exists(forecast))
        self.assertEqual([t for t in transcript_texts(s) if t.startswith("Session budget")], [])


if __name__ == "__main__":
    unittest.main()
