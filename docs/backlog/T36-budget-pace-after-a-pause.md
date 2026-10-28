# T36 — Session budget: a pause no longer leaves a few seconds to decide the pace

Priority: P0 · Epic: limits · Depends on: T35 · Size: S · Status: done (unreleased)

## Problem

At 1% of the 5-hour limit the budget asked a session for 0 agents three times in
twenty minutes and then froze it until the reset, 4.5 hours away. Each forecast
was back to `ok` within seconds; the freeze was not.

## Evidence

2026-10-28, coreward session (`~/.agent-retrier/log`, cr 28288):

```
10:12:54 ok→slow   1.0%  18.64%/min  forecast 5351%
10:13:33 slow→ok   1.1%   0.07%/min  forecast 21%
10:23:34 ok→slow   1.0%  11.44%/min
10:33:44 ok→freeze 1.0%  58.18%/min  forecast 15493%
10:33:54 freeze→ok 1.0%  -0.06%/min
10:34:22 handoff written; holding the session until the reset at 15:00
```

The same at 05:25 (617%/min) and 05:36 (48%/min) in the window before. The
statusline reports `five_hour_percent` as a whole number (`"five_hour_percent": 1`
in the status file), and the token estimate between two reports runs a tenth
past it, so the points zig-zag 1.0 → 1.1 → 1.0. The pace was a least-squares
line over the last `CR_BUDGET_WINDOW_SEC` (600 s) only: any pause longer than
that emptied the window, and the next turn's two or three points, seconds apart,
were the whole fit. The trips come every ~10 minutes, the window's length.

## What was done

- `SessionBudget._rate` fits every point of the window, weighted by
  `exp(−age / CR_BUDGET_WINDOW_SEC)`, plus the window's start (0% five hours
  before the reset). Points older than ten time constants are let go.
- `CR_BUDGET_WINDOW_SEC` is now that time constant, default 300.
- Tests: a step of the whole-number percent seconds after a pause, and the
  estimate zig-zagging around a report, are no pace; recent points still lead.
