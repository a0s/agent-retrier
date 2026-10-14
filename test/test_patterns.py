"""Detection: which renders are a live usage limit, and which only look like one.

The positive cases include every banner wording recovered from real transcripts
on this machine; the negative cases are the false positives that upstream
claude-auto-retry shipped and then had to fix (issues #63, #15, #38, #19).
"""
import os
import re
import unittest

import fake_claude
from helper import load

cr = load()


LIVE_BANNERS = [
    # --- verbatim from ~/.claude/projects transcripts ---
    "You've hit your session limit · resets 3:20am (Europe/Warsaw)",
    "You've hit your session limit · resets 1:30pm (Europe/Warsaw)",
    "You've hit your session limit · resets 9pm (Europe/Warsaw)",
    "You've hit your weekly limit · resets 4pm (Europe/Warsaw)",
    "You've hit your weekly limit · resets Jul 22 at 6am (Europe/Warsaw)",
    # --- wordings reported in upstream issues ---
    "You've hit your limit · resets 4am (America/Buenos_Aires)",
    "You've hit your session limit · resets 4:50pm (Asia/Shanghai)",
    "⎿  You've hit your limit · resets 4am (America/Buenos_Aires)",
    "Claude usage limit reached. Resets at 2pm",
    "5-hour limit reached · resets 3pm (UTC)",
    # --- plausible variants the pattern table is meant to absorb ---
    "You have reached your daily limit. Try again in 3 hours.",
    "Usage limit reached — available again at 18:00 (UTC)",
    "Rate limit exceeded. Retry-After: 3600",
    "You've exceeded your monthly limit, resets on Aug 1",
    "Too many requests. try again in 15 minutes",
    "quota exceeded · resets tomorrow at 9am",
]

# A two-line render: the banner and its reset time arrive on separate rows.
MULTILINE = """\
⚠ You've hit your session limit
· resets 3pm (UTC)
"""

# The banner pushed far up the pane by a task widget — upstream #38, where a
# fixed 12-line tail window scrolled right past it.
BURIED = "You've hit your session limit · resets 4:40pm (UTC)\n" + "\n".join(
    "  ☐ task item %d" % i for i in range(90))

NOT_LIMITS = [
    # #63: banner text quoted inside a tool call is text ABOUT a limit.
    '● Bash(grep -c "5-hour limit reached - resets 3pm (UTC)" ~/logs/x.log)\n  ⎿  3',
    '⏺ Read(notes.md)\n  ⎿  You\'ve hit your session limit · resets 3pm (UTC)',
    # The API-429 render, which explicitly is NOT a usage limit.
    "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
    # The soft warning, which does not stop the session.
    "Approaching your session limit · resets 3pm (UTC)",
    # A limit line with no reset time: prose, not a banner.
    "You've hit your session limit before, so I added a retry.",
    "Let me explain how the usage limit works in Claude Code.",
    # A reset time with no limit line.
    "The cache resets at 3pm every day.",
    # This tool's own source and docs.
    "CR_LIMIT_PATTERNS=( \"you've hit your session limit\" ) # resets 3pm",
]


class TestBannerDetection(unittest.TestCase):
    def test_live_banners_detected(self):
        for text in LIVE_BANNERS:
            with self.subTest(text=text):
                self.assertIsNotNone(cr.find_limit(text))

    def test_multiline_render(self):
        found = cr.find_limit(MULTILINE)
        self.assertIsNotNone(found)
        self.assertIn("resets 3pm", found)

    def test_banner_buried_under_a_tall_widget(self):
        # No fixed tail window here: the whole rolling buffer is searched, so the
        # distance between the banner and the bottom of the screen is irrelevant.
        self.assertIsNotNone(cr.find_limit(BURIED))

    def test_non_limits_rejected(self):
        for text in NOT_LIMITS:
            with self.subTest(text=text):
                self.assertIsNone(cr.find_limit(text))

    def test_freshest_banner_wins(self):
        pane = ("You've hit your session limit · resets 11:30am (UTC)\n"
                "...work...\n"
                "You've hit your session limit · resets 4:30pm (UTC)\n")
        self.assertIn("4:30pm", cr.find_limit(pane))

    def test_ansi_is_stripped_before_matching(self):
        colored = "\x1b[1m\x1b[31mYou've hit your session limit\x1b[0m \x1b[2m· resets 3pm (UTC)\x1b[0m"
        self.assertIsNotNone(cr.find_limit(colored))

    def test_osc_hyperlink_does_not_break_matching(self):
        text = "You've hit your session limit \x1b]8;;https://claude.ai/upgrade\x1b\\· resets 3pm\x1b]8;;\x1b\\"
        self.assertIsNotNone(cr.find_limit(text))


