# Changelog

Every released version, newest first. The section for a tag is what GitHub shows
as that release's notes — `.github/workflows/release.yml` reads it straight out
of this file, so a release cannot describe itself differently from here.

The version in `agent-retrier.sh` (`CR_VERSION`) must match the newest entry
below; the test suite checks it.

## [3.0.2] - 2026-10-05

### Fixed
- A session cleared by hand while the fold waited is no longer cleared a
  second time. Once the handoff was verified ("folded"), a `/clear` typed by
  the person was not recognised as one: the wrapper went on to send its own,
  wiping the turn they had just started in the fresh session, and typed the
  resume phrase over it. A new session id from claude's own registry at that
  point now counts as the clear; if the person then submits a prompt of their
  own, the restart is finished and nothing is unfolded, and if they don't, the
  session is unfolded once both the keyboard and the new transcript go quiet.
- The draft gate no longer sees text in an empty prompt box on terminals that
  speak the kitty keyboard protocol or modifyOtherKeys (Ghostty, cmux, kitty,
  WezTerm), which Claude Code turns on. Esc, Ctrl+C, Ctrl+U, Ctrl+W and
  Alt+Backspace arrive there as escape sequences and were not counted as
  clearing the box, so a restart could sit at "folded" for up to
  `CR_DRAFT_GRACE_SEC` (10 minutes) behind a draft that was already gone.

## [3.0.1] - 2026-09-26

### Changed
- One name, a flag per agent. `agent-retrier --claude` and
  `agent-retrier --codex` pick the agent (claude when neither is given), and
  `agent-retrier-claude` / `agent-retrier-codex` are the same file with the
  flag built into the name — the install puts both next to `agent-retrier`.
  An explicit `--cmd` or `--agent` still wins, in any order; of `--claude` and
  `--codex`, the last one given wins.

### Removed
- The `codex-retrier` name: use `agent-retrier-codex` or `agent-retrier --codex`.

### Fixed
- The screen model the wrapper keeps of the session (what it reads prompts,
  menus and the badge's corner from) went wrong on output Claude Code really
  sends: an escape sequence split across two reads was printed as text
  (`38;2;153;153;153m`), `ESC ( B` left a stray `B`, `CSI S`/`CSI T` scrolls
  were ignored, and Claude Code's keyboard setup (`CSI > 4;2 m`,
  `CSI < u`) was taken for a colour change and a cursor restore.
- The README's demo GIF is re-recorded: English throughout, 16:10, and
  rendered glyph by glyph on the cell grid with a fallback font.

## [3.0.0] - 2026-09-26

### Changed
- **Renamed to agent-retrier.** The project has wrapped codex as well as
  Claude Code for a while, so the name now says so. Everything that carried
  the old name moved with it:
  - the command is `agent-retrier` (`codex-retrier` is unchanged), and the
    script is `agent-retrier.sh`;
  - the repository is `github.com/a0s/agent-retrier`, the Homebrew tap is
    `a0s/agent-retrier` (formula `agent-retrier`);
  - the state directory is `~/.agent-retrier/` (log, sessions, status, folds);
    the per-project handoff files live in `.agent-retrier/`;
  - the nesting guard is `AGENT_RETRIER_ACTIVE`.
- Upgrading from the old name: see "Upgrading from the old name" in the
  README.

## [2.0.2] - 2026-09-23

### Fixed
- A context restart could hang for good after a perfect handoff. When the
  fold phrase was typed while a turn was still running, Claude Code queued
  it and wrote it into the transcript as a `queued_command` attachment
  rather than an ordinary user row; the wrapper never recognised that as
  the phrase arriving, retyped it twice (making the model rewrite the
  handoff each time), and then waited on that recognition with no timeout,
  so `/clear` never went out. Now:
  - a handoff file that ends with the current attempt's marker is itself
    proof the phrase reached the session — the marker is fresh per attempt
    and typed into this terminal only;
  - a `queued_command` row counts as the phrase being delivered, and a
    phrase seen waiting in Claude Code's queue is not retyped;
  - every wait in the handoff step is bounded by `CR_HANDOFF_TIMEOUT_SEC`.
- Because a marked file proves the model was told to wrap up, an abort
  after one (the turn cut off at `max_tokens`, refused, or a file under
  `CR_HANDOFF_MIN_BYTES`) now sends `CR_CANCEL_MSG` instead of leaving the
  session on that instruction.

