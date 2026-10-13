# T32 — A session that is not named yet is not guessed at

Priority: P0 · Epic: A · Depends on: T02, T04 · Size: M · Status: done (unreleased)

## Problem

T02 gave the wrapper claude's own answer to "which transcript is mine":
`~/.claude/sessions/<pid>.json`. It also gave `ClaudeSessionRegistry` a rule
for a claude the parent chain could not reach — a record in our `cwd` whose
`startedAt` is no earlier than our own start — and left the growth heuristic
running from the first poll, before the registry had said anything.

Both are guesses, and both are made at the moment a guess is most likely to be
wrong. A record "in this directory, started after us" is our session, and is
equally every session anyone starts in this directory after us; the only thing
that told the two apart was there being exactly one. So:

1. **At launch.** Claude Code writes its session file a second or two after it
   starts. Two wrappers started together in one directory each poll before
   their own file exists; the one whose file is the later of the two finds the
   other's, alone, in its cwd, started after it — and binds. It rebinds when
   its own appears.
2. **At exit.** claude removes its file on the way out, a poll or so before
   the pty closes. With a neighbour started since, the last poll binds to it.
3. **With no file of its own** (`claude attach`, the agent roster, an older
   build). The first session to start in the directory afterwards is taken,
   and — the binding being sticky — kept after that session has exited. The
   wrapper's own session is not read again.

Bound to a neighbour, the wrapper reads the neighbour's context count, model,
turn endings and busy/idle status as its own, and types on that basis.

## Evidence

`~/.agent-retrier/log` and its predecessor, from the first tagged line (T01)
on: 2026-09-18 … 2026-10-13, 63 claude wrappers.

- How long the file takes: `start:` → `session bound … (pid file)` is 2 s for
  30 launches, 3 s for 13, 4 s for 4, 5 s for 2 and 13 s for 1 (log resolution
  1 s, poll 2 s — so 43 of 50 had it by the first poll after the one at
  launch, 6 by the next). Nobody is named for at least one poll of every
  launch; with the fake it was a few milliseconds, which is why the suite only
  ever saw it as a flake.
- Eight binds by the `cwd+startedAt` rule, none of them a claude it was meant
  for. Three at exit — the clearest, wrapper 12975 in `some_game`, 2026-10-08:

  ```
  13:32:21  [cr 12975] session bound: 514b1c25 (pid file)
  16:15:43  [cr 84461] session bound: 063da01d (pid file)        ← a neighbour, same cwd
  17:10:25  [cr 12975] session rebound: 514b1c25 → 063da01d (cwd+startedAt)
  17:10:26  [cr 12975] exit: 129 after 3h38m
  ```

  Five by wrappers with no file of their own: four by three roster wrappers
  (2026-09-20), and one by wrapper 45890 (`claude attach 6cb059d2`, voxik),
  which had followed its session by the heuristic for two days, then:

  ```
  10-01 17:13:28  session bound: 1b900c55 (cwd+startedAt)       ← somebody else's, just started
  10-01 18:14:09  no session registry for pid 45932 …           ← and gone again
  10-03 21:38:15  exit: 129 after 106h53m
  ```

  Its own transcript (`f123d92c`) gained 559 rows after 17:13:28 and reached
  402k of a 510k threshold. None of them were read: the wrapper stayed bound
  to a session that had exited. It did not cost a fold only because the
  session stopped short of one.
- No two wrappers in those logs were started within ten seconds of each other
  in one directory, so case 1 is not in them. It is in the suite:
  `test_a_neighbours_answer_does_not_cancel_our_wait` logged `limit detected
  (neighbour)` for a wrapper's own limit, and its assertion had been relaxed
  to `limit detected (` to live with it. Run alone against the old code with
  the strict assertion and a check that a's log never names b's session, it
  failed 16 times in 30 — every time on the second: a had been bound to b's
  session. (The label itself did not come out `(neighbour)` in those thirty;
  it needs the limit row read in the same poll as the wrong binding.)

