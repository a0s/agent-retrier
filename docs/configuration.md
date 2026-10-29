# Configuration

Every setting in one place, grouped by feature. All optional, all environment
variables.

[← back to README](../README.md)

- [How to read this page](#how-to-read-this-page)
- [Minimal configs](#minimal-configs)
- [Commands and agents](#commands-and-agents)
- [Usage limits and typing](#usage-limits-and-typing)
- [Stalls](#stalls)
- [Context restart](#context-restart)
- [Session budget](#session-budget)
- [Codex](#codex)
- [The badge and the subagent overlay](#the-badge-and-the-subagent-overlay)
- [Updates](#updates)
- [Logging and switching it off](#logging-and-switching-it-off)
- [Rarely touched](#rarely-touched)
- [Detection patterns](#detection-patterns)

Wrapper flags: `--cmd` (same as `CR_CLAUDE_CMD`), `--agent` / `--cr-agent`
(same as `CR_AGENT`), `--cr-models` (print the model profile table resolved
against your environment), `--cr-dump-argv` (print what will be executed),
`--cr-help` (includes the default `CR_HANDOFF_MSG`). Every other argument goes
to claude or codex.

## How to read this page

The wrapper drives **two** agents — Claude Code and codex — and a setting is
always one of four kinds. The `applies to` column in every table below says
which:

| shape | meaning | example |
|---|---|---|
| `CR_FOO` — **both** | one value, used by whichever agent is running | `CR_MESSAGE`, `CR_HANDOFF_FILE` |
| `CR_CLAUDE_FOO` / `CR_CODEX_FOO` — **override** | narrows `CR_FOO` to one agent; unset means "use `CR_FOO`" | `CR_CODEX_RESUME_MSG` |
| `CR_CLAUDE_FOO` / `CR_CODEX_FOO` — **agent-only** | exists for one agent alone; there is no shared `CR_FOO` behind it | `CR_CODEX_RESERVE_TOKENS`, `CR_STATUSLINE_PROXY` |
| `CR_CLAUDE_TOKENS_<SLUG>` / `CR_CODEX_TOKENS_<SLUG>` — **one model** | a value for a single model, by name | `CR_CLAUDE_TOKENS_CLAUDE_HAIKU_4_5` |

Three rules cover every case:

1. **The more specific setting wins.** one model → one agent → both. Nothing
   is merged; the most specific value that is set is simply the one used.
2. **A per-agent override with no value falls back to the shared one.** So
   `CR_HANDOFF_MSG` alone covers claude and codex, and adding
   `CR_CODEX_HANDOFF_MSG` peels codex off it without touching claude.
3. **One exception, deliberate, about absolute token counts.**
   `CR_CODEX_CONTEXT_TOKENS` never inherits `CR_CONTEXT_TOKENS` — a count
   picked for a 1M claude window is nonsense against a 258k codex one. Under
   `CR_CONTEXT_RESTART=1` each agent reaches for its own row in the
   [profile table](context-restart.md#the-context-window) instead, so there
   is nothing to inherit either way.

`<SLUG>` is the model's slug, uppercased, with every non-alphanumeric
character turned into `_`: `claude-opus-5` → `CLAUDE_OPUS_5`, `gpt-5.6-sol` →
`GPT_5_6_SOL`. `--cr-models` prints the slugs your environment actually
resolves, so you never have to guess one.

## Minimal configs

**Nothing at all** is a working config: `agent-retrier-claude` and `agent-retrier-codex`
wait out usage limits and nudge refused turns out of the box. The context
restart is the one feature that is off until you ask for it, because it clears
a session's history.

**The whole context restart, both agents, one repo** — this is the config to
copy:

```sh
export CR_CONTEXT_RESTART=1
export CR_HANDOFF_FILE='.agent-retrier/handoff-{id}.md'
```

`CR_CONTEXT_RESTART=1` arms the restart without making you pick a number: each
agent uses its own row in the [model profile table](context-restart.md#the-context-window)
— 51% of the window for claude, the hard cap minus the 64k reserve for codex
— which is exactly the point, because the right number is not the same one on
both. `{id}` gives every session its own handoff path, so a claude and a codex
session running side by side in the same directory can never write over each
other. Add `.agent-retrier/` to `.gitignore` and that is the setup.

**Pick the numbers yourself** instead:

```sh
export CR_CONTEXT_TOKENS=500k         # claude: this many tokens, exactly
export CR_CODEX_CONTEXT_TOKENS=190k   # codex: its own number — never inherited from claude's
export CR_HANDOFF_FILE='.agent-retrier/handoff-{id}.md'
```

**One phrase, both agents**, when the handoff should go through a skill of
yours — `{skill:NAME}` expands to `/NAME` on claude and `$NAME` on codex:

```sh
export CR_RESUME_MSG='{skill:catch-up} read `{file}` and continue from it'
```

Everything else on this page is tuning. [context-restart.md](context-restart.md)
explains what the restart actually does; [codex.md](codex.md) explains where
codex differs.

## Commands and agents

More in [custom-command.md](custom-command.md) and [codex.md](codex.md).

| variable | default | applies to | |
|---|---|---|---|
| `CR_CLAUDE_CMD` | | claude | your claude command (same as `--cmd`) |
| `CR_CODEX_CMD` | `codex` | codex | the command `agent-retrier --codex` runs; never borrowed from `CR_CLAUDE_CMD` |
| `CR_AGENT` | `auto` | both | `auto` \| `claude` \| `codex` — which one you are running (same as `--agent`) |
| `CR_SHELL` | `$SHELL` | both | shell that knows your aliases |
| `CR_CLAUDE_BIN` | | claude | skip resolution and run exactly this executable |
| `CR_CLAUDE_FALLBACKS` | `~/.claude/local/claude:~/.local/bin/claude:/opt/homebrew/bin/claude:/usr/local/bin/claude` | claude | where to look when `claude` is not on `PATH` |

## Usage limits and typing

More in [usage-limits.md](usage-limits.md). None of this differs between the
two agents: a limit is a limit.

| variable | default | applies to | |
|---|---|---|---|
| `CR_MESSAGE` | `continue` | both | what to type when the limit lifts |
| `CR_MARGIN_SEC` | `45` | both | extra wait past the stated reset time |
| `CR_MAX_ATTEMPTS` | `3` | both | sends per incident before giving up |
| `CR_USER_IDLE_SEC` | `20` | both | don't type while you are typing |
| `CR_TYPING_MAX_SEC` | `900` | both | but not past this, with an empty input box |
| `CR_RESUME_SEC` | `15` | both | the agent working this long during a wait ends it |
| `CR_DRAFT_GRACE_SEC` | `600` | both | an untouched draft this old stops blocking |
| `CR_FALLBACK_WAIT_SEC` | `18000` | both | how long to wait when the notice names no reset time (5 h) |
| `CR_MAX_WAIT_SEC` | `691200` | both | ceiling on any single wait (8 days) |
| `CR_SCRAPE` | `auto` | both | `auto` \| `always` \| `never` — read the limit off the screen as well as the transcript |

## Stalls

More in [stalls.md](stalls.md).

| variable | default | applies to | |
|---|---|---|---|
| `CR_STALL_WAIT_SEC` | `60` | both | first wait after a stall; `0` switches stalls off |
| `CR_STALL_BACKOFF` | `2` | both | multiply the wait by this for each repeat |
| `CR_STALL_MAX_WAIT_SEC` | `600` | both | the longest a stall wait gets |
| `CR_STALL_MAX_ATTEMPTS` | `8` | both | consecutive stalls before it stops nudging |

## Context restart

More in [context-restart.md](context-restart.md). Everything here does nothing
until one of `CR_CONTEXT_RESTART`, `CR_CONTEXT_TOKENS` or a per-model
`CR_CLAUDE_TOKENS_<SLUG>`/`CR_CODEX_TOKENS_<SLUG>` is set.

### Arming it, and where the threshold comes from

Resolved most specific first — the full order is in
[choosing a threshold](context-restart.md#choosing-a-threshold).

| variable | default | applies to | |
|---|---|---|---|
| `CR_CLAUDE_TOKENS_<SLUG>` | unset | one claude model | absolute threshold for that model alone, e.g. `CR_CLAUDE_TOKENS_CLAUDE_OPUS_5=510000`; outranks everything below |
| `CR_CODEX_TOKENS_<SLUG>` | unset | one codex model | the same, e.g. `CR_CODEX_TOKENS_GPT_5_6_SOL=140000` |
| `CR_CONTEXT_TOKENS` | `0` | both | absolute threshold (`500k` is fine); beats the model's own row |
| `CR_CODEX_CONTEXT_TOKENS` | `0` — **never** `CR_CONTEXT_TOKENS` | codex | absolute threshold for codex; a count picked for a 1M window would be past the end of codex's 258k one |
| `CR_CONTEXT_RESTART` | `0` | both | `1` arms the restart without a number: each agent uses its own row in the profile table (`--cr-models`), or — for a model that row does not describe — `DEFAULT_RESTART_PCT` (51%) applied to whatever window the session actually has. An explicit `CR_CONTEXT_TOKENS` — `0` included — still wins outright |
| `CR_CONTEXT_WINDOW` | `auto` | both | `auto` \| `200k` \| `1M` \| a number. Rarely needed on codex, which states its window in every rollout |

### Knowing the window

| variable | default | applies to | |
|---|---|---|---|
| `CR_STATUSLINE_PROXY` | `1` | claude | `0` disables the statusline proxy — no `--settings` is added to claude's launch, and `[1m]` vs 200k falls back to the profile table; see [the statusline proxy](context-restart.md#1m-vs-200k-the-statusline-proxy) |
| `CR_STATUS_DIR` | `~/.agent-retrier/status` | claude | where `--cr-statusline` writes what claude's own statusline reports, one file per wrapper pid |
| `CR_MODEL_LOOKUP` | `1` | both | look an unfamiliar model up; `0` never touches the network |
| `CR_MODEL_CACHE` | `~/.agent-retrier/windows.json` | both | what the lookup learned |
| `CR_MODEL_CACHE_TTL_SEC` | `604800` | both | a week |

### The handoff file and the phrases

Every phrase variable here is a **shared** setting with a per-agent override
next to it. Set the shared one and both agents use it; set
`CR_CLAUDE_*`/`CR_CODEX_*` to peel one off.

| variable | default | applies to | |
|---|---|---|---|
| `CR_HANDOFF_FILE` | `.agent-retrier/handoff.md` | both | where the fold is written; `{id}` makes it unique per session |
| `CR_HANDOFF_REGISTRY_DIR` | `~/.agent-retrier/sessions` | both | where wrapper instances claim their handoff path, so two sharing an `{id}`-free `CR_HANDOFF_FILE` do not overwrite each other (T05) |
| `CR_HANDOFF_MSG` | (see `--cr-help`) | both | the folding phrase; `{file}`, `{marker}` |
| `CR_RESUME_MSG` | ``Read `{file}` and continue from it.`` | both | the unfolding phrase; `{file}` |
| `CR_CANCEL_MSG` | (see `--cr-help`) | both | said instead of a plain "restart aborted" notice when the abort comes after the fold already reached the session (T09); `{file}` |
| `CR_CLEAR_CMD` | `/clear` | both | the command that starts a fresh session in place |
| `CR_CLAUDE_HANDOFF_MSG` / `CR_CODEX_HANDOFF_MSG` | = `CR_HANDOFF_MSG` | override | a phrase naming a command cannot always be shared — claude and codex do not recognize the same ones |
| `CR_CLAUDE_RESUME_MSG` / `CR_CODEX_RESUME_MSG` | = `CR_RESUME_MSG` | override | same, for the unfolding phrase |
| `CR_CLAUDE_CANCEL_MSG` / `CR_CODEX_CANCEL_MSG` | = `CR_CANCEL_MSG` | override | same, for the cancel phrase |
| `CR_CLAUDE_CLEAR_CMD` / `CR_CODEX_CLEAR_CMD` | = `CR_CLEAR_CMD` | override | same, for the clear command (`/clear` itself works on both — verified live, codex-cli 0.155.1) |
| `{skill:NAME}` | — | both | not a variable — a placeholder inside any of the phrases above, expanded per agent to the syntax that agent actually understands: `/NAME` for claude, `$NAME` for codex (T17); see [writing your own phrases](context-restart.md#writing-your-own-phrases) |
| `CR_HANDOFF_MARKER` | `HANDOFF` | both | prefix; a nonce is appended to it |
| `CR_HANDOFF_MIN_BYTES` | `200` | both | a shorter file is not a handoff |

### Timing and safety rails

| variable | default | applies to | |
|---|---|---|---|
| `CR_HANDOFF_ATTEMPTS` | `2` | both | folds attempted before giving up |
| `CR_ROOT_IDLE_SEC` | `20` | claude | transcript quiet this long means the turn is over (codex reads its rollout's own turn rows instead) |
| `CR_HANDOFF_TIMEOUT_SEC` | `900` | both | per step, and frozen while a limit runs |
| `CR_GATE_RETRY_SEC` | `5` | both | a step held back because you are typing or the session is still writing is looked at again this much later; zero, negative or unparseable means the default |
| `CR_CLEAR_SETTLE_SEC` | `5` | both | screen quiet this long after `/clear` counts as confirmation on its own |
| `CR_STEP_GAP_SEC` | `3` | both | between `/clear` and the resume phrase |
| `CR_RESUME_ATTEMPTS` | `5` | both | unfold retries, each wait doubling, before the debt becomes visible instead of retried |
| `CR_CONTEXT_COOLDOWN_SEC` | `600` | both | silence after any restart |
| `CR_CONTEXT_MAX_CYCLES` | `0` | both | `0` = no cap; a fuse against a loop |
| `CR_CONTEXT_MIN_HEADROOM` | `80k` | both | floor the threshold is raised to protect once a restart lands, if the baseline it comes back to leaves less than this (capped to 30% of a window ≤ 200k); see [choosing a threshold](context-restart.md#choosing-a-threshold) |
| `CR_CONTEXT_MAX_PER_HOUR` | `3` | both | more restarts than this in a rolling hour switches the trigger off for the rest of the session; `0` = no cap |
| `CR_NOTIFY_REPEAT_SEC` | `300` | both | how often an `unfold failed` or `restart off` debt is said again, until a key is pressed; see [how-it-works.md](how-it-works.md#a-sign-of-life) |
| `CR_SLASH_ENTER` | `2` | both | Enters sent for a `/command`; `1` turns the insurance Enter off |
| `CR_SLASH_GAP_SEC` | `0.9` | both | pause after a `/command` before the first Enter, letting the command list settle on the exact match |
| `CR_SLASH_ENTER_GAP_SEC` | `0.6` | both | gap between those two Enters |

## Session budget

More in [session-budget.md](session-budget.md). Claude only: codex is not paced.

| variable | default | applies to | |
|---|---|---|---|
| `CR_SESSION_BUDGET` | off | claude | target, in percent of the 5-hour limit (above 0, up to 100). Empty or `0` is off. Turns the statusline proxy on, as the context restart does |
| `CR_FORECAST_FILE` | none | claude | where the forecast is written, schema 1 (see [the file](session-budget.md#the-forecast-file)) |
| `CR_BUDGET_WINDOW_SEC` | `900` | claude | the pace is how far the percent moved over this many seconds |
| `CR_BUDGET_FREEZE_MIN` | `5` | claude | freeze when the target is this many minutes away |
| `CR_BUDGET_BURST_MIN` | `45` | claude | how long the pace of the moment is taken to hold before the window's average takes over |
| `CR_BUDGET_HOLD_SEC` | `180` | claude | how long a budget state holds before a message says so; reaching the target freezes at once |
| `CR_BUDGET_SLOW_MSG` | see [the messages](session-budget.md#the-messages) | claude | the slow message; `{percent}`, `{rate}`, `{forecast}`, `{resets}`, `{target}`, `{agents}`, `{file}` |
| `CR_BUDGET_FREEZE_MSG` | see [the messages](session-budget.md#the-messages) | claude | the freeze message, typed before the handoff phrase; `{target}`, `{minutes}`, `{resets}` |
| `CR_BUDGET_CLEAR_MSG` | see [the messages](session-budget.md#the-messages) | claude | the message that lifts a slow one once the forecast is 10 points under the target; `{percent}`, `{forecast}`, `{resets}`, `{target}` |
| `CR_BUDGET_CANCEL_MSG` | see [the messages](session-budget.md#the-messages) | claude | the phrase that calls off a freeze the session had already seen; an aborted context restart keeps `CR_CANCEL_MSG` |

## Codex

More in [codex.md](codex.md#context-restart-on-codex). Codex's thresholds are
in the [context restart](#context-restart) tables above; what follows exists
only for codex, with no shared setting behind it, and only matters while the
context restart is on.

| variable | default | applies to | |
|---|---|---|---|
| `CR_CODEX_HOLD_COMPACT` | `1` | codex | start codex with its own compaction threshold moved out of the restart's way; `0` leaves it alone |
| `CR_CODEX_RESERVE_TOKENS` | `64k` | codex | room kept under codex's compaction cap for the fold; the threshold never goes past it |
| `CR_CODEX_RESERVE_ADAPT` | `0` | codex | `1` raises the effective reserve to 1.25× the largest fold ever recorded, instead of only recommending it |
| `CR_CODEX_FOLDS_FILE` | `~/.agent-retrier/folds.json` | codex | where the cost of each fold is recorded, so the reserve can be judged against real ones |
| `CR_CODEX_INTERRUPT` | `1` | codex | press Esc on a running turn that crosses the line; `0` = only restart between turns |
| `CR_CODEX_INTERRUPT_AFTER_SEC` | `0` | codex | `0` = only interrupt at the cap-minus-reserve line; a number interrupts a turn that has sat past the ordinary threshold this many seconds |
| `CR_CODEX_LOGS_DB` | newest `$CODEX_HOME/logs_*.sqlite` | codex | where codex logs the count its compaction is decided on |

`CR_CODEX_CMD` is under [Commands and agents](#commands-and-agents).

## The badge and the subagent overlay

More in [how-it-works.md](how-it-works.md#a-sign-of-life).

| variable | default | applies to | |
|---|---|---|---|
| `CR_BADGE` | `1` | both | `0` hides the corner mark |
| `CR_BADGE_POS` | `bottom-right` | both | also `bottom-left`, `top-right`, `top-left` |
| `CR_BADGE_LABEL` | `cr` | both | the word next to the mark |
| `CR_NOTIFY` | `1` | both | `0` stops the dim `[agent-retrier] …` lines being printed into the session |
| `CR_AGENTS_OVERLAY` | `0` | claude | `1` annotates Claude Code's subagent tree with each agent's model and effort (T27) |
| `CR_AGENTS_POS` | `right` | claude | `right` \| `label` — where that annotation is drawn |
| `CR_AGENTS_POLL_SEC` | `1.0` | claude | how often the subagent transcripts are re-read for it |

## Updates

More in [how-it-works.md](how-it-works.md#updates).

| variable | default | applies to | |
|---|---|---|---|
| `CR_UPDATE_CHECK` | `1` | both | `0` never checks for a newer release and never mentions it |
| `CR_UPDATE_NOTICE_SEC` | `2` | both | how long the notice stays before the agent starts |
| `CR_UPDATE_TTL_SEC` | `86400` | both | between checks |
| `CR_UPDATE_CACHE` | `~/.agent-retrier/update.json` | both | |
| `CR_UPDATE_REPO` | `a0s/agent-retrier` | both | whose releases to read |
| `CR_UPDATE_BREW_FORMULA` | `a0s/agent-retrier/agent-retrier` | both | named in the brew command |

## Logging and switching it off

`CR_LOG` is shared by every wrapper process on the machine — that's what T01's
per-line `[cr <pid> <agent>]` tag is for — so nothing ever truncates it on its
own. Left alone it grows without bound; `CR_LOG_MAX_BYTES`/`CR_LOG_KEEP` cap
that. Rotation is checked only once, when a wrapper starts up and opens the
log (never mid-run, so it can't rename the file out from under a neighboring
wrapper in the middle of an incident): past the size limit, `log` becomes
`log.1`, the previous `log.1` becomes `log.2`, and so on up to `CR_LOG_KEEP`
copies, dropping whatever would spill past that. A process that already had
the old file open for writing keeps writing into what is now `log.1` — its
lines stay readable via the same per-line tag, so this is harmless.

| variable | default | applies to | |
|---|---|---|---|
| `CR_LOG` | `~/.agent-retrier/log` | both | |
| `CR_LOG_MAX_BYTES` | `5M` | both | rotate once the log passes this size; accepts `5M`/`500k`-style sizes |
| `CR_LOG_KEEP` | `2` | both | how many rotated copies (`log.1`, `log.2`, ...) to keep |
| `CR_DISABLE` | | both | `1` runs plain claude or codex |

## Rarely touched

Real settings, listed for completeness. The defaults are right in every setup
these were written for; reach for them when something specific is wrong.

| variable | default | applies to | |
|---|---|---|---|
| `CR_POLL_SEC` | `2` | both | how often the transcript (and claude's statusline file) is re-read |
| `CR_SESSION_GRACE_SEC` | `30` | both | how long claude is given to write `~/.claude/sessions/<pid>.json` (codex: to name a thread for our pid in its log) before the wrapper concludes it never will and falls back to guessing which transcript is its own; until then it follows none. Not used by `claude attach` (named by its job) or `claude agents` (named by nothing), which never guess — see [which session is mine](context-restart.md#which-session-is-mine) |
| `CR_BUSY_IDLE_SEC` | `6` | both | no "working" footer for this long means the session is idle |
| `CR_VERIFY_SEC` | `60` | both | how long to watch that a typed retry actually took hold |
| `CR_SCRAPE_CONFIRM_SEC` | `3` | both | a limit banner read off the screen must persist this long to count |
| `CR_PROBE_TIMEOUT_SEC` | `5` | both | timeout when probing the resolved command or shell at startup |
| `CR_MODEL_LOOKUP_TIMEOUT_SEC` | `10` | both | per-request timeout for the model-window lookup |
| `CR_MODELS_API_URL` | `https://api.anthropic.com/v1/models` | both | where a model window is looked up when `ANTHROPIC_API_KEY` is set |
| `CR_MODELS_DOC_URL` | `https://platform.claude.com/docs/en/models/overview.md` | both | and where it is looked up otherwise |
| `CR_UPDATE_URL` | derived from `CR_UPDATE_REPO` | both | override the releases feed outright |
| `CR_UPDATE_TIMEOUT_SEC` | `10` | both | request timeout for the update check |
| `CR_WAIT_SCALE` | `1` | both | divide every wait by this; the test suite runs at `3600`, which is what it is for |
| `CR_TMP_SWEEP` | `1` | both | `0` leaves `$TMPDIR` alone. Otherwise a session, as it starts, removes the `agent-retrier.XXXXXX` copies of the supervisor that 3.0.3 and earlier left there — its own by name, owner and content, and a day old at least |

## Detection patterns

Detection patterns live in one array at the top of `agent-retrier.sh`. Add a
wording and nothing else changes. Patterns for a refused turn live in
`CR_STALL_PATTERNS`, next to the other pattern arrays — see
[stalls.md](stalls.md#detection-patterns).

They are arrays in the script, not environment variables: the `CR_PAT_*`
variables the script exports are how the bash half hands those arrays to the
Python half, and they are overwritten on every start, so exporting one has no
effect. Edit the array.