## [2.0.1] - 2026-09-20

### Fixed
- `agent-retrier attach <id>` could start a new prompt instead of attaching
  to the named background session whenever context restart was armed. The
  statusline proxy added `--settings` before `attach`, but current Claude Code
  only recognizes that subcommand as the first token. Attach launches now keep
  their original argument vector; the statusline proxy is skipped for them.

## [2.0.0] - 2026-09-19

**Breaking:** `CR_CONTEXT_PCT` and `CR_CODEX_CONTEXT_PCT` are removed. A
session that still has either set in its environment now refuses to start,
with a message naming the replacement — this is deliberate: silently
ignoring the variable would leave a session that used to be protected
running with no restart armed at all. A percentage of a window meant a
different thing on each agent, and nothing at all until you knew the window;
now that every model has its own row in the profile table (`--cr-models`),
the percentage was a redundant, misleading layer on top of it. Replace it
with one of:
- `CR_CONTEXT_RESTART=1` — arms the restart at each model's own row (or, for
  a model the table does not describe, `DEFAULT_RESTART_PCT` of whatever
  window that session actually has — no model is left unprotected just
  because this build predates it).
- `CR_CONTEXT_TOKENS=510k` — one absolute number, same as before.
- `CR_CLAUDE_TOKENS_<SLUG>=510k` / `CR_CODEX_TOKENS_<SLUG>=510k` — one model
  only.

See `docs/context-restart.md#choosing-a-threshold`.

### Removed
- `CR_CONTEXT_PCT`, `CR_CODEX_CONTEXT_PCT`, and the `context_pct`/
  `codex_context_pct` config keys built from them. The threshold is now
  purely token-based: explicit, from the model's own profile row, or
  computed from that row's own percentage applied to the session's actual
  window.

### Fixed
- The echo check that gates `/clear` (T06) built its search key with
  `json.dumps(text)`, which ASCII-escapes any non-ASCII character into a
  `\uXXXX` sequence — but real Claude Code and codex-cli write that same
  character as its raw UTF-8 bytes, never escaped. The default
  `CR_HANDOFF_MSG` contains an em dash, so this silently broke the echo
  check for every restart using the shipped default: the file was written
  correctly, the turn closed with `end_turn`, and the wrapper still sat
  waiting for an echo that could never match, forever. Found recording the
  `docs/demo/` GIFs against real Claude Code — `fake_claude.py`/
  `fake_codex.py` used the same (wrong) encoding as the code they were
  testing, so the mismatch never showed up against the test suite's own
  fakes. Any custom phrase with a non-ASCII character (Cyrillic, an
  accented letter, an em dash) was equally affected.

## [1.12.0] - 2026-09-19

Two sessions in one project used to be indistinguishable — the wrapper would
fold on a neighbour's numbers, clobber its handoff file, or lose an unfold to
whichever transcript grew last. This release gives every session a
fingerprint of its own (a registered `sessionId`/rollout, a unique handoff
file, a fold phrase the wrapper waits to see echoed back) and makes every
signal it reacts to — usage, "still writing", "session is responding again"
— provably about that session and no other. On top of that identity, the
restart machine itself is now a closed loop: `/clear` is confirmed before the
resume phrase goes out, the resume phrase is retried and never silently
dropped, and an abort after a delivered fold no longer strands the model on
an empty prompt. Models get a real per-model table instead of one global
percentage, and the subagent tree finally shows what each agent is actually
running and costing.

