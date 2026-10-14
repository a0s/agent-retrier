# T33 — `claude attach` is its job's session, and the roster is nobody's

Priority: P0 · Epic: A · Depends on: T02, T32 · Size: M · Status: done (unreleased)

## Problem

T32 made a claude session's identity pid-only: a session is ours if
`sessions/<child pid>.json` names it, or a record whose pid descends from the
child. Until one does the wrapper follows nothing for `CR_SESSION_GRACE_SEC`,
then hands the directory to the growth heuristic (`_pick_current`). Two
launches never get a record of the child's, so they ended on the heuristic for
good — and the heuristic is built for the one case they are not:

- **`claude attach <id|name>`** opens a background session. Its transcript is
  older than the wrapper, and the heuristic prefers a transcript that did not
  exist when the wrapper started: in a shared directory, a neighbour's. T32
  "Left open" says so, and that `attach` names its session in argv.
- **`claude agents`** (the roster) has no transcript of its own at all.
  Whatever the heuristic picks there is somebody else's, and the roster's one
  input box dispatches a new session with whatever is typed into it.

Bound to a neighbour, the wrapper counts, folds and answers on the
neighbour's transcript and types into its own terminal on that basis.

## Evidence

From the owner's logs (`~/.agent-retrier/log`, `.claude-retrier/log`) and
`~/.claude` (read only), Claude Code **2.1.294** installed.