class TestScreenRows(unittest.TestCase):
    """A TUI paints rows by moving the cursor, not by printing newlines.

    Captured from `claude agents` in a pty: the whole screen arrived with zero
    "\\n" in it. Flattened to one line, the roster's oldest card supplied the
    reset time for another card's limit wording, and the session was parked for
    six hours over a banner that was three hours old.
    """

    def test_cursor_motion_becomes_a_line_break(self):
        screen = ("\x1b[1;1HYou've hit your session limit"
                  "\x1b[2;1H· resets 3pm (UTC)")
        self.assertEqual(cr.strip_ansi(screen).split("\n")[-2:],
                         ["You've hit your session limit", "· resets 3pm (UTC)"])

    def test_a_carriage_return_starts_a_row(self):
        self.assertEqual(cr.strip_ansi("first\rsecond").split("\n"), ["first", "second"])

    def test_cells_from_different_rows_do_not_pair(self):
        # Two unrelated cards, each complete on its own row: a limit with no
        # reset time next to a reset time with no limit is not a banner.
        screen = ("\x1b[3;1H∙ some agent   You've hit your session limit  3h"
                  "\x1b[4;1H∙ other agent  resets 9:30pm (Europe/Warsaw)  1h")
        rows = cr.strip_ansi(screen).split("\n")
        self.assertEqual(len([r for r in rows if r.strip()]), 2)


class TestAgentRoster(unittest.TestCase):
    """`claude agents` lists OTHER sessions; none of it is this terminal's state."""

    # Trimmed from the real render that parked a session until 9:30pm.
    ROSTER = (
        "\x1b[1;1H▐▛███▜▌Claude Code v2.1.222  ~/src/app"
        "\x1b[3;1H1 awaiting input · 1 working · 6 completed"
        "\x1b[5;1H∙ color matching tool seo   rate limited — wait and retry…  2h"
        "\x1b[6;1H∙ close the debts D9, D15…  You've hit your session limit · resets 9:30pm (Eur…  3h"
        "\x1b[8;1H❯ describe a task for a new session"
    )

    def test_a_card_is_not_this_session_s_banner(self):
        self.assertIsNone(cr.find_limit(self.ROSTER))

    def test_the_roster_is_recognised(self):
        self.assertTrue(cr.is_roster(cr.strip_ansi(self.ROSTER)))

    def test_an_ordinary_session_is_not_a_roster(self):
        for text in ["You've hit your session limit · resets 3pm (UTC)",
                     "Waiting for 2 background agents to finish",
                     "● Task(3 completed)"]:
            with self.subTest(text=text):
                self.assertFalse(cr.is_roster(text))