### Fixed
- Two sessions in the same project could fold on each other's context
  numbers, write over each other's handoff file, or accept a foreign
  session's "still writing"/"idle again" signal, because `TranscriptWatcher`
  picked "the current file" by which one grew last. Claude sessions are now
  bound through `~/.claude/sessions/<pid>.json` (`ClaudeSessionRegistry`),
  codex sessions through the rollout that echoes our own nonce back
  (`Controller.on_candidates` holds the restart back while more than one
  unconfirmed rollout exists), `bind_transcript` is the only place the bound
  path ever changes, and every callback (echo, alive, limit, context) drops
  anything not on `watcher.current`. The handoff file is unique per session
  (`CR_HANDOFF_FILE`'s `{id}`, or a `HandoffRegistry` suffix on collision),
  and the fold phrase itself is only considered delivered once its nonce
  comes back through the transcript — not after a timeout.
- `/clear` used to flip to `CLEARED` on a timer, so a slow or dropped clear
  meant typing the resume phrase into a session that had not actually
  cleared, and a resume phrase that never landed (a codex `$skill` popup
  eating the first Enter, most often) silently left the session empty
  forever. `/clear` now waits for confirmation before `CLEARED`, the resume
  phrase retries with growing gaps up to `CR_RESUME_ATTEMPTS` before landing
  in a red `UNFOLD_FAILED` badge instead of quietly giving up, and codex
  self-compacting mid-race no longer aborts a fold that had already landed —
  it re-probes the handoff file and drops straight into `CLEARED`.
- Aborting a restart after the fold phrase had already been echoed back used
  to leave the session as-is, with the model already told to wrap up and
  start nothing new — an unattended session stuck on an empty prompt. A
  `CANCEL_PENDING` state now sends `CR_CANCEL_MSG` through the same
  identity-bound gates before ending the restart.
- Claude Code's `<synthetic>`/all-zero-usage rows (a turn with no real
  response) used to reset the badge to 0, look like an unfamiliar model, and
  make a completed `/clear` indistinguishable from one still in flight;
  they're now recognized and ignored. Collapsing consecutive assistant rows
  could also drop a real `end_turn` under a same-poll streaming fragment, or
  merge a root row into a sidechain one — aborting an already-landed fold
  with no unfold, or dropping the observation for that poll entirely. Rows
  now only merge when they agree on `sidechain`, and a later `None`
  `stop_reason` never overwrites a real one.
- An unfamiliar model slug used to disarm the context-restart trigger
  entirely (`cr window?`) — exactly when a new model ships and the table is
  stalest. It now falls back to an estimate (same-family for claude, the
  agent's own cache for codex), keeps asking the network in the background,
  and self-corrects from the agent's own compaction point in both
  directions. A small window's baseline cost (system prompt, `CLAUDE.md`,
  MCP tool defs, the resume read) alone used to eat most of a fresh session's
  headroom and fire the next restart 30-40 minutes later; `context_limit` is
  now raised to a baseline-aware floor after every restart, and more than
  `CR_CONTEXT_MAX_PER_HOUR` restarts inside an hour switches the trigger off
  for the session instead of thrashing.
