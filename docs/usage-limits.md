# Waiting out a usage limit

How the wrapper notices a usage limit, sleeps until it resets, and types
`continue` for you — without ever typing over you.

[← back to README](../README.md)

## What happens

The session stops on `You've hit your session limit · resets 3pm`, and the
wrapper sleeps until the reset and types `continue` for you.

That is the whole setup for the usage-limit half. Start a session and forget
about it: the next time you look, the limit will have passed and the session
will have carried on.

While it waits, the [corner badge](how-it-works.md#a-sign-of-life) shows the
time left, and `◆ cr held` when the reset has passed but you are still at the
keyboard.

## When a limit lifts early

A wait also ends when the limit does. Log into another account with `/login`,
upgrade the plan, or simply get your quota back early: nothing announces any of
that, so the wrapper takes the session answering again as the answer, drops the
countdown and goes back to watching.

## It never types over you

It owns the terminal, so it sees your keystrokes. Before typing anything it
checks that the session is not mid-turn and that you are not typing yourself.
An unsent draft in the prompt box is never overwritten — it is enough to make
the wrapper wait, and so is a turn that has not finished.

## How a limit is detected

It detects a limit on two channels. The first is the transcript: Claude Code
writes `{"error":"rate_limit","isApiErrorMessage":true}` into
`~/.claude/projects/<project>/<session>.jsonl`, and codex writes its own JSONL
"rollout" under `$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/`, stating outright the
reason a turn ended and the size of the context window. Either is structured,
unambiguous, and the one the wrapper trusts.

The second is the screen, matched against patterns, and it is only used when
the transcript is unavailable (`CR_SCRAPE` controls this). It is read the way a
terminal shows it, rendered cell by cell: Claude Code moves the cursor over a
gap between words as often as it prints a space there.
One command is never
read that way at all: started on `claude agents`, the wrapper switches the
screen channel off for the run. Every card on that roster is a different
session's last line, a limit shown there belongs to somebody else and is
usually hours old, and opening a card scrolls that session's history — old
banners included — past the same scraper. A limit in one of the directory's
transcripts is still read there and waited out, but nothing is typed into the
roster when it lifts: its one input box starts a new session with whatever
lands in it. A wait the screen did schedule stays
open to it: a banner stating an earlier reset takes over, so a wrong one cannot
hold the session past the moment it could have gone back to work.

Detection patterns live in one array at the top of `agent-retrier.sh`. Add a
wording and nothing else changes.

## Resuming a session

Claude's own flags pass straight through, so whatever you would type after
`claude` you type after `agent-retrier` instead:

```sh
agent-retrier --resume deb786e8-3006-4edf-b5e6-2ca73e25620e   # claude --resume <id>
agent-retrier --continue                                      # the last session here
agent-retrier --cmd claude-work --resume deb786e8-3006-4edf-b5e6-2ca73e25620e
```

A resumed session is watched exactly like a fresh one. The wrapper follows the
transcript claude is already appending to, so a limit hit an hour into the
resumed conversation is picked up the same way.

## Caveats

- The screen-scraping fallback cannot tell a live banner from a session that is
  discussing one. It is off whenever the transcript is being written, which is
  the normal case.
- A weekly limit stated as a bare `resets Jul 22` with no year is assumed to be
  the next occurrence.

## Settings

`CR_MESSAGE`, `CR_MARGIN_SEC`, `CR_MAX_ATTEMPTS`, `CR_USER_IDLE_SEC`,
`CR_TYPING_MAX_SEC`, `CR_RESUME_SEC`, `CR_DRAFT_GRACE_SEC` and `CR_SCRAPE` are
described in [configuration.md](configuration.md#usage-limits-and-typing).

See also: [the stall](stalls.md) (a turn refused with no reset time),
[codex](codex.md), [how it works](how-it-works.md).
