# Session budget: pacing the 5-hour limit

[← back to README](../README.md) · [configuration](configuration.md#session-budget)

The wrapper otherwise reacts only once a usage limit has been hit: it waits for
the reset and types `continue`. A session that fans out to many subagents burns
the 5-hour limit in bursts, so it can run out in the middle of its work: the
subagents die mid-turn, their partial output is lost, and the head session has
written no handoff.

With `CR_SESSION_BUDGET` set, the wrapper paces the limit instead:

- **Slow.** When the pace says the window would reach the target before it
  resets, the session is asked to start no more agents than the pace allows.
  It is asked once; again only when the pace has gone back under the target
  or the number of agents it allows has at least halved.
- **Freeze.** At the target, or when the target is about to be reached (within
  `CR_BUDGET_FREEZE_MIN` minutes), the session is frozen. It is asked to stop
  its running agents at a safe point and to write a handoff, the same handoff
  a context restart writes (`CR_HANDOFF_MSG`, with its marker). Nothing is
  cleared. Once the handoff is written and the session is quiet, it waits for
  its window to reset, then `CR_RESUME_MSG` is typed and the session carries on.
  A freeze happens once per 5-hour window.

Claude only. Codex has no statusline to report its limit in, so codex sessions
are not paced; the wrapper says so once in its log.

## Where the numbers come from

Claude Code hands its statusline command `rate_limits.five_hour.used_percentage`
and `resets_at`. The wrapper's statusline proxy (`--cr-statusline`, see
[context restart](context-restart.md#1m-vs-200k-the-statusline-proxy)) records
them, and the proxy is switched on for a session that has a budget, even one
without a context restart. `CR_STATUSLINE_PROXY=0` turns it off, and then there
are no samples and so no budget.

Claude draws its statusline after its own updates, not continuously, so between
two samples the percent is estimated from the tokens the session and its
subagents have written, weighted (output 5, input 1, cache writes 1.25, cache
reads 0.1). The estimate is calibrated from the last pair of samples that moved.

The pace is a least-squares line through the points of the last
`CR_BUDGET_WINDOW_SEC` seconds, and the forecast is where that line stands at
the reset:

- `rate` is the slope, in percent a minute;
- `forecast_at_reset = percent + rate × minutes to reset`;
- `minutes_to_target = (target − percent) / rate`;
- `max_agents = floor(max(active, 1) × safe_rate / rate)`, where `safe_rate` is
  the pace that would just reach the target at the reset and `active` is the
  number of subagent transcripts written in the last two minutes.

The state is `freeze` when the percent is at the target or `minutes_to_target`
is under `CR_BUDGET_FREEZE_MIN`; `slow` when `forecast_at_reset` is over the
target; otherwise `ok`.

Only samples of the window being paced count. A sample that names an older
reset is dropped, and a newer one starts that window afresh.

## The messages

Both are typed through the wrapper's usual safe path: not over a draft in the
prompt box, not while a person is typing, and not while the session is running
a turn. Each is one line.

| message | default | placeholders |
|---|---|---|
| slow | `Session budget: {percent}% of the 5-hour limit used, pace {rate}%/min, forecast {forecast}% at the reset ({resets}); target {target}%. Reduce parallel work to at most {agents} agents: start no new ones until the pace drops.` | `{percent} {rate} {forecast} {resets} {target} {agents}` |
| freeze | `Session budget: the {target}% target will be reached in about {minutes} min (reset at {resets}). Start no new agents and stop the running ones at a safe point.` followed by the handoff phrase | `{target} {minutes} {resets}`, and the handoff phrase's own `{file}`, `{marker}` |
| cancel | `The session-budget freeze was cancelled — the handoff is not needed now. Continue with what you were doing before it was requested.` | none |

`{rate}` is percent a minute, `{forecast}` percent at the reset, `{minutes}`
minutes to the target, `{resets}` the local time of the reset (HH:MM),
`{agents}` the number of agents the pace allows. `{file}` is the handoff file
and `{skill:NAME}` expands to `/NAME`, as in the other phrases. Override them
with `CR_BUDGET_SLOW_MSG`, `CR_BUDGET_FREEZE_MSG` and `CR_BUDGET_CANCEL_MSG`. The
cancel phrase is typed only when a freeze is called off after the session has
seen its freeze phrase; an aborted context restart keeps `CR_CANCEL_MSG`, since
a freeze is no restart and the session should not be told it was one. The handoff phrase always
follows the freeze message, because the freeze is only complete with a verified
handoff.

The log records each state change, for example:

```
budget: ok→slow 91.4% 0.42%/min forecast 104%
```

## When a limit is hit anyway

A freeze is a restart without a `/clear`, so a usage limit that lands in the
middle of one is handled the same way: the wait is served, and the unfold is
typed once the reset has come, instead of `continue`. A limit that lifts before
the window's reset keeps the session held until the reset.

A freeze that fails (the handoff is never written, or the unfold never reaches
the session) is called off and said so in the badge and log. If the session had
seen the freeze phrase, it is also told the freeze is off, with
`CR_BUDGET_CANCEL_MSG`; if not, nothing more is typed. A freeze is not retried
in the same window; the existing limit wait then takes over if the limit is hit.

## The forecast file

With `CR_FORECAST_FILE` set, the wrapper writes the forecast there every time it
recomputes it, which is every `CR_POLL_SEC` while there is one. The write is
atomic (a temporary file in the same directory, then a rename). Nothing is
written without the setting, and nothing is written while there is no forecast:
no sample yet, or the window is over.

Schema 1 (`"v": 1`), one JSON object:

```json
{"v":1,"agent":"claude","session":"<id>","updated_at":1789000000,
 "percent":91.4,"rate_per_min":0.42,"resets_at":1789001800,
 "forecast_at_reset":104.2,"minutes_to_target":13.3,"target":97,
 "state":"ok|slow|freeze","max_agents":3,"active_agents":7,
 "seven_day_percent":40.1}
```

- `session` is claude's own session id, from its statusline.
- `minutes_to_target`, `rate_per_min`, `forecast_at_reset`, `max_agents` and
  `seven_day_percent` are `null` when there is no value to give.
- `resets_at` and `updated_at` are Unix seconds.

**Rule for readers:** a file that is missing, does not parse, or whose
`updated_at` is more than 300 seconds old says *no budget information*. It never
means *freeze*. The wrapper does not know or care who reads the file.

## Settings

| variable | default | |
|---|---|---|
| `CR_SESSION_BUDGET` | off | target, in percent of the 5-hour limit (above 0 and up to 100). Empty or `0` is off |
| `CR_FORECAST_FILE` | none | where the forecast is written (schema 1) |
| `CR_BUDGET_WINDOW_SEC` | `600` | how far back the pace is fitted |
| `CR_BUDGET_FREEZE_MIN` | `5` | freeze when the target is this many minutes away |
| `CR_BUDGET_SLOW_MSG` | see above | the slow message |
| `CR_BUDGET_FREEZE_MSG` | see above | the freeze message, before the handoff phrase |
| `CR_BUDGET_CANCEL_MSG` | see above | the phrase that calls off a freeze the session had seen |

The resume waits for the reset plus `CR_MARGIN_SEC`, as a usage limit does; the
handoff uses `CR_HANDOFF_MSG`, `CR_HANDOFF_FILE`, `CR_RESUME_MSG` and their
timeouts.

## Limits

- The pace is only as fresh as Claude's statusline. Between two statusline
  updates the estimate from tokens is a guess, and a session that has just
  resumed after a long pause has no sample until Claude draws its next statusline.
- The freeze asks the model to stop its agents; the wrapper does not kill them.
- A subagent started before the wrapper's first look is not counted towards the
  estimate, and a session resumed onto an old transcript counts only from the
  point the wrapper binds to it.
