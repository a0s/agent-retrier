# T38 — Session budget: the pace is the move over a horizon, not a fit to the latest points

Priority: P0 · Epic: limits · Depends on: T35, T36, T37 · Size: S · Status: done (unreleased)

## Problem

A freshly started wrapper allowed 0 agents at 11% of the limit, three hours
into the window, a minute after it started.

## Evidence

2026-10-28, coreward session, 3.1.2 (`~/.agent-retrier/log`, cr 69907, target 99%):

```
13:02:10 session budget armed: target 99% …
13:03:38 budget: ok→slow 11.8% 0.98%/min forecast 126%
13:04:03 slow message typed (at most 0 agents while the pace holds)
```

The session's own meter said 11.0%, 0.03%/min at the same time. The wrapper
had no samples of its own yet; claude's whole-number percent stepped from 11
to 12 within its first minute, and the token estimate between reports was
calibrated on that one step. T36's weighted fit gave the window's start (0%,
three hours back) a weight of e^-37, so the fit was that minute alone.

## What was done

- `SessionBudget._rate`: the pace is the move of the reported percent over the
  last `CR_BUDGET_WINDOW_SEC` (default 900), read off straight lines between
  samples, 0% at the window's start. Without samples that far back, the line
  from the window's start to the first sample stands in. The whole-number error
  is at most one point over the horizon.
- The token estimate moves the percent shown only, never past the next whole
  number, and is not a point of the pace.
- `max_agents` divides by the mean agent count over the same horizon.
