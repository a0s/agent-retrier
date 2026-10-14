# T03 — Codex: binding rollouts and `codex resume` for old sessions

Priority: P1 · Epic: A · Depends on: T01, T04, T06 · Size: L

## Problem

`CodexAgent.keep` (agent-retrier.sh:1528) filters out subagent rollouts and other
`cwd` values, but two codex sessions in the same directory are indistinguishable,
so the same `_pick_current` heuristic as for claude (T02) is used afterward.
Furthermore, `CodexAgent.paths` (1518) scans only
`sessions/<yesterday|today|tomorrow>/`, while `codex resume` appends to the **old**
rollout located in the directory for its creation day: a session resumed two days
later is completely invisible to the wrapper — neither limits nor restart apply.

## Evidence

- `session_meta` for a rollout: `id, cwd, source, thread_source, originator,
  cli_version, context_window, git, …` — no pid.
- `~/.codex/thread-writer-locks/` exists (empty without live sessions) — probably
  a lock on the thread with the writer's pid; `~/.codex/session_index.jsonl` — id
  + name + `updated_at`.
- `lsof -p <pid>` returns nothing for claude (append-open); check it for codex
  (Rust).

## What to do

1. **Investigation (record in “Checked”)**, on a live codex 0.154, with a cheap
   prompt: (a) whether the codex process keeps the rollout file open (`lsof -p`);
   (b) what appears in `thread-writer-locks/` during a session (name, contents,
   pid?); (c) whether the rollout is created at startup or on the first message;
   (d) what happens to the rollout after `/new` and `/clear` (a new file? when?).
2. **Binding** (based on result 1, in order of preference): lock file with pid →
   `lsof` → heuristic “file created after our startup, our `cwd`,
   `thread_source=user`” + mandatory echo nonce confirmation (T06). Until
   confirmation is available, the controller operates in “candidate” mode: usage
   is read, but fold is not sent if there is more than one candidate in the
   directory.
3. **Old sessions.** `paths()` additionally returns files modified after wrapper
   startup from the entire `sessions/` tree (traverse by the mtime of
   year/month/day directories, no more than once every 30 s), so that
   `codex resume <old>` is visible.
4. Account for `--cd` (already present, `codex_cwd`).

## Acceptance criteria

- [ ] The “Checked” section is filled with the results of item 1 and the codex
      version.
- [ ] Unit test: two user rollouts in the same `cwd`, created after startup; ours
      is confirmed by an echo nonce → `current` remains fixed on it, and growth of
      the other does not switch it.
- [ ] Unit test: a rollout in `sessions/2026/08/01/`, modified after startup
      (simulating `codex resume`), is included in `paths()` and read.
- [ ] Unit test: a subagent rollout (`thread_source=subagent`) is still never
      bound.
- [ ] Pty test with `fake_codex.py`: two wrappers around two fake_codex processes
      in one directory — only the one whose context is above the threshold
      receives fold.
- [ ] `docs/codex.md` (“Rollouts: what the wrapper reads”) describes the
      mechanism.
- [ ] `./test/run.sh` is green; `./test/codegraph-sync.sh` has been run.

## Where in the code

`CodexAgent` (1497–1585), `codex_records` (1375), `TranscriptWatcher` (1749),
`test/fake_codex.py`, `test/test_codex.py`.

## Checked

Filled in by T34, on codex-cli 0.161.0 and the owner's own `~/.codex`, read
only. No codex was running, so (a) is from the binary and the files, not
`lsof`.

- (a)/(b) `thread-writer-locks/<thread id>.lock` is empty, made when the
  thread is made, and held with `flock` by the writing process: it names the
  thread, and only `lsof`/`/proc` would name the holder. Not used.
- (c) The rollout is made at the thread's **first message**, not at startup:
  `rollout-2026-10-09T13-56-55-…` (the creation time, in local time, and the
  thread's UUIDv7 id) was born at 13:58:56. Its lock file, and codex's log
  row for the thread, at 13:56:55.
- (d) `/clear` and `/new` make a new thread — a new id, a new lock, a log row
  at once — and its rollout at its first message (11:57:21 → 12:54:50 in one
  of the owner's sessions).
- What does name the writer: `$CODEX_HOME/logs_2.sqlite`, whose rows carry
  `thread_id` and `process_uuid = pid:<pid>:<uuid>`, written as a thread is
  made (`rpc.method="thread/start"`). That is what binds a codex session now
  (`CodexSessionRegistry`, T34).

What item 2 here asked for — candidate mode, held until the echo of the
fold phrase confirms a rollout — shipped, and broke in the field in both
directions (T32 "Left open", T34): with our rollout not yet made, the
neighbour's was a single candidate and was folded on; with two, the hold had
nothing to lift it but the phrase only a fold types. It is now the fallback
for a codex whose log does not name processes, and gives way after
`Controller.PROOF_WAIT`.
