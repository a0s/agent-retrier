# The stall: a turn the server refused

What the wrapper does when the server refuses a turn outright and gives no
reset time.

[← back to README](../README.md)

A usage limit says when it lifts, and the wrapper [waits that
out](usage-limits.md). The other way a session stops says nothing at all: the
server refuses the turn outright — `Selected model is at capacity. Please try a
different model.` — with no reset time and nothing scheduling a way back. On
codex that refusal ends the whole turn, which takes every agent the session was
running down with it, and leaves the session sitting at an idle prompt with no
sign anything is wrong.

So the wrapper waits a minute and types `continue`, the same thing a person
watching the screen would do. A repeat refusal doubles the wait — 60s, 120s,
240s — up to `CR_STALL_MAX_WAIT_SEC`, because a service that has just said it is
full does not want to be asked again every minute. Any turn that finishes ends
the streak, and the next stall starts back at a minute. The usual gates still
apply: nothing is typed while the session is mid-turn or while there is an
unsent draft in the prompt box.

Only this terminal's own session is nudged. A usage limit is the account's,
and one read in any session's transcript in the directory is waited out; a
refused turn is the session's, and another codex session in the same
directory that was refused is its own wrapper's (or its own person's) to
nudge — typing `continue` here would start a turn nobody asked for.

## Settings

`CR_STALL_WAIT_SEC` (first wait, `60`; `0` switches stalls off),
`CR_STALL_BACKOFF`, `CR_STALL_MAX_WAIT_SEC` and `CR_STALL_MAX_ATTEMPTS` are in
[configuration.md](configuration.md#stalls).

## Detection patterns

Detection patterns for a refused turn live in `CR_STALL_PATTERNS`, next to the
other pattern arrays at the top of `agent-retrier.sh`. That array is kept
narrow on purpose: a wrong stall is a message typed into a live session for no
reason.

See also: [usage limits](usage-limits.md), [codex](codex.md).
