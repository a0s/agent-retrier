# codex

Wrapping codex: `agent-retrier-codex` (or `agent-retrier --codex`), which rollout the wrapper reads, which
subcommands are wrapped, and the codex context-restart threshold.

[← back to README](../README.md)

- [Running it](#running-it)
- [Which agent: `--agent`](#which-agent-agent)
- [Rollouts: what the wrapper reads](#rollouts-what-the-wrapper-reads)
- [Subcommands](#subcommands)
- [Skills and file mentions in codex](#skills-and-file-mentions-in-codex)
- [Context restart on codex](#context-restart-on-codex)
- [How it differs from claude underneath](#how-it-differs-from-claude-underneath)
- [Staying ahead of codex's own compaction](#staying-ahead-of-codexs-own-compaction)

## Running it

Run `agent-retrier-codex` (or `agent-retrier --codex`) where you would have run `codex`:

```sh
agent-retrier-codex                              # instead of: codex
agent-retrier-codex resume --last                # any codex arguments work
agent-retrier --codex resume --last              # the same, through the flag
CR_CODEX_CMD='codex --model gpt-5.6-sol' agent-retrier-codex
```

It is `agent-retrier` under another name: a symlink the install puts next to
it, which runs the same file with codex as the default. Its command is
`CR_CODEX_CMD`, or plain `codex` — never `CR_CLAUDE_CMD`, so a claude command in
your rc file stays claude's. `--cmd` and `--agent` still work and still win.

Both names are installed whichever agents you have. On a machine without codex
all `agent-retrier-codex` does is say so — which also means installing codex later
needs nothing reinstalled. See [Install](../README.md#install).

## Which agent: `--agent`

The long way round is the same thing:

```sh
agent-retrier --cmd codex
agent-retrier --agent codex --cmd 'codex --model gpt-5.6-sol'
```

`--agent` (or `CR_AGENT`) says which one you are running; the default, `auto`,
works it out from the command, so `agent-retrier --cmd codex` needs nothing
else. `--cr-agent` is the same flag under the prefix the other wrapper options
carry. `claude` and `codex` are the only two values.

## Rollouts: what the wrapper reads

codex writes one JSONL "rollout" per thread under
`$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/` (`CODEX_HOME` defaults to `~/.codex`),
and it states outright several things Claude Code's transcript only implies:
where a turn started and ended, the exact size of the context window
(`model_context_window`), what the last request was sent with, and the reason a
turn ended. So on codex the context window is read rather than guessed from a
model name, and `CR_CONTEXT_WINDOW` is not needed.

Every project's rollouts share one tree, and a codex subagent gets a rollout of
its own. The wrapper reads only the rollout whose head says `thread_source:
user` and whose `cwd` is the directory it is running in — reading a subagent's
would report a limit this terminal never hit, and a context that is not ours to
restart. When codex is given `--cd`, that directory is the one followed.

### Two sessions in one directory

A rollout's head names no pid, and codex ships nothing else that reliably
does either (no usable lock file, and `lsof` does not report an append-open
the way it would for a process holding the file for writing). So two codex
sessions started in the same `cwd` are, on paper, indistinguishable: both
pass the `cwd`/`thread_source` check above, and either one's rollout could be
this terminal's.

The wrapper picks a "current" candidate the same way it always has (whichever
recently-created rollout is growing — see `TranscriptWatcher._pick_current`
in the source), and reads usage from it right away. But that pick is only a
guess until something proves it: the [context restart](context-restart.md)'s
fold phrase carries a nonce unique machine-wide, and its echo landing in a
rollout is the one piece of proof that rollout is really this session's.
Until that has happened, and only while more than one rollout in the
directory still qualifies as ours (created since this wrapper started, `cwd`
matching, not yet ruled out), the controller keeps reading — the badge and
the log still show a context percentage — but will not act on it: no fold is
sent while a neighbour's climbing count could be the one it just read. Once
the echo confirms a rollout, that one is locked in for the rest of the
session, and every other candidate seen so far is permanently ruled out —
a neighbour growing afterwards can no longer hand `current` back to it.

What that does not cover is a neighbour's rollout being the *only* one. Codex
creates a rollout when a session is first spoken to, not when it starts, so a
wrapper whose own session has said nothing yet has none — and a second session
started in the directory since, and already at work, is then one candidate
and not one of two. The wrapper reads it, and if that count is past the
threshold once the neighbour's turn has ended, types the fold phrase into its
own session. The echo corrects it from there (it is what creates this
session's rollout, and proves which one that is), but the phrase has been
typed by then, into a session with nothing in it to fold. Claude Code states
which session a process is running, and the wrapper waits for that rather
than guess ([which session is mine](context-restart.md#which-session-is-mine));
nothing codex writes says as much.

### `codex resume` on an old session

`codex resume <old>` reopens the rollout in the directory named for the day
it was *created*, which can be long outside the three days the ordinary scan
covers. So `CodexAgent.paths` also walks the rest of `$CODEX_HOME/sessions`
looking for anything with a newer mtime than this wrapper's own start — the
only sign, short of reading every file, that something out there was just
resumed. That walk is throttled to once every 30 seconds; a directory tree
going back months is not worth re-reading on every poll for a case that, by
definition, changes rarely.

## Subcommands

Most of what codex can be asked to do is not a session at all. `codex exec`,
`codex login`, `codex mcp` and the rest run untouched, the way `claude -p`
already does; `codex resume` and `codex fork` are sessions, and are wrapped.

## Skills and file mentions in codex

Typing `/` at the start of a codex prompt opens its command list, the same as
Claude Code — `CR_SLASH_ENTER`'s longer pause and extra Enter exist for
exactly that hazard (see
[context-restart.md](context-restart.md#commands-skills-and-mentions)). One
real hazard sits behind that list, though: an **unrecognized** `/word` is
rejected inline ("Unrecognized command…") and never sent, not even as plain
text (T15, live codex-cli 0.155.1) — a bare `/command` is only safe as a
resume or handoff phrase if `command` is one codex itself defines.

`$` and `@`, by contrast, turned out **not** to open any popup at all (T15
live-drove all of this on codex-cli 0.155.1, correcting an earlier guess
recorded here): `$name` is codex's own "invoke a skill" convention, but it is
a *model-level* instruction, not a composer feature, and `@file` (file
mention) is likewise plain text once the whole phrase arrives in one write —
which is how `schedule_injection`/`typing_plan` always send it (T16). Both
are delivered exactly like any other phrase, with the ordinary single Enter,
no popup-settling pause needed.

This is why a resume or handoff phrase that needs to name a codex skill
should use `{skill:NAME}` rather than writing `$NAME` or `/NAME` by hand — it
expands to `$NAME` for codex and `/NAME` for claude (T17):

```sh
export CR_RESUME_MSG='{skill:supervisor} continue from `{file}`'
```

See [T15's results table](backlog/T15-codex-unfold-live-investigation.md#results)
for the full input-by-input findings.

## Context restart on codex

The [context restart](context-restart.md) works on codex the same way it does
on claude, with a threshold of its own:

```sh
CR_CONTEXT_RESTART=1 agent-retrier --cmd codex
```

Codex restarts against a hard cap under `CR_CODEX_RESERVE_TOKENS` — a number
already tuned for exactly that cap — not against a fraction of its own window
the way claude does, so a shared percentage would have meant something
different, and arguably worse, for codex than it does for claude; that is why
codex has never shared claude's `CR_CONTEXT_TOKENS` and why the old
`CR_CONTEXT_PCT`/`CR_CODEX_CONTEXT_PCT` pair was removed in 2.0. Under the
flag, codex reads its own row out of `MODEL_PROFILES` (T18; `--cr-models`
prints the table): for the 5.6-class models that row is `258,400 / 194,400 /
258,400` (window / restart_at / compact_at), i.e. the cap minus the same 64k
`CR_CODEX_RESERVE_TOKENS` default — reclamped against whatever you actually
set that to, not the frozen number. That row only answers at all when the
rollout's window matches the one it was written for; a `model_context_window`
raised in `config.toml` reports a different window, the row sits out, and the
same flag falls back to the window minus `CR_CODEX_RESERVE_TOKENS` applied to
whatever window the rollout actually reports, rather than leaving codex
unprotected (see
[context-restart.md](context-restart.md#the-context-window)).

`CR_CODEX_CONTEXT_TOKENS` has no default and never borrows `CR_CONTEXT_TOKENS`.
An absolute count is tied to a window — 500k picked for a 1M claude session is
past the end of a 258k codex one — and it would overrule codex's own row.
`CR_CODEX_TOKENS_<SLUG>` sits above both: an absolute threshold for one codex
model by name (`gpt-5.6-sol` → `CR_CODEX_TOKENS_GPT_5_6_SOL`), useful once more
than one codex model is in play and they do not compact at the same point.

### The badge percentage matches codex's status line

Whatever the threshold is, the corner badge shows how full the session is as a
percentage the same way codex's own status line does — `Context 19% used`.
codex leaves a fixed 12,000 tokens (the prompt a session carries before anyone
speaks) out of both sides of that fraction, and the wrapper does the same, so
`cr 60%` matches the status line's own 60%, not a few points earlier. Nothing
is read off the screen: the window comes from the rollout, and the count from
codex's own log (see [below](#staying-ahead-of-codexs-own-compaction)).

## How it differs from claude underneath

Two things differ from claude underneath. Claude Code writes a `stop_reason` into
every answer and codex writes none, so "the folding turn finished" is read off
the row that closes it: `task_complete` counts as a clean end, `turn_aborted`
(somebody pressed Esc) does not. And "the session is mid-turn" is the span
between `task_started` and that closing row, rather than the transcript going
quiet for `CR_ROOT_IDLE_SEC` — a codex root waiting on its agents can write
nothing for minutes while its turn is very much still running. `/clear` starts
a new chat in codex too, and that chat's rollout is a new file, which is how the
wrapper confirms the context really fell.

That is also why, in the log, `restart step held: a turn is still running` can
last as long as the turn does on codex, however quiet it is — that is a root
waiting on its agents.

A refused turn (`Selected model is at capacity`) on codex ends the whole turn
and every agent with it; the wrapper [nudges it](stalls.md).

## Staying ahead of codex's own compaction

A restart that codex's own compaction beats to it is no restart at all — the
history is already summarised away by the time the handoff is asked for. So when
the context restart is on for codex, the wrapper makes sure it gets there first.

### Where codex compacts

Read from the codex-cli 0.154 source and checked against real sessions:

- **The window is 272k, not 1M.** gpt-5.6-sol is capable of 1.05M through the
  API, but codex uses `context_window: 272000` from its model list unless
  `model_context_window` says otherwise (the most it accepts for sol is 872k).
  95% of that is usable: the rollout's `model_context_window` is 258,400.
- **It compacts at 90% of the raw window — 244,800 tokens** — or at the 258,400
  hard cap, whichever comes first.
- **It counts more than the rollout shows.** The number compared is the last
  request's tokens plus an estimate of everything added since, reasoning
  included: 251,023 in one session whose rollout said 232,673. That is why
  sessions compact while the status line still says the mid 80s.
- **It compacts in the middle of a turn**, after any model response that is
  followed by a tool call. Waiting for a turn to end is not enough.
- **Nothing is written before it starts.** The rollout's `compacted` row appears
  once it is over.

### What the wrapper does about it

1. **Moves codex's threshold out of the way.** codex is started with
   `-c model_auto_compact_token_limit_scope="body_after_prefix"
   -c model_auto_compact_token_limit=1000000000`. Under the default scope a
   configured limit is clamped to 90%; under this one it is not, which leaves
   only the hard cap (258,400), a limit no setting moves. Your `config.toml` is
   not touched. `CR_CODEX_HOLD_COMPACT=0` leaves codex's settings alone.
2. **Reads the count codex decides on.** After every request codex writes
   `post sampling token usage … total_usage_tokens=… full_context_window_limit=…`
   into `$CODEX_HOME/logs_2.sqlite`. The wrapper follows those rows for its own
   thread, read-only, so both the count and the cap are codex's figures, not an
   estimate of them. Without the database it falls back on the rollout.
3. **Keeps room for the fold.** The restart threshold is never allowed past the
   cap minus `CR_CODEX_RESERVE_TOKENS` (64k): the folding turn adds its own reply
   and a file write to a context that is already nearly full, and a heavy one —
   reading files, running shell commands before it writes — can burn most of a
   smaller reserve on its own. A higher threshold is lowered to that line, and
   the log says so.
4. **Interrupts a turn that is about to be compacted.** If a turn is still
   running when the count crosses that line, the wrapper presses Esc — codex
   records `turn_aborted` — and folds the session up straight away. The handoff
   phrase asks for what was in flight, so the interrupted work is written down
   rather than lost. The usual gates apply: nothing is pressed while you are
   typing. `CR_CODEX_INTERRUPT=0` turns this off, and the restart then only ever
   happens between turns.

Below the line, a running turn is left to finish and the restart waits for it —
but for codex that check *between* turns is nearly irrelevant on its own: an
orchestrator turn can run for hours, so in practice the cap-minus-reserve line
above is the only restart that actually happens. `CR_CODEX_INTERRUPT_AFTER_SEC`
(default `0`, unchanged behavior) interrupts a turn early instead of waiting
for that line, once it has sat past the ordinary threshold this many seconds:

```sh
CR_CODEX_INTERRUPT_AFTER_SEC=1800 agent-retrier --cmd codex
```

### If codex still gets there first

The log says `codex compacted the thread on its own before the restart could`,
with the count and the cap at the time (the context it was judging no longer
exists either way). If the handoff it was racing had not yet landed, the
restart is dropped and the session falls back on codex's own compaction. If it
had — the file already passes every check `/clear` would have waited for — the
wrapper skips straight to sending the resume phrase instead of aborting, since
codex already did the clearing for it. A larger `CR_CODEX_RESERVE_TOKENS` or a
lower `CR_CODEX_CONTEXT_TOKENS` gives the next one more room either way.

### Judging the reserve against real folds

`CR_CODEX_RESERVE_TOKENS` (64k) was picked from one incident, not measured
across many. The wrapper already knows what a fold costs — tokens when the
handoff is accepted minus tokens when it was asked for — so after every codex
fold it logs that number and appends `{agent, cwd, cost, date}` to
`CR_CODEX_FOLDS_FILE` (default `~/.agent-retrier/folds.json`):

```
the fold cost 31k tokens (reserve 64k)
```

A fold that burns more than 80% of the configured reserve gets a line
recommending a larger `CR_CODEX_RESERVE_TOKENS`. `CR_CODEX_RESERVE_ADAPT=1`
acts on that automatically instead of just recommending it: `1.25×` the
largest fold cost ever seen in the file (this session's own folds included)
becomes the effective reserve used by every later computation, whenever that
is larger than the configured number — never lower than it, only ever
raising it, and seeded from the file so a fresh session benefits from what an
earlier one already learned.

### Why not a PreCompact hook

codex does run a `PreCompact` hook just before compacting, and it can say
`continue: false`. But that aborts the turn rather than skipping the compaction,
and the next turn — the one asking for the handoff — hits the same check before
it starts, so the hook would refuse it too. It would also need to be trusted in
`/hooks` before codex runs it at all.

See also: [configuration](configuration.md#codex),
[context restart](context-restart.md), [stalls](stalls.md).
