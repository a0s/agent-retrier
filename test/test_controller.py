"""The decision layer: when it is safe to type into someone else's session.

Every guard here exists because the tmux design got it wrong at least once:
 - #7/#19  the retry landed in the input box but was never submitted
 - #19     a bare Enter confirmed "Upgrade your plan" in the limit menu
 - #21     a cleared banner at reset time was misread as "the user resumed"
 - #39     a second limit arriving during a wait was never noticed
and the "don't type over a half-written prompt" case, which the tmux design
listed as unsolvable by scraping (DESIGN-NOTES §6) but is trivial here: we are
the terminal, so we see the keystrokes.
"""
import json
import os
import shutil
import signal
import tempfile
import time
import unittest

from helper import load

cr = load()

CFG = dict(message="continue", margin=0, max_attempts=3, fallback_wait=18000,
           max_wait=691200, user_idle=20, busy_idle=6, verify=60, wait_scale=1,
           draft_grace=600, resume=15, typing_max=900)

BANNER = "You've hit your session limit · resets in 2 hours"


def controller(**over):
    cfg = dict(CFG)
    cfg.update(over)
    logs = []
    ctl = cr.Controller(cfg, logs.append, now=0)
    ctl.log_lines = logs
    return ctl


class TestScheduling(unittest.TestCase):
    def test_a_limit_schedules_a_wake_up_at_the_reset(self):
        ctl = controller()
        self.assertTrue(ctl.on_limit(BANNER, now=1000, source="transcript"))
        self.assertEqual(ctl.state, cr.WAITING)
        self.assertAlmostEqual(ctl.wake_at, 1000 + 7200, delta=1)

    def test_margin_is_added(self):
        ctl = controller(margin=45)
        ctl.on_limit(BANNER, now=0, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 7245, delta=1)

    def test_unparseable_banner_uses_the_fallback_wait(self):
        ctl = controller()
        ctl.on_limit("You've hit your session limit", now=0, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 18000, delta=1)

    def test_wait_is_capped(self):
        ctl = controller(max_wait=100)
        ctl.on_limit("resets in 40 days", now=0, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 100, delta=1)

    def test_the_same_banner_does_not_reschedule(self):
        # The banner is repainted continuously and the transcript may report it
        # too; only the first sighting sets the timer.
        ctl = controller()
        self.assertTrue(ctl.on_limit(BANNER, now=0, source="screen"))
        self.assertFalse(ctl.on_limit(BANNER, now=5, source="transcript"))
        self.assertFalse(ctl.on_limit("  YOU'VE HIT YOUR SESSION LIMIT · resets in 2 hours ",
                                      now=6, source="screen"))
        self.assertAlmostEqual(ctl.wake_at, 7200, delta=1)

    def test_a_new_limit_during_a_wait_supersedes_the_old_one(self):
        # Upstream #39: a monitor parked on a stale timer never re-evaluated, so
        # a second, genuine limit was ignored for hours.
        ctl = controller()
        ctl.on_limit("resets in 10 hours", now=0, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 36000, delta=1)
        ctl.on_limit("resets in 1 hours", now=100, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 3700, delta=1)


class TestAScrapedWaitCanBeTalkedOutOfIt(unittest.TestCase):
    """A wait the screen scheduled stays open to the screen.

    A scraped banner is the one kind that can belong to somebody else — a card on
    the agent roster, a line of history behind one — and until now the wait it
    scheduled was unfixable: the screen channel closes the moment the controller
    leaves IDLE, and a roster has no transcript of its own to correct it from.
    A neighbour's "resets 2:10am" parked a terminal for 11h18m over a limit that
    lifted in seven minutes, and killing the process was the only way out.
    """

    def test_the_screen_may_still_be_read_during_a_scraped_wait(self):
        ctl = controller()
        ctl.on_limit("resets in 10 hours", now=0, source="screen")
        self.assertTrue(ctl.rescrapable())

    def test_a_transcript_wait_is_not_second_guessed(self):
        # The transcript reports this session's own limit; there is nothing on
        # screen worth weighing against it.
        ctl = controller()
        ctl.on_limit("resets in 10 hours", now=0, source="transcript")
        self.assertFalse(ctl.rescrapable())

    def test_a_stall_is_not_re_read_that_way(self):
        ctl = controller(stall_wait=60, stall_backoff=2, stall_max_wait=600,
                         stall_max_attempts=5)
        ctl.on_stall("Selected model is at capacity", now=0, source="screen")
        self.assertEqual(ctl.state, cr.WAITING)
        self.assertFalse(ctl.rescrapable())

    def test_an_earlier_reset_on_screen_shortens_the_wait(self):
        ctl = controller()
        ctl.on_limit("resets in 10 hours", now=0, source="screen")
        self.assertTrue(ctl.on_limit("resets in 1 hours", now=100, source="screen"))
        self.assertAlmostEqual(ctl.wake_at, 3700, delta=1)

    def test_a_later_one_cannot_extend_it(self):
        # The stale-banner direction: another card, read a moment later, must
        # not push the wake-up further out — that is how eleven hours happened.
        ctl = controller()
        ctl.on_limit("resets in 1 hours", now=0, source="screen")
        self.assertFalse(ctl.on_limit("resets in 10 hours", now=100, source="screen"))
        self.assertAlmostEqual(ctl.wake_at, 3600, delta=1)

    def test_a_banner_turned_down_once_is_not_re_read_forever(self):
        # The screen keeps the card on it, so the same text arrives every few
        # seconds for as long as the wait runs.
        ctl = controller()
        ctl.on_limit("resets in 1 hours", now=0, source="screen")
        ctl.on_limit("resets in 10 hours", now=100, source="screen")
        before = len(ctl.log_lines)
        for t in range(103, 130, 3):
            self.assertFalse(ctl.on_limit("resets in 10 hours", now=t, source="screen"))
        self.assertEqual(len(ctl.log_lines), before)

    def test_the_transcript_still_outranks_the_screen(self):
        # Whatever the screen said, a limit this session actually hit replaces it.
        ctl = controller()
        ctl.on_limit("resets in 1 hours", now=0, source="screen")
        self.assertTrue(ctl.on_limit("resets in 10 hours", now=100, source="transcript"))
        self.assertAlmostEqual(ctl.wake_at, 36100, delta=1)
        self.assertFalse(ctl.rescrapable())

    def test_the_slate_is_clean_after_the_wait_ends(self):
        ctl = controller()
        ctl.on_limit("resets in 1 hours", now=0, source="screen")
        ctl.on_limit("resets in 10 hours", now=10, source="screen")   # turned down
        ctl.on_output("esc to interrupt", 100)
        ctl.on_output("esc to interrupt", 120)
        self.assertTrue(ctl.on_alive(200, "output"))
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertFalse(ctl.rescrapable())
        self.assertTrue(ctl.on_limit("resets in 10 hours", now=300, source="screen"))