- Codex inherited claude's 51% restart default even though codex folds
  against a hard compaction cap, not a fraction of its window; it now
  defaults to codex's own 90% soft-cap semantics, with the reserve line
  (raised from 32k to 64k after a live fold burned through the old one) doing
  the real work. A session's effective window (`[1m]`, a custom
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS`) is also no longer guessed at: a
  `--cr-statusline` proxy reads it straight from Claude Code's own statusLine
  data.
- `claude stop <id>`, `claude mcp list`, `claude update` and similar control
  subcommands were launched under the pty supervisor like a real session
  (sleeping for the update-notice delay, writing `start:`/`exit:`, even
  having "continue" injected into their output); they now exec directly
  instead.
- Two wrapper sessions writing into one shared `CR_LOG` produced
  indistinguishable lines; every line is now tagged `[cr <pid> <agent>]`,
  with `start:`/`exit:` carrying cwd and session duration.

### Added
- `MODEL_PROFILES`: one `(window, restart_at, compact_at)` per model, for
  both agents, replacing the scattered window table and single global
  restart percentage (`--cr-models` prints the resolved table). Switching
  models mid-session is recognized in real time (`Controller.on_model`) and
  recomputes the threshold immediately rather than on the next restart.
- Codex rollouts are tracked the same way old sessions are exposed to
  `codex resume`: `CodexAgent.paths` now also walks recently-touched files
  under `sessions/`, not just the one rollout created at startup.
- `CR_CODEX_INTERRUPT_AFTER_SEC` interrupts a codex turn that has simply sat
  past the ordinary threshold too long, instead of only ever restarting at
  the hard cap-minus-reserve line. Fold cost is now logged and accumulated
  (`~/.agent-retrier/folds.json`), and `CR_CODEX_RESERVE_ADAPT=1` raises the
  effective reserve to 1.25x the largest observed cost.
- The badge now ranks its warnings by severity — an unfold failure or a
  restart switched off for the session stays on screen (repeating every
  `CR_NOTIFY_REPEAT_SEC`) instead of fading behind a passing `~est` — and
  marks an unconfirmed window estimate with `~`.
- `CR_CLAUDE_<X>`/`CR_CODEX_<X>` override `CR_HANDOFF_MSG`/`CR_CLEAR_CMD`/
  `CR_RESUME_MSG` for one agent only, since a skill invoked as `/foo` means
  nothing to codex. `CR_CONTEXT_RESTART=1` alone now arms a sane default
  threshold without anyone picking a percentage first, and
  `CR_CLAUDE_TOKENS_<SLUG>`/`CR_CODEX_TOKENS_<SLUG>` set one for a single
  model by name.
- `CR_AGENTS_OVERLAY=1` renders the subagent tree's own terminal output
  through a small screen emulator and paints each agent's real model
  (`sonnet-5/?`, read only from its own transcript, never the spawn request)
  at the row's right edge — including on the persistent `/tasks` panel, not
  only the live tree. `Badge` and the overlay now share one row-paint
  primitive and an occupied-row registry instead of two copies of the same
  column arithmetic.
- Log rotation is now concurrency-safe under two wrapper sessions.
- Test infrastructure for exactly the class of bug this release fixes:
  `helper.two_wrappers` runs two wrappers over one project dir at once,
  `fake_claude`/`fake_codex` gained scenario scripting (growth, dropped
  phrases, synthetic rows, popups, backdated rollouts), and `run.sh` now
  fails with `FAILURES` if any wrapped process is still alive afterward.

## [1.11.0] - 2026-09-14

The context restart never once worked on codex, and now does — with a threshold
of its own, and ahead of codex's own compaction rather than behind it. And
`codex-retrier` is `agent-retrier` with codex as the default.

### Fixed
- The context restart on codex asked for a handoff, got a good one, and then
  sat on it until the step timed out: the check that the folding turn ended
  cleanly waited for a `stop_reason` of `end_turn`, which is a Claude Code field
  that codex never writes. The turn's ending is now read off codex's own rows —
  `task_complete` is a clean end, `turn_aborted` (Esc) is not — and the whole
  fold, `/clear` and unfold have been run against a live codex-cli 0.154.
- A codex session is mid-turn from `task_started` until the row that closes it.
  It used to be "mid-turn until the rollout goes quiet for 20 seconds", which a
  root waiting minutes on its agents satisfies while its turn is still running.
- A codex model name is no longer sent to Anthropic's models table to be sized.
  The rollout states the window, now from the first row of a turn, and Claude
  Code's `CLAUDE_CODE_MAX_CONTEXT_TOKENS` and `CLAUDE_CODE_DISABLE_1M_CONTEXT`
  no longer narrow a codex window.
- The percentage in the corner is rounded rather than truncated, so it agrees
  with the status line under it.

### Added
- The restart on codex gets there before codex's own compaction. codex compacts
  at 90% of its raw window (244.8k of 272k), on a count that runs ~18k ahead of
  anything the rollout shows, and in the middle of a turn — a threshold read off
  the rollout loses that race. With the restart on, codex is now started with
  `-c model_auto_compact_token_limit_scope="body_after_prefix"` and a huge
  limit, which leaves only its hard cap at 258.4k; the wrapper reads the count
  and the cap codex itself logs to `logs_2.sqlite`; the threshold never goes
  past the cap minus `CR_CODEX_RESERVE_TOKENS` (32k); and a turn still running
  past that line is interrupted with Esc and folded up. Checked against a live
  codex-cli 0.154: the turn was aborted mid-task, the handoff recorded the step
  still in flight, and the new thread started with the threshold held back
  again. `CR_CODEX_HOLD_COMPACT=0` and `CR_CODEX_INTERRUPT=0` turn the two
  halves off; a compaction that happens anyway is logged.
- `codex-retrier`: the same file under a second name, with codex as its default
  — `CR_CODEX_CMD` or plain `codex`, never the `CR_CLAUDE_CMD` meant for claude.
  Installed alongside `agent-retrier` whatever agents the machine has; without
  codex it says `codex not found on PATH` and exits 127.
- codex has context thresholds of its own: `CR_CODEX_CONTEXT_PCT` and
  `CR_CODEX_CONTEXT_TOKENS`. The percentage defaults to claude's; the absolute
  count never does, since a count picked for a 1M window says nothing about a
  258k one. The percentage is counted the way codex's status line
  counts `Context N% used`, leaving out the 12,000 tokens it treats as a
  baseline, so 60% fires when that line says 60% rather than a few points early.

## [1.10.0] - 2026-09-12

A model this file had never heard of was assumed to be small, and a session that
was 12% full got folded and cleared for it. Also: the wrapper now notices when
there is a newer one of itself.

### Fixed
- A point release is its family's window. `claude-fable-5-1` was not in the
  table — `claude-fable-5` is — so it read as an unfamiliar slug, the window was
  assumed to be 200k, and a 1M session sitting at 118k was called 59% full and
  folded. Twice, on two different days. The slug now drops its trailing version
  segments until something in the table matches, and stops at
  `claude-<family>-<major>`, which is far enough for a point release and not far
  enough to hand `claude-opus-4-9` the window of some other opus.
- A window that is genuinely unknown is no longer invented. Assuming the small
  window was chosen as the safe direction, on the grounds that a restart which
  never fires is invisible; in practice an unfamiliar slug means a NEW model,
  which means a large one, and the wrong guess does not fail quietly — it types
  into a live session and throws its history away. Nothing is assumed now: the
  percentage trigger stays disarmed, the corner says `cr window?`, and the log
  says what to set. `CR_CONTEXT_TOKENS` was never affected, as it needs no
  window.

- The supervisor is handed to python on a file descriptor instead of as an
  argument. Linux caps a single argument at 128 KiB and nothing raises that
  limit; the embedded Python crossed it in this release and every wrapped
  session on Linux died with "Argument list too long" — which is to say
  degraded to no wrapper at all, silently. macOS caps only the whole vector, so
  the suite was green there and red on the other half of the matrix. Where
  `/dev/fd` is not mounted the fallback is a temp file the supervisor unlinks as
  its first act. Both routes are covered by tests now.

### Added
- It says when a newer release exists, the way claude and codex do — two dim
  lines before the session starts, naming both versions (`1.9.0 → 1.10.0`) and
  the command that updates the copy you are actually running: `brew upgrade` for
  a cellar install, `git -C <clone> pull` for a clone, the releases page for a
  loose file. No session ever waits on that check: what is printed comes from a
  cache the previous run left behind, and the fetch that refreshes it happens in
  the background once claude is already up — a failed check included, so a
  machine with no network does not ask on every launch. `CR_UPDATE_CHECK=0`
  turns it off; `CR_UPDATE_NOTICE_SEC` is how long the notice stays.
- The wrapper looks a model up instead of guessing at it. An unfamiliar slug is
  asked about in a worker thread — the Models API when `ANTHROPIC_API_KEY` is
  set, the published models table otherwise, which needs no credentials — and
  the answer arms the trigger without a restart. It is cached in
  `CR_MODEL_CACHE` for a week, and the log names the build as the thing that is
  out of date. `CR_MODEL_LOOKUP=0` keeps the wrapper offline as before.

## [1.9.0] - 2026-08-30

`claude agents` again, from the other side: not the roster's own screen this
time, but a session opened from it.

### Fixed
- Wrapping `claude agents` no longer reads the screen at all. 1.6.0 taught the
  scraper to recognise a roster by what is drawn on it, which holds exactly as
  long as the roster is what is drawn: open a card and that session's history
  scrolls past — old banners included — with the header the check keys on gone.
  A neighbour's `resets 2:10am` was taken for this terminal's own, the corner
  counted down `11h18m`, and the limit actually in force lifted seven minutes
  later. The subcommand is known at launch, so the screen channel is switched
  off for the run; the roster has no input box to type `continue` into either.
  `CR_SCRAPE=always` still means always.
- A wait the screen scheduled can now be talked out of it. The scraper only ran
  while the controller was idle, so a wrong banner scheduled a wait that no
  channel could correct — and a roster has no transcript of its own to correct
  it from. The screen stays readable during such a wait, and a banner stating an
  EARLIER reset takes over. Only earlier: a later one is how a stale card would
  push the wake-up out forever. A wait the transcript scheduled is this
  session's own and is not second-guessed; a banner turned down once is not
  weighed again. Killing the process was previously the only way out.

## [1.8.0] - 2026-08-28

A second agent, and a second way a session stops without being finished.

### Added
- **codex support.** `--agent codex` (or `CR_AGENT=codex`) wraps `codex`
  instead of `claude`; left on `auto`, the default, the wrapper works out
  which one from the command it is given, so `agent-retrier --cmd codex`
  needs nothing else. `--cr-agent` is the same flag under the prefix the
  other wrapper options carry.

  codex writes one JSONL "rollout" per thread under
  `$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/`, and it states outright several
  things Claude Code's transcript only implies: where a turn started and
  ended, the exact size of the context window (`model_context_window`), what
  the last request was sent with, and the reason a turn ended. So on codex
  the context window is read rather than guessed from a model name, and
  `CR_CONTEXT_WINDOW` is not needed.

  Every project's rollouts share one tree, and a codex subagent gets a
  rollout of its own. The wrapper reads only the rollout whose head says
  `thread_source: user` and whose `cwd` is the directory it is running in —
  reading a subagent's would report a limit this terminal never hit, and a
  context that is not ours to restart. When codex is given `--cd`, that
  directory is the one followed.

  Most of codex's subcommands are not a session at all (`codex exec`, `codex
  login`, `codex mcp`, …), and the wrapper execs codex unchanged for those,
  the way it already does for `claude -p`. `codex resume` and `codex fork`
  are sessions, and are wrapped.

- **The stall.** A limit says when it lifts. The other way a session stops
  says nothing: the server refuses the turn — `Selected model is at
  capacity. Please try a different model.` — and nothing schedules a way
  back. On codex that refusal ends the whole turn, which takes every agent
  the session was running down with it, and leaves the session sitting at an
  idle prompt with no sign anything is wrong.

  So the wrapper waits a minute and types `continue`, the same thing a person
  would do. Repeat refusals double the wait — 60s, 120s, 240s — up to
  `CR_STALL_MAX_WAIT_SEC`, because a service that has just said it is full
  does not want to be asked again every minute. Any turn that finishes ends
  the streak, and the next stall starts at a minute again. All the usual
  gates still apply: nothing is typed while the session is mid-turn or while
  there is an unsent draft in the prompt box.

  Detection wordings for a refused turn live in their own array,
  `CR_STALL_PATTERNS`, kept narrow on purpose: a wrong stall is a message
  typed into a live session for no reason.

- `CR_AGENT`, `CR_STALL_WAIT_SEC`, `CR_STALL_BACKOFF`, `CR_STALL_MAX_WAIT_SEC`
  and `CR_STALL_MAX_ATTEMPTS`, all listed in the README.

## [1.7.0] - 2026-08-21

A second trigger. The first one answers "the quota ran out"; this one answers
"the context window is filling up" — and it is off until you switch it on.

### Added
- **Context restart.** At `CR_CONTEXT_PCT` of the model's context window the
  wrapper asks Claude to fold the session into a file, checks that the file
  really was written, types `/clear`, and hands the file to the fresh session.
  It automates a ritual people were already doing by hand, several times a day,
  at whatever moment they happened to notice the window filling up.

  Nothing is counted on our side: every assistant row in the transcript carries
  the API's own `usage`, and `input + cache_creation + cache_read` is the prompt
  that was sent. The check rides on the row the watcher has already parsed, so
  it costs two comparisons and happens only when the file actually grew. The
  denominator comes from the model slug in the same row, narrowed by
  `CLAUDE_CODE_DISABLE_1M_CONTEXT` or `CLAUDE_CODE_MAX_CONTEXT_TOKENS` if either
  is set, and raised if the session is ever seen past it.

  `/clear` throws a session's history away, so it goes out only when four
  independent things agree: the handoff file ends with a one-time marker that
  was generated for this attempt (proof the write ran to the end, not that the
  model believes it did); the runtime recorded the turn as `end_turn` rather
  than `max_tokens` or `refusal`; the file is newer than the request and over a
  size floor; and the session's own transcript has stopped growing. Any of them
  missing and the fold is asked for again, then abandoned — with the session
  left exactly as it was, because falling back on Claude Code's own compaction
  is a far better outcome than a history cleared on a promise.

  A usage limit landing in the middle suspends the restart instead of cancelling
  it: every clock it runs on stops for the duration, its attempt budget is not
  spent on the world's problems, and when the quota returns the step that was
  interrupted is re-sent — `continue`, which is what the limit path types, is
  the wrong instruction at every step of a restart but the last.

  Off by default (`CR_CONTEXT_PCT=0`). Existing installs behave exactly as they
  did.

- `CR_HANDOFF_FILE`, `CR_HANDOFF_MSG`, `CR_RESUME_MSG`, `CR_CONTEXT_WINDOW`,
  `CR_CONTEXT_TOKENS`, `CR_HANDOFF_ATTEMPTS`, `CR_ROOT_IDLE_SEC`,
  `CR_CONTEXT_COOLDOWN_SEC`, `CR_CONTEXT_MAX_CYCLES` and the rest of the knobs,
  all listed in the README.

- The corner badge shows the context percentage once the threshold is in sight,
  and names the step while a restart is running. It is the only way to find out
  whether the threshold you picked is a sensible one.

### Changed
- A message beginning with `/` is typed as a command rather than as prose:
  a longer pause after the text, so Claude Code's command list can settle on the
  exact match, and two Enters. The second submits if the first only completed
  the highlighted entry, and lands in an empty input box if it did not — where
  Claude Code ignores it. `CR_SLASH_ENTER=1` for a build that does not need it.
- The transcript watcher says which file each row came from, and which of the
  growing files belongs to the session at this terminal. A limit is the
  account's and never cared; a context reading taken from another session's
  transcript would be a restart at the wrong moment, or none at all.
- A subagent's rows are marked as such. Its work is the session's work, so it
  holds a restart back — but its context is its own and is not read as the
  session's.

## [1.6.0] - 2026-08-06

What `claude agents` did to the scraper, found in a session that sat out a
five-hour wait for a limit that had lifted five minutes after it started.

### Fixed
- The roster (`claude agents`) painted another session's three-hour-old card —
  `You've hit your session limit · resets 9:30pm … 3h` — and the wrapper read it
  as its own live banner, parking the terminal until half past nine while the
  real reset (3:40pm) came and went. A roster is a list of OTHER sessions and
  has no input box worth typing `continue` into, so a screen showing one is no
  longer scraped at all. The transcript channel is untouched: a limit hit by
  this session's own claude still arrives structured.
