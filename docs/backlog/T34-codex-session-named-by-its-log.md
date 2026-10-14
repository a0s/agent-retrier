# T34 — A codex session is named by codex's own log

Priority: P0 · Epic: A · Depends on: T03, T06, T22, T32 · Size: M · Status: done (unreleased)

## Problem

A rollout names no pid, and until now nothing else did either, so T03 told
two codex sessions in one directory apart by the growth heuristic, and held
the restart over two candidates "until the handoff phrase proves which is
ours". T32's "Left open" found both halves of that broken in the field:

1. **The later wrapper reads the earlier one's rollout.** codex makes a
   thread as it starts, and the thread's rollout only at its first message.
   Until a session has been spoken to it has no rollout at all, so the
   neighbour's is the only one there is — and the heuristic, finding no
   rollout newer than us growing, takes a growing older one (the fallback that
   exists for `codex resume`). Bound to it, the wrapper reads the neighbour's
   count as its own and, once the neighbour's turn ends, types the fold phrase
   into a session with nothing in it.
2. **The earlier wrapper stops folding for good.** The neighbour's rollout,
   once it exists, is newer than us too: two candidates, so the restart is
   held until the phrase's echo proves which is ours. Only a restart types
   the phrase. Nothing else can lift the hold; the session runs on to
   codex's own compaction at the hard cap, which is what the wrapper exists
   to get ahead of.
3. Found on the way: a **stall** (a refused turn) was acted on from any
   rollout in the directory. Unlike a limit it is not the account's: a
   neighbour's refused turn had this wrapper type `continue` into a session
   that never stopped.
4. Found on the way: a rollout `codex resume` reopened turned up by its mtime
   and was read **from byte 0**, as a new file would be — history included.
   A limit the session hit last month was scheduled as a wait today.

## Evidence

`~/.agent-retrier/log`, 2026-09-27 … 2026-10-09, sixteen codex wrappers with
a `context bound` line. Fourteen were bound to a rollout whose thread was
made 0–6 s after the wrapper started — their own, made as codex started.
Two were bound to one made before they started:

```
10-07 15:51:02  [cr 29982] start                     (cwd some_game)
10-07 15:51:04  [cr 29982] context bound: …rollout-2026-10-07T15-25-58-01a1168a…   ← 25 min older
10-08 19:54:04  [cr 75256] start
10-08 19:54:14  [cr 75256] context bound: …rollout-2026-10-08T19-38-18-01a11c98…   ← wrapper 9345's
```

Four wrappers logged `more than one candidate rollout … holding the restart`
and never `no longer held`: 90222 (exited six seconds later), 40477 (held for
the last three of its six days), 29982 (held for its last 1½ h), and 9345:

```
10-08 19:55:42  [cr 9345] more than one candidate rollout in this directory; holding the restart …
10-08 20:09:44  [cr 9345] codex compacted the thread on its own before the restart could — 258k of a 258k cap
```

Between them, 354 `transcript switched … (fallback heuristic)` lines (132 on
10-07, 204 on 10-08): the heuristic handing `current` back and forth between
two sessions' rollouts, two lines to a switch.

Reproduced without a pty against the old script (real watcher, real
controller, rows handed over as `main()` hands them): the later wrapper,
its codex silent, folded on the neighbour's 250k; the earlier one, at 250k
of a 194k threshold and quiet for ten hours, typed nothing. Both are now
`TestTwoCodexSessionsInOneDirectory`, and on a pty in `TestCodexEndToEnd`.

## What names a codex session

Checked on codex-cli 0.161.0, this machine, read-only (the logs database was
copied before it was queried):

- **`$CODEX_HOME/logs_2.sqlite`**, which the wrapper already reads for codex's
  compaction count (T22), tags every row with `thread_id` and with
  `process_uuid = pid:<pid>:<uuid>` — the process that wrote it. And codex
  writes such a row the moment a TUI makes a thread, at launch and at every
  `/clear`/`/new` (`rpc.method="thread/start"`), not when the thread first
  speaks: 01:10:53 for a thread whose rollout was born at 01:11:50; 11:57:21
  for one born at 12:54:50. One process's rows name its root threads one
  after another (pid 28883: four, across three restarts) and its subagents'
  threads, whose rollout heads say `thread_source: subagent`. Run over a copy
  of the owner's database, the registry names, for each of the three codex
  processes still in it, the root thread its wrapper had been bound to.
  Retention is per thread (the last 1,000 rows or so) and per process for
  rows with no thread; the wrapper reads it every poll, minutes ahead of any
  pruning.
- The rollout's file name carries the thread's **creation** time and its id
  (`rollout-2026-10-09T13-56-55-01a12086-…`); ids are UUIDv7, so the id
  itself carries the millisecond. The file is born later: 13:58:56 for that
  one.
- `session_meta` has `id`, `cwd`, `timestamp` (creation), `originator`,
  `source`, `thread_source`; for a subagent `parent_thread_id`. No pid.
- `thread-writer-locks/<thread id>.lock`: empty, made at thread creation and
  held with `flock` by the writer (`unix-flock` in the binary). Not read:
  naming the holder needs `lsof` (macOS) or `/proc/<pid>/fd` (Linux), and
  says nothing the log does not.
- No codex was running on this machine during the investigation, so
  `lsof -p` was not tried on one; see "Not verified".

## What was done

