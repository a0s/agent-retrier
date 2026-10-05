#!/usr/bin/env bash
# agent-retrier — keep a Claude Code or codex session going, without tmux.
#
# One file. Wraps the agent in a PTY it owns, so it can BOTH see everything the
# session prints AND type into it — the two capabilities that forced the tmux
# design in claude-auto-retry (capture-pane + send-keys). Everything else (detached
# monitor, event markers, launchd/systemd reconcilers, shell-function installer)
# falls out as unnecessary.
#
# Usage:  agent-retrier.sh [claude args...]
#         agent-retrier.sh --cmd <your-claude> [claude args...]
#         agent-retrier.sh --agent codex --cmd codex [codex args...]
#         agent-retrier.sh --codex [codex args...]   # codex instead of claude
#         agent-retrier-codex / agent-retrier-claude  # the same file; the name picks the agent
#         agent-retrier.sh --cr-dump-python      # print the embedded Python (used by tests)
#         agent-retrier.sh --cr-models           # print the model profile table (T18)
#         agent-retrier.sh --cr-statusline <path> # statusline proxy (used via --settings, T20)
#         agent-retrier.sh --cr-version
#
# `--cmd` (or CR_CLAUDE_CMD) is whatever YOU type to start the agent: a binary, a
# script, a name on PATH, an alias or shell function from your ~/.zshrc, or a whole
# command line. Anything the wrapper cannot exec itself is run through your login
# shell, so rc-file aliases work exactly as they do when you type them.
#
# `--agent` (or CR_AGENT) picks whose transcript to read: claude or codex. The
# default works it out from the command being run, so naming it is only needed
# when that command hides which one it starts.
#
# Two things stop a session that is not finished, and it handles both: a usage
# limit, which states when it lifts and is waited out, and a server that refuses
# the turn ("Selected model is at capacity") — which states nothing, so it is
# nudged again after a minute, then two, then four. On codex that refusal takes
# every running agent down with it, which is what makes the nudge worth having.
#
# It can also restart a session that is running out of context window: at
# a token threshold it asks for a handoff file, checks that the file
# really was written, clears the session and unfolds it from that file. Off
# unless you set CR_CONTEXT_RESTART or CR_CONTEXT_TOKENS — see the README. How
# large the window is comes from the model slug; a model this file has never
# heard of is looked up over the network (CR_MODEL_LOOKUP=0 to keep it off) and
# estimated in the meantime, rather than left to disarm the trigger. codex
# states its window in the rollout, and has a threshold of its own:
# CR_CODEX_CONTEXT_TOKENS, which is never borrowed from CR_CONTEXT_TOKENS. If that
# restart is aborted after the fold already reached the session, CR_CANCEL_MSG
# is typed instead of just leaving the session be — default:
# "The context restart was cancelled — the handoff is not needed now. Continue
# with what you were doing before it was requested."
#
# CR_HANDOFF_MSG/CR_RESUME_MSG/CR_CANCEL_MSG (and their per-agent
# CR_CLAUDE_*/CR_CODEX_* overrides) may contain {skill:NAME}, expanded to
# whichever syntax that agent actually sends: /NAME for claude, $NAME for
# codex — e.g. CR_RESUME_MSG='{skill:supervisor} continue from `{file}`'
# unfolds both agents into a "supervisor" skill/subagent instead of only one
# of them. CR_CLEAR_CMD (default /clear, same on both agents — codex-cli
# 0.155.1 runs it identically to claude's) is the built-in that starts a new
# session in place; CR_CLAUDE_CLEAR_CMD/CR_CODEX_CLEAR_CMD override it for one
# agent only.
#
# While it runs there is a dim `◆ cr` in a corner of the screen — the wrapper's
# only visible output. CR_BADGE=0 removes it.
#
# CR_AGENTS_OVERLAY=1 annotates each row of Claude Code's own subagent tree with
# the model that agent is actually running on (read from its transcript, never
# from the spawn request, which the model is free to ignore) — off by default,
# since it means running a small terminal emulator over everything claude draws.
# CR_AGENTS_POS (right|label, default right) picks where the annotation goes;
# CR_AGENTS_POLL_SEC (default 1) how often the subagent files are re-read; and
# CR_PAT_AGENT_ROW is the pattern that finds a tree row on screen, in case
# Claude Code's own render of it ever changes shape. CR_PAT_AGENTS_PANEL_ROW is
# the same, for the separate row shape drawn in the persistent panel opened by
# typing /tasks while a subagent is still running (no tree glyph; a
# "(running)"/"(done)" marker instead) — both are annotated the same way.
#
# On startup it says so when a newer release exists, with the command that
# updates the copy you are actually running — brew, git, or a link. The check
# itself runs in the background and is read from a cache next time, so a session
# never waits on it. CR_UPDATE_CHECK=0 turns the whole thing off.
#
# Set CR_DISABLE=1 to bypass the wrapper entirely.

set -u

CR_VERSION="3.0.2"
# Which copy of this file is running. The update notice prints the command
# that updates THIS one, and `brew upgrade` at someone running a git clone
# would be advice that does nothing.
CR_SELF=${BASH_SOURCE[0]:-$0}

# =============================================================================
# SECTION 1 — DETECTION PATTERNS
# =============================================================================
# Every wording Claude Code has used (or plausibly will use) for "you are out of
# quota". These are Python regexes, matched case-insensitively, one per array
# entry. Order does not matter; any single match makes the line a LIMIT line.
#
# Detection requires a LIMIT line AND a RESET line near each other (or a
# structured `"error":"rate_limit"` transcript record, which needs no pattern at
# all). That pairing is what keeps prose about limits from triggering a retry.
#
# Add your own without touching anything else: append to the array.
# -----------------------------------------------------------------------------
CR_LIMIT_PATTERNS=(
  # --- "You've hit your <qualifier> limit" family (the current TUI wording) ---
  "you'?ve hit your (session|weekly|daily|monthly|hourly|opus|sonnet|usage|[0-9]+-hour|current)?\\s*limit"
  "you have hit your (session|weekly|daily|monthly|hourly|usage|[0-9]+-hour)?\\s*limit"
  "you'?ve hit the (session|weekly|daily|usage|rate)?\\s*limit"
  "hit your (session|weekly|daily|monthly|usage|rate|[0-9]+-hour)\\s+limit"
  # --- "reached" family ---
  "you'?ve reached your (session|weekly|daily|monthly|usage|rate|[0-9]+-hour)?\\s*limit"
  "you have reached your .{0,24}limit"
  "(claude|claude ai|claude code)?\\s*usage limit reached"
  "(session|weekly|daily|monthly|[0-9]+-hour) limit reached"
  "\\blimit reached\\b"
  "\\bquota (exceeded|reached)\\b"
  # --- "exceeded" family ---
  "you'?ve exceeded your .{0,24}limit"
  "you have exceeded your .{0,24}limit"
  "(rate|usage|request|token) limit exceeded"
  "exceeded (your|the) .{0,24}(quota|limit|allowance)"
  # --- generic noun phrases that only ever appear on a real limit render ---
  "\\busage limit\\b"
  "\\brate limit\\b"
  "\\brate_limit_error\\b"
  "\\brate_limit\\b"
  "\\bratelimit(ed)?\\b"
  "\\b[0-9]+-hour limit\\b"
  "out of (extra )?usage"
  "you'?re out of (usage|credits|messages)"
  "run out of (usage|credits|messages)"
  "no (remaining|more) (usage|credits|messages)"
  "insufficient (quota|credits)"
  "\\btoo many requests\\b"
  "http 429|status(:| code)? 429|\\berror 429\\b"
  # --- upsell / companion lines Claude prints right next to a live banner ---
  "/upgrade to increase your usage limit"
  "/usage-credits\\b"
  "upgrade your plan"
  "stop and wait for limit to reset"
  # --- localisations seen in the wild ---
  "límite de uso alcanzado"
  "limite d'utilisation atteinte"
  "nutzungslimit erreicht"
  "使用上限に達しました"
  "已达到使用上限"
)

# Lines that say WHEN the quota comes back. One of these must sit near a LIMIT
# line for the scraper path to fire, and the first one found is what gets parsed
# into a wall-clock wait.
CR_RESET_PATTERNS=(
  "resets?\\s+(at\\s+)?[0-9]{1,2}(:[0-9]{2})?\\s*(am|pm)?"          # resets 3pm / resets at 3:20am / resets 15:30
  "resets?\\s+(on\\s+)?[a-z]{3,9}\\.?\\s+[0-9]{1,2}"                # resets Jul 22 / resets on July 22
  "resets?\\s+(tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
  "resets?\\s+in[:\\s]\\s*[0-9]"                                     # resets in 3 hours
  "reset(s|ting)?\\s+(at|on|in)\\b"
  "will reset (at|on|in)\\b"
  "available again (at|on|in)\\b"
  "try again (at|in|after)\\s+[0-9]"                                 # try again in 5 minutes
  "try again (at|on)\\s+[a-z]{3,9}\\.?\\s+[0-9]{1,2}"                # try again at Aug 20th, 2026 10:00 AM (codex)
  "come back (at|in)\\s+[0-9]"
  "retry[- ]after[:\\s]+[0-9]+"
  "wait\\s+[0-9]+\\s*(seconds?|minutes?|hours?|secs?|mins?|hrs?)\\b"
  "[0-9]{4}-[0-9]{2}-[0-9]{2}t[0-9]{2}:[0-9]{2}"                     # ISO-8601 instant
)

# "Claude is mid-flight" markers. Seen anywhere in the recent output stream they
# mean the session is alive and must not be typed into. The streaming footer is
# repainted several times a second, so its absence for a few seconds is a solid
# idle signal — much stronger than the tmux design's foreground-process check.
# Claude Code 2.1.x dropped "esc to interrupt" from the footer entirely: what it
# paints now is a pulsing glyph, a gerund, and a running total — "✶ Nebulizing… "
# growing into "✻ Cogitating… 20m 57s · ↓ 6.8k tokens". Matching only the old
# wording made every live session look idle, which cost us the one thing the
# verify step is for: telling "the retry landed" from "the retry vanished". The
# old wordings stay for older builds.
CR_WORKING_PATTERNS=(
  "esc to interrupt"
  "\\besc\\b[^\\n]{0,24}\\binterrupt\\b"
  "ctrl\\+c to (stop|interrupt)"
  "retrying in [0-9]"
  "attempt [0-9]+/[0-9]+"
  "waiting for [0-9]+ background agents? to finish"
  "tokens?\\s*·\\s*esc"
  "[✢✳✶✷✸✹✺✻✽*][ ]?[a-z]{3,}…"                       # ✶ Nebulizing…
  "…\\s*[0-9]+(h|m|s)([ 0-9hms]{0,8})\\s*·\\s*↓"       # … 20m 57s · ↓ 6.8k tokens
  "↓\\s*[0-9.]+k?\\s*tokens"
  # codex paints "• Working (12s • esc to interrupt)" and, while a turn runs,
  # offers the composer as a queue instead of a prompt. Either says the same
  # thing the footer above does: this session is mid-turn, do not type into it.
  "\\bworking\\s*\\([0-9]+[hms]"
  "\\btab to queue message\\b"
)

# Transient server-side failures: the model is out of capacity, the service is
# overloaded, the request was refused by the infrastructure rather than by the
# account. Not a quota — nothing resets, there is no time to wait until, and the
# fix is to ask again in a minute.
#
# codex ends the whole turn on one of these, which on a session running agents
# takes every agent down with it and leaves the session sitting at an idle
# prompt. That is the shape this list is for: a stop that announces no way out
# of itself.
#
# Kept narrow on purpose. Anything vaguer than these would fire on a model
# talking ABOUT capacity, and a wrong stall is a message typed into a live
# session for no reason.
CR_STALL_PATTERNS=(
  "selected model is at capacity"
  "model is at capacity"
  "please try a different model"
  "\\bserver_overloaded\\b"
  "\\boverloaded_error\\b"
  "(server|service|model|api) is (currently )?overloaded"
  "we'?re (currently )?experiencing high demand"
  "http 503|status(:| code)? 503|\\berror 503\\b"
  "\\bservice unavailable\\b"
)

# The interactive /rate-limit-options selector. If this is on screen a bare Enter
# confirms the highlighted default — historically "Upgrade your plan" (upstream
# issue #19) — so we dismiss it with Escape before typing anything.
CR_MENU_PATTERNS=(
  "what do you want to do\\?"
  "stop and wait for limit to reset"
  "1\\.\\s*upgrade your plan"
  "/rate-limit-options"
)

# Lines that LOOK like a limit but are not one. Checked first; a matching line is
# dropped before pairing. Keeps the API-429 "not your usage limit" render and
# this tool's own docs/logs from parking the session for hours.
CR_IGNORE_PATTERNS=(
  "not your usage limit"                       # "Server is temporarily limiting requests (not your usage limit)"
  "temporarily limiting requests"
  "approaching (your )?.{0,16}limit"           # the 90%-warning banner: not a stop
  "you are nearing"
  "agent-retrier|claude-auto-retry|CR_LIMIT_PATTERNS|CR_RESET_PATTERNS|CR_STALL_PATTERNS"
  "^\\s*[#>]\\s"                               # markdown quote / comment in a rendered doc
)

# `claude agents` — the roster. Every card on it is a DIFFERENT session's last
# line plus how long ago that line was written; none of it describes the
# terminal we are wrapping. A card saying "You've hit your session limit ·
# resets 9:30pm … 3h" is an agent that ran out three hours ago, and taking it
# for a live banner parked a fresh session for six hours over a limit that had
# already lifted. The roster also has no input box of its own worth typing
# "continue" into. So a screen showing one is never scraped — the structured
# transcript channel still reports a limit hit by this session's own claude.
CR_ROSTER_PATTERNS=(
  # the header tally: two counters joined by the middle dot, which is the one
  # shape no ordinary session output produces.
  "[0-9]+ (awaiting input|working|completed)\\s*·\\s*[0-9]+ (awaiting input|working|completed)"
  "describe a task for a new session"
)

# A row of Claude Code's own subagent tree, rendered into the screen grid (not
# matched against the byte stream — the render is differential and word-split,
# so grep finds nothing there). Column 1: the tree glyph (├/└/│ — a row whose
# label starts with ⎿ is a child's status line, not an agent of its own, and
# is filtered out in Python, not here). Column 2: the label, which is the
# `description` Claude gave the Agent tool call and the only thing that ties a
# screen row back to a real agent. Kept swappable so a render change upstream
# is a variable, not a release.
CR_AGENT_ROW_PATTERNS=(
  "^ {3}([├└│])\\s(.+)$"
)

# A row of the OTHER subagent view: the persistent panel opened by typing
# /tasks while at least one Task-tool subagent is still running, which (unlike
# the tree above) never collapses on its own — it stays until Esc closes it
# (T29). Its rows carry no tree glyph: an unselected one is indented 5 spaces,
# the one under the cursor has "❯" at column 4 instead, and both carry a
# "(running)"/"(done)"/... state marker right after the label, often followed
# by " · <model>". That "(word)" is also what tells a real row apart from the
# section header ("Local agents (N)") sharing the exact same 6-column indent,
# which never carries one — column position alone cannot distinguish them.
# Matched as a PREFIX, not the whole line (no trailing $): whatever comes
# after the state, model text or otherwise, is not this pattern's problem,
# same as CR_AGENT_ROW_PATTERNS above never anchors past its own label.
# Investigated live on 2.1.273 (T29): the footer's own "<- for agents" hint,
# in both the default and "manual" permission mode, does NOT open this or any
# per-session view — it backgrounds the conversation into the cross-session
# roster instead (other, unrelated sessions), which is why that key is never
# sent by anything in this file. /tasks is the only confirmed way in.
CR_AGENTS_PANEL_ROW_PATTERNS=(
  "^ {3}[ ❯]\\s(.+?) \\(([a-z]+)\\)"
)

# =============================================================================
# SECTION 2 — configuration (all overridable from the environment)
# =============================================================================
: "${CR_AGENT:=auto}"                  # auto | claude | codex — whose session this is
: "${CR_CODEX_CMD:=}"                  # --codex's command (default: codex)
: "${CR_MESSAGE:=continue}"            # what to type when the limit lifts
: "${CR_MARGIN_SEC:=45}"               # extra wait past the stated reset time
: "${CR_MAX_ATTEMPTS:=3}"              # sends per incident before giving up
: "${CR_FALLBACK_WAIT_SEC:=18000}"     # 5h, used when no reset time can be parsed
: "${CR_MAX_WAIT_SEC:=691200}"         # 8d hard cap on any single wait
: "${CR_USER_IDLE_SEC:=20}"            # don't type while the human is typing
: "${CR_TYPING_MAX_SEC:=900}"          # ...but not past this, with an empty input box
: "${CR_DRAFT_GRACE_SEC:=600}"         # an untouched "draft" this old is not real
: "${CR_BUSY_IDLE_SEC:=6}"             # no working-footer for this long => idle
: "${CR_RESUME_SEC:=15}"               # claude working this long during a wait => limit is gone
: "${CR_VERIFY_SEC:=60}"               # how long to watch for the retry taking hold
: "${CR_SCRAPE:=auto}"                 # auto | always | never  (screen-scrape fallback)
: "${CR_LOG:=$HOME/.agent-retrier/log}"
: "${CR_LOG_MAX_BYTES:=5M}"            # rotate the shared log once it passes this size
: "${CR_LOG_KEEP:=2}"                  # how many rotated copies (log.1, log.2, ...) to keep
: "${CR_NOTIFY:=1}"                    # print a one-line status note into the terminal
: "${CR_BADGE:=1}"                     # dim marker in a screen corner: "we are here"
: "${CR_BADGE_POS:=bottom-right}"      # bottom-right | bottom-left | top-right | top-left
: "${CR_BADGE_LABEL:=cr}"              # the word drawn next to the mark
: "${CR_AGENTS_OVERLAY:=0}"            # 1 = annotate the subagent tree with models
: "${CR_AGENTS_POS:=right}"            # right | label
: "${CR_AGENTS_POLL_SEC:=1.0}"         # how often subagent files are re-read
: "${CR_WAIT_SCALE:=1}"                # divide every wait by this (tests use 3600)
: "${CR_CLAUDE_BIN:=}"                 # override the claude binary (a file, nothing else)
: "${CR_CLAUDE_CMD:=}"                 # YOUR claude command: binary, PATH name, alias,
                                       # shell function, or a full command line
: "${CR_SHELL:=}"                      # shell that knows your aliases (default: $SHELL)
# Where to look when `claude` is not on PATH (colon-separated, in order).
: "${CR_CLAUDE_FALLBACKS:=$HOME/.claude/local/claude:$HOME/.local/bin/claude:/opt/homebrew/bin/claude:/usr/local/bin/claude}"
: "${CR_POLL_SEC:=2}"                  # transcript poll interval
: "${CR_SCRAPE_CONFIRM_SEC:=3}"        # a scraped banner must persist this long

# --- transient stalls --------------------------------------------------------
# The other way a session stops without being finished: the server refuses the
# turn — out of capacity, overloaded — and nothing schedules a way back. There is
# no reset time to parse, so the wait is one we choose, and it doubles each time
# the same stall comes straight back rather than hammering a service that has
# just said it is full.
: "${CR_STALL_WAIT_SEC:=60}"           # first wait after a stall; 0 = ignore stalls
: "${CR_STALL_BACKOFF:=2}"             # multiply the wait by this for each repeat
: "${CR_STALL_MAX_WAIT_SEC:=600}"      # ...but never wait longer than this
: "${CR_STALL_MAX_ATTEMPTS:=8}"        # consecutive stalls before we stop nudging

# --- context restart ---------------------------------------------------------
# The second trigger: not "the quota ran out" but "the context window is filling
# up". At the threshold the wrapper asks Claude to fold the session into a file,
# proves the file was really written, clears the session, and hands the file to
# a fresh one.
#
# OFF by default, and it has to be: this types into a live session and throws its
# history away. Nothing below happens until CR_CONTEXT_RESTART (or a token
# threshold: CR_CONTEXT_TOKENS, CR_CODEX_CONTEXT_TOKENS, or one of the
# CR_*_TOKENS_<SLUG> overrides) is set.
: "${CR_CONTEXT_TOKENS:=0}"            # absolute threshold; wins over the model's own row
: "${CR_CONTEXT_WINDOW:=auto}"         # auto | 200k | 1M | a plain number
# Turns the restart on without asking you to pick a number first: at the
# model's own row in the profile table (--cr-models), or — for a model that
# table doesn't know — DEFAULT_RESTART_PCT (51) of whatever window the session
# actually reports. Set CR_CONTEXT_TOKENS yourself and that still wins
# outright, exactly as if this were never set.
: "${CR_CONTEXT_RESTART:=0}"           # 1 = on, at the model's row (or its default) unless overridden
# An absolute count is never carried over to codex, because it does not mean
# the same session on both: 500k chosen for a 1M claude window is past the end
# of a 258k codex one. Unset, codex falls through to its own row or default
# (stage 3/4) exactly like claude does.
: "${CR_CODEX_CONTEXT_TOKENS:=}"        # unset = none; never CR_CONTEXT_TOKENS
# Removed in 2.0. A percentage of a window meant a different thing on each
# agent and nothing at all until you knew the window; the model profile table
# (--cr-models) answers that question per model now. Refusing is deliberate:
# silently ignoring it would leave a session that used to be protected
# running with no restart armed at all.
for cr_gone in CR_CONTEXT_PCT CR_CODEX_CONTEXT_PCT; do
  eval "cr_gone_val=\${$cr_gone:-}"
  [ -n "$cr_gone_val" ] || continue
  printf '%s\n' \
    "agent-retrier: $cr_gone was removed in 2.0 and is no longer read." \
    "  Unset it, then arm the restart one of these ways:" \
    "    CR_CONTEXT_RESTART=1          # each model's own row (--cr-models)" \
    "    CR_CONTEXT_TOKENS=510k        # one absolute number" \
    "    CR_CLAUDE_TOKENS_CLAUDE_OPUS_5=510k   # one model only" \
    "  See docs/context-restart.md#choosing-a-threshold." >&2
  exit 2
done
# A restart that codex's own compaction beats to it is no restart at all, so with
# the restart on the wrapper keeps codex from compacting first. codex compacts at
# 90% of the raw window (244.8k of 272k), counted from an estimate that runs ahead
# of anything the rollout shows, and it does so in the middle of a turn. So:
#   - its threshold is moved out of the way, passing codex
#     -c model_auto_compact_token_limit_scope="body_after_prefix" and a huge
#     model_auto_compact_token_limit, which leaves only its hard cap at 95% of
#     the window (258.4k) — a cap nothing can move;
#   - the count is read where codex writes the number it compares against that
#     cap: the "post sampling token usage" rows of $CODEX_HOME/logs_*.sqlite;
#   - a turn still running when the count gets within CR_CODEX_RESERVE_TOKENS of
#     the cap is interrupted (Esc), so the fold has room to happen in.
: "${CR_CODEX_HOLD_COMPACT:=1}"         # 0 = leave codex's compaction settings alone
# 32k was measured against a light fold; a heavy one (a handoff message that
# has the model read files or run shell commands before it writes) can burn
# close to that on its own, leaving the interrupt no room to land ahead of the
# hard cap. 64k is the reserve a fold like that needs to land in.
: "${CR_CODEX_RESERVE_TOKENS:=64k}"     # room kept under codex's cap for the fold
: "${CR_CODEX_INTERRUPT:=1}"            # 0 = never interrupt a running turn
# An orchestrator turn can run for hours -- far longer than the cap-reserve line
# gives it room for -- so the threshold between turns is nearly irrelevant on its
# own (T22). 0 (default) changes nothing: a running turn is still only ever
# interrupted at the cap-reserve line. A number here also interrupts it once it
# has sat past the ordinary threshold (trigger_limit) that long.
: "${CR_CODEX_INTERRUPT_AFTER_SEC:=0}"
: "${CR_CODEX_LOGS_DB:=}"               # default: the newest $CODEX_HOME/logs_*.sqlite
# What a fold actually costs (tokens at the handoff being accepted minus tokens
# when it was asked for) is logged and accumulated here after every codex fold,
# so CR_CODEX_RESERVE_TOKENS can be judged against real folds instead of the one
# incident it was picked from (T22).
: "${CR_CODEX_FOLDS_FILE:=$HOME/.agent-retrier/folds.json}"
# 1 = once an observed fold cost outgrows CR_CODEX_RESERVE_TOKENS, use
# 1.25x the largest one seen (from CR_CODEX_FOLDS_FILE) as the effective reserve
# from then on, instead of only recommending the change in the log.
: "${CR_CODEX_RESERVE_ADAPT:=0}"
# A model this file has never heard of is estimated rather than left to disarm
# the trigger (T19): a slug nobody shipped this build knowing about is almost
# always a NEW model, i.e. a large one, and a session with nothing armed at all
# dies of its own context instead of folding a little early on a guess. The
# network is still asked for something firmer while that estimate stands — the
# Models API when ANTHROPIC_API_KEY is set, the published models table
# otherwise — in a worker thread, once a week per slug. CR_MODEL_LOOKUP=0 turns
# the network off; the estimate is what the trigger runs on from then on,
# unless CR_CONTEXT_WINDOW or CR_CONTEXT_TOKENS says what to use instead.
# The wrapper's own version, checked against the newest release once a day. The
# fetch never blocks a session: what is printed at startup comes from the cache
# the previous run left, and the refresh happens in the background afterwards.
: "${CR_UPDATE_CHECK:=1}"              # 0 = never look, never mention it
: "${CR_UPDATE_REPO:=a0s/agent-retrier}"
: "${CR_UPDATE_URL:=}"                 # overrides the repo's releases feed
: "${CR_UPDATE_BREW_FORMULA:=a0s/agent-retrier/agent-retrier}"
: "${CR_UPDATE_CACHE:=$HOME/.agent-retrier/update.json}"
: "${CR_UPDATE_TTL_SEC:=86400}"        # a day between checks
: "${CR_UPDATE_TIMEOUT_SEC:=10}"
: "${CR_UPDATE_NOTICE_SEC:=2}"         # how long the notice stays before claude starts

: "${CR_MODEL_LOOKUP:=1}"              # 0 = never ask anything over the network
: "${CR_MODEL_LOOKUP_TIMEOUT_SEC:=10}" # per request, and it is never waited on
: "${CR_MODEL_CACHE:=$HOME/.agent-retrier/windows.json}"
: "${CR_MODEL_CACHE_TTL_SEC:=604800}"  # a week
: "${CR_MODELS_DOC_URL:=https://platform.claude.com/docs/en/models/overview.md}"
: "${CR_MODELS_API_URL:=https://api.anthropic.com/v1/models}"
: "${CR_HANDOFF_FILE:=.agent-retrier/handoff.md}"   # relative to cwd — .gitignore it
                                        # supports {id}, a short id unique per session (T05)
: "${CR_HANDOFF_REGISTRY_DIR:=$HOME/.agent-retrier/sessions}"
                                        # where wrapper instances claim their handoff path
: "${CR_HANDOFF_MARKER:=HANDOFF}"      # a nonce is appended; must end the file
: "${CR_HANDOFF_MIN_BYTES:=200}"       # anything shorter is not a handoff
: "${CR_HANDOFF_ATTEMPTS:=2}"          # tries to fold before giving up
: "${CR_RESUME_ATTEMPTS:=5}"           # retypes of the unfold phrase, each wait doubling,
                                        # before the debt is UNFOLD_FAILED instead of retried
# Three things in this phrase are load-bearing: no new work, "a fresh session
# that has no memory of this one", and the marker as the LAST LINE OF THE FILE.
# A marker in the chat would only prove the model believes it finished; a marker
# at the end of the file proves the write ran to completion. {file} and {marker}
# are substituted. It has to stay one line: a newline submits it.
CR_HANDOFF_MSG_DEFAULT='Wrap up now. Do not start new work. Write a complete handoff to `{file}` for a fresh session that has no memory of this one: current state, what is in flight, what to do next, and the exact commands to verify. The very last line of that file must be exactly {marker} and nothing else — write it only after the rest of the file is complete.'
: "${CR_HANDOFF_MSG:=$CR_HANDOFF_MSG_DEFAULT}"
: "${CR_CLEAR_CMD:=/clear}"            # the built-in that starts a new session in place
CR_RESUME_MSG_DEFAULT='Read `{file}` and continue from it.'
: "${CR_RESUME_MSG:=$CR_RESUME_MSG_DEFAULT}"
# Said instead of the plain "restart aborted" notice when the abort comes
# AFTER the fold already reached the session (T09): the model was told to
# wrap up and start nothing new, and an autonomous session left on that
# instruction sits on an empty input until a person shows up. {file} is
# substituted, same as above, though this phrase has no need of it.
CR_CANCEL_MSG_DEFAULT='The context restart was cancelled — the handoff is not needed now. Continue with what you were doing before it was requested.'
: "${CR_CANCEL_MSG:=$CR_CANCEL_MSG_DEFAULT}"
# claude and codex do not speak the same commands, so a phrase that names one —
# a slash command, a skill, a custom prompt — cannot be shared between them.
# CR_CLAUDE_<X>/CR_CODEX_<X> override CR_<X> for one agent only; unset (the
# default), the agent uses the plain phrase above, exactly as before.
: "${CR_CLAUDE_HANDOFF_MSG:=}"
: "${CR_CODEX_HANDOFF_MSG:=}"
: "${CR_CLAUDE_CLEAR_CMD:=}"
: "${CR_CODEX_CLEAR_CMD:=}"
: "${CR_CLAUDE_RESUME_MSG:=}"
: "${CR_CODEX_RESUME_MSG:=}"
: "${CR_CLAUDE_CANCEL_MSG:=}"
: "${CR_CODEX_CANCEL_MSG:=}"
: "${CR_ROOT_IDLE_SEC:=20}"            # transcript quiet this long => the turn is over
: "${CR_HANDOFF_TIMEOUT_SEC:=900}"     # per restart step, and frozen while a limit runs
: "${CR_STEP_GAP_SEC:=3}"              # between /clear and the resume phrase
: "${CR_CLEAR_SETTLE_SEC:=5}"          # screen quiet this long after /clear counts as
                                        # confirmation on its own, without a rebind
: "${CR_CONTEXT_COOLDOWN_SEC:=600}"    # silence after any restart, successful or not
: "${CR_CONTEXT_MAX_CYCLES:=0}"        # 0 = no cap; >0 is a fuse against a loop
# A session fresh out of a restart already carries a baseline (system prompt,
# CLAUDE.md, MCP tool defs, the resume read) before it writes a word -- against
# a small window that alone can eat most of a percentage threshold's own
# headroom, and the restart it just bought fires again in 30-40 minutes
# instead of hours. CR_CONTEXT_MIN_HEADROOM is the floor the threshold is
# raised to protect, capped to 30% of a window at or under 200k so a small
# window is never asked for more slack than it could ever spare (T13).
: "${CR_CONTEXT_MIN_HEADROOM:=80k}"
# A threshold that keeps needing to raise itself, or a session that keeps
# refilling in minutes, is not being protected by this feature any more -- it
# is being ground down by it. More restarts than this inside a rolling hour
# switches the trigger off for the rest of the session instead of guessing at
# a better number (T13). 0 = no cap.
: "${CR_CONTEXT_MAX_PER_HOUR:=3}"
# A standing debt -- an unfold that never reached the session, or the trigger
# switched off for the rest of it -- gets one notify() line same as anything
# else, and Claude's own repaint erases that line within a frame. Said again
# every this many seconds until a key is pressed, so it cannot go unnoticed
# for hours just because nothing else changed (T14).
: "${CR_NOTIFY_REPEAT_SEC:=300}"
# Typing a "/" opens Claude Code's command list, where Enter can pick the
# highlighted entry instead of submitting what was typed. On Claude Code 2.1.222
# one Enter runs /clear and nothing asks for confirmation — so a slash command
# gets a longer pause, for the list to settle on the exact match, and a second
# Enter purely as insurance. That second one is free: an Enter into an empty box
# submits nothing.
: "${CR_SLASH_GAP_SEC:=0.9}"           # after typing a slash command, before Enter
: "${CR_SLASH_ENTER:=2}"               # Enters to send for one
: "${CR_SLASH_ENTER_GAP_SEC:=0.6}"     # between them

# =============================================================================
# SECTION 3 — argument handling / degradation
# =============================================================================
case "${1:-}" in
  --cr-version) echo "agent-retrier $CR_VERSION"; exit 0 ;;
  --cr-help|-h|--help-retrier)
    sed -n '2,73p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
esac

# `--cmd <command>` — the user's own way of starting Claude. Leading position
# only: everything after it belongs to claude, and claude takes a bare prompt as
# its first argument, so a positional guess would eat the prompt of anyone typing
# `agent-retrier.sh "fix the bug"`.
#
# `--cr-cmd` is the same flag under the prefix every other wrapper option carries.
# It stays because it is the unambiguous spelling: should claude ever grow a
# `--cmd` of its own, that one is still reachable through the prefixed form.
CR_CMD_SPEC="$CR_CLAUDE_CMD"

# Which agent to wrap: `--claude` / `--codex`, or the name this file is called
# by — `agent-retrier-claude` / `agent-retrier-codex` are the symlinks an install
# puts next to it. Plain `agent-retrier` means claude. In codex mode the command
# is CR_CODEX_CMD or plain `codex`, never the CR_CLAUDE_CMD a claude user keeps
# in their rc file. An explicit `--cmd` or `--agent` still wins, whichever order
# the flags come in.
CR_PROG=agent-retrier
CR_MODE=
case "${CR_SELF##*/}" in
  agent-retrier-codex|agent-retrier-codex.sh) CR_MODE=codex ;;
  agent-retrier-claude|agent-retrier-claude.sh) CR_MODE=claude ;;
esac

CR_CMD_EXPLICIT=0
CR_AGENT_EXPLICIT=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --codex|--cr-codex) CR_MODE=codex; shift ;;
    --claude|--cr-claude) CR_MODE=claude; shift ;;
    --cmd|--cr-cmd)
      [ "$#" -ge 2 ] || { echo "agent-retrier: $1 needs a command" >&2; exit 2; }
      CR_CMD_SPEC="$2"; CR_CMD_EXPLICIT=1; shift 2 ;;
    --cmd=*) CR_CMD_SPEC="${1#--cmd=}"; CR_CMD_EXPLICIT=1; shift ;;
    --cr-cmd=*) CR_CMD_SPEC="${1#--cr-cmd=}"; CR_CMD_EXPLICIT=1; shift ;;
    --agent|--cr-agent)
      [ "$#" -ge 2 ] || { echo "agent-retrier: $1 needs claude or codex" >&2; exit 2; }
      CR_AGENT="$2"; CR_AGENT_EXPLICIT=1; shift 2 ;;
    --agent=*) CR_AGENT="${1#--agent=}"; CR_AGENT_EXPLICIT=1; shift ;;
    --cr-agent=*) CR_AGENT="${1#--cr-agent=}"; CR_AGENT_EXPLICIT=1; shift ;;
    *) break ;;
  esac
done

CR_CMD_DEFAULTED=0
case "$CR_MODE" in
  codex)
    CR_PROG=agent-retrier-codex
    if [ "$CR_CMD_EXPLICIT" = 0 ]; then
      CR_CMD_SPEC="${CR_CODEX_CMD:-codex}"
      [ -n "${CR_CODEX_CMD:-}" ] || CR_CMD_DEFAULTED=1
    fi
    [ "$CR_AGENT_EXPLICIT" = 1 ] || [ "$CR_AGENT" != auto ] || CR_AGENT=codex ;;
  claude)
    CR_PROG=agent-retrier-claude
    [ "$CR_AGENT_EXPLICIT" = 1 ] || [ "$CR_AGENT" != auto ] || CR_AGENT=claude ;;
esac

case "$CR_AGENT" in
  auto|claude|codex) ;;
  *) echo "agent-retrier: unknown agent '$CR_AGENT' — expected claude or codex" >&2; exit 2 ;;
esac

# A user command that resolves back to this script would fork-bomb the machine.
# Every exec below inherits this counter; two levels are legitimate (the wrapper
# degrading into plain claude), a third means the command points at us.
CR_DEPTH=$(( ${CR_DEPTH:-0} + 1 ))
export CR_DEPTH
if [ "$CR_DEPTH" -gt 3 ]; then
  echo "agent-retrier: refusing to recurse — does your claude command point back at agent-retrier?" >&2
  exit 1
fi

# Never let the wrapper be the reason `claude` stops working (upstream issue #65:
# an orphaned shell function bricked the command). Any doubt => plain claude.
cr_find_claude() {
  if [ -n "$CR_CLAUDE_BIN" ]; then
    # An explicit override that isn't runnable is a configuration mistake, not a
    # reason to silently fall back to some other claude.
    { [ -f "$CR_CLAUDE_BIN" ] && [ -x "$CR_CLAUDE_BIN" ]; } || return 1
    printf '%s' "$CR_CLAUDE_BIN"
    return 0
  fi
  local p
  # `type -P` searches PATH for an executable FILE only — a shell function or
  # alias named `claude` (Claude Code's own installer adds one, and so does
  # claude-auto-retry) is skipped, so the wrapper can never recurse into itself.
  p=$(type -P claude 2>/dev/null)
  if [ -n "$p" ] && [ -f "$p" ] && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  local IFS=:
  for p in $CR_CLAUDE_FALLBACKS; do
    # -f as well as -x: a bare -x test also passes for a DIRECTORY (the execute
    # bit means "may traverse"), and exec'ing one fails with a baffling EACCES.
    if [ -f "$p" ] && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  done
  return 1
}

# The shell that knows the user's aliases and functions. $SHELL is the one they
# actually configured; the fallbacks only matter in stripped environments (cron,
# containers) where no alias can be defined anyway.
cr_find_shell() {
  local s
  for s in ${CR_SHELL:-} ${SHELL:-} bash zsh sh; do
    [ -n "$s" ] || continue
    command -v "$s" >/dev/null 2>&1 && { printf '%s' "$s"; return 0; }
  done
  return 1
}