- A full-screen TUI positions the cursor instead of printing rows, and the
  roster reached the scraper with not one `\n` in it. Stripping the escapes left
  the whole screen as a single line, where any limit wording pairs with any
  reset wording — that is how a card's clock time was attached to a different
  card's banner. Cursor motions and carriage returns are now line breaks, so the
  pairing rule sees the rows the eye sees.
- The badge froze. It waits for a gap in claude's output before painting over a
  finished frame, and the roster animates about ten times a second, so the gap
  never came and the corner kept whichever frame it drew first — a countdown
  that does not count, which reads as a dead wrapper. The wait for the gap is
  now bounded (2s), after which it paints into the traffic and lets the next
  repaint tidy up.

## [1.5.0] - 2026-08-04

The other half of the 1.3.0 fix, found the same way: a session that had waited
out a four-hour limit, reached the reset, and then never typed anything.

### Fixed
- `deferring retry: user is typing` once every 15 seconds while nobody was
  touching the keyboard. 1.3.0 taught the wrapper that the terminal's replies to
  claude are not a draft, but every one of them still stamped "the human is
  here", and that gate wants twenty quiet seconds. A cursor-position report, a
  focus event from switching windows, a mouse move over the terminal — any of
  them, arriving oftener than that, held the retry for as long as the session
  lived. Presence now means a key: replies are told from keystrokes by shape
  (the string replies, `CSI…R`, a private `CSI ?…`, `CSI…c`, `CSI…t`, focus and
  mouse reports, the paste brackets), and everything unrecognised stays human,
  because mistaking a key for a report is the expensive direction.
