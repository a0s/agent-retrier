# T02 — Claude: transcript binding via `~/.claude/sessions/<pid>.json`

Priority: P0 · Epic: A · Depends on: T01 · Size: L

> Amended by [T32](T32-session-not-yet-named.md): the "`startedAt` ≥ our start
> time" half of item 1 below is gone. It bound whichever session started in
> the directory after us, and every bind it made in the field was one of
> those. The heuristic of item 3 also no longer runs until the registry has
> had `CR_SESSION_GRACE_SEC` to name the session.

## Problem

`TranscriptWatcher._pick_current` (agent-retrier.sh:1816) selects “our”
transcript using the heuristic “stay on a file while it grows; otherwise take
the newest of the files that grew.” Our session can be silent for seconds or
minutes (an API request, a long tool call), and any poll during which a foreign
file grew switches `current` to it. `main()` (4128) passes to `ctl.on_context`
only lines with `path == watcher.current`, but `current` belongs to another
session, so the controller receives foreign values: `context_tokens`,
`context_model`, `last_stop_reason`, and `context_grew_at`. The result: fold
reports “far from the limit,” two sessions fold simultaneously, and unfold is
not sent because of another session’s “still writing.”

For the watcher, “our file went silent while another one is being written” is
**indistinguishable** from `/clear` (“our file ended and the session moved to a
new one”). A heuristic cannot fix this — explicit identity is required.

## Evidence

- Log from 2026-09-17 22:37–22:45: `claude-opus-5 … restarting at 510k` /
  `claude-sonnet-5 …` alternate every few seconds.
- Session `4a2c66dc` (opus, maximum 298k) received a fold at “516k” — the
  numbers belonged to session `b5d84050` (sonnet, 550k).
- Claude Code 2.1.273 writes `~/.claude/sessions/<pid>.json`:
  `{pid, sessionId, cwd, startedAt, version, kind: "interactive", status:
  "busy"|"idle", statusUpdatedAt, …}`; the file disappears when the process
  exits. It also exports `CLAUDE_CODE_SESSION_ID` to child processes.
- The transcript after `/clear` is a new file `<newSessionId>.jsonl`, whose 4th
  line is the user line `<command-name>/clear</command-name>`.

## What to do