class TestInjection(unittest.TestCase):
    def test_nothing_happens_before_the_reset(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        self.assertIsNone(ctl.tick(7100))

    def test_the_retry_fires_at_the_reset(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        action = ctl.tick(7201)
        self.assertEqual(action, ("inject", "continue", False))
        self.assertEqual(ctl.state, cr.VERIFY)

    def test_the_menu_is_dismissed_first(self):
        # Upstream #19: Enter into /rate-limit-options confirmed "Upgrade your plan".
        ctl = controller()
        ctl.on_output("What do you want to do?\n 2. Stop and wait for limit to reset", now=0)
        ctl.on_limit(BANNER, now=0, source="screen")
        action = ctl.tick(7201)
        self.assertEqual(action, ("inject", "continue", True))

    def test_a_cleared_banner_still_gets_a_retry(self):
        # Upstream #21: Claude clears the banner exactly when the limit lifts, and
        # the monitor read that as "the user must have continued" — then sent
        # nothing, leaving the session idle. Nothing here consults the banner at
        # wake-up time; only "is the session busy" gates the send.
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        self.assertIsNotNone(ctl.tick(7201))


class TestSafetyGates(unittest.TestCase):
    def setUp(self):
        self.ctl = controller()
        self.ctl.on_limit(BANNER, now=0, source="transcript")

    def test_not_while_claude_is_working(self):
        self.ctl.on_output("✻ Cogitating… (esc to interrupt)", now=7200)
        self.assertIsNone(self.ctl.tick(7201))
        self.assertEqual(self.ctl.state, cr.WAITING)

    def test_retries_once_the_session_goes_quiet(self):
        self.ctl.on_output("✻ Cogitating… (esc to interrupt)", now=7200)
        self.ctl.tick(7201)
        self.assertIsNotNone(self.ctl.tick(7300))

    def test_not_while_the_user_is_typing(self):
        self.ctl.on_user_bytes(b"hel", now=7195)
        self.assertIsNone(self.ctl.tick(7201))

    def test_not_over_unsent_text_in_the_prompt_box(self):
        # DESIGN-NOTES §6 calls this unsolvable by scraping. Owning the pty makes
        # it a byte count: characters typed since the last Enter.
        self.ctl.on_user_bytes(b"half a thought", now=7100)
        self.assertIsNone(self.ctl.tick(7201))
        self.ctl.on_user_bytes(b"\r", now=7202)          # user submits it
        self.assertIsNotNone(self.ctl.tick(7300))

    def test_backspacing_the_draft_away_clears_the_gate(self):
        self.ctl.on_user_bytes(b"ab", now=0)
        self.ctl.on_user_bytes(b"\x7f\x7f", now=1)
        self.assertIsNotNone(self.ctl.tick(7201))

    def test_ctrl_u_clears_the_gate(self):
        self.ctl.on_user_bytes(b"draft", now=0)
        self.ctl.on_user_bytes(b"\x15", now=1)
        self.assertIsNotNone(self.ctl.tick(7201))

    def test_arrow_keys_do_not_count_as_a_draft(self):
        self.ctl.on_user_bytes(b"\x1b[A", now=0)         # up arrow: an escape seq
        self.assertIsNotNone(self.ctl.tick(7201))


class TestTheLimitCanEndWithoutTheReset(unittest.TestCase):
    """Field failure: a weekly limit was detected, the human logged into another
    account and worked for an hour — and the wrapper sat there counting down its
    48 hours, ready to type "continue" into the middle of that work.

    Nothing announces "your quota is back": not the transcript, not the render.
    The evidence is that claude is answering, which is exactly what the wait was
    waiting to find out.
    """
    def waiting(self, **over):
        ctl = controller(**over)
        ctl.on_limit("You've hit your weekly limit · resets in 48 hours", now=1000,
                     source="transcript")
        return ctl

    def working(self, ctl, start, seconds, step=2):
        for t in range(int(start), int(start + seconds) + 1, step):
            ctl.on_output("✻ Cogitating… (esc to interrupt)", now=t)
        return start + seconds

    def test_a_long_working_spell_cancels_the_wait(self):
        ctl = self.waiting()
        end = self.working(ctl, 1100, 20)
        self.assertEqual(ctl.tick(end), ("resume",))
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertEqual(ctl.wake_at, 0.0)

    def test_a_flicker_of_footer_does_not(self):
        # A refused turn still paints a spinner for a second or two before the
        # banner lands. Acting on that would drop every wait the moment the human
        # tried again.
        ctl = self.waiting()
        end = self.working(ctl, 1100, 4)
        self.assertIsNone(ctl.tick(end))
        self.assertEqual(ctl.state, cr.WAITING)

    def test_a_spell_that_began_before_the_limit_does_not_count(self):
        # The footer was already running when the limit arrived — that spell is
        # the turn that got refused, not proof of a new one.
        ctl = controller()
        end = self.working(ctl, 1000, 30)
        ctl.on_limit("resets in 48 hours", now=end, source="screen")
        self.working(ctl, end, 4)
        self.assertIsNone(ctl.tick(end + 4))
        self.assertEqual(ctl.state, cr.WAITING)

    def test_a_spell_that_has_since_ended_does_not_count(self):
        ctl = self.waiting()
        end = self.working(ctl, 1100, 30)
        self.assertIsNone(ctl.tick(end + 60))       # quiet again for a minute
        self.assertEqual(ctl.state, cr.WAITING)

    def test_two_short_spells_do_not_add_up(self):
        ctl = self.waiting()
        end = self.working(ctl, 1100, 8)
        self.assertIsNone(ctl.tick(end))
        end = self.working(ctl, end + 30, 8)
        self.assertIsNone(ctl.tick(end))
        self.assertEqual(ctl.state, cr.WAITING)

    def test_an_answered_turn_in_the_transcript_cancels_the_wait(self):
        ctl = self.waiting()
        self.assertTrue(ctl.on_alive(now=1100, source="transcript"))
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertIsNone(ctl.tick(1e9 + 1))        # and stays cancelled

    def test_rows_written_just_before_the_banner_are_not_evidence(self):
        # The transcript is polled, so the answer that ran out of quota can reach
        # us a moment after the limit row that followed it.
        ctl = self.waiting()
        self.assertFalse(ctl.on_alive(now=1001, source="transcript"))
        self.assertEqual(ctl.state, cr.WAITING)

    def test_an_idle_session_is_not_cancelled_by_anything(self):
        ctl = controller()
        self.assertFalse(ctl.on_alive(now=1000, source="transcript"))
        self.assertEqual(ctl.state, cr.IDLE)

    def test_a_cancelled_wait_can_be_scheduled_again(self):
        # The new quota runs out too. Nothing about the first incident may block
        # the second: same banner text, hours apart.
        ctl = self.waiting()
        ctl.on_alive(now=2000, source="transcript")
        self.assertTrue(ctl.on_limit("You've hit your weekly limit · resets in 48 hours",
                                     now=3000, source="transcript"))
        self.assertEqual(ctl.state, cr.WAITING)

    def test_a_verify_in_progress_is_cancelled_too(self):
        ctl = self.waiting()
        self.assertEqual(ctl.tick(1000 + 48 * 3600 + 1)[0], "inject")
        self.assertEqual(ctl.state, cr.VERIFY)
        self.assertTrue(ctl.on_alive(now=1000 + 48 * 3600 + 30, source="transcript"))
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertEqual(ctl.attempts, 0)


class TestTheTerminalTalksBackToo(unittest.TestCase):
    """Field failure: a session waited out its reset and then deferred the retry
    every 15s for three hours, logging "unsent text in the prompt box" while
    nobody had touched the keyboard. claude sends "\\x1b[>q" at startup and the
    terminal's XTVERSION reply came back on our stdin, where 15 of its bytes were
    counted as typing. Nothing but Enter clears that counter, so the gate stayed
    shut for the rest of the session.

    Everything the terminal can answer with belongs here, not just what a human
    can press.
    """
    def draft_after(self, data):
        ctl = controller()
        ctl.on_user_bytes(data, now=0)
        return ctl.pending_input_chars

    def test_xtversion_reply_is_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1bP>|iTerm2 3.5.11\x1b\\"), 0)

    def test_xtversion_reply_through_tmux_is_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1bP>|tmux 3.5a\x1b\\"), 0)

    def test_a_dcs_reply_terminated_by_bel_is_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1bP1$r0m\x07"), 0)

    def test_an_osc_colour_reply_is_not_typing(self):
        self.assertEqual(
            self.draft_after(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\"), 0)

    def test_an_osc_reply_terminated_by_bel_is_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1b]10;rgb:c0c0/c0c0/c0c0\x07"), 0)

    def test_a_cursor_position_report_is_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1b[12;40R"), 0)

    def test_a_device_attributes_reply_is_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1b[?1;2c"), 0)

    def test_x10_mouse_coordinates_are_not_typing(self):
        # "\x1b[M" then three raw bytes; claude enables ?1000h, so a terminal
        # without SGR reporting sends these for every click and drag.
        self.assertEqual(self.draft_after(b"\x1b[M\x20\x40\x30"), 0)

    def test_sgr_mouse_reports_are_not_typing(self):
        self.assertEqual(self.draft_after(b"\x1b[<0;36;12M\x1b[<0;36;12m"), 0)

    def test_a_reply_split_across_two_reads_is_not_typing(self):
        ctl = controller()
        ctl.on_user_bytes(b"\x1bP>|iTer", now=0)
        ctl.on_user_bytes(b"m2 3.5.11\x1b\\", now=1)
        self.assertEqual(ctl.pending_input_chars, 0)

    def test_a_reply_arriving_mid_draft_leaves_the_draft_alone(self):
        ctl = controller()
        ctl.on_user_bytes(b"hi", now=0)
        ctl.on_user_bytes(b"\x1bP>|iTerm2 3.5.11\x1b\\", now=1)
        self.assertEqual(ctl.pending_input_chars, 2)

    def test_pasted_text_still_counts(self):
        # Bracketed paste is real content in the box; only the markers are ours
        # to swallow.
        self.assertEqual(self.draft_after(b"\x1b[200~hello\x1b[201~"), 5)


class TestTheKeyboardSpeaksKittyToo(unittest.TestCase):
    """T31. Claude Code asks for the kitty keyboard protocol (`CSI > 5 u`) and
    modifyOtherKeys (`CSI > 4 ; 2 m`), and a terminal that grants them (Ghostty,
    cmux, kitty, WezTerm...) sends the keys that empty the box as escape
    sequences. Field failure: a draft cleared that way stayed counted, and a
    verified handoff sat behind "unsent text in the prompt box" for seven
    minutes over an empty box, until its owner cleared the session by hand."""

    def draft_after(self, *chunks):
        ctl = controller()
        for t, data in enumerate(chunks):
            ctl.on_user_bytes(data, now=t)
        return ctl.pending_input_chars

    def test_esc_as_csi_u_clears_the_draft(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[27u"), 0)

    def test_ctrl_c_as_csi_u_clears_the_draft(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[99;5u"), 0)

    def test_ctrl_u_and_ctrl_w_as_csi_u_clear_it_like_their_plain_bytes(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[117;5u"), 0)
        self.assertEqual(self.draft_after(b"hello", b"\x1b[119;5u"), 0)

    def test_ctrl_c_under_modify_other_keys_clears_the_draft(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[27;5;99~"), 0)

    def test_alt_backspace_clears_it_like_ctrl_w(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[127;3u"), 0)
        self.assertEqual(self.draft_after(b"hello", b"\x1b\x7f"), 0)

    def test_enter_as_csi_u_submits(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[13u"), 0)

    def test_shift_enter_is_a_newline_not_a_submit(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[13;2u"), 6)

    def test_backspace_as_csi_u_erases_one(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[127u"), 4)

    def test_a_key_release_changes_nothing(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[99;5:3u"), 5)

    def test_a_typed_character_as_csi_u_counts(self):
        self.assertEqual(self.draft_after(b"\x1b[97u\x1b[65;2u"), 2)

    def test_arrows_and_function_keys_leave_the_draft_alone(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[D\x1b[1;3C\x1b[15~"), 5)

    def test_a_clear_split_across_two_reads_still_clears(self):
        self.assertEqual(self.draft_after(b"hello", b"\x1b[99", b";5u"), 0)

    def test_the_pressed_key_is_still_presence(self):
        ctl = controller()
        ctl.on_user_bytes(b"\x1b[27u", now=50)
        self.assertEqual(ctl.last_user_input, 50)


class TestPresenceIsNotTraffic(unittest.TestCase):
    """The other half of the same field failure. Swallowing the terminal's
    replies kept them out of the draft counter, but every one of them still
    stamped `last_user_input`, and that gate wants twenty quiet seconds. A
    session that had waited out a four-hour limit then logged "user is typing"
    every 15s while its owner sat watching the screen: switching to another app
    and back, moving the mouse over the window, anything that makes a terminal
    speak. Presence has to mean a key, not a byte.
    """
    def present_after(self, data):
        ctl = controller()
        ctl.on_user_bytes(data, now=500)
        return ctl.last_user_input == 500

    def test_a_cursor_position_report_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b[12;40R"))

    def test_a_device_attributes_reply_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b[?1;2c"))

    def test_an_xtversion_reply_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1bP>|iTerm2 3.5.11\x1b\\"))

    def test_an_osc_colour_reply_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07"))

    def test_focus_events_are_not_presence(self):
        # Switching to another window and back — which is what taking a
        # screenshot of the stuck session looks like from down here.
        self.assertFalse(self.present_after(b"\x1b[O"))
        self.assertFalse(self.present_after(b"\x1b[I"))

    def test_mouse_traffic_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b[<35;36;12M"))
        self.assertFalse(self.present_after(b"\x1b[M\x20\x40\x30"))

    def test_a_window_size_report_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b[8;40;120t"))

    def test_a_device_status_report_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b[0n"))

    def test_a_kitty_keyboard_flags_report_is_not_presence(self):
        self.assertFalse(self.present_after(b"\x1b[?3u"))

    def test_a_key_is_presence_even_when_it_is_an_escape_sequence(self):
        for key in (b"\x1b[A", b"\x1b[1;5C", b"\x1bOP", b"\x1b[3~", b"\x1bb"):
            self.assertTrue(self.present_after(key), key)

    def test_a_lone_escape_is_presence(self):
        self.assertTrue(self.present_after(b"\x1b"))

    def test_ordinary_typing_is_presence(self):
        self.assertTrue(self.present_after(b"hello"))
        self.assertTrue(self.present_after(b"\r"))

    def test_a_reply_riding_along_with_a_keystroke_still_counts(self):
        self.assertTrue(self.present_after(b"\x1b[12;40Rx"))

    def test_the_retry_goes_out_over_a_chattering_terminal(self):
        # The failure end to end: the reset has passed and the only thing on
        # stdin is the terminal answering claude. Nothing may defer this.
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        for t in range(7100, 7220, 5):
            ctl.on_user_bytes(b"\x1b[24;80R\x1b[I", now=t)
        self.assertIsNotNone(ctl.tick(7220))
        self.assertFalse(any("deferring" in line for line in ctl.log_lines))


class TestTheGateCannotJam(unittest.TestCase):
    """Whatever desyncs the counter next, the wait must still end. The gate is a
    courtesy to the human; the retry is the product."""
    def test_a_draft_nobody_touches_stops_blocking(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        ctl.on_user_bytes(b"stray", now=7100)
        self.assertIsNone(ctl.tick(7201))                # inside the grace period
        self.assertIsNotNone(ctl.tick(7100 + 601))
        self.assertTrue(any("stale" in line for line in ctl.log_lines))

    def test_a_draft_being_edited_keeps_blocking(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        ctl.on_user_bytes(b"a real", now=0)
        for t in range(7201, 7201 + 3000, 300):          # still typing at it
            ctl.on_user_bytes(b"x", now=t - 60)
            self.assertIsNone(ctl.tick(t))

    def test_the_keyboard_gate_has_a_ceiling(self):
        # Nothing is behind this one: the box is empty (a draft has its own
        # grace period), so it defers to a pair of hands and no more. Whatever
        # keeps stamping presence next — a key that repeats, a report we failed
        # to recognise — the retry still has to happen.
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        for t in range(7201, 7201 + 900, 15):
            ctl.on_user_bytes(b"\r", now=t)              # empty box, present
            self.assertIsNone(ctl.tick(t))
            self.assertTrue(ctl.deferred)
        ctl.on_user_bytes(b"\r", now=7201 + 900)
        self.assertIsNotNone(ctl.tick(7201 + 900))
        self.assertTrue(any("ceiling" in line or "held for" in line
                            for line in ctl.log_lines))

    def test_the_ceiling_does_not_type_over_a_draft(self):
        # Someone typing has an empty box between thoughts and a full one during
        # them, and only the empty case is safe to overrule. A message being
        # written for half an hour still waits.
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        for t in range(7201, 7201 + 3000, 15):
            ctl.on_user_bytes(b"y", now=t)               # the draft keeps growing
            self.assertIsNone(ctl.tick(t))

    def test_deferring_is_visible_and_clears(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        ctl.on_user_bytes(b"hi\r", now=7200)
        self.assertIsNone(ctl.tick(7201))
        self.assertTrue(ctl.deferred)
        self.assertIsNotNone(ctl.tick(7300))             # nobody there any more
        self.assertFalse(ctl.deferred)


class TestVerifyAndGiveUp(unittest.TestCase):
    def test_a_resumed_session_ends_the_incident(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        ctl.tick(7201)
        ctl.on_output("✻ Thinking… (esc to interrupt)", now=7210)
        ctl.tick(7211)
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertEqual(ctl.attempts, 0)

    def test_a_retry_that_did_not_take_hold_is_repeated(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        self.assertIsNotNone(ctl.tick(7201))            # attempt 1
        self.assertIsNotNone(ctl.tick(7201 + 61))       # verify window elapsed
        self.assertEqual(ctl.attempts, 2)

    def test_it_gives_up_rather_than_typing_forever(self):
        ctl = controller(max_attempts=2)
        ctl.on_limit(BANNER, now=0, source="transcript")
        t = 7201
        for _ in range(2):
            self.assertIsNotNone(ctl.tick(t))
            t += 61
        self.assertIsNone(ctl.tick(t))
        self.assertEqual(ctl.state, cr.DONE)

    def test_a_new_limit_reactivates_a_given_up_controller(self):
        ctl = controller(max_attempts=1)
        ctl.on_limit(BANNER, now=0, source="transcript")
        ctl.tick(7201)
        ctl.tick(7300)
        self.assertEqual(ctl.state, cr.DONE)
        ctl.on_limit("resets in 3 hours", now=8000, source="transcript")
        self.assertEqual(ctl.state, cr.WAITING)
        self.assertIsNotNone(ctl.tick(8000 + 10801))


class TestARetryThatWorkedIsNotRepeated(unittest.TestCase):
    """Field failure: the reset came, `continue` went out and claude resumed —
    and the wrapper typed it twice more and then painted "cr stopped", because
    the only thing it watched for was a footer wording that no longer exists.
    The transcript says it plainly, so ask the transcript."""
    def test_the_transcript_echo_ends_the_incident(self):
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="transcript")
        self.assertIsNotNone(ctl.tick(7201))            # `continue` goes out
        self.assertTrue(ctl.on_echo(now=7203))          # claude logged it
        self.assertEqual(ctl.state, cr.IDLE)
        self.assertEqual(ctl.attempts, 0)
        self.assertIsNone(ctl.tick(7203 + 3600))        # and never types again

    def test_an_echo_outside_an_incident_changes_nothing(self):
        ctl = controller()
        self.assertFalse(ctl.on_echo(now=10))
        self.assertEqual(ctl.state, cr.IDLE)

    def test_the_new_footer_alone_also_ends_it(self):
        # Belt and braces: with no transcript (scrape mode) the screen has to do.
        ctl = controller()
        ctl.on_limit(BANNER, now=0, source="screen")
        ctl.tick(7201)
        ctl.on_output("✻ Cogitating… 1m 3s · ↓ 812 tokens", now=7210)
        ctl.tick(7211)
        self.assertEqual(ctl.state, cr.IDLE)


class TestWaitScale(unittest.TestCase):
    def test_scale_divides_the_wait(self):
        ctl = controller(wait_scale=3600)
        ctl.on_limit("resets in 2 hours", now=0, source="transcript")
        self.assertAlmostEqual(ctl.wake_at, 2.0, delta=0.01)


# --------------------------------------------------------------------------- #
# the context restart
# --------------------------------------------------------------------------- #
# Settings for a controller with the feature on, small enough to drive by hand:
# a 1M window, a threshold at half of it, and a handoff of a couple of lines.
CTX = dict(
    context_tokens=500000, context_window="1M",
    context_env_max=0, context_no_1m=False,
    handoff_file="H.md", handoff_marker="HANDOFF", handoff_min_bytes=20,
    handoff_attempts=2,
    handoff_msg="Fold into `{file}` and end it with {marker}.",
    clear_cmd="/clear", resume_msg="Read `{file}` and continue from it.",
    root_idle=20, handoff_timeout=900, step_gap=3,
    context_cooldown=600, context_max_cycles=0,
)

MINE = "/proj/mine.jsonl"          # the transcript this terminal's session writes
AFTER = "/proj/after.jsonl"        # the fresh transcript /clear starts in its place
FULL = 700000                      # past the 500k threshold
RESUME = "Read `H.md` and continue from it."


class Handoff:
    """The file the model was asked to write, as the controller can see it.

    The controller reads it through an injected probe precisely so that a test
    can hand it a file that was never written, one that stops halfway, or one
    still carrying the marker from a previous attempt.
    """

    def __init__(self):
        self.state = None

    def write(self, ctl, at, body=None, marker=True, size=None):
        text = body if body is not None else "everything you need to know\n" * 4
        if marker:
            text += ctl.nonce + "\n"
        self.state = dict(size=len(text.encode()) if size is None else size,
                          mtime=at, tail=text[-512:])

    def touch(self, at):
        self.state["mtime"] = at

    def __call__(self, _path):
        return self.state


def restart_controller(**over):
    session_id = over.pop("session_id", None)
    cfg = dict(CFG)
    cfg.update(CTX)
    cfg.update(over)
    logs = []
    hand = Handoff()
    ctl = cr.Controller(cfg, logs.append, now=0, probe=hand, session_id=session_id)
    ctl.log_lines = logs
    ctl.handoff = hand
    return ctl


def usage(ctl, at, tokens, model="claude-opus-5", stop="end_turn", path=MINE,
          sidechain=False):
    """One assistant row, the way the watcher hands them over."""
    ctl.on_context(dict(kind="alive", path=path, tokens=tokens, model=model,
                        stop_reason=stop, sidechain=sidechain), at)


def moved(ctl, path, at, why="restart"):
    """What `main()` does before handing over the first row of a new
    transcript: `bind_transcript` is the only thing allowed to move
    `context_path`, so a test standing in for a real `/clear` has to call it
    too, the same as a neighbour's stray row must not."""
    ctl.bind_transcript(path, why, at)


class RestartTestCase(unittest.TestCase):
    """Every action the controller returns is recorded, because half of what is
    being asserted here is what it did NOT do — and `/clear` is the one action
    that cannot be taken back."""

    def setUp(self):
        self.acts = []

    def tick(self, ctl, t):
        action = ctl.tick(t)
        if action:
            self.acts.append(action)
        return action

    def injected(self):
        return [a[1] for a in self.acts if a[0] == "inject"]

    def assertNeverCleared(self):
        self.assertNotIn("/clear", self.injected())

    # -- the machine, one step at a time ------------------------------------ #
    def fold(self, ctl, tokens=FULL, at=0, tick_at=30, echo=True):
        """From "the context is full" to the folding phrase going out.

        `echo=True` simulates the ordinary case, where claude wrote the phrase
        into the transcript (T06) — every test about what happens once a
        handoff file is present assumes that already happened, the same way it
        assumes the phrase was typed at all. The tests that are specifically
        about a missing echo pass `echo=False`.
        """
        usage(ctl, at, tokens)
        action = self.tick(ctl, tick_at)
        if echo and action and action[0] == "inject":
            ctl.on_handoff_echo(MINE, tick_at)
        return action

    def folded(self, ctl, written_at=40):
        """...and the model writes the file and finishes its turn."""
        ctl.handoff.write(ctl, at=written_at)
        usage(ctl, written_at, FULL)

    def cleared(self, ctl):
        """...through the verification, /clear confirmed, and out the other side."""
        self.fold(ctl)
        self.folded(ctl)
        self.tick(ctl, 65)                  # /clear goes out (CLEAR_SENT)
        moved(ctl, AFTER, 66)                # claude starts a new session in place (T02)
        self.tick(ctl, 66)                   # confirmed: rstate -> CLEARED
        return ctl

    def unfolding(self, ctl):
        """...and the resume phrase sent."""
        self.cleared(ctl)
        self.tick(ctl, 69)
        return ctl


class TestTheHappyPath(RestartTestCase):
    def test_full_context_becomes_a_fresh_session(self):
        ctl = restart_controller()
        action = self.fold(ctl)
        self.assertEqual(action[0], "inject")
        self.assertIn("H.md", action[1])
        self.assertIn(ctl.nonce, action[1])
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)

        self.folded(ctl)
        self.assertIsNone(self.tick(ctl, 45))      # the transcript is still growing
        self.assertEqual(self.tick(ctl, 65), ("inject", "/clear", False))
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        moved(ctl, AFTER, 66)                      # claude starts a new session in place
        self.assertIsNone(self.tick(ctl, 66))      # confirmed: CLEAR_SENT -> CLEARED
        self.assertEqual(ctl.rstate, cr.CLEARED)

        self.assertEqual(self.tick(ctl, 69), ("inject", RESUME, False))
        self.assertEqual(ctl.rstate, cr.RESUME_SENT)

        # The new session answers, and it answers small.
        usage(ctl, 72, 8000, path=AFTER)
        action = self.tick(ctl, 73)
        self.assertEqual(action[0], "notify")
        self.assertIn("restarted", action[1])
        self.assertIsNone(ctl.rstate)

    def test_the_marker_is_different_every_time(self):
        first = restart_controller()
        self.fold(first)
        second = restart_controller()
        self.fold(second)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertTrue(first.nonce.startswith("HANDOFF-"))

    def test_a_restart_is_followed_by_silence(self):
        ctl = restart_controller()
        self.unfolding(ctl)
        moved(ctl, "/proj/after.jsonl", 72)
        usage(ctl, 72, 8000, path="/proj/after.jsonl")
        self.tick(ctl, 73)
        usage(ctl, 100, FULL, path="/proj/after.jsonl")   # full again straight away
        self.assertIsNone(self.tick(ctl, 200))     # inside the cooldown
        self.assertIsNotNone(self.tick(ctl, 700))

    def test_the_fuse_caps_how_many_a_session_gets(self):
        ctl = restart_controller(context_max_cycles=1, context_cooldown=0)
        self.fold(ctl)
        usage(ctl, 40, FULL)                       # no file: attempt 2
        self.tick(ctl, 65)
        usage(ctl, 70, FULL)
        self.tick(ctl, 95)                         # ...and the abort
        self.assertEqual(ctl.cycles, 1)
        usage(ctl, 100, FULL)
        self.assertIsNone(self.tick(ctl, 200))


class TestPostRestartHeadroom(RestartTestCase):
    """T13: a threshold that is a fraction of the window says nothing about the
    absolute baseline a freshly-cleared session already carries (system prompt,
    CLAUDE.md, MCP tool defs, the resume read) -- against a small window that
    baseline alone can eat most of the threshold's own headroom, and the
    restart it just bought fires again in 30-40 minutes instead of hours.
    """

    def restarted(self, ctl, pre_tokens, post_tokens, model):
        usage(ctl, 0, pre_tokens)
        action = self.tick(ctl, 30)
        if action and action[0] == "inject":
            ctl.on_handoff_echo(MINE, 30)
        ctl.handoff.write(ctl, at=40)
        usage(ctl, 40, pre_tokens)
        self.tick(ctl, 65)                 # /clear goes out
        moved(ctl, AFTER, 66)
        self.tick(ctl, 66)                 # confirmed: CLEARED
        self.tick(ctl, 69)                 # resume phrase sent
        usage(ctl, 72, post_tokens, model=model, path=AFTER)
        return self.tick(ctl, 73)

    def test_a_baseline_that_eats_the_threshold_raises_it(self):
        # 200k window, 51% threshold (102k), 57k left after the restart: only
        # 45k of headroom, under the 80k floor, so the threshold is raised.
        ctl = restart_controller(context_tokens=102000, context_window="200k")
        action = self.restarted(ctl, pre_tokens=150000, post_tokens=57000,
                                model="claude-haiku-4-5")
        self.assertEqual(action[0], "notify")
        self.assertIn("restarted", action[1])
        self.assertTrue(any("raising the threshold" in l for l in ctl.log_lines))
        # baseline (57k) + the full CR_CONTEXT_MIN_HEADROOM (80k)...
        self.assertGreaterEqual(ctl.context_limit, 137000)
        # ...but never past this model's own compaction point (190k for the
        # 200k claude family), the ceiling nothing here is allowed to guess past.
        self.assertLessEqual(ctl.context_limit, 190000)

    def test_plenty_of_headroom_leaves_the_threshold_alone(self):
        # 1M window, 51% threshold (510k), 55k left after the restart: 455k of
        # headroom is nowhere near the 80k floor, so nothing changes.
        ctl = restart_controller(context_tokens=510000, context_window="1M")
        action = self.restarted(ctl, pre_tokens=700000, post_tokens=55000,
                                model="claude-opus-5")
        self.assertEqual(action[0], "notify")
        self.assertNotIn("raising the threshold", action[1])
        self.assertFalse(any("raising the threshold" in l for l in ctl.log_lines))
        self.assertEqual(ctl.context_limit, 510000)


class TestRestartFrequencyGuard(RestartTestCase):
    """T13: a threshold that cannot hold for longer than 30-40 minutes is not
    being protected by the restart, it is being ground down by it. More than
    `CR_CONTEXT_MAX_PER_HOUR` restarts inside a rolling hour disables the
    trigger for the rest of the session instead of guessing at a better one."""

    def cycle(self, ctl, t0, before_path, after_path):
        usage(ctl, t0, FULL, path=before_path)
        action = self.tick(ctl, t0 + 30)
        if action and action[0] == "inject":
            ctl.on_handoff_echo(before_path, t0 + 30)
        ctl.handoff.write(ctl, at=t0 + 40)
        usage(ctl, t0 + 40, FULL, path=before_path)
        self.tick(ctl, t0 + 65)
        moved(ctl, after_path, t0 + 66)
        self.tick(ctl, t0 + 66)
        self.tick(ctl, t0 + 69)
        usage(ctl, t0 + 72, 8000, path=after_path)
        return self.tick(ctl, t0 + 73)

    def test_a_fourth_restart_within_the_hour_switches_it_off(self):
        ctl = restart_controller(context_cooldown=0)
        p1, p2, p3 = "/proj/after1.jsonl", "/proj/after2.jsonl", "/proj/after3.jsonl"
        self.cycle(ctl, 0, MINE, p1)
        self.cycle(ctl, 100, p1, p2)
        self.cycle(ctl, 200, p2, p3)
        self.assertEqual(ctl.cycles, 3)
        self.assertTrue(ctl.context_enabled)

        usage(ctl, 300, FULL, path=p3)
        action = self.tick(ctl, 330)
        self.assertEqual(action[0], "notify")
        self.assertIn("restarts in the last hour", action[1])
        self.assertTrue(ctl.context_off)
        self.assertFalse(ctl.context_enabled)
        self.assertIsNone(ctl.badge_context())
        self.assertIsNone(ctl.rstate)
        self.assertEqual(ctl.cycles, 3)            # the fourth never started
        self.assertEqual(ctl.badge_warn(), "restart off")

        # T14: this debt never went through UNFOLD_FAILED at all, but it is the
        # same standing-off-for-the-session state, and it is repeated the same
        # way -- until a key proves someone is actually looking.
        self.assertIsNone(self.tick(ctl, 330 + 299))
        again = self.tick(ctl, 330 + 301)
        self.assertEqual(again[0], "notify")
        self.assertIn("switched off for this session", again[1])

        ctl.on_user_bytes(b"x", now=330 + 302)
        self.assertIsNone(self.tick(ctl, 330 + 301 + 400))    # acknowledged: quiet now
        self.assertEqual(ctl.badge_warn(), "restart off")     # ...but still true on screen


class TestACollapsedRowKeepsEndTurn(RestartTestCase):
    """T11: the row that closes the fold turn can be followed, in the very same
    poll, by a streaming fragment of the NEXT turn whose stop_reason is still
    None. `transcript_limit_records` is what merges rows seen in one poll into
    one before the controller ever sees them; if that merge let the None
    overwrite "end_turn", `_handoff_fault` would see a turn that "ended" with
    stop_reason=None and call the fold a failure that never happened.
    """

    def _assistant_row(self, text, tokens, stop):
        msg = {"role": "assistant", "model": "claude-opus-5", "stop_reason": stop,
               "content": [{"type": "text", "text": text}],
               "usage": {"input_tokens": 2, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": tokens - 2, "output_tokens": 10}}
        return {"type": "assistant", "isSidechain": False, "message": msg}

    def _one_poll(self, *rows):
        """The single collapsed record `transcript_limit_records` hands back for
        rows all seen within one poll — the same path production code takes."""
        tmpdir = tempfile.mkdtemp(prefix="cr-t11-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "s.jsonl")
        with open(path, "a") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        _, recs = cr.transcript_limit_records(path, 0)
        self.assertEqual(len(recs), 1, recs)
        return recs[0]

    def test_a_streaming_fragment_after_end_turn_does_not_flip_the_verdict(self):
        ctl = restart_controller()
        self.fold(ctl)
        ctl.handoff.write(ctl, at=40)
        rec = self._one_poll(self._assistant_row("closing the fold", FULL, "end_turn"),
                             self._assistant_row("next turn starting", FULL, None))
        self.assertEqual(rec["stop_reason"], "end_turn")
        ctl.on_context(dict(kind="alive", path=MINE, tokens=rec["tokens"],
                            model=rec["model"], stop_reason=rec["stop_reason"],
                            sidechain=rec["sidechain"]), 40)
        self.assertEqual(self.tick(ctl, 65), ("inject", "/clear", False))
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)


class TestTheHandoffLatchSurvivesBackgroundActivity(RestartTestCase):
    """T12: agent notifications, notify_idle, hooks -- any of them can wake the
    model into a new turn once the fold turn's own end_turn already landed on
    a valid file. Latching the moment those two line up, rather than waiting
    for the transcript to fall quiet, is what keeps that later turn from
    reading as "no usable handoff" 900s on.
    """

    def test_background_tool_use_after_end_turn_does_not_undo_the_latch(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl, written_at=40)             # file lands, turn ends with end_turn
        self.assertIsNone(self.tick(ctl, 41))       # still inside root_idle: too soon for "quiet"
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)
        self.assertTrue(any("handoff accepted" in ln for ln in ctl.log_lines))

        last = 41
        for t in range(45, 345, 15):                # ~5 minutes of background chatter
            usage(ctl, t, FULL, stop="tool_use")
            self.assertIsNone(self.tick(ctl, t))
            self.assertEqual(ctl.rstate, cr.HANDOFF_OK)   # the latch survives it
            last = t
        self.assertNeverCleared()

        self.assertIsNone(self.tick(ctl, last + 5))       # still settling
        self.assertEqual(self.tick(ctl, last + 25), ("inject", "/clear", False))

    def test_the_file_losing_its_marker_after_the_latch_reopens_it(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl, written_at=40)
        self.tick(ctl, 41)
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)

        ctl.handoff.write(ctl, at=45, marker=False)  # something rewrote the file
        usage(ctl, 46, FULL, stop="tool_use")
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        self.assertIn("changed after it was accepted", ctl.log_lines[-1])
        self.assertNeverCleared()


class TestTheRegistryStatusIsAnExtraBusySignal(RestartTestCase):
    """T02 item 4: `sessions/<pid>.json`'s status/statusUpdatedAt holds the
    gate too, on top of the transcript-based checks -- never instead of them."""

    def test_a_busy_status_holds_the_clear_even_once_the_transcript_looks_idle(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl, written_at=40)
        ctl.on_agent_status("busy", 60)
        self.assertEqual(ctl._session_busy(65), "the session reports busy")
        self.assertIsNone(self.tick(ctl, 65))       # transcript alone would clear here
        ctl.on_agent_status("idle", 60)
        # T12: the latch already caught at t=65 regardless of busy, so what is
        # being waited out here is `_send_clear`'s own backoff (GATE_RETRY),
        # not the busy status -- it flipped idle before this tick either way.
        self.assertEqual(self.tick(ctl, 65 + ctl.GATE_RETRY), ("inject", "/clear", False))

    def test_a_stale_busy_status_is_not_trusted(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl, written_at=40)
        ctl.on_agent_status("busy", 4)              # 61s old by t=65: too stale
        self.assertEqual(self.tick(ctl, 65), ("inject", "/clear", False))


class TestTheClearNeedsConfirmation(RestartTestCase):
    """T08: `/clear` going out is not proof it landed. `CLEAR_SENT` sits between
    the send and `CLEARED` until something says it actually happened -- a rebind
    to a fresh transcript, or (see `TestCodexSettling` in test_codex.py) the
    screen simply going quiet."""

    def test_no_trace_within_verify_sends_clear_again_then_confirms_and_resumes(self):
        # A huge clear_settle isolates the rebind-only path: nothing here is
        # allowed to confirm on its own just because the screen went quiet.
        ctl = restart_controller(clear_settle=10000)
        self.fold(ctl)
        self.folded(ctl)
        self.assertEqual(self.tick(ctl, 65), ("inject", "/clear", False))
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        self.assertIsNone(self.tick(ctl, 100))      # under CR_VERIFY_SEC(60) since the send
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        action = self.tick(ctl, 126)                # 60s+ since the send: retype
        self.assertEqual(action, ("inject", "/clear", False))
        self.assertIn("left no trace; sending it again", ctl.log_lines[-1])
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)

        moved(ctl, AFTER, 130)                       # claude starts a new session in place
        self.assertIsNone(self.tick(ctl, 130))
        self.assertEqual(ctl.rstate, cr.CLEARED)

        self.assertEqual(self.tick(ctl, 133), ("inject", RESUME, False))
        self.assertEqual(ctl.rstate, cr.RESUME_SENT)

    def test_cleared_waits_out_a_typing_person_for_as_long_as_it_takes(self):
        ctl = restart_controller(clear_settle=10000)
        self.cleared(ctl)
        self.assertEqual(ctl.rstate, cr.CLEARED)

        notifies = []
        t = 70
        while t < 1070:
            ctl.on_user_bytes(b"\r", now=t)          # a person at the keyboard, never idle
            action = self.tick(ctl, t)
            if action:
                notifies.append(action)
            t += 5
        # No abort in 1000s of being held, and it kept saying so.
        self.assertEqual(ctl.rstate, cr.CLEARED)
        self.assertGreaterEqual(len(notifies), 2)
        for action in notifies:
            self.assertEqual(action[0], "notify")
            self.assertIn("unfold is waiting for", action[1])

        # The last keystroke was at t=1065; the gate opens CR_USER_IDLE_SEC(20)
        # later, and the resume goes out the moment it does.
        self.assertIsNone(self.tick(ctl, 1080))
        self.assertEqual(self.tick(ctl, 1090), ("inject", RESUME, False))

    def test_a_gate_stuck_past_a_minute_says_so_in_the_badge(self):
        # T14: CLEARED has no timeout of its own -- the only way the corner can
        # tell a routine few-second wait for the gate apart from one that has
        # been stuck for minutes is to time it itself.
        ctl = restart_controller()
        self.cleared(ctl)                             # rstate -> CLEARED at t=66
        for t in range(66, 132, 5):                    # a person at the keyboard, never idle
            ctl.on_user_bytes(b"x", now=t)
            self.tick(ctl, t)
        self.assertEqual(ctl.rstate, cr.CLEARED)        # still held throughout

        self.assertIsNone(ctl.badge_warn(100))          # under a minute since CLEARED began
        self.assertEqual(ctl.badge_warn(130), "unfold?")   # the same hold, past a minute now


class TestAClearDoneByHand(RestartTestCase):
    """T30. Field failure: the handoff was verified, the draft gate held our
    `/clear` back, and the person cleared the session and pointed it at the
    handoff themselves. The wrapper, still at "folded", then sent its own
    `/clear` into that fresh session -- wiping the turn they had just started --
    and typed the resume phrase over it. A change of session identity while
    the fold waits is a clear someone else already did."""

    def held_fold(self, ctl):
        """Folded and verified, with our /clear held back by a draft."""
        self.fold(ctl)
        self.folded(ctl)
        ctl.on_user_bytes(b"x", now=41)
        self.assertIsNone(self.tick(ctl, 65))
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)
        ctl.on_user_bytes(b"\x1b[27u/clear\r", now=98)   # ...then their own /clear
        moved(ctl, AFTER, 100, why="session identity")

    def test_our_clear_never_follows_theirs(self):
        ctl = restart_controller()
        self.held_fold(ctl)
        self.assertEqual(ctl.rstate, cr.CLEARED)
        for t in range(101, 400, 5):
            self.tick(ctl, t)
        self.assertNeverCleared()

    def test_a_prompt_of_their_own_means_nothing_is_left_to_unfold(self):
        ctl = restart_controller()
        self.held_fold(ctl)
        self.assertTrue(ctl.on_human_prompt(104))
        self.assertIsNone(ctl.rstate)
        for t in range(105, 400, 5):
            self.tick(ctl, t)
        self.assertNeverCleared()
        self.assertNotIn(RESUME, self.injected())

    def test_left_alone_after_it_the_session_is_unfolded(self):
        ctl = restart_controller()
        self.held_fold(ctl)
        self.assertIsNone(self.tick(ctl, 115))      # their keystroke is under 20s old
        self.assertEqual(self.tick(ctl, 125), ("inject", RESUME, False))
        self.assertNeverCleared()

    def test_a_turn_of_theirs_in_the_new_session_holds_the_unfold(self):
        ctl = restart_controller()
        self.held_fold(ctl)
        ctl.note_growth([AFTER], 120)
        self.assertIsNone(self.tick(ctl, 125))
        self.assertEqual(self.tick(ctl, 141), ("inject", RESUME, False))

    def test_a_guess_from_the_growth_heuristic_is_not_a_clear(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl)
        ctl.on_user_bytes(b"x", now=41)
        self.tick(ctl, 65)
        moved(ctl, AFTER, 100, why="fallback heuristic")
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)

    def test_a_finished_handoff_not_yet_latched_counts_too(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl)
        moved(ctl, AFTER, 45, why="session identity")
        self.assertEqual(ctl.rstate, cr.CLEARED)
        self.assertEqual(self.tick(ctl, 70), ("inject", RESUME, False))
        self.assertNeverCleared()

    def test_a_fold_cut_short_by_it_is_dropped_without_a_word(self):
        ctl = restart_controller()
        self.fold(ctl)
        moved(ctl, AFTER, 35, why="session identity")
        self.assertIsNone(ctl.rstate)
        for t in range(36, 400, 5):
            self.tick(ctl, t)
        self.assertEqual(len(self.injected()), 1)   # the fold phrase, and nothing since

    def test_a_prompt_after_our_own_clear_does_not_cancel_the_unfold(self):
        ctl = restart_controller()
        self.cleared(ctl)
        self.assertFalse(ctl.on_human_prompt(67))
        self.assertEqual(ctl.rstate, cr.CLEARED)


class TestNothingIsClearedOnAPromise(RestartTestCase):
    """The dangerous failure in the whole design: the fold was asked for, the
    model did not manage it, and the context is cleared anyway — which loses the
    session's work with nothing written down. The model reporting success is not
    evidence; it says only that the model believes it finished."""

    def failing(self, **over):
        ctl = restart_controller(**over)
        self.fold(ctl)
        return ctl

    def second_attempt_then_abort(self, ctl):
        """Both attempts end the same way; the second one is the abort."""
        usage(ctl, 70, FULL)
        return self.tick(ctl, 95)

    def assertCancelledFor(self, ctl, action, why):
        """A file carrying this attempt's marker proves the phrase was delivered,
        so the abort owes the model a cancel (T09) rather than a plain notify."""
        self.assertIsNone(action)
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)
        self.assertIn(why, ctl.log_lines[-1])

    def test_a_file_that_was_never_written(self):
        ctl = self.failing()
        usage(ctl, 40, FULL)
        action = self.tick(ctl, 65)
        self.assertEqual(action[0], "inject")      # attempt 2, not /clear
        self.assertEqual(ctl.handoff_tries, 2)
        action = self.second_attempt_then_abort(ctl)
        self.assertEqual(action[0], "notify")
        self.assertIn("never written", action[1])
        self.assertNeverCleared()
        self.assertIsNone(ctl.rstate)

    def test_a_file_too_short_to_be_a_handoff(self):
        ctl = self.failing()
        ctl.handoff.write(ctl, at=40, body="", size=8)
        usage(ctl, 40, FULL)
        self.tick(ctl, 65)
        ctl.handoff.write(ctl, at=70, body="", size=8)
        action = self.second_attempt_then_abort(ctl)
        self.assertCancelledFor(ctl, action, "byte floor")
        self.assertNeverCleared()

    def test_a_file_that_stops_before_the_marker(self):
        # The one failure the marker exists for: a write that ran out partway,
        # which looks exactly like a complete file from every other angle.
        ctl = self.failing()
        ctl.handoff.write(ctl, at=40, marker=False)
        usage(ctl, 40, FULL)
        self.tick(ctl, 65)
        ctl.handoff.write(ctl, at=70, marker=False)
        action = self.second_attempt_then_abort(ctl)
        self.assertIn("does not end with", action[1])
        self.assertNeverCleared()

    def test_a_file_older_than_the_request_for_it(self):
        ctl = self.failing()
        ctl.handoff.write(ctl, at=5)               # left over from before
        usage(ctl, 40, FULL)
        self.tick(ctl, 65)
        action = self.second_attempt_then_abort(ctl)
        self.assertIn("older than the request", action[1])
        self.assertNeverCleared()

    def test_a_turn_cut_off_at_max_tokens(self):
        # A perfect-looking file whose turn the runtime says was truncated. The
        # marker cannot have been written by a finished answer, whatever the
        # bytes say.
        ctl = self.failing()
        ctl.handoff.write(ctl, at=40)
        usage(ctl, 40, FULL, stop="max_tokens")
        self.tick(ctl, 65)
        ctl.handoff.write(ctl, at=70)
        usage(ctl, 70, FULL, stop="max_tokens")
        self.assertCancelledFor(ctl, self.tick(ctl, 95), "max_tokens")
        self.assertNeverCleared()

    def test_a_turn_that_was_refused(self):
        ctl = self.failing()
        ctl.handoff.write(ctl, at=40)
        usage(ctl, 40, FULL, stop="refusal")
        self.tick(ctl, 65)
        ctl.handoff.write(ctl, at=70)
        usage(ctl, 70, FULL, stop="refusal")
        self.assertCancelledFor(ctl, self.tick(ctl, 95), "refusal")
        self.assertNeverCleared()

    def test_a_session_that_kept_working_is_waited_for_not_cleared(self):
        # The file is complete and end_turn already landed, so T12 latches it
        # (HANDOFF_OK) without waiting for the transcript to fall quiet -- but
        # "do not start new work" was ignored, and clearing now would take that
        # new work with it, so `/clear` itself still has to wait.
        ctl = self.failing()
        ctl.handoff.write(ctl, at=40)
        usage(ctl, 40, FULL)
        for t in range(45, 400, 10):
            ctl.note_growth([MINE], now=t)         # still writing
            self.assertIsNone(self.tick(ctl, t))
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)
        self.assertNeverCleared()

    def test_a_turn_still_in_flight_is_not_a_failed_one(self):
        # Quiet transcript, no file yet, and the last row is a tool call: the
        # answer has not landed. Failing it here would burn an attempt on a
        # session that is doing exactly as it was asked.
        ctl = self.failing()
        usage(ctl, 40, FULL, stop="tool_use")
        self.assertIsNone(self.tick(ctl, 65))
        self.assertEqual(ctl.handoff_tries, 1)

    def test_the_marker_from_the_last_attempt_is_not_this_attempts_proof(self):
        # Without a fresh nonce, the file left behind by a rejected attempt
        # satisfies the next one — which turns "the write finished" into "a
        # write finished once", and those are not the same claim.
        ctl = self.failing()
        ctl.handoff.write(ctl, at=40)
        usage(ctl, 40, FULL, stop="max_tokens")
        first = ctl.nonce
        action = self.tick(ctl, 65)
        self.assertEqual(action[0], "inject")
        self.assertNotEqual(ctl.nonce, first)
        ctl.handoff.touch(70)                      # same bytes, same old marker
        usage(ctl, 70, FULL)
        self.assertIn("does not end with", self.tick(ctl, 95)[1])
        self.assertNeverCleared()

    def test_the_fold_gives_up_rather_than_asking_forever(self):
        ctl = restart_controller(handoff_attempts=3)
        self.fold(ctl)
        for t in (40, 70, 100):
            usage(ctl, t, FULL)
            self.tick(ctl, t + 25)
        self.assertEqual(ctl.handoff_tries, 3)
        self.assertEqual(len([a for a in self.acts if a[0] == "notify"]), 1)
        self.assertNeverCleared()

    def test_a_fold_nobody_answers_times_out(self):
        ctl = restart_controller(handoff_timeout=60)
        self.fold(ctl)
        usage(ctl, 40, FULL, stop="tool_use")      # a turn that never ends
        # T09: the phrase was echoed (fold()'s default), so the timeout leads
        # into a cancel rather than a plain notify -- see TestCancelAfterAbort.
        self.assertIsNone(self.tick(ctl, 200))
        self.assertIn("no usable handoff", ctl.log_lines[-1])
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)
        self.assertNeverCleared()


class TestTheHandoffPhraseHasToHaveLanded(RestartTestCase):
    """T06: the fold phrase's own echo is proof it reached the session at all —
    the popup-ate-the-keystrokes case a valid-looking file can never rule out on
    its own, because a file that merely looks right could belong to a
    neighbouring session (T04/T06)."""

    def test_a_phrase_with_no_echo_and_no_file_is_retyped_then_gives_up(self):
        ctl = restart_controller()
        self.fold(ctl, echo=False)
        self.assertIsNone(self.tick(ctl, 89))          # under CR_VERIFY_SEC(60)

        action = self.tick(ctl, 90)                    # 60s since the send: retype
        self.assertEqual(action[0], "inject")
        self.assertEqual(ctl.handoff_tries, 1)          # not spent on a no-echo resend
        self.assertEqual(ctl.handoff_echo_retries, 1)
        self.assertIn("left no trace", ctl.log_lines[-2])

        action = self.tick(ctl, 150)                    # 60s since THAT send
        self.assertEqual(action[0], "inject")
        self.assertEqual(ctl.handoff_echo_retries, 2)

        action = self.tick(ctl, 210)                    # the cap is spent
        self.assertEqual(action[0], "notify")
        self.assertIn("never reached the session", action[1])
        self.assertNeverCleared()
        self.assertIsNone(ctl.rstate)

    def test_the_echo_is_proof_of_identity_even_on_a_different_transcript(self):
        ctl = restart_controller()
        self.fold(ctl, echo=False)
        self.assertEqual(ctl.context_path, MINE)
        other = "/proj/neighbour.jsonl"
        self.assertTrue(ctl.on_handoff_echo(other, 31))
        self.assertTrue(ctl.handoff_echoed)
        self.assertEqual(ctl.context_path, other)
        self.assertIn("was echoed there", ctl.log_lines[-1])

    def test_a_file_ending_with_this_attempts_marker_proves_delivery(self):
        # The echo can go unrecognised (a phrase queued mid-turn is written as
        # a row of its own), but the marker was typed into this terminal only:
        # a file ending with it is the answer to it. Waiting on the echo here
        # once left a restart hanging forever on a perfect handoff.
        ctl = restart_controller()
        self.fold(ctl, echo=False)
        self.folded(ctl, written_at=40)
        self.assertEqual(self.tick(ctl, 65), ("inject", "/clear", False))
        self.assertTrue(ctl.handoff_echoed)
        self.assertTrue(any("the phrase reached the session" in l for l in ctl.log_lines))

    def test_a_file_ending_with_a_previous_marker_proves_nothing(self):
        ctl = restart_controller()
        self.fold(ctl, echo=False)
        ctl.handoff.write(ctl, at=40)
        ctl.nonce = "HANDOFF-00000000"                  # as if a later attempt were out
        usage(ctl, 40, FULL)
        self.tick(ctl, 65)
        self.assertFalse(ctl.handoff_echoed)
        self.assertNeverCleared()

    def test_a_queued_phrase_is_not_retyped(self):
        # Retyping a phrase that is only waiting in claude's queue stacks a
        # second marker behind the first and makes the model write it twice.
        ctl = restart_controller()
        self.fold(ctl, echo=False)
        self.assertTrue(ctl.on_handoff_queued(31))
        self.assertIn("queued behind the running turn", ctl.log_lines[-1])
        self.assertIsNone(self.tick(ctl, 90))            # past CR_VERIFY_SEC
        self.assertIsNone(self.tick(ctl, 150))
        self.assertEqual(ctl.handoff_echo_retries, 0)
        self.assertEqual(len(self.injected()), 1)

    def test_a_queued_phrase_still_answers_to_the_timeout(self):
        ctl = restart_controller(handoff_timeout=120)
        self.fold(ctl, echo=False)
        ctl.on_handoff_queued(31)
        action = self.tick(ctl, 400)
        self.assertEqual(action[0], "notify")
        self.assertIn("never written", action[1])
        self.assertIsNone(ctl.rstate)
        self.assertNeverCleared()


class TestTheClearHasToHaveWorked(RestartTestCase):
    """The other half: `/clear` went out and the context did not move. Retrying
    that forever would type into a session that stays exactly as full as it was,
    for as long as it lives."""

    def test_a_clear_that_did_nothing_switches_the_feature_off(self):
        ctl = restart_controller()
        self.unfolding(ctl)
        ctl.on_resume_echo(70)                     # the phrase was accepted...
        usage(ctl, 80, FULL, path=AFTER)           # ...and the context is untouched
        action = self.tick(ctl, 130)
        self.assertEqual(action[0], "notify")
        self.assertIn("did not fall", action[1])
        self.assertTrue(ctl.context_off)
        usage(ctl, 200, FULL)
        self.assertIsNone(self.tick(ctl, 200))     # too soon to repeat, and never retries
        # T14: the debt itself is repeated well past CR_NOTIFY_REPEAT_SEC -- the
        # one notify() line and Claude's own repaint are not enough for a debt
        # that can sit for hours -- but the trigger stays off, not merely quiet.
        again = self.tick(ctl, 1000)
        self.assertEqual(again[0], "notify")
        self.assertIn("switched off for this session", again[1])
        self.assertIsNone(ctl.rstate)

    def test_the_reading_it_is_judged_against_is_the_one_it_acted_on(self):
        # A project directory can hold more than one live transcript, and the
        # one that grew last is only usually ours. Taking the "before" figure at
        # /clear time rather than at the trigger would mean judging the restart
        # against whichever session happened to speak in between — and against a
        # small enough number, a real restart reads as a failure and switches the
        # feature off for good. T04: the neighbour's row is not a `/clear`
        # `bind_transcript` never runs for it, so `on_context` ignores it
        # outright rather than needing `context_before`'s max() to paper over it.
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl)
        usage(ctl, 66, 9000, path="/proj/somebody-else.jsonl")
        self.assertEqual(ctl.context_tokens, FULL)
        self.assertEqual(self.tick(ctl, 90), ("inject", "/clear", False))
        self.assertEqual(ctl.context_before, FULL)

    def test_a_resume_that_never_lands_says_that_and_not_something_else(self):
        # Two failures end in the same place and read completely differently in
        # a log: a clear that did nothing, and a phrase that never arrived.
        ctl = restart_controller(resume_attempts=1)
        self.unfolding(ctl)
        usage(ctl, 80, FULL, path=AFTER)
        self.assertEqual(self.tick(ctl, ctl.rwake), ("inject", RESUME, False))
        action = self.tick(ctl, ctl.rwake)
        self.assertEqual(action[0], "notify")
        self.assertIn("never reached the session", action[1])
        self.assertTrue(ctl.context_off)
        self.assertEqual(ctl.rstate, cr.UNFOLD_FAILED)

    def test_a_resume_that_left_no_trace_is_sent_again(self):
        ctl = restart_controller()
        self.unfolding(ctl)
        usage(ctl, 80, FULL, path=AFTER)
        action = self.tick(ctl, 130)
        self.assertEqual(action, ("inject", RESUME, False))
        self.assertEqual(ctl.resume_tries, 1)

    def test_a_context_that_only_dipped_is_not_a_restart(self):
        ctl = restart_controller()
        self.unfolding(ctl)
        ctl.on_resume_echo(70)
        moved(ctl, "/proj/after.jsonl", 80)
        usage(ctl, 80, 600000, path="/proj/after.jsonl")   # 700k -> 600k: no
        self.assertEqual(self.tick(ctl, 130)[0], "notify")
        self.assertTrue(ctl.context_off)

    def test_a_zero_reading_does_not_complete_a_restart(self):
        # A "<synthetic>"/no-usage row on the new transcript reads as
        # tokens=0, not as "the context is empty". Reading it as a fall would
        # end the restart on a row that never actually measured anything, with
        # no real `/clear` verification behind it.
        ctl = restart_controller()
        self.unfolding(ctl)
        moved(ctl, "/proj/after.jsonl", 72)
        usage(ctl, 72, 0, path="/proj/after.jsonl")
        self.assertIsNone(self.tick(ctl, 73))
        self.assertEqual(ctl.rstate, cr.RESUME_SENT)
        # A real answer on the same transcript still completes it.
        usage(ctl, 80, 55000, path="/proj/after.jsonl")
        action = self.tick(ctl, 81)
        self.assertEqual(action[0], "notify")
        self.assertIn("restarted", action[1])
        self.assertIsNone(ctl.rstate)


class TestCancelAfterAbort(RestartTestCase):
    """T09: an abort that comes AFTER the fold already reached the session (an
    echo was seen) cannot just leave the session be, the way an ordinary abort
    does -- the model was told to wrap up and start nothing new, and walking
    away leaves an autonomous session sitting on that instruction until a
    person happens to show up. Only HANDOFF_SENT/HANDOFF_OK, and only once the
    phrase was echoed, take this branch instead of the plain notify."""

    def test_a_delivered_fold_with_no_file_by_the_timeout_gets_a_cancel(self):
        ctl = restart_controller()
        self.fold(ctl)                              # echo=True: the phrase landed
        self.assertTrue(ctl.handoff_echoed)

        self.assertIsNone(self.tick(ctl, 30 + 900 + 1))   # handoff_timeout elapses, no file
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)
        self.assertIn("restart aborted at handoff_sent", ctl.log_lines[-1])

        action = self.tick(ctl, 30 + 900 + 2)
        self.assertEqual(action[0], "inject")
        self.assertIn("cancelled", action[1])
        self.assertIn("asking the session to carry on", ctl.log_lines[-1])
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)    # one more tick still to close it

        end_at = 30 + 900 + 3
        self.assertIsNone(self.tick(ctl, end_at))
        self.assertIsNone(ctl.rstate)
        self.assertEqual(ctl.cooldown_until, end_at + ctl.cfg["context_cooldown"])

    def test_an_undelivered_fold_gets_no_cancel(self):
        ctl = restart_controller()
        self.fold(ctl, echo=False)
        self.assertFalse(ctl.handoff_echoed)
        self.tick(ctl, 90)                          # left no trace; retyped (1/2)
        self.tick(ctl, 150)                          # retyped again (2/2)
        action = self.tick(ctl, 210)                 # the cap is spent
        self.assertEqual(action[0], "notify")
        self.assertIn("never reached the session", action[1])
        self.assertIsNone(ctl.rstate)

    def test_abort_while_accepted_but_not_yet_cleared_is_a_cancel(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl, written_at=40)
        ctl.on_turn("open", 41)                      # holds `_send_clear` back
        self.assertIsNone(self.tick(ctl, 65))
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)

        action = self.tick(ctl, 65 + 901)             # handoff_timeout elapses, still held
        self.assertIsNone(action)
        self.assertEqual(ctl.rstate, cr.CANCEL_PENDING)
        self.assertIn("restart aborted at handoff_ok", ctl.log_lines[-1])

    def test_abort_while_clearing_is_not_a_cancel(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl)
        self.tick(ctl, 65)                            # handoff verified; /clear sent
        self.assertEqual(ctl.rstate, cr.CLEAR_SENT)
        self.assertTrue(ctl.handoff_echoed)            # the fold WAS delivered...

        action = self.tick(ctl, 65 + 901)              # ...but /clear itself never confirms
        self.assertEqual(action[0], "notify")
        self.assertIsNone(ctl.rstate)

    def test_abort_restart_only_treats_handoff_states_as_cancellable(self):
        """CLEARED/UNFOLD_FAILED never reach `_abort_restart` through the
        public state machine at all (T08 resolves them a different way), and
        RESUME_SENT's only abort is `permanent`. None of that is reachable
        from the outside to prove a negative, so the guard is exercised
        directly here instead."""
        for state in (cr.CLEARED, cr.UNFOLD_FAILED, cr.RESUME_SENT):
            ctl = restart_controller()
            ctl.rstate = state
            ctl.handoff_echoed = True
            action = ctl._abort_restart("forced for the test", 100)
            self.assertEqual(action[0], "notify")
            self.assertIsNone(ctl.rstate)


class TestUnfoldCanFail(RestartTestCase):
    """T08: a resume phrase that never once reaches the session is not the
    world's problem to retry forever, but it is not a one-way failure either --
    the context really is gone, so the debt has to stay visible until a human
    does something about it."""

    def test_five_reprints_without_echo_fail_the_unfold_visibly(self):
        ctl = restart_controller()             # CR_RESUME_ATTEMPTS defaults to 5
        self.unfolding(ctl)
        self.assertEqual(ctl.rstate, cr.RESUME_SENT)

        for n in range(1, 6):
            t = ctl.rwake
            action = self.tick(ctl, t)
            self.assertEqual(action, ("inject", RESUME, False))
            self.assertEqual(ctl.resume_tries, n)

        t = ctl.rwake
        action = self.tick(ctl, t)
        self.assertEqual(action[0], "notify")
        self.assertIn("never reached the session", action[1])
        self.assertEqual(ctl.rstate, cr.UNFOLD_FAILED)
        self.assertTrue(ctl.context_off)
        self.assertFalse(ctl.context_enabled)
        self.assertEqual(ctl.badge_warn(), "unfold failed")     # T14: outranks everything

        badge = cr.Badge(dict(badge=1, badge_pos="bottom-right", badge_label="cr"))
        text, sgr = badge.frame(cr.IDLE, 0, 0, 3, now=0, restart=ctl.rstate)
        self.assertIn("unfold failed", text)
        self.assertEqual(sgr, "2;31")               # red, not the blinking magenta

        self.assertIsNone(self.tick(ctl, t + 1))    # too soon to say it again
        again = self.tick(ctl, t + 301)
        self.assertEqual(again[0], "notify")
        self.assertIn("never reached the session", again[1])

        ctl.on_user_bytes(b"\r", now=t + 302)       # any key dismisses the notice
        self.assertIsNone(ctl.rstate)
        self.assertTrue(ctl.context_off)            # ...but the trigger stays off
        # T14: the specific "unfold failed" notice resets, and what is left of
        # the debt (the trigger permanently off) takes its place in the badge.
        self.assertEqual(ctl.badge_warn(), "restart off")
        self.assertIsNone(self.tick(ctl, t + 302 + 400))   # ...and stays quiet: acknowledged


class TestTheLimitOutranksTheContext(RestartTestCase):
    """A session that is out of quota is stopped either way, so there is nothing
    to fold into a file and no point typing at it. A limit that arrives DURING a
    restart is a different thing: it suspends it, never cancels it."""

    def test_a_limit_holds_a_restart_back(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.on_limit(BANNER, now=10, source="transcript")
        self.assertIsNone(self.tick(ctl, 30))
        self.assertIsNone(ctl.rstate)
        self.assertEqual(self.tick(ctl, 7300), ("inject", "continue", False))

    def test_and_the_restart_happens_once_the_limit_is_gone(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.on_limit(BANNER, now=10, source="transcript")
        self.tick(ctl, 7300)
        ctl.on_echo(now=7302)                      # the retry was accepted
        action = self.tick(ctl, 7400)
        self.assertEqual(action[0], "inject")
        self.assertIn("H.md", action[1])

    def test_a_fold_interrupted_by_a_limit_is_re_sent_whole(self):
        # `continue` would be wrong here: the phrase rewrites the file from
        # scratch, so what a half-written handoff needs is the phrase again.
        ctl = restart_controller()
        self.fold(ctl)
        first = ctl.nonce
        ctl.on_limit("resets in 1 hours", now=40, source="transcript")
        self.assertIsNone(self.tick(ctl, 100))     # frozen for the duration
        action = self.tick(ctl, 40 + 3601)
        self.assertEqual(action[0], "inject")
        self.assertIn("H.md", action[1])
        self.assertNotEqual(ctl.nonce, first)
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)
        self.assertEqual(ctl.state, cr.IDLE)

    def test_a_limit_does_not_spend_a_fold_attempt(self):
        # The attempt counter is for a model that will not fold, not for the
        # world getting in the way. Two limits in a row must not exhaust it.
        ctl = restart_controller()
        self.fold(ctl)
        for at in (40, 4000):
            ctl.on_limit("resets in 1 hours", now=at, source="transcript")
            self.tick(ctl, at + 3601)
        self.assertEqual(ctl.handoff_tries, 1)
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)

    def test_the_fold_timeout_does_not_run_while_a_limit_does(self):
        # A weekly limit is days long; a fifteen-minute fold timeout inside one
        # would abort a restart that is going perfectly well.
        ctl = restart_controller(handoff_timeout=60)
        self.fold(ctl)
        ctl.on_limit("resets in 40 hours", now=35, source="transcript")
        for t in range(100, 40 * 3600, 1800):
            self.assertIsNone(self.tick(ctl, t))
        self.assertEqual(ctl.rstate, cr.HANDOFF_SENT)

    def test_a_verified_handoff_survives_a_limit_and_still_clears(self):
        ctl = restart_controller()
        self.fold(ctl)
        self.folded(ctl)
        ctl.on_user_bytes(b"typing", now=64)       # a draft holds the clear back
        self.assertIsNone(self.tick(ctl, 65))
        self.assertEqual(ctl.rstate, cr.HANDOFF_OK)
        ctl.on_limit("resets in 1 hours", now=66, source="transcript")
        self.assertEqual(self.tick(ctl, 66 + 3601), ("inject", "/clear", False))

    def test_a_limit_between_the_clear_and_the_resume_sends_the_resume(self):
        ctl = restart_controller()
        self.cleared(ctl)
        ctl.on_limit("resets in 1 hours", now=66, source="transcript")
        self.assertEqual(self.tick(ctl, 66 + 3601), ("inject", RESUME, False))
        self.assertEqual(ctl.rstate, cr.RESUME_SENT)

    def test_a_resume_that_never_landed_is_sent_again(self):
        ctl = restart_controller()
        self.unfolding(ctl)
        ctl.on_limit("resets in 1 hours", now=70, source="transcript")
        action = self.tick(ctl, 70 + 3601)
        self.assertEqual(action, ("inject", RESUME, False))

    def test_a_resume_that_had_landed_falls_back_to_continue(self):
        # The one position in the machine where "carry on with what you were
        # doing" is exactly the right instruction.
        ctl = restart_controller()
        self.unfolding(ctl)
        ctl.on_resume_echo(70)
        ctl.on_limit("resets in 1 hours", now=71, source="transcript")
        self.assertEqual(self.tick(ctl, 71 + 3601), ("inject", "continue", False))
        self.assertIsNone(ctl.rstate)

    def test_a_wait_that_ends_early_resumes_the_restart_too(self):
        # The quota can come back without the reset it announced, and the only
        # sign is the session answering. The restart still has to go on.
        ctl = restart_controller()
        self.cleared(ctl)
        ctl.on_limit("resets in 40 hours", now=66, source="transcript")
        self.assertTrue(ctl.on_alive(now=200, source="transcript"))
        self.assertEqual(self.tick(ctl, 201), ("inject", RESUME, False))


class TestWhoIsAllowedToBeInterrupted(RestartTestCase):
    def test_it_does_not_fold_over_someone_typing(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.on_user_bytes(b"mid-thought", now=25)
        self.assertIsNone(self.tick(ctl, 30))
        self.assertIsNone(ctl.rstate)
        ctl.on_user_bytes(b"\r", now=26)                   # they send it themselves
        self.assertIsNotNone(self.tick(ctl, 60))

    def test_it_does_not_fold_into_a_running_turn(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.note_growth([MINE], now=25)
        self.assertIsNone(self.tick(ctl, 30))
        self.assertIsNotNone(self.tick(ctl, 50))

    def test_another_sessions_transcript_holds_nothing_back(self):
        # Work running in the background under the same cwd writes its own
        # transcript. It is not this session's turn and must not gate it.
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.note_growth(["/proj/somebody-else.jsonl"], now=29)
        self.assertIsNotNone(self.tick(ctl, 30))

    def test_a_busy_screen_holds_nothing_back(self):
        # In a session that keeps background work going, something is always
        # painting a footer. Waiting for the screen to go quiet would mean
        # waiting for ever, so the restart asks the transcript instead.
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        for t in range(0, 31, 2):
            ctl.on_output("✻ Cogitating… 4m 12s · ↓ 8.1k tokens", now=t)
        self.assertIsNotNone(self.tick(ctl, 30))


class TestWhatCountsAsContext(RestartTestCase):
    def test_the_feature_is_off_unless_it_is_asked_for(self):
        ctl = restart_controller(context_tokens=0)
        usage(ctl, 0, 999999)
        self.assertFalse(ctl.context_enabled)
        self.assertIsNone(self.tick(ctl, 100))

    def test_below_the_threshold_nothing_happens(self):
        ctl = restart_controller()
        usage(ctl, 0, 400000)
        self.assertIsNone(self.tick(ctl, 100))

    def test_a_subagents_context_is_not_the_sessions(self):
        # A subagent writes into the same transcript, and its usage is its own.
        ctl = restart_controller()
        usage(ctl, 0, 100000)
        usage(ctl, 5, 900000, sidechain=True)
        self.assertEqual(ctl.context_tokens, 100000)
        self.assertIsNone(self.tick(ctl, 60))

    def test_a_subagent_still_counts_as_the_session_working(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        usage(ctl, 25, 40000, sidechain=True)
        self.assertIsNone(self.tick(ctl, 30))      # the session is not idle
        self.assertIsNotNone(self.tick(ctl, 50))

    def test_the_model_decides_the_denominator(self):
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)
        self.assertEqual(ctl.context_limit, 500000)

    def test_a_short_window_model_triggers_sooner(self):
        ctl = restart_controller(context_window="auto", context_tokens=0,
                                 context_restart_on=True)
        usage(ctl, 0, 10, model="claude-sonnet-4-5")
        self.assertEqual(ctl.context_limit, 102000)   # the model's own row: 51% of 200k

    def test_an_unfamiliar_model_gets_a_modal_window_estimate(self):
        # The actual bug: assuming the SMALL window put a 1M session on a 200k
        # denominator and folded it at 12% full. Leaving the trigger disarmed
        # instead just traded one failure for another: a new model appears
        # exactly when the table goes stale, and a session with nothing armed
        # dies of its own context instead. T19: guess large (a new slug is
        # almost always a new model) and keep asking the network for
        # something firmer while that guess stands.
        ctl = restart_controller(context_window="auto", context_tokens=0,
                                 context_restart_on=True)
        usage(ctl, 0, 10, model="claude-something-9")
        self.assertEqual(ctl.context_window, 1000000)     # the table's modal window
        self.assertEqual(ctl.context_limit, 510000)   # stage 4: DEFAULT_RESTART_PCT of it
        self.assertTrue(ctl.context_estimated)
        self.assertEqual(ctl.window_unknown, "claude-something-9")   # still worth asking
        # T14: far from the threshold `badge_context()` has nothing to show, so
        # the guess still gets a word of its own rather than going silent.
        self.assertIsNone(ctl.badge_context())
        self.assertEqual(ctl.badge_warn(), "~est")

    def test_an_estimate_keeps_escalating_past_what_usage_proves_it(self):
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="not-a-claude-slug-at-all")
        self.assertEqual(ctl.context_window, 200000)      # nothing claude-shaped to go on
        self.assertTrue(ctl.context_estimated)
        usage(ctl, 5, 250000, model="not-a-claude-slug-at-all")
        self.assertEqual(ctl.context_window, 1000000)
        self.assertTrue(any("past the 200k window assumed" in l for l in ctl.log_lines))
        usage(ctl, 10, 1100000, model="not-a-claude-slug-at-all")
        self.assertEqual(ctl.context_window, 1500000)     # past 1M too: +50%

    def test_an_estimate_that_survives_most_of_itself_stops_being_a_guess(self):
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-something-9")
        self.assertTrue(ctl.context_estimated)
        usage(ctl, 5, 650000, model="claude-something-9")     # past 60% of 1M, no compaction
        self.assertFalse(ctl.context_estimated)
        self.assertTrue(any("no longer marked as a guess" in l for l in ctl.log_lines))

    def test_a_synthetic_row_between_two_real_ones_is_not_a_model_change(self):
        # "<synthetic>" ("No response requested", an interrupted request) sits
        # between two real rows in the transcript. `assistant_row` already
        # reports its model as None, and here that must not read as "the model
        # changed": no `_resolve_window` re-run and no "unfamiliar model slug"
        # log noise over a row that never named a model at all.
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)
        logged_before = len(ctl.log_lines)

        synthetic = cr.assistant_row({"message": {
            "model": "<synthetic>",
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "output_tokens": 0},
            "stop_reason": "stop_sequence"}})
        self.assertIsNone(synthetic["model"])
        ctl.on_context(dict(synthetic, path=MINE), 5)

        self.assertEqual(ctl.context_window, 1000000)      # untouched
        self.assertIsNone(ctl.window_unknown)
        new_logs = ctl.log_lines[logged_before:]
        self.assertFalse(any("unfamiliar model slug" in ln for ln in new_logs))
        self.assertFalse(any("context is" in ln for ln in new_logs))

        usage(ctl, 10, 20, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)

    def test_a_point_release_is_its_familys_window(self):
        # The actual bug: claude-fable-5-1 is a 1M model, the table knew only
        # claude-fable-5, and the difference was a session cleared for nothing.
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-fable-5-1")
        self.assertEqual(ctl.context_window, 1000000)
        self.assertEqual(ctl.context_limit, 500000)
        self.assertIsNone(ctl.window_unknown)

    def test_an_unfamiliar_model_still_folds_on_its_estimate(self):
        # T19: the estimate arms the trigger from the first turn, not just
        # once a lookup answers — the badge shows the guess is unconfirmed
        # (~), it is not left silent as "window?".
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 900000, model="claude-something-9")
        self.assertIsNotNone(self.tick(ctl, 60))
        self.assertIsNone(ctl.badge_warn())

    def test_a_confirmed_answer_replaces_the_estimate(self):
        ctl = restart_controller(context_window="auto", context_tokens=0,
                                 context_restart_on=True)
        usage(ctl, 0, 10, model="claude-something-9")
        self.assertTrue(ctl.context_estimated)
        self.assertEqual(ctl.context_window, 1000000)      # the modal-window guess
        self.assertEqual(ctl.window_unknown, "claude-something-9")
        self.assertTrue(ctl.on_window_learned("claude-something-9", 2000000,
                                              "the models docs"))
        self.assertEqual(ctl.context_window, 2000000)
        self.assertEqual(ctl.context_limit, 1020000)   # stage 4: 51% of the learned window
        self.assertFalse(ctl.context_estimated)
        self.assertIsNone(ctl.window_unknown)
        self.assertIsNone(ctl.badge_warn())

    def test_an_answer_about_another_model_is_kept_but_not_applied(self):
        # A lookup takes seconds and a session can change model inside them.
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-something-9")
        self.assertEqual(ctl.context_window, 1000000)      # the estimate, not None
        self.assertFalse(ctl.on_window_learned("claude-other-9", 300000, "docs"))
        self.assertEqual(ctl.context_window, 1000000)      # unaffected either way
        usage(ctl, 5, 10, model="claude-other-9")
        self.assertEqual(ctl.context_window, 300000)

    def test_an_absolute_threshold_needs_no_window_at_all(self):
        ctl = restart_controller(context_window="auto", context_tokens=50000)
        usage(ctl, 0, 60000, model="claude-something-9")
        self.assertIsNone(ctl.window_unknown)
        self.assertIsNone(ctl.badge_warn())
        self.assertIsNotNone(self.tick(ctl, 60))

    def test_an_explicit_window_wins(self):
        ctl = restart_controller(context_window="300k")
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 300000)

    def test_claude_codes_own_switch_narrows_it(self):
        ctl = restart_controller(context_window="auto", context_no_1m=True)
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 200000)

    def test_claude_codes_own_ceiling_is_honoured(self):
        ctl = restart_controller(context_window="auto", context_env_max=150000)
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 150000)

    def test_a_window_proved_too_small_is_raised(self):
        # The table can be right about the model and wrong about this session:
        # a 200k model served with a longer window still writes 200k's slug.
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-4-5")
        self.assertEqual(ctl.context_window, 200000)
        usage(ctl, 5, 260000, model="claude-opus-4-5")
        self.assertEqual(ctl.context_window, 1000000)
        self.assertEqual(ctl.context_limit, 500000)

    def test_an_explicit_window_is_not_second_guessed(self):
        ctl = restart_controller(context_window="200k")
        usage(ctl, 0, 260000, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 200000)

    def test_an_absolute_threshold_ignores_the_window_entirely(self):
        ctl = restart_controller(context_tokens=123456)
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertTrue(ctl.context_enabled)
        self.assertEqual(ctl.context_limit, 123456)

    def test_the_session_moving_to_a_new_transcript_replaces_the_reading(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL, path=MINE)
        moved(ctl, "/proj/after.jsonl", 1)
        usage(ctl, 1, 9000, path="/proj/after.jsonl")
        self.assertEqual(ctl.context_tokens, 9000)

    def test_a_row_from_a_transcript_never_bound_to_is_ignored(self):
        # T04: only `bind_transcript` may move `context_path`. A row that
        # simply arrives on a different file — a neighbour's session, not a
        # `/clear` — is not taken as one.
        ctl = restart_controller()
        usage(ctl, 0, FULL, path=MINE)
        usage(ctl, 1, 9000, path="/proj/somebody-else.jsonl")
        self.assertEqual(ctl.context_tokens, FULL)
        self.assertEqual(ctl.context_path, MINE)


class TestSelfCompactionCorrectsTheWindowDownward(RestartTestCase):
    """T19: the agent compacting on its own is evidence, not a guess — the
    session just told us its effective window is no bigger than what it held
    right before folding itself."""

    def test_a_compact_boundary_shrinks_an_assumed_1m_window(self):
        ctl = restart_controller(context_window="auto", context_tokens=0,
                                 context_restart_on=True)
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)
        ctl.on_context(dict(kind="alive", path=MINE, quiet=True, sidechain=False,
                            compacted=True, pre_tokens=190000), 5)
        self.assertEqual(ctl.context_window, 206521)          # 190000 / 0.92
        # stage 4: the shrunk window no longer matches claude-opus-5's own row
        # (1M), so DEFAULT_RESTART_PCT applies to the corrected window instead.
        self.assertEqual(ctl.context_limit, 105325)
        self.assertFalse(ctl.context_estimated)                # observed, not guessed
        self.assertTrue(any("the effective window is" in l for l in ctl.log_lines))

    def test_a_compaction_right_at_the_windows_own_edge_proves_nothing(self):
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5")
        ctl.on_context(dict(kind="alive", path=MINE, quiet=True, sidechain=False,
                            compacted=True, pre_tokens=950000), 5)
        self.assertEqual(ctl.context_window, 1000000)          # untouched: 950k / 0.92 > 1M

    def test_codexs_own_compaction_still_uses_the_last_known_count(self):
        # codex's own `compacted` row carries no preTokens of its own; the
        # count this session last reported stands in, same as before T19.
        ctl = restart_controller(agent="codex", context_window="auto", context_restart_on=True)
        ctl.on_context(dict(kind="alive", path=MINE, sidechain=False,
                            window=258400, tokens=200000), 0)
        ctl.on_context(dict(kind="alive", path=MINE, quiet=True, sidechain=False,
                            compacted=True), 1)
        self.assertEqual(ctl.context_window, int(200000 / 0.92))
        self.assertEqual(ctl.self_compactions, 1)


class TestAPerModelTokenOverride(RestartTestCase):
    """CR_CLAUDE_TOKENS_<SLUG>/CR_CODEX_TOKENS_<SLUG>: one specific model, one
    absolute number, read live because the model is not known until its first
    turn — everything else in `cfg` is fixed before that."""

    ENV = "CR_CLAUDE_TOKENS_CLAUDE_OPUS_5"

    def setUp(self):
        super().setUp()
        self.addCleanup(os.environ.pop, self.ENV, None)

    def test_it_wins_over_the_percentage(self):
        os.environ[self.ENV] = "300000"
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)   # the window is untouched
        self.assertEqual(ctl.context_limit, 300000)      # only the threshold moves

    def test_it_wins_over_an_absolute_threshold_too(self):
        os.environ[self.ENV] = "300000"
        ctl = restart_controller(context_tokens=999999)
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_limit, 300000)

    def test_it_reaches_through_a_1m_suffix_and_a_dated_snapshot(self):
        # model_slug() is what both sides key on: "claude-opus-5[1m]" and
        # "claude-opus-5-20260101" are this override's model too.
        os.environ[self.ENV] = "300000"
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5[1m]")
        self.assertEqual(ctl.context_limit, 300000)

    def test_it_only_applies_to_the_model_it_names(self):
        os.environ[self.ENV] = "300000"
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-sonnet-5")
        self.assertNotEqual(ctl.context_limit, 300000)
        self.assertEqual(ctl.context_limit, 500000)

    def test_a_codex_override_does_not_touch_claude(self):
        os.environ["CR_CODEX_TOKENS_CLAUDE_OPUS_5"] = "300000"
        self.addCleanup(os.environ.pop, "CR_CODEX_TOKENS_CLAUDE_OPUS_5", None)
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_limit, 500000)

    def test_unset_falls_back_to_the_percentage(self):
        ctl = restart_controller(context_window="auto")
        usage(ctl, 0, 10, model="claude-opus-5")
        self.assertEqual(ctl.context_limit, 500000)

    def test_the_env_slug_matches_a_dotted_hyphenated_name(self):
        self.assertEqual(cr.model_env_slug("gpt-5.6-sol"), "GPT_5_6_SOL")
        self.assertEqual(cr.model_env_slug("claude-opus-5"), "CLAUDE_OPUS_5")


class TestModelSwitchRealtime(RestartTestCase):
    """T21: switching models mid-session, in real time -- before an assistant
    row ever confirms the new one, and folding immediately when a downward
    switch is already past its (smaller) threshold."""

    def _armed(self, **over):
        return restart_controller(context_tokens=0,
                                  context_restart_on=True, context_window="auto",
                                  **over)

    def test_downward_switch_folds_on_the_very_next_tick(self):
        ctl = self._armed()
        usage(ctl, 0, 150000, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)
        self.assertEqual(ctl.context_limit, 510000)
        self.assertIsNone(self.tick(ctl, 30))      # nowhere near opus's own threshold
        logged_before = len(ctl.log_lines)
        self.assertTrue(ctl.on_model("claude-haiku-4-5", "assistant line", 31))
        self.assertEqual(ctl.context_window, 200000)
        self.assertEqual(ctl.context_limit, 102000)
        switched = ctl.log_lines[logged_before:]
        self.assertTrue(any("model switched: claude-opus-5" in l
                            and "claude-haiku-4-5" in l and "[assistant line]" in l
                            for l in switched), switched)
        # 150000 tokens is already well past haiku's 102k threshold -- the
        # fold has to go out on the very next check, not the next transcript row.
        action = self.tick(ctl, 32)
        self.assertEqual(action[0], "inject")

    def test_there_and_back_restores_the_limit_and_the_bump_flag(self):
        ctl = self._armed()
        usage(ctl, 0, 10, model="claude-opus-5")
        ctl.learned["claude-some-other-model"] = 700000   # unrelated to either side
        ctl.on_model("claude-haiku-4-5", "assistant line", 1)
        self.assertEqual(ctl.context_window, 200000)
        self.assertEqual(ctl.context_limit, 102000)
        ctl._window_bumped = True      # simulate a bump that happened under haiku
        ctl.on_model("claude-opus-5", "assistant line", 2)
        self.assertEqual(ctl.context_window, 1000000)
        self.assertEqual(ctl.context_limit, 510000)
        self.assertFalse(ctl._window_bumped)
        self.assertEqual(ctl.learned["claude-some-other-model"], 700000)

    def test_a_per_model_override_reaches_the_model_switched_to(self):
        env = "CR_CLAUDE_TOKENS_CLAUDE_HAIKU_4_5"
        os.environ[env] = "90000"
        self.addCleanup(os.environ.pop, env, None)
        ctl = self._armed()
        usage(ctl, 0, 10, model="claude-opus-5")
        ctl.on_model("claude-haiku-4-5", "assistant line", 1)
        self.assertEqual(ctl.context_limit, 90000)

    def test_no_op_when_the_slug_has_not_actually_changed(self):
        ctl = self._armed()
        usage(ctl, 0, 10, model="claude-opus-5")
        logged_before = len(ctl.log_lines)
        self.assertFalse(ctl.on_model("claude-opus-5-20260101", "assistant line", 1))
        self.assertEqual(len(ctl.log_lines), logged_before)

    def _local_command_hint(self, display):
        """A "Set model to <display>" row the way `transcript_limit_records`
        reports it. A preceding <command-name>/model</command-name> row is
        included for realism but never inspected: the stdout line already
        names the switch unambiguously."""
        tmpdir = tempfile.mkdtemp(prefix="cr-t21-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "s.jsonl")
        rows = [
            {"type": "user", "isSidechain": False,
             "message": {"role": "user",
                        "content": "<command-name>/model</command-name>\n"
                                   "<command-message>model</command-message>\n"
                                   "<command-args></command-args>"}},
            {"type": "user", "isSidechain": False,
             "message": {"role": "user", "content": [
                 {"type": "text",
                  "text": "<local-command-stdout>Set model to %s</local-command-stdout>"
                          % display}]}},
        ]
        with open(path, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        _, recs = cr.transcript_limit_records(path, 0)
        return recs

    def test_the_local_command_line_is_parsed_into_a_hint(self):
        recs = self._local_command_hint("Haiku 4.5")
        hints = [r["model_hint"] for r in recs if r.get("model_hint")]
        self.assertEqual(hints, ["claude-haiku-4-5"])

    def test_a_slug_named_outright_in_parens_is_trusted_over_the_display(self):
        recs = self._local_command_hint("Some New Thing (claude-opus-5)")
        hints = [r["model_hint"] for r in recs if r.get("model_hint")]
        self.assertEqual(hints, ["claude-opus-5"])

    def test_local_command_hint_switches_early_but_the_assistant_line_wins(self):
        ctl = self._armed()
        usage(ctl, 0, 50000, model="claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)
        for rec in self._local_command_hint("Haiku 4.5"):
            ctl.on_context(dict(rec, path=MINE), 1)
        self.assertEqual(ctl.context_window, 200000)
        self.assertEqual(ctl.context_limit, 102000)
        self.assertTrue(any("model switched: claude-opus-5" in l
                            and "claude-haiku-4-5" in l and "[local command]" in l
                            for l in ctl.log_lines))
        self.assertIsNone(self.tick(ctl, 30))      # 50000 tokens folds nothing yet
        logged_before = len(ctl.log_lines)
        usage(ctl, 31, 60000, model="claude-opus-5")    # the user changed their mind
        self.assertEqual(ctl.context_window, 1000000)
        self.assertEqual(ctl.context_limit, 510000)
        switched = ctl.log_lines[logged_before:]
        self.assertTrue(any("model switched: claude-haiku-4-5" in l
                            and "claude-opus-5" in l and "[assistant line]" in l
                            for l in switched), switched)
        self.assertIsNone(self.tick(ctl, 32))      # never folded on the transient hint


class TestClaudesOwnStatusLine(RestartTestCase):
    """`on_status` (T20): the `--cr-statusline` proxy's own signal, filed
    under the same `context_window_hint` codex's rollout accounting already
    uses -- the fix for a 200k session that assumed 1M (from the model's own
    profile table) and let Claude Code self-compact around ~190k instead of
    the wrapper ever restarting it.
    """

    def test_a_200k_report_overrides_the_1m_model_table(self):
        # claude-sonnet-5 is a _BIG_CLAUDE (1M) model in the profile table;
        # the statusline is the one thing that can prove THIS session did not
        # actually get that window.
        ctl = restart_controller(context_window="auto", context_tokens=0,
                                 context_restart_on=True)
        ctl.on_status(dict(context_window_size=200000, model_id="claude-sonnet-5"), 0)
        self.assertEqual(ctl.context_window, 200000)
        self.assertEqual(ctl.context_limit, 102000)   # stage 4: 51% of this session's actual window
        self.assertTrue(any("claude's status line" in l for l in ctl.log_lines),
                        ctl.log_lines)

    def test_the_model_id_moves_context_model_inline(self):
        # T21 will add a dedicated on_model(); until it merges this is
        # handled inline, mirroring on_context's own model-change branch.
        ctl = restart_controller(context_window="auto")
        ctl.on_status(dict(model_id="claude-opus-5"), 0)
        self.assertEqual(ctl.context_model, "claude-opus-5")
        self.assertEqual(ctl.context_window, 1000000)   # the profile table, by the new model

    def test_a_repeated_report_does_not_re_resolve(self):
        ctl = restart_controller(context_window="auto", context_tokens=0,
                                 context_restart_on=True)
        ctl.on_status(dict(context_window_size=200000, model_id="claude-sonnet-5"), 0)
        before = len(ctl.log_lines)
        ctl.on_status(dict(context_window_size=200000, model_id="claude-sonnet-5"), 5)
        self.assertEqual(len(ctl.log_lines), before)

    def test_codexs_own_rollout_label_is_unaffected(self):
        # This is a claude-only signal; codex's own accounting must keep
        # saying "the transcript", never relabelled by anything on_status does.
        ctl = restart_controller(agent="codex", context_window="auto", context_restart_on=True)
        ctl.on_context(dict(kind="alive", path=MINE, sidechain=False,
                            tokens=10, model="gpt-5", window=400000), 0)
        self.assertTrue(any("(the transcript)" in l for l in ctl.log_lines), ctl.log_lines)

    def test_session_id_is_filed_but_does_not_bind_the_transcript(self):
        # A secondary, corroborating signal at most -- T02's own
        # ClaudeSessionRegistry binding is untouched by it.
        ctl = restart_controller(context_window="auto")
        ctl.on_status(dict(session_id="abc123"), 0)
        self.assertEqual(ctl.status_session_id, "abc123")
        self.assertIsNone(ctl.context_path)

    def test_a_missing_field_is_simply_not_acted_on(self):
        ctl = restart_controller(context_window="auto")
        ctl.on_status(dict(), 0)
        self.assertIsNone(ctl.context_window_hint)
        self.assertIsNone(ctl.context_model)


class TestSettingsAsPeopleWriteThem(unittest.TestCase):
    """The thresholds are typed by hand into a shell, and a value that fails to
    parse falls back on the default — which for this feature is "off". Silently
    off is the one outcome nobody would ever notice."""

    def test_an_absolute_threshold_may_be_written_the_short_way(self):
        from helper import load as reload_impl
        for written, want in (("500k", 500000), ("1M", 1000000), ("450000", 450000)):
            mod = reload_impl(CR_CONTEXT_TOKENS=written)
            self.assertEqual(mod.CFG["context_tokens"], want, written)

    def test_nonsense_is_not_a_threshold(self):
        from helper import load as reload_impl
        self.assertEqual(reload_impl(CR_CONTEXT_TOKENS="lots").CFG["context_tokens"], 0)

    def test_context_restart_is_off_by_default(self):
        from helper import load as reload_impl
        self.assertIs(reload_impl().CFG["context_restart_on"], False)

    def test_context_restart_flag_arms_the_profile_not_a_percentage(self):
        # T18: the flag used to default CFG["context_pct"] to DEFAULT_RESTART_PCT
        # outright. Now it just sets context_restart_on, and it is
        # model_restart_at (via MODEL_PROFILES) that answers with a number —
        # see test_models.py's TestTheThresholdResolutionOrder for that.
        from helper import load as reload_impl
        mod = reload_impl(CR_CONTEXT_RESTART="1")
        self.assertTrue(mod.CFG["context_restart_on"])
        self.assertEqual(mod.DEFAULT_RESTART_PCT, 51.0)   # still the number the table bakes in

    def test_an_explicit_token_threshold_still_wins_over_the_flag(self):
        from helper import load as reload_impl
        mod = reload_impl(CR_CONTEXT_RESTART="1", CR_CONTEXT_TOKENS="300000")
        self.assertEqual(mod.CFG["context_tokens"], 300000)
        self.assertTrue(mod.CFG["context_restart_on"])


class TestWhatTheCornerSays(RestartTestCase):
    def test_nothing_until_the_threshold_is_in_sight(self):
        ctl = restart_controller()
        usage(ctl, 0, 100000)
        self.assertIsNone(ctl.badge_context())

    def test_the_percentage_once_it_is(self):
        ctl = restart_controller()
        usage(ctl, 0, 450000)                      # 90% of the way to 500k
        self.assertAlmostEqual(ctl.badge_context(), 45.0, delta=0.1)

    def test_nothing_at_all_when_the_feature_is_off(self):
        ctl = restart_controller(context_tokens=0)
        usage(ctl, 0, 999999)
        self.assertIsNone(ctl.badge_context())

    def test_the_restart_names_the_step_it_is_on(self):
        badge = cr.Badge(dict(badge=1, badge_pos="bottom-right", badge_label="cr"))
        text, _ = badge.frame(cr.IDLE, 0, 0, 3, now=0, restart=cr.HANDOFF_SENT)
        self.assertIn("folding", text)
        text, _ = badge.frame(cr.IDLE, 0, 0, 3, now=0, context=47.0)
        self.assertIn("47%", text)

    def test_a_wait_still_outranks_it_on_screen(self):
        badge = cr.Badge(dict(badge=1, badge_pos="bottom-right", badge_label="cr"))
        text, _ = badge.frame(cr.WAITING, 3600, 0, 3, now=0, restart=cr.HANDOFF_SENT)
        self.assertIn("1h00m", text)


class TestBindTranscriptIsTheSoleSwitchPoint(RestartTestCase):
    """T04: everything `on_context` used to reset by itself on a path change —
    plus the fields it never did — now resets exactly once, exactly here."""

    def test_binding_resets_every_per_transcript_field_but_the_model(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL, model="claude-opus-5", stop="tool_use")
        ctl.on_turn("open", 0)
        ctl.note_growth([MINE], 0)
        ctl._window_bumped = True
        ctl.codex_cap = 900000
        ctl.counted_by_log = MINE
        moved(ctl, "/proj/after.jsonl", 10)
        self.assertIsNone(ctl.context_tokens)
        self.assertIsNone(ctl.turn_open)
        self.assertIsNone(ctl.codex_cap)
        self.assertIsNone(ctl.context_window_hint)
        self.assertIsNone(ctl.counted_by_log)
        self.assertIsNone(ctl.last_stop_reason)
        self.assertEqual(ctl.context_grew_at, 0.0)
        self.assertFalse(ctl._window_bumped)
        self.assertEqual(ctl.context_model, "claude-opus-5")   # the one survivor

    def test_the_first_row_after_binding_reads_correctly(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        moved(ctl, "/proj/after.jsonl", 10)
        usage(ctl, 10, 8000, path="/proj/after.jsonl")
        self.assertEqual(ctl.context_tokens, 8000)
        self.assertAlmostEqual(ctl.context_pct(), 0.8, delta=0.1)

    def test_binding_logs_exactly_one_line(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        before = len(ctl.log_lines)
        moved(ctl, "/proj/after.jsonl", 10)
        switched = [l for l in ctl.log_lines[before:] if "transcript switched" in l]
        self.assertEqual(len(switched), 1)
        self.assertIn("mine.jsonl", switched[0])
        self.assertIn("after.jsonl", switched[0])

    def test_a_context_window_flip_flop_does_not_repeat_the_cap_line(self):
        # The bug the old self-adopting on_context had: a row from any path
        # different from context_path reset context_window_hint, and codex's
        # own compaction line (_note_cap) re-fired on every such flip.
        ctl = restart_controller(agent="codex")
        ctl.on_context(dict(kind="alive", source="log", path=MINE, sidechain=False,
                            tokens=100000, cap=900000), 0)
        before = len(ctl.log_lines)
        ctl.on_context(dict(kind="alive", source="log", path=MINE, sidechain=False,
                            tokens=110000, cap=900000), 1)
        self.assertFalse(any("codex compacts this thread" in l for l in ctl.log_lines[before:]))


class TestOnAliveTrustsOnlyItsOwnTranscript(RestartTestCase):
    """T04 item 3: clearing a wait from a transcript row requires the row's
    path to be the one this session is bound to."""

    def test_a_foreign_path_does_not_clear_the_wait(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)                       # binds context_path to MINE
        ctl.on_limit(BANNER, now=10, source="transcript")
        self.assertFalse(ctl.on_alive(now=1000, source="transcript",
                                      path="/proj/somebody-else.jsonl"))
        self.assertEqual(ctl.state, cr.WAITING)

    def test_our_own_path_clears_the_wait(self):
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.on_limit(BANNER, now=10, source="transcript")
        self.assertTrue(ctl.on_alive(now=1000, source="transcript", path=MINE))
        self.assertEqual(ctl.state, cr.IDLE)

    def test_no_path_at_all_is_trusted_as_before(self):
        # A screen-sourced wake, or a caller with nothing to say about paths,
        # is unaffected: only a path that actively disagrees is rejected.
        ctl = restart_controller()
        usage(ctl, 0, FULL)
        ctl.on_limit(BANNER, now=10, source="transcript")
        self.assertTrue(ctl.on_alive(now=1000, source="transcript"))


class TestTheFallbackHeuristicStaysVisible(unittest.TestCase):
    """T04 item 4: once a registry binds identity, `_pick_current`'s growth
    guess only ever runs as a fallback — and when it does, every switch it
    makes is logged, since a wrong guess is least expected right there."""

    def test_a_switch_without_a_registry_is_logged(self):
        cr_ = load()
        logs = []
        watcher = cr_.TranscriptWatcher.__new__(cr_.TranscriptWatcher)
        watcher.log = logs.append
        watcher.current = "/proj/a.jsonl"
        watcher.grown = ["/proj/b.jsonl"]
        watcher.preexisting = {"/proj/a.jsonl"}
        watcher._demoted = set()
        watcher._mtime = lambda p: {"/proj/b.jsonl": 2.0}.get(p, 0.0)
        watcher._pick_current()
        self.assertEqual(watcher.current, "/proj/b.jsonl")
        self.assertTrue(any("fallback heuristic" in l for l in logs))
        self.assertTrue(any("a.jsonl" in l and "b.jsonl" in l for l in logs))

    def test_the_first_pick_is_not_a_switch(self):
        cr_ = load()
        logs = []
        watcher = cr_.TranscriptWatcher.__new__(cr_.TranscriptWatcher)
        watcher.log = logs.append
        watcher.current = None
        watcher.grown = ["/proj/a.jsonl"]
        watcher.preexisting = set()
        watcher._demoted = set()
        watcher._mtime = lambda p: 1.0
        watcher._pick_current()
        self.assertEqual(watcher.current, "/proj/a.jsonl")
        self.assertFalse(logs)


# --------------------------------------------------------------------------- #
# T05: a unique handoff file per session
# --------------------------------------------------------------------------- #
class TestTheIdPlaceholder(unittest.TestCase):
    """`{id}` in CR_HANDOFF_FILE makes a shared path unique per session without
    any registry involved — the controller resolves it on its own."""

    def test_it_is_substituted_in_both_the_path_and_the_resume_text(self):
        ctl = restart_controller(handoff_file="scratchpad/RESUME-{id}.md")
        base = os.path.basename(ctl.handoff_path)
        self.assertRegex(base, r"^RESUME-[0-9a-f]{8}\.md$")
        self.assertNotIn("{id}", ctl.handoff_path)
        self.assertIn(base, ctl.resume_text)

    def test_a_caller_supplied_id_is_used_verbatim(self):
        ctl = restart_controller(handoff_file="R-{id}.md", session_id="deadbeef")
        self.assertTrue(ctl.handoff_path.endswith("R-deadbeef.md"), ctl.handoff_path)

    def test_a_plain_path_is_left_untouched(self):
        ctl = restart_controller(handoff_file="H.md")
        self.assertTrue(ctl.handoff_path.endswith("H.md"), ctl.handoff_path)


class TestHandoffRegistry(unittest.TestCase):
    """`{id}`-free paths rely on `~/.agent-retrier/sessions/<pid>.json`
    (`CR_HANDOFF_REGISTRY_DIR`) to notice a live collision and move aside."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cr-handoff-reg-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.registry = cr.HandoffRegistry(self.dir)

    def _alive_pid(self):
        """A real, live pid this test controls and reaps on cleanup."""
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        self.addCleanup(self._kill, pid)
        return pid

    @staticmethod
    def _kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_a_second_claim_on_the_same_path_is_suffixed_and_logged(self):
        holder = self._alive_pid()
        logs = []
        first = self.registry.claim("H.md", "aaaa1111", holder, "/proj", "claude", 0, logs.append)
        self.assertEqual(first, "H.md")
        second = self.registry.claim("H.md", "bbbb2222", os.getpid(), "/proj", "claude", 0,
                                     logs.append)
        self.assertEqual(second, "H-bbbb2222.md")
        self.assertTrue(any("is taken by pid %d" % holder in ln for ln in logs), logs)

    def test_an_id_path_never_touches_the_registry(self):
        logs = []
        first = self.registry.claim("H-{id}.md", "aaaa1111", 111111, "/proj", "claude", 0,
                                    logs.append)
        second = self.registry.claim("H-{id}.md", "bbbb2222", 222222, "/proj", "claude", 0,
                                     logs.append)
        self.assertEqual(first, "H-aaaa1111.md")
        self.assertEqual(second, "H-bbbb2222.md")
        self.assertEqual(logs, [])

    def test_a_dead_pids_entry_is_not_a_conflict_and_is_removed(self):
        dead = self._alive_pid()
        self._kill(dead)
        self.registry.register(dead, "/proj", os.path.abspath("H.md"), "claude", 0)
        entry = os.path.join(self.dir, "%d.json" % dead)
        self.assertTrue(os.path.exists(entry))
        conflict = self.registry.find_conflict(os.path.abspath("H.md"), exclude_pid=os.getpid())
        self.assertIsNone(conflict)
        self.assertFalse(os.path.exists(entry))

    def test_unregister_removes_the_entry(self):
        self.registry.register(os.getpid(), "/proj", "/proj/H.md", "claude", 0)
        self.registry.unregister(os.getpid())
        self.assertEqual(os.listdir(self.dir), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
