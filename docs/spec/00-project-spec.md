# agent-retrier — product spec

Created 2026-09-18 as the root spec for `harness-plan`. Source: audit of
`docs/backlog/README.md` (2026-09-18, agent-retrier 1.11.0 + 4 unreleased
commits). Precedence: this file > Plans.md.

## Purpose

A wrapper around `claude`/`codex` that keeps a long-lived agent session
alive through proactive context restarts (fold → `/clear` → unfold), without
silently leaving the user stranded during disconnects, races, or multiple
sessions in one directory.

## Users And Workflows

A single user may run multiple sessions (claude and/or codex) with a shared or
different `cwd` values. The wrapper must: restart the context before the agent
compacts it itself; reliably restore the working state (unfold) after `/clear`;
never confuse signals from its two processes; and make every failure visible
(badge/notify/log), rather than silent.

## Core Rules

The system consists of four independent blocks, each with its own invariant.

### 1. Session identity

The wrapper forks exactly one agent process and must know deterministically,
at every moment, which transcript that process is writing—including its move
to a new file after `/clear`. All controller signals (usage, stop_reason,
“session busy,” phrase echoes, “session responding again”) are read **only**
from the bound file. A file change may happen only through an explicit
re-binding event, never through the heuristic “this file grew most recently”
while a stronger identity source exists.

- claude: `~/.claude/sessions/<pid>.json` → `sessionId` → `<project>/<sessionId>.jsonl`.
- codex: a rollout created after startup in our `cwd`, with
  `thread_source=user`, confirmed by an echo of our nonce.
- The heuristic “most recently grown file” is a fallback only when no strong
  source exists, and every such switch is visible in the log. A strong source
  that has not answered yet is not the same as none: until claude has had
  `CR_SESSION_GRACE_SEC` to write its session file, no transcript is followed
  at all, and a session file is ours only by the pid that wrote it (T32).

### 2. Model profile

For each (agent, slug) pair, the following are known: native window, effective
window of the current session (`[1m]`, env limits, rollout), the agent's own
compaction point, and the restart threshold in tokens. Trust sources, in
descending order: explicit user setting → what the agent itself reports
(statusline/rollout) → profile table in code → local agent cache → network
lookup → family-based estimate. An estimate is marked as such and self-corrects
in both directions: usage above the window → larger window; the agent compacted
the context itself at N → window no larger than N. A model change within a
session creates a new (agent, slug) pair on the next turn, with the threshold
recalculated and a log entry.

### 3. Restart machine

Fold → check → `/clear` → unfold, guarantees:

- **G1** fold is requested only based on figures from the bound transcript.
- **G2** the fold phrase is considered delivered only upon echo (nonce in the
  user line).
- **G3** `/clear` is sent only after a verified handoff.
- **G4** after `/clear`, unfold must be sent and confirmed by echo; a timeout
  must not end in a silent abort.
- **G5** an abort after a delivered fold phrase must “unstick” the model (it was
  already told “stop, start nothing new”—this must be explicitly cancelled).
- **G6** input accounts for the agent's grammar (`/` for both, `$skill` for
  codex, `@`/`#`).
- **G7** protection against “looping” restarts on a small window (headroom
  after restart).
- **G8** every failure is visible in the badge, not only in the log.

### 4. Limits and stalls

“Session responding again” and “our phrase accepted” are also path-bound (the
rule from block 1); otherwise, this backlog does not change the logic.

## Data And Contracts

- `~/.agent-retrier/log` — shared machine-wide; every line must carry the tag
  `[cr <pid> <agent>]` (T01) for investigating incidents involving multiple
  sessions.
- `~/.agent-retrier/sessions/<pid>.json` — wrapper registry for handoff-file
  uniqueness (T05).
- `MODEL_PROFILES` (in code) — the sole source of the default window/threshold/
  compaction for each (agent, slug) pair; overridden through the chain in block
  2.
- `~/.agent-retrier/folds.json` — fold-turn cost history for adapting the
  codex reserve (T22).

## Non-Goals

- We do not override the communication protocol with the agent provider
  (claude/codex CLIs remain the source of truth for their UI and commands).
- We do not add a UI/web interface; the badge and notify inside the terminal
  are the only user-signal channel.
- We do not introduce a shared lint/formatter baseline for bash+Python heredoc
  as part of this backlog—it was not requested, and backlog tasks do not change
  code style.

## Open Decisions

- T15 (live investigation of codex `/clear` + `$skill`) has not been conducted
  — exact input timings in T16/T17 depend on it. Until it is conducted, T16/T17
  use the preliminary recipes from T15 as a hypothesis marked `unknown`.
- T20 (statusline proxy) requires live verification: whether `--settings` is
  merged with or replaces user settings. Until verified, the mechanism remains
  behind `CR_STATUSLINE_PROXY`, with a safe default of disabled until confirmed
  (see task).
- The order of epics A/B before release (T24) may turn the release into a major
  one—the decision is deferred until it is clear which tasks are included.

## Links

- `docs/backlog/README.md` — original audit, evidence, and recommended order.
- `docs/backlog/T01..T26-*.md` — detailed task files (problem/evidence/what to
  do/AC/where in code); the Plans.md task states the DoD briefly, with details
  there.
- `CLAUDE.md` — map of the `agent-retrier.sh` file, CodeGraph mirror, and test
  runner.