class TestWrappingTheRosterItself(unittest.TestCase):
    """`claude agents` is not a session, so its screen is never ours to read.

    Recognising the roster by what is drawn only holds while the roster is what
    is drawn. Opening a card scrolls THAT session's history past the scraper —
    banners and all — and the header the check keys on is gone by then. A
    neighbour's "resets 2:10am" was read as this terminal's own and the badge
    counted down 11h18m while the limit it was actually under lifted in seven
    minutes. Knowing the subcommand at launch settles it once for the run.
    """

    def test_the_subcommand_is_recognised(self):
        self.assertTrue(cr.is_roster_launch(["agents"]))
        self.assertTrue(cr.is_roster_launch(["--debug", "agents"]))

    def test_an_ordinary_session_is_not(self):
        for argv in ([], ["--continue"], ["attach", "b4f68d7e"], ["-p", "hi"]):
            with self.subTest(argv=argv):
                self.assertFalse(cr.is_roster_launch(argv))

    def test_a_prompt_that_says_agents_is_not_the_subcommand(self):
        # `claude "write me a skill for agents"` starts a session like any other.
        self.assertFalse(cr.is_roster_launch(["write me a skill for agents"]))
        self.assertFalse(cr.is_roster_launch(["fix the tests", "agents"]))


class TestWhatAnAttachNames(unittest.TestCase):
    """`claude attach <id|name>` names its session on our own command line
    (T33): the one word after `attach`, an id prefix or a name."""

    def test_the_word_after_attach(self):
        self.assertEqual(cr.attach_target(["attach", "6cb059d2"]), "6cb059d2")
        self.assertEqual(cr.attach_target(["--debug", "attach", "6cb"]), "6cb")
        self.assertEqual(cr.attach_target(["attach", "iPhone и диск"]), "iPhone и диск")

    def test_no_word_or_a_flag_is_no_target(self):
        for argv in (["attach"], ["attach", "--help"], ["attach", ""]):
            with self.subTest(argv=argv):
                self.assertIsNone(cr.attach_target(argv))

    def test_anything_else_is_not_an_attach(self):
        for argv in ([], ["agents"], ["--continue"], ["attach the logs please"],
                     ["fix it", "attach", "x"]):
            with self.subTest(argv=argv):
                self.assertIsNone(cr.attach_target(argv))


class TestWorkingDetection(unittest.TestCase):
    def test_streaming_footer_is_working(self):
        for text in ["✻ Cogitating… (esc to interrupt)",
                     "· Retrying in 5s · attempt 3/10",
                     "Waiting for 2 background agents to finish"]:
            with self.subTest(text=text):
                self.assertTrue(cr.is_working(text))

    def test_the_footer_claude_code_2_1_actually_paints(self):
        # Captured from claude 2.1.220 in a pty: the wording the older patterns
        # keyed on ("esc to interrupt") is simply gone. Reading a live session as
        # idle is what made the wrapper retype `continue` into a session that had
        # already resumed, and then declare defeat.
        for text in ["✶ Nebulizing… ",
                     "✽ Perusing… ",
                     "✻ Cogitating… 20m 57s · ↓ 6.8k tokens",
                     "✳ Combobulating… 1m 3s · ↓ 812 tokens"]:
            with self.subTest(text=text):
                self.assertTrue(cr.is_working(text))

    def test_idle_prompt_is_not_working(self):
        for text in ["╭────╮\n│ >  │\n╰────╯", "⏵⏵ auto mode on", ""]:
            with self.subTest(text=text):
                self.assertFalse(cr.is_working(text))


class TestMenuDetection(unittest.TestCase):
    def test_rate_limit_options_menu(self):
        menu = ("What do you want to do?\n"
                "❯ 1. Upgrade your plan\n"
                "  2. Stop and wait for limit to reset\n")
        self.assertTrue(cr.is_menu(menu))

    def test_ordinary_prompt_is_not_a_menu(self):
        self.assertFalse(cr.is_menu("│ > what do you think? │"))


class TestToolEchoMask(unittest.TestCase):
    def test_children_of_a_tool_header_are_masked(self):
        lines = ['● Bash(grep "limit")', '  ⎿  You\'ve hit your limit · resets 3pm', 'back to content']
        mask = cr.tool_echo_mask(lines)
        self.assertEqual(mask, [True, True, False])

    def test_a_plain_bullet_is_not_a_tool_call(self):
        # "● API Error: …" is a real error render, not a Name(...) tool header.
        lines = ["● API Error: 529 overloaded"]
        self.assertEqual(cr.tool_echo_mask(lines), [False])