- Wrapper 45890, `claude attach 6cb059d2`, voxik: `no session registry for
  pid 45932` at start, `context bound: …/6cb059d2-1300-….jsonl (fallback
  heuristic)` a day later, then — under the rule T32 removed —
  `session bound: 1b900c55 (cwd+startedAt)` on 10-01 17:13, a stranger's
  session, kept until exit on 10-03 while its own transcript `f123d92c` gained
  559 rows (T32's evidence). Four more `cwd+startedAt` binds on 2026-09-20
  came from three roster wrappers (78469, 27845 twice, 74059), each the first
  session to start in voxik after the roster did; 27845 also flapped between
  two sessions' models off the statusline proxy.
- What `attach` is: `claude attach --help` — "Open the background session in
  this terminal. ← returns to agent view … The session keeps running either
  way. Part of the session name works in place of the id." In the binary,
  `rt()` resolves the argument against `$CLAUDE_CONFIG_DIR/jobs/`: the one
  directory matching `^[a-f0-9]{8}$` that starts with it; otherwise (when it
  is not 1–8 hex) a name, ranked exact name 3 / part of a name 2 / task text
  1, opened only when exactly one name matches.
- Where the session is: `jobs/6cb059d2/state.json` holds `sessionId`
  (`6cb059d2-1300-…`, what it was dispatched under), `resumeSessionId`
  (`f123d92c-…`) and `linkScanPath`
  (`~/.claude/projects/-Users-a0s-a0s-github-voxik/f123d92c-….jsonl`) — under
  the job's cwd, not necessarily the attaching shell's. The session updates
  both fields on `/clear` (`z9r(K(), Gu())` in the conversation-reset path).
  The daemon resumes a job from `resumeSessionId ?? sessionId`, looking for
  the file at `linkScanPath`, then its directory, then the project dirs.
- Whose record: daemon workers are spawned with
  `CLAUDE_CODE_SESSION_KIND=bg`, `CLAUDE_JOB_DIR=<jobs>/<short>`; the pid file
  writer records `kind` and `jobId: kind==="bg" && CLAUDE_JOB_DIR ?
  basename(CLAUDE_JOB_DIR) : undefined`, with `sessionId` the live session.
  Claude Code's own `y8()` maps a session id to its job by exactly those
  fields. `daemon/roster.json` shows the worker (`pid`) and the REPL
  (`replPid`) as children of the daemon supervisor — never of the attach
  client, so the descendant rule cannot reach them.
- The attach client writes no record (45890, 2.1.283). Two older attach
  wrappers (25113, 17504, 2026-09-20, 2.1.273) did bind a `(pid file)` — of a
  fresh session whose only user row is `attach`: the `--settings`-before-
  `attach` bug since fixed in `claude_launch_args`, not attach behaviour.
- The roster: `claude agents --help` — `--settings` applies to "the agent
  view and dispatched sessions". The binary's fleet view draws the input with
  placeholder `describe a task for a new session`. Nothing in the roster
  process is one session.

What the old code did, reproduced (tests below, run against the unpatched
script): an attach wrapper over a job at 50k with a neighbour at 700k typed
the fold phrase into the attached session on the neighbour's count; a roster
wrapper beside the same neighbour typed the fold phrase into the roster; a
roster wrapper typed `continue` into the roster three times after a
neighbour's limit lifted.

## What was done

1. `ClaudeSessionRegistry(…, attach=, roster=)`, built by `claude_registry(pid,
   argv)` from the launch. An attach is resolved to its job the way `rt()`
   does (unique id prefix; else a name contained in exactly one job's `name`;
   hex-looking text is never a name) and named by the `sessions/*.json`
   record with `kind: "bg"` and that `jobId` (newest `startedAt` if a respawn
   overlaps), or — no such record — by the job's `state.json`
   (`resumeSessionId ?? sessionId`). The child's own record and descendants
   are not consulted. The record carries `transcriptPath`, found where the
   daemon would look (`linkScanPath`, its directory, the project dir of the
   session's cwd). The roster is named by nothing.
2. `may_guess = False` on both: `TranscriptWatcher._may_guess` never hands
   them to the heuristic, and logs no fallback.
3. `TranscriptWatcher`: a registry may give the transcript path
   (`transcriptPath`); the first transcript bound outside the scanned project
   dir is seeded at its size, so an attach from another directory does not
   replay the session's history (yesterday's limit row included). A later
   new file there (a `/clear`) is read from 0.
4. `Controller.hands_off`: set for the roster. A wait still runs and shows;
   when it ends, nothing is typed (`the limit is over; nothing typed: …`) —
   limit and stall alike.
5. `claude_launch_args` adds no statusline proxy for the roster: dispatched
   sessions inherit `--settings`, and would report into this wrapper's status
   file — and keep doing so from the daemon under this pid after it exits.

Rejected:

- *Binding attach by matching the argument against record `sessionId`
  prefixes.* The argument is a job id or a name; the session id is the job's
  only until the first `/clear` (`6cb059d2-1300…` became `f123d92c…`).
- *The job's `state.json` only.* It is the daemon's bookkeeping; the record
  is the running session's own word, carries busy/idle, and covers a respawn
  under a fresh id. Kept as the fallback.
- *Trusting the child's own record for attach when one appears.* The only
  ones ever seen described a different session.
- *Restricting the heuristic for attach rather than removing it.* Its
  transcript is pre-existing by definition; "fresh" is exactly wrong.
- *A roster that types only when the roster is not on screen* (`is_roster` on
  the rolling window). Whichever session is open there was never identified,
  and an 8 KB window is a guess about what is drawn.
- *Refusing to bind the old-build heuristic when more than one transcript is
  live.* Old builds (no session file, < 2.1.273) keep T32's behaviour. The
  growth heuristic and the T03 candidate hold are shared with codex, which is
  being reworked separately; the hold already applies to claude's fallback
  (`on_candidates` in `main()` has no agent check), and what remains — a
  lone neighbour's fresh transcript taken while ours is unwritten — is the
  same hole codex's "later wrapper" case is, and belongs with that fix.

## What each launch does now

| launch | followed | folds | typed on a limit |
|---|---|---|---|
| ordinary session, Claude Code ≥ 2.1.273 | its pid file (T02/T32) | on its own count | yes |
| ordinary session, no session file | heuristic after 30 s (T32) | yes, held with >1 fresh candidate | yes |
| `claude attach <id\|name>` | the job's record, else its `state.json`; nothing until one exists | on the job's count | yes, into the terminal |
| `claude agents` | nothing, ever | never | no — waited out and logged |

## Acceptance criteria

- [x] `test_transcript.py`: `TestWhatALaunchIsNamedBy` (the job's record; the
      client is not the session; nothing else names an attach; id prefix,
      ambiguous prefix, name, shared name, hex-looking text; `state.json`
      fallback; record over state; respawn overlap; transcript under the
      session's cwd and its job's `linkScanPath`; the roster is nobody;
      `may_guess`), `TestAnAttachIsFollowedByItsJob` (the 45890 field case
      with registry + watcher + controller over two hours; a `/clear` in the
      attached session; an attach from another directory does not replay
      history; the roster follows nothing and still reads a limit).
- [x] `test_controller.py`: `TestNothingIsTypedIntoTheRoster`.
- [x] `test_patterns.py`: `TestWhatAnAttachNames`; `test_statusline.py`:
      no proxy on the roster.
- [x] `test_two_wrappers.py` with `fake_claude.py` playing `attach <id>` (no
      record of its own, the job's `kind: "bg"` record and `state.json` kept
      current) and `agents` (no record, no transcript): an attach never folds
      on a neighbour; an attach folds its own session through `/clear`, by
      record and by `state.json`; the roster folds nothing and types nothing
      when a limit lifts.
- [x] Red against the unpatched script: all five pty tests, and at unit
      level every test that states the attach or roster binding (the 45890
      reproduction, the roster, the no-typing rule, the statusline proxy).
- [x] `docs/context-restart.md`, `docs/troubleshooting.md`,
      `docs/usage-limits.md`, `docs/configuration.md`; T32 "Left open".

## Left open

- **`←` inside an attach** returns to the agent view, where another session
  can be opened in the same process. The wrapper goes on following the job on
  its command line; anything it types — a retry, a fold phrase — goes to what
  the terminal shows. Detecting that needs the screen, and none of the
  roster's own patterns reliably survives the switch back.
- **A backgrounded session keeps our statusline proxy.** A session wrapped
  interactively and then sent to the background keeps its launch flags as
  `respawnFlags` — job 6cb059d2 still runs `--cr-statusline
  …/status/59146.json`, a wrapper long gone. A later wrapper that happens to
  get pid 59146 reads that session's model and window as its own.
- **Old builds' heuristic** (see Rejected): left to the codex identity work.

## Not verified

Against a live attach (not run: the owner's real sessions are off limits):
that a running background session's record carries `jobId` in every build
since background sessions shipped (read from the 2.1.294 binary only; the
`state.json` fallback covers a record without it); that a session dispatched
from a spare host keeps `jobId` in its record after the claim; how soon after
`Waking session …` the respawned session writes its record. Whether 2.1.294's
roster process writes a record of its own is unknown — it makes no
difference now, since the roster is named by nothing whatever is written.