What a wrong binding can do, each reproduced against the old code with the
real registry, watcher and controller, no pty (the first four are now tests:
`TestTwoWrappersStartedTogether`, and on a pty the late-session-file cases in
`test_two_wrappers.py`):

| | happens | undone by the rebind |
|---|---|---|
| neighbour's count, model, window and busy/idle read as ours | yes, from its first row | the count yes; the model, and the window and threshold worked out from it, stayed until our own next row restated them (always before a count of ours could be compared) |
| our own limit logged as `(neighbour)` | yes | — (the wait is scheduled either way) |
| our wait dropped because the neighbour answered | yes, once their answer is `ALIVE_GRACE` (8 s) after the limit | **no**: the limit row is not read twice, so the retry is never typed |
| our session folded on the neighbour's count | yes, once their transcript has been quiet `CR_ROOT_IDLE_SEC` (20 s) | **no**: the phrase is typed. Left bound, the session is cleared and resumed for nothing |
| that fold, cut short by our own file appearing before the phrase's echo does | yes | **no**: `the session was replaced at handoff_sent … dropping this restart`, with no cancel phrase (T09) — the model is left told to wrap up |
| that fold, cut short by the neighbour's `/clear` | yes, at `HANDOFF_OK`: `the session was cleared by hand … skipping our /clear` (T30) | **no**: the resume phrase is typed into a session that was never cleared |
| our `/clear` sent to the wrong session | no — everything typed goes into our own pty, and `/clear` needs the handoff our own model wrote | |
| our handoff file claimed by the neighbour | no — claims are by wrapper pid (T05), made before the fork | |

At launch the wrong binding lasts as long as our file is late: one poll (2 s)
for most launches, two for one in eight, 13 s at the worst on record. That is
long enough for the first two rows and, at the worst, for the third; never
for the 20 s the rest need. Cases 2 and 3 are where those were reachable —
case 3 for days.

## What was done

1. `ClaudeSessionRegistry.lookup()` binds on whose process wrote the record
   and nothing else: `sessions/<child pid>.json`, or a record in our cwd whose
   pid descends from the child. The `cwd+startedAt` rule is gone, and with it
   the `started_at` argument. Naming nobody is a silent answer.
2. `TranscriptWatcher` does not run the growth heuristic while a registry
   exists and has named nobody: for `CR_SESSION_GRACE_SEC` (30 s) `current`
   stays `None`, so nothing `main()` routes by path reaches the controller.
   Files are still read — a limit is the account's. When the wait runs out the
   heuristic takes over as before and the log says
   `no session registry for pid N after 30s; falling back to newest transcript`,
   once, then and not at every launch. A session named once stays the session
   when its file disappears.
