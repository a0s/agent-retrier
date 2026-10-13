# How it works

Design notes: the pty, the two detection channels, the corner badge, the update
check, requirements and tests.

[← back to README](../README.md)

- [One pty instead of tmux](#one-pty-instead-of-tmux)
- [Transcripts and the screen](#transcripts-and-the-screen)
- [Typing safely](#typing-safely)
- [Getting out of the way](#getting-out-of-the-way)
- [A sign of life](#a-sign-of-life)
- [Updates](#updates)
- [Requirements](#requirements)
- [Tests](#tests)

## One pty instead of tmux

`agent-retrier.sh` runs your claude on a pty it owns, so it can read the output
and write input at the same time. That single fact removes the need for tmux
(`capture-pane` plus `send-keys`), a detached monitor process, event marker
files, and a launchd or systemd reconciler.

It passes everything else through: keys, colours, window resizes, exit codes,
and any flag you would have handed to `claude`.

## Transcripts and the screen

It detects a limit on two channels. The first is the transcript: Claude Code
writes `{"error":"rate_limit","isApiErrorMessage":true}` into
`~/.claude/projects/<project>/<session>.jsonl`, and codex writes its own JSONL
"rollout" under `$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/`, stating outright the
reason a turn ended and the size of the context window. Either is structured,
unambiguous, and the one the wrapper trusts. The second is the screen, matched
against patterns, and it is only used when the transcript is unavailable.

The full detail — `claude agents`, and how a screen-scheduled wait can be
corrected — is in [usage-limits.md](usage-limits.md#how-a-limit-is-detected).
Which codex rollout is followed is in
[codex.md](codex.md#rollouts-what-the-wrapper-reads).

## Typing safely

Before typing anything it checks that Claude is not mid-turn and that you are
not typing yourself. It can see your keystrokes, so an unsent draft in the
prompt box is never overwritten.

A wait also ends when the limit does. Log into another account with `/login`,
upgrade the plan, or simply get your quota back early: nothing announces any of
that, so the wrapper takes the session answering again as the answer.

## Getting out of the way

Nothing goes into your shell config, no background process outlives the session,
and the only file it writes under your home directory is the log.

Two sessions share that same log file, and every line says which one wrote
it: `[cr <pid> <agent>]` right after the timestamp, the pid being the
supervisor process itself (`Logger`'s `tag`). See
[troubleshooting.md](troubleshooting.md#how-to-read-the-log-by-pid) for
filtering one session's lines out of the interleaved file.

No python3, a `claude -p` batch run, `CR_DISABLE=1`: any of those and it execs
plain claude rather than becoming the reason your session will not start.

## A sign of life

A wrapper you cannot see is indistinguishable from a wrapper that died an hour
ago. So there is one mark, dim, in a corner of the screen: `◆ cr` while it is
watching, the time left while it is waiting out a limit, and `◆ cr held` when
the reset has passed but you are still at the keyboard. With the
[context restart](context-restart.md#what-you-will-see) on, it also shows the
percentage (`◆ cr 47%`) as the session nears the threshold.

Everything else the badge can say:

| you see | meaning |
| --- | --- |
| `◆ cr` | idle, watching |
| `◆ cr 1h59m` | waiting out a limit, blinking |
| `◆ cr held` | the reset passed; still waiting on you |
| `◆ cr 2/3` | retyping the message, verifying it landed |
| `◆ cr stopped` | gave up after `CR_MAX_ATTEMPTS` |
| `◆ cr folding` / `clearing` / `unfolding` | a context restart in progress, blinking |
| `◆ cr 47%` (`~47%` if the window is a guess) | context restart armed, nearing the threshold |
| `◆ cr unfold failed` | the resume phrase never reached the session in `CR_RESUME_ATTEMPTS` tries — read `CR_HANDOFF_FILE` by hand; clears on the next key you press |
| `◆ cr unfold?` | `/clear` landed but the unfold has been waiting on you or a busy session for over a minute |
| `◆ cr restart off` | the trigger is off for the rest of the session (a failed unfold, or too many restarts in an hour) — permanent, not cleared by a key |
| `◆ cr window?` | the restart is armed but nothing has said how big the context window is yet |
| `◆ cr ~est` | the window is a guess (T19), not yet confirmed by usage |

`unfold failed` and `restart off` are debts, not moments — `notify()` repeats
them every `CR_NOTIFY_REPEAT_SEC` (default 300) on top of the badge, because a
line Claude's own repaint erases within a frame is not enough for something
that can sit unnoticed for hours. `unfold failed`'s repeat stops the moment you
press any key (the debt itself, `restart off`, does not go away — only the
nagging about it does); a `restart off` reached directly, without ever
passing through `unfold failed`, stops nagging the same way.

<p align="center">
  <img src="badge.svg" width="620"
       alt="Two terminal frames: an idle session with a dim '◆ cr' in the bottom-right corner, and the same session after a limit, showing '◆ cr 1h59m'">
</p>

Nothing is reserved from Claude. The badge is painted over the finished frame in
the gaps between repaints, with the cursor saved and restored around it and the
last column left empty so it can never wrap the screen. Claude paints over it
and it comes back a moment later: about forty bytes, a few times a second at
most, and never a byte into the session itself.

```sh
CR_BADGE=0 agent-retrier                  # off
CR_BADGE_POS=top-right agent-retrier      # any of the four corners
CR_BADGE_LABEL=retrier agent-retrier      # your own word next to the mark
```

The picture above is not a mockup. `python3 docs/badge-shot.py` runs the real
wrapper over a stand-in that prints one Claude-shaped frame, replays what the
wrapper wrote through the terminal emulator the tests use, and renders the
screen that came out.

## Updates

It checks once a day whether there is a newer release, and says so in two dim
lines before the session starts:

```
[agent-retrier] agent-retrier 1.9.0 → 1.10.0 is out
                 brew upgrade a0s/agent-retrier/agent-retrier
```

The command is the one that updates the copy you are running — `brew upgrade`
from a cellar install, `git -C <clone> pull` from a clone, the releases page for
a file you downloaded. Nothing waits on the network for it: the notice is read
out of a cache the previous run wrote, and the check that refreshes it runs in
the background after claude is already up, so the first run after installing
says nothing at all.

`CR_UPDATE_CHECK=0` never checks and never mentions it. The rest of the update
settings are in [configuration.md](configuration.md#updates).

## Requirements

`bash` and `python3` (3.9 or newer, standard library only). If either is
missing, or claude is invoked with `-p`, the wrapper execs claude unchanged. It
never becomes the reason your session will not start.

Windows is not supported (no pty).

## Tests

```sh
./test/run.sh              # 760 tests: patterns, time parsing, transcript, model
                           # windows, update checks, state machine, the badge, custom
                           # commands, degradation, end-to-end runs on a real pty
                           # (rendered through a terminal emulator, so "what the user
                           # sees" is asserted), and two wrappers sharing one project
                           # dir (the "wrong session's numbers" bug class)
./test/run.sh --docker     # the same suite on Linux, from anywhere with docker
./test/run.sh test_time.py # just one file
./test/run.sh --live-codex # the T15 checklist against real codex — asks first
```

Two wrappers over one project dir — the shape a "chose the wrong session's
transcript" bug (fold on someone else's numbers, unfold lost) needs to
reproduce — are covered by `test/test_two_wrappers.py` and
`test/test_fake_agents.py`, built on `helper.two_wrappers()`. That helper runs
both `fake_claude.py`/`fake_codex.py` at once in one project dir and one
shared `CR_LOG`, filters each wrapper's own log lines by its T01 `[cr <pid>
...]` tag, and kills the whole process tree on close — including the agent
process, which `os.setsid()` deliberately moves out of the wrapper's own
process group, so a plain `killpg` on the wrapper alone would leave it
running. `fake_claude.py`'s `FAKE_SCRIPT` directives (`grow`, `dropfirstfold`,
`synthetic`, `delayclear`, `stream`) and `fake_codex.py`'s `FAKE_POPUP` /
`FAKE_ROLLOUT_AGE_DAYS` reproduce the scenarios those bugs need: a session
that keeps climbing, a lost handoff reply, a `<synthetic>` row, a slow
`/clear` ack, a mid-stream row with no `stop_reason` yet, a `$`/`/` popup that
eats the first Enter, and an old rollout `codex resume` should still see.
`FAKE_SESSION_DELAY` and `FAKE_SESSION_FILE=0` are the two things about
`sessions/<pid>.json` a fake that writes it instantly cannot show: real Claude
Code takes a second or two over it, which is the whole of the time a wrapper
started beside another has to decide whose session it is looking at, and some
claudes never write it at all.

Everything above runs against fakes. One thing cannot: whether real codex
still accepts the keystrokes the wrapper sends. `./test/run.sh --live-codex`
(`test/live_codex.py`) re-runs the
[T15](backlog/T15-codex-unfold-live-investigation.md) checklist as assertions
against the real thing — `/clear` accepted by the Enters the shipped recipe
sends and creating no rollout of its own, the phrase after it getting
through, `$skill …`/`@file …`/Cyrillic delivered verbatim, and an
unrecognized `/name` dropped inline and never sent. It drives codex *under*
the wrapper and types with the shipped `typing_plan()` itself, so it cannot
pass against a recipe the wrapper no longer sends.

It spends real quota — two sessions, a handful of three-word turns on the
cheapest model — so it is never part of the default run and never starts
unasked: an interactive `y/N` naming what it will spend, or `CR_LIVE_CONFIRM=1`
for a non-interactive one; anything else is refused (exit 2). No codex on
`PATH` is a skip, not a failure (exit 0); a codex that is installed but logged
out exits 3. `CR_LIVE_MODEL`, `CR_LIVE_CODEX_BIN` and `CR_LIVE_PROJ` override
the model, the binary and the throwaway project dir. Every session is torn
down process-group by process-group from a `finally` and from `SIGINT`/`SIGTERM`
— a killed pty driver otherwise leaves the supervisor behind, still writing to
the same log — and the run ends by calling `check-orphans.py` whatever else
happened.

`./test/run.sh` ends with an orphan check (`test/check-orphans.py`): any
process still carrying `CR_CLAUDE_ARGV` in its environment after the suite
finished is a supervisor (or agent under it) that should have been reaped and
was not. It reads `/proc/<pid>/environ`, so it is authoritative under
`--docker` (Linux) and a documented no-op on macOS, where there is no portable
equivalent.

Prior art: [claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry),
whose issue tracker supplied most of the edge cases tested here.