# What the screen channel has actually read off this machine: the two rows
# Claude Code 2.1.22x drew for a limit, as the wrapper logged them in August
# (the log keeps the text, not the indent — that part is put back by hand).
# And codex-cli's wording, which is its transcript's and its binary's.
SCREEN_BANNERS = [
    "  ⎿  You've hit your session limit · resets 3:40pm (Europe/Warsaw)\n"
    "     /upgrade to increase your usage limit.",
    "  ⎿  You've hit your session limit · resets 2:10am (Europe/Warsaw)\n"
    "     /upgrade or /usage-credits to finish what you’re working on.",
    "  ⎿  You've hit your weekly limit · resets Aug 26 at 6am (Europe/Warsaw)\n"
    "     /upgrade to increase your usage limit.",
    "■ You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    "to purchase more credits or try again at Aug 20th, 2026 10:00 AM.",
]

# A command that quotes a banner: upstream #63, word for word.
QUOTING = 'Bash(grep "hit your session limit - resets 3pm (UTC)" log)'

# One row of a real claude 2.1.2xx session, byte for byte out of
# docs/demo/claude-restart.cast (the temp path in the hyperlink shortened): a
# tool call still running, painted in the half of the blink where its bullet
# is a space — the dim " " in front of the cursor move to column 3.
PAINTED_WITHOUT_ITS_BULLET = (
    "\r\x1b[10B\x1b[38;2;153;153;153m \x1b[3G\x1b[39m\x1b[1mWrite\x1b[22m("
    "\x1b]8;id=3d7qa5;file:///tmp/cr-demo-claude/HANDOFF.md\x07HANDOFF.md\x1b]8;;\x07)"
    "\x1b[K\r\x1b[1B\x1b[K")


class TestACallIsAQuotation(unittest.TestCase):
    """`Name(` is what makes a row a tool call. The bullet in front of it is not.

    Claude Code blinks the bullet of a call that is still running — "⏺", then
    a space, 600ms each — and paints only the cells that changed. So the row
    reaches the wrapper with its bullet or without, whichever half it was
    first drawn in, and a header known by its glyph was a banner in the dark one.
    """

    def test_whatever_is_drawn_in_front_of_it(self):
        # The TUI's own three; nothing, which is the same call a blink later;
        # and what a list, a diff, a spinner frame or another agent would put
        # there. None of them makes the command any less of a quotation.
        for lead in ["⏺ ", "● ", "∙ ", "", " ", "  ", "* ", "• ", "- ", "+ ",
                     "✻ ", "✽ ", "· ", "│ ", "❯ ", "> "]:
            with self.subTest(lead=lead):
                self.assertIsNone(cr.find_limit(lead + QUOTING))
                self.assertEqual(cr.tool_echo_mask([lead + QUOTING]), [True])

    def test_the_row_claude_code_really_paints(self):
        rows = [l.rstrip() for l in cr.strip_ansi(PAINTED_WITHOUT_ITS_BULLET).split("\n")]
        self.assertIn(" Write(HANDOFF.md)", rows)
        at = rows.index(" Write(HANDOFF.md)")
        self.assertTrue(cr.tool_echo_mask(rows)[at])

    def test_the_same_paint_with_a_command_that_quotes_a_banner(self):
        painted = PAINTED_WITHOUT_ITS_BULLET.replace("Write", "Bash").replace(
            "\x1b]8;id=3d7qa5;file:///tmp/cr-demo-claude/HANDOFF.md\x07HANDOFF.md\x1b]8;;\x07",
            QUOTING[len("Bash("):-1])
        self.assertIn(QUOTING, cr.strip_ansi(painted))
        self.assertIsNone(cr.find_limit(painted))

    def test_an_mcp_tool_goes_by_its_display_name(self):
        # "server - tool (MCP)(": the one header whose name has spaces in it
        # and a bracket of its own before the one that opens the arguments.
        call = ('codegraph - codegraph_explore (MCP)'
                '(query: "where is the usage limit that resets in an hour logged")')
        for lead in ["⏺ ", " ", ""]:
            with self.subTest(lead=lead):
                self.assertIsNone(cr.find_limit(lead + call))

    def test_a_command_codex_ran(self):
        # codex has no call syntax: a command is "• Ran <command>", and a
        # tool it called through MCP is "• Called server.tool({...})".
        for row in ['• Ran grep -c "5-hour limit reached - resets 3pm (UTC)" x.log',
                    '• Running grep -c "5-hour limit reached - resets 3pm (UTC)" x.log',
                    '• Called notes.search({"query":"usage limit resets in an hour"})']:
            with self.subTest(row=row):
                self.assertIsNone(cr.find_limit(row))
        self.assertIsNone(cr.find_stall("• Ran grep -r 'model is at capacity' ."))

    def test_a_stall_quoted_in_one_is_not_a_stall(self):
        for lead in ["", " ", "* "]:
            with self.subTest(lead=lead):
                self.assertIsNone(cr.find_stall(lead + "Bash(grep -r 'model is at capacity' .)"))