- The keyboard gate now has a ceiling (`CR_TYPING_MAX_SEC`, 15 minutes). With an
  empty input box there is no half-written thought behind it, only the courtesy
  of not typing while someone is — and whatever stamps presence next, the retry
  still has to happen. A draft is untouched by this and keeps its own, longer
  grace.

### Changed
- The badge says `◆ cr held` while a retry is being deferred. Each deferral
  re-arms the clock by 15 seconds, so what the corner used to show was a
  countdown restarting from 15s forever — indistinguishable from waiting out a
  quota, which is the one thing it no longer meant.
- A narrower badge frame now covers the wider one it replaces. `◇ cr 1m` giving
  way to `◆ cr` left `◇ c◆ cr` in the corner until claude next repainted that
  row.

## [1.4.0] - 2026-08-03

### Fixed
- A weekly limit was detected, the account was switched with `/login`, and the
  session went back to work — while the wrapper kept counting down its 48 hours
  over it, ready to type `continue` into the middle of that work. Nothing
  announces "your quota is back": not the transcript, not the render. What says
  it is that claude is answering again, so a wait now ends on either of two
  sightings — an ordinary assistant record in the transcript, or the working
  footer running for `CR_RESUME_SEC` (15s) without a break. A refused turn still
  paints a second or two of footer before the banner lands, which is why a
  flicker is not enough, and rows written within eight seconds of the banner are
  taken as belonging to it.