# A word we can hand to `exec` or `type -P` as-is: no whitespace, no character the
# shell would expand. Anything else is a command LINE and needs a shell to read it.
cr_is_bare_word() {
  case "$1" in
    "") return 1 ;;
    *[!A-Za-z0-9._/@%+:=-]*) return 1 ;;
    *) return 0 ;;
  esac
}

# An rc file is arbitrary code, and some of it asks questions: zsh's compinit
# stops on "insecure directories … [y/n]?" and reads the answer straight from
# /dev/tty, which is the terminal our caller is sitting at. Inheriting that hang
# would mean a session that never starts — the one failure this wrapper must not
# have — so every probe runs detached from stdin and on a leash.
: "${CR_PROBE_TIMEOUT_SEC:=5}"

cr_probe() {
  local tmp rc pid waited=0 limit=$((CR_PROBE_TIMEOUT_SEC * 10))
  tmp=$(mktemp 2>/dev/null) || return 1
  "$@" >"$tmp" 2>/dev/null </dev/null &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$limit" ]; then
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      rm -f "$tmp"
      return 124
    fi
    sleep 0.1
    waited=$((waited + 1))
  done
  wait "$pid"; rc=$?
  cat "$tmp"
  rm -f "$tmp"
  return "$rc"
}

# Skip the system-wide rc when asking zsh about a name. On a stock Ubuntu (and on
# GitHub's runners) /etc/zsh/zshrc is where compinit lives, while the alias we are
# asking about is by definition in the user's own file. bash has no equivalent
# switch, but also no equivalent prompt.
cr_probe_flags() {
  case "${1##*/}" in
    zsh) printf '%s' '--no-globalrcs' ;;
  esac
}