1. **Identity source.** Add a new `ClaudeSessionRegistry` class (next to
   `ClaudeAgent`): given the child process pid, read
   `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (default `~/.claude/sessions`).
   Account for the exec'ed command being a shell (`sh -c "alias …"`) and
   claude being a grandchild rather than a child: if `<childpid>.json` does not
   exist, scan all `sessions/*.json` whose `cwd` matches ours and whose process
   `pid` is a descendant of our child (the `ps -o ppid=` / `/proc/<pid>/stat`
   chain), or whose `startedAt` ≥ our start time. Select one; if ambiguous,
   do not bind and log it.
2. **Binding.** Add a `bind(path, why)` method to `TranscriptWatcher`; on
   binding, log `session bound: <sessionId> (<why>)`. Poll the registry no more
   often than `CR_POLL_SEC`. When `sessionId` in the file changes (after
   `/clear` or `/resume`), rebind: `session rebound: <old> → <new> (<why>)`.
3. **`current` behavior.** While a binding exists, `current` equals the bound
   path and `_pick_current` is not called. The heuristic remains only as a
   fallback when the registry is unavailable (older Claude Code), subject to
   the limitations in T04.
4. **`status` from the registry.** Pass `busy/idle` to the controller as an
   additional (not sole) “session is busy” signal:
   `Controller.on_agent_status(status, now)`; `_session_busy` returns
   `"the session reports busy"` when the registry reports `busy` and
   `statusUpdatedAt` is less than 60 seconds old. Verify the semantics live
   (item 6).
5. **Verify live** on Claude Code ≥ 2.1.273 and record the result in this file
   (the “Verified” section): (a) does `sessionId` in `sessions/<pid>.json`
   change after `/clear`, and how long does it take; (b) with
   `--resume`/`--continue`, does the file point to the resumed id; (c) does
   `status` switch `busy↔idle` at turn boundaries; (d) for an alias launch,
   does the pid file belong to the grandchild?
6. `test/fake_claude.py`: write `sessions/<pid>.json` (using
   `CLAUDE_CONFIG_DIR`), and support `/clear` → a new file plus a pid-file
   update.

## Acceptance criteria

- [ ] Unit test `test_transcript.py`: with the registry, `current` does not
      change when our file is silent and a foreign file grows for three
      consecutive polls.
- [ ] Unit test: after `sessionId` changes in the pid file, the watcher rebinds
      to `<new>.jsonl`, the old file is no longer read, and the log contains
      `session rebound`.
- [ ] Unit test: the pid file belongs to a grandchild (alias launch) — binding
      finds it through the parent chain / cwd+startedAt.
- [ ] Unit test: no pid file → fallback heuristic, with
      `no session registry for pid …; falling back to newest transcript` logged
      exactly once.
- [ ] Pty test (`test_pty.py`): two wrappers around two fake_claude processes
      in one project-dir; one has context above the threshold and the other
      below it — only the first receives a fold; the second log has no
      `folding up`.
- [ ] Pty test: `/clear` through a wrapper → `session rebound` in the log;
      unfold goes to the new file (the resume phrase echo is found in
      `<new>.jsonl`).
- [ ] The “Verified” section in this file is filled with the results of item 5
      and the Claude Code version.
- [ ] `docs/context-restart.md`: the “Caveats” paragraph about “another
      session may be the file that grew most recently” is removed or rewritten.
- [ ] `./test/run.sh` is green; `./test/codegraph-sync.sh` has been run.

## Where in the code

`TranscriptWatcher` (1749–1843), `ClaudeAgent` (1479), `main()` — watcher
creation (3953) and the record loop (4117–4157), `Controller.on_context` (2946),
`_session_busy` (3299), `test/fake_claude.py`, `test/test_transcript.py`,
`test/test_pty.py`.

## Verified

Live run against the real `claude` binary, Claude Code **2.1.273** (macOS,
`darwin`), driven on a pty in an isolated scratch cwd never used before,
launched with `CLAUDE*`/`ANTHROPIC*` stripped from the environment (this
audit itself runs inside a live Claude Code session, and a nested `claude`
that inherits `CLAUDE_CODE_CHILD_SESSION` turns its own transcript/session
writing off — worth knowing on its own, but orthogonal to T02, since
agent-retrier never runs nested that way for a real user).

- **(a) `sessionId` after `/clear`.** Changed within 0.3s of the phrase being
  submitted, same pid, same `cwd`, `startedAt` unchanged. `updatedAt` moved,
  `statusUpdatedAt` did not (a `/clear` is not a status transition).
- **(b) `--resume`/`--continue`.** Not exercised live this pass (would need a
  second launch against a session already on disk); the mechanism binds on
  `sessionId` alone regardless of how it got there, so nothing about `bind()`
  is resume-specific. Left `unknown` per the Skeptic note in `Plans.md` —
  does not block Phase 1/2.
- **(c) `status` at turn boundaries.** `idle` at rest; flipped to `busy`
  within 0.5s of a prompt being submitted; back to `idle` within ~2s of the
  turn's own `end_turn`. Confirms item 4's design (an extra signal, correctly
  timed, never the only one `_session_busy` trusts).
- **(d) alias/grandchild launch.** `sh -c "claude; true"` (the same
  fork-without-exec shape a shell alias produces) writes `sessions/<pid>.json`
  at the **grandchild's** pid; no file appears at the launching shell's own
  pid. Confirms the direct-match-then-descendant-scan design in
  `ClaudeSessionRegistry.lookup()` is necessary, not defensive-for-nothing.
- **Field shapes.** `startedAt`/`statusUpdatedAt` are epoch **milliseconds**
  (integers), not ISO strings — `test/fake_claude.py` originally wrote ISO
  strings for these and has been corrected to match. `cwd` in the record is
  always the kernel's **resolved** path (macOS: `/var/folders/...` resolves
  through the `/var` → `/private/var` symlink) — a registry built from
  `os.getcwd()` on the wrapper's own process gets the same resolution for
  free, since neither process ever `chdir`s through the symlink itself; only
  a test comparing against the pre-resolution string needs `os.path.realpath`.
  The real record carries several fields the doc's "Evidence" section did not
  list (`procStart`, `peerProtocol`, `peerFeatures`, `messagingSocketPath`,
  `name`, `nameSource`, `nameSince`, `updatedAt`) — none read by T02, ignored.
- **A bug this live pass found, unrelated to the binary above.** Driving this
  same registry against a real environment (not the isolated one every other
  pty test in `test/test_pty.py` used) exposed that `test_pty.py`'s `Session`
  never set `CLAUDE_CONFIG_DIR`, so before this task every wrapper it started
  read and wrote the **developer's real** `~/.claude/sessions` and
  `~/.claude/projects` — harmless before T02 (transcripts self-isolate by a
  cwd slug unique to each tempdir), but with T02's registry actively scanning
  *every* entry in `sessions/` as a fallback candidate, a stale or
  differently-shaped real entry there raised an uncaught `TypeError` and
  killed the whole wrapper. Fixed two ways: `Session` now gets an isolated
  `CLAUDE_CONFIG_DIR` by default (`test/test_pty.py`), and
  `ClaudeSessionRegistry.lookup()` no longer trusts a candidate's `startedAt`
  without checking its type first (`_is_recent`) — a malformed or
  foreign-format entry now just loses the candidate instead of taking down
  the process reading it.
