# Backlog: context restart, session identity, models

Compiled on 2026-09-18 following a full audit of `agent-retrier.sh` (1.11.0 +
4 unreleased commits), `~/.agent-retrier/log`, real transcripts from
Claude Code 2.1.273, and codex-cli 0.154 rollout files.

Each task is a separate `T##-*.md` file with the sections “Problem / Evidence /
What to do / Acceptance criteria / Where in the code”. A task is considered
complete only when **all** AC items are satisfied and `./test/run.sh` passes.
After modifying `agent-retrier.sh`, always run `./test/codegraph-sync.sh` (see
`CLAUDE.md`).

## Logical model (how it should work)

The wrapper consists of four independent blocks, each with its own invariant:

1. **Session binding.** The wrapper forked exactly one agent process and must
   deterministically know which transcript *it* is writing at every moment —
   including after `/clear`, when the agent moves to a new file. All controller
   signals (usage, stop_reason, “session busy”, echoes of our phrases, “session
   is responding again”) are read **only** from the bound file. The file changes
   only through an explicit rebinding event, never because “this file grew last”.
   - claude: `~/.claude/sessions/<pid>.json` → `sessionId` → `<project>/<sessionId>.jsonl`.
   - codex: rollout created after startup, in our `cwd`, `thread_source=user`,
     confirmed by an echo of our nonce (the first thing we print there).

2. **Model profile.** For each (agent, slug) pair, the native window, the
   *effective* window for that session (accounting for `[1m]`, env, and rollout),
   the agent’s own compaction point, and the restart threshold in tokens are
   known. Sources, in order of trust: explicit user setting → what the agent
   itself reported (statusline / rollout) → the table in the file → the agent’s
   local cache (`models_cache.json`) → network lookup → family-based estimate.
   An estimate is marked as such and self-corrects in both directions (usage
   above the window → larger window; agent compacted context at N → window no
   larger than N). Switching models within a session is simply a new (agent,
   slug) pair on the next turn, with recalculation and a log entry.

3. **Restart machine.** Fold → check → `/clear` → unfold, with guarantees:
   - G1: fold is requested only from the numbers in the bound transcript;
   - G2: the fold phrase is considered delivered only after its echo (nonce in the user line);
   - G3: `/clear` is sent only after a verified handoff (as now);
   - G4: after `/clear`, unfold **must** be sent and its echo confirmed; a timeout
     here cannot end in a silent abort;
   - G5: an abort after a delivered fold phrase must “unstick” the model (it was
     told “stop, start nothing new”);
   - G6: input accounts for the agent’s grammar (`/` for both, `$skill` for codex);
   - G7: protection against restart loops on a small window;
   - G8: every failure is visible in the badge, not only in the log.

4. **Limits and stalls.** No substantive changes, but “session is responding
   again” and “our phrase was accepted” are also path-bound (see 1).

## What is currently broken (briefly, with evidence)

| # | User symptom | Root cause | Evidence |
|---|---|---|---|
| 1 | fold arrives “far below the limit” | `TranscriptWatcher._pick_current` switches “its” file to any file that grew while ours was silent for 2 s; `on_context` uses numbers from another session | log 2026-09-17 22:37–22:45: model jumps opus↔sonnet every few seconds; fold at 516k in session `4a2c66dc` (opus, max 298k) using numbers from `b5d84050` (sonnet, 550k) |
| 2 | two sessions fold into one RESUME.md simultaneously | same issue + shared `CR_HANDOFF_FILE` | logs 22:45:36 and 22:45:38, two nonces; the second fold “does not end with HANDOFF-081d06b8” |
| 3 | no unfold after fold | (a) another transcript holds the “still writing” gate → 900 s → abort in CLEARED; (b) codex: resume phrase `$supervisor …` was never sent (no new rollout was created after `/clear`) | log 2026-09-15 05:23–05:26 “left no trace” ×3, permanent abort; no rollout after 03:23Z in `~/.codex/sessions` |
| 4 | session “dies” after abort | abort prints nothing, while the model has already been told “wrap up, start nothing new” | `_abort_restart` by design |
| 5 | restarts every 40 minutes | 200k window; after restart, already 57k of the 102k threshold is used | log 2026-09-04 16:22 → 16:36 → 17:19 → 18:06 |
| 6 | false “context restarted: … down to 0” is possible | `model="<synthetic>"` rows with zero usage produce `tokens=0` | voxik transcripts contain such rows |

## Recommended order of implementation

First fix observable bugs and make logs readable, then implement restart-machine
guarantees, then models, and then everything else.

0. **T27** model and effort of subagents over the tree — placed first at the
   user’s explicit request: `harness-loop` sessions spend money on the agent
   tree, about which the TUI reports nothing, and this is the only backlog task
   answering “where is the quota going right now”. Technically independent of
   everything (the overlap with T18 only shortens the label term; see “Open
   questions” in T27).