- Cancelling a wait clears the rolling screen window with it. The banner is
  still in there, and it would otherwise be re-detected the moment the
  controller went idle and park the session all over again.

## [1.3.0] - 2026-08-02

Two field failures from the same evening, both of them the wrapper reading the
terminal too literally.

### Fixed
- A session waited out its reset and then deferred the retry every 15 seconds
  for three hours, logging `unsent text in the prompt box` while nobody had
  touched the keyboard. Not everything arriving on our stdin was typed: claude
  sends `\x1b[>q` at startup and the terminal's XTVERSION reply comes back the
  same way, where 15 of its bytes were counted as a draft. Only CSI and SS3 end
  at their first final byte — DCS, OSC, APC, PM and SOS run to a String
  Terminator, and an X10 mouse report carries three raw coordinate bytes after
  its `M`. All of them are now consumed whole, including when a read boundary
  cuts one in half.
- A draft nobody has touched for `CR_DRAFT_GRACE_SEC` (10 minutes) no longer
  blocks a retry. Whatever desyncs that counter next, the wait has to end.
- The retry landed, claude resumed — and the wrapper typed `continue` twice more
  and painted `cr stopped`. It was watching the footer for `esc to interrupt`,
  which Claude Code 2.1 no longer prints: the footer is now `✶ Nebulizing… `
  growing into `✻ Cogitating… 20m 57s · ↓ 6.8k tokens`. Every live session
  therefore looked idle.