# The body of an alias, asked of the shell that defines it. Returning the body
# rather than running the alias through `sh -i` keeps the interactive shell out of
# the final exec: one less process between us and claude, and no "no job control
# in this shell" on stderr when we are running without a tty (`claude -p`).
# bash 3.2 (the /bin/bash macOS still ships) has no BASH_ALIASES and returns
# nothing here — the interactive path below covers it.
cr_alias_body() {   # $1 = shell, $2 = name
  local body flags rc
  flags=$(cr_probe_flags "$1")
  body=$(cr_probe "$1" ${flags:+$flags} -ic \
         'if [ -n "${ZSH_VERSION:-}" ]; then print -r -- "${aliases[$1]:-}"
          else printf "%s" "${BASH_ALIASES[$1]:-}"; fi' cr-probe "$2")
  rc=$?
  # 124 (the probe timed out) has to survive: it means the shell is stuck, which
  # the caller handles differently from "this name is not an alias".
  [ "$rc" -eq 0 ] || return "$rc"
  [ -n "$body" ] || return 1
  printf '%s' "$body"
}

# The definition of a shell function, for the same reason: asked once, up here,
# so that the shell we exec later can be a plain non-interactive one. An
# interactive shell inside the pty is not merely wasteful — on a headless machine
# (CI, a container, a cron job) it contends for the terminal it has just been
# handed and can hang there, which is a session that never starts.
cr_function_def() {   # $1 = shell, $2 = name
  local def flags
  flags=$(cr_probe_flags "$1")
  def=$(cr_probe "$1" ${flags:+$flags} -ic \
        'if [ -n "${ZSH_VERSION:-}" ]; then typeset -f -- "$1"
         else declare -f -- "$1"; fi' cr-probe "$2") || return 1
  [ -n "$def" ] || return 1
  printf '%s' "$def"
}

# Turns "whatever the user types to start Claude" into an argv vector in CR_ARGV.
# Four shapes, tried in the order that keeps the common ones cheap and exact:
#
#   /opt/bin/claude-work   a path            -> exec it
#   claude-work            a name on PATH    -> exec the file
#   claude-work            an alias/function -> run it through an interactive shell
#   "claude --model opus"  a command line    -> ditto, shell reads the arguments
#
# The interactive shell is what makes rc-file aliases work: they exist nowhere
# else. It is only reached when the cheap paths miss, so the usual case pays
# nothing for it.
CR_ARGV=()
CR_RESOLVE_ERR=""        # "timeout" when the shell never answered
cr_resolve_cmd() {
  local spec="$1" p sh
  CR_ARGV=()
  CR_RESOLVE_ERR=""

  if [ -z "$spec" ]; then
    p=$(cr_find_claude) || return 1
    CR_ARGV=("$p")
    return 0
  fi

  if cr_is_bare_word "$spec"; then
    case "$spec" in
      */*)
        # An explicit path that isn't runnable is a mistake worth reporting, not a
        # reason to go looking for some other claude.
        { [ -f "$spec" ] && [ -x "$spec" ]; } || return 1
        CR_ARGV=("$spec"); return 0 ;;
    esac
    p=$(type -P -- "$spec" 2>/dev/null)
    if [ -n "$p" ] && [ -f "$p" ] && [ -x "$p" ]; then CR_ARGV=("$p"); return 0; fi
  fi

  sh=$(cr_find_shell) || return 1
  if cr_is_bare_word "$spec"; then
    # Not a file, so it can only be an alias or a function — and only an
    # interactive shell has read the rc file that defines one. Lift the definition
    # out of it here, once, and the shell we actually exec can be non-interactive.
    # "$@" carries the claude arguments through untouched — no re-quoting, no
    # word-splitting of anything the user did not write themselves.
    local body def rc
    body=$(cr_alias_body "$sh" "$spec"); rc=$?
    if [ "$rc" -eq 0 ]; then
      CR_ARGV=("$sh" -c "$body \"\$@\"" "$spec")
      return 0
    fi
    # A shell that did not answer the first question will not answer the next two
    # either; asking anyway just multiplies the wait the user sits through.
    if [ "$rc" -eq 124 ]; then CR_RESOLVE_ERR="timeout"; return 1; fi
    if def=$(cr_function_def "$sh" "$spec"); then
      CR_ARGV=("$sh" -c "$def
$spec \"\$@\"" "$spec")
      return 0
    fi
    # Neither, on a shell that could tell us (bash 3.2 has no BASH_ALIASES). Fall
    # back to letting the interactive shell run it, but probe first so a typo
    # fails here with a clear message rather than inside the pty.
    local flags
    flags=$(cr_probe_flags "$sh")
    cr_probe "$sh" ${flags:+$flags} -ic 'command -v -- "$1" >/dev/null 2>&1' \
             cr-probe "$spec" >/dev/null || return 1
    CR_ARGV=("$sh" -ic "$spec \"\$@\"" "$spec")
    return 0
  fi
  # A command line the user wrote: a shell has to read it, but it needs no rc file
  # to do so.
  CR_ARGV=("$sh" -c "$spec \"\$@\"" "$spec")
  return 0
}

# Colon-separated interpreters to try, in order. Overridable so an unusual
# install (or a test) can point at a specific one.
: "${CR_PYTHON_CANDIDATES:=python3:/usr/bin/python3:/usr/local/bin/python3:/opt/homebrew/bin/python3}"

cr_find_python() {
  local p real IFS=:
  for p in ${CR_PYTHON:-} $CR_PYTHON_CANDIDATES; do
    [ -z "$p" ] && continue
    command -v "$p" >/dev/null 2>&1 || continue
    # Resolve to the actual interpreter. `python3` on PATH is frequently a
    # wrapper script (pyenv/asdf shims, conda stubs); running through one adds a
    # shell process between us and the supervisor that does not reliably pass the
    # child's exit status back, so `claude`'s exit code would be lost.
    real=$("$p" -c 'import sys; sys.stdout.write(sys.executable)' 2>/dev/null) || continue
    { [ -n "$real" ] && [ -f "$real" ] && [ -x "$real" ]; } || real="$p"
    if "$real" -c 'import pty,termios,select,json,re,zoneinfo' >/dev/null 2>&1; then
      printf '%s' "$real"
      return 0
    fi
  done
  return 1
}

# =============================================================================
# SECTION 4 — the wrapper itself
# =============================================================================
IFS= read -r -d '' CR_PY <<'CR_PYTHON_EOF' || true
"""agent-retrier PTY supervisor.

Runs claude on a pty we own, forwards bytes both ways untouched, and watches two
independent channels for "the session stopped because the quota ran out":

  1. the JSONL transcript  — structured, `"error":"rate_limit"`. Primary.
  2. the output stream     — pattern pairing. Fallback for when (1) is unavailable.

When a limit is seen it computes the wall-clock wait, sleeps, and types the retry
message into the pty — but only while the human is idle and claude is not busy.
The wait also ends early if the session starts answering again, which is the only
evidence there is that a limit lifted before the reset it announced.
"""
import collections
import errno
import fcntl
import glob
import json
import os
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:          # pragma: no cover - stdlib since 3.9
    ZoneInfo = None


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def parse_tokens(text):
    """"200k", "1M", "1_000_000" -> an int. Anything else -> None.

    Up here with the other config readers because it is one: both the window and
    the absolute threshold are written by hand, and `int("500k")` raising would
    fall back on the default, which for the threshold means silently off.
    """
    s = str(text or "").strip().lower().replace("_", "").replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([km])?$", s)
    if not m:
        return None
    n = float(m.group(1)) * {"k": 1e3, "m": 1e6}.get(m.group(2), 1)
    return int(n) if n > 0 else None


def _env(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except Exception:
        return default


# No single number is "the" community consensus — recommendations run 40-70%,
# with "around 60%" and "half the window" both common — but 51% is what this
# project's own docs have suggested from the start and what every claude
# session in its own logs has actually restarted at. It no longer lives here as
# a CFG default (see MODEL_PROFILES below, and model_restart_at) but the claude
# entries in that table are the same 51% baked in per model instead of applied
# blind to whatever window a session happens to report.
DEFAULT_RESTART_PCT = 51.0

# CR_CONTEXT_RESTART=1 arms the restart without making you pick a number — see
# model_restart_at for what it reaches for once armed. An explicit
# CR_CONTEXT_TOKENS (0 included) still wins outright, exactly as if this flag
# did not exist.
_CONTEXT_RESTART_ON = _env("CR_CONTEXT_RESTART", "0") != "0"

CFG = dict(
    agent=_env("CR_AGENT", "auto"),
    message=_env("CR_MESSAGE", "continue"),
    margin=_env("CR_MARGIN_SEC", 45, float),
    max_attempts=_env("CR_MAX_ATTEMPTS", 3, int),
    fallback_wait=_env("CR_FALLBACK_WAIT_SEC", 18000, float),
    max_wait=_env("CR_MAX_WAIT_SEC", 691200, float),
    user_idle=_env("CR_USER_IDLE_SEC", 20, float),
    typing_max=_env("CR_TYPING_MAX_SEC", 900, float),
    draft_grace=_env("CR_DRAFT_GRACE_SEC", 600, float),
    busy_idle=_env("CR_BUSY_IDLE_SEC", 6, float),
    resume=_env("CR_RESUME_SEC", 15, float),
    verify=_env("CR_VERIFY_SEC", 60, float),
    scrape=_env("CR_SCRAPE", "auto"),
    log=_env("CR_LOG", os.path.expanduser("~/.agent-retrier/log")),
    log_max_bytes=parse_tokens(os.environ.get("CR_LOG_MAX_BYTES") or "5M") or 5_000_000,
    log_keep=_env("CR_LOG_KEEP", 2, int),
    notify=_env("CR_NOTIFY", "1") == "1",
    badge=_env("CR_BADGE", "1") == "1",
    badge_pos=_env("CR_BADGE_POS", "bottom-right"),
    badge_label=_env("CR_BADGE_LABEL", "cr"),
    agents_overlay=_env("CR_AGENTS_OVERLAY", "0") == "1",
    agents_pos=_env("CR_AGENTS_POS", "right"),
    agents_poll=_env("CR_AGENTS_POLL_SEC", 1.0, float),
    wait_scale=max(1e-6, _env("CR_WAIT_SCALE", 1.0, float)),
    poll=_env("CR_POLL_SEC", 2.0, float),
    scrape_confirm=_env("CR_SCRAPE_CONFIRM_SEC", 3.0, float),
    # -- transient stalls --
    stall_wait=_env("CR_STALL_WAIT_SEC", 60.0, float),
    stall_backoff=_env("CR_STALL_BACKOFF", 2.0, float),
    stall_max_wait=_env("CR_STALL_MAX_WAIT_SEC", 600.0, float),
    stall_max_attempts=_env("CR_STALL_MAX_ATTEMPTS", 8, int),
    # -- context restart --
    # An explicit number here always means exactly that number; CR_CONTEXT_RESTART
    # arming the trigger with nothing else set is handled entirely in
    # model_restart_at (via context_restart_on below) — a bare percentage was
    # exactly the "one global number" MODEL_PROFILES replaced.
    context_tokens=parse_tokens(os.environ.get("CR_CONTEXT_TOKENS")) or 0,
    context_window=_env("CR_CONTEXT_WINDOW", "auto"),
    context_restart_on=_CONTEXT_RESTART_ON,
    # None means unset, which means "the same as claude's" (see `agent_cfg`).
    codex_context_tokens=(None if not os.environ.get("CR_CODEX_CONTEXT_TOKENS")
                          else parse_tokens(os.environ["CR_CODEX_CONTEXT_TOKENS"]) or 0),
    codex_hold_compact=_env("CR_CODEX_HOLD_COMPACT", "1") != "0",
    codex_reserve=parse_tokens(os.environ.get("CR_CODEX_RESERVE_TOKENS") or "64k") or 0,
    codex_interrupt=_env("CR_CODEX_INTERRUPT", "1") != "0",
    codex_interrupt_after_sec=_env("CR_CODEX_INTERRUPT_AFTER_SEC", 0.0, float),
    codex_reserve_adapt=_env("CR_CODEX_RESERVE_ADAPT", "0") == "1",
    codex_folds_file=_env("CR_CODEX_FOLDS_FILE",
                          os.path.expanduser("~/.agent-retrier/folds.json")),
    codex_logs_db=_env("CR_CODEX_LOGS_DB", ""),
    handoff_file=_env("CR_HANDOFF_FILE", ".agent-retrier/handoff.md"),
    handoff_registry_dir=_env("CR_HANDOFF_REGISTRY_DIR",
                              os.path.expanduser("~/.agent-retrier/sessions")),
    handoff_marker=_env("CR_HANDOFF_MARKER", "HANDOFF"),
    handoff_min_bytes=_env("CR_HANDOFF_MIN_BYTES", 200, int),
    handoff_attempts=_env("CR_HANDOFF_ATTEMPTS", 2, int),
    resume_attempts=_env("CR_RESUME_ATTEMPTS", 5, int),
    handoff_msg=_env("CR_HANDOFF_MSG", ""),
    clear_cmd=_env("CR_CLEAR_CMD", "/clear"),
    resume_msg=_env("CR_RESUME_MSG", "Read `{file}` and continue from it."),
    cancel_msg=_env("CR_CANCEL_MSG", ""),
    root_idle=_env("CR_ROOT_IDLE_SEC", 20.0, float),
    handoff_timeout=_env("CR_HANDOFF_TIMEOUT_SEC", 900.0, float),
    step_gap=_env("CR_STEP_GAP_SEC", 3.0, float),
    clear_settle=_env("CR_CLEAR_SETTLE_SEC", 5.0, float),
    context_cooldown=_env("CR_CONTEXT_COOLDOWN_SEC", 600.0, float),
    context_max_cycles=_env("CR_CONTEXT_MAX_CYCLES", 0, int),
    context_min_headroom=parse_tokens(os.environ.get("CR_CONTEXT_MIN_HEADROOM") or "80k") or 0,
    context_max_per_hour=_env("CR_CONTEXT_MAX_PER_HOUR", 3, int),
    notify_repeat=_env("CR_NOTIFY_REPEAT_SEC", 300.0, float),
    slash_gap=_env("CR_SLASH_GAP_SEC", 0.9, float),
    slash_enter=_env("CR_SLASH_ENTER", 2, int),
    slash_enter_gap=_env("CR_SLASH_ENTER_GAP_SEC", 0.6, float),
    # Claude Code's own switches, read from the environment we hand it: the
    # wrapper starts claude itself, so whatever narrows its window narrows ours.
    # -- a newer release of the wrapper itself --
    update_check=_env("CR_UPDATE_CHECK", "1") == "1",
    update_repo=_env("CR_UPDATE_REPO", "a0s/agent-retrier"),
    update_url=_env("CR_UPDATE_URL", ""),
    update_formula=_env("CR_UPDATE_BREW_FORMULA", "a0s/agent-retrier/agent-retrier"),
    update_cache=_env("CR_UPDATE_CACHE",
                      os.path.expanduser("~/.agent-retrier/update.json")),
    update_ttl=_env("CR_UPDATE_TTL_SEC", 86400.0, float),
    update_timeout=_env("CR_UPDATE_TIMEOUT_SEC", 10.0, float),
    update_notice=_env("CR_UPDATE_NOTICE_SEC", 2.0, float),
    version=_env("CR_VERSION", ""),
    # -- learning a window the table does not have --
    model_lookup=_env("CR_MODEL_LOOKUP", "1") == "1",
    model_lookup_timeout=_env("CR_MODEL_LOOKUP_TIMEOUT_SEC", 10.0, float),
    model_cache=_env("CR_MODEL_CACHE",
                     os.path.expanduser("~/.agent-retrier/windows.json")),
    model_cache_ttl=_env("CR_MODEL_CACHE_TTL_SEC", 604800.0, float),
    models_doc_url=_env("CR_MODELS_DOC_URL",
                        "https://platform.claude.com/docs/en/models/overview.md"),
    models_api_url=_env("CR_MODELS_API_URL", "https://api.anthropic.com/v1/models"),
    context_env_max=_env("CLAUDE_CODE_MAX_CONTEXT_TOKENS", 0, int),
    context_no_1m=_env("CLAUDE_CODE_DISABLE_1M_CONTEXT", "0") not in ("0", "false", "no"),
    # -- claude's own statusline, relayed by --cr-statusline (T20) --
    statusline_proxy=_env("CR_STATUSLINE_PROXY", "1") == "1",
    status_dir=_env("CR_STATUS_DIR", os.path.expanduser("~/.agent-retrier/status")),
)


def _patterns(var):
    raw = os.environ.get(var, "")
    out = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(re.compile(line, re.I))
        except re.error:
            pass          # a bad user pattern must never take the wrapper down
    return out


PAT = {
    "limit": _patterns("CR_PAT_LIMIT"),
    "reset": _patterns("CR_PAT_RESET"),
    "working": _patterns("CR_PAT_WORKING"),
    "menu": _patterns("CR_PAT_MENU"),
    "ignore": _patterns("CR_PAT_IGNORE"),
    "roster": _patterns("CR_PAT_ROSTER"),
    "stall": _patterns("CR_PAT_STALL"),
    "agent_row": _patterns("CR_PAT_AGENT_ROW"),
    "agents_panel_row": _patterns("CR_PAT_AGENTS_PANEL_ROW"),
}


# --------------------------------------------------------------------------- #
# terminal text handling
# --------------------------------------------------------------------------- #
_ANSI = re.compile(
    r"\x1b\][\s\S]*?(?:\x07|\x1b\\)"      # OSC
    r"|\x1bP[\s\S]*?(?:\x07|\x1b\\)"      # DCS
    r"|\x1b[_X^][\s\S]*?(?:\x07|\x1b\\)"  # APC/SOS/PM
    r"|\x1b\[[\x20-\x3f]*[\x40-\x7e]"     # CSI
    r"|\x1b[()][B0UK]"                    # charset select
    r"|\x1b[=><78MDEHc]"                  # misc single-char escapes
)


# A full-screen TUI does not print rows, it moves the cursor to them. Claude
# Code's agent roster repaints every cell through CUP and carriage returns and
# arrives here without a single "\n" in it, so dropping the escapes left the
# whole screen as ONE line — and one line always pairs some limit wording with
# some reset wording, whichever cells they came from. That is how a roster card
# reading "· resets 9:30pm … 3h" (another agent's limit, three hours old) was
# read as this session's banner and parked it until half past nine. Turning the
# motions back into line breaks restores what the eye sees: separate rows.
_ANSI_MOVE = re.compile(
    r"\x1b\[[\x30-\x3f]*[HfABEFd]"        # CUP/HVP, cursor up/down, next/prev line, VPA
    r"|\x1b[ME]"                          # RI / NEL
    r"|\r\n?"                             # a bare CR is a row of its own; CRLF is one break
)


def strip_ansi(text):
    return _ANSI.sub("", _ANSI_MOVE.sub("\n", text))


# A tool-call render quotes text ABOUT an error; it is never the live state.
# (upstream issue #63: a grep argument containing banner text parked a monitor
# for 22 hours.) Same discipline, far cheaper here because we also require the
# structured channel to be unavailable before scraping at all.
_TOOL_HEADER = re.compile(r"^\s*[●⏺∙]\s*\S+\(")
_TOOL_CHILD = re.compile(r"^\s*[⎿└↳]")


def tool_echo_mask(lines):
    mask = [False] * len(lines)
    in_block = False
    for i, line in enumerate(lines):
        if _TOOL_HEADER.search(line):
            in_block = True
            mask[i] = True
            continue
        if in_block and (_TOOL_CHILD.search(line) or (line[:1].isspace() and line.strip())):
            mask[i] = True
            continue
        in_block = False
    return mask


def _any(pats, s):
    return any(p.search(s) for p in pats)


def is_ignored(line):
    return _any(PAT["ignore"], line)


def is_limit_line(line):
    return (not is_ignored(line)) and _any(PAT["limit"], line)


def is_reset_line(line):
    return _any(PAT["reset"], line)


def is_stall_line(line):
    return (not is_ignored(line)) and _any(PAT["stall"], line)


def is_working(text):
    return _any(PAT["working"], text)


def is_menu(text):
    return _any(PAT["menu"], strip_ansi(text))


def is_roster(text):
    """Is this the agent roster rather than a session? Then it is not ours."""
    return _any(PAT["roster"], text)


WINDOW = 6          # how far apart a limit line and its reset line may sit


def find_limit(text):
    """Return the banner line if `text` contains a live limit render, else None.

    Requires a LIMIT line with a RESET line within WINDOW lines — the pairing is
    what separates a banner from prose that merely says the word "limit". Scans
    bottom-up so the freshest banner wins over a stale one further up. The agent
    roster is skipped whole: everything on it belongs to other sessions.
    """
    flat = strip_ansi(text)
    if is_roster(flat):
        return None
    lines = [l.rstrip() for l in flat.split("\n")]
    mask = tool_echo_mask(lines)
    for i in range(len(lines) - 1, -1, -1):
        if mask[i] or not is_limit_line(lines[i]):
            continue
        lo, hi = max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)
        for j in range(lo, hi):
            if mask[j] or not is_reset_line(lines[j]):
                continue
            # Prefer whichever of the two carries the reset time: that is what
            # gets parsed. Joining keeps a two-line render ("⚠ limit" / "· resets
            # 3pm") intact for the parser.
            if i == j:
                return lines[i].strip()
            return (lines[i].strip() + " " + lines[j].strip()).strip()
    return None


def find_stall(text):
    """Return the line saying the server refused the turn, else None.

    No pairing here, because there is nothing to pair with: a stall states no
    reset time — that is exactly what makes it one. The narrowness of the
    patterns is what stands in for the pairing, so this stays a bottom-up scan
    with the same two exclusions (a tool call quoting the words, the roster).
    """
    flat = strip_ansi(text)
    if is_roster(flat):
        return None
    lines = [l.rstrip() for l in flat.split("\n")]
    mask = tool_echo_mask(lines)
    for i in range(len(lines) - 1, -1, -1):
        if mask[i] or not is_stall_line(lines[i]):
            continue
        return lines[i].strip()
    return None


# --------------------------------------------------------------------------- #
# reset-time parsing
# --------------------------------------------------------------------------- #
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_WEEKDAYS = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}

# "resets [on] [Jul 22] [at] 6[:30][am] [(Europe/Warsaw)]", and codex's own
# wording for the same thing: "try again at Aug 20th, 2026 10:00 AM".
_ABS = re.compile(
    r"(?:reset(?:s|ting)?|try again|available again|come back)\s+(?:(?:at|on)\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4}),?\s+)?)?"
    r"(?:(?P<rel>tomorrow|today|tonight)\s+)?"
    r"(?:(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?"
    r"(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
    r"(?:\s*\((?P<tz>[^)]{2,40})\))?",
    re.I)
# same, but the clock time comes before the date: "resets at 6am on Jul 22"
_ABS_TAIL_DATE = re.compile(
    r"\bon\s+(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})",
    re.I)
_REL = re.compile(
    r"(?:try again|come back|resets?|available again|wait|retry[- ]after)"
    r"(?:\s+in|\s+after|:)?\s*(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.I)
# The HTTP header form carries no unit — it is seconds by definition.
_RETRY_AFTER = re.compile(r"retry[- ]after[:\s]+(?P<n>\d+)\b", re.I)
_ISO = re.compile(r"(?P<iso>\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?)\s*(?P<z>z|[+-]\d{2}:?\d{2})?", re.I)


def _tz(name):
    if not name or ZoneInfo is None:
        return None
    name = name.strip()
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    # Abbreviations ("UTC", "PST") are not IANA ids; only the unambiguous ones.
    fixed = {"utc": 0, "gmt": 0, "z": 0}
    if name.lower() in fixed:
        try:
            return ZoneInfo("UTC")
        except Exception:
            return None
    return None


def parse_reset(text, now=None):
    """Seconds to wait, or None when nothing parseable is present.

    `now` is injectable so the tests can pin a moment; it must be timezone-aware.
    """
    if not text:
        return None
    now = now or datetime.now().astimezone()

    m = _ISO.search(text)
    if m:
        raw = m.group("iso").replace(" ", "T")
        z = m.group("z")
        try:
            if z and z.lower() == "z":
                target = datetime.fromisoformat(raw + "+00:00")
            elif z:
                target = datetime.fromisoformat(raw + z)
            else:
                target = datetime.fromisoformat(raw).replace(tzinfo=now.tzinfo)
            return max(0.0, (target - now).total_seconds())
        except Exception:
            pass

    m = _REL.search(text)
    if m:
        n = float(m.group("n"))
        u = m.group("unit").lower()
        mult = 1 if u.startswith("s") else 60 if u.startswith("m") else 3600 if u.startswith("h") else 86400
        return n * mult

    m = _RETRY_AFTER.search(text)
    if m:
        return float(m.group("n"))

    m = _ABS.search(text)
    if not m:
        return None

    tz = _tz(m.group("tz")) or now.tzinfo
    local_now = now.astimezone(tz)

    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ap = (m.group("ap") or "").lower()
    if hour > 23 or minute > 59:
        return None
    ambiguous = not ap and 1 <= hour <= 12
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0

    def at(day_offset=0, h=None, y=None, mo=None, d=None):
        base = local_now + timedelta(days=day_offset)
        return base.replace(
            year=y or base.year, month=mo or base.month, day=d or base.day,
            hour=hour if h is None else h, minute=minute, second=0, microsecond=0)

    mon = m.group("mon")
    day = m.group("day")
    if not mon:
        tail = _ABS_TAIL_DATE.search(text)
        if tail:
            mon, day = tail.group("mon"), tail.group("day")

    candidates = []
    if mon and day:
        mo, d = _MONTHS[mon[:3].lower()], int(day)
        # A stated year settles it. Without one the date could be either side of
        # a new year, so both are offered and the nearest future one wins below.
        stated = m.group("year")
        years = (int(stated),) if stated else (local_now.year, local_now.year + 1)
        for year in years:
            try:
                candidates.append(at(y=year, mo=mo, d=d))
            except ValueError:
                pass
    elif m.group("wd"):
        want = _WEEKDAYS[m.group("wd").lower()]
        delta = (want - local_now.weekday()) % 7
        candidates.append(at(day_offset=delta))
        candidates.append(at(day_offset=delta + 7))
    else:
        rel = (m.group("rel") or "").lower()
        base_off = 1 if rel == "tomorrow" else 0
        for off in (base_off, base_off + 1):
            candidates.append(at(day_offset=off))
            if ambiguous:
                candidates.append(at(day_offset=off, h=(hour + 12) % 24))

    # Subtracting two datetimes that share a tzinfo is defined to compare WALL
    # CLOCK, silently ignoring the zone — so across a DST transition it is off by
    # an hour in whichever direction hurts (waking early with the banner still
    # live burns the attempt budget). Compare absolute instants instead.
    ref = local_now.timestamp()
    deltas = sorted(c.timestamp() - ref for c in candidates)

    # A reset stated in the recent past means it just happened — retry now rather
    # than rolling a full day forward (upstream: "resets 10am" seen at 10:03
    # parked the session for ~24h).
    future = [d for d in deltas if d > -3600]
    if not future:
        return None
    return max(0.0, future[0])


# --------------------------------------------------------------------------- #
# transcript watcher — the structured, non-scraping channel
# --------------------------------------------------------------------------- #
def project_dir(cwd=None, config_dir=None):
    cwd = cwd or os.getcwd()
    config_dir = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return os.path.join(config_dir, "projects", slug)


# Matched against the raw row, before any JSON parsing: the rows we care about
# are a small fraction of a transcript and the rest are large.
_ASSISTANT_ROW = re.compile(rb'"type"\s*:\s*"assistant"')
_COMPACT_ROW = re.compile(rb'"subtype"\s*:\s*"compact_boundary"')
# A prompt a person submitted, as opposed to one a peer session, a subagent's
# hand-back or a local command like `/clear` wrote into the same transcript:
# Claude Code marks the person's own with `turnOrigin: "human"` (and leaves it
# off the `<command-name>/clear</command-name>` row entirely).
_HUMAN_PROMPT_ROW = re.compile(rb'"turnOrigin"\s*:\s*"human"')

# The claude CLI's own `/model` command answers itself, before any turn ever
# reaches the API: "<command-name>/model</command-name>" is the invocation,
# "<local-command-stdout>Set model to <display>[ (<slug>)]</local-command-stdout>"
# is the answer (T21). Only the stdout line is matched -- it already names the
# switch unambiguously, so tracking the command-name row that precedes it
# would only add state across rows for no further certainty. A slug in
# parentheses, if the display carries one, is trusted outright; otherwise the
# display name is transformed (`_display_to_claude_slug`, defined by
# MODEL_PROFILES further down but only ever called once a row like this is
# actually seen).
_LOCAL_MODEL_ROW = re.compile(
    r"<local-command-stdout>\s*Set model to\s+(?P<display>[^(<\n]+?)"
    r"(?:\s*\((?P<slug>[a-z][a-z0-9.-]*)\))?\s*</local-command-stdout>", re.IGNORECASE)


def assistant_row(rec):
    """What an assistant row says beyond "the session answered".

    `tokens` is the context that turn was sent with, straight out of the API's
    own accounting; `stop_reason` is how the turn ended, which is the difference
    between a model that finished and one that was cut off mid-sentence.

    `sidechain` is here because a subagent started with the Agent tool writes
    into the SAME transcript as the session that started it, and its usage is ITS
    context, not the session's. Read as the session's it would either hide a full
    window behind a small subagent or call for a restart over a subagent that
    grew large.
    """
    msg = rec.get("message") or {}
    model = msg.get("model")
    if isinstance(model, str) and model.startswith("<"):
        # "<synthetic>" etc: Claude Code's stand-in for a turn with no real
        # model response ("No response requested", an interrupted request).
        # The row still proves the account answered, but it carries no
        # usable data about tokens/model/stop_reason.
        return dict(kind="alive", ts=rec.get("timestamp"),
                    tokens=None, model=None, stop_reason=None,
                    sidechain=bool(rec.get("isSidechain")))
    return dict(kind="alive", ts=rec.get("timestamp"),
                tokens=usage_tokens(msg.get("usage")),
                model=model,
                stop_reason=msg.get("stop_reason"),
                sidechain=bool(rec.get("isSidechain")))


def _appended(path, offset):
    """(new_offset, [whole rows]) added past `offset`, as bytes.

    Both transcript formats are one JSON object per line appended to a file we
    are following, so the following is shared and only the reading of a row
    differs. A partial last line is left for the next poll rather than parsed
    half-written.
    """
    try:
        with open(path, "rb") as fh:
            if offset:
                # Our offset is only meaningful if it still lands on a record
                # boundary. If the file was rewritten rather than appended to,
                # it points into the middle of a line and everything after it
                # would decode as garbage — start over instead.
                fh.seek(offset - 1)
                if fh.read(1) != b"\n":
                    offset = 0
            fh.seek(offset)
            data = fh.read()
            new_offset = fh.tell()
    except OSError:
        return offset, []
    if not data:
        return new_offset, []
    tail_nl = data.rfind(b"\n")
    if tail_nl == -1:
        return offset, []                     # a partial line: re-read it next tick
    return offset + tail_nl + 1, data[:tail_nl].split(b"\n")


def _echo_keys(echo):
    """The messages we typed, and what they look like once written into a row."""
    echoes = [echo] if isinstance(echo, str) else [e for e in (echo or []) if e]
    # json.dumps rather than quoting by hand: a message with a quote, a backslash
    # or a tab in it is escaped in the row exactly the way it is escaped here.
    # ensure_ascii=False is deliberate: both Claude Code and codex write a non-ASCII
    # character (an em dash in the default phrase, Cyrillic in a custom one) as its
    # raw UTF-8 bytes, never as a \uXXXX escape -- encoding this key the default
    # (ASCII-escaped) way would produce a byte string that can never appear in a
    # real transcript, silently failing this prefilter and leaving the echo (and
    # everything gated on it: /clear, the whole restart) waiting forever.
    return echoes, [json.dumps(e, ensure_ascii=False).encode("utf-8", "replace") for e in echoes]


def transcript_limit_records(path, offset=0, echo=None):
    """(new_offset, [records]) for rows appended past `offset` that we care about.

    Four kinds: the rate-limit rows; — when `echo` is a message we typed, or a
    list of them — the user row claude writes when it accepts a prompt;
    assistant rows; and the row claude writes when IT compacts the context on
    its own (`compact_boundary`, T19). That user row is proof the message was
    submitted rather than left sitting in the input box, which is the only
    question the verify step is really asking (screen-scraping the footer to
    answer it broke the day Claude Code reworded it). An assistant row is proof
    the account is serving requests, which is the question a wait is really
    asking — and it carries the usage figures the context trigger reads. A
    `compact_boundary` row states the one figure this build cannot otherwise
    know until it is too late: how many tokens the context actually held right
    before claude decided on its own that it was full.

    A prompt a person submitted (`turnOrigin: "human"`) that is none of the
    echoes comes back as "prompt": the one sign there is that someone took the
    session over in the middle of a restart.

    A message typed while a turn is still running never gets that user row:
    Claude Code queues it, logs a `queue-operation` "enqueue" row, and later
    hands it to the model as an `attachment` row of type `queued_command`
    (mid-turn, or once the turn ends). The attachment is the same proof the
    user row is and comes back as an "echo"; the enqueue row only says the
    message is waiting — it can still be pulled back out of the queue — and
    comes back as its own kind, "queued".

    Consecutive assistant rows collapse into one: a single answer can be a dozen
    of them, and the caller only needs the fact. A `compact_boundary` row never
    joins that collapse in either direction — merging it into a neighbour would
    hide the model/stop_reason it does not carry, or hide the compaction fact
    inside a row nobody would think to check for it.
    """
    out = []
    echoes, echo_keys = _echo_keys(echo)
    new_offset, rows = _appended(path, offset)
    for raw in rows:
        limitish = b"rate_limit" in raw or b"isApiErrorMessage" in raw
        echoish = ((b'"user"' in raw or b"queued_command" in raw or b"queue-operation" in raw)
                   and any(k in raw for k in echo_keys))
        modelish = b"local-command-stdout" in raw and b"Set model to" in raw
        promptish = bool(_HUMAN_PROMPT_ROW.search(raw))
        # Prefilters only — all four can match on a tool result that merely
        # quotes the words, so the row's own "type" decides below.
        aliveish = bool(_ASSISTANT_ROW.search(raw))
        compactish = bool(_COMPACT_ROW.search(raw))
        if (not limitish and not echoish and not aliveish and not compactish
                and not modelish and not promptish):
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if compactish and rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            pre = (rec.get("compactMetadata") or {}).get("preTokens")
            out.append(dict(kind="alive", ts=rec.get("timestamp"), quiet=True,
                            compacted=True, sidechain=False,
                            pre_tokens=int(pre) if isinstance(pre, (int, float)) else None))
            continue
        if echoish and rec.get("type") in ("attachment", "queue-operation"):
            att = rec.get("attachment") if rec.get("type") == "attachment" else None
            if isinstance(att, dict) and att.get("type") == "queued_command":
                queued = att.get("prompt")
                if isinstance(queued, str) and queued.strip() in echoes:
                    out.append(dict(kind="echo", text=queued.strip(), ts=rec.get("timestamp")))
            elif rec.get("type") == "queue-operation" and rec.get("operation") == "enqueue":
                queued = rec.get("content")
                if isinstance(queued, str) and queued.strip() in echoes:
                    out.append(dict(kind="queued", text=queued.strip(), ts=rec.get("timestamp")))
            continue
        if not rec.get("isApiErrorMessage"):
            text = (record_text(rec)
                    if (echoish or modelish or promptish) and rec.get("type") == "user" else None)
            if text is not None and echoish and text in echoes:
                out.append(dict(kind="echo", text=text, ts=rec.get("timestamp")))
            elif (text is not None and promptish and rec.get("turnOrigin") == "human"
                    and not rec.get("isMeta") and not rec.get("isSidechain")):
                out.append(dict(kind="prompt", text=text, ts=rec.get("timestamp")))
            elif text is not None and modelish:
                m = _LOCAL_MODEL_ROW.search(text)
                slug = (m and (m.group("slug") or _display_to_claude_slug(m.group("display"))))
                if slug:
                    # An early hint only (T21): the next real assistant row's
                    # "model" field is the one `on_model` trusts as confirmed,
                    # and overrides this the moment it disagrees.
                    out.append(dict(kind="alive", ts=rec.get("timestamp"), quiet=True,
                                    sidechain=False, model_hint=slug))
            elif rec.get("type") == "assistant":
                row = assistant_row(rec)
                prev = out[-1] if out else None
                if (prev and prev["kind"] == "alive" and not prev.get("compacted")
                        and prev["sidechain"] == row["sidechain"]):
                    # The collapse keeps the NEWEST row's figures. Keeping the
                    # older row's would freeze the context reading at whatever it
                    # was when the answer began, which for a long answer is a
                    # threshold that arrives an answer late. stop_reason is the
                    # one field exempt from that: a streaming fragment reports
                    # stop_reason=None, and letting it overwrite a real value
                    # (e.g. "end_turn" from the row that just closed the turn)
                    # would erase the very fact a caller needs.
                    stop_reason = row["stop_reason"]
                    if stop_reason is None:
                        stop_reason = prev["stop_reason"]
                    prev.update(row)
                    prev["stop_reason"] = stop_reason
                else:
                    # A sidechain row never merges with a root row (or vice
                    # versa): its usage figures belong to a subagent, not the
                    # session, and mixing them into one record would hide
                    # whichever side lost the merge from the caller entirely.
                    out.append(row)
            continue
        err = str(rec.get("error") or "")
        status = rec.get("apiErrorStatus")
        if err != "rate_limit" and status != 429:
            continue
        out.append(dict(kind="limit", text=record_text(rec), ts=rec.get("timestamp"),
                        error=err))
    return new_offset, out


def record_text(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text").strip()
    if isinstance(content, str):
        return content.strip()
    return ""


# --------------------------------------------------------------------------- #
# codex — the same two questions, asked of a different file
# --------------------------------------------------------------------------- #
# codex keeps one JSONL "rollout" per thread under
# $CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/, and it says more than Claude Code's
# transcript does: the turn boundaries are in it, so is the size of the context
# window, and so is the reason a turn ended. Nothing here has to be inferred
# from the screen.
#
# The rows that matter, all shaped `{"type": …, "payload": {"type": …}}`:
#
#   event_msg / task_started     a turn began, and the window it runs in.
#   event_msg / task_complete    a turn ended. With no `error` it ended well;
#                                with one, `codex_error_info` separates "the
#                                account ran out" from "the server did".
#   event_msg / turn_aborted     a turn ended because someone pressed Esc.
#   event_msg / token_count      what the last request was sent with, and how
#                                large the window is: the context trigger's two
#                                numbers, in codex's own accounting.
#   response_item / message      role "user" is the echo of what was typed;
#                                role "assistant" is the session answering.
#   turn_context                 which model (0.154); older builds said it in
#   event_msg / thread_settings_applied   instead.
#
# The turn boundaries are what a context restart leans on. Claude Code writes a
# stop_reason into every answer and codex writes none, so "the folding turn
# closed cleanly" is read off task_complete instead — the same fact, stated by
# the runtime rather than the model — and "the session is mid-turn" is the span
# between task_started and its ending, which stays true while the root sits for
# minutes waiting on its agents without writing a byte.
#
# The error that prompted all of this is a turn ending with
# `{"message": "Selected model is at capacity. …", "codex_error_info":
# "server_overloaded"}` — a stall: no reset time, no way out of itself, and on a
# session running agents it takes every one of them down with it.
CODEX_LIMIT_INFO = ("usage_limit_exceeded", "usage_limit_reached",
                    "rate_limit_exceeded", "rate_limit", "quota_exceeded")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def codex_text(payload):
    """The words in a codex message row, whichever content shape it uses."""
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return " ".join(parts).strip()


def codex_records(path, offset=0, echo=None):
    """(new_offset, [records]) for the rollout rows appended past `offset`.

    Emits the same five kinds the rest of the wrapper already speaks — limit,
    stall, echo, alive, and an alive marked `quiet` for a row that names the
    model without proving anything is being served. A row that opens or closes a
    turn also carries `turn`: "open" or "closed".

    Rows are not collapsed the way Claude Code's assistant rows are. There, a
    dozen rows are one answer; here each row is a different statement about the
    turn, and merging them would let one without figures blank out the one that
    had them.
    """
    out = []
    echoes, echo_keys = _echo_keys(echo)
    new_offset, rows = _appended(path, offset)
    for raw in rows:
        # Prefilters only, and deliberately spelling-insensitive: codex has
        # written these files both with and without spaces after the colons.
        interesting = (b"task_complete" in raw or b"token_count" in raw
                       or b"task_started" in raw or b"turn_aborted" in raw
                       or b"turn_context" in raw or b'"compacted"' in raw
                       or b'"assistant"' in raw or b"thread_settings_applied" in raw
                       or (echo_keys and any(k in raw for k in echo_keys)))
        if not interesting:
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = rec.get("timestamp")
        if rec.get("type") == "turn_context":
            model = payload.get("model")
            if model:
                out.append(dict(kind="alive", ts=ts, quiet=True, sidechain=False,
                                model=str(model)))
            continue
        if rec.get("type") == "compacted":
            # codex compacted the thread itself. Written when it is over — nothing
            # in the rollout announces one starting — so this can only be
            # reported, never prevented from here.
            out.append(dict(kind="alive", ts=ts, quiet=True, sidechain=False,
                            compacted=True))
            continue
        kind = payload.get("type")
        if kind == "task_started":
            # Opens a turn and proves nothing else: a turn the server is about
            # to refuse starts exactly like one it will serve.
            window = payload.get("model_context_window")
            out.append(dict(kind="alive", ts=ts, quiet=True, sidechain=False, turn="open",
                            window=int(window) if isinstance(window, (int, float)) else None))
        elif kind == "turn_aborted":
            out.append(dict(kind="alive", ts=ts, quiet=True, sidechain=False,
                            turn="closed", stop_reason="aborted"))
        elif kind == "task_complete":
            err = payload.get("error")
            if not isinstance(err, dict) or not err:
                # A turn that ended on its own terms. It says the session is
                # alive AND that whatever was refusing turns has stopped — and,
                # for a restart, that the folding turn landed: end_turn is the
                # word the rest of the wrapper already uses for that.
                out.append(dict(kind="alive", ts=ts, clean=True, sidechain=False,
                                turn="closed", stop_reason="end_turn"))
                continue
            text = str(err.get("message") or "").strip()
            info = str(err.get("codex_error_info") or "").strip().lower()
            if info in CODEX_LIMIT_INFO or (not info and is_limit_line(text)):
                out.append(dict(kind="limit", text=text, ts=ts, turn="closed",
                                error=info or "usage_limit"))
            else:
                # Everything else the server refused a turn over. The wait for
                # one is ours to pick, so it does not matter which it was.
                out.append(dict(kind="stall", text=text or info or "the turn was refused",
                                ts=ts, turn="closed", error=info))
        elif kind == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last = info.get("last_token_usage")
            tokens = last.get("total_tokens") if isinstance(last, dict) else None
            window = info.get("model_context_window")
            out.append(dict(kind="alive", ts=ts, sidechain=False,
                            tokens=int(tokens) if isinstance(tokens, (int, float)) else None,
                            window=int(window) if isinstance(window, (int, float)) else None))
        elif kind == "thread_settings_applied":
            settings = payload.get("thread_settings")
            model = settings.get("model") if isinstance(settings, dict) else None
            if model:
                out.append(dict(kind="alive", ts=ts, quiet=True, sidechain=False,
                                model=str(model)))
        elif kind == "message":
            role = payload.get("role")
            if role == "user":
                text = codex_text(payload)
                if text and text in echoes:
                    out.append(dict(kind="echo", text=text, ts=ts))
            elif role == "assistant":
                out.append(dict(kind="alive", ts=ts, sidechain=False))
    return new_offset, out


class ClaudeAgent:
    """Where Claude Code writes, and how to read it."""

    name = "claude"

    def __init__(self, directory=None):
        self.dir = project_dir() if directory is None else directory

    def paths(self, now=None):
        return sorted(glob.glob(os.path.join(self.dir, "*.jsonl")))

    def keep(self, path):
        return True          # every transcript in a project dir is a session's

    def records(self, path, offset, echo):
        return transcript_limit_records(path, offset, echo)

    def path_for(self, session_id):
        return os.path.join(self.dir, session_id + ".jsonl")


class ClaudeSessionRegistry:
    """Where claude's own identity lives: `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`.

    The pid this wrapper forked is not always the pid claude runs as: a shell
    alias (`sh -c "alias claude=...; claude"`) execs a shell that then execs
    claude as a *grandchild*, never touching the pid we hold. `<child_pid>.json`
    is tried first; when it is missing, every `sessions/*.json` whose `cwd`
    matches ours is checked for being a descendant of the child (walking `ppid`
    the way `ps -o ppid=` would) or, failing that, having started at or after
    our own start time. More than one candidate surviving that scan means no
    bind: a wrong guess here is the whole bug T02 exists to remove.
    """

    def __init__(self, child_pid, cwd=None, config_dir=None, started_at=None,
                log=None, proc_table=None):
        self.child_pid = child_pid
        self.cwd = cwd or os.getcwd()
        base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        self.dir = os.path.join(base, "sessions")
        self.started_at = started_at      # ms epoch, for the cwd+startedAt fallback
        self.log = log or (lambda *_: None)
        self.proc_table = proc_table or self._ps_table
        self._logged_fallback = False
        self._logged_ambiguous = False

    def _read(self, pid):
        try:
            with open(os.path.join(self.dir, "%d.json" % pid)) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _ps_table():
        """pid -> ppid for every process on the box, read once per lookup()."""
        try:
            out = subprocess.run(["ps", "-axo", "pid=,ppid="],
                                 capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return {}
        table = {}
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    table[int(parts[0])] = int(parts[1])
                except ValueError:
                    pass
        return table

    def _is_descendant(self, pid, table):
        seen = set()
        cur = table.get(pid)
        while cur and cur not in seen:
            if cur == self.child_pid:
                return True
            seen.add(cur)
            cur = table.get(cur)
        return False

    def _is_recent(self, started_at):
        """`startedAt >= ours`, tolerant of whatever a stranger's file holds.

        A malformed or foreign-format entry (a wrong Claude Code version, a
        half-written file) must lose the candidate, never crash the wrapper
        reading it.
        """
        if not isinstance(started_at, (int, float)):
            return False
        return started_at >= self.started_at

    def lookup(self):
        """`(record, why)` for our session, or `(None, None)` if nothing binds."""
        direct = self._read(self.child_pid)
        if direct:
            self._logged_ambiguous = self._logged_fallback = False
            return direct, "pid file"
        try:
            names = os.listdir(self.dir)
        except OSError:
            names = []
        table = None
        matches = []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                pid = int(name[:-5])
            except ValueError:
                continue
            rec = self._read(pid)
            if not rec or rec.get("cwd") != self.cwd:
                continue
            if table is None:
                table = self.proc_table()
            if self._is_descendant(pid, table):
                matches.append((rec, "descendant of pid %d" % self.child_pid))
            elif self.started_at is not None and self._is_recent(rec.get("startedAt")):
                matches.append((rec, "cwd+startedAt"))
        if len(matches) == 1:
            self._logged_ambiguous = self._logged_fallback = False
            return matches[0]
        if len(matches) > 1:
            if not self._logged_ambiguous:
                self.log("session registry: %d candidates for pid %d in %s; not binding"
                         % (len(matches), self.child_pid, self.cwd))
                self._logged_ambiguous = True
            return None, None
        self._logged_ambiguous = False
        if not self._logged_fallback:
            self.log("no session registry for pid %d; falling back to newest transcript"
                     % self.child_pid)
            self._logged_fallback = True
        return None, None


class CodexAgent:
    """Where codex writes, and which of those files are this terminal's.

    Three things differ from Claude Code and all three matter. The rollouts of
    every project live in one tree, under a directory named for the day — so
    the scan is dated rather than fixed, and spans three of them, because
    "which day" is a question the local clock and codex can answer differently
    and a session can outlive midnight either way. A subagent gets a rollout
    of its own: reading one as the session's would report a limit this
    terminal never hit and a context that is not ours to restart. And
    `codex resume <old>` writes into whichever day that rollout was CREATED
    on, which the three-day scan can be long past by the time it happens
    (T03) — so `paths` also looks at the rest of the tree, for anything that
    has changed since we started.
    """

    name = "codex"
    OLD_SCAN_INTERVAL = 30.0   # a resumed session touches one file in a tree that
                               # can be months deep; walking all of it every poll
                               # would cost more than the feature is worth

    def __init__(self, home=None, cwd=None, now=None):
        self.home = home or codex_home()
        self.dir = os.path.join(self.home, "sessions")
        self.cwd = os.path.realpath(cwd or os.getcwd())
        self.started_at = now if now is not None else time.time()
        self._verdicts = {}          # path -> is this session's, once we can tell
        self._threads = {}           # path -> the thread id its head names
        self._old_paths = []         # last full-tree scan's answer (throttled)
        self._old_scan_at = 0.0

    def paths(self, now=None):
        now = now if now is not None else time.time()
        out = []
        for offset in (-86400, 0, 86400):
            t = time.localtime(now + offset)
            out.extend(glob.glob(os.path.join(
                self.dir, "%04d" % t.tm_year, "%02d" % t.tm_mon, "%02d" % t.tm_mday,
                "*.jsonl")))
        out.extend(self._resumed_paths(now))
        return sorted(set(out))

    def _resumed_paths(self, now):
        """Files anywhere under `sessions/` that changed after we started.

        `codex resume` reopens a rollout in place; nothing about that touches
        the mtime of the day directory it lives in, only the file's own, so
        there is no shortcut past looking at each one -- only at how often.
        """
        if now - self._old_scan_at < self.OLD_SCAN_INTERVAL:
            return self._old_paths
        self._old_scan_at = now
        found = []
        for dirpath, _dirnames, filenames in os.walk(self.dir):
            for name in filenames:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) > self.started_at:
                        found.append(path)
                except OSError:
                    continue
        self._old_paths = found
        return found

    def keep(self, path):
        verdict = self._verdicts.get(path)
        if verdict is None:
            verdict = self._classify(path)
            if verdict is not None:
                self._verdicts[path] = verdict
        # Unknown means the head of the file has not landed yet. Reading it for
        # one more poll is the cheap mistake; ignoring a session's own rollout
        # for the rest of its life is not.
        return verdict is not False

    def _classify(self, path):
        """True: this terminal's. False: somebody else's. None: cannot tell yet."""
        head = b""
        try:
            with open(path, "rb") as fh:
                while len(head) < 4 * 1024 * 1024:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    head += chunk
                    if b"\n" in head:
                        break
        except OSError:
            return None
        line, sep, _ = head.partition(b"\n")
        if not sep:
            return None
        try:
            rec = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return None
        payload = rec.get("payload")
        if rec.get("type") != "session_meta" or not isinstance(payload, dict):
            return None
        if payload.get("id"):
            self._threads[path] = str(payload["id"])
        if payload.get("thread_source") == "subagent":
            return False
        if "subagent" in str(payload.get("source") or ""):
            return False
        cwd = payload.get("cwd")
        if cwd:
            try:
                return os.path.realpath(cwd) == self.cwd
            except Exception:
                return True
        return True

    def records(self, path, offset, echo):
        return codex_records(path, offset, echo)

    def thread_id(self, path):
        """The thread a rollout belongs to, which is how codex's own log names it."""
        if path and path not in self._threads:
            self._classify(path)
        return self._threads.get(path)


# --------------------------------------------------------------------------- #
# codex's own count — the number its compaction is decided on
# --------------------------------------------------------------------------- #
# The rollout says what the last request was sent with. codex decides whether to
# compact on something larger: that figure, plus an estimate of every item added
# since and of the reasoning it carries — 251,023 against a rollout reading of
# 232,673 in one session that compacted. A threshold read off the rollout is
# therefore a threshold codex can pass first. codex writes the number it actually
# compares, and the limit it compares it with, into its log database after every
# request (TRACE, on by default):
#
#   post sampling token usage turn_id=… total_usage_tokens=251023
#     auto_compact_scope_tokens=251023 auto_compact_scope_limit=Some(244800)
#     … full_context_window_limit=Some(258400) … token_limit_reached=true
#
# Compaction happens once the scope count reaches the scope limit or the total
# reaches the full-window cap, so the total at which it happens is
# min(full, total - scope + scope_limit): that one expression covers both scopes.
_USAGE_ROW = re.compile(
    r"post sampling token usage\b.*?\btotal_usage_tokens=(?P<total>\d+)"
    r".*?\bauto_compact_scope_tokens=(?P<scope>\d+)"
    r".*?\bauto_compact_scope_limit=(?:Some\((?P<limit>\d+)\)|None)"
    r".*?\bfull_context_window_limit=(?:Some\((?P<full>\d+)\)|None)", re.S)


def parse_usage_row(body):
    """(total, compaction point) from one log row, or None when it is not one."""
    m = _USAGE_ROW.search(body or "")
    if not m:
        return None
    total, scope = int(m.group("total")), int(m.group("scope"))
    caps = []
    if m.group("limit"):
        caps.append(total - scope + int(m.group("limit")))
    if m.group("full"):
        caps.append(int(m.group("full")))
    return total, (min(caps) if caps else None)


class CodexUsageLog:
    """Follows codex's log database for one thread's usage rows.

    Read-only, polled, and allowed to fail in every way a file owned by another
    program can: missing, locked, migrated to a new name. Any of those leaves the
    rollout's figures in charge, which is where things stood before this existed.
    """

    NEEDLE = "%post sampling token usage%"

    def __init__(self, cfg, log, home=None):
        self.log = log
        self.path = cfg.get("codex_logs_db") or None
        self.home = home or codex_home()
        self.conn = None
        self.last_id = {}            # thread -> newest row already read
        self.failed = False
        self.next_poll = 0.0
        self.poll_every = cfg.get("poll", 2.0)

    def _db(self):
        if self.path:
            return self.path
        found = sorted(glob.glob(os.path.join(self.home, "logs_*.sqlite")),
                       key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
        return found[-1] if found else None

    def _connect(self):
        if self.conn is not None:
            return self.conn
        path = self._db()
        if not path or not os.path.exists(path):
            return None
        try:
            import sqlite3
            self.conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.2,
                                        check_same_thread=False)
        except Exception as exc:
            if not self.failed:
                self.log("cannot read codex's log database %s: %s" % (path, exc))
            self.failed = True
            return None
        self.log("reading codex's own context count from %s" % path)
        return self.conn

    def poll(self, thread, now):
        """[(total, compaction point)] written for `thread` since the last call."""
        if not thread or now < self.next_poll:
            return []
        self.next_poll = now + self.poll_every
        conn = self._connect()
        if conn is None:
            return []
        try:
            if thread not in self.last_id:
                # A resumed thread can have thousands of these; only the newest
                # says anything about now.
                rows = conn.execute(
                    "SELECT id, feedback_log_body FROM logs WHERE thread_id = ? "
                    "AND feedback_log_body LIKE ? ORDER BY id DESC LIMIT 1",
                    (thread, self.NEEDLE)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, feedback_log_body FROM logs WHERE thread_id = ? "
                    "AND id > ? AND feedback_log_body LIKE ? ORDER BY id",
                    (thread, self.last_id[thread], self.NEEDLE)).fetchall()
        except Exception as exc:
            # Locked mid-write, or rotated away: drop the handle and ask again
            # next time rather than holding on to a file that has moved.
            self.log("codex's log database did not answer: %s" % exc)
            try:
                conn.close()
            except Exception:
                pass
            self.conn = None
            return []
        self.last_id.setdefault(thread, 0)
        out = []
        for row_id, body in rows:
            self.last_id[thread] = max(self.last_id[thread], row_id)
            parsed = parse_usage_row(body)
            if parsed:
                out.append(parsed)
        return out


# codex takes the directory to work in on the command line, and when it is given
# one the rollouts to follow are that directory's rather than ours.
_CD_FLAGS = ("--cd", "--cwd", "-C")


def codex_cwd(argv):
    argv = list(argv or [])
    for i, arg in enumerate(argv):
        for flag in _CD_FLAGS:
            if arg == flag and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(flag + "="):
                return arg[len(flag) + 1:]
    return None


def pick_agent(name, launch):
    """Which of the two we are wrapping: what was asked for, or what was run.

    Only the launch vector is read, never the arguments meant for the agent. A
    prompt is one of those — `agent-retrier "fix the codex build"` starts
    claude, and reading the sentence would have it looking for a rollout file
    that will never be written.
    """
    name = (name or "auto").strip().lower()
    if name not in ("auto", ""):
        return name
    hay = " ".join(launch or [])
    return "codex" if re.search(r"(?:^|[/\s'\"])codex(?:[\s'\"]|$)", hay) else "claude"


def build_agent(name, argv=None):
    if name == "codex":
        return CodexAgent(cwd=codex_cwd(argv))
    return ClaudeAgent()


class TranscriptWatcher:
    """Follows every transcript in this project that grows after we start.

    With a `registry` (claude, Claude Code >= 2.1.273), identity is explicit:
    `current` is whatever `sessions/<pid>.json` names, never a guess, and
    `_pick_current`'s growth heuristic never runs. Without one (codex, or an
    older claude with no pid file), the heuristic is all there is. Files
    already present are seeded at their current size either way, so a
    `--continue` run never replays yesterday's banner.
    """

    def __init__(self, directory=None, poll=2.0, now=None, echo=None, agent=None,
                registry=None, log=None):
        # `directory` stays first and positional: it is how the claude side has
        # always been built, and naming a directory is still the whole of what
        # that side needs.
        self.agent = agent or ClaudeAgent(directory)
        self.dir = getattr(self.agent, "dir", directory)
        self.poll = poll
        self.echo = echo
        self.registry = registry
        self.log = log or (lambda *_: None)
        self.offsets = {}
        self.next_poll = 0.0
        self.seen_any = False
        self.grown = []          # files that gained bytes in the last poll
        self.current = None      # of those, the one this terminal's session writes
        self.bound_session_id = None   # set once the registry names one (T02)
        self.agent_status = None       # "busy"/"idle" from the registry, or None
        self.agent_status_at = 0.0     # when the registry says that last changed
        self.confirmed = False   # `confirm` has fired: no registry, but no longer a guess (T03)
        self.candidates = []     # kept, fresh, not-yet-ruled-out files (T03)
        self._demoted = set()    # candidates `confirm` has since ruled out
        self._seed(now or time.time())
        self.preexisting = set(self.offsets)

    def _seed(self, now):
        for p in self.agent.paths(now):
            try:
                self.offsets[p] = os.path.getsize(p)
            except OSError:
                self.offsets[p] = 0

    def expect(self, text):
        """Start watching for `text` as a verbatim echo of a submitted prompt.

        `self.echo` is read fresh on every `poll_now` (see `__init__`), so a
        mutation here takes effect on the very next poll with no other
        plumbing — the handoff phrase's nonce changes every attempt, and
        `_send_handoff` calls this each time it sends one.
        """
        if not text:
            return
        if isinstance(self.echo, str):
            self.echo = [self.echo]
        else:
            self.echo = list(self.echo) if self.echo else []
        if text not in self.echo:
            self.echo.append(text)

    def forget(self, text):
        """Stop watching for a string added by `expect` (or given at construction)."""
        if not text or not self.echo:
            return
        if isinstance(self.echo, str):
            if self.echo == text:
                self.echo = []
            return
        self.echo = [e for e in self.echo if e != text]

    def poll_now(self, now=None):
        now = now if now is not None else time.time()
        self.grown = []
        if now < self.next_poll:
            return []
        self.next_poll = now + self.poll
        if self.registry is not None:
            self._poll_registry(now)
        found = []
        # The file we are following stays on the list even if it drops off the
        # scan: codex's directories are named for the day, and a session that
        # runs long enough leaves its own behind.
        scan = set(self.agent.paths(now))
        if self.current:
            scan.add(self.current)
        kept = [p for p in sorted(scan) if self.agent.keep(p)]
        for p in kept:
            start = self.offsets.get(p)
            if start is None:
                start = 0                       # created after we started: read it all
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < start:                    # truncated/rotated
                start = 0
            if size > start:
                self.seen_any = True
                self.grown.append(p)
            offset, recs = self.agent.records(p, start, self.echo)
            self.offsets[p] = offset
            for rec in recs:
                rec["path"] = p
            found.extend(recs)
        # Every kept, not-yet-ruled-out file created since we started -- not
        # just the ones that happened to grow this tick, or a wrapper reading
        # a neighbour's climbing count in the gaps between its own turns would
        # never see the ambiguity at all (T03).
        self.candidates = [p for p in kept
                           if p not in self.preexisting and p not in self._demoted]
        if not self.bound_session_id:
            self._pick_current()
        return found

    def _poll_registry(self, now):
        """Identity, straight from `sessions/<pid>.json`: no growth to watch.

        `status`/`statusUpdatedAt` ride along as an extra "is anyone home"
        signal for the controller (T02 item 4) — never the only one, since a
        Claude Code that predates the field would otherwise look permanently
        idle.
        """
        rec, why = self.registry.lookup()
        if rec is None:
            self.agent_status = None
            return
        self.agent_status = rec.get("status")
        updated = rec.get("statusUpdatedAt")
        self.agent_status_at = updated / 1000.0 if isinstance(updated, (int, float)) else now
        session_id = rec.get("sessionId")
        if not session_id or session_id == self.bound_session_id:
            return
        self.bind(session_id, self.agent.path_for(session_id), why)

    def bind(self, session_id, path, why):
        old = self.bound_session_id
        self.bound_session_id = session_id
        self.current = path
        self.offsets.setdefault(path, 0)
        if old is None:
            self.log("session bound: %s (%s)" % (session_id, why))
        else:
            self.log("session rebound: %s → %s (%s)" % (old, session_id, why))

    def confirm(self, path):
        """The one proof stronger than the growth heuristic: our own fold
        phrase's nonce, echoed into `path` (T06). No registry ever names codex
        a session, so without this a directory holding two live rollouts has
        no way to tell them apart beyond a guess that a neighbour's next burst
        can undo (T03). Once it fires, every other candidate seen so far is
        ruled out for good, and `_pick_current` can never hand `current` back
        to one of them just because it grew and ours, for a moment, did not.
        """
        self.confirmed = True
        self._demoted |= {p for p in self.offsets if p != path}
        self.candidates = [p for p in self.candidates if p not in self._demoted]
        self.current = path
        self.offsets.setdefault(path, 0)

    def _pick_current(self):
        """Which of the growing transcripts belongs to the session at this terminal.

        A project directory holds more than one: yesterday's sessions, and — when
        work is run under the same cwd from somewhere else — other live ones. For
        a limit that never mattered, since a limit is the account's and not a
        session's. For the context trigger it matters entirely: reading another
        session's usage as this one's is a restart at the wrong moment or none at
        all.

        Two rules, and no guessing beyond them. Stay with the file already being
        followed for as long as it keeps growing — that survives another session
        writing a burst in between. When it stops (which is what `/clear` looks
        like from here: the session moves to a new file), prefer one that did not
        exist when we started, because our claude created its transcript within a
        second of us and anything else in this directory is older.

        A `confirm`ed path is excluded from every other transcript's `_demoted`
        set, so once the fold phrase's echo has settled who is who, this never
        hands `current` back to a candidate that proof already ruled out.
        """
        if not self.grown or self.current in self.grown:
            return
        grown = [p for p in self.grown if p not in self._demoted]
        if not grown:
            return
        fresh = [p for p in grown if p not in self.preexisting]
        old = self.current
        self.current = max(fresh or grown, key=self._mtime)
        if old is not None:
            # A registry (T02/T03) makes this rule a fallback rather than the
            # switch point; that is exactly when a wrong guess is least
            # expected, so every guess it still makes stays visible in the log.
            self.log("transcript switched: %s → %s (fallback heuristic)"
                     % (old, self.current))

    @staticmethod
    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0


# --------------------------------------------------------------------------- #
# context accounting — the numbers Claude already wrote down
# --------------------------------------------------------------------------- #
# Nothing here counts tokens. Every assistant row in the transcript carries the
# API's own `usage`, and these three input counters ARE the prompt that was sent,
# which is what "how full is the context" means. Output tokens are deliberately
# left out: they are not context until the next turn quotes them back, and by
# then they are inside cache_creation.
_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# --------------------------------------------------------------------------- #
# model profiles — window, restart threshold, and where the agent compacts on
# its own (T18)
# --------------------------------------------------------------------------- #
# One row states three things this file used to keep in three different
# places: the window (used to live alone in CONTEXT_WINDOWS, claude only), the
# restart threshold (used to be one global percentage, DEFAULT_RESTART_PCT /
# DEFAULT_CODEX_RESTART_PCT), and the point the agent folds the context on its
# own (a comment, or codex's `codex_cap` read live from its log). `since` is
# when a row was last checked against the model's own docs — models move, and
# a stale row needs to be findable.
Profile = collections.namedtuple(
    "Profile", "window restart_at compact_at since note",
    defaults=(None, "2026-09-18", ""))

SMALL_WINDOW = 200000
BIG_WINDOW = 1000000

# claude: restart_at is the same 51% (DEFAULT_RESTART_PCT) this project has
# always used, now baked in per model rather than applied blind to whatever
# window a session happens to report. compact_at is Claude Code's own
# auto-compact point: "~967K" for the 1M family per its docs; nothing that
# precise is published for the 200k family, so ~0.95 of the window stands in.
_BIG_CLAUDE = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
              "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
_SMALL_CLAUDE = ("claude-opus-4-5", "claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5")

# codex: from ~/.codex/models_cache.json on codex 0.154, window is EFFECTIVE
# (context_window × effective_context_window_percent) = 258,400, not the raw
# 272k. restart_at is that window minus the reserve this project already tuned
# for it (CR_CODEX_RESERVE_TOKENS, 64k) — model_restart_at reclamps against the
# live value of that knob rather than trusting this frozen number. compact_at
# is the hard cap (== window): codex's softer 90%-of-raw-window compaction
# (244,800) only applies with CR_CODEX_HOLD_COMPACT=0, and the hard cap is what
# binds under this wrapper's default of holding that back.
_CODEX_5_6 = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")
_CODEX_NOTE = ("hard cap; the softer 244,800 (90% of the raw window) applies "
              "only with CR_CODEX_HOLD_COMPACT=0. max_context_window is "
              "872,000 for sol/terra/luna/astra — raising model_context_window "
              "in config.toml makes the rollout report a window this profile "
              "was not written for; model_restart_at then sits this out.")

MODEL_PROFILES = {
    "claude": dict(
        [(slug, Profile(BIG_WINDOW, int(BIG_WINDOW * DEFAULT_RESTART_PCT / 100), 967000))
         for slug in _BIG_CLAUDE] +
        [(slug, Profile(SMALL_WINDOW, int(SMALL_WINDOW * DEFAULT_RESTART_PCT / 100),
                        int(SMALL_WINDOW * 0.95)))
         for slug in _SMALL_CLAUDE]),
    "codex": {slug: Profile(258400, 194400, 258400, note=_CODEX_NOTE) for slug in _CODEX_5_6},
}

# Native context window per model slug, the way model_window() has always
# looked it up — sourced from MODEL_PROFILES now instead of kept separately.
# Guessing LOW is still the safe direction: a window assumed too small
# restarts a little early, one assumed too large never restarts at all, and
# "never" is the failure that is invisible until the session dies of a full
# context.
CONTEXT_WINDOWS = {slug: p.window for slug, p in MODEL_PROFILES["claude"].items()}

# codex keeps this much of every window out of its "Context N% used": the system
# prompt and tools a session carries before anyone has said anything. Its status
# line reads (tokens - baseline) / (window - baseline), so a codex threshold is
# read the same way — otherwise a restart set at 60% would fire while the line
# the user is watching still says 57%. Measured, not remembered: 13,711 tokens
# of a 258,400 window is "Context 1% used" on codex-cli 0.154, and 5% without
# the baseline.
#
# T22 ran against the same codex-cli version and did NOT re-measure this live —
# there was no way to drive a real codex session in that environment. Left as
# is on the strength of the 0.154 measurement above; someone with a live codex
# session on a newer release should re-check it rather than assume it still
# holds.
CODEX_BASELINE_TOKENS = 12000


# A phrase that names a command cannot travel between agents — claude and codex
# do not recognize the same ones. CR_CLAUDE_<X>/CR_CODEX_<X>, unset by default,
# override CR_<X> for one agent only; unset, the agent keeps the shared phrase.
AGENT_MESSAGE_KEYS = (("handoff_msg", "HANDOFF_MSG"),
                      ("clear_cmd", "CLEAR_CMD"),
                      ("resume_msg", "RESUME_MSG"),
                      ("cancel_msg", "CANCEL_MSG"))


# Which leading character opens something the composer itself resolves and
# accepts with Enter, per agent (T16). Live driving of both TUIs (T15,
# codex-cli 0.155.1; the claude side already shipped and tested) found only
# one such character: a leading "/" that names exactly one known command,
# shown as a description hint the instant the whole token is typed. "$name"
# (codex's own "invoke a skill" convention — a system-prompt instruction to
# the model, not a composer feature) and "@file" (both agents' file-mention
# convention) are delivered as plain text with no popup at all, as long as
# the whole phrase arrives in one write() the way schedule_injection sends
# it — a real human typing character-by-character might trigger a live
# autocomplete list, but the wrapper never does that. Kept as a per-agent
# table, not a bare constant, so a future release that reintroduces popup
# handling for another prefix has somewhere to go without touching
# schedule_injection again.
AGENT_INPUT = {
    "claude": dict(popup_prefixes="/"),
    "codex": dict(popup_prefixes="/"),
}


def typing_plan(agent, text, cfg):
    """The writes schedule_injection turns into pty input, as (delay, bytes)
    pairs measured from the moment `text` itself is typed (delay 0).

    A pure function so the T15 recipes are unit-testable without a running
    Controller or pty: given (agent, text, cfg), the sequence is fixed.
    Unrecognized "/word" is not handled here and cannot be from here — codex
    rejects it inline and never sends it (T15), so a caller must never pass a
    bare "/name" that is not one of the agent's own commands (see
    agent_cfg's {skill:NAME} expansion, T17).
    """
    prefixes = AGENT_INPUT.get(agent, AGENT_INPUT["claude"])["popup_prefixes"]
    plan = [(0.0, text.encode())]
    if text and text[0] in prefixes:
        t = cfg["slash_gap"]
        for _ in range(max(1, cfg["slash_enter"])):
            plan.append((t, b"\r"))
            t += cfg["slash_enter_gap"]
    else:
        plan.append((0.6, b"\r"))
    return plan


SKILL_PLACEHOLDER = re.compile(r"\{skill:([^{}]+)\}")


def expand_skill(text, agent):
    """{skill:NAME} -> the syntax this agent actually sends: "/NAME" for
    claude, "$NAME" for codex (T17). Never the other way around: codex
    silently drops an unrecognized "/NAME" — never sends it as text at all —
    while "$NAME" is always delivered as plain text (T15). So one shared
    CR_HANDOFF_MSG/CR_RESUME_MSG/CR_CANCEL_MSG with {skill:...} in it now
    unfolds on both agents instead of quietly failing on whichever one got
    the wrong prefix.
    """
    prefix = "$" if agent == "codex" else "/"
    return SKILL_PLACEHOLDER.sub(lambda m: prefix + m.group(1), text)


def agent_cfg(cfg, agent):
    """The config a controller for this agent runs with.

    codex has a threshold of its own: an absolute count does not travel
    between windows — claude's 500k is past the end of a 258k codex window —
    so codex only ever sees its own CR_CODEX_CONTEXT_TOKENS, never claude's.
    """
    cfg = dict(cfg, agent=agent)
    for key, env_suffix in AGENT_MESSAGE_KEYS:
        override = os.environ.get("CR_%s_%s" % (agent.upper(), env_suffix))
        if override:
            cfg[key] = override
    for key in ("handoff_msg", "resume_msg", "cancel_msg"):
        if cfg.get(key):
            cfg[key] = expand_skill(cfg[key], agent)
    if agent != "codex":
        return cfg
    cfg["context_tokens"] = int(cfg.get("codex_context_tokens") or 0)
    return cfg


# What moves codex's own compaction threshold out of the restart's way. The scope
# change is what lets the limit be raised at all: under the default "total" scope
# a configured limit is clamped to 90% of the window. What remains is the hard cap
# at the usable window, which no setting moves, and which the log reports.
CODEX_HOLD_ARGS = ["-c", 'model_auto_compact_token_limit_scope="body_after_prefix"',
                   "-c", "model_auto_compact_token_limit=1000000000"]


def codex_launch_args(cfg):
    """Arguments put in front of the user's own when the wrapper starts codex."""
    if cfg.get("agent") != "codex" or not cfg.get("codex_hold_compact", True):
        return []
    if cfg.get("context_tokens", 0) <= 0 and not cfg.get("context_restart_on"):
        return []                    # nothing is racing codex, so leave it be
    return list(CODEX_HOLD_ARGS)


def claude_launch_args(cfg, agent, argv, pid=None):
    """`--settings` that route claude's own statusline through our proxy (T20).

    Claude-only (codex has no statusline of its own, hence no need of this),
    and only once something is actually racing the context: without a restart
    armed there is nothing for the window signal to inform, same reasoning as
    `codex_launch_args` above. Skipped outright when the user's own argv
    already names `--settings` -- an explicit flag on the command line always
    wins, exactly as if this feature did not exist, rather than fight it for
    the same key.

    Whether Claude Code MERGES a `--settings` value with the user's real
    settings.json or REPLACES it outright is not something this project has
    verified live (no real Claude Code session was available while building
    this -- see docs/backlog/T20-claude-effective-window.md's "Verified"
    section). MERGE is the assumption this is coded against, since a proxy
    that silently replaced someone's other settings would be a worse failure
    than this feature just not helping.
    """
    if agent != "claude" or not cfg.get("statusline_proxy", True):
        return []
    if cfg.get("context_tokens", 0) <= 0 and not cfg.get("context_restart_on"):
        return []                    # nothing needs the window signal yet
    if any(a == "--settings" or a.startswith("--settings=") for a in (argv or [])):
        return []                    # the user's own flag always wins
    if is_attach_launch(argv):
        # Claude's `attach` command must be its first token.  A global
        # `--settings` before it makes recent Claude Code builds treat `attach`
        # and its session id as a prompt instead, so retaining the command's
        # semantics is more important than observing its status line.
        return []
    raw_self = os.environ.get("CR_SELF") or ""
    self_path = os.path.realpath(raw_self) if raw_self else "agent-retrier"
    status_path = os.path.join(cfg["status_dir"],
                               "%d.json" % (pid if pid is not None else os.getpid()))
    command = "%s --cr-statusline %s" % (shlex.quote(self_path), shlex.quote(status_path))
    settings = json.dumps({"statusLine": {"type": "command", "command": command}})
    return ["--settings", settings]


def usage_tokens(usage):
    """The context a turn was sent with, or None when the row does not say."""
    if not isinstance(usage, dict):
        return None
    nums = [usage.get(k) for k in _USAGE_KEYS]
    nums = [n for n in nums if isinstance(n, (int, float)) and not isinstance(n, bool)]
    if not nums:
        return None
    total = int(sum(nums))
    return total if total else None


def model_slug(model):
    """A model name in the form the table is keyed by, or "" for no name at all."""
    slug = (model or "").strip().lower()
    slug = re.sub(r"\[[^\]]*\]$", "", slug)      # "claude-opus-5[1m]"
    return re.sub(r"-\d{8}$", "", slug)          # "claude-haiku-4-5-20251001"


def _display_to_claude_slug(display):
    """"Opus 5" -> "claude-opus-5", "Haiku 4.5" -> "claude-haiku-4-5" (T21).

    What claude's own `/model` command names a model by, in the shape every
    entry in `_BIG_CLAUDE`/`_SMALL_CLAUDE` already takes once its dots and
    spaces become hyphens — so this stands in for a display-name table rather
    than one more list to keep in sync with MODEL_PROFILES by hand. Read only
    off `<local-command-stdout>Set model to <display></local-command-stdout>`,
    an early hint the next real assistant row's own slug always overrides.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", display.strip().lower()).strip("-")
    return "claude-%s" % slug if slug else ""


def model_env_slug(slug):
    """A model slug in the shape an environment variable name can hold.

    "gpt-5.6-sol" -> "GPT_5_6_SOL", "claude-opus-5" -> "CLAUDE_OPUS_5". Used to
    build CR_CLAUDE_TOKENS_<X>/CR_CODEX_TOKENS_<X> — a per-model override in
    absolute tokens, because once the model's own window is known a number is
    unambiguous where a second percentage would only be one more conversion.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").upper()


def model_window(model):
    """Native window for a model slug, or None when nothing in the table fits.

    A point release is its family's window until something says otherwise.
    `claude-fable-5-1` was never in the table — `claude-fable-5` is — and reading
    the first as unknown is what put a 1M session on a 200k denominator and folded
    it at 12% full. So the trailing version segments are dropped one at a time,
    and never past `claude-<family>-<major>`: far enough to catch a point release,
    not far enough to hand `claude-opus-4-9` the window of some other opus.
    """
    parts = model_slug(model).split("-")
    while len(parts) >= 3:
        win = CONTEXT_WINDOWS.get("-".join(parts))
        if win:
            return win
        parts.pop()
    return None


_FAMILY_MAJOR = re.compile(r"^claude-([a-z]+)-(\d+)")


def _claude_family_estimate(slug):
    """(window, "same family"|"modal window") for a claude-shaped slug with no
    exact or point-release match in CONTEXT_WINDOWS, or None when the slug is
    not even shaped like a claude model (T19).

    `model_window` already refuses to guess past a family/major it has never
    seen — right for a KNOWN model, since a wrong number there folds a session
    that is nowhere near full. But a slug this whole build has never heard of
    at all is a different question: it is almost always a NEW model, i.e. a
    large one, and disarming the trigger over it is its own failure (the log
    that started this: `claude-fable-5-1` read as unfamiliar and assumed 200k
    folded a 1M session at 12% full — except here the fix is a guess in the
    OPPOSITE direction, large rather than small). A member of a family the
    table already has (`claude-opus-6` when `claude-opus-5` is in it) gets that
    family's newest window — a new major version is not presumed smaller than
    the last one. A brand new family name has nothing family-specific to go
    on, so it gets the modal window of the table's own newest generation
    instead ("large is what ships now").
    """
    m = _FAMILY_MAJOR.match(slug)
    if not m:
        return None
    family = m.group(1)
    by_major = {}           # major version -> [window, window, ...], every family
    family_majors = {}      # major version -> [window, ...], this family only
    for known, window in CONTEXT_WINDOWS.items():
        km = _FAMILY_MAJOR.match(known)
        if not km:
            continue
        major = int(km.group(2))
        by_major.setdefault(major, []).append(window)
        if km.group(1) == family:
            family_majors.setdefault(major, []).append(window)
    if family_majors:
        newest = family_majors[max(family_majors)]
        return collections.Counter(newest).most_common(1)[0][0], "same family"
    if not by_major:
        return None
    newest = by_major[max(by_major)]
    return collections.Counter(newest).most_common(1)[0][0], "modal window"


def _codex_model_cache():
    """{slug: effective window} out of codex's own `$CODEX_HOME/models_cache.json`,
    or {} when it is missing or unreadable (T19).

    codex writes this file itself for every model it has ever been pointed at
    on this machine — the one source that can genuinely know a model newer
    than this build's own table, without asking the network at all.
    `context_window` is the raw figure; `effective_context_window_percent` (a
    whole-number percentage, e.g. 95) narrows it to the effective one, the same
    computation already documented above `MODEL_PROFILES`.
    """
    try:
        with open(os.path.join(codex_home(), "models_cache.json")) as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for slug, row in data.items():
        if not isinstance(row, dict):
            continue
        window = row.get("context_window")
        if not isinstance(window, (int, float)) or window <= 0:
            continue
        pct = row.get("effective_context_window_percent")
        if isinstance(pct, (int, float)) and pct > 0:
            window *= pct / 100.0 if pct > 1 else pct
        out[model_slug(str(slug))] = int(window)
    return out


def estimate_window(agent, model):
    """(window, source) for a model neither the profile table, the cache, nor a
    live lookup has ever sized (T19) — the fallback that replaces disarming the
    trigger outright. A new model ships exactly when this build's table is
    stale, and a session left with nothing armed dies of its own context
    instead of being folded a little early on a guess; self-correction (the
    upward bump in `Controller.on_context`, the downward one off the agent's
    own compaction) is what takes the guess back out of it once the session
    itself says whether it was right.

    `SMALL_WINDOW` is the last resort, used only when nothing above has ANY
    opinion at all — explicitly provisional, not a considered guess the way
    the family/cache stages are.
    """
    slug = model_slug(model)
    if agent == "codex":
        cache = _codex_model_cache()
        if slug in cache:
            return cache[slug], "the codex model cache"
        if cache:
            return (collections.Counter(cache.values()).most_common(1)[0][0],
                    "the codex cache's modal window")
        return SMALL_WINDOW, "nothing published, no codex model cache either"
    got = _claude_family_estimate(slug)
    if got:
        return got
    return SMALL_WINDOW, "nothing published"


def model_restart_at(agent, slug, window):
    """Stage 3 of the threshold order `_recompute_limit` walks, top to bottom:

      1. CR_<AGENT>_TOKENS_<SLUG>            (`_model_tokens_override`)
      2. CR_CONTEXT_TOKENS / CR_CODEX_CONTEXT_TOKENS
      3. this: MODEL_PROFILES[agent][slug].restart_at, armed by CR_CONTEXT_RESTART=1
      4. `default_restart_at`: the same rule applied to whatever window the
         session actually has, for a model this row does not describe
      5. nothing — no window at all (codex before its first turn) stays
         disarmed rather than guessing

    Stages 1-2, and whether CR_CONTEXT_RESTART=1 armed stage 3 at all, are the
    caller's job (`cfg["context_restart_on"]`, already agent-resolved by
    `agent_cfg`) — this function is only ever reached once all of that is
    settled, so it does not re-check the flag itself.

    Only fires when `window` is the EXACT window the profile was written
    against: a model whose window has been raised past what the profile
    assumes (`model_context_window` in codex's config.toml, an explicit
    CR_CONTEXT_WINDOW) is a model this row no longer describes — returning
    `None` sends the caller on to `default_restart_at` instead of guessing here.

    Whatever this returns is reclamped under `compact_at - reserve` using the
    LIVE value of CR_CODEX_RESERVE_TOKENS, not the frozen number baked into the
    profile — that knob is one the profile has no way to see.
    """
    prof = _matching_profile(agent, slug, window)
    if not prof:
        return None
    candidate = prof.restart_at
    if prof.compact_at:
        candidate = min(candidate, prof.compact_at - _compaction_reserve(agent))
    return max(0, int(candidate))


def default_restart_at(agent, window):
    """Stage 4: the threshold for a window no profile row describes — a model
    this build has never heard of, or one whose window has been moved past
    what its row was written for (`model_context_window` in codex's
    config.toml, an explicit CR_CONTEXT_WINDOW).

    Same rule the rows themselves are built from, applied to the window this
    session actually has: DEFAULT_RESTART_PCT of it for claude, and for codex
    the usable window minus the reserve the fold needs (CR_CODEX_RESERVE_TOKENS,
    read live) — codex restarts against its own hard cap, not a fraction.

    A new model ships exactly when this build's table is stale, and a session
    with nothing armed dies of its own context; the estimate machinery (T19)
    already guarantees `window` is never None here.
    """
    if window is None or window <= 0:
        return None
    if agent == "codex":
        return max(0, int(window) - _compaction_reserve(agent))
    return max(0, int(window * DEFAULT_RESTART_PCT / 100))


def _matching_profile(agent, slug, window):
    """The profile row for this exact window, or None.

    Shared by `model_restart_at` and the post-restart headroom guard (T13) --
    both refuse a model whose window has moved past what the row was written
    for rather than apply a profile that no longer describes it.
    """
    prof = MODEL_PROFILES.get(agent, {}).get(slug)
    return prof if prof and prof.window == window else None


def _compaction_reserve(agent):
    """Live headroom this project holds back under a model's own compaction
    point -- CR_CODEX_RESERVE_TOKENS for codex, the only agent with a knob of
    its own; nothing for claude. Read fresh rather than trusting a number
    frozen into the profile, which has no way to see this env var.

    CR_CODEX_RESERVE_ADAPT=1 (T22) can raise this further: once
    CR_CODEX_FOLDS_FILE shows a fold that cost more than this number, 1.25x
    the largest one ever recorded there is used instead, so a session that
    starts after the file already knows this benefits immediately rather than
    waiting for one more fold of its own."""
    if agent != "codex":
        return 0
    base = parse_tokens(os.environ.get("CR_CODEX_RESERVE_TOKENS") or "64k") or 0
    if _env("CR_CODEX_RESERVE_ADAPT", "0") != "1":
        return base
    path = os.path.expanduser(_env("CR_CODEX_FOLDS_FILE", "~/.agent-retrier/folds.json"))
    return max(base, int(1.25 * _fold_history_max(path)))


def _fold_history_max(path):
    """The largest codex fold cost ever recorded at `path` (T22), or 0.

    A missing or corrupt file is simply empty -- the same tolerance
    `WindowLookup._read_cache` gives the model-window cache, for the same
    reason: this is a courtesy to future sessions, never a source of truth
    worth failing over."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    costs = [r.get("cost") for r in data if isinstance(r, dict)]
    costs = [c for c in costs if isinstance(c, (int, float)) and not isinstance(c, bool)]
    return int(max(costs)) if costs else 0


def _append_fold_record(path, agent, cwd, cost, now):
    """`{agent, cwd, cost, date}` appended to the folds.json array at `path`
    (T22): read, append, atomic rename -- the same shape `WindowLookup._store`
    already uses for the model-window cache."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    data.append(dict(agent=agent, cwd=cwd, cost=int(cost),
                     date=datetime.fromtimestamp(now).strftime("%Y-%m-%d")))
    tmp = "%s.%d" % (path, os.getpid())
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.rename(tmp, path)                  # atomic: two sessions can race
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def human_tokens(n):
    if n is None:
        return "?"
    if n >= 1000000:
        return "%.1fM" % (n / 1e6)
    if n >= 1000:
        return "%dk" % (n // 1000)
    return str(int(n))


def _cr_models_rows():
    """(agent, slug, window, restart_at, compact_at, source) for every entry in
    MODEL_PROFILES, resolved against THIS process's environment — the same
    order `_recompute_limit` runs, for a session that would be running that
    model at its profile's own window, without ever opening one. `--cr-models`
    is the only caller; it exists so a person can check what the wrapper
    thinks about their model without starting a session.
    """
    rows = []
    for agent in ("claude", "codex"):
        cfg = agent_cfg(CFG, agent)
        for slug in sorted(MODEL_PROFILES.get(agent, {})):
            prof = MODEL_PROFILES[agent][slug]
            window = prof.window
            over_name = "CR_%s_TOKENS_%s" % ("CODEX" if agent == "codex" else "CLAUDE",
                                             model_env_slug(slug))
            over = parse_tokens(os.environ.get(over_name))
            if over:
                restart_at, source = over, over_name
            elif cfg["context_tokens"] > 0:
                restart_at = int(cfg["context_tokens"])
                source = "CR_CODEX_CONTEXT_TOKENS" if agent == "codex" else "CR_CONTEXT_TOKENS"
            elif cfg.get("context_restart_on"):
                restart_at = model_restart_at(agent, slug, window)
                if restart_at is not None:
                    source = "CR_CONTEXT_RESTART"
                else:
                    restart_at = default_restart_at(agent, window)
                    source = "estimated" if restart_at is not None else "disarmed"
            else:
                restart_at, source = None, "off"
            rows.append((agent, slug, window, restart_at, prof.compact_at, source))
    return rows


def print_models_table(out=None):
    out = out or sys.stdout
    headers = ("agent", "model", "window", "restart_at", "compact_at", "source")
    lines = [headers]
    for agent, slug, window, restart_at, compact_at, source in _cr_models_rows():
        lines.append((agent, slug, human_tokens(window), human_tokens(restart_at),
                      human_tokens(compact_at), source))
    widths = [max(len(row[i]) for row in lines) for i in range(len(headers))]
    for i, row in enumerate(lines):
        out.write("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() + "\n")
        if i == 0:
            out.write("  ".join("-" * w for w in widths) + "\n")
    return 0


# --------------------------------------------------------------------------- #
# asking what a window is, when the table has never heard of the model
# --------------------------------------------------------------------------- #
# A table shipped in a file is a table that goes stale, and the day it does is
# the day a new model comes out — which is the day the wrapper is most likely to
# be pointed at one. Both published tables answer the question outright, so a
# slug this build does not know is looked up rather than guessed at:
#
#   the Models API   GET /v1/models/<slug> -> `max_input_tokens`. Exact, and
#                    needs an ANTHROPIC_API_KEY; a Claude subscription is not
#                    one, so most sessions never take this path.
#   the models docs  the published table, one column per model, with a row
#                    naming the API id and a row naming the context window.
#                    No credentials, which is why it is the fallback that
#                    actually runs.
#
# It happens in a worker thread and arrives as a finished answer or not at all:
# the pty loop cannot block on the network, and no session may fail to start
# because a documentation site is down. The answer is cached on disk, so a slug
# is asked about once a week rather than once a session.
MAX_FETCH = 1 << 20            # the docs page is ~20KB; this is a sanity bound
# urllib's default agent string ("Python-urllib/3.x") is refused outright by the
# CDN in front of the docs — a 403, measured. Saying who is actually asking is
# both what gets through and the honest thing to send.
USER_AGENT = "agent-retrier (+https://github.com/a0s/agent-retrier)"
# The slug is read out of a JSON file and then pasted into a URL, so it is held
# to what a model name can actually look like. A name with a slash or a query in
# it is not a model this wrapper has anything to ask about.
SANE_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DOC_ID_ROWS = ("claude api id", "claude api alias")
DOC_WINDOW_ROW = "context window"


def _doc_cell(cell):
    """One markdown cell, with the markup that is not the value taken off."""
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell.strip())   # [text](url)
    return cell.replace("`", "").replace("*", "").strip()


def parse_models_doc(text):
    """{slug: window} out of the published models table.

    The table is one column per model and one row per property, so the rows worth
    reading are the ones naming the API id and the context window, matched to each
    other column by column. The id row and the alias row both count: they differ
    exactly where a model is published under a dated id, and the transcript can
    write either.
    """
    ids, windows = {}, {}
    for line in text.split("\n"):
        if not line.lstrip().startswith("|"):
            continue
        cells = [_doc_cell(c) for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        head = cells[0].lower()
        if head in DOC_ID_ROWS:
            for i, cell in enumerate(cells[1:], 1):
                slug = model_slug(cell)
                if slug.startswith("claude-"):
                    ids.setdefault(i, []).append(slug)
        elif head == DOC_WINDOW_ROW:
            for i, cell in enumerate(cells[1:], 1):
                win = parse_tokens(cell.lower().replace("tokens", "").strip())
                if win:
                    windows[i] = win
    out = {}
    for col, slugs in ids.items():
        win = windows.get(col)
        if win:
            for slug in slugs:
                out[slug] = win
    return out


def parse_models_api(payload):
    """The window out of one Models API object, or None if it does not say."""
    try:
        data = json.loads(payload)
    except Exception:
        return None
    win = data.get("max_input_tokens") if isinstance(data, dict) else None
    return int(win) if isinstance(win, (int, float)) and not isinstance(win, bool) \
        and win > 0 else None


def fetch_url(url, timeout, headers=None):
    """The body of a GET, as text. Raises on anything that is not a clean 200."""
    head = {"user-agent": USER_AGENT}
    head.update(headers or {})
    req = urllib.request.Request(url, headers=head)
    fh = urllib.request.urlopen(req, timeout=timeout)
    try:
        return fh.read(MAX_FETCH).decode("utf-8", "replace")
    finally:
        fh.close()


class WindowLookup:
    """Learns the context window of a model the shipped table does not know.

    `want()` is called from the pty loop and returns immediately; `take()` hands
    back whatever has finished since it was last called. Nothing in here can
    raise into that loop and nothing in it waits on the network.
    """

    def __init__(self, cfg, log=None, fetch=None, clock=time.time):
        self.cfg = cfg
        self.log = log or (lambda *_: None)
        self.fetch = fetch or fetch_url
        self.clock = clock
        self.asked = set()          # slugs already looked up this session
        self.done = []              # finished answers waiting to be taken
        self.lock = threading.Lock()

    # -- the pty loop's two calls ------------------------------------------- #
    def want(self, model):
        """Learn this model's window, if it is not already being learned."""
        slug = model_slug(model)
        if not slug or slug in self.asked or not SANE_SLUG.match(slug):
            return False
        self.asked.add(slug)
        cached = self._cached(slug)
        if cached:
            window, source = cached
            self._finish(slug, window, "%s, cached" % source)
            return True
        if not self.cfg.get("model_lookup"):
            self.log("%s is not in this build's table and CR_MODEL_LOOKUP is off; "
                     "set CR_CONTEXT_WINDOW to give the percentage trigger a "
                     "denominator" % slug)
            return False
        threading.Thread(target=self._work, args=(slug,), daemon=True).start()
        return True

    def take(self):
        """[(slug, window, source)] for the lookups that have come back."""
        with self.lock:
            out, self.done = self.done, []
        return out

    # -- the worker --------------------------------------------------------- #
    def _work(self, slug):
        window, source = None, None
        # Each source is tried on its own: an API key that is expired, or set for
        # a different account, or simply does not know this model, must not take
        # the credential-free source down with it.
        for ask in (self._ask_api, self._ask_docs):
            try:
                window, source = ask(slug)
            except Exception as exc:              # a thread that raises is a crash
                self.log("asking %s about %s failed: %s"
                         % (ask.__name__[5:], slug, exc))
                continue
            if window:
                break
        if not window:
            self.log("nothing published says how large %s's context window is; "
                     "the percentage trigger stays disarmed — set CR_CONTEXT_WINDOW "
                     "or CR_CONTEXT_TOKENS" % slug)
            return
        self._store(slug, window, source)
        self.log("%s: a %s context window per %s — this build's table does not know "
                 "that slug, so the figure came off the network; update "
                 "agent-retrier and it will not have to ask again"
                 % (slug, human_tokens(window), source))
        self._finish(slug, window, source)

    def _ask_api(self, slug):
        key = os.environ.get("ANTHROPIC_API_KEY")
        url = self.cfg.get("models_api_url")
        if not key or not url:
            return None, None
        text = self.fetch("%s/%s" % (url.rstrip("/"), slug),
                          self.cfg["model_lookup_timeout"],
                          {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return parse_models_api(text), "the models API"

    def _ask_docs(self, slug):
        url = self.cfg.get("models_doc_url")
        if not url:
            return None, None
        table = parse_models_doc(self.fetch(url, self.cfg["model_lookup_timeout"]))
        # Every model on that page is cached, not just the one asked about: the
        # page was fetched either way, and the next unfamiliar slug is likely to
        # be one of the others on it.
        for other, win in table.items():
            if other != slug:
                self._store(other, win, "the models docs")
        return table.get(slug), "the models docs"

    def _finish(self, slug, window, source):
        with self.lock:
            self.done.append((slug, window, source))

    # -- the cache ---------------------------------------------------------- #
    # One file, shared by every session on the machine, holding only what is
    # public anyway: a slug and a number. It is a courtesy to the network, never
    # a source of truth — an entry older than the TTL is simply not there.
    def _read_cache(self):
        try:
            with open(self.cfg["model_cache"], "r") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _cached(self, slug):
        row = self._read_cache().get(slug)
        if not isinstance(row, dict):
            return None
        window, at = row.get("window"), row.get("at")
        if not isinstance(window, int) or not isinstance(at, (int, float)):
            return None
        if self.clock() - at > self.cfg["model_cache_ttl"]:
            return None
        return window, str(row.get("source") or "the models docs")

    def _store(self, slug, window, source):
        path = self.cfg["model_cache"]
        data = self._read_cache()
        data[slug] = dict(window=int(window), source=source, at=int(self.clock()))
        tmp = "%s.%d" % (path, os.getpid())
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.rename(tmp, path)                  # atomic: two sessions can race
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# "there is a newer one of these"
# --------------------------------------------------------------------------- #
# Both agents this wrapper wraps say so on startup, and a wrapper that is a
# single file people copy around needs it more than either of them: there is no
# package manager in the loop unless the user chose one, and nothing else to
# notice that a release happened.
#
# The rule that makes it safe is that a session NEVER waits on the network for
# this. What is printed comes out of a file written by the previous run; the
# fetch that refreshes that file happens in a worker thread, after claude is
# already up, and its answer is for next time. A first run says nothing, which
# is the correct thing for it to say.
def parse_version(text):
    """"v1.10.0" -> (1, 10, 0). None for anything that is not a release tag."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


class UpdateCheck:
    """What the last check found, and the exact command that acts on it."""

    def __init__(self, cfg, log=None, fetch=None, clock=time.time):
        self.cfg = cfg
        self.log = log or (lambda *_: None)
        self.fetch = fetch or fetch_url
        self.clock = clock

    # -- what the user sees ------------------------------------------------- #
    def notice(self, current, self_path=None):
        """The two lines to print before handing the terminal over, or None."""
        if not self.cfg.get("update_check"):
            return None
        have, latest = parse_version(current), parse_version(self._cached())
        if not have or not latest or latest <= have:
            return None
        return ("agent-retrier %s \u2192 %s is out"
                % (".".join(str(n) for n in have), ".".join(str(n) for n in latest)),
                self.upgrade_command(self_path))

    def upgrade_command(self, self_path=None):
        """How THIS copy is updated — which depends on how it was installed.

        Printing `brew upgrade` at someone running a git clone would be wrong in
        the one way that matters: they would run it and nothing would change.
        """
        real = os.path.realpath(self_path or os.environ.get("CR_SELF") or "")
        if "/Cellar/" in real:
            return "brew upgrade %s" % self.cfg["update_formula"]
        here = os.path.dirname(real)
        if here and os.path.isdir(os.path.join(here, ".git")):
            return "git -C %s pull" % here
        return "https://github.com/%s/releases/latest" % self.cfg["update_repo"]

    # -- the check, which is never on the way to anything -------------------- #
    def refresh(self, background=True):
        """Bring the cache up to date, for the NEXT run to read."""
        if not self.cfg.get("update_check") or not self._stale():
            return False
        if not background:
            self._work()
            return True
        threading.Thread(target=self._work, daemon=True).start()
        return True

    def _work(self):
        url = self.cfg["update_url"] or (
            "https://api.github.com/repos/%s/releases/latest" % self.cfg["update_repo"])
        try:
            payload = json.loads(self.fetch(url, self.cfg["update_timeout"],
                                            {"accept": "application/vnd.github+json"}))
            tag = payload.get("tag_name") if isinstance(payload, dict) else None
        except Exception as exc:
            self.log("checking for a newer release failed: %s" % exc)
            self._store(None)
            return
        if not parse_version(tag):
            self.log("the release feed did not name a version")
            self._store(None)
            return
        self._store(tag)

    # -- the cache ---------------------------------------------------------- #
    # Also the clock for the check itself: an entry younger than the TTL means
    # the question was asked recently enough, whatever its answer was.
    def _read(self):
        try:
            with open(self.cfg["update_cache"], "r") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _cached(self):
        return str(self._read().get("version") or "")

    def _stale(self):
        at = self._read().get("at")
        if not isinstance(at, (int, float)):
            return True
        return self.clock() - at > self.cfg["update_ttl"]

    def _store(self, tag):
        """Record that the question was asked, and — when there is one — the answer.

        A check that failed still stamps the time. Otherwise a machine that is
        offline, or behind a proxy that eats api.github.com, asks again on every
        single launch, forever, and the one thing this feature must not become is
        a tax on starting a session.
        """
        path = self.cfg["update_cache"]
        row = self._read()
        row["at"] = int(self.clock())
        if tag:
            row["version"] = tag
        tmp = "%s.%d" % (path, os.getpid())
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(row, fh)
            os.rename(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_handoff(path, tail_bytes=512):
    """dict(size, mtime, tail) for the handoff file, or None when there isn't one.

    Only the tail is read. The file is the session's entire working state and can
    be long; the one thing wanted from it is whether its last line is the nonce
    that was asked for — the proof the write ran to the end instead of stopping
    somewhere in the middle.
    """
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            if st.st_size > tail_bytes:
                fh.seek(st.st_size - tail_bytes)
            tail = fh.read(tail_bytes)
    except OSError:
        return None
    return dict(size=st.st_size, mtime=st.st_mtime,
                tail=tail.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# controller — all decision-making, no I/O, so tests can drive it directly
# --------------------------------------------------------------------------- #
IDLE, WAITING, VERIFY, DONE = "idle", "waiting", "verify", "done"

# The context restart runs alongside those rather than inside them. A limit that
# lands in the middle of one does not cancel it — it suspends it — so the two
# machines each have to be able to hold a position of their own.
HANDOFF_SENT, HANDOFF_OK, CLEAR_SENT, CLEARED, RESUME_SENT, UNFOLD_FAILED = (
    "handoff_sent", "handoff_ok", "clear_sent", "cleared", "resume_sent", "unfold_failed")
# A short-lived state of its own (T09): an abort that comes after the fold
# already reached the session cannot just walk away like an ordinary abort --
# it has one more thing to say before the restart is actually over.
CANCEL_PENDING = "cancel_pending"

RESTART_LABELS = {HANDOFF_SENT: "folding", HANDOFF_OK: "folded",
                  CLEAR_SENT: "clearing", CLEARED: "cleared", RESUME_SENT: "unfolding",
                  UNFOLD_FAILED: "unfold failed", CANCEL_PENDING: "cancelling"}

# Defaults for everything the context restart and the stall handling read, so a
# Controller built from a config dict written before either existed (the tests
# build several) still has them, and for the restart disabled is what it gets.
CONTEXT_DEFAULTS = dict(
    stall_wait=60.0, stall_backoff=2.0, stall_max_wait=600.0, stall_max_attempts=8,
    context_tokens=0, context_window="auto",
    context_env_max=0, context_no_1m=False,
    handoff_file=".agent-retrier/handoff.md", handoff_marker="HANDOFF",
    handoff_min_bytes=200, handoff_attempts=2, resume_attempts=5, handoff_msg="",
    clear_cmd="/clear", resume_msg="Read `{file}` and continue from it.",
    cancel_msg="The context restart was cancelled — the handoff is not needed "
               "now. Continue with what you were doing before it was requested.",
    root_idle=20.0, handoff_timeout=900.0, step_gap=3.0, clear_settle=5.0,
    context_cooldown=600.0, context_max_cycles=0,
    context_min_headroom=80000, context_max_per_hour=3, notify_repeat=300.0,
    model_lookup=False, model_lookup_timeout=10.0,
    model_cache=os.path.expanduser("~/.agent-retrier/windows.json"),
    model_cache_ttl=604800.0, models_doc_url="", models_api_url="",
)


class HandoffRegistry:
    """Where agent-retrier's own wrapper instances announce their handoff path.

    Two sessions in the same project dir defaulting to the same
    `CR_HANDOFF_FILE` would otherwise overwrite each other's fold (T05); this
    is how the second one notices and moves aside, at
    `CR_HANDOFF_REGISTRY_DIR/<pid>.json`.
    """

    def __init__(self, directory=None):
        self.dir = directory or os.path.expanduser("~/.agent-retrier/sessions")

    def _entries(self):
        """`(pid, record)` for every live registration, pruning dead ones."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                pid = int(name[:-5])
            except ValueError:
                continue
            path = os.path.join(self.dir, name)
            if not self._alive(pid):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            try:
                with open(path) as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            yield pid, rec

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True     # e.g. EPERM: it exists, just is not ours to signal
        return True

    def find_conflict(self, handoff_path, exclude_pid):
        """The live pid already claiming `handoff_path`, if any."""
        for pid, rec in self._entries():
            if pid != exclude_pid and rec.get("handoff_path") == handoff_path:
                return pid
        return None

    def register(self, pid, cwd, handoff_path, agent, started):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = os.path.join(self.dir, ".%d.json.tmp" % pid)
            with open(tmp, "w") as fh:
                json.dump(dict(cwd=cwd, handoff_path=handoff_path,
                               agent=agent, started=started), fh)
            os.rename(tmp, os.path.join(self.dir, "%d.json" % pid))
        except OSError:
            pass

    def unregister(self, pid):
        try:
            os.remove(os.path.join(self.dir, "%d.json" % pid))
        except OSError:
            pass

    def claim(self, handoff_file, session_id, pid, cwd, agent, started, log):
        """The path this session should use, registering it for the next one.

        A path carrying `{id}` is unique by construction and skips the
        registry entirely. One without it is checked against every live
        session's claim; a collision gets `-<id>` appended, once, and logged.
        """
        if "{id}" in handoff_file:
            resolved = handoff_file.replace("{id}", session_id)
        else:
            resolved = handoff_file
            conflict = self.find_conflict(os.path.abspath(resolved), pid)
            if conflict is not None:
                stem, ext = os.path.splitext(resolved)
                resolved = "%s-%s%s" % (stem, session_id, ext)
                log("handoff file is taken by pid %d; using %s" % (conflict, resolved))
        self.register(pid, cwd, os.path.abspath(resolved), agent, started)
        return resolved


class Controller:
    def __init__(self, cfg, log=lambda *_: None, now=None, probe=None, session_id=None):
        # A config that predates the context restart keeps working, disabled.
        self.cfg = dict(CONTEXT_DEFAULTS, **cfg)
        cfg = self.cfg
        self.log = log
        self.state = IDLE
        self.wake_at = 0.0
        self.attempts = 0
        self.banner = None
        self.last_user_input = 0.0
        self.last_working = 0.0
        self.working_since = 0.0   # start of the current uninterrupted working spell
        self.limit_at = 0.0        # when the wait we are serving was scheduled
        self.pending_input_chars = 0
        self.menu_open = False
        self.deferred = False      # the reset has passed and a gate is holding it
        self.typing_since = 0.0    # when that gate was first found shut
        self.started = now if now is not None else time.time()
        self.banner_text = None
        self.incident = None      # "limit" or "stall": what we are waiting out
        self.limit_source = None  # "transcript" or "screen": how we learned of it
        self.rejected = set()     # banners already weighed against the current wait
        self.stall_key = None     # the last stall acted on, and when
        self.stall_at = 0.0
        self.stall_streak = 0     # stalls since the last turn that finished
        self._carry = ""          # overlap so a marker split across two reads still matches
        self._input_carry = b""   # an escape sequence cut in half by a read boundary
        self.last_draft_change = now if now is not None else time.time()

        # -- the context restart ---------------------------------------------- #
        # Reading the handoff file is the only I/O any of this needs, and it goes
        # through `probe` so the tests can hand over a file that never existed.
        self.probe = probe or read_handoff
        # `{id}` makes a shared CR_HANDOFF_FILE unique per session on its own;
        # a path without it may still get suffixed by HandoffRegistry.claim
        # before this cfg ever reaches here (T05) — either way, self.cfg
        # carries the resolved path from this point on.
        self.session_id = session_id or os.urandom(4).hex()
        if "{id}" in cfg["handoff_file"]:
            cfg["handoff_file"] = cfg["handoff_file"].replace("{id}", self.session_id)
        self.handoff_path = os.path.abspath(cfg["handoff_file"])
        self.resume_text = cfg["resume_msg"].replace("{file}", cfg["handoff_file"])
        self.cancel_text = cfg["cancel_msg"].replace("{file}", cfg["handoff_file"])
        self.context_tokens = None    # what the session's last turn was sent with
        self.context_model = None
        self.context_window_hint = None   # a window the transcript states outright
        self.context_window_hint_source = "the transcript"   # _resolve_window's log label
        self.status_session_id = None   # claude's own statusline session_id (T20): a
                                         # corroborating signal only -- T02's own
                                         # ClaudeSessionRegistry binding still decides
                                         # which transcript is ours.
        self.context_window = None
        self.context_estimated = False  # the window above is a guess, not a confirmed one (T19)
        self.window_unknown = None    # a model nothing here can size, for the lookup
        self.learned = {}             # slug -> window, from whoever answered
        self.context_limit = None     # the threshold in tokens; None = no trigger
        self.context_path = None      # the transcript those figures came from
        self.context_ambiguous = False   # >1 unconfirmed candidate rollout (T03)
        self.context_grew_at = 0.0    # ...and when it last gained a byte
        self.context_off = False      # a failure bad enough not to repeat
        self.last_stop_reason = None  # how the turn we are waiting on ended
        # codex states where a turn starts and ends; None is "nobody said", which
        # is all Claude Code's transcript ever says.
        self.turn_open = None
        self.agent_status = None      # "busy"/"idle" from sessions/<pid>.json (T02)
        self.agent_status_at = 0.0    # when the registry last said that changed
        self.codex = cfg.get("agent") == "codex"
        self.context_baseline = CODEX_BASELINE_TOKENS if self.codex else 0
        self.codex_cap = None         # where codex compacts, as its own log states it
        self.counted_by_log = None    # the rollout whose count now comes from that log
        self.interrupt_at = 0.0       # when a running turn was last interrupted
        self.self_compactions = 0     # times codex got there first
        self.threshold_crossed_at = None  # when context_tokens first reached
                                       # trigger_limit() for the turn now running;
                                       # None until it does, cleared on open/close
                                       # (CR_CODEX_INTERRUPT_AFTER_SEC, T22)
        self._observed_fold_cost = None   # largest codex fold cost seen so far,
                                       # seeded from codex_folds_file on first use
                                       # (CR_CODEX_RESERVE_ADAPT, T22)
        self.rstate = None            # None | HANDOFF_SENT | HANDOFF_OK | CLEAR_SENT |
                                       # CLEARED | RESUME_SENT | UNFOLD_FAILED |
                                       # CANCEL_PENDING
        self.nonce = None             # the marker only this attempt can satisfy
        self.handoff_sent_at = 0.0
        self.end_turn_seen_at = 0.0   # when end_turn last landed on the attached
                                       # transcript, at or after handoff_sent_at --
                                       # a later tool_use must not erase this (T12)
        self.handoff_verified_at = None   # when the latch caught (T12); None until it does
        self._handoff_verified_st = None  # the file snapshot the latch was taken from
        self.handoff_tries = 0        # the model failing to fold, not the world failing
        self.resume_tries = 0
        self.restart_left = 0.0       # timeout budget for the current step
        self.rwake = 0.0              # earliest moment for the next step
        self.cycles = 0
        self.cooldown_until = 0.0
        self._restart_times = []      # when each restart actually began, for the
                                       # rolling-hour frequency guard (T13)
        self.context_before = 0       # what the context read just before /clear
        self.resume_echoed = False
        self.handoff_text = None      # the exact fold phrase last sent, nonce and all
        self.handoff_echoed = False   # that phrase, seen echoed back in a transcript
        self.handoff_queued = False   # ...or at least seen waiting in claude's queue
        self.handoff_echo_retries = 0 # retypes for want of an echo, not for a bad file
        self.last_output_at = 0.0     # last byte of any kind read from the pty
        self.clear_sent_at = 0.0      # when the current /clear (or its one reprint) went out
        self.clear_retried = False    # the one reprint CLEAR_SENT allows itself
        self._clear_from_path = None  # context_path at /clear time; a change confirms it
        self._debt_notify_at = 0.0    # last time a stuck CLEARED/UNFOLD_FAILED/off-for-
                                       # the-session debt said so again (T14)
        self._cleared_at = 0.0        # when this restart's CLEARED step began, so a gate
                                       # held past a minute can say so (badge_warn, T14)
        self._cleared_by_hand = False # CLEARED because a person cleared the session
                                       # themselves while the fold waited, not by our /clear
        self._off_reason = None       # why context_off was set, for the repeated notify
        self._context_off_acked = False   # a key was pressed while that debt stood (T14)
        self._restart_tick = None     # last tick the restart clock actually ran
        self._rearm = False           # a wait ended mid-restart: re-send the step
        self._gate_note = None
        self._window_bumped = False
        self._cancel_sent = False     # CANCEL_PENDING: the phrase already went out,
                                       # this tick only closes the restart out (T09)

    # -- inputs ------------------------------------------------------------- #
    # Not everything arriving on our stdin was typed. The terminal answers
    # claude's queries down the same pipe — a device attributes report, a cursor
    # position report, and (claude sends "\x1b[>q" at startup) an XTVERSION reply
    # such as "\x1bP>|iTerm2 3.5.11\x1b\\". Only CSI and SS3 end at their first
    # final byte; DCS/OSC/APC/PM/SOS run until a String Terminator, and an X10
    # mouse report carries three raw coordinate bytes after its final "M". Anything
    # we mis-parse is counted as characters the human typed, which parks the retry
    # behind a draft that does not exist.
    _STRING_OPENERS = (0x50, 0x5d, 0x58, 0x5e, 0x5f)   # DCS  OSC  SOS  PM  APC

    def _is_terminal_reply(self, data, i, j):
        """Is data[i:j] the terminal talking, rather than a key someone pressed?

        Both arrive as escape sequences and there is no flag that separates them,
        so this goes by shape. Only what a keyboard cannot produce is claimed:
        the string replies, the reports claude asked for, mouse and focus
        traffic. Arrows, function keys, Home/End, a lone Alt+key and everything
        else unrecognised stay human — mistaking a key for a report is the
        expensive direction, because it lets the wrapper type over someone.
        """
        nxt = data[i + 1]
        if nxt in self._STRING_OPENERS:
            return True                 # XTVERSION, colour queries, DECRQSS…
        if nxt != 0x5b:                 # SS3 (F1-F4, keypad) and Alt+key: human
            return False
        body = data[i + 2:j]
        if not body:
            return False
        head, final = body[0], body[-1]
        if head == 0x4d:                            # "\x1b[M": X10 mouse
            return True
        if head == 0x3c and final in (0x4d, 0x6d):  # "\x1b[<0;36;12M": SGR mouse
            return True
        if head == 0x3f:                            # private report: DA, kitty…
            return True
        if final in (0x52, 0x63, 0x74, 0x6e):       # R: cursor  c: DA  t: window  n: DSR
            return True
        if final in (0x49, 0x4f) and len(body) == 1:            # focus in/out
            return True
        if final == 0x7e and body[:-1] in (b"200", b"201"):     # paste brackets
            return True                 # the pasted text between them still counts
        return False

    def _skip_escape(self, data, i, n):
        """Index just past the escape sequence at data[i], or None if truncated."""
        nxt = data[i + 1]
        if nxt in (0x5b, 0x4f):                 # CSI "\x1b[" / SS3 "\x1bO"
            j = i + 2
            while j < n and 0x20 <= data[j] <= 0x3f:
                j += 1
            if j >= n:
                return None
            if nxt == 0x5b and j == i + 2 and data[j] == 0x4d:
                # X10 mouse: "\x1b[M" then button/x/y as raw bytes, each of them
                # 0x20 or above and so indistinguishable from typed text.
                return j + 4 if j + 4 <= n else None
            return j + 1
        if nxt in self._STRING_OPENERS:
            j = i + 2
            while j < n:
                if data[j] == 0x07:                                     # BEL
                    return j + 1
                if data[j] == 0x1b:
                    if j + 1 >= n:
                        return None
                    if data[j + 1] == 0x5c:                             # ST
                        return j + 2
                j += 1
            return None
        return i + 2                            # Alt+key

    # Claude Code asks the terminal for the kitty keyboard protocol (`CSI > 5 u`)
    # and xterm's modifyOtherKeys (`CSI > 4 ; 2 m`) at startup, and a terminal
    # that honours either one stops sending the plain control bytes for the
    # keys that empty the input box: Esc arrives as `CSI 27 u`, Ctrl+C as
    # `CSI 99 ; 5 u` or `CSI 27 ; 5 ; 99 ~`. Skipped as anonymous escapes, they
    # left a draft that had already been cleared counted as still there, and
    # that phantom held a context restart behind the draft gate for minutes
    # (T31). Modifier bits are the protocol's value minus one.
    _MOD_SHIFT, _MOD_ALT, _MOD_CTRL, _MOD_SUPER = 1, 2, 4, 8
    _CTRL_CLEARS = (ord("c"), ord("u"), ord("w"))    # ^C clears, ^U/^W erase back

    def _escaped_key(self, data, i, j):
        """What data[i:j], an escape sequence a key produced, does to the box:
        "submit", "clear", "erase" (one character), "type" (one more), or None
        for anything that leaves it alone (arrows, F-keys, key releases...)."""
        nxt = data[i + 1]
        if nxt in (0x7f, 0x08) and j == i + 2:
            return "clear"                      # Alt+Backspace: a word, like ^W
        if nxt != 0x5b:
            return None
        body = data[i + 2:j]
        if not body or body[-1:] not in (b"u", b"~"):
            return None
        fields = body[:-1].split(b";")
        try:
            if body[-1:] == b"u":                   # kitty: code[:alt];mods[:event];text
                code = int(fields[0].split(b":")[0])
                mod = fields[1].split(b":") if len(fields) > 1 and fields[1] else [b"1"]
                if len(mod) > 1 and mod[1] == b"3":
                    return None                     # a key release, not a press
                mods = int(mod[0]) - 1
            elif len(fields) == 3 and fields[0] == b"27":   # modifyOtherKeys
                mods, code = int(fields[1]) - 1, int(fields[2])
            else:
                return None
        except ValueError:
            return None
        if mods < 0:
            return None
        if code == 27:
            return "clear" if not mods else None
        if code == 13:
            return "submit" if not mods else "type"     # Shift+Enter: a newline
        if code in (0x7f, 0x08):
            return "clear" if mods & (self._MOD_ALT | self._MOD_CTRL | self._MOD_SUPER) else "erase"
        if mods & self._MOD_CTRL and not mods & (self._MOD_ALT | self._MOD_SUPER):
            return "clear" if code in self._CTRL_CLEARS else None
        if code >= 0x20 and not mods & (self._MOD_ALT | self._MOD_CTRL | self._MOD_SUPER):
            return "type"
        return None

    def _apply_key(self, key):
        if key in ("submit", "clear"):
            self.pending_input_chars = 0
            if key == "submit":
                self.menu_open = False
        elif key == "erase":
            self.pending_input_chars = max(0, self.pending_input_chars - 1)
        elif key == "type":
            self.pending_input_chars += 1

    def on_user_bytes(self, data, now):
        """Track the human so we never type over a half-written prompt.

        Counts printable characters typed since the last submit/clear, and marks
        when someone was last at the keyboard. Escape SEQUENCES (arrows, function
        keys, mouse reports, the terminal's replies to claude) are consumed whole
        — naively counting their bytes made every cursor movement look like an
        unsent draft, which would defer the retry indefinitely. The ones the
        terminal wrote by itself do not count as presence either: a report
        arriving every few seconds would hold the "user is typing" gate shut for
        as long as the session lives.
        """
        before = self.pending_input_chars
        human = False
        if self._input_carry:
            data = self._input_carry + data
            self._input_carry = b""
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == 0x1b:
                if i + 1 >= n:
                    # A lone Escape at the end of a read is the human pressing
                    # Esc, which clears Claude Code's input box.
                    self.pending_input_chars = 0
                    human = True
                    i += 1
                    continue
                j = self._skip_escape(data, i, n)
                if j is not None and not self._is_terminal_reply(data, i, j):
                    human = True
                    self._apply_key(self._escaped_key(data, i, j))
                if j is None:
                    # Cut in half by the read boundary. Finish it next time rather
                    # than let its tail be mistaken for typing; cap the carry so a
                    # sequence that never terminates cannot grow without bound.
                    tail = data[i:]
                    self._input_carry = tail if len(tail) <= 4096 else b""
                    break
                i = j
                continue
            human = True                        # nothing below this line is ours
            if b in (0x0d, 0x0a):               # Enter: submitted, box is empty
                self.pending_input_chars = 0
                self.menu_open = False
            elif b in (0x03, 0x15, 0x17):       # ^C / ^U / ^W: cleared
                self.pending_input_chars = 0
            elif b in (0x7f, 0x08):
                self.pending_input_chars = max(0, self.pending_input_chars - 1)
            elif b >= 0x20:
                self.pending_input_chars += 1
            i += 1
        if human:
            self.last_user_input = now
            if self.rstate == UNFOLD_FAILED:
                # The failure itself is not undone -- context_off stays set -- but
                # a human is now at the keyboard, and a badge/notify that keeps
                # repeating at someone who already saw it is just noise. What is
                # left of the debt (context_off, badge_warn's "restart off") is
                # acknowledged in the same stroke -- the same person just saw it.
                self.log("a key was pressed; the unfold-failed notice is dismissed")
                self._end_restart(now)
                self._context_off_acked = True
            elif self.context_off:
                # Not the unfold-failed case -- T13's frequency guard, or a clear
                # that did nothing -- but the same standing debt (T14): a key
                # pressed while it is outstanding is proof enough that someone
                # finally saw it, and the repeated notify can stop.
                self._context_off_acked = True
        if self.pending_input_chars != before:
            self.last_draft_change = now

    def on_output(self, text, now):
        self.last_output_at = now     # any byte at all, not just a working footer (T08)
        # Claude's streaming footer can land split across two reads, so match on a
        # small overlap with the previous chunk rather than the chunk alone. It must
        # stay small: matching the whole rolling window would keep `last_working`
        # fresh long after the footer stopped repainting, and the idle gate would
        # never open.
        probe = self._carry + text
        self._carry = probe[-160:]
        if is_working(probe):
            if now - self.last_working >= self.cfg["busy_idle"]:
                # A gap that long ended the previous spell; this is a new one.
                self.working_since = now
            self.last_working = now
        if is_menu(probe):
            self.menu_open = True

    def on_echo(self, now):
        """claude wrote our retry into the transcript: it was really submitted."""
        if self.state != VERIFY:
            return False
        self.log("retry accepted (transcript echo)")
        self.state = IDLE
        self.attempts = 0
        self.banner = None
        return True

    # A wait can also end because the limit did, without the reset we were told
    # about ever arriving: the human logs into another account, upgrades the plan,
    # or the quota simply comes back early. Nothing announces that — the only
    # evidence is that claude is answering again. Without this, the wrapper keeps
    # counting down its 48 hours over a session that has been working for hours,
    # and eventually types "continue" into the middle of it.
    ALIVE_GRACE = 8.0        # transcript rows this close to the banner are its own

    def on_alive(self, now, source, path=None):
        """The session is answering again: whatever we were waiting for is over.

        `path` is the transcript the row came from, when the caller knows one
        (main() always does for `source == "transcript"`). A neighbour's
        session answering is not evidence about ours, so a path that does not
        match `context_path` clears nothing — the omitted-path callers below
        (a screen read, or a test with no transcript of its own) are trusted
        as before.
        """
        if self.state not in (WAITING, VERIFY):
            return False
        if path is not None and path != self.context_path:
            return False
        # Scaled like every other wait, so a compressed test run does not have to
        # spend eight real seconds proving something about a 48-hour limit.
        if now - self.limit_at < self.ALIVE_GRACE / self.cfg["wait_scale"]:
            # Rows written just before the limit landed can reach us just after
            # it did. They say nothing about the limit that followed them.
            return False
        self.log("session is answering again (%s); dropping the wait" % source)
        self.state = IDLE
        self.attempts = 0
        self.banner = None
        self.banner_text = None
        self.limit_source = None
        self.rejected = set()
        self.wake_at = 0.0
        if self.rstate is not None:
            # The restart was suspended by this wait, not cancelled by it. Its
            # current step has to go out again — nothing else will send it.
            self._rearm = True
        return True

    # A stall is the other way a session stops without being finished: the
    # server refused the turn. There is no reset time to parse — that is what
    # makes it a stall rather than a limit — so the wait is one we pick, and it
    # doubles for each stall that comes straight back, because a service that
    # just said it is full does not want to be asked again in a minute forever.
    #
    # The streak is what carries that across incidents. `attempts` cannot: the
    # nudge lands, the session answers, the controller goes idle, and the next
    # refusal arrives as a brand new incident with the count back at zero.
    STALL_SAME = 30.0        # the same wording again this soon is the same stall

    def on_stall(self, text, now, source):
        """The server refused the turn. Nudge it again, later each time."""
        if self.cfg["stall_wait"] <= 0:
            return False
        key = re.sub(r"\s+", " ", text or "").strip().lower()
        if self.state in (WAITING, VERIFY):
            return False                        # already waiting something out
        if key and key == self.stall_key and now - self.stall_at < self.STALL_SAME:
            # A repainting TUI redraws the same line for as long as it is on
            # screen, and the transcript can report it once more besides.
            return False
        if self.stall_streak >= self.cfg["stall_max_attempts"]:
            if self.stall_key is not None:
                self.log("stalled %d times in a row (%s); leaving it alone"
                         % (self.stall_streak, source))
                self.stall_key = None
            return False
        secs = min(self.cfg["stall_wait"] * self.cfg["stall_backoff"] ** self.stall_streak,
                   self.cfg["stall_max_wait"], self.cfg["max_wait"])
        self.stall_streak += 1
        self.stall_key = key
        self.stall_at = now
        self.log("stall detected (%s), nudging in %.0fs (streak %d): %s"
                 % (source, secs, self.stall_streak, text))
        if self.rstate is not None:
            self.log("the stall landed mid-restart (%s); the restart waits it out"
                     % self.rstate)
        self.banner = key
        self.banner_text = text
        self.incident = "stall"
        self.state = WAITING
        self.attempts = 0
        self.deferred = False
        self.typing_since = 0.0
        self.limit_at = now
        self.wake_at = now + secs / self.cfg["wait_scale"]
        return True

    def on_turn_done(self, now):
        """A turn ended with no error, so nothing is refusing turns any more."""
        if self.stall_streak:
            self.log("a turn completed; the stall streak (%d) is over" % self.stall_streak)
        self.stall_streak = 0
        self.stall_key = None

    def rescrapable(self):
        """May the screen still be read while this wait runs?

        Only for a wait the screen itself scheduled. A scraped banner can be the
        wrong one — a card on the agent roster, a line of history behind it — and
        the wait it schedules is otherwise unfixable: the screen channel closes
        the moment we leave IDLE, and a roster has no transcript of its own to
        correct it from. Something told this terminal to sleep until 2:10am over
        a neighbour's limit and nothing could talk it out of it. A wait the
        transcript scheduled is this session's own and needs no second opinion.
        """
        return (self.state == WAITING and self.incident == "limit"
                and self.limit_source == "screen")

    def on_limit(self, banner, now, source):
        key = re.sub(r"\s+", " ", banner or "").strip().lower()
        if self.state in (WAITING, VERIFY) and key == self.banner:
            return False                        # same incident, already scheduled
        if key in self.rejected:
            return False                        # weighed against this wait already
        secs = parse_reset(banner)
        # A second reading during a scraped wait is a correction, not an
        # incident: it is only worth having if it frees the session EARLIER.
        # Anything later is how a stale banner would extend a wait forever.
        if self.rescrapable() and source == "screen":
            wake = now + min((secs if secs is not None else self.cfg["fallback_wait"])
                             + self.cfg["margin"], self.cfg["max_wait"]) / self.cfg["wait_scale"]
            if wake >= self.wake_at:
                self.rejected.add(key)
                return False
            self.log("the screen states an earlier reset than the one we are waiting on "
                     "(%s earlier); taking it: %s"
                     % (human_left(self.wake_at - wake), banner))
        if secs is None:
            secs = self.cfg["fallback_wait"]
            self.log("limit detected (%s), no reset time parsed -> fallback %.0fs: %s"
                     % (source, secs, banner))
        else:
            self.log("limit detected (%s), resets in %.0fs: %s" % (source, secs, banner))
        if self.rstate is not None:
            # Not a reason to abandon the restart: the handoff file outlives a
            # wait of any length, and every clock the restart runs on stops here.
            self.log("the limit landed mid-restart (%s); the restart waits it out"
                     % self.rstate)
        secs = min(secs + self.cfg["margin"], self.cfg["max_wait"])
        self.banner = key
        self.banner_text = banner
        self.incident = "limit"
        self.limit_source = source
        self.rejected = set()
        self.state = WAITING
        self.attempts = 0
        self.deferred = False
        self.typing_since = 0.0
        self.limit_at = now
        self.wake_at = now + secs / self.cfg["wait_scale"]
        return True

    # -- clock -------------------------------------------------------------- #
    def tick(self, now):
        """Return an action: None, ('inject', text, dismiss_menu), ('resume',) or
        ('notify', text)."""
        if self.state in (WAITING, VERIFY):
            # A restart makes no progress while the quota is out, and none of its
            # clocks may run out either: a weekly limit is days long and a
            # fifteen-minute handoff timeout would expire inside it, aborting a
            # restart that is going perfectly well.
            self._restart_tick = None
            return self._tick_limit(now)
        action = self._tick_restart(now)
        if action is not None:
            return action
        return self._tick_limit(now)

    def _tick_limit(self, now):
        if self.state == WAITING:
            if self._alive_again(now) and self.on_alive(now, "output"):
                return ("resume",)
            if now < self.wake_at:
                return None
            if self.attempts >= self.cfg["max_attempts"]:
                self.log("giving up after %d attempts" % self.attempts)
                self.state = DONE
                self.deferred = False
                return None
            blocked = self._blocked(now)
            if blocked == "user is typing" and not self.pending_input_chars:
                # With an empty box this gate defers to a pair of hands and
                # nothing else — there is no half-written thought to protect,
                # only the courtesy of not typing while someone is. If it has
                # held this long, either nobody is there or something is
                # stamping presence on their behalf. (A draft takes the branch
                # below instead, and keeps its own, longer grace.)
                if not self.typing_since:
                    self.typing_since = now
                elif now - self.typing_since >= self.cfg["typing_max"]:
                    self.log("the keyboard gate has held for %.0fs; retrying anyway"
                             % (now - self.typing_since))
                    blocked = None
            else:
                self.typing_since = 0.0
            if blocked:
                self.deferred = True
                self.log("deferring retry: %s" % blocked)
                self.wake_at = now + 15
                return None
            self.deferred = False
            self.typing_since = 0.0
            # `continue` means "carry on with what you were doing", and that is
            # the wrong instruction at every step of a restart but the last.
            if self.rstate is not None:
                handled, step = self._after_limit(now)
                if handled:
                    self.state = IDLE
                    self.attempts = 0
                    return step
            self.attempts += 1
            self.state = VERIFY
            self.wake_at = now + self.cfg["verify"]
            self.log("sending retry (attempt %d/%d)" % (self.attempts, self.cfg["max_attempts"]))
            return ("inject", self.cfg["message"], self._take_dismiss())

        if self.state == VERIFY:
            if now - self.last_working < self.cfg["busy_idle"]:
                self.log("session resumed")
                self.state = IDLE
                self.attempts = 0
                self.banner = None
                return None
            if now >= self.wake_at:
                self.log("retry did not take hold; re-arming")
                self.state = WAITING
                self.wake_at = now
                # Re-enter immediately: making the caller wait for the next tick
                # would silently drop an attempt, and at the give-up boundary
                # would leave the controller parked in WAITING forever.
                return self._tick_limit(now)
            return None
        return None

    def _alive_again(self, now):
        """Has claude been working steadily since the limit we are waiting on?

        A refusal still paints a second or two of footer before it lands, so a
        flicker proves nothing — and the /rate-limit-options retry loop paints
        one too. A spell that keeps the footer alive for `resume` seconds cannot
        be a session that is still refused.
        """
        if self.working_since <= self.limit_at:
            return False                        # the spell predates the banner
        if now - self.last_working >= self.cfg["busy_idle"]:
            return False                        # it has already ended
        return self.last_working - self.working_since >= self.cfg["resume"]

    def _take_dismiss(self):
        """Consume the "a menu is open" flag: the next thing typed dismisses it."""
        dismiss = self.menu_open
        self.menu_open = False
        return dismiss

    def _blocked(self, now):
        if now - self.last_working < self.cfg["busy_idle"]:
            return "claude is working"
        return self._blocked_by_human(now)

    def _blocked_by_human(self, now):
        """The gates that protect a person, apart from the one that protects a turn.

        A context restart uses only these. Whether the session is busy is a
        question the transcript answers exactly (`_session_busy`); the screen
        cannot, because in a session that keeps background work running there is
        always something painting a footer and it would never read idle.
        """
        if now - self.last_user_input < self.cfg["user_idle"]:
            return "user is typing"
        if self.pending_input_chars > 0:
            if now - self.last_draft_change < self.cfg["draft_grace"]:
                return "unsent text in the prompt box"
            # The counter only moves on keystrokes we can see, so anything that
            # desyncs it — a terminal reply we failed to parse, a box claude
            # cleared on its own — would otherwise defer the retry forever, which
            # is the one failure mode this whole wrapper exists to prevent. A
            # draft nobody has touched for this long is treated as not there.
            self.log("draft untouched for %.0fs; the input gate looks stale, "
                     "proceeding" % (now - self.last_draft_change))
            self.pending_input_chars = 0
            self.last_draft_change = now
        return None

    # ----------------------------------------------------------------------- #
    # the context restart
    # ----------------------------------------------------------------------- #
    # Fold the session into a file, prove the file is real, clear the session,
    # unfold it. Four steps, and the third one is irreversible — everything else
    # here exists to make sure it is never reached on a promise.

    MTIME_SLACK = 2.0      # some filesystems keep mtime to the second
    RESTART_DROP = 0.5     # the context has to at least halve for /clear to have worked
    BADGE_NEAR = 0.8       # show the percentage only once the threshold is in sight
    ESTIMATE_CONFIRM_FRAC = 0.6  # a guess that survives this much of itself is not a guess (T19)
    GATE_RETRY = 5.0       # how long to sit on a held step before looking again
    INTERRUPT_RETRY = 20.0 # between Escs, if the first did not close the turn
    HANDOFF_ECHO_RETRIES = 2   # retypes for a phrase that left no trace at all
    UNFOLD_STUCK = 60.0    # CLEARED held on the gate this long reads as stuck, not routine (T14)

    @property
    def context_enabled(self):
        return not self.context_off and (
            self.cfg["context_tokens"] > 0 or self.cfg.get("context_restart_on", False))

    def context_pct(self):
        """How full, in the accounting of the agent's own status line."""
        if not self.context_window or self.context_tokens is None:
            return None
        base = self.context_baseline
        if self.context_window <= base:
            return None
        return 100.0 * max(0, self.context_tokens - base) / (self.context_window - base)

    # -- inputs ------------------------------------------------------------- #
    def bind_transcript(self, path, why, now):
        """The one place `context_path` changes.

        Every per-transcript count `on_context` had been carrying for the file
        we are leaving means nothing for the one we are joining — a stale
        `context_window_hint` or `last_stop_reason` from another session would
        otherwise leak into this one's first reading. `context_model` is the
        one exception: the API has not yet had a chance to restate it, and
        until it does the old value is still the best guess at the window.
        """
        old = self.context_path
        self.context_path = path
        self.context_tokens = None
        self.turn_open = None
        self.codex_cap = None
        self.context_window_hint = None
        self.context_window_hint_source = "the transcript"
        self.counted_by_log = None
        self.last_stop_reason = None
        self.context_grew_at = 0.0
        self._window_bumped = False
        if old is None:
            self.log("context bound: %s (%s)" % (path, why))
        else:
            self.log("transcript switched: %s → %s (%s)" % (old, path, why))
            if why == "session identity":
                self._overtaken(now)

    def _overtaken(self, now):
        """The session's identity moved on before our `/clear` went out.

        Only a person can have done that -- `/clear` (or `/resume`) typed by
        hand while the fold sat behind a gate -- and the session it leaves in
        place no longer holds the context the fold was about. Sending our own
        `/clear` now would wipe whatever they started in its place; one such
        `/clear` once landed on a session that had already read the handoff
        and was back at work. Only claude's own registry naming a new session
        gets here: a guess from the growth heuristic proves nothing of the
        kind, and our fold phrase echoed elsewhere only corrects which
        transcript was ours all along.
        """
        if self.rstate not in (HANDOFF_SENT, HANDOFF_OK):
            return
        at = self.rstate
        if at == HANDOFF_SENT and self._handoff_fault(self.probe(self.handoff_path)):
            # The fold never finished, and the session that was asked for it
            # is gone: there is nothing to unfold, and nobody left to cancel
            # the ask with.
            self.log("the session was replaced at %s, before the handoff was "
                     "finished; dropping this restart" % at)
            self._end_restart(now)
            return
        self.log("the session was cleared by hand at %s; skipping our %s"
                 % (at, self.cfg["clear_cmd"]))
        self._cleared_by_hand = True
        self.restart_left = self.cfg["handoff_timeout"]
        self._enter_cleared("cleared by hand", now)

    def on_human_prompt(self, now):
        """A person submitted a prompt to this terminal's session (T30).

        After a clear done by hand, that is them carrying on themselves --
        reading the handoff, or doing something else entirely -- and typing
        the resume phrase on top of it would only interrupt them."""
        if self.rstate != CLEARED or not self._cleared_by_hand:
            return False
        self.log("a prompt was typed into the session cleared by hand; "
                 "the unfold is not needed")
        self._end_restart(now)
        return True

    def on_candidates(self, ambiguous, now):
        """More than one just-started rollout shares this directory and no
        handoff echo has proved yet which one is ours (T03).

        Usage still updates from whatever `bind_transcript` currently points
        at either way — that number is only wrong for as long as the guess
        is, and the guess is the whole reason this exists. What it gates is
        the one thing a wrong guess cannot be allowed to do: fold a session
        that was never actually near its limit because a neighbour's was.
        """
        if ambiguous == self.context_ambiguous:
            return
        self.context_ambiguous = ambiguous
        if ambiguous:
            self.log("more than one candidate rollout in this directory; "
                     "holding the restart until the handoff phrase proves which is ours")
        else:
            self.log("down to one candidate rollout; the restart is no longer held")

    def on_context(self, rec, now):
        """An assistant row from the transcript this terminal's session writes.

        Everything the trigger needs is already in the row, so this is two
        comparisons and no arithmetic of our own. It runs where the watcher has
        just parsed the row — no extra poll, no re-read, and nothing at all on a
        tick where the file did not grow.
        """
        path = rec.get("path")
        if path:
            if self.context_path is None:
                # Bootstrap only: in the running wrapper `bind_transcript` has
                # already been called by the time a row reaches here, so this
                # fires only for a controller fed rows directly, with no
                # switch point of its own to have called it first.
                self.bind_transcript(path, "first transcript", now)
            elif path != self.context_path:
                # A different session's transcript. `bind_transcript` is the
                # only thing allowed to move `context_path`; a row that just
                # happens to arrive on a foreign one is not evidence of a
                # `/clear`, and mixing its figures into ours is the bug this
                # guards against.
                return
        from_log = rec.get("source") == "log"
        if from_log:
            # codex's own count, and where it will compact. Once one has arrived
            # for this thread the rollout's smaller figure stops being the reading:
            # alternating between the two would move the trigger back and forth.
            self.counted_by_log = path
            cap = rec.get("cap")
            if cap and cap != self.codex_cap:
                self.codex_cap = int(cap)
                self._note_cap()
        else:
            self.context_grew_at = now
        if rec.get("compacted"):
            self._lost_the_race(now, rec.get("pre_tokens"))
        if rec.get("sidechain"):
            return                       # a subagent's context, not the session's
        if rec.get("stop_reason") is not None:
            self.last_stop_reason = rec["stop_reason"]
            if rec["stop_reason"] == "end_turn" and now >= self.handoff_sent_at:
                self.end_turn_seen_at = now
        if self.rstate == HANDOFF_OK:
            # Latched already; a row here is the only sign that the file
            # backing it might have moved out from under the wait for quiet.
            self._recheck_handoff_latch(now)
        window = rec.get("window")
        model = rec.get("model")
        hint = rec.get("model_hint")
        moved = False
        if window and window != self.context_window_hint:
            # codex writes the window it is actually using into every row of
            # accounting. Nothing we could work out from a model name beats
            # being told. Done before `on_model` below so a row that somehow
            # carried both would have the model resolve against the fresh
            # window rather than the stale one.
            self.context_window_hint = int(window)
            moved = True
        if hint and not model:
            # claude's own "Set model to <display>" line (T21): earlier than
            # any real turn, and only ever a hint -- `model` below, from an
            # actual assistant row, is what confirms or overrides it.
            self.on_model(hint, "local command", now)
        if model and self.on_model(
                model, "codex log" if self.codex else "assistant line", now):
            moved = False        # on_model already resolved against the live window
        if moved:
            self._resolve_window()
        tokens = rec.get("tokens")
        if tokens is None:
            return
        if not from_log and self.codex and self.counted_by_log == path:
            return
        self.context_tokens = tokens
        if (self.context_window and tokens > self.context_window
                and not self.context_window_hint
                and not parse_tokens(self.cfg["context_window"])):
            # Guessing low is the safe direction, but being PROVED low is not a
            # reason to keep the guess: a threshold above the window we assumed
            # is a restart that can never happen.
            self._bump_window(tokens)
        elif (self.context_estimated and self.context_window
              and tokens >= self.context_window * self.ESTIMATE_CONFIRM_FRAC):
            self._confirm_estimate(tokens)

    def on_model(self, model, source, now):
        """The one place a model change is recognized, from whichever of three
        sources noticed first (T21):

          - claude's own `<local-command-stdout>Set model to <display>` line
            (`"local command"`, via `transcript_limit_records`'s `model_hint`)
            — earlier than any turn, and only ever a hint: it can be undone
            before a single real answer comes back.
          - an assistant row's own `message.model` (`"assistant line"`) — the
            confirmed answer, and the one that always wins if it disagrees
            with an earlier hint, since it is what the account actually ran.
          - codex's `turn_context`/`thread_settings_applied` (`"codex log"`)
            — stated on every turn, the way codex states everything about
            itself.

        A no-op unless the slug actually changes (`model_slug`, not the raw
        string — a snapshot date or a "[1m]" suffix is not a different
        model). Logs the switch once, by name, distinct from `_resolve_window`'s
        own "restarting at" line: the OLD model's numbers are whatever this
        session already had resolved for it a moment ago (`context_window`/
        `context_limit`), not recomputed from scratch, so they reflect
        whatever forced window, learned answer, or override was actually in
        effect. Resets what a new model invalidates — codex's cached
        compaction cap, stale until a fresh usage line names the new one — and
        leaves the rest to `_resolve_window` (which already clears
        `_window_bumped` and recomputes the limit).

        Nothing here enacts a restart. The next `tick()` — whenever it next
        runs, not gated on a further transcript row — already re-checks the
        threshold against the freshly resolved limit, so a downward switch
        already past it folds without waiting on anything more to arrive.
        Returns whether a switch actually happened, so a caller who already
        has its own window-hint bookkeeping to do (`on_context`) knows not to
        also re-resolve.
        """
        slug = model_slug(model)
        old_slug = model_slug(self.context_model)
        if not slug or slug == old_slug:
            return False
        old_window, old_limit = self.context_window, self.context_limit
        self.context_model = model
        if self.codex:
            self.codex_cap = None
        self._resolve_window()
        if old_slug:
            self.log("model switched: %s (%s, restart at %s) → %s (%s, restart at %s) [%s]"
                     % (old_slug, human_tokens(old_window), human_tokens(old_limit),
                        slug, human_tokens(self.context_window), human_tokens(self.context_limit),
                        source))
        return True

    def _bump_window(self, tokens):
        """Escalate the assumed window past what usage just proved it to be
        (T19). One rung at a time, repeated until the number actually fits:
        200k -> 1M covers the two windows this build's own table uses; past
        that (a model bigger than the biggest one it knows, or an estimate
        that keeps being wrong) codex's own advertised ceiling is tried before
        falling back to a flat 50% bump. The result is marked as a fresh guess
        either way — a bumped window is exactly as unconfirmed as the one it
        replaced, whether the one it replaced came from a guess or from the
        profile table getting this specific session wrong.
        """
        old = self.context_window
        steps = 0
        while self.context_window < tokens and steps < 20:
            if self.context_window < BIG_WINDOW:
                self.context_window = BIG_WINDOW
            elif self.codex:
                cache = _codex_model_cache().get(model_slug(self.context_model))
                self.context_window = (cache if cache and cache > self.context_window
                                       else int(self.context_window * 1.5))
            else:
                self.context_window = int(self.context_window * 1.5)
            steps += 1
        self._window_bumped = True
        self.context_estimated = True
        self.log("context is %s, past the %s window assumed for %s; assuming %s"
                 % (human_tokens(tokens), human_tokens(old),
                    self.context_model, human_tokens(self.context_window)))
        self._recompute_limit()

    def _confirm_estimate(self, tokens):
        """A guessed window that has carried this session past
        `ESTIMATE_CONFIRM_FRAC` of itself without compacting has earned the
        benefit of the doubt (T19): stop marking it with `~` in the badge.
        """
        self.context_estimated = False
        self.log("%s: the %s window assumed for it has held past %s without "
                 "compacting; no longer marked as a guess"
                 % (self.context_model, human_tokens(self.context_window),
                    human_tokens(tokens)))

    def _correct_window_down(self, pre_tokens, now):
        """The agent just told us, by compacting on its own, that its
        effective window is no bigger than `pre_tokens` (T19): `window =
        pre_tokens / 0.92` backs off the ~8% headroom both agents' own
        compaction leaves before the true ceiling — the same margin the
        `_SMALL_CLAUDE` profiles already assume (`window * 0.95`), close
        enough not to invent a second constant for it. This can only move the
        assumption DOWN: a compaction well inside a window that already fits
        it is not evidence the window is smaller, just that the agent folded
        early. Unlike a guess, this is observed directly from the session's
        own behavior, so it does not carry the `~` an estimate does.
        """
        if not isinstance(pre_tokens, (int, float)) or pre_tokens <= 0:
            return
        corrected = int(pre_tokens / 0.92)
        if self.context_window and corrected >= self.context_window:
            return
        old = self.context_window
        self.context_window = corrected
        self.context_estimated = False
        self._window_bumped = False
        self._recompute_limit()
        if not self.context_enabled:
            return
        self.log("%s compacted at %s: the effective window is ~%s, not the %s "
                 "assumed; restarting at %s from now on"
                 % ("codex" if self.codex else "claude", human_tokens(pre_tokens),
                    human_tokens(corrected),
                    human_tokens(old) if old else "nothing",
                    human_tokens(self.context_limit) if self.context_limit
                    else "nothing"))

    def on_turn(self, state, now):
        """codex opened or closed a turn in the rollout this session writes."""
        self.turn_open = state == "open"
        # Whatever "over the threshold this long" had been counting belonged to
        # the turn that just ended, or is stale from before this one opened
        # either way, not something the next turn should inherit (T22).
        self.threshold_crossed_at = None

    def on_agent_status(self, status, updated_at):
        """`sessions/<pid>.json` (T02): one more "is anyone home" signal.

        Never the only one `_session_busy` trusts — a claude that predates the
        field, or codex, never calls this at all, and the transcript-based
        checks above still carry the whole load for them.
        """
        self.agent_status = status
        self.agent_status_at = updated_at

    def on_status(self, rec, now=None):
        """Claude's own statusline JSON, relayed by the `--cr-statusline`
        proxy (T20): a second, independent source for the window and the
        model, on top of whatever the transcript itself states.

        `context_window.context_window_size` is the one thing nothing derived
        from the model slug alone can answer — whether THIS session actually
        got the 1M window or fell back to 200k depends on the account plan,
        `/model …[1m]`, and CLAUDE_CODE_DISABLE_1M_CONTEXT, none of which show
        up in the slug (see `_resolve_window`). Filed under the same
        `context_window_hint` codex's own rollout accounting already uses, so
        the priority order in `_resolve_window` falls out unchanged.

        `model.id` is routed through `on_model()`, the same single place
        `on_context`'s own model-change branch uses — a no-op unless the
        slug actually changed, and already re-resolving the window itself.

        `session_id` is filed away as a corroborating signal only: T02's own
        `ClaudeSessionRegistry` binding is still the one thing that decides
        which transcript is ours, and nothing here competes with it.
        """
        moved = False
        window = rec.get("context_window_size")
        if window and window != self.context_window_hint:
            # Done before `on_model` below so a switch that arrives on the
            # same report resolves against the fresh window, not the stale one.
            self.context_window_hint = int(window)
            self.context_window_hint_source = "claude's status line"
            moved = True
        model = rec.get("model_id")
        if model and self.on_model(model, "claude's status line", now):
            moved = False        # on_model already resolved against the live window
        if moved:
            self._resolve_window()
        session_id = rec.get("session_id")
        if session_id:
            self.status_session_id = session_id

    # -- codex's own compaction --------------------------------------------- #
    def interrupt_line(self):
        """The count past which a running codex turn is stopped, or None.

        Far enough under codex's cap for the fold to be asked for and written:
        the folding turn adds its own reply and a file write to a context that is
        already nearly full, and a turn that reaches the cap is compacted by codex
        halfway through.
        """
        if not self.codex or not self.codex_cap:
            return None
        return max(0, self.codex_cap - self._effective_codex_reserve())

    def _folds_path(self):
        return self.cfg.get("codex_folds_file") or os.path.expanduser(
            "~/.agent-retrier/folds.json")

    def _effective_codex_reserve(self):
        """CR_CODEX_RESERVE_TOKENS, or more once CR_CODEX_RESERVE_ADAPT=1 and an
        observed fold has outgrown it (T22): 1.25x the largest fold cost seen,
        seeded from codex_folds_file so a session benefits from what earlier
        ones already learned even before it folds once itself. Never lower
        than the configured number -- this only ever raises it.
        """
        base = int(self.cfg.get("codex_reserve", 0) or 0)
        if not self.cfg.get("codex_reserve_adapt"):
            return base
        if self._observed_fold_cost is None:
            self._observed_fold_cost = _fold_history_max(self._folds_path())
        return max(base, int(1.25 * (self._observed_fold_cost or 0)))

    def trigger_limit(self):
        """The restart threshold, never past the point codex would get there first."""
        line = self.interrupt_line()
        if self.context_limit is None:
            return None
        return min(self.context_limit, line) if line is not None else self.context_limit

    def _note_cap(self):
        if not self.context_enabled:
            return
        line = self.interrupt_line()
        self.log("codex compacts this thread at %s by its own count; restarting at %s, "
                 "interrupting a running turn past %s"
                 % (human_tokens(self.codex_cap), human_tokens(self.trigger_limit() or 0),
                    human_tokens(line or 0)))
        if self.context_limit and line is not None and self.context_limit > line:
            self.log("the %s threshold is past the point codex would compact first; "
                     "using %s instead" % (human_tokens(self.context_limit),
                                           human_tokens(line)))

    def _lost_the_race(self, now, pre_tokens=None):
        """The agent compacted the context on its own, ahead of the restart:
        codex's own `compacted` event (no count of its own — `pre_tokens`
        defaults to the last count this session reported), or claude's
        `compact_boundary` (which states `pre_tokens` outright, T19).
        """
        if not self.context_enabled:
            return                       # nothing was racing it
        self.self_compactions += 1
        pre_tokens = pre_tokens if pre_tokens is not None else self.context_tokens
        agent = "codex" if self.codex else "claude"
        if self.codex:
            self.log("codex compacted the thread on its own before the restart could "
                     "(%d this session) — the count stood at %s of a %s cap"
                     % (self.self_compactions, human_tokens(pre_tokens),
                        human_tokens(self.codex_cap)))
        else:
            self.log("claude compacted the thread on its own before the restart could "
                     "(%d this session) — the count stood at %s"
                     % (self.self_compactions, human_tokens(pre_tokens)))
        self._correct_window_down(pre_tokens, now)
        # Whatever this thread's count was, it is gone, and a restart in flight is
        # judging a context that no longer exists.
        self.context_tokens = None
        if self.rstate is None:
            return
        if self.rstate in (HANDOFF_SENT, HANDOFF_OK) and self._handoff_fault(
                self.probe(self.handoff_path)) is None:
            # The agent reached its own compaction before the wrapper's /clear
            # did, but the handoff it was racing had already landed — the file
            # passes every layer `_check_handoff` would have accepted. It did
            # the clearing for us; sending `/clear` again would only retype
            # into a context that is already gone, so what is missing is the
            # unfold, not another restart from scratch.
            self.log("the handoff had already landed when %s compacted; "
                     "skipping straight to unfold instead of aborting" % agent)
            self.rstate = CLEARED
            self.restart_left = self.cfg["handoff_timeout"]
            self.rwake = now
            self._debt_notify_at = now
            self._cleared_at = now
            return
        self._abort_restart("%s compacted the thread itself during %s" % (agent, self.rstate),
                            now)

    def _maybe_interrupt(self, now):
        """Stop a running codex turn that is about to be compacted -- or one
        that has simply sat past the ordinary threshold too long, or None.

        This is only ever reached (via `_maybe_restart`) once `context_tokens`
        has already reached `trigger_limit()`, so the first call each turn is
        exactly when that crossing happened. An orchestrator turn can run for
        hours -- far longer than the cap-reserve line below reliably gives it
        room for -- so CR_CODEX_INTERRUPT_AFTER_SEC (T22) interrupts it once it
        has sat past that ordinary threshold this many seconds, without waiting
        for the harder line. 0 (default): unchanged, only the line below fires.
        """
        if not (self.codex and self.turn_open and self.cfg.get("codex_interrupt", True)):
            return None
        if self.threshold_crossed_at is None:
            self.threshold_crossed_at = now
        line = self.interrupt_line()
        over_line = (line is not None and self.context_tokens is not None
                    and self.context_tokens >= line)
        after_sec = self.cfg.get("codex_interrupt_after_sec", 0) or 0
        stuck = (not over_line and after_sec > 0
                and now - self.threshold_crossed_at >= after_sec)
        if not (over_line or stuck):
            return None
        if now - self.interrupt_at < self.INTERRUPT_RETRY:
            return None                  # one Esc, then give the turn time to close
        why = self._blocked_by_human(now)
        if why:
            self._gate_note = why
            return None
        self.interrupt_at = now
        if over_line:
            self.log("context is %s, within %s of codex compacting at %s; interrupting the "
                     "running turn to fold it up" % (human_tokens(self.context_tokens),
                                                     human_tokens(self.codex_cap - line),
                                                     human_tokens(self.codex_cap)))
        else:
            self.log("the running turn has sat past the %s threshold for %.0fs "
                     "(CR_CODEX_INTERRUPT_AFTER_SEC=%d); interrupting it before codex "
                     "gets there on its own"
                     % (human_tokens(self.trigger_limit() or 0),
                        now - self.threshold_crossed_at, int(after_sec)))
        return ("interrupt", "context is nearly full; interrupting the turn to fold it up")

    def note_growth(self, paths, now):
        """The transcript we follow gained bytes, so the session is mid-turn.

        Idle is measured here rather than on the screen (see `_blocked_by_human`)
        and by bytes rather than by the rows we happen to parse: the gap between
        a tool call and its result can be minutes long, and the file grows across
        it while assistant rows do not. Subagents write into this same file,
        which is right — their work is the session's work.
        """
        if self.context_path and self.context_path in paths:
            self.context_grew_at = now

    def on_resume_echo(self, now):
        """claude wrote the resume phrase into the transcript: it was submitted."""
        if self.rstate != RESUME_SENT:
            return False
        self.log("the resume phrase was accepted")
        self.resume_echoed = True
        return True

    def on_handoff_echo(self, path, now):
        """claude wrote the fold phrase into a transcript: it was submitted.

        The nonce in it is unique machine-wide, so this is also the one
        completely certain proof of which transcript is really ours — worth
        acting on even when it turns up somewhere other than `context_path`,
        which is exactly why `main()` checks it ahead of the ordinary
        current-transcript filter every other echo kind is held to.
        """
        if self.rstate != HANDOFF_SENT:
            return False
        self.log("the handoff phrase was accepted")
        self.handoff_echoed = True
        if path and path != self.context_path:
            self.bind_transcript(path, "our handoff phrase was echoed there", now)
        return True

    def on_handoff_queued(self, now):
        """claude queued the fold phrase behind a turn still running.

        Not yet proof it was delivered -- a queued message can still be pulled
        back out -- but proof enough that retyping it would only stack a second
        phrase, with a second marker, behind the first.
        """
        if self.rstate != HANDOFF_SENT or self.handoff_queued:
            return False
        self.log("the handoff phrase is queued behind the running turn")
        self.handoff_queued = True
        return True

    # -- what the badge shows ----------------------------------------------- #
    def badge_context(self):
        """The context percentage worth putting on screen, or None.

        Only near the threshold. A number sitting at 12% all day is noise, and
        the single question the corner can usefully answer about this feature is
        whether the threshold you picked is anywhere near sane.
        """
        if not self.context_enabled or not self.context_limit:
            return None
        if self.context_tokens is None:
            return None
        if self.context_tokens < self.context_limit * self.BADGE_NEAR:
            return None
        return self.context_pct()

    def badge_warn(self, now=None):
        """The one thing the corner has to say while nothing is happening.

        In priority order (T14), each ranked above the next because it is
        further along the road to a session nobody is watching any more:

          1. `unfold failed` -- the debt T08 leaves behind when the resume
             phrase never once reached the session. Outranks everything: no
             retry is coming, and only a person can do anything about it.
          2. `unfold?` -- CLEARED has no timeout of its own (`_wait_for_unfold_
             gate`), so a gate held shut past `UNFOLD_STUCK` looks the same on
             screen as an ordinary few-second wait unless this says otherwise.
          3. `restart off` -- `context_off`, permanent for the rest of the
             session (T13's frequency guard included, via the same flag). A
             disarmed trigger is silent by construction, which looks exactly
             like an armed one quietly protecting you; this is the difference.
          4. `window?` -- the trigger is armed but nothing has said how big the
             window is yet (only ever true before a session's first turn).
          5. `~est` -- the window came from a guess (T19), not a firm answer,
             for as long as nothing nearer the threshold already says so:
             `badge_context()` covers that moment with its own "~" prefix, and
             this only speaks when that one has nothing to show.
        """
        if self.rstate == UNFOLD_FAILED:
            return "unfold failed"
        if (self.rstate == CLEARED and now is not None
                and now - self._cleared_at > self.UNFOLD_STUCK):
            return "unfold?"
        if self.context_off:
            return "restart off"
        if self._needs_window() and self.context_model:
            return "window?"
        if self.context_estimated and self.context_enabled and self.badge_context() is None:
            return "~est"
        return None

    def inject_note(self):
        """The one line the human sees when something is typed for them."""
        return {
            HANDOFF_SENT: "context is filling up; asking for a handoff",
            CLEAR_SENT: "handoff verified; clearing the context",
            RESUME_SENT: "context cleared; unfolding the handoff",
            CANCEL_PENDING: "restart cancelled; asking the session to carry on",
        }.get(self.rstate,
              "asking the session to carry on" if self.incident == "stall"
              else "limit lifted; resuming session")

    # -- the window --------------------------------------------------------- #
    def _resolve_window(self):
        """How large the context window is, in the order the answers are trusted
        (T19):

          1. CR_CONTEXT_WINDOW — said outright, so nothing below is asked.
          2. reported by the agent itself — codex states one in every row of
             accounting; claude's own statusline does the same, relayed by
             the `--cr-statusline` proxy (T20) into `on_status`.
          3. claude's own environment (CLAUDE_CODE_MAX_CONTEXT_TOKENS /
             CLAUDE_CODE_DISABLE_1M_CONTEXT) — narrows what its native window
             would otherwise be; meaningless for codex, which states its own.
          4. the profile table (T18) — ground truth for a model it lists,
             including a same-family point release (`model_window`'s own
             fallback) and codex's exact-slug entries in MODEL_PROFILES.
          5. a slug learned off the network (`self.learned`, via WindowLookup)
             — stands in for a model the shipped table does not carry.
          6. an estimate (`estimate_window`, T19) — a slug NOTHING above
             answered for is guessed rather than left to disarm the trigger:
             an unfamiliar slug is almost always evidence of a new, large
             model, and a session with no restart armed at all is a worse
             failure than a guess. `window_unknown` (below) keeps asking the
             network for something firmer while the guess stands, and
             `on_context` corrects it both ways as the session proves it
             right or wrong.
        """
        forced = parse_tokens(self.cfg["context_window"])
        slug = model_slug(self.context_model)
        if self.codex:
            prof = MODEL_PROFILES.get("codex", {}).get(slug)
            native = prof.window if prof else None
        else:
            native = model_window(self.context_model)
        native = native or self.learned.get(slug)
        self.context_estimated = False
        if forced:
            self.context_window, why = forced, "CR_CONTEXT_WINDOW"
        elif self.context_window_hint:
            self.context_window, why = (self.context_window_hint,
                                        self.context_window_hint_source)
        elif not self.codex and self.cfg["context_env_max"] > 0:
            self.context_window, why = (int(self.cfg["context_env_max"]),
                                        "CLAUDE_CODE_MAX_CONTEXT_TOKENS")
        elif native is not None:
            if not self.codex and self.cfg["context_no_1m"]:
                # We started claude ourselves, so its environment is ours to
                # read: the long window is switched off for this session
                # whatever the model is capable of.
                self.context_window, why = (min(native, SMALL_WINDOW),
                                            "CLAUDE_CODE_DISABLE_1M_CONTEXT")
            else:
                self.context_window, why = native, "the model"
        else:
            self.context_window, why = estimate_window(
                "codex" if self.codex else "claude", self.context_model)
            self.context_estimated = True
        self._window_bumped = False
        self._recompute_limit()
        # Nothing above codex's own rollout ever names its window, so a lookup
        # here would only ever ask about a slug it cannot answer.
        self.window_unknown = (self.context_model if self.context_estimated
                               and not self.codex
                               and self.cfg["context_tokens"] <= 0
                               and self.cfg.get("context_restart_on")
                               and not self.context_off else None)
        if not self.context_enabled:
            return
        if self._needs_window():
            self.log("%s: %s, so the window is unknown and the restart stays "
                     "disarmed until something says otherwise"
                     % (self.context_model or "this session", why))
        else:
            marker = " (estimated)" if self.context_estimated else ""
            self.log("%s: a %s context window (%s)%s, restarting at %s"
                     % (self.context_model or "this session",
                        human_tokens(self.context_window), why, marker,
                        human_tokens(self.context_limit)))

    def _needs_window(self):
        """Is a window the missing piece? An absolute threshold never needs one.

        True today only for codex before its first turn has stated one: every
        other path through `_resolve_window` always lands on SOME number now,
        even if only a T19 estimate — which is the point of that fallback.
        """
        return (self.context_window is None and self.cfg["context_tokens"] <= 0
                and self.cfg.get("context_restart_on") and not self.context_off)

    def on_window_learned(self, model, window, source):
        """Somebody answered the question `window_unknown` was asking.

        Late and for the wrong model is the normal case — a lookup takes seconds
        and a session can change models inside them — so the answer is filed
        under its own slug and only resolves the window if it is the slug we are
        actually on.
        """
        slug = model_slug(model)
        if not slug or not window:
            return False
        self.learned[slug] = int(window)
        if slug != model_slug(self.context_model):
            return False
        self._resolve_window()
        return True

    def _model_tokens_override(self):
        """CR_CLAUDE_TOKENS_<SLUG>/CR_CODEX_TOKENS_<SLUG> for the current model.

        The finest-grained knob there is: one specific model, one absolute
        number, read fresh from the environment rather than carried in `cfg`,
        because which model is on the other end of the session is not known
        until its first turn — everything cfg holds is fixed before that.
        """
        slug = model_slug(self.context_model)
        if not slug:
            return None
        name = "CR_%s_TOKENS_%s" % ("CODEX" if self.codex else "CLAUDE",
                                    model_env_slug(slug))
        return parse_tokens(os.environ.get(name))

    def _recompute_limit(self):
        override = self._model_tokens_override()
        agent = "codex" if self.codex else "claude"
        if override:
            self.context_limit = override
        elif self.cfg["context_tokens"] > 0:
            self.context_limit = int(self.cfg["context_tokens"])
        elif self.cfg.get("context_restart_on") and self.context_window:
            # Stages 3 and 4 of the order on model_restart_at: the model's own
            # row when this session's window is the one it was written for,
            # and the same rule applied to whatever window it actually has
            # when it is not.
            self.context_limit = (
                model_restart_at(agent, model_slug(self.context_model),
                                 self.context_window)
                or default_restart_at(agent, self.context_window))
        else:
            self.context_limit = None

    # -- the machine -------------------------------------------------------- #
    def _tick_restart(self, now):
        prev, self._restart_tick = self._restart_tick, now
        if self.rstate is None:
            if self.context_off:
                # The trigger itself never fires again this session, but a debt
                # that sits silent for hours is exactly what T14 exists to
                # prevent -- repeat it until a key proves someone finally saw it.
                return (None if self._context_off_acked else
                        self._renotify(now, "context restart is switched off for "
                                            "this session (%s)" % self._off_reason))
            return self._maybe_restart(now)
        if prev is not None:
            # The clock only runs on ticks where the restart could actually have
            # made progress; `tick` blanks it for the duration of a limit.
            self.restart_left -= max(0.0, now - prev)
        if self._rearm:
            self._rearm = False
            return self._after_limit(now)[1]
        if self.rstate == HANDOFF_SENT:
            return self._check_handoff(now)
        if self.rstate == HANDOFF_OK:
            if self.restart_left <= 0:
                return self._abort_restart("%s took longer than %.0fs"
                                           % (self.rstate, self.cfg["handoff_timeout"]), now)
            return self._send_clear(now)
        if self.rstate == CLEAR_SENT:
            if self.restart_left <= 0:
                return self._abort_restart("%s took longer than %.0fs"
                                           % (self.rstate, self.cfg["handoff_timeout"]), now)
            return self._check_clear(now)
        if self.rstate == CLEARED:
            return self._wait_for_unfold_gate(now)
        if self.rstate == RESUME_SENT:
            return self._check_resume(now)
        if self.rstate == UNFOLD_FAILED:
            return self._renotify(now,
                "read `%s` yourself: the resume phrase never reached the session"
                % self.cfg["handoff_file"])
        if self.rstate == CANCEL_PENDING:
            return self._send_cancel(now)
        return None

    def _held(self, now, session_gate=True):
        """Is a person or a running turn holding the next step back?

        `session_gate` is False for `CLEARED`: once `/clear` is confirmed, whatever
        "busy" meant on the transcript that came before it says nothing about the
        session now in front of it (T04) -- only a person still counts there.

        Logged only when the answer changes: this is asked several times a second
        and a line each time would bury everything else in the log.
        """
        why = self._blocked_by_human(now) or (session_gate and self._session_busy(now))
        why = why or None                # False -> None: a plain "nothing" for the log
        if why != self._gate_note:
            self._gate_note = why
            if why:
                self.log("restart step held: %s" % why)
        if why:
            self.rwake = now + self.GATE_RETRY
        return why

    def _renotify(self, now, text):
        """Say a standing debt again if it has been `CR_NOTIFY_REPEAT_SEC` since
        the last time -- a debt that can sit for hours must not go quiet after
        the first mention just because nothing else changed (T14: covers a
        stuck unfold, a failed one, and the trigger switched off for good)."""
        if now - self._debt_notify_at < self.cfg["notify_repeat"]:
            return None
        self._debt_notify_at = now
        return ("notify", text)

    def _wait_for_unfold_gate(self, now):
        """CLEARED has no timeout: the context is already gone, so the only thing
        left to do is unfold it, and the only reason not to yet is a person."""
        if now < self.rwake:
            return None
        # A clear done by hand is followed by whatever the person does next, so
        # a session writing to its fresh transcript is theirs to finish first.
        why = self._held(now, session_gate=self._cleared_by_hand)
        if why:
            return self._renotify(now, "context cleared; unfold is waiting for: %s" % why)
        return self._send_resume(now)

    def _session_busy(self, now):
        if self.turn_open:
            # Said outright, and it outlasts the byte count: a codex root waiting
            # on its agents can go minutes without writing anything.
            return "a turn is still running"
        if self.context_grew_at and now - self.context_grew_at < self.cfg["root_idle"]:
            return "the session is still writing to its transcript"
        if self.agent_status == "busy" and self.agent_status_at and now - self.agent_status_at < 60:
            return "the session reports busy"
        return None

    def _restart_thrashing(self, now):
        """More restarts than `CR_CONTEXT_MAX_PER_HOUR` inside the last hour (T13).

        A threshold that cannot hold for longer than 30-40 minutes is not
        protecting the session any more, it is grinding it: the handoff fully
        reprinted and the details it can't carry lost, again and again. `0`
        means no cap, the same convention `context_max_cycles` uses.
        """
        cap = self.cfg["context_max_per_hour"]
        if cap <= 0:
            return False
        cutoff = now - 3600.0
        self._restart_times = [t for t in self._restart_times if t > cutoff]
        return len(self._restart_times) >= cap

    def _maybe_restart(self, now):
        limit = self.trigger_limit() if self.context_enabled else None
        if limit is None:
            return None
        if self.context_tokens is None or self.context_tokens < limit:
            return None
        if self.context_ambiguous:
            return None
        if now < self.cooldown_until:
            return None
        cap = self.cfg["context_max_cycles"]
        if cap and self.cycles >= cap:
            return None
        interrupt = self._maybe_interrupt(now)
        if interrupt:
            return interrupt
        if self._held(now):
            return None
        if self._restart_thrashing(now):
            return self._abort_restart(
                "more than %d restarts in the last hour" % self.cfg["context_max_per_hour"],
                now, permanent=True)
        self._restart_times.append(now)
        self.cycles += 1
        self.handoff_tries = 0
        self.resume_tries = 0
        self.handoff_echo_retries = 0
        # The reading we acted on is the one the restart has to be judged
        # against. Taking it at /clear time instead would mean trusting whatever
        # the last row said by then — and a project directory can hold more than
        # one live transcript, so "by then" is not always this session.
        self.context_before = self.context_tokens
        self.log("context is %s of a %s window (%.0f%%, threshold %s); folding up"
                 % (human_tokens(self.context_tokens), human_tokens(self.context_window),
                    self.context_pct() or 0.0, human_tokens(limit)))
        return self._send_handoff(now, fresh=True)

    def _send_handoff(self, now, fresh):
        """Ask for the handoff, under a marker only this attempt can satisfy.

        The nonce is the whole point of the marker. Without one, the marker left
        in the file by the previous attempt — or by the previous restart, hours
        ago — reads as this one's, which turns "the write finished" into "a write
        finished once", and those are not the same claim at all.
        """
        if fresh:
            self.handoff_tries += 1
        self.nonce = "%s-%s" % (self.cfg["handoff_marker"], os.urandom(4).hex())
        self.rstate = HANDOFF_SENT
        self.handoff_sent_at = now
        self.restart_left = self.cfg["handoff_timeout"]
        self.rwake = now
        self.last_stop_reason = None      # only the coming turn's ending counts
        self.end_turn_seen_at = 0.0
        self.handoff_verified_at = None   # a fresh nonce means a fresh proof, too
        self._handoff_verified_st = None
        text = (self.cfg["handoff_msg"]
                .replace("{file}", self.cfg["handoff_file"])
                .replace("{marker}", self.nonce))
        # A fresh nonce means a fresh phrase, so the proof that THIS one reached
        # the session starts over too -- the echo (if any) still in the
        # transcript belongs to whichever attempt came before.
        self.handoff_text = text
        self.handoff_echoed = False
        self.handoff_queued = False
        self.log("asking for a handoff into %s (attempt %d/%d, marker %s)"
                 % (self.cfg["handoff_file"], self.handoff_tries,
                    self.cfg["handoff_attempts"], self.nonce))
        return ("inject", text, self._take_dismiss())

    def _send_clear(self, now):
        if now < self.rwake or self._held(now):
            return None
        # The one irreversible step, and the only route to it is a handoff that
        # satisfied all four layers below. It is not, on its own, proof that
        # Claude Code actually acted on it -- CLEAR_SENT is what waits for that.
        self.context_before = max(self.context_before or 0, self.context_tokens or 0)
        self.rstate = CLEAR_SENT
        self.restart_left = self.cfg["handoff_timeout"]
        self.clear_sent_at = now
        self.clear_retried = False
        self._clear_from_path = self.context_path
        self.log("handoff verified; clearing the context with %s" % self.cfg["clear_cmd"])
        return ("inject", self.cfg["clear_cmd"], self._take_dismiss())

    def _check_clear(self, now):
        """Did `/clear` actually land? Unfolding into a session that never saw it
        would type the resume phrase straight into the still-full one (T08).

        Two signals count as proof, either one: the transcript identity moved on
        (claude rebinding to a new sessionId/file, codex to a new rollout -- the
        same `bind_transcript` call either way), or the screen has simply gone
        quiet for `clear_settle` -- the only signal there is on an agent that does
        not rebind. Absent either, one reprint is cheap insurance against a
        keystroke that never reached the input box at all.
        """
        if self.context_path != self._clear_from_path:
            return self._enter_cleared("a new session replaced it", now)
        if now - max(self.last_output_at, self.clear_sent_at) >= self.cfg["clear_settle"]:
            return self._enter_cleared("the screen went quiet", now)
        if (not self.clear_retried and now - self.clear_sent_at >= self.cfg["verify"]
                and not self._held(now)):
            self.clear_retried = True
            self.clear_sent_at = now
            self.log("the clear command left no trace; sending it again")
            return ("inject", self.cfg["clear_cmd"], self._take_dismiss())
        return None

    def _enter_cleared(self, why, now):
        self.rstate = CLEARED
        self.rwake = now + self.cfg["step_gap"]
        self._debt_notify_at = now
        self._cleared_at = now
        self.log("the clear took hold (%s); unfolding once the gate opens" % why)
        return None

    def _send_resume(self, now):
        self.rstate = RESUME_SENT
        self.restart_left = self.cfg["handoff_timeout"]
        # Growing gaps between reprints (60s, 120, 240, ...): a phrase that never
        # reached the input box once is not made more likely to by asking again
        # every minute for however long CR_RESUME_ATTEMPTS allows.
        self.rwake = now + self.cfg["verify"] * (2 ** self.resume_tries)
        self.resume_echoed = False
        self.log("unfolding from %s" % self.cfg["handoff_file"])
        return ("inject", self.resume_text, self._take_dismiss())

    def _record_fold_cost(self, now):
        """What this fold actually cost -- tokens when the handoff was accepted
        minus tokens when it was asked for -- logged and accumulated in
        codex_folds_file (T22), so CR_CODEX_RESERVE_TOKENS can be judged
        against real folds rather than the one incident it was picked from.
        """
        cost = max(0, (self.context_tokens or 0) - (self.context_before or 0))
        reserve = int(self.cfg.get("codex_reserve", 0) or 0)
        self.log("the fold cost %s tokens (reserve %s)"
                 % (human_tokens(cost), human_tokens(reserve)))
        path = self._folds_path()
        prior_max = _fold_history_max(path)
        _append_fold_record(path, "codex", os.getcwd(), cost, now)
        self._observed_fold_cost = max(prior_max, cost)
        if reserve and cost > 0.8 * reserve:
            self.log("the fold cost more than 80%% of the %s reserve; "
                     "CR_CODEX_RESERVE_TOKENS could stand to be raised"
                     % human_tokens(reserve))
        if (self.cfg.get("codex_reserve_adapt")
                and int(1.25 * self._observed_fold_cost) > reserve):
            self.log("CR_CODEX_RESERVE_ADAPT: the effective reserve is now %s "
                     "(1.25x the largest fold cost seen)"
                     % human_tokens(self._effective_codex_reserve()))

    # -- the five layers ------------------------------------------------------ #
    def _check_handoff(self, now):
        """`/clear` goes out only when all five of these agree.

        The model saying it is done is not one of them. It is a report about its
        own intent, and the failure this guards against is precisely the one
        where that intent was sincere and the file is still half a page.

        That the phrase reached this session (T06) is proven either by its echo
        or by the file itself ending with this attempt's marker: the marker is
        fresh per attempt and typed only into this terminal, so no neighbour's
        file can carry it. Waiting on the echo alone once hung a restart for
        good -- Claude Code wrote a phrase queued mid-turn as a row the echo
        check did not know, the file came out perfect, and `/clear` never went.
        """
        busy = self._session_busy(now)
        st = self.probe(self.handoff_path)
        if not self.handoff_echoed and self._carries_nonce(st):
            # The marker is fresh for this attempt and was only ever typed into
            # this terminal, so a file that ends with it is the model's answer to
            # it: the phrase got through even if its echo was never recognised
            # (Claude Code's rows for a message queued mid-turn look nothing like
            # the ordinary user row). A neighbour's file cannot carry it.
            self.handoff_echoed = True
            self.log("the handoff file ends with %s: the phrase reached the session"
                     % self.nonce)
        fault = self._handoff_fault(st)
        if fault is None:
            # Latch here rather than wait for `busy` to clear: background
            # activity (an agent's notification, notify_idle, a hook) can start
            # a new turn once the fold's end_turn already landed, and that turn
            # would otherwise keep this file "not yet accepted" until the
            # handoff_timeout aborts a restart that already succeeded (T12).
            # Quiet is still required before `/clear` actually goes out --
            # `_send_clear` enforces that on its own.
            self.rstate = HANDOFF_OK
            self.handoff_verified_at = now
            self._handoff_verified_st = st
            self.restart_left = self.cfg["handoff_timeout"]
            self.rwake = now
            self.log("handoff accepted: %d bytes ending in %s, turn closed with end_turn"
                     % (st["size"], self.nonce))
            if self.codex:
                self._record_fold_cost(now)
            return self._send_clear(now)
        if self.restart_left <= 0:
            return self._abort_restart(
                "no usable handoff within %.0fs: %s"
                % (self.cfg["handoff_timeout"], fault or busy), now)
        if (not self.handoff_echoed and not self.handoff_queued and st is None
                and now - self.handoff_sent_at >= self.cfg["verify"]):
            # Nothing at all came of it: no echo, no file. A popup that ate the
            # keystrokes and a lost write look identical from here, and both are
            # fixed the same way -- type it again, rather than sit out the whole
            # handoff timeout on a phrase that never reached the input box.
            if self.handoff_echo_retries >= self.HANDOFF_ECHO_RETRIES:
                return self._abort_restart(
                    "the handoff phrase never reached the session", now)
            self.handoff_echo_retries += 1
            self.log("the handoff phrase left no trace; sending it again (%d/%d)"
                     % (self.handoff_echo_retries, self.HANDOFF_ECHO_RETRIES))
            return self._send_handoff(now, fresh=False)
        if busy:
            # Still writing. The answer is to wait, and the timeout above is
            # the bound -- as it is for every other wait in this step.
            return None
        if self.last_stop_reason in (None, "tool_use", "pause_turn"):
            return None                  # the turn has not landed yet
        # Quiet, with the turn closed: this is as good as the handoff is ever
        # going to get, and it is not good enough.
        self.log("handoff attempt %d/%d failed: %s"
                 % (self.handoff_tries, self.cfg["handoff_attempts"], fault))
        if self.handoff_tries >= self.cfg["handoff_attempts"]:
            return self._abort_restart(fault, now)
        return self._send_handoff(now, fresh=True)

    def _carries_nonce(self, st):
        """The file was written after this attempt's request and ends with its marker."""
        return (st is not None and bool(self.nonce)
                and st["mtime"] >= self.handoff_sent_at - self.MTIME_SLACK
                and re.search(r"(?:\A|\n)[ \t]*%s\Z" % re.escape(self.nonce),
                              st["tail"].rstrip()) is not None)

    def _handoff_fault(self, st):
        """Which layer is not satisfied, in words, or None when all of them are.

        Ordered so that the cheap physical facts are reported before the ones
        that need the transcript — the sentence this returns is what the log says
        an abort was about, and "the file was never written" and "the turn was
        cut off at max_tokens" call for very different reading.
        """
        if st is None:
            return "the handoff file was never written"
        if st["mtime"] < self.handoff_sent_at - self.MTIME_SLACK:
            return "the handoff file is older than the request for it"
        if st["size"] < self.cfg["handoff_min_bytes"]:
            return ("the handoff file is %d bytes, under the %d-byte floor"
                    % (st["size"], self.cfg["handoff_min_bytes"]))
        if not re.search(r"(?:\A|\n)[ \t]*%s\Z" % re.escape(self.nonce or "\0"),
                         st["tail"].rstrip()):
            return "the handoff file does not end with %s" % self.nonce
        if self.end_turn_seen_at < self.handoff_sent_at:
            # A fact from the runtime, not from the model: max_tokens is an
            # answer cut off by length and refusal is one that never started,
            # and a marker that somehow survived either proves nothing. Judged
            # by whether end_turn was EVER seen for this attempt, not by
            # `last_stop_reason` alone -- a later tool_use from background
            # activity must not undo an end_turn this attempt already had (T12).
            return "the turn ended with stop_reason=%s" % self.last_stop_reason
        return None

    def _recheck_handoff_latch(self, now):
        """A latch is a claim about a snapshot, not a promise about the
        future: the file can still be overwritten while the busy gate holds
        `/clear` back. Skip the re-check whenever the snapshot has not
        actually moved, so ordinary transcript growth is not a stat() and a
        regex on every row.
        """
        st = self.probe(self.handoff_path)
        if st == self._handoff_verified_st:
            return
        if self._handoff_fault(st) is None:
            self._handoff_verified_st = st
            return
        self.rstate = HANDOFF_SENT
        self.handoff_verified_at = None
        self._handoff_verified_st = None
        self.log("the handoff file changed after it was accepted")

    # -- did it work -------------------------------------------------------- #
    def _context_fell(self):
        """Did the context actually go away? The only evidence `/clear` landed."""
        if not self.context_before or self.context_tokens is None:
            return False
        if self.context_tokens <= 0:
            return False              # a zero reading is a synthetic/empty row, not a /clear
        return self.context_tokens <= self.context_before * self.RESTART_DROP

    def _min_headroom(self):
        """The floor `_guard_headroom` measures headroom against.

        Capped to 30% of a window at or under 200k (T13): asking a small
        window for the same 80k a 1M one can spare is asking for something it
        never had to give, and would leave the guard firing every restart.
        """
        floor = self.cfg["context_min_headroom"]
        if self.context_window and self.context_window <= 200000:
            return min(floor, self.context_window * 0.3)
        return floor

    def _compaction_line(self):
        """Where this model's own profile says the agent starts compacting on
        its own, net of the same live reserve `model_restart_at` ceilings
        restart_at under -- or None when no profile matches the window we are
        actually on, the same case `model_restart_at` refuses to guess for."""
        agent = "codex" if self.codex else "claude"
        prof = _matching_profile(agent, model_slug(self.context_model), self.context_window)
        if not prof or not prof.compact_at:
            return None
        return prof.compact_at - _compaction_reserve(agent)

    def _guard_headroom(self):
        """A session fresh out of a restart is not actually at zero (T13): the
        system prompt, CLAUDE.md, MCP tool defs and the resume read already
        cost real tokens, and against a small window that baseline alone can
        eat most of a percentage threshold's own headroom -- so the restart it
        just bought fires again in 30-40 minutes instead of hours.

        Raises `context_limit` when the model's own compaction point leaves
        real room to do that, and says so quietly (a log line); when it does
        not, says so out loud instead -- disarming would only trade one
        failure mode for another (never restarting again), so the trigger
        stays as it is and a person is told to widen it by hand.
        """
        if not self.context_window or not self.context_limit or self.context_tokens is None:
            return None
        baseline = self.context_tokens
        headroom = self.context_limit - baseline
        if headroom >= self._min_headroom():
            return None
        ceiling = self._compaction_line()
        raised = min(baseline + self.cfg["context_min_headroom"], ceiling) if ceiling else None
        if raised and raised > self.context_limit:
            self.log("after the restart the context already sits at %s of a %s threshold; "
                     "raising the threshold to %s for this session"
                     % (human_tokens(baseline), human_tokens(self.context_limit),
                        human_tokens(raised)))
            self.context_limit = raised
            return None
        return ("threshold leaves %s of working room; consider a larger CR_CONTEXT_TOKENS "
                "or a bigger window" % human_tokens(headroom))

    def _check_resume(self, now):
        if self._context_fell():
            note = ("context restarted: %s down to %s"
                    % (human_tokens(self.context_before), human_tokens(self.context_tokens)))
            self.log(note)
            warn = self._guard_headroom()
            self._end_restart(now)
            return ("notify", "%s; %s" % (note, warn) if warn else note)
        if now < self.rwake:
            return None
        if self._held(now):
            return None
        if not self.resume_echoed:
            if self.resume_tries < self.cfg["resume_attempts"]:
                self.resume_tries += 1
                self.log("the resume phrase left no trace; sending it again (%d/%d)"
                         % (self.resume_tries, self.cfg["resume_attempts"]))
                return self._send_resume(now)
            # It reached none of the CR_RESUME_ATTEMPTS tries -- not one bad
            # keystroke, but every one of them. That is not the world's problem
            # to retry its way out of, but it is not the one-way failure a bad
            # `/clear` is either: the context really is gone, so the debt stays
            # owed and visible (badge, notify) until a person does something
            # about it, rather than the trigger just going quiet for good.
            return self._enter_unfold_failed(now)
        # The phrase landed and the context is still the size it was — which
        # means the clear did nothing. Retrying would type into a session that
        # is as full as it was, so this one does stop for good.
        why = ("the context did not fall after %s (still %s)"
               % (self.cfg["clear_cmd"], human_tokens(self.context_tokens)))
        return self._abort_restart(why, now, permanent=True)

    def _enter_unfold_failed(self, now):
        self.rstate = UNFOLD_FAILED
        self.context_off = True
        self._off_reason = "the resume phrase never reached the session"
        self._context_off_acked = False
        self._debt_notify_at = now
        self.log("unfold failed: the resume phrase never reached the session in "
                 "%d attempts — switched off for this session until a key is pressed"
                 % self.cfg["resume_attempts"])
        return ("notify",
                "read `%s` yourself: the resume phrase never reached the session"
                % self.cfg["handoff_file"])

    # -- a limit in the middle of it ---------------------------------------- #
    def _after_limit(self, now):
        """(handled, action) for a wait that ended while a restart was in flight.

        `handled` False means the restart is over and the ordinary `continue` is
        the right thing to type after all.
        """
        self.log("the wait ended during a restart (%s)" % self.rstate)
        if self.rstate == HANDOFF_SENT:
            # A limit is the world failing, not the model failing to fold, so it
            # does not spend an attempt. The phrase rewrites the file from
            # scratch, which makes re-sending it whole both correct and cheaper
            # than reasoning about how much of it got written.
            return True, self._send_handoff(now, fresh=False)
        if self.rstate == RESUME_SENT and (self.resume_echoed or self._context_fell()):
            # The resume landed and was cut off partway through. That is the one
            # position in this machine where "continue" is exactly the right word.
            self.log("the resume phrase had already landed; the ordinary retry fits")
            self._end_restart(now)
            return False, None
        self.rwake = 0.0
        return True, self._tick_restart(now)

    # -- endings ------------------------------------------------------------ #
    def _end_restart(self, now):
        self.rstate = None
        self.nonce = None
        self.rwake = 0.0
        self.resume_echoed = False
        self.handoff_text = None
        self.handoff_echoed = False
        self.handoff_queued = False
        self.context_before = 0
        self._restart_tick = None
        self._rearm = False
        self._gate_note = None
        self._cancel_sent = False
        self._cleared_by_hand = False
        self.cooldown_until = now + self.cfg["context_cooldown"]

    def _abort_restart(self, why, now, permanent=False):
        """Stop, name the layer that failed, and leave the session exactly as it is.

        Nothing is cleared, nothing is retyped, no state is unwound. The worst
        outcome reachable from here is a session that has to fall back on Claude
        Code's own compaction — which is a great deal better than one whose
        history was thrown away on the strength of a handoff that was never
        written.

        Except when the fold itself already landed (T09): once the phrase was
        echoed back at HANDOFF_SENT/HANDOFF_OK, the model was told to wrap up
        and start nothing new, and "leave the session exactly as it is" means
        leaving it on that instruction — fine for a person at the keyboard,
        useless for an autonomous one sitting on an empty input until someone
        shows up. CANCEL_PENDING says the ask is off instead, once the same
        gates that hold every other step back let it through.
        """
        at = self.rstate
        if not permanent and at in (HANDOFF_SENT, HANDOFF_OK) and self.handoff_echoed:
            self.log("restart aborted at %s: %s" % (at, why))
            self.rstate = CANCEL_PENDING
            self._cancel_sent = False
            self.rwake = now
            return None
        self._end_restart(now)
        if permanent:
            self.context_off = True
            self._off_reason = why
            self._context_off_acked = False
            self._debt_notify_at = now
            self.log("restart aborted at %s: %s — not attempting another this session"
                     % (at, why))
            return ("notify", "context restart aborted (%s); switched off for this session"
                    % why)
        self.log("restart aborted at %s: %s" % (at, why))
        return ("notify", "context restart aborted: %s" % why)

    def _send_cancel(self, now):
        """CANCEL_PENDING has no timeout of its own -- the restart already
        failed, so there is nothing left to retry, only one phrase left to
        send once nothing blocks the keystroke. The state lingers one tick
        past that so `inject_note` (read right after this return, in `main`)
        still finds it in place, rather than a restart that already ended.
        """
        if self._cancel_sent:
            self._end_restart(now)
            return None
        if now < self.rwake:
            return None
        if self._held(now):
            return None
        self._cancel_sent = True
        dismiss = self._take_dismiss()
        self.log("restart cancelled; asking the session to carry on")
        return ("inject", self.cancel_text, dismiss)


# --------------------------------------------------------------------------- #
# status badge — a dim mark in a corner, the wrapper's only visible pixel
# --------------------------------------------------------------------------- #
# Claude Code owns the screen and repaints it several times a second, so nothing
# can be reserved from it: no scroll region (the TUI does not know about one and
# would lay out against the wrong height), no extra line (it would scroll away).
# What is left is to paint OVER the finished frame and let it be overwritten —
# the badge is re-drawn a moment after every repaint, which costs ~40 bytes and
# never touches the byte stream Claude reads.
#
# Two rules keep it from corrupting the render:
#   * only in the quiet gap after Claude stopped writing (QUIET), and never while
#     a control sequence of Claude's is still half-received;
#   * cursor saved and restored around the write, and the rightmost column left
#     empty so that a character in the last cell can never trigger a scroll.
def human_left(secs):
    """Coarse on purpose: a countdown that ticks every second is a distraction."""
    secs = max(0, int(secs + 0.5))
    if secs >= 3600:
        return "%dh%02dm" % (secs // 3600, (secs % 3600) // 60)
    if secs >= 60:
        return "%dm" % (secs // 60)
    return "%ds" % secs


BADGE_POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left")


# T27 shipped a copy of this arithmetic in AgentOverlay._paint_row that
# computed the column from the NEW (narrower) text while padding out to the
# OLD (wider) one -- a shrinking label could then reach past the protected
# last column and scroll the screen. Badge never had that bug, because it
# always derived the column from the padded string. Routing both painters
# through the same three functions (T28) makes that class of bug impossible
# to reintroduce, rather than merely absent today.
def edge_column(edge, width, anchor):
    """1-based column for a `width`-wide field anchored at `edge`: a "right"
    anchor ends just before `edge`, a "left"/"start" anchor begins at it.
    `width` must already be the FINAL width (maxed against whatever is
    currently on screen) -- see the note above."""
    return edge - width if anchor == "right" else edge


def pad_to(text, width, anchor):
    """Pad `text` out to `width`, on the side away from its anchor, so a
    shorter frame covers every cell the previous, wider one used."""
    if len(text) >= width:
        return text
    fill = " " * (width - len(text))
    return fill + text if anchor == "right" else text + fill


def annotation_bytes(row, col, text, sgr):
    """DECSC -> CUP -> SGR -> text -> SGR reset -> DECRC: save the cursor and
    colour, paint, restore both. Never emits a newline, so it can never
    scroll the screen or leave a stray colour behind for Claude's own next
    write."""
    return ("\x1b7\x1b[%d;%dH\x1b[%sm%s\x1b[0m\x1b8" % (row, col, sgr, text)).encode()


def write_annotation(fd, row, col, text, sgr):
    return write_all(fd, annotation_bytes(row, col, text, sgr))


class OccupiedRows:
    """Which rows are already spoken for this frame, so a painter can avoid
    them without being told by name which one belongs to somebody else (T28).
    Badge registers its own row before AgentOverlay paints; a third
    participant, if one ever exists, would do the same and need no change
    here or in AgentOverlay."""
    def __init__(self):
        self._rows = set()

    def claim(self, row):
        if row is not None:
            self._rows.add(row)

    def __contains__(self, row):
        return row in self._rows


class Badge:
    MARK = "◆"
    MARK_ALT = "◇"
    PULSE = 1.6            # seconds per half-blink while waiting
    QUIET = 0.12           # output has to have stopped for this long
    STALE = 2.0            # ...but a screen that never goes quiet still gets painted
    MIN_INTERVAL = 0.15    # hard floor on repaint rate

    def __init__(self, cfg):
        self.enabled = bool(cfg["badge"])
        self.pos = cfg["badge_pos"] if cfg["badge_pos"] in BADGE_POSITIONS else "bottom-right"
        self.label = cfg["badge_label"]
        self.last_output = 0.0
        self.last_paint = -1e9
        self.quiet_since = 0.0   # when the never-arriving quiet gap was first waited on
        self.painted = None      # text currently on screen, None when nothing is
        self.painted_width = 0   # cells it took, so a shorter frame can cover them
        self.pending = True      # claude drew something since we last painted

    # -- what it says ------------------------------------------------------- #
    def frame(self, state, remaining, attempts, max_attempts, now, deferred=False,
              restart=None, context=None, warn=None, context_estimated=False):
        """(text, sgr) for a controller state. Pure, so the tests can drive it."""
        mark = self.MARK
        if state == WAITING:
            # A blink is the difference between "waiting" and "dead", and it is
            # the only motion the badge ever has.
            if int(now / self.PULSE) % 2:
                mark = self.MARK_ALT
            if deferred:
                # The reset came and went; what is left is a gate, and each
                # deferral re-arms the clock 15 seconds at a time. Showing that
                # countdown would read as "still waiting for the quota", which
                # is the one thing it no longer means.
                return ("%s %s held" % (mark, self.label), "2;35")
            return ("%s %s %s" % (mark, self.label, human_left(remaining)), "2;33")
        if state == VERIFY:
            return ("%s %s %d/%d" % (mark, self.label, attempts, max_attempts), "2;36")
        if state == DONE:
            return ("%s %s stopped" % (mark, self.label), "2;31")
        if restart == UNFOLD_FAILED:
            # Red and not blinking, like `warn`: nothing is being typed into the
            # session any more, and the debt sits there until a person acts.
            return ("%s %s %s" % (mark, self.label, RESTART_LABELS[restart]), "2;31")
        if warn:
            # Dim red and not blinking: something needs attention, and -- unlike
            # an active step -- nothing is being typed into the session over it.
            # Ranked ahead of the ordinary restart label below (T14): CLEARED's
            # gate stuck past badge_warn's own "unfold?" threshold is a stall,
            # not progress, and a blinking "cleared" over it would say otherwise.
            return ("%s %s %s" % (mark, self.label, warn), "2;31")
        if restart:
            # Minutes of typing into a live session, ending in a cleared one.
            # While that is happening it is the most important thing the corner
            # has to say, and it blinks for the same reason a wait does.
            if int(now / self.PULSE) % 2:
                mark = self.MARK_ALT
            return ("%s %s %s" % (mark, self.label,
                                  RESTART_LABELS.get(restart, restart)), "2;35")
        if context is not None:
            # Rounded, not truncated: codex's status line rounds, and a corner
            # saying 4% under a line saying 5% reads as two different sessions.
            pct = "%d%%" % int(context + 0.5)
            if context_estimated:
                # T19: the denominator is a guess, not a confirmed window --
                # worth saying on the one line anyone is actually watching.
                pct = "~" + pct
            return ("%s %s %s" % (mark, self.label, pct), "2;32")
        return ("%s %s" % (mark, self.label), "2")

    # -- when it says it ---------------------------------------------------- #
    def note_output(self, now):
        self.last_output = now
        self.pending = True

    def due(self, text, now, blocked=False):
        if not self.enabled or blocked:
            return False
        if now - self.last_paint < self.MIN_INTERVAL:
            return False
        if now - self.last_output < self.QUIET:
            # Claude is mid-frame; painting now could split it. Waiting for a gap
            # is only safe as long as a gap arrives: the agent roster animates
            # about ten times a second and never leaves one, which froze the badge
            # on whichever frame it drew first — a countdown that does not count,
            # which reads as a dead wrapper, the one thing the badge exists to
            # disprove. So the wait itself is bounded: after STALE we paint into
            # the traffic and let claude's next repaint tidy up after us.
            if not self.quiet_since:
                self.quiet_since = now
            if now - self.quiet_since < self.STALE:
                return False
        else:
            self.quiet_since = 0.0
        return self.pending or text != self.painted

    # -- where it goes ------------------------------------------------------ #
    def row(self, rows):
        """The row this badge sits on, top or bottom -- independent of column
        or of whether it actually repaints this tick. The corner is reserved
        for as long as the badge is enabled, so AgentOverlay can avoid it
        (via OccupiedRows) without knowing anything about Badge itself."""
        return rows if self.pos.startswith("bottom") else 1

    def _anchor(self):
        return "right" if self.pos.endswith("right") else "left"

    def place(self, rows, cols, width):
        """(row, col), 1-based, or None if the terminal is too small to bother."""
        if rows < 2 or cols < width + 2:
            return None
        anchor = self._anchor()
        # cols - width leaves the last cell untouched: writing into it is what
        # makes a terminal wrap and scroll the whole screen by one line.
        col = edge_column(cols if anchor == "right" else 1, width, anchor)
        return (self.row(rows), max(1, col))

    def sequence(self, rows, cols, text, sgr):
        spot = self.place(rows, cols, len(text))
        if spot is None:
            return None
        row, col = spot
        # DECSC/DECRC rather than CSI s/u: it saves the SGR state too, so the
        # colour used here cannot leak into whatever claude draws next.
        return annotation_bytes(row, col, text, sgr)

    def paint(self, fd, rows, cols, state, remaining, attempts, max_attempts, now,
              blocked=False, deferred=False, restart=None, context=None, warn=None,
              context_estimated=False):
        text, sgr = self.frame(state, remaining, attempts, max_attempts, now, deferred,
                               restart, context, warn, context_estimated)
        if not self.due(text, now, blocked):
            return False
        # A narrower frame than the last one would leave the tail of that one on
        # screen — "◇ cr 1m" becoming "◆ cr" reads as "◇ c◆ cr" until claude
        # happens to repaint that row. Pad away from the anchored edge so the
        # cells we used are the cells we clear.
        draw = pad_to(text, self.painted_width, self._anchor())
        seq = self.sequence(rows, cols, draw, sgr)
        if seq is None:
            return False
        self.last_paint = now
        self.quiet_since = 0.0
        self.pending = False
        self.painted = text
        self.painted_width = len(text)
        return write_all(fd, seq)

    def erase(self, fd, rows, cols):
        """Take it back off the screen on the way out."""
        if not self.enabled or not self.painted:
            return
        seq = self.sequence(rows, cols, " " * self.painted_width, "0")
        if seq:
            write_all(fd, seq)
        self.painted = None
        self.painted_width = 0


# --------------------------------------------------------------------------- #
# pty plumbing
# --------------------------------------------------------------------------- #
def write_all(fd, data):
    """os.write is allowed to write less than asked; a short write on the pty
    would silently drop keystrokes, and on stdout would corrupt the render."""
    while data:
        try:
            n = os.write(fd, data)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                select.select([], [fd], [], 0.05)
                continue
            return False
        data = data[n:]
    return True


_ESC_TAIL = re.compile(r"\x1b[\x20-\x3f\[\]P_X^()=><]*$")


def split_escape_tail(text):
    """Return (complete, carry) so a control sequence cut in half by a read
    boundary is not decoded as literal text into the detection window."""
    m = _ESC_TAIL.search(text)
    if m and len(text) - m.start() < 64:
        return text[:m.start()], text[m.start():]
    return text, ""


def tcset(fd, attr):
    """Apply terminal attributes without ever blocking on it.

    TCSADRAIN waits for pending output to be consumed — if whatever is on the
    other end has stopped reading (a suspended terminal, a pane that went away),
    restoring the mode on exit hangs forever and the wrapper never returns.
    TCSANOW cannot block. SIGTTOU is masked for the call because a process that
    is no longer in the terminal's foreground group gets stopped by it instead of
    an error, which looks identical to a hang.
    """
    old = None
    try:
        old = signal.getsignal(signal.SIGTTOU)
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    except Exception:
        pass
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attr)
    except Exception:
        pass
    finally:
        if old is not None:
            try:
                signal.signal(signal.SIGTTOU, old)
            except Exception:
                pass


def fork_pty(rows, cols):
    """Like pty.fork(), but the window size is set BEFORE the child execs.

    pty.fork() hands the child an 0x0 terminal and leaves us to fix it after the
    fact; anything the TUI renders in that gap is laid out for the wrong width.
    """
    master, slave = pty.openpty()
    set_winsize(master, rows, cols)
    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.setsid()
        try:
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        except Exception:
            pass
        for target in (0, 1, 2):
            os.dup2(slave, target)
        if slave > 2:
            os.close(slave)
        return 0, -1
    os.close(slave)
    return pid, master


def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def get_winsize(fd):
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        if rows and cols:
            return rows, cols
    except Exception:
        pass
    return 24, 80


# --------------------------------------------------------------------------- #
# subagent tree overlay (T27) — annotates Claude Code's own agent tree with the
# model each row is really running on.
# --------------------------------------------------------------------------- #
_SCREEN_CSI = re.compile(r"\x1b\[([\x30-\x3f]*)([\x20-\x2f]*)([\x40-\x7e])")
_SCREEN_OSC = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)")
_SCREEN_CSI_PREFIX = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*\Z")


def _screen_escape_unfinished(rest):
    """True when `rest` (starting at ESC) is the beginning of an escape
    sequence whose end has not arrived yet. An OSC that never ends is let go
    after 4 KB, so a stray `ESC ]` cannot swallow the screen for good."""
    if rest == "\x1b":
        return True
    if rest.startswith("\x1b["):
        return bool(_SCREEN_CSI_PREFIX.match(rest))
    if rest[1:] in ("(", ")", "*", "+"):
        return True
    if rest.startswith("\x1b]"):
        return len(rest) < 4096 and not _SCREEN_OSC.match(rest)
    return False


class Screen:
    """A terminal emulator, just big enough to judge what a user would actually
    see. A badge or an overlay is drawn with cursor positioning, so a test (or
    the overlay itself) that greps the byte stream proves nothing: the question
    is where those bytes land, whether the screen scrolled, and where the
    cursor was left afterwards. This renders the stream into a grid and answers
    that. test/screen.py re-exports this class via --cr-dump-python, so the
    grid a test renders can never drift from what the supervisor actually feeds.

    Supported: printable text with deferred wrap, CR/LF/BS/TAB, CUP,
    CUU/CUD/CUF/CUB, CHA, ED/EL, SGR, DECSC/DECRC and CSI s/u, IL/DL (ESC[L /
    ESC[M), ICH/DCH (ESC[@ / ESC[P), a scroll region (DECSTBM) with IND/RI, and
    the DEC private modes (?25 cursor visibility, ?2026 synchronized update...)
    a frame is wrapped in — consumed and ignored, since none of them move the
    cursor or print text. Everything else is consumed and ignored too, which is
    the right behaviour here: an unhandled sequence must never become text.
    """
    def __init__(self, rows=24, cols=80):
        self.rows, self.cols = rows, cols
        self.cells = [[" "] * cols for _ in range(rows)]
        self.attrs = [[""] * cols for _ in range(rows)]
        self.row = self.col = 0
        self.attr = ""
        self.wrap_pending = False
        self.saved = (0, 0, "")
        self.scrolled = 0            # how many times the screen scrolled up
        self.pending = ""            # an escape sequence cut off by the last read
        self.top, self.bottom = 0, rows - 1   # scroll region, 0-based inclusive

    # -- reading it back ---------------------------------------------------- #
    def line(self, n):
        """Row n, 1-based, right-stripped."""
        return "".join(self.cells[n - 1]).rstrip()

    def text(self):
        return "\n".join(self.line(n + 1) for n in range(self.rows))

    def attr_at(self, row, col):
        return self.attrs[row - 1][col - 1]

    def cursor(self):
        """1-based (row, col), the way the escape sequences count."""
        return (self.row + 1, self.col + 1)

    # -- writing to it ------------------------------------------------------ #
    def feed(self, data):
        # A pty read ends wherever it ends, often in the middle of an escape
        # sequence; its tail is kept for the next feed instead of being
        # printed as text (`38;2;153;153;153m` on screen).
        if self.pending:
            data, self.pending = self.pending + data, ""
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            if ch == "\x1b":
                if _screen_escape_unfinished(data[i:]):
                    self.pending = data[i:]
                    break
                i += self._escape(data, i)
                continue
            i += 1
            if ch == "\r":
                self.col, self.wrap_pending = 0, False
            elif ch == "\n":
                self._newline()
            elif ch == "\b":
                self.col = max(0, self.col - 1)
                self.wrap_pending = False
            elif ch == "\t":
                self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
            elif ch == "\x07":
                pass
            elif ch >= " ":
                self._put(ch)
        return self

    def _put(self, ch):
        if self.wrap_pending:
            self.col = 0
            self._newline()
            self.wrap_pending = False
        self.cells[self.row][self.col] = ch
        self.attrs[self.row][self.col] = self.attr
        if self.col + 1 >= self.cols:
            self.wrap_pending = True      # deferred: the cell is filled, no scroll yet
        else:
            self.col += 1

    def _newline(self):
        if self.row == self.bottom:
            self._scroll_up(self.top, self.bottom)
        elif self.row + 1 >= self.rows:
            self._scroll_up(0, self.rows - 1)
        else:
            self.row += 1

    def _scroll_up(self, top, bottom, n=1):
        if top > bottom:
            return      # IL/DL issued below an active region's margin: no-op,
                         # not a slice-insert that would grow the grid past rows
        for _ in range(max(0, n)):
            self.cells[top:bottom + 1] = self.cells[top + 1:bottom + 1] + [[" "] * self.cols]
            self.attrs[top:bottom + 1] = self.attrs[top + 1:bottom + 1] + [[""] * self.cols]
            if top == 0:
                self.scrolled += 1

    def _scroll_down(self, top, bottom, n=1):
        if top > bottom:
            return
        for _ in range(max(0, n)):
            self.cells[top:bottom + 1] = [[" "] * self.cols] + self.cells[top:bottom]
            self.attrs[top:bottom + 1] = [[""] * self.cols] + self.attrs[top:bottom]

    def _escape(self, data, i):
        rest = data[i:]
        m = _SCREEN_OSC.match(rest)
        if m:
            return m.end()
        m = _SCREEN_CSI.match(rest)
        if m:
            if not m.group(2):              # an intermediate (DECSCUSR's " q"
                self._csi(m.group(1), m.group(3))   # and kin) never moves anything
            return m.end()
        if len(rest) >= 2:
            nxt = rest[1]
            if nxt == "7":
                self.saved = (self.row, self.col, self.attr)
            elif nxt == "8":
                self.row, self.col, self.attr = self.saved
                self.wrap_pending = False
            elif nxt == "D":                 # IND: down, scrolling at the margin
                self._newline()
            elif nxt == "M":                 # RI: up, reverse-scrolling at the margin
                self._reverse_index()
            elif nxt == "E":                 # NEL
                self.col = 0
                self._newline()
            elif nxt in "()*+" and len(rest) >= 3:
                return 3                     # SCS: `ESC ( B` picks a charset, prints nothing
            return 2
        return 1

    def _reverse_index(self):
        if self.row == self.top:
            self._scroll_down(self.top, self.bottom)
        else:
            self.row = max(0, self.row - 1)

    def _csi(self, params, final):
        if params[:1] in ("?", "<", ">", "="):
            # Private parameter prefixes: DEC modes (?25 cursor visibility,
            # ?2026 synchronized update), and Claude Code's keyboard setup at
            # startup -- `>4;2m` (modifyOtherKeys, not an SGR), `<u` / `>5u`
            # (kitty keyboard flags, not a cursor restore), `>0q`. None of
            # them moves the cursor or prints anything.
            return
        nums = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else []

        def arg(k, default=1):
            return nums[k] if k < len(nums) and nums[k] else default

        if final in "Hf":
            self.row = min(self.rows - 1, max(0, arg(0) - 1))
            self.col = min(self.cols - 1, max(0, arg(1) - 1))
            self.wrap_pending = False
        elif final == "A":
            self.row = max(0, self.row - arg(0))
        elif final == "B":
            self.row = min(self.rows - 1, self.row + arg(0))
        elif final == "C":
            self.col = min(self.cols - 1, self.col + arg(0))
        elif final == "D":
            self.col = max(0, self.col - arg(0))
        elif final == "G":
            self.col = min(self.cols - 1, max(0, arg(0) - 1))
        elif final == "J":
            self._erase_display(arg(0, 0))
        elif final == "K":
            self._erase_line(arg(0, 0))
        elif final == "L":                       # IL: insert blank lines at the cursor
            self._scroll_down(self.row, self.bottom, arg(0))
        elif final == "M":                       # DL: delete lines at the cursor
            self._scroll_up(self.row, self.bottom, arg(0))
        elif final == "S":                       # SU: scroll the region up
            self._scroll_up(self.top, self.bottom, arg(0))
        elif final == "T":                       # SD: scroll the region down
            self._scroll_down(self.top, self.bottom, arg(0))
        elif final == "@":                       # ICH
            self._insert_chars(arg(0))
        elif final == "P":                       # DCH
            self._delete_chars(arg(0))
        elif final == "r":                       # DECSTBM: scroll region
            top = min(self.rows, arg(0)) - 1
            bottom = min(self.rows, arg(1, self.rows)) - 1
            self.top, self.bottom = (top, bottom) if top < bottom else (0, self.rows - 1)
            # Origin mode (DECOM) is not implemented — this emulator only ever
            # runs with it off — and with it off DECSTBM homes the cursor to
            # the screen's absolute origin, not the new region's top margin.
            self.row, self.col = 0, 0
        elif final == "m":
            self.attr = "" if not params or params == "0" else params
        elif final == "s":
            self.saved = (self.row, self.col, self.attr)
        elif final == "u":
            self.row, self.col, self.attr = self.saved

    def _insert_chars(self, n):
        row, arow = self.cells[self.row], self.attrs[self.row]
        for _ in range(max(0, min(n, self.cols))):
            row.insert(self.col, " ")
            arow.insert(self.col, "")
        del row[self.cols:]
        del arow[self.cols:]

    def _delete_chars(self, n):
        row, arow = self.cells[self.row], self.attrs[self.row]
        for _ in range(max(0, min(n, self.cols - self.col))):
            del row[self.col]
            del arow[self.col]
        row.extend([" "] * (self.cols - len(row)))
        arow.extend([""] * (self.cols - len(arow)))

    def _blank_row(self, r, lo, hi):
        for c in range(lo, hi):
            self.cells[r][c] = " "
            self.attrs[r][c] = ""

    def _erase_line(self, mode):
        if mode == 0:
            self._blank_row(self.row, self.col, self.cols)
        elif mode == 1:
            self._blank_row(self.row, 0, self.col + 1)
        else:
            self._blank_row(self.row, 0, self.cols)

    def _erase_display(self, mode):
        if mode == 0:
            self._blank_row(self.row, self.col, self.cols)
            for r in range(self.row + 1, self.rows):
                self._blank_row(r, 0, self.cols)
        elif mode == 1:
            for r in range(0, self.row):
                self._blank_row(r, 0, self.cols)
            self._blank_row(self.row, 0, self.col + 1)
        else:
            for r in range(self.rows):
                self._blank_row(r, 0, self.cols)


_CHILD_GLYPH = "⎿"  # a child's own status line, not an agent of its own


def find_agent_rows(screen):
    """[(row, label, label_col)] for every row of Claude Code's subagent tree
    currently on screen — scanned in the GRID, never in the byte stream: the
    render is differential and word-split (words separated by ESC[<col>G), so
    a row's text does not exist as a contiguous run of bytes anywhere in the
    stream. A row whose label starts with ⎿ is a child's status line, not an
    agent's own row, and is left out; so is the trailing "· N tool uses" claude
    appends to a label, which is not part of the description Agent tool calls
    are keyed on.
    """
    found = []
    for r in range(1, screen.rows + 1):
        line = screen.line(r)
        for pat in PAT["agent_row"]:
            m = pat.match(line)
            if not m:
                continue
            raw = m.group(2)
            rest = raw.strip()
            if not rest or rest.startswith(_CHILD_GLYPH):
                break
            label = rest.split(" · ")[0].strip()
            if label:
                # 1-based column the label actually starts at in THIS row, not
                # an assumed constant — a render that indents differently (or
                # a test grid that places it elsewhere) still gets the real
                # position back.
                label_col = m.start(2) + (len(raw) - len(raw.lstrip())) + 1
                found.append((r, label, label_col))
            break
    return found


def find_panel_agent_rows(screen):
    """[(row, label, label_col)] for every row of the OTHER subagent view
    (T29): the persistent panel opened by typing /tasks while at least one
    Task-tool subagent is still running. Unlike find_agent_rows's tree, this
    one never collapses on its own — but it also carries a section header
    ("Local agents (N)") at the exact same indent as a real row, with no tree
    glyph to tell them apart by column alone. The discriminator is the
    trailing "(running)"/"(done)"/... state marker CR_PAT_AGENTS_PANEL_ROW
    requires and the header never has.
    """
    found = []
    for r in range(1, screen.rows + 1):
        line = screen.line(r)
        for pat in PAT["agents_panel_row"]:
            m = pat.match(line)
            if not m:
                continue
            label = m.group(1).strip()
            if label:
                found.append((r, label, m.start(1) + 1))
            break
    return found


_MODEL_DISPLAY = {
    "claude-sonnet-5": "sonnet-5",
    "claude-opus-5": "opus-5",
    "claude-haiku-4-5-20251001": "haiku-4.5",
    "claude-fable-5-1": "fable-5.1",
}


def model_label(slug, effort=None):
    """The right-of-row annotation for one agent: 'sonnet-5/high', 'opus-5/?'
    when the model is known but the effort is not (true of every subagent as
    of 2.1.273 — perTurnEffort is always null on them), '…' before the first
    assistant line has arrived (model unknown for ~3s on a fresh agent), or
    '?' when two agents share a label and which is which cannot be told apart.

    The spawn request's own "model" field is never accepted here: forks have
    been observed to ignore it and run on a different model entirely (T27
    evidence), so showing it would show a lie with a straight face.
    """
    if slug is None:
        return "…"
    if slug == "?":
        return "?"
    suffix = ""
    base = slug
    if base.endswith("[1m]"):
        base, suffix = base[:-4], "[1m]"
    name = _MODEL_DISPLAY.get(base, base) + suffix
    return "%s/%s" % (name, effort) if effort else "%s/?" % name


class SubagentRegistry:
    """Maps a tree row's label to the model its agent is really running on.

    Reads `<sessionId>/subagents/agent-*.{meta.json,jsonl}` next to the
    transcript TranscriptWatcher is currently following — meta.json has the
    `description` a row's label is matched against (and is written at spawn
    time, before the model is known); the model comes only from the last
    assistant line of the agent's OWN jsonl, never from meta.json or the
    spawn request, both of which state an intent a fork is free to ignore.
    Polled by mtime, the same way TranscriptWatcher is, and no more often than
    `poll` seconds.
    """
    def __init__(self, poll=1.0):
        self.poll = poll
        self.next_poll = 0.0
        self.session_path = None
        self.dir = None
        self.descriptions = {}     # agent_id -> description
        self.meta_mtimes = {}      # agent_id -> mtime last read
        self.jsonl_offsets = {}    # agent_id -> bytes already scanned
        self.models = {}           # agent_id -> (slug, effort)
        self.by_label = {}         # label -> agent_id, or "?" on a collision

    def _rebind(self, session_path):
        self.session_path = session_path
        if session_path:
            base = os.path.splitext(os.path.basename(session_path))[0]
            self.dir = os.path.join(os.path.dirname(session_path), base, "subagents")
        else:
            self.dir = None
        self.descriptions = {}
        self.meta_mtimes = {}
        self.jsonl_offsets = {}
        self.models = {}
        self.by_label = {}

    def poll_now(self, session_path, now=None):
        now = now if now is not None else time.time()
        if session_path != self.session_path:
            self._rebind(session_path)
        if not self.dir or now < self.next_poll:
            return
        self.next_poll = now + self.poll
        try:
            metas = glob.glob(os.path.join(self.dir, "agent-*.meta.json"))
        except OSError:
            metas = []
        by_label = {}
        for path in metas:
            agent_id = os.path.basename(path)[len("agent-"):-len(".meta.json")]
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if self.meta_mtimes.get(agent_id) != mtime:
                self.meta_mtimes[agent_id] = mtime
                try:
                    with open(path) as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    meta = {}
                self.descriptions[agent_id] = meta.get("description")
            label = self.descriptions.get(agent_id)
            if label:
                by_label.setdefault(label, []).append(agent_id)
            self._read_model(agent_id)
        # A label two agents both claim can never be told apart on screen —
        # show '?' for both rather than silently picking one (T27 AC).
        self.by_label = {label: (ids[0] if len(ids) == 1 else "?")
                          for label, ids in by_label.items()}

    def _read_model(self, agent_id):
        path = os.path.join(self.dir, "agent-%s.jsonl" % agent_id)
        offset = self.jsonl_offsets.get(agent_id, 0)
        # _appended() holds back a partial trailing line rather than parsing it
        # half-written — the same idiom TranscriptWatcher's own records() uses,
        # needed here too: a poll landing mid-write must not strand the rest of
        # that line unreadable once it does land.
        new_offset, rows = _appended(path, offset)
        self.jsonl_offsets[agent_id] = new_offset
        for raw in rows:
            raw = raw.decode("utf-8", "replace").strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("type") != "assistant" or rec.get("isSidechain"):
                continue
            msg = rec.get("message") or {}
            model = msg.get("model")
            if not model or model == "<synthetic>":
                continue
            effort = rec.get("effort") or msg.get("perTurnEffort") or None
            self.models[agent_id] = (model, effort)

    def model_for(self, label):
        """(slug, effort) for a row's label; ("?", None) on a label collision;
        None before any subagent file has named this label at all."""
        agent_id = self.by_label.get(label)
        if agent_id is None:
            return None
        if agent_id == "?":
            return ("?", None)
        return self.models.get(agent_id)


class StatusPoller:
    """Reads back what `--cr-statusline`'s proxy mode last wrote for THIS
    session (T20): `~/.agent-retrier/status/<pid>.json`, one file per
    wrapper pid, written atomically every time Claude Code invokes the
    statusline command. Polled by mtime, no more often than `poll` seconds --
    the same idiom `SubagentRegistry`/`TranscriptWatcher` already use.
    """
    def __init__(self, path, poll=2.0):
        self.path = path
        self.poll = poll
        self.next_poll = 0.0
        self.mtime = None

    def poll_now(self, now=None):
        now = now if now is not None else time.time()
        if now < self.next_poll:
            return None
        self.next_poll = now + self.poll
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return None
        if mtime == self.mtime:
            return None
        self.mtime = mtime
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None


class AgentOverlay:
    def __init__(self, cfg):
        self.enabled = bool(cfg["agents_overlay"])
        self.pos = cfg["agents_pos"] if cfg["agents_pos"] in ("right", "label") else "right"
        self.painted = {}      # row -> width last painted there

    def paint(self, fd, rows, cols, screen, registry, badge_row=None, occupied=None):
        if not self.enabled:
            return False
        # Which rows are already spoken for. `occupied` (an OccupiedRows,
        # T28) is how main() coordinates this with Badge now — Badge claims
        # its own row without AgentOverlay having to know its geometry.
        # `badge_row` stays as a direct single-row override for callers (unit
        # tests) that have no registry to build; bottom-right/bottom-left
        # share the last row, top-right/top-left the first, so with neither
        # given the corner is assumed to be the bottom row, as before T28.
        if occupied is None:
            occupied = {rows if badge_row is None else badge_row}
        seen = set()
        wrote = False
        # Two independent row shapes (T27's inline tree, T29's /tasks panel)
        # can each have an agent on screen at once — neither ever produces a
        # row the other one also matches (disjoint glyph sets), so this is a
        # concatenation, not a merge that needs de-duplication.
        for row, label, label_col in find_agent_rows(screen) + find_panel_agent_rows(screen):
            if row in occupied:
                continue        # never contest a row somebody else already claimed
            model = registry.model_for(label)
            text = model_label(*model) if model else model_label(None)
            seen.add(row)
            if self._paint_row(fd, rows, cols, row, label_col, len(screen.line(row)), text):
                wrote = True
        for row in [r for r in self.painted if r not in seen]:
            del self.painted[row]
        return wrote

    def _paint_row(self, fd, rows, cols, row, label_col, own_width, text):
        # The column has to be derived from the PADDED width, not the new
        # text's own — otherwise a shrinking annotation (e.g. "sonnet-5/high"
        # -> "sonnet-5/?", or two agents colliding down to a bare "?") is
        # placed as if it were only as wide as the new text while still being
        # padded out to the old, wider one, and the write overruns past the
        # column it was placed at (in "right" mode: into or past the
        # protected last column). edge_column/pad_to (T28) are the same
        # functions Badge uses for exactly this reason: the arithmetic can no
        # longer drift between the two painters.
        prev = self.painted.get(row, 0)
        width = max(len(text), prev)
        if cols < width + 2:
            return False
        if self.pos == "label":
            col = edge_column(label_col - 1, width, "right")
            if col < 1:
                return False
        else:
            # cols - width leaves the last cell untouched — writing into it is
            # what makes a terminal wrap and scroll the whole screen by one
            # line (the same rule Badge.place follows).
            col = edge_column(cols, width, "right")
            if own_width >= col - 1:
                return False    # claude's own text already reaches into our column
        draw = pad_to(text, width, "right")
        self.painted[row] = len(text)
        return write_annotation(fd, row, col, draw, "2")


def _rotate_log_if_needed(path, max_bytes, keep):
    """Rotate `path` to `path.1` (and shift `path.1..path.keep-1` up by one,
    dropping whatever would spill past `path.keep`) if it is already over
    `max_bytes`. No-op if the file doesn't exist yet or is under the limit.

    Only ever called from Logger.__init__, i.e. at wrapper startup — never
    mid-run, so a rotation can't happen underneath a neighboring wrapper
    process in the middle of an incident. A process that already has `path`
    open for writing when this runs keeps its file descriptor pointed at
    the same inode, which after the rename below is `path.1` — it just goes
    on appending there. That's accepted (T01's per-line tag keeps such lines
    readable) and not something this function tries to prevent.
    """
    try:
        if os.path.getsize(path) <= max_bytes:
            return
    except OSError:
        return   # nothing on disk to rotate
    if keep < 1:
        return
    try:
        oldest = "%s.%d" % (path, keep)
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(keep - 1, 0, -1):
            src = "%s.%d" % (path, n)
            if os.path.exists(src):
                os.rename(src, "%s.%d" % (path, n + 1))
        os.rename(path, "%s.1" % path)
    except OSError:
        pass


class Logger:
    """Writes to the one log file every wrapper on the machine shares.

    `tag` names which process wrote a line — without it, two sessions in the
    same project interleave into an unreadable log (T01).
    """
    def __init__(self, path, tag="", max_bytes=5_000_000, keep=2):
        self.path = path
        self.tag = tag
        _rotate_log_if_needed(path, max_bytes, keep)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.fh = open(path, "a", buffering=1)
        except Exception:
            self.fh = None

    def __call__(self, msg):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] [%s] %s" % (stamp, self.tag, msg) if self.tag \
            else "[%s] %s" % (stamp, msg)
        if self.fh:
            try:
                self.fh.write(line + "\n")
            except Exception:
                pass


def launch_vector():
    """How to start Claude, as an argv prefix the shell layer already resolved.

    Usually one element (the binary). For an alias or a command line it is a
    shell invocation instead; either way we just exec it with the user's
    arguments appended, so nothing here needs to know which shape it got.
    """
    raw = os.environ.get("CR_CLAUDE_ARGV") or ""
    vec = raw.split("\x1f")
    if vec and vec[-1] == "":
        vec.pop()                      # trailing separator from printf
    return vec or [os.environ.get("CR_CLAUDE_RESOLVED") or "claude"]


def is_roster_launch(argv):
    """Were we started on the agent roster rather than on a session?

    `claude agents` is a list of OTHER sessions. Every card on it states somebody
    else's limit, and opening one scrolls that session's history — old banners
    included — past our scraper. Neither is this terminal's state, and both parse
    into a reset time that is not ours: a card reading "resets 2:10am" parked a
    wrapper for eleven hours while the limit it was actually under lifted in
    seven minutes. There is no input box on the roster to type "continue" into
    either, so the screen channel has nothing to offer here at all. The
    transcript channel is untouched: a session we open from the roster and then
    work in still reports its own limit through it.

    Read as the first positional word, so a prompt that happens to contain
    "agents" is not mistaken for the subcommand.
    """
    for arg in argv or []:
        if arg.startswith("-"):
            continue
        return arg == "agents"
    return False


def is_attach_launch(argv):
    """Does argv start Claude's interactive background-session attachment?"""
    for arg in argv or []:
        if arg.startswith("-"):
            continue
        return arg == "attach"
    return False


# --------------------------------------------------------------------------- #
# claude's own statusline, relayed to us (T20)
# --------------------------------------------------------------------------- #
def _user_statusline_command(cwd):
    """Claude Code's own `statusLine` setting, read straight from whichever
    settings file would answer it for a session started in `cwd` -- checked in
    the precedence Claude Code's own docs describe: a local project override
    first, then the shared project one, then the user's own. Not independently
    verified live (see docs/backlog/T20-claude-effective-window.md's
    "Verified" section) -- `None` either way means "nothing configured",
    never a reason to invent one of our own.
    """
    home_settings = os.path.join(
        os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
        "settings.json")
    for path in (os.path.join(cwd, ".claude", "settings.local.json"),
                os.path.join(cwd, ".claude", "settings.json"),
                home_settings):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        line = data.get("statusLine") if isinstance(data, dict) else None
        if isinstance(line, dict) and line.get("command"):
            return line
    return None


def run_statusline_proxy(status_path, stdin=None, stdout=None):
    """`--cr-statusline <path>`: what Claude Code actually runs as the
    statusline command, once `claude_launch_args` has pointed `--settings` at
    us (T20).

    Reads exactly what Claude Code hands any statusline command -- one blob
    of JSON on stdin, documented to carry `model.id`,
    `context_window.context_window_size`, `session_id` and
    `transcript_path` -- records the fields the supervisor wants at
    `status_path` (atomically: a temp file in the same directory, then
    `os.rename`, the pattern `WindowLookup`/`UpdateCheck`/`HandoffRegistry`
    already use for a JSON file more than one process can touch), then runs
    the user's OWN `statusLine` command, if `settings.json`/
    `settings.local.json` name one, with that same stdin, and prints exactly
    what it printed. A user with no statusline configured at all gets no
    output here either -- inventing one would be a lie about what their
    settings actually say.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    raw = stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    model = payload.get("model") or {}
    window = payload.get("context_window") or {}
    record = dict(model_id=model.get("id"),
                 context_window_size=window.get("context_window_size"),
                 session_id=payload.get("session_id"),
                 transcript_path=payload.get("transcript_path"),
                 at=time.time())
    try:
        parent = os.path.dirname(status_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = "%s.%d.tmp" % (status_path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(record, fh)
        os.rename(tmp, status_path)
    except OSError:
        pass
    workspace = payload.get("workspace") or {}
    cwd = workspace.get("current_dir") or workspace.get("project_dir") or os.getcwd()
    line = _user_statusline_command(cwd)
    if not line or line.get("type", "command") != "command":
        return 0
    try:
        result = subprocess.run(line["command"], shell=True, input=raw,
                                text=True, capture_output=True)
    except OSError:
        return 0
    stdout.write(result.stdout)
    return result.returncode


def main(argv):
    # Started from a temp file because /dev/fd was not available: it has done its
    # job the moment python has read it, and leaving it behind would litter /tmp
    # with copies of the wrapper.
    tmp = os.environ.pop("CR_PY_TMP", "")
    if tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    launch = launch_vector()
    claude = launch[0]
    agent_name = pick_agent(CFG["agent"], launch)
    log = Logger(CFG["log"], tag="cr %d %s" % (os.getpid(), agent_name),
                 max_bytes=CFG["log_max_bytes"], keep=CFG["log_keep"])
    session_start = time.time()
    log("start: %s %s (agent: %s) cwd=%s"
        % (" ".join(launch), " ".join(argv), agent_name, os.getcwd()))

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    interactive = os.isatty(stdin_fd) and os.isatty(stdout_fd)

    # Before the fork, while the terminal is still ours and still cooked: after
    # it, every cell belongs to claude's TUI and anything written here is either
    # painted over or paints over something.
    updater = UpdateCheck(CFG, log)
    if interactive:
        notice = updater.notice(CFG["version"])
        if notice:
            headline, how = notice
            os.write(stdout_fd,
                     ("\x1b[2m[agent-retrier] %s\n"
                      "                 %s\x1b[0m\n" % (headline, how)).encode())
            log("%s — %s" % (headline, how))
            time.sleep(max(0.0, CFG["update_notice"]))

    run_cfg = agent_cfg(CFG, agent_name)
    session_id = os.urandom(4).hex()
    handoff_registry = None
    if run_cfg["context_tokens"] > 0 or run_cfg.get("context_restart_on"):
        # A custom phrase that drops {file} cannot carry a per-session handoff
        # path — worth one line in the log, once, rather than a silent fold
        # into a path nobody told the model about (T05).
        for key, env_name in (("resume_msg", "CR_RESUME_MSG"), ("handoff_msg", "CR_HANDOFF_MSG")):
            phrase = run_cfg.get(key) or ""
            if phrase and "{file}" not in phrase:
                log("%s does not contain {file}; a per-session handoff path "
                    "cannot be passed to it" % env_name)
        handoff_registry = HandoffRegistry(run_cfg["handoff_registry_dir"])
        run_cfg["handoff_file"] = handoff_registry.claim(
            run_cfg["handoff_file"], session_id, os.getpid(), os.getcwd(),
            agent_name, session_start, log)
    extra = codex_launch_args(run_cfg)
    if extra:
        log("holding codex's own compaction back for the context restart: %s"
            % " ".join(extra))
    supervisor_pid = os.getpid()   # stable for the whole session; fork_pty below
                                    # only ever changes the CHILD's pid, never ours
    status_path = os.path.join(run_cfg["status_dir"], "%d.json" % supervisor_pid)
    statusline_args = claude_launch_args(run_cfg, agent_name, argv, pid=supervisor_pid)
    if statusline_args:
        log("claude's own statusline is proxied through --cr-statusline, so "
            "its own report of the session's window (200k vs [1m]) can correct "
            "the restart threshold (T20; CR_STATUSLINE_PROXY=0 to disable)")
    extra = extra + statusline_args

    rows, cols = get_winsize(stdout_fd) if interactive else (24, 80)
    pid, master = fork_pty(rows, cols)
    if pid == 0:
        try:
            os.execvp(launch[0], launch + extra + argv)
        except Exception as exc:
            sys.stderr.write("agent-retrier: cannot exec %s: %s\n" % (claude, exc))
            os._exit(127)

    old_attr = None
    if interactive:
        try:
            old_attr = termios.tcgetattr(stdin_fd)
            raw = termios.tcgetattr(stdin_fd)
            tty.cfmakeraw(raw)
            tcset(stdin_fd, raw)
        except Exception:
            old_attr = None

    resized = [False]

    def on_winch(_sig, _frm):
        resized[0] = True

    old_winch = signal.getsignal(signal.SIGWINCH)
    try:
        signal.signal(signal.SIGWINCH, on_winch)
    except Exception:
        pass

    # pty.fork() puts claude in its own session, so signals delivered to us (a
    # `kill` from outside, or ^C when stdin is a pipe rather than a raw tty) do
    # not reach it. Forward them instead of dying and orphaning the child.
    def forward(sig, _frm):
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    old_sigs = {}
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        try:
            old_sigs[s] = signal.getsignal(s)
            signal.signal(s, forward)
        except Exception:
            pass

    # Started only now: a fork out of a multi-threaded process is a hazard, and
    # this thread's whole purpose is to be ready by the NEXT launch anyway.
    updater.refresh()

    ctl = Controller(run_cfg, log, session_id=session_id)
    status_poller = StatusPoller(status_path, poll=CFG["poll"]) if statusline_args else None
    lookup = WindowLookup(CFG, log)
    badge = Badge(CFG)
    agents = AgentOverlay(CFG)
    agent_registry = SubagentRegistry(poll=CFG["agents_poll"])
    # Nothing is built when the overlay is off: no Screen, no per-frame feed,
    # not one extra byte (CR_AGENTS_OVERLAY=0 AC). T28 open question: Badge
    # stays on its own due() quiet-gap timer rather than switching to this
    # ESC[?2026l frame-closure trigger, even though the rendering primitive is
    # now shared. Moving it over would require a Screen (and the per-frame
    # feed cost) whenever CR_BADGE=1 — effectively always, since it defaults
    # on — while CR_AGENTS_OVERLAY defaults off precisely to avoid that cost.
    # Sharing the REDRAW trigger too would flip that default by the back
    # door; sharing only the primitive keeps CR_BADGE's cost unchanged.
    tree_screen = Screen(rows, cols) if CFG["agents_overlay"] else None
    if not interactive:
        badge.enabled = False      # nothing to paint on when stdout is a pipe
        agents.enabled = False
        tree_screen = None
    usage_log = CodexUsageLog(CFG, log) if agent_name == "codex" and ctl.context_enabled else None
    registry = (ClaudeSessionRegistry(pid, cwd=os.getcwd(), started_at=int(session_start * 1000), log=log)
               if agent_name == "claude" else None)
    watcher = TranscriptWatcher(poll=CFG["poll"], agent=build_agent(agent_name, argv),
                                echo=[CFG["message"], ctl.resume_text],
                                registry=registry, log=log)
    if ctl.context_enabled:
        agent_kind = "codex" if ctl.codex else "claude"
        override = ctl._model_tokens_override()
        if override:
            threshold_desc = "%d tokens (per-model override)" % override
        elif ctl.cfg["context_tokens"] > 0:
            threshold_desc = "%d tokens" % ctl.cfg["context_tokens"]
        else:
            # The window is not known yet at this point (T19), so which of
            # model_restart_at's own row or default_restart_at's fallback for
            # a window it does not describe will supply the number is not
            # decided until the model states one.
            threshold_desc = ("the model's own profile, or %s's default for "
                              "a window this build has never heard of" % agent_kind)
        log("context restart armed: handoff -> %s, %s"
            % (ctl.handoff_path, threshold_desc))
        # The Write tool would create it, but a directory that is already there
        # is one fewer thing for the folding turn to get wrong.
        parent = os.path.dirname(ctl.handoff_path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            log("cannot create %s: %s" % (parent, exc))
    scrape_allowed = CFG["scrape"] in ("auto", "always")
    if scrape_allowed and CFG["scrape"] != "always" and is_roster_launch(argv):
        scrape_allowed = False
        log("wrapping the agent roster; the screen channel is off "
            "(every banner on it belongs to another session)")
    window = ""            # rolling, ansi-stripped view of what claude just drew
    esc_carry = ""         # half-received control sequence from the previous read
    pending = []           # [(due_ts, bytes)] scheduled writes into the pty
    pending_scrape = None  # (banner, confirm_at) — a scraped banner awaiting confirmation
    handoff_watch_text = None  # the fold phrase currently registered with watcher.expect
    watch_stdin = True
    exit_code = 0

    def notify(msg):
        """A single dim line, drawn where the human can see it.

        Claude's TUI repaints continuously, so this is transient by design — the
        durable record is the log file.
        """
        if not CFG["notify"] or not interactive:
            return
        try:
            os.write(stdout_fd, ("\r\x1b[2m[agent-retrier] %s\x1b[0m\r\n" % msg).encode())
        except Exception:
            pass
        badge.pending = True       # the line just scrolled the badge away

    def wait_cancelled():
        """The limit lifted on its own. Forget everything that described it.

        The banner is still somewhere in the rolling window and in whatever the
        scraper had half-confirmed; left there, it would be re-detected the
        moment the controller went idle and park the session all over again.
        """
        nonlocal window, pending_scrape
        window = ""
        pending_scrape = None
        notify("session is answering again; wait cancelled")

    def schedule_injection(text, dismiss_menu, now):
        """Type like a human, not like a paste.

        Claude Code treats a burst of text ending in CR as a paste and turns the
        CR into a newline, so the message piles up in the box unsent (upstream
        issues #7/#19). Text and Enter therefore go out as separate writes,
        several hundred ms apart, with an optional Escape first to dismiss the
        /rate-limit-options selector. The exact write sequence after that is
        `typing_plan`'s (T16) — per agent, because "/" opens a command list on
        both, but codex has no popup at all for "$" or "@" (T15).
        """
        t = now
        if dismiss_menu:
            pending.append((t, b"\x1b"))
            t += 0.35
        for delay, data in typing_plan(agent_name, text, CFG):
            pending.append((t + delay, data))

    try:
        while True:
            if resized[0]:
                resized[0] = False
                rows, cols = get_winsize(stdout_fd)
                set_winsize(master, rows, cols)
                badge.painted = None       # the corner it lived in has moved
                badge.pending = True
                if tree_screen is not None:
                    tree_screen = Screen(rows, cols)
                    agents.painted = {}

            now = time.time()
            timeout = 0.25
            for due, _ in pending:
                timeout = min(timeout, max(0.0, due - now))
            if badge.enabled:
                # Wake up for the badge too, or a session that goes quiet keeps a
                # stale countdown on screen until claude happens to print again.
                timeout = min(timeout, badge.QUIET)

            rlist = [master] + ([stdin_fd] if watch_stdin else [])
            try:
                ready, _, _ = select.select(rlist, [], [], timeout)
            except (OSError, select.error) as exc:
                if getattr(exc, "errno", None) == errno.EINTR:
                    continue
                break

            now = time.time()

            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    # EIO is how a pty reports "the child closed its end" on Linux.
                    data = b""
                if not data:
                    break
                write_all(stdout_fd, data)
                badge.note_output(now)
                text, esc_carry = split_escape_tail(
                    esc_carry + data.decode("utf-8", "replace"))
                if tree_screen is not None:
                    # Fed the same text write_all just sent to the real screen,
                    # with an incomplete escape sequence held back exactly as
                    # split_escape_tail already holds it back from detection —
                    # so a frame cut in half by a read boundary is completed on
                    # the next read rather than half-decoded as literal text.
                    frame_closed = "\x1b[?2026l" in text
                    tree_screen.feed(text)
                    if frame_closed:
                        agent_registry.poll_now(watcher.current, now)
                        occupied = OccupiedRows()
                        if badge.enabled:
                            occupied.claim(badge.row(rows))
                        agents.paint(stdout_fd, rows, cols, tree_screen, agent_registry,
                                    occupied=occupied)
                chunk = strip_ansi(text)
                window = (window + chunk)[-8192:]
                ctl.on_output(chunk, now)
                # The transcript is the trustworthy channel; scraping the render is
                # the fallback for when it is unavailable (no transcript directory,
                # a Claude Code build that stops writing the field). Running both
                # unconditionally would just re-import the false-positive class the
                # structured channel exists to avoid.
                # A wait the screen scheduled stays open to the screen: it is the
                # only channel that can correct it, and a scraped banner is the
                # one kind that can be somebody else's (see `rescrapable`). A
                # stall is not re-read that way — there is nothing in it to
                # correct, and one refusal must not read as an endless run.
                rescrape = ctl.rescrapable()
                if scrape_allowed and pending_scrape is None and (ctl.state == IDLE or rescrape):
                    if CFG["scrape"] == "always" or not watcher.seen_any:
                        banner = find_limit(window)
                        if banner:
                            pending_scrape = ("limit", banner, now + CFG["scrape_confirm"])
                        elif not rescrape:
                            stall = find_stall(window)
                            if stall:
                                pending_scrape = ("stall", stall,
                                                  now + CFG["scrape_confirm"])

            if watch_stdin and stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    # Our stdin hit EOF. Stop selecting on it (a closed fd stays
                    # readable forever and would spin the loop), but leave claude
                    # running — piped input followed by an interactive session is
                    # a normal shape.
                    watch_stdin = False
                else:
                    ctl.on_user_bytes(data, now)
                    write_all(master, data)

            recs = watcher.poll_now(now)
            if watcher.current and watcher.current != ctl.context_path:
                # The one place `main()` tells the controller a transcript is
                # now ours: whatever the watcher used to decide it (identity,
                # when the registry names one; its growth heuristic otherwise).
                why = "session identity" if watcher.bound_session_id else "fallback heuristic"
                ctl.bind_transcript(watcher.current, why, now)
            # >1 candidate and nothing has echoed yet (T03): read on, but a
            # number that might be a neighbour's is not one to fold on.
            ctl.on_candidates(len(watcher.candidates) > 1 and not watcher.confirmed
                              and not watcher.bound_session_id, now)
            for rec in recs:
                if rec.get("kind") == "queued":
                    if ctl.handoff_text and rec.get("text") == ctl.handoff_text:
                        ctl.on_handoff_queued(now)
                    continue
                if rec.get("kind") == "prompt":
                    if rec.get("path") == watcher.current and ctl.on_human_prompt(now):
                        notify("context restart finished by hand; nothing to unfold")
                    continue
                if rec.get("kind") == "echo":
                    # The fold phrase's nonce is unique machine-wide, so its
                    # echo is definitive proof of identity on its own (T06) —
                    # checked ahead of the current-transcript filter below,
                    # unlike every other echo kind, which a neighbour's session
                    # can produce just as easily as ours.
                    echo_path = rec.get("path")
                    if (ctl.handoff_text and rec.get("text") == ctl.handoff_text
                            and ctl.on_handoff_echo(echo_path, now)):
                        # Syncing the watcher too keeps its own growth heuristic
                        # from handing `current` back to a candidate this same
                        # proof just ruled out (T03).
                        watcher.confirm(echo_path)
                        continue
                    # claude's own echo of our retry/resume is only evidence
                    # about the session at this terminal; a neighbour's is not.
                    if rec.get("path") != watcher.current:
                        continue
                    if rec.get("text") == ctl.resume_text and ctl.on_resume_echo(now):
                        continue
                    if ctl.on_echo(now):
                        notify("session resumed")
                    continue
                if rec.get("kind") == "alive":
                    # The usage figures, the turn state, and "the session is
                    # answering" are read only off the transcript this
                    # terminal's session writes; another session's are another
                    # session's.
                    if rec.get("path") == watcher.current:
                        ctl.on_context(rec, now)
                        if rec.get("turn"):
                            ctl.on_turn(rec["turn"], now)
                        if rec.get("clean"):
                            ctl.on_turn_done(now)
                        # A row that only names the model says nothing about
                        # whether anything is being served, and a wait must
                        # not end on it.
                        # Rows are in file order, so an assistant row that
                        # follows a limit row really did come after it.
                        if not rec.get("quiet") and ctl.on_alive(now, "transcript", rec.get("path")):
                            wait_cancelled()
                    continue
                if rec.get("turn") and rec.get("path") == watcher.current:
                    ctl.on_turn(rec["turn"], now)     # a turn that died of it
                if rec.get("kind") == "stall":
                    pending_scrape = None        # the structured channel wins
                    if ctl.on_stall(rec["text"], now, "transcript"):
                        notify("the server refused the turn; trying again shortly")
                    continue
                text = rec["text"] or "usage limit"
                pending_scrape = None            # the structured channel wins
                # A limit is the account's, not the session's, so it is read
                # from any file in the project — but a wait it schedules is
                # only "ours" to clear on a neighbour's say-so, not on a
                # screen correction meant for someone else's terminal.
                source = "transcript" if rec.get("path") == watcher.current else "neighbour"
                if ctl.on_limit(text, now, source):
                    notify("usage limit detected; waiting for reset")
            # Rows are not the only thing a transcript gains, and the gap between
            # a tool call and its result can be minutes: bytes are what say the
            # session is still mid-turn.
            ctl.note_growth(watcher.grown, now)
            if watcher.agent_status is not None:
                ctl.on_agent_status(watcher.agent_status, watcher.agent_status_at)
            if usage_log is not None and watcher.current:
                for total, cap in usage_log.poll(watcher.agent.thread_id(watcher.current), now):
                    ctl.on_context(dict(kind="alive", source="log", path=watcher.current,
                                        sidechain=False, tokens=total, cap=cap), now)
            if status_poller is not None:
                status = status_poller.poll_now(now)
                if status:
                    ctl.on_status(status, now)

            # A model the shipped table cannot size. Asking is off the hot path
            # in a worker thread, so this is only ever "post the question" and
            # "collect whatever came back" — never a wait.
            if ctl.window_unknown:
                lookup.want(ctl.window_unknown)
            for slug, learned, source in lookup.take():
                ctl.on_window_learned(slug, learned, source)

            # A scraped banner is acted on only after it has stood for a moment.
            # That covers two races at once: a frame captured mid-repaint, and a
            # transcript that had not yet announced itself when the banner was
            # first drawn (in which case the structured channel takes over and
            # this is dropped).
            if pending_scrape and now >= pending_scrape[2]:
                kind, text, _ = pending_scrape
                pending_scrape = None
                if CFG["scrape"] == "always" or not watcher.seen_any:
                    if kind == "stall":
                        if ctl.on_stall(text, now, "screen"):
                            # The line stays on screen after we have acted on it,
                            # and on a TUI it is repainted for as long as it is
                            # there. Forgetting the window is what keeps one
                            # refusal from reading as an endless run of them.
                            window = ""
                            notify("the server refused the turn; trying again shortly")
                    elif ctl.on_limit(text, now, "screen"):
                        window = ""
                        notify("usage limit detected; waiting for reset")

            action = ctl.tick(now)
            if ctl.handoff_text != handoff_watch_text:
                # The one place `main()` keeps the watcher's echo list in step
                # with the controller's: a fresh nonce (or the restart ending)
                # replaces `handoff_text` in exactly one spot (`_send_handoff`,
                # `_end_restart`), so comparing against it here is proof enough
                # that something changed, without main() having to know why.
                if handoff_watch_text:
                    watcher.forget(handoff_watch_text)
                handoff_watch_text = ctl.handoff_text
                if handoff_watch_text:
                    watcher.expect(handoff_watch_text)
            if action and action[0] == "inject":
                schedule_injection(action[1], action[2], now)
                notify(ctl.inject_note())
            elif action and action[0] == "resume":
                wait_cancelled()
            elif action and action[0] == "interrupt":
                # Esc stops a running codex turn; the turn_aborted row it writes is
                # what lets the fold go out.
                pending.append((now, b"\x1b"))
                notify(action[1])
            elif action and action[0] == "notify":
                notify(action[1])

            if pending:
                stay = []
                for due, payload in pending:
                    if due <= now:
                        write_all(master, payload)
                    else:
                        stay.append((due, payload))
                pending = stay

            if badge.enabled:
                badge.paint(stdout_fd, rows, cols, ctl.state,
                            max(0.0, ctl.wake_at - now), ctl.attempts,
                            CFG["max_attempts"], now,
                            # A half-received sequence of claude's is already in
                            # the terminal; anything written now lands inside it.
                            blocked=bool(esc_carry), deferred=ctl.deferred,
                            restart=ctl.rstate, context=ctl.badge_context(),
                            warn=ctl.badge_warn(now), context_estimated=ctl.context_estimated)

            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done, status = pid, 0
            if done == pid:
                exit_code = os.waitstatus_to_exitcode(status) if status else 0
                # Drain whatever claude printed on its way out before we tear the
                # pty down, or the last frame (including its exit message) is lost.
                try:
                    while True:
                        r, _, _ = select.select([master], [], [], 0.05)
                        if not r:
                            break
                        data = os.read(master, 65536)
                        if not data:
                            break
                        write_all(stdout_fd, data)
                except OSError:
                    pass
                break
    finally:
        if handoff_registry is not None:
            try:
                handoff_registry.unregister(os.getpid())
            except Exception:
                pass
        # AgentOverlay gets no erase() to match, even though T28 made the
        # primitive shared: Badge always owns a fixed corner, so its erase
        # blanks exactly the cells it last drew. A tree/panel row is not
        # fixed — by exit time the row an annotation was painted on may have
        # scrolled into history, been claimed by an unrelated later agent, or
        # gone back to plain conversation text, and blanking it from stale
        # (row, width) bookkeeping would overwrite whatever is there now
        # rather than the annotation. The annotations that remain are inert
        # text in the scrollback, same as T27 left them.
        try:
            badge.erase(stdout_fd, rows, cols)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGWINCH, old_winch)
        except Exception:
            pass
        for s, handler in old_sigs.items():
            try:
                signal.signal(s, handler)
            except Exception:
                pass
        if old_attr is not None:
            tcset(stdin_fd, old_attr)
        try:
            os.close(master)
        except Exception:
            pass
        # We may have left the loop because the pty reported EOF, which happens
        # before the child is reaped. Wait briefly so the real exit code is
        # reported instead of a default 0.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            except Exception:
                break
            if done == pid:
                exit_code = os.waitstatus_to_exitcode(status) if status else 0
                break
            time.sleep(0.02)
    log("exit: %d after %s" % (exit_code, human_left(time.time() - session_start)))
    return exit_code


if __name__ == "__main__":
    if sys.argv[1:2] == ["--cr-models"]:
        # A pure lookup, not a session: no pty, no fork, nothing to supervise.
        sys.exit(print_models_table())
    if sys.argv[1:2] == ["--cr-statusline"]:
        # Claude Code itself is the caller here, not a person: no pty, no
        # fork, and its own exit code is the only thing it ever looks at.
        sys.exit(run_statusline_proxy(sys.argv[2] if len(sys.argv) > 2 else ""))
    code = main(sys.argv[1:])
    # waitstatus_to_exitcode reports a signal death as a negative number, which
    # sys.exit would turn into 255. Report it the way a shell does.
    sys.exit(128 - code if code < 0 else code)
CR_PYTHON_EOF

CR_PAT_LIMIT=$(printf '%s\n' "${CR_LIMIT_PATTERNS[@]}")
CR_PAT_RESET=$(printf '%s\n' "${CR_RESET_PATTERNS[@]}")
CR_PAT_WORKING=$(printf '%s\n' "${CR_WORKING_PATTERNS[@]}")
CR_PAT_MENU=$(printf '%s\n' "${CR_MENU_PATTERNS[@]}")
CR_PAT_IGNORE=$(printf '%s\n' "${CR_IGNORE_PATTERNS[@]}")
CR_PAT_ROSTER=$(printf '%s\n' "${CR_ROSTER_PATTERNS[@]}")
CR_PAT_STALL=$(printf '%s\n' "${CR_STALL_PATTERNS[@]}")
CR_PAT_AGENT_ROW=$(printf '%s\n' "${CR_AGENT_ROW_PATTERNS[@]}")
CR_PAT_AGENTS_PANEL_ROW=$(printf '%s\n' "${CR_AGENTS_PANEL_ROW_PATTERNS[@]}")
export CR_PAT_LIMIT CR_PAT_RESET CR_PAT_WORKING CR_PAT_MENU CR_PAT_IGNORE CR_PAT_ROSTER
export CR_PAT_STALL CR_PAT_AGENT_ROW CR_PAT_AGENTS_PANEL_ROW

case "${1:-}" in
  --cr-dump-python)
    printf '%s\n' "$CR_PY"
    exit 0 ;;
  --cr-dump-patterns)
    # The test suite reads the pattern arrays from here rather than re-declaring
    # them, so a pattern can never be tested in a form the wrapper doesn't use.
    for _n in LIMIT RESET WORKING MENU IGNORE ROSTER STALL AGENT_ROW AGENTS_PANEL_ROW; do
      eval "printf '### CR_PAT_%s\n%s\n' \"\$_n\" \"\$CR_PAT_$_n\""
    done
    exit 0 ;;
  --cr-models)
    # A lookup, not a session — no claude/codex needs to be installed for
    # this. Whatever CR_* the caller already exported reaches python exactly
    # as it would for a real run: nothing here needs re-exporting.
    CR_PYTHON_BIN=$(cr_find_python) || {
      echo "agent-retrier: no usable python3 found" >&2
      exit 1
    }
    CR_MODELS_TMP=$(mktemp "${TMPDIR:-/tmp}/agent-retrier.XXXXXX") || {
      echo "agent-retrier: cannot write a temporary file" >&2
      exit 1
    }
    printf '%s' "$CR_PY" >"$CR_MODELS_TMP"
    "$CR_PYTHON_BIN" "$CR_MODELS_TMP" --cr-models
    CR_MODELS_RC=$?
    rm -f "$CR_MODELS_TMP"
    exit "$CR_MODELS_RC" ;;
  --cr-statusline)
    # Claude Code itself runs this, as the command `claude_launch_args` (T20)
    # put in `--settings`: no claude/codex needs to be installed for it, and
    # stdin/stdout are left exactly alone, since the JSON claude hands over
    # arrives on the former and the user's own statusline output has to leave
    # on the latter.
    CR_PYTHON_BIN=$(cr_find_python) || {
      echo "agent-retrier: no usable python3 found" >&2
      exit 1
    }
    CR_STATUSLINE_TMP=$(mktemp "${TMPDIR:-/tmp}/agent-retrier.XXXXXX") || {
      echo "agent-retrier: cannot write a temporary file" >&2
      exit 1
    }
    printf '%s' "$CR_PY" >"$CR_STATUSLINE_TMP"
    "$CR_PYTHON_BIN" "$CR_STATUSLINE_TMP" --cr-statusline "${2:-}"
    CR_STATUSLINE_RC=$?
    rm -f "$CR_STATUSLINE_TMP"
    exit "$CR_STATUSLINE_RC" ;;
esac

# ---- degrade paths: any of these and we run claude untouched -----------------
cr_resolve_cmd "$CR_CMD_SPEC" || {
  if [ "$CR_RESOLVE_ERR" = "timeout" ]; then
    echo "agent-retrier: your shell did not answer within ${CR_PROBE_TIMEOUT_SEC}s when asked" >&2
    echo "about '$CR_CMD_SPEC'. An rc file that prompts (zsh's compinit asks about insecure" >&2
    echo "directories) will do that. Name a path or a full command line instead, or raise" >&2
    echo "CR_PROBE_TIMEOUT_SEC." >&2
  elif [ "$CR_CMD_DEFAULTED" = 1 ]; then
    # The one failure agent-retrier --codex has on a machine with no codex.
    echo "$CR_PROG: codex not found on PATH — install it, or name yours with" >&2
    echo "--cmd (or CR_CODEX_CMD)" >&2
  elif [ -n "$CR_CMD_SPEC" ]; then
    echo "$CR_PROG: cannot run '$CR_CMD_SPEC' — not a runnable file, and your" >&2
    echo "shell does not know it as a command, alias or function" >&2
  else
    echo "agent-retrier: claude not found on PATH" >&2
  fi
  exit 127
}
CR_CLAUDE_RESOLVED="${CR_ARGV[0]}"

case "${1:-}" in
  --cr-dump-argv)
    # What we would exec, one element per line. Answers "which claude is this
    # actually going to run" without starting a session, and lets the tests
    # assert on the shape of the vector rather than on its side effects.
    printf '%s\n' "${CR_ARGV[@]}"
    exit 0 ;;
esac

if [ "${CR_DISABLE:-0}" = "1" ] || [ "${AGENT_RETRIER_ACTIVE:-0}" = "1" ]; then
  exec "${CR_ARGV[@]}" "$@"
fi

CR_PYTHON_BIN=$(cr_find_python) || exec "${CR_ARGV[@]}" "$@"

# `claude -p` is a batch run: no TUI to type into, and the exit code already
# tells the caller what happened. Nothing to supervise.
for arg in "$@"; do
  case "$arg" in
    -p|--print) exec "${CR_ARGV[@]}" "$@" ;;
  esac
done

# The same judgement, made about codex: most of its subcommands are not a
# session at all. `resume` and `fork` are, so they are not on this list.
cr_looks_like_codex() {
  case "$CR_AGENT" in
    codex) return 0 ;;
    claude) return 1 ;;
  esac
  case " $CR_CMD_SPEC ${CR_ARGV[*]} " in
    *[/\ ]codex\ *) return 0 ;;
  esac
  return 1
}

if cr_looks_like_codex; then
  # Every argument is looked at, not just the first one that is not a flag: the
  # subcommand can sit behind a flag that took a value (`codex --cd DIR exec`),
  # and stopping at that value would wrap a batch run. A prompt is one argument,
  # so `codex "exec the plan"` is still a session.
  for arg in "$@"; do
    case "$arg" in
      exec|login|logout|mcp|app|app-server|apply|completion|cloud-tasks|debug|doctor|sandbox|update|features|models|execpolicy|generate-ts)
        exec "${CR_ARGV[@]}" "$@" ;;
    esac
  done
fi

# The same judgement, made about claude: most of its subcommands are not a
# session either. `attach` opens one, and `agents` is the roster of OTHER
# sessions (handled inside the supervisor itself, not exec'd) — so neither is
# on this list.
cr_looks_like_claude() {
  case "$CR_AGENT" in
    claude) return 0 ;;
    codex) return 1 ;;
  esac
  case " $CR_CMD_SPEC ${CR_ARGV[*]} " in
    *[/\ ]claude\ *) return 0 ;;
  esac
  return 1
}

if cr_looks_like_claude; then
  # Same reasoning as the codex loop above: every argument is checked, so a
  # subcommand behind a flag that took a value (`claude --model opus stop abc`
  # — hypothetically) is still judged correctly, and a prompt that merely
  # mentions one of these words (`claude "fix the stop command"`) stays wrapped
  # because it is a single argument that matches none of them exactly.
  for arg in "$@"; do
    case "$arg" in
      auth|login|logout|setup-token|update|upgrade|install|doctor|mcp|plugin|plugins|project|logs|stop|kill|rm|respawn|gateway|import|ultrareview|--version|-v|--help|-h)
        exec "${CR_ARGV[@]}" "$@" ;;
    esac
  done
fi

# \037 (unit separator) rather than a newline: it cannot occur in a path, a
# command name, or anything a shell would accept as one.
CR_CLAUDE_ARGV=$(printf '%s\037' "${CR_ARGV[@]}")
export CR_CLAUDE_RESOLVED CR_CLAUDE_ARGV AGENT_RETRIER_ACTIVE=1
export CR_MESSAGE CR_MARGIN_SEC CR_MAX_ATTEMPTS CR_FALLBACK_WAIT_SEC CR_MAX_WAIT_SEC
export CR_USER_IDLE_SEC CR_BUSY_IDLE_SEC CR_VERIFY_SEC CR_SCRAPE CR_LOG CR_LOG_MAX_BYTES CR_LOG_KEEP CR_NOTIFY
export CR_RESUME_SEC
export CR_DRAFT_GRACE_SEC CR_TYPING_MAX_SEC
export CR_BADGE CR_BADGE_POS CR_BADGE_LABEL
export CR_AGENTS_OVERLAY CR_AGENTS_POS CR_AGENTS_POLL_SEC
export CR_WAIT_SCALE CR_POLL_SEC CR_SCRAPE_CONFIRM_SEC
export CR_AGENT
export CR_STALL_WAIT_SEC CR_STALL_BACKOFF CR_STALL_MAX_WAIT_SEC CR_STALL_MAX_ATTEMPTS
export CR_CONTEXT_TOKENS CR_CONTEXT_WINDOW CR_CONTEXT_RESTART
export CR_CODEX_CONTEXT_TOKENS
export CR_CODEX_HOLD_COMPACT CR_CODEX_RESERVE_TOKENS CR_CODEX_INTERRUPT CR_CODEX_LOGS_DB
export CR_CODEX_INTERRUPT_AFTER_SEC CR_CODEX_FOLDS_FILE CR_CODEX_RESERVE_ADAPT
export CR_UPDATE_CHECK CR_UPDATE_REPO CR_UPDATE_URL CR_UPDATE_BREW_FORMULA
export CR_UPDATE_CACHE CR_UPDATE_TTL_SEC CR_UPDATE_TIMEOUT_SEC CR_UPDATE_NOTICE_SEC
export CR_VERSION CR_SELF
export CR_MODEL_LOOKUP CR_MODEL_LOOKUP_TIMEOUT_SEC CR_MODEL_CACHE
export CR_MODEL_CACHE_TTL_SEC CR_MODELS_DOC_URL CR_MODELS_API_URL
export CR_HANDOFF_FILE CR_HANDOFF_MARKER CR_HANDOFF_MIN_BYTES CR_HANDOFF_ATTEMPTS
export CR_RESUME_ATTEMPTS
export CR_HANDOFF_REGISTRY_DIR
export CR_HANDOFF_MSG CR_CLEAR_CMD CR_RESUME_MSG CR_CANCEL_MSG
export CR_CLAUDE_HANDOFF_MSG CR_CODEX_HANDOFF_MSG
export CR_CLAUDE_CLEAR_CMD CR_CODEX_CLEAR_CMD
export CR_CLAUDE_RESUME_MSG CR_CODEX_RESUME_MSG
export CR_CLAUDE_CANCEL_MSG CR_CODEX_CANCEL_MSG
export CR_ROOT_IDLE_SEC CR_HANDOFF_TIMEOUT_SEC CR_STEP_GAP_SEC CR_CLEAR_SETTLE_SEC
export CR_CONTEXT_COOLDOWN_SEC CR_CONTEXT_MAX_CYCLES
export CR_CONTEXT_MIN_HEADROOM CR_CONTEXT_MAX_PER_HOUR CR_NOTIFY_REPEAT_SEC
export CR_SLASH_GAP_SEC CR_SLASH_ENTER CR_SLASH_ENTER_GAP_SEC

# The supervisor is handed over on a file descriptor rather than as an argument.
# Linux caps a SINGLE argument at 128 KiB (MAX_ARG_STRLEN, and no ulimit raises
# it) while macOS only caps the whole vector, so `-c "$CR_PY"` worked on one
# platform and stopped working on the other the moment this file grew past that
# — "Argument list too long", and every wrapped session degrading to nothing.
#
# /dev/fd/3 is read by python exactly like a script file, and the code never
# touches the disk. Where /dev/fd is not mounted (a bare chroot), a temp file is
# the fallback; the supervisor unlinks it as its first act, so it lives for the
# length of one exec and belongs to nobody afterwards.
# auto | fd | tmp — the tests drive both routes; nobody else needs to.
: "${CR_PY_VIA:=auto}"
if [ "$CR_PY_VIA" != "tmp" ] && [ -d /dev/fd ]; then
  exec "$CR_PYTHON_BIN" /dev/fd/3 "$@" 3< <(printf '%s' "$CR_PY")
fi

CR_PY_TMP=$(mktemp "${TMPDIR:-/tmp}/agent-retrier.XXXXXX") || {
  echo "agent-retrier: cannot write a temporary file; running claude unwrapped" >&2
  exec "${CR_ARGV[@]}" "$@"
}
printf '%s' "$CR_PY" >"$CR_PY_TMP"
export CR_PY_TMP
exec "$CR_PYTHON_BIN" "$CR_PY_TMP" "$@"