1. **T01** log with wrapper identifier — without it, incidents involving two sessions cannot be analyzed
2. **T02** claude: binding through `sessions/<pid>.json`
3. **T04** controller: all signals only from the bound transcript
4. **T05** unique handoff file per session
5. **T06** echo verification of the fold phrase (nonce) + binding confirmation
6. **T10** ignore `<synthetic>`/zero rows
7. **T11** collapsing assistant rows must not lose `end_turn`
8. **T08** unfold is mandatory after `/clear`
9. **T09** cancel phrase after abort
10. **T12** handoff verification latch
11. **T15** codex: live investigation of `/clear` + `$skill`
12. **T16** per-agent input grammar
13. **T17** per-agent command defaults
14. **T03** codex: rollout binding (+ `codex resume` for old sessions)
15. **T18** model profile table
16. **T19** fallback window estimate
17. **T21** real-time model switching
18. **T20** effective claude window (`[1m]` vs 200k)
19. **T13** restart-loop protection
20. **T14** failure visibility in badge
21. **T22** codex: threshold semantics and reserve
22. **T07** passthrough of non-session claude subcommands
23. **T23** log rotation
24. **T24** CHANGELOG + release
25. **T25** documentation after A/B
26. **T26** test infrastructure for two sessions

## Status (2026-09-19)

**This backlog is closed.** All 26 numbered tasks above, plus T27/T28/T29 from
the separate subagent-rendering investigation, are implemented and merged;
T01–T14 and T18–T25 shipped as v1.12.0, and T15/T16/T17 (Phase 3, the last
open epic) landed afterwards in 9a93c2d.

T15 was the long-blocked one — a live codex TUI investigation nobody wanted to
hand to an unsupervised subagent. It was finally run in-session against real
codex-cli **0.155.1** on the cheapest model available, and it **disproved its
own premise**: `$name` opens no popup in codex (it is a model-level
convention), `@file` is plain text too, and `/clear`, `/new`, Cyrillic and a
plain phrase right after `/clear` all work on the first try. The 2026-09-15
incident it was written to explain almost certainly predates — and was fixed
by — T02–T06's transcript binding. One genuinely new hazard came out of it:
codex silently drops an unrecognized `/word` without ever sending it as text,
which is why `{skill:NAME}` expands to `$NAME` there and never `/NAME`.
Full input-by-input table in
[T15's Results section](T15-codex-unfold-live-investigation.md#results).

The only thing left anywhere is the optional **T26-live** (`./test/run.sh
--live-codex`), which is gated on explicit confirmation because it
deliberately spends quota, and which nothing else depends on.

## Index

| ID | Task | Priority | Epic |
|---|---|---|---|
| [T27](T27-subagent-model-overlay.md) | Model and effort of each subagent over the on-screen tree | P0 | F |
| [T01](T01-log-wrapper-id.md) | Wrapper identifier in every log line | P0 | A |
| [T02](T02-claude-session-binding.md) | Claude: transcript binding through `~/.claude/sessions/<pid>.json` | P0 | A |
| [T03](T03-codex-session-binding.md) | Codex: rollout binding and `codex resume` for old sessions | P1 | A |
| [T04](T04-path-bound-signals.md) | Controller: all signals only from the bound transcript | P0 | A |
| [T05](T05-unique-handoff-file.md) | Unique handoff file per session | P0 | A |
| [T06](T06-handoff-echo-binding.md) | Echo verification of the fold phrase and binding confirmation | P0 | A |
| [T07](T07-claude-passthrough.md) | Passthrough of non-session claude subcommands | P2 | A |
| [T08](T08-unfold-is-owed.md) | Unfold is mandatory after `/clear` | P0 | B |
| [T09](T09-cancel-after-abort.md) | Cancel phrase after a delivered fold | P0 | B |
| [T10](T10-ignore-synthetic-rows.md) | Ignore `<synthetic>` and zero-usage rows | P0 | B |
| [T11](T11-row-collapse-keeps-end-turn.md) | Row collapsing does not lose `end_turn` or sidechain | P0 | B |
| [T12](T12-handoff-verification-latch.md) | Handoff verification latch | P1 | B |
| [T13](T13-restart-thrash-guard.md) | Restart-loop protection | P1 | B |
| [T14](T14-failure-visible-in-badge.md) | Failures visible in the badge | P1 | B |
| [T15](T15-codex-unfold-live-investigation.md) | Codex: live investigation of `/clear` + `$skill` | P0 | D |
| [T16](T16-agent-input-grammar.md) | Per-agent input grammar (`/`, `$`, `@`) | P0 | D |
| [T17](T17-agent-command-defaults.md) | Per-agent command and skill-syntax defaults | P1 | D |
| [T18](T18-model-profiles-table.md) | Model profile table (window + threshold) for both agents | P1 | C |
| [T19](T19-window-fallback-estimate.md) | Fallback window estimate for an unknown model | P1 | C |
| [T20](T20-claude-effective-window.md) | Claude: effective session window (`[1m]` vs 200k) | P1 | C |
| [T21](T21-model-switch-realtime.md) | Real-time model switching within a session | P1 | C |
| [T22](T22-codex-threshold-semantics.md) | Codex: threshold semantics, reserve, and its measurement | P2 | C |
| [T23](T23-log-rotation.md) | Log rotation | P2 | E |
| [T24](T24-changelog-release.md) | CHANGELOG for unreleased commits and release | P2 | E |
| [T25](T25-docs-after-binding.md) | Documentation after epics A/B | P2 | E |
| [T26](T26-two-session-test-infra.md) | Test infrastructure: two sessions in one project | P1 | E |
| [T30](T30-clear-done-by-hand.md) | A clear done by hand while the fold waits | P0 | B |
| [T31](T31-kitty-keys-in-the-draft-gate.md) | The draft gate under the kitty keyboard protocol | P0 | B |

Epics: A — session identity; B — restart-machine guarantees; C — models and
windows; D — per-agent commands and input; E — miscellaneous; F — subagent
observability.
