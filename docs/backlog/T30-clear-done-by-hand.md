# T30 — A clear done by hand while the fold waits

Priority: P0 · Epic: B · Depends on: T02, T08 · Size: S · Status: done (3.0.2)

## Problem

Once the handoff is verified (`HANDOFF_OK`, badge "folded"), our `/clear` waits
for the human gates. If the person gets there first — types `/clear` and points
the fresh session at the handoff themselves — the wrapper never notices: the
rebind to a new `sessionId` was only ever read as proof of *our* `/clear`
(`_check_clear`, `CLEAR_SENT`). Once the gate opened, `_send_clear` cleared the
session the person had just started and typed the resume phrase over it.

## Evidence

`~/.agent-retrier/log`, 2026-10-05, wrapper 56371 (openhopper.app, Claude Code
2.1.283):

```
20:26:50  handoff accepted … turn closed with end_turn
20:26:50  restart step held: unsent text in the prompt box      (T31)
20:33:16  session rebound b1ae85ba → 5b15d237 (pid file)         ← their /clear
20:34:22  handoff verified; clearing the context with /clear     ← ours, on top
20:34:33  unfolding from scratchpad/RESUME-05d8704b.md
```

Transcript `5b15d237`: the person's `/supervisor продолжи …` turn is running
when our `/clear` is enqueued (18:34:23Z) and runs as soon as it ends.

## What was done

1. `bind_transcript` → `_overtaken`: a rebind by **session identity** (claude's
   registry, never the growth heuristic, never our own fold echo) while at
   `HANDOFF_OK` — or at `HANDOFF_SENT` with a handoff that already passes every
   layer — goes straight to `CLEARED` with `_cleared_by_hand`, skipping our
   `/clear`. At `HANDOFF_SENT` with an unfinished handoff the restart is dropped
   silently: the fold was asked of a conversation that no longer exists.
2. The transcript parser reports a person's own prompt (`turnOrigin: "human"`,
   not `isMeta`, not a sidechain, not one of our echoes) as `kind="prompt"`.
   Peer messages, subagent hand-backs and the `/clear` command row carry no
   such origin.
3. `on_human_prompt`: such a prompt in a session cleared by hand ends the
   restart — they carried on themselves, nothing is unfolded.
4. A session cleared by hand is unfolded only once its fresh transcript is
   quiet too (`session_gate=True`), not just the keyboard.

## Acceptance criteria

- [x] `TestAClearDoneByHand` in `test_controller.py`: no `/clear` ever follows
      theirs; their prompt ends the restart; left alone, the session is
      unfolded; a turn of theirs holds the unfold; a heuristic rebind is not a
      clear; a fold cut short is dropped without a cancel phrase.
- [x] `test_transcript.py`: human prompt → `prompt`; peer message, `/clear`
      row, `isMeta` skill body → nothing; our own phrase → `echo`.