### Added
- The transcript now confirms a retry as well as reporting a limit. claude
  writes our message back as a `user` row the moment it accepts it, which is
  direct proof the retry was submitted rather than left sitting in the input
  box — the question the verify step was trying to answer by reading pixels.
- `CR_DRAFT_GRACE_SEC`.

## [1.2.0] - 2026-08-01

### Added
- A dim badge in a corner of the terminal: `◆ cr` while the session is being
  watched, a countdown while a limit is being waited out, and the attempt number
  while a retry is being confirmed. It is painted over Claude's finished frame in
  the gaps between repaints — nothing is reserved from the TUI, the cursor and
  colour are restored around every write, and the last column is left empty so it
  can never wrap the screen. `CR_BADGE=0` turns it off; `CR_BADGE_POS` picks the
  corner and `CR_BADGE_LABEL` the word.
- `docs/badge-shot.py`, which regenerates the README picture by running the real
  wrapper and rendering what it actually wrote to the terminal.

### Fixed
- The suite no longer measures the caller's session: `CR_*` and
  `AGENT_RETRIER_ACTIVE` are stripped from the environment before each wrapper
  under test is started. Running `./test/run.sh` from inside a wrapped session
  used to make every wrapper degrade to a plain exec and the tests pass or fail
  for the wrong reason.

### Internal
- `test/screen.py`: a small terminal emulator, so end-to-end tests can assert on
  the screen a user would see rather than on the byte stream.

## [1.1.0] - 2026-08-01

### Added
- `--cmd` (and `CR_CLAUDE_CMD`): run *your* claude — a binary, a path, a name on
  PATH, an rc-file alias, a shell function, or a whole command line.
- `--cr-dump-argv`, which prints exactly what will be executed.

### Fixed
- An interactive shell is no longer left inside the pty: the alias or function is
  lifted out once, at startup, and the session runs from a plain shell.
- A shell whose rc file asks a question can no longer hang the wrapper forever;
  the probe times out and says what to do about it.

## [1.0.0] - 2026-08-01

### Added
- Single-file, tmux-free auto-resume for Claude Code: the wrapper runs claude on
  a pty it owns, detects a usage limit through the transcript (primary) or the
  screen (fallback), waits out the stated reset, and types `continue` once the
  session is idle and the human is not mid-sentence.

[1.2.0]: https://github.com/a0s/agent-retrier/releases/tag/v1.2.0
[1.1.0]: https://github.com/a0s/agent-retrier/releases/tag/v1.1.0
[1.0.0]: https://github.com/a0s/agent-retrier/releases/tag/v1.0.0