class TestOnlyTheTuisOwnBulletOwnsWhatHangsUnderIt(unittest.TestCase):
    """A row that merely opens with `word(` hides itself and nothing else.

    Under "⏺" the next rows are that tool's echo, because the TUI says so.
    Under anything else they may be what the session answered — and the row
    under your own prompt is exactly where Claude Code draws the banner.
    Hiding that one is a session left dead overnight.
    """

    BANNER = "  ⎿  You've hit your session limit · resets 3:40pm (Europe/Warsaw)"

    def test_a_call_without_the_bullet_masks_itself_only(self):
        self.assertEqual(cr.tool_echo_mask(["Bash(ls)", self.BANNER]), [True, False])
        self.assertEqual(cr.tool_echo_mask(["* Bash(ls)", self.BANNER]), [True, False])
        self.assertEqual(cr.tool_echo_mask(["⏺ Bash(ls)", self.BANNER]), [True, True])

    def test_the_banner_under_a_prompt_that_opens_with_a_call(self):
        for prompt in ["❯ retry(3) is what I meant, not retry(1)",
                       "> retry(3) is what I meant, not retry(1)",
                       "  retry(3) is what I meant, not retry(1)"]:
            with self.subTest(prompt=prompt):
                found = cr.find_limit(prompt + "\n" + self.BANNER
                                      + "\n     /upgrade to increase your usage limit.")
                self.assertIsNotNone(found)
                self.assertIn("resets 3:40pm", found)


class TestARealBannerIsNeverACall(unittest.TestCase):
    def test_the_banners_this_machine_has_seen(self):
        for text in SCREEN_BANNERS:
            with self.subTest(text=text):
                self.assertIsNotNone(cr.find_limit(text))
                self.assertNotIn(True, cr.tool_echo_mask(text.split("\n")))

    def test_after_a_tool_call_that_has_finished(self):
        # The limit lands mid-turn: the last thing above it is a tool's block,
        # and the blank row between the two is what ends that block.
        above = "⏺ Bash(make test)\n  ⎿  ok\n\n"
        for text in SCREEN_BANNERS:
            with self.subTest(text=text):
                self.assertIsNotNone(cr.find_limit(above + text))

    def test_whatever_is_drawn_in_front_of_it(self):
        for lead in ["", "  ", "⎿  ", "⏺ ", "● ", "• ", "■ ", "⚠ ", "* ", "- "]:
            for text in LIVE_BANNERS:
                with self.subTest(lead=lead, text=text):
                    self.assertIsNotNone(cr.find_limit(lead + text))

    def test_a_space_the_renderer_dropped_does_not_make_it_one(self):
        # A gap is a cursor move as often as it is a space, and wherever the
        # stream is read with its escapes dropped the move is gone: "launched
        # (↓ to manage)" arrives as "launched(↓ to manage)". The screen channel
        # renders the move back into a gap (TestTheScreenChannelReadsTheScreen)
        # but find_limit is not only ever handed its grid. A banner that lost
        # the space in front of its bracket is still a banner.
        for text in ["You've hit your session limit · resets 3pm(Europe/Warsaw)",
                     "5-hour limit reached(resets 3pm)",
                     "Usage limit reached(resets at 3pm)"]:
            with self.subTest(text=text):
                self.assertIsNotNone(cr.find_limit(text))

    def test_an_error_is_the_app_reporting_not_a_tool(self):
        # A failure reported with a bracket — "API Error (529 …" with its gap
        # dropped, an exception printed whole — opens with `word(` too. No tool
        # is called …Error, and a bullet in front does not make it one.
        refusal = 'APIError(529 {"type":"error","error":{"type":"overloaded_error"}})'
        for lead in ["", "⏺", "⏺ ", "⎿  "]:
            with self.subTest(lead=lead):
                self.assertEqual(cr.tool_echo_mask([lead + refusal]), [False])
                self.assertIsNotNone(cr.find_stall(lead + refusal))


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def rendered(paint, rows=50, cols=120):
    ch = cr.ScreenChannel(rows, cols)
    ch.feed(paint)
    return ch