3. The registry is asked after the transcripts are read, not before: claude
   names a session before writing its first row, so no row can be read ahead
   of the name it belongs under. (Asked first, a file and a row landing
   between the two steps of one poll were still logged as a neighbour's.)
4. `Controller.bind_transcript`: the registry naming our session over a
   transcript the heuristic had picked (possible only for a file later than
   the wait) is a correction, not a session moving on — the model learned off
   the guess is dropped with everything else, and it is not taken for a
   `/clear` done by hand.
5. `_ps_table` falls back to `/proc/<pid>/stat` where there is no `ps` (a slim
   container): the parent chain is now the only way an alias launch is found.
   `_read` returns nothing for a file that is not a JSON object — a list there
   crashed the wrapper reading it.

Rejected: keeping the rule for "exactly one candidate" (one candidate is the
race — the other one's file, alone); a `startedAt` window around our own start
(two wrappers started together are inside each other's); passing `--session-id`
ourselves (conflicts with `--resume`/`--continue` and a caller's own flag, and
`/clear` moves the id anyway); holding back every read while unnamed (a limit
would wait thirty seconds for a name it does not need).

## What each fallback case does now

- **Older Claude Code, no session file:** nothing followed for
  `CR_SESSION_GRACE_SEC`, then the heuristic, as before. Limits throughout.
- **`--resume` / `--continue`:** the pid file names the resumed session, same
  path as any other; a transcript present at start is seeded, not replayed.
  (What the file holds after `--resume` is still unverified live — T02 (b).)
- **`/clear`, `/resume` in the session:** `session rebound … (pid file)`,
  unchanged.
- **Alias / shell function:** `descendant of pid N`, unchanged; without `ps`,
  through `/proc`.
- **`claude attach`, the roster:** the heuristic after the wait, permanently.
  They no longer take a neighbour's session. `attach` names its session in
  argv and could be bound outright; not done here.
- **codex:** has no registry, so none of this code runs for it. It has the
  same exposure by another route, and it is in the log — see "Left open".

## Acceptance criteria

- [x] `test_transcript.py`: `TestSessionRegistry` (a session started beside us
      is not ours; the race; our file going away), `TestBeforeTheRegistryHasNamedAnyone`
      (nothing picked while unnamed; a limit still read; a row never ahead of
      its name; the heuristic after the wait, said once; a late file still
      decides; a file gone is still the session),
      `TestTwoWrappersStartedTogether` (registry + watcher + controller).
- [x] `test_controller.py`: `TestAGuessTheRegistryCorrects`.
- [x] `test_two_wrappers.py`: the strict `limit detected (transcript)` is
      back; a session file that is late (`FAKE_SESSION_DELAY`) is waited for,
      and does not fold us on the neighbour's count; a claude that writes none
      (`FAKE_SESSION_FILE=0`) is still followed, and its limit is not kept
      waiting.
- [x] Red against the unpatched script: every test above that states the
      race or the wait (18 at unit level, the four new pty tests every run,
      the restored two-wrapper test 16 runs in 30). Nine more are guards
      and pass on both — what must go on holding: a late file still decides,
      a file gone is still the session, a `/clear` still keeps the model.
- [x] `docs/context-restart.md` ("Which session is mine", "Caveats"),
      `docs/troubleshooting.md`, `docs/configuration.md`.

## Left open

Found while checking whether codex has the same race. It does not — it has no
registry to guess with — but two codex wrappers in one directory are worse
off than two claudes were, by the growth heuristic alone. Neither is changed
here: both need something that names a codex session, and T03's "Checked"
section (lock files, `lsof`) was never filled in.

- **The later wrapper reads the earlier one's rollout.** A rollout is created
  at the first message, so until its own session has spoken the neighbour's is
  the only candidate, and one candidate is not "ambiguous" (T03 holds the fold
  only for two). `some_game`, 2026-10-08: wrapper 75256, started 19:54:04, is
  `context bound` at 19:54:14 to `01a11c98` — the rollout wrapper 9345 had
  been bound to since 19:39. Reproduced without a pty: the fold phrase is
  typed into the empty session on the neighbour's count.
- **The earlier wrapper stops folding for good.** The neighbour's rollout
  makes two candidates; the restart is held "until the handoff phrase proves
  which is ours"; and the phrase is only ever typed by a restart. Same day:
  9345 is held from 19:55:42, and apart from switching between the two
  rollouts the next thing it logs is
  `codex compacted the thread on its own … 258k of a 258k cap` at 20:09:44 —
  which is what a wrapper that cannot fold, having moved codex's own
  compaction out of the way, would be expected to end in. Reproduced: at 250k
  of a 194k threshold and quiet for ten hours, nothing is typed. Between
  2026-09-28 and 2026-10-09 the log has 354
  `transcript switched … (fallback heuristic)` lines from codex wrappers, two
  to a switch.
- **`claude attach` and the roster** still live on the heuristic, and it can
  still pick a neighbour's transcript and fold on it (one fresh candidate,
  ours being older than the wrapper). `attach <id>` names its session in argv.
- A codex **stall** is read from any rollout in the directory, like a limit,
  and nudges this terminal's session whoever was refused. Whether that is
  meant is not said anywhere.

## Not verified

Against real Claude Code: that the session file is always written before the
session's first transcript row (assumed from the order they are observed in);
what `sessionId` it holds after `--resume`; whether `claude -p` writes one at
all. Thirty seconds is twice the slowest launch in one machine's log, not a
measured bound.
