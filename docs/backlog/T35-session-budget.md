# T35 — Session budget: pace the 5-hour limit instead of hitting it

Priority: P1 · Epic: limits · Depends on: T20 (statusline proxy) · Size: L · Status: open

## Problem

The wrapper only reacts once a usage limit has been hit: it waits for the reset
and types `continue`. A session that fans out to many subagents burns the
5-hour limit in bursts and hits it mid-work: subagents die in the middle of a
turn, their partial work is lost, and the head session has written no handoff.
The owner wants to come as close to the limit as possible without touching it,
then freeze and continue after the reset.

## Evidence

2026-10-26, coreward session: at 97% of the 5-hour limit the owner had to stop
ten running subagents by hand. Claude Code already hands every statusline
command `rate_limits.five_hour.used_percentage` and `.resets_at` (and
`seven_day`), and the wrapper already receives that blob through the T20
statusline proxy (`run_statusline_proxy`, `on_status`). Subagent transcripts are
on disk next to the session's: `~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl`,
each assistant line with `message.usage`.

## What to do

Opt-in, claude only (codex: out of scope, note it in the docs). Off unless
`CR_SESSION_BUDGET` is set; with it unset nothing below runs and nothing changes.

1. **Settings** (bash defaults block and `cfg` in the Python, documented in
   `docs/configuration.md`):
   - `CR_SESSION_BUDGET` — target percent of the 5-hour limit (e.g. `97`); empty/`0` = off.
   - `CR_FORECAST_FILE` — optional path; when set, the forecast is written there (see 5). Unset = no file.
   - `CR_BUDGET_WINDOW_SEC` (default `600`) — look-back window of the pace fit.
   - `CR_BUDGET_FREEZE_MIN` (default `5`) — freeze when the target is forecast within this many minutes.
   - `CR_BUDGET_SLOW_MSG`, `CR_BUDGET_FREEZE_MSG` — optional overrides. Built-in
     English defaults (`CR_BUDGET_SLOW_MSG_DEFAULT`, `CR_BUDGET_FREEZE_MSG_DEFAULT`,
     same pattern as `CR_HANDOFF_MSG_DEFAULT`). Placeholders: `{percent}`, `{target}`,
     `{rate}` (%/min), `{forecast}` (% at reset), `{minutes}` (to target), `{resets}`
     (local HH:MM), `{agents}` (suggested parallel agents), `{file}` (handoff file,
     as in `CR_HANDOFF_MSG`), and `{skill:...}` like the other phrases.
   - Default slow message: "Session budget: {percent}% of the 5-hour limit used, pace {rate}%/min, forecast {forecast}% at the reset ({resets}); target {target}%. Reduce parallel work to at most {agents} agents: start no new ones until the pace drops."
   - Default freeze message: "Session budget: the {target}% target will be reached in about {minutes} min (reset at {resets}). Start no new agents and stop the running ones at a safe point. " followed by the handoff phrase (`CR_HANDOFF_MSG`, with its `{file}` and marker rules), so the freeze reuses the same handoff as the context restart.
2. **Samples.** Each statusline blob with `rate_limits.five_hour.used_percentage`
   is a sample `(t, percent, resets_at)`; keep only samples of the current window
   (same `resets_at`). Between samples, estimate the percent from token usage:
   sum of weighted tokens of the bound transcript and of the subagent transcripts
   of the same session (weights: output 5, input 1, cache_creation 1.25,
   cache_read 0.1), converted with a ratio percent-per-weighted-token calibrated
   from consecutive statusline samples (keep the last good ratio; no ratio yet →
   statusline samples only).
3. **Forecast.** Least-squares fit over the window → `rate` (%/min);
   `forecast_at_reset = percent + rate * minutes_to_reset`; `minutes_to_target =
   (target - percent) / rate`; `active_agents` = subagent transcripts written in
   the last 120 s; `max_agents = max(0, floor(max(active_agents,1) * safe_rate / rate))`
   where `safe_rate = (target - percent) / minutes_to_reset`.
   State: `freeze` if `percent >= target` or `minutes_to_target < CR_BUDGET_FREEZE_MIN`;
   `slow` if `forecast_at_reset > target`; else `ok`. Fewer than 2 points → `ok`, `rate` null.
4. **Messages** (through the existing safe typing path; never over the user's draft):
   - `ok → slow`: type the slow message once. Again only if the state went back to
     `ok` in between, or `max_agents` dropped by half since the last one.
   - `→ freeze`: type the freeze message once per 5-hour window. Wait for the
     handoff marker the same way the context restart does (no `/clear`). Then
     wait for `resets_at` (+ the same grace used for usage limits) and type the
     resume phrase (`CR_RESUME_MSG` with `{file}`). If a real usage limit is hit
     meanwhile, the existing limit wait takes over; do not type twice.
   - Log each transition in `~/.agent-retrier/log` (`budget: ok→slow 91.4% 0.42%/min forecast 104%`).
5. **Forecast file** (only with `CR_FORECAST_FILE`), written atomically
   (tmp + rename) on every new forecast, schema v1:
   `{"v":1,"agent":"claude","session":"<id>","updated_at":<epoch>,"percent":91.4,"rate_per_min":0.42,"resets_at":<epoch>,"forecast_at_reset":104.2,"minutes_to_target":13.3,"target":97,"state":"ok|slow|freeze","max_agents":3,"active_agents":7,"seven_day_percent":40.1}`.
   The wrapper does not know or care who reads it.
6. **Docs:** new `docs/session-budget.md` (what it does, settings, schema v1 of the
   file with the rule for readers: missing, unparsable or `updated_at` older than
   300 s means "no budget information", never "freeze"), a line in the README
   feature list and the docs table, `CHANGELOG.md` entry under 3.1.0.
7. Version `CR_VERSION="3.1.0"`.

## Acceptance criteria

- [ ] AC1 With `CR_SESSION_BUDGET` unset, behaviour is unchanged: `./test/run.sh` passes and a test asserts no budget messages and no forecast file.
- [ ] AC2 Unit tests (new `test/test_budget.py`): fit over synthetic samples gives the expected rate; state ok/slow/freeze at the boundaries; `max_agents` formula; samples from an older `resets_at` are dropped; token calibration fills between statusline samples.
- [ ] AC3 Controller test with `fake_claude.py`: statusline samples drive `ok→slow` → the slow message is typed exactly once with placeholders filled; then `→freeze` → the freeze message (contains the handoff phrase with `{file}`) is typed once; after the marker and the reset the resume phrase is typed once.
- [ ] AC4 Overrides `CR_BUDGET_SLOW_MSG`/`CR_BUDGET_FREEZE_MSG` replace the defaults (test).
- [ ] AC5 Forecast file: written atomically, valid JSON with every schema v1 key; not written without `CR_FORECAST_FILE` (test).
- [ ] AC6 No typing over a user draft (reuse of the existing gate; test).
- [ ] AC7 `./test/run.sh` passes in full; `./test/codegraph-sync.sh` run; `docs/session-budget.md`, `docs/configuration.md`, README, CHANGELOG updated; `--cr-version` prints 3.1.0.

## Where in the code

- Bash defaults: near `CR_HANDOFF_MSG_DEFAULT` (`agent-retrier.sh:453`).
- `cfg` construction: `agent-retrier.sh:~981–1023`.
- Statusline blob: `on_status` (`~5333`), `run_statusline_proxy` (`~7532`), reader at `~7278`.
- Typing gate and context restart fold/marker: search `handoff_msg`, `fold`, `marker` in the controller.
- Usage-limit wait: `docs/usage-limits.md`, search `rate_limit` in the controller.