def stripped(paint):
    return [l.rstrip() for l in cr.strip_ansi(paint).split("\n")]


# No real banner has ever been captured as bytes: the log keeps the text it
# read, not the paint. So this is the banner as SCREEN_BANNERS has it, drawn
# the way Claude Code draws default-coloured text (`paint_like_claude`). The
# real one is coloured, and coloured text keeps its spaces — see the test
# that says so below.
BANNER_ROWS = ("  ⎿  You've hit your session limit · resets 3:40pm (Europe/Warsaw)\r\n"
               "     /upgrade to increase your usage limit.")


class TestTheScreenChannelReadsTheScreen(unittest.TestCase):
    """Claude Code skips a cell that already shows what it wants, and to its
    renderer a blank cell IS an unstyled space: every gap in default-coloured
    text is a cursor move, not a space. Dropping the moves (`strip_ansi`) glues
    the words; the screen channel renders them into a grid and reads that."""

    def test_a_row_of_real_2_1_273_bytes(self):
        raw = fixture("agent-tree-2.1.273.bin")
        self.assertIn("Thatbackgroundmonitorstreamhasended—noactionneeded."
                      "Stillwaitingonyou:whatwouldyouliketoworkon?", stripped(raw))
        self.assertIn("⏺ That background monitor stream has ended — no action needed. "
                      "Still waiting on you: what would you like to work on?",
                      rendered(raw).text().split("\n"))

    def test_the_letters_a_repaint_skips_are_kept(self):
        # "1 background agent launched" became "2 … agents …": the repaint
        # wrote what changed and stepped over the rest. A move is not always
        # a gap — here it skips "launched"'s neighbours, which no reading of
        # the stream alone can put back.
        raw = fixture("agents-panel-2.1.273.bin")
        self.assertIn("2 background agentlaunched (↓ to manage)", stripped(raw))
        self.assertIn("⏺ 2 background agents launched (↓ to manage)",
                      rendered(raw).text().split("\n"))

    def test_a_tool_call_from_real_bytes_is_still_a_tool_call(self):
        # "⏺⎋[3G⎋[1mExplore⎋[22m(Find⎋[16Gfirst⎋[22Gfile⎋[27Gin⎋[30Gcwd)", read
        # up to the end of the frame that first draws it.
        raw = fixture("agent-tree-2.1.273.bin")
        end = raw.index("\x1b[?2026l", raw.index("\x1b[1mExplore\x1b[22m(Find"))
        self.assertIn("⏺Explore(Findfirstfileincwd)", stripped(raw[:end]))
        rows = rendered(raw[:end]).text().split("\n")
        self.assertIn("⏺ Explore(Find first file in cwd)", rows)
        self.assertTrue(cr.tool_echo_mask(rows)[rows.index("⏺ Explore(Find first file in cwd)")])

    def test_a_banner_in_the_default_colour(self):
        paint = fake_claude.paint_like_claude(BANNER_ROWS)
        self.assertNotIn(" ", paint)
        # What the screen channel used to read: nothing.
        self.assertIsNone(cr.find_limit(cr.strip_ansi(paint)))
        # What it reads now — the same line it logged off 2.1.22x in August.
        self.assertEqual(rendered(paint).limit(),
                         "/upgrade to increase your usage limit. ⎿  You've hit your "
                         "session limit · resets 3:40pm (Europe/Warsaw)")

    def test_a_banner_in_its_own_colour_was_always_read(self):
        # Coloured text keeps its spaces, a move only where the colour starts:
        # the shape of the amber "You've used 93% of your weekly limit ·
        # resets …" warning in docs/demo/claude-restart.cast. The colours here
        # are made up. This is why the August banners read whole.
        paint = ("\r\x1b[1B\x1b[3G\x1b[38;2;153;153;153m⎿  \x1b[38;2;255;107;128m"
                 "You've hit your session limit · resets 3:40pm (Europe/Warsaw)\x1b[39m"
                 "\r\x1b[1B\x1b[6G\x1b[38;2;255;107;128m/upgrade to increase your usage "
                 "limit.\x1b[39m")
        self.assertIn("resets 3:40pm", cr.find_limit(paint))
        self.assertIn("resets 3:40pm", rendered(paint).limit())

    def test_a_refusal_in_the_default_colour(self):
        paint = fake_claude.paint_like_claude(
            "  ⎿  API Error: Selected model is at capacity. Please try a different model.")
        self.assertIsNone(cr.find_stall(cr.strip_ansi(paint)))
        self.assertEqual(rendered(paint).stall(), "⎿  API Error: Selected model is at "
                         "capacity. Please try a different model.")

    def test_a_banner_painted_over_is_gone(self):
        # Erased the way the 2.1.273 captures erase a row: blanks written over
        # each old word, the old gaps stepped over. The stream still has the
        # banner in it; the screen does not.
        row = "You've hit your session limit · resets 3pm (UTC)"
        paint = "\r\x1b[1B\x1b[31m%s\x1b[39m\r" % row + "".join(
            "\x1b[%dG%s" % (m.start() + 1, " " * len(m.group(0)))
            for m in re.finditer(r"\S+", row))
        self.assertIsNotNone(cr.find_limit(cr.strip_ansi(paint)))
        self.assertIsNone(rendered(paint).limit())

    def test_a_move_to_another_row_does_not_pair_the_two(self):
        # Adjacent in the stream, 25 rows apart on the screen: a limit with no
        # reset time and a reset time with no limit, not a banner.
        paint = ("\x1b[5;3H\x1b[31mYou've hit your session limit\x1b[39m"
                 "\x1b[30;3H\x1b[2m· resets 3pm (UTC)\x1b[22m")
        self.assertIsNotNone(cr.find_limit(cr.strip_ansi(paint)))
        self.assertIsNone(rendered(paint).limit())

    def test_a_call_painted_like_claude_is_still_a_quotation(self):
        paint = fake_claude.paint_like_claude("⏺ " + QUOTING + "\r\n  ⎿  1")
        self.assertIn("⏺ " + QUOTING, rendered(paint).text().split("\n"))
        self.assertIsNone(rendered(paint).limit())

    def test_forgetting_keeps_the_cursor_where_claude_left_it(self):
        # The next frame repaints only what changed, at positions claude
        # computed from where its cursor is: blanking must not move it.
        ch = rendered(fake_claude.paint_like_claude(BANNER_ROWS))
        at = ch.screen.cursor()
        ch.forget()
        self.assertIsNone(ch.limit())
        self.assertEqual(ch.text().strip(), "")
        self.assertEqual(ch.screen.cursor(), at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