1. **`CodexSessionRegistry`** (codex's `ClaudeSessionRegistry`). Ours is a
   row whose `process_uuid` pid is our child or descends from it (npm's
   `codex.js` puts codex a generation down); of several such processes — a
   `codex exec` the session ran itself — the shallowest. Of its threads, the
   newest with a user rollout is the session. A thread with no rollout yet
   answers only when there is no such rollout at all: at launch, `lookup()`
   names it with `path: None`, the watcher binds with `current` still None,
   and nothing is read as ours until the file exists. After `/clear` the new
   thread is named at once but the session stays on the old rollout until
   the new one is written (at the resume phrase), as the restart needs it to.
   Rows from before we started are not read (a reused pid). A log with no
   `process_uuid` column is `unavailable` and the heuristic runs at once, as
   it always did for codex; a log that names nobody for our pid gets T32's
   grace first.
2. **`strangers()`**: rollouts the log pins on another codex (or on a codex
   the session started), and — without the log — any whose thread was made
   before we started, unless codex was started with `resume`. The watcher
   demotes them: the heuristic, when it is all there is, never settles on
   one, and none of them is a second candidate for T03's hold.
3. **The hold is not for good.** Past `Controller.PROOF_WAIT` (300 s) of a
   fold held over an ambiguous reading, the fold goes out on that reading —
   between turns only; an unproven reading never presses Esc — and the
   phrase's echo settles it: landing in the rollout it was folded on, it is
   proof and the restart goes on; landing anywhere else, the count was a
   neighbour's and the restart is called off with the cancel phrase (T09).
   Compared against the rollout the fold was decided on, not `context_path`:
   the echo's own growth can hand the heuristic our rollout in the same poll.
4. **Stalls are path-bound** (`main()`): a refused turn in a rollout that is
   not ours is not ours to nudge.
5. **`CodexAgent.seed_offset`**: a rollout first seen after we started whose
   thread is older than us is read from where it stands, not from byte 0.
   Used by the watcher for every file it meets (a no-op for claude) and for
   the file `bind()` lands on.
6. `test/fake_codex.py` logs `thread/start` with `process_uuid` like codex
   0.161 (`FAKE_LOG_PID=0`: an older codex), mints UUIDv7 ids, makes its
   rollout only at the first write with `FAKE_LAZY_ROLLOUT=1`, and serves
   `usage N` / `refuse` on cue.

Rejected: **`lsof`/`/proc/<pid>/fd`** on the child (a process to spawn every
poll on macOS, not in a slim container at all, unverified against a live
codex, and it would name the thread no earlier than the log does); a
**creation-time window** around our start alone (two wrappers started
together are inside each other's; kept only as the fallback's exclusion of
older threads); **holding until proven, forever** (the deadlock); **folding
on the guess as soon as it is ambiguous** (failure 1 again); **asking the
model**, or typing anything else, to get a proof (only the fold phrase is
worth typing into someone's session, and it is the proof already);
**`thread_source`/`originator`** (`codex exec` and the VS Code extension
write `user` rollouts in the same cwd; they say what kind of session, not
whose).

## What each case does now

- **Two TUIs in one directory, either order:** each is told its own thread
  by pid; the other's rollout is a stranger. No hold, no switching.
- **Our rollout not written yet:** bound, `current` None, nothing folded,
  however long the neighbour talks; the T32 grace never runs out on it.
- **`codex resume <id>` / `--last`:** the resumed thread is logged under our
  pid; its rollout is found in the day its UUIDv7 id names (or by a throttled
  search of the tree for an older id), read from where it stands.
- **`/clear` (ours or by hand), `/new`:** the new thread is named at once; the
  session moves when its rollout exists. A clear by hand mid-fold is now
  `session identity` and takes T30's path.
- **In-session `/resume`:** the reopened thread logs under our pid and is
  followed. (Without the log it is a thread older than us and is not.)
- **Subagents:** logged under our pid, excluded by their rollouts' heads, as
  before.
- **An older codex (no `process_uuid`):** the heuristic at once, minus
  threads older than us; T03's hold where two candidates remain, giving way
  after `PROOF_WAIT`.
- **No log database:** T32's grace (30 s), then the same.

## Acceptance criteria

- [x] `test_codex.py`: `TestCodexSessionRegistry` (named before written; a
      neighbour's alone; behind a shim; a nested codex; subagents; `/clear`;
      a reused pid; no column; no database; resume; strangers; resumed
      history not replayed), `TestTwoCodexSessionsInOneDirectory`
      (registry + watcher + controller: both failures, the old-codex hold
      giving way, a wrong guess called off), `TestCandidateModeHoldsTheFoldBack`
      (the wait, no Esc on an unproven reading, the echo either way).
- [x] Pty (`TestCodexEndToEnd`): a silent later session is not folded on its
      neighbour's count; the earlier one still folds once a neighbour has
      started; a neighbour's refused turn is not nudged here; a restart
      unfolds into a thread that has no rollout yet; the T03 pair, with and
      without a log that names processes.
- [x] Red against the unpatched script: the four end-to-end unit tests and
      the resume replay on their assertions; the six pty tests (three on the
      fold, the hold and the nudge themselves, three on the new log lines).
      The registry/hold unit tests need the new code to run at all.
- [x] `docs/codex.md` ("Two sessions in one directory"), `docs/stalls.md`,
      T03 ("Checked"), T32 ("Left open").

## Not verified

Against a running codex: that `lsof -p` shows the rollout and the writer
lock (no codex was running, and starting one was not needed for the log);
that a TUI attached to the shared app-server daemon logs its threads under
the daemon's pid (the daemon's threads are deliberately counted for no one);
whether codex-cli 0.154/0.155 have `process_uuid` — if not, they get the
fallback. The `thread/start` row is logged inside a shell-snapshot span; a
codex with shell snapshots off may name a new thread only at its first
message, which is when its rollout appears too — the same poll, since the
registry is asked after the files are read.
