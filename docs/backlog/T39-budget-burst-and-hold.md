# T39 — Session budget: a burst fades into the window's average, and a state must hold

Priority: P1 · Epic: limits · Depends on: T38 · Size: S · Status: done (unreleased)

## Problem

After T36–T38 the pace was right, but the forecast carried the pace of a burst
all the way to the reset, and every whole-number step flipped it across the
target. The session was held back on windows that ended far under the target,
and told slow and clear by turns.

## Evidence

2026-10-28, coreward, 3.1.3 (cr 41075, target 99%), against the statusline's
own history (`~/.claude/usage-log.tsv`):

| window | at its end | forecast said |
|---|---|---|
| 15:00–20:00 | 57% | 102% at 15:10, 109% at 18:01 |
| 20:00–01:00 | 60% | 102% at 21:47 |

18:01–18:13: slow, clear, slow, clear, slow, clear — six messages in twelve
minutes. 18:06:01 clear at forecast 89%, 18:06:11 slow at 104%.

## What was done

- `SessionBudget.snapshot`: the pace of the moment holds for
  `CR_BUDGET_BURST_MIN` (45), then the window's average (percent over the time
  since it began, over no less than the burst). `minutes_to_target` and
  `max_agents` follow the same line; the agents scale the burst only.
- `Controller._tick_budget`: a state says nothing until it has held
  `CR_BUDGET_HOLD_SEC` (180); the target itself reached freezes at once.
- Replayed over `usage-log.tsv` (26–28.10): the two windows above get no
  message (3.1.3: 10 and 2); the windows of 26–27.10 that ended at 87–97%
  without a budget get 1–8 (3.1.3: 11–22), none a freeze.
