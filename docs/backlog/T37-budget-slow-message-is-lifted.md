# T37 — Session budget: the agents allowed are those that made the pace, and a slow message is lifted

Priority: P0 · Epic: limits · Depends on: T35, T36 · Size: S · Status: done (unreleased)

## Problem

A slow message told a session to cut down to N agents "until the pace drops".
The session did, and was then told ever fewer agents were allowed, down to 0,
and nothing ever told it the pace had dropped. It sat idle with 90% of its
5-hour limit unused.

## Evidence

2026-10-28, coreward session, 3.1.1 (`~/.agent-retrier/log`, cr 52667, target 99%):

```
12:09:13 budget: ok→slow 5.5% 0.55%/min forecast 99%
12:09:39 slow message typed (at most 6 agents while the pace holds)
12:14:20 slow message typed (at most 3 agents while the pace holds)
12:16:26 slow message typed (at most 1 agents while the pace holds)
12:18:31 slow message typed (at most 0 agents while the pace holds)
12:18:35 budget: slow→ok 9.2% 0.55%/min forecast 98%
```

At 12:28 the session was at 10% with 2h39m to the reset and waiting "for the
budget to allow". `max_agents` divided the pace by the subagents written in the
last two minutes: stopped agents left that count in two minutes, while the pace
they had made stayed in the fit for about ten, so every two minutes the count
fell and the message was typed again with half as many. And `slow→ok` typed
nothing at all.

## What was done

- `SessionBudget._pacing_agents`: the agent count at each read, weighted like
  the points, is what `max_agents` divides by.
- `CR_BUDGET_CLEAR_MSG`: once a slow message is in force and the forecast is 10
  points or more under the target, the session is told the limit on agents is
  lifted. A freeze ends a slow message too (the unfold says to carry on).
- Tests: the agents stopped on a slow message do not collapse `max_agents`; the
  clear message is typed once, not for a forecast just under the target, and
  never without a slow message before it.
