# Troubleshooting

Where to look when the wrapper does not do what you expected, and the known
limitations.

[← back to README](../README.md)

## Start with the log

`~/.agent-retrier/log` (or `CR_LOG`) has the whole story with numbers. It is
the only file the wrapper writes under your home directory.

## How to read the log by pid

Two sessions in the same project write into the same log, so every line
carries `[cr <pid> <agent>]` right after the timestamp — the pid is the
supervisor process, printed on `start:`. `grep 'cr 48213'` isolates one
session's own `start: … → … → exit:` sequence out of the interleaved file.

## Two sessions in one project

This used to be a real hazard — the wrapper guessed which transcript was
"ours" by which file grew last, and a neighbour under the same working
directory could briefly win that guess. It no longer does: claude identifies
its own transcript from `~/.claude/sessions/<pid>.json`, and codex confirms
its rollout with the fold phrase's own nonce echoed back into it — see
[context-restart.md](context-restart.md#which-session-is-mine) for the whole
chain. Each session also gets its own handoff file (`{id}` in
`CR_HANDOFF_FILE`, or the wrapper's own registry-based coordination when you
leave it out — see [context-restart.md](context-restart.md#turning-it-on)),
so two folds in the same project no longer overwrite each other. What is left
of the old limitation is the fallback path for a claude too old to write a
session registry file — see
[Caveats](context-restart.md#caveats). `claude attach` is bound by the job it
names and the agent roster by nothing at all; neither is ever guessed at. Follow
[how to read the log by pid](#how-to-read-the-log-by-pid) above to untangle
the two sessions' log lines while you check.

What to look for in one session's lines:

- `session bound: <id> (pid file)` a couple of seconds after `start:` is the
  normal case; `(descendant of pid N)` is the same thing for an alias. From
  then on that session is the only one read.
- `session rebound: <old> → <new> (pid file)` is a `/clear` or `/resume` in
  that terminal.
- `no session registry for pid N after 30s; falling back to newest transcript`
  means claude never wrote the file and the heuristic is in charge. Every
  guess it makes after that is a `transcript switched: … (fallback heuristic)`
  line. A `claude attach` or `claude agents` wrapper never logs it.
- `claude attach 6cb0: background job 6cb059d2 — following the transcript its
  own session names` then `session bound: <id> (background job 6cb059d2)` is
  an attach found; `(background job 6cb059d2, from its state.json)` is the
  same from a build whose session record does not name its job. A `/clear` in
  the attached session is `session rebound: … (background job 6cb059d2)`.
  `claude attach X: no background job matches it …` means nothing will be
  followed in that terminal: no context count, no fold, limits only.
- `the agent roster is no session of ours: …` at the start of a `claude
  agents` wrapper, and later `the limit is over; nothing typed: …`, are that
  wrapper doing what it should: nothing is followed or typed there.
- A `session bound:` or `session rebound:` line ending in `(cwd+startedAt)`
  can only be in a log written by an older wrapper, and is that wrapper bound
  to a **different** session: one that merely started in the same directory
  later. It happened as a session's own claude exited beside a neighbour
  started since, and to `claude attach` and roster sessions for as long as
  they ran; two wrappers started in the same moment could do it to each
  other. A `context bound: … (fallback heuristic)` in a `claude attach` or
  `claude agents` wrapper's lines is the same thing from a wrapper older than
  T33: the heuristic's guess, which there was usually a neighbour.

## Unfold failed — what to do

`◆ cr unfold failed` in the corner means `/clear` went out, but the resume
phrase (`CR_RESUME_MSG`) never reached the session after `CR_RESUME_ATTEMPTS`
retries — the session is sitting there empty, waiting for nobody. The log
names the handoff file it was about to point the session at; open it
yourself and paste its contents (or the resume phrase itself) into the
session by hand. Pressing any key in the session clears the nagging repeat of
this notice, but the context-restart trigger itself (`context_off`) stays off
for the rest of the session either way — nothing here re-arms on its own,
because a restart that already failed once is not owed a second try against
the same session. See
[context-restart.md](context-restart.md#why-clear-is-the-last-thing-it-will-do)
for the full unfold sequence and why giving up looks the way it does.

## Common checks

- **The context restart never fires.** See
  [When nothing happens](context-restart.md#when-nothing-happens) — the log
  lines listed there, in order of likelihood.
- **The badge says `unfold failed`.** See
  [Unfold failed — what to do](#unfold-failed-what-to-do).
- **The wrong command runs, or an alias is not found.**
  `agent-retrier --cmd X --cr-dump-argv` prints exactly what will be executed.
  See [custom-command.md](custom-command.md).
- **codex is not being wrapped.** `codex exec`, `codex login`, `codex mcp` and
  the rest run untouched on purpose; only sessions (plain `codex`,
  `codex resume`, `codex fork`) are wrapped. See [codex.md](codex.md#subcommands).
- **codex compacted before the restart.** The log says `codex compacted the
  thread on its own before the restart could`. Give the fold more room with
  `CR_CODEX_RESERVE_TOKENS` or a lower `CR_CODEX_CONTEXT_TOKENS`; see
  [codex](codex.md#staying-ahead-of-codexs-own-compaction).
- **The wrapper seems to do nothing at all.** Without `python3`, with `claude
  -p`, or with `CR_DISABLE=1`, it execs plain claude unchanged. See
  [requirements](how-it-works.md#requirements).
- **Is it still running?** Look for the dim `◆ cr` mark in a corner
  ([a sign of life](how-it-works.md#a-sign-of-life)); `CR_BADGE=0` hides it.
- **You want it out of the way for one run.** `CR_DISABLE=1` runs plain claude.

## Limitations

- The screen-scraping fallback cannot tell a live banner from a session that is
  discussing one. It is off whenever the transcript is being written, which is
  the normal case.
- A weekly limit stated as a bare `resets Jul 22` with no year is assumed to be
  the next occurrence.
- An alias or function is read out of your rc file once, at startup, and run
  from a plain non-interactive shell afterwards. One that calls *another* alias
  defined in the same rc file will not find it. (bash 3.2, still what macOS
  ships as `/bin/bash`, cannot be asked for an alias body at all and falls back
  to running the alias through `bash -i`.)
- The context restart reads its figures from one transcript at a time, the one
  this terminal's session writes. claude identifies that transcript
  explicitly and codex confirms it by echo nonce (see
  [two sessions in one project](#two-sessions-in-one-project)); only for a
  Claude Code too old to write a session registry file does the wrapper fall
  back to guessing which file grew last, and only there can a neighbour under
  the same working directory briefly win that guess — see
  [Caveats](context-restart.md#caveats). `claude attach` follows the job it
  names and the roster follows nothing.
- An attach follows the job it was started with even after `←` has taken the
  terminal to the agent view and another session has been opened there, and
  anything it types goes to what the terminal shows.
- `$skill` and `@file` phrases on codex are not yet given the same safe-typing
  treatment `/` gets, so their Enter can be swallowed by codex's own popup —
  see [codex: skills and file mentions](codex.md#skills-and-file-mentions-in-codex).
- Windows is not supported (no pty).
