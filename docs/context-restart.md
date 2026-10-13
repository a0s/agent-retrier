# Restarting a full context

Opt-in: at a threshold you pick, the wrapper asks the session for a handoff
file, verifies it, runs `/clear` and points the fresh session at the file.

[← back to README](../README.md)

- [Turning it on](#turning-it-on)
- [What you will see](#what-you-will-see)
- [The four steps](#the-four-steps)
- [Which session is mine](#which-session-is-mine)
- [Why `/clear` is the last thing it will do](#why-clear-is-the-last-thing-it-will-do)
- [Writing your own phrases](#writing-your-own-phrases)
- [Choosing a threshold](#choosing-a-threshold)
- [The context window](#the-context-window)
- [`[1m]` vs 200k: the statusline proxy](#1m-vs-200k-the-statusline-proxy)
- [Commands, skills and mentions](#commands-skills-and-mentions)
- [When nothing happens](#when-nothing-happens)
- [Caveats](#caveats)
- [Settings](#settings)

The other way a session stops is by filling up its context window, and then
there is a ritual: ask Claude to write down where it got to, `/clear`, hand the
note to the fresh session. People do that by hand, several times a day, at
whatever moment they happen to notice.

Set a threshold and the wrapper does it for you — on claude and on codex, each
with a threshold of its own ([codex details](codex.md#context-restart-on-codex)).

## Turning it on

The context restart is opt-in, because it clears a session's history and nobody
should get that by accident. One variable. It is off until you set it, and
nothing below happens without it:

```sh
CR_CONTEXT_RESTART=1 agent-retrier-claude  # restart where this model's own row says
CR_CONTEXT_RESTART=1 agent-retrier-codex   # ... which is a different point on codex
CR_CONTEXT_TOKENS=500k agent-retrier     # or: restart at this many tokens, exactly
```

Which of the two to reach for: `CR_CONTEXT_RESTART=1` if you run both agents,
or do not want to think about a number — it is the only setting that means the
right thing on claude *and* codex at once, because each one reads its own row
rather than sharing one number. `CR_CONTEXT_TOKENS` if you have watched the
badge for a day and want a specific number on claude. Copy-paste versions of
both are in [minimal configs](configuration.md#minimal-configs), and
[how to read a setting](configuration.md#how-to-read-this-page) explains which
variables are shared between the agents and which are not.

`CR_CONTEXT_RESTART=1` is the same thing without having to pick a number: it
arms the model's own row in the [profile table](#the-context-window) instead —
`--cr-models` prints exactly what that resolves to for your environment — unless
`CR_CONTEXT_TOKENS` (or a per-model override) says otherwise. For the claude models
this project has always shipped 1M/200k windows for, that row's `restart_at` is
the same 51% (`DEFAULT_RESTART_PCT`) it has always used, just baked in per
model now instead of applied blind to whatever window a session happens to
report. Codex's row is not a percentage at all — its own hard cap and reserve
already give it a number of its own; see
[codex](codex.md#context-restart-on-codex) for what actually decides codex's
threshold under this flag.

Two more make it fit a project you actually work in:

```sh
export CR_CONTEXT_RESTART=1
export CR_HANDOFF_FILE=scratchpad/RESUME.md      # relative to cwd, .gitignore it
export CR_RESUME_MSG='Read `{file}` and carry on from it.'
```

Put those three in your shell rc, or in a `direnv` file for one repo, and there
is nothing else to run. Write `{file}` rather than the literal path in both
phrases — it is substituted with whatever `CR_HANDOFF_FILE` actually resolves
to, which is not always the path you set (see below). What the session is told
is always the absolute path, even when you set a relative one: a model asked to
write `scratchpad/RESUME.md` may decide that means a scratchpad of its own
somewhere else, and the wrapper only ever looks in the one place.

Add the handoff file to your `.gitignore`. It is a scratch note about one
session and it is rewritten from scratch every time.

Two sessions in the same project default to the same `CR_HANDOFF_FILE`, and
without either of the two things below, the second one to fold overwrites the
first one's file mid-write:

- Put `{id}` in the path — `CR_HANDOFF_FILE=scratchpad/RESUME-{id}.md` — and
  each session gets a short id of its own (`scratchpad/RESUME-3f9a1c2b.md`);
  no coordination needed, because the path can never collide.
- Leave it out and the wrapper coordinates for you: it registers the path it
  is about to use, and a session that finds another live one already holding
  it moves its own aside to `<stem>-<id><ext>` automatically, logging `handoff
  file is taken by pid N; using scratchpad/RESUME-3f9a1c2b.md`.
  `CR_HANDOFF_REGISTRY_DIR` (default `~/.agent-retrier/sessions`) is where
  that coordination lives.

## What you will see

Nothing at all, until the session gets near the threshold. Then the badge in the
corner starts showing the percentage, `◆ cr 47%`, so you can tell whether the
number you picked is a sensible one before it ever fires.

When it does fire, four things are typed into your session, and the wrapper
prints a dim line before each of them so you are never left wondering what just
happened:

```
[agent-retrier] context is filling up; asking for a handoff
[agent-retrier] handoff verified; clearing the context
[agent-retrier] context cleared; unfolding the handoff
[agent-retrier] context restarted: 700k down to 5k
```

Behind those, `~/.agent-retrier/log` has the whole story with numbers:

```
claude-opus-5: a 1.0M context window (the model), restarting at 510k
context is 700k of a 1.0M window (70%, threshold 510k); folding up
asking for a handoff into scratchpad/RESUME.md (attempt 1/2, marker HANDOFF-4acfe679)
handoff accepted: 3417 bytes ending in HANDOFF-4acfe679, turn closed with end_turn
handoff verified; clearing the context with /clear
unfolding from scratchpad/RESUME.md
context restarted: 700k down to 5k
```

The whole thing takes a minute or two, nearly all of it spent waiting for Claude
to finish writing the file and for the transcript to fall quiet afterwards.

## The four steps

1. **Fold.** `CR_HANDOFF_MSG` asks Claude to stop, start nothing new, write a
   complete handoff into `CR_HANDOFF_FILE`, and finish that file with a one-time
   marker.
2. **Check.** The next section, which is the whole design.
3. **`/clear`.** Claude Code's own command. The process, the pty, the MCP
   connections and the warm prompt cache all stay; only the history goes.
4. **Unfold.** `CR_RESUME_MSG` points the fresh session at the file.

Nothing is counted on this side. Every assistant row in the transcript carries
the API's own `usage`, and `input + cache_creation + cache_read` is the prompt
that was sent. The check rides on a row the wrapper had already parsed, so it
costs two comparisons and happens only when the transcript actually grew. The
denominator comes from the model slug in the same row.

## Which session is mine

Every step above — the badge's percentage, whether the fold is answered,
whether `/clear` is safe to send — is read off one transcript, and two
sessions sharing a project directory means there is more than one to choose
from. Three things pin down which one is this terminal's:

- **claude** identifies it explicitly: Claude Code (>= 2.1.273) writes
  `~/.claude/sessions/<pid>.json` naming the transcript for that pid, and the
  wrapper reads it (`ClaudeSessionRegistry`) rather than guessing which file
  grew last. A record is this terminal's because of whose process wrote it —
  the pid the wrapper forked, or a descendant of it when the command is an
  alias and claude runs one shell further down — and for no other reason. In
  particular not because it is in this directory and started after the wrapper
  did: a second session started beside this one fits that description exactly
  as well.

  That file is written a second or two after claude starts, so every launch
  begins with **nobody named yet**. For that stretch the wrapper picks no
  transcript at all — there is no context count, no turn, and no answer that
  could end a wait, only limits, which are the account's and are read from
  any transcript. It waits `CR_SESSION_GRACE_SEC` (30 s) for the file. Only a
  claude that never writes one — an older build, `claude attach`, the agent
  roster — is followed by a heuristic after that; see [Caveats](#caveats) for
  what the heuristic still gets wrong.
- **codex** has no such file to read (a rollout's head names no pid), so
  identity rests on the one thing nobody else can produce: the fold phrase's
  nonce (`HANDOFF-xxxxxxxx`), unique machine-wide. Its echo landing in a
  rollout is the proof that rollout is really this session's — see
  [codex: two sessions in one directory](codex.md#two-sessions-in-one-directory)
  for how the wrapper reads, but does not act on, a candidate before that
  proof arrives.
- Either way, `bind_transcript` is the one place the wrapper ever switches
  which file it is reading. The context percentage, whether the session
  counts as busy, the alive check that clears a wait, and the fold/clear/resume
  checks are all filtered to that one file, so a neighbour's turn cannot feed
  this session a number, or an echo, that was never its own.

Once identity is settled, the [per-session handoff file](#turning-it-on)
means the two sessions do not need to agree on a filename either: put `{id}`
in `CR_HANDOFF_FILE` yourself, or leave it out and let the wrapper's own
registry-based coordination pick one aside automatically.

## Why `/clear` is the last thing it will do

Clearing a session that was not folded up loses the work with nothing written
down, so `/clear` goes out only when four independent things agree:

- the handoff file **ends with the marker generated for this attempt**, which is
  proof the write ran to the end. "Done" in the chat would only be proof that
  the model believes it did. The marker is different every time, so one left
  behind by a previous attempt cannot be mistaken for this one's;
- the runtime recorded the turn as `end_turn`, not `max_tokens` (cut off by
  length) or `refusal`;
- the file is newer than the request for it and over `CR_HANDOFF_MIN_BYTES`;
- the session's own transcript has stopped growing for `CR_ROOT_IDLE_SEC`. That
  is how "the turn is over" is asked here, because in a session that keeps
  background work running the screen never goes quiet.

(On codex, "the turn finished" and "mid-turn" are read off the rollout's own
turn rows instead — see [codex](codex.md#how-it-differs-from-claude-underneath).)

If any of them is missing the wrapper asks for the fold again, and then gives
up. What giving up means depends on whether the fold phrase ever reached the
session. That is proven either by its echo in the transcript or by the handoff
file ending with this attempt's marker — the marker was typed into this terminal
and nowhere else, so a file carrying it is the model's answer. (A phrase typed
while a turn is still running is queued by Claude Code and written as a
`queued_command` row rather than an ordinary user row; the wrapper reads both,
and does not retype a phrase it can see waiting in that queue.) If neither was
ever seen, nothing was told to the model, and giving
up really does leave the session untouched — it can still fall back on Claude
Code's own compaction, a far better outcome than a history thrown away on a
promise. But once the phrase was delivered, the model has already been told to
wrap up and start nothing new, and simply walking away would leave it sitting
on an instruction that never gets followed up. So instead the wrapper types
`CR_CANCEL_MSG` — "The context restart was cancelled — the handoff is not
needed now. Continue with what you were doing before it was requested." — to
say the ask is off (`restart aborted at handoff_sent: ...` followed by
`restart cancelled; asking the session to carry on` in the log; see
[When nothing happens](#when-nothing-happens)). If `/clear` did go out and the
context did not actually fall, the feature switches itself off for the rest of
the session rather than typing into a full one for ever.

And the unfold is owed after it. Once `/clear` has gone out, the session sitting
there has nothing in it — the one thing left to do is type the resume phrase,
and giving up part-way through would leave it empty forever. So `/clear` itself
is confirmed rather than trusted: the wrapper waits for the session's identity
to move on (a fresh transcript, the same signal a normal restart rebinds on) or
for the screen to go quiet for `CR_CLEAR_SETTLE_SEC`, and reprints `/clear` once
if neither shows up in time. Past that point there is no timeout that gives up
on its own — a person typing, or a foreign transcript still finishing a turn,
just holds the step, with a reminder every five minutes ("unfold is waiting
for: ..."), and the resume phrase goes out the moment the gate opens. If the
resume phrase itself keeps leaving no trace, it is retyped with a growing gap
between attempts (a minute, then two, then four, ...) up to `CR_RESUME_ATTEMPTS`
times before the wrapper stops trying — but even then it does not go quiet: the
badge turns red ("unfold failed"), a notice repeats until someone reads the
handoff file by hand, and the debt only clears once a key is pressed. What does
switch off for good, the same as before, is the *trigger* — nothing here starts
another restart on top of a session nobody has looked at yet.

A usage limit landing in the middle suspends the restart rather than cancelling
it. Every clock it runs on stops for the duration, because a weekly limit is
days long and would otherwise expire a fifteen-minute fold timeout from the
inside. Its attempt budget is not spent on the world's problems either, and when
the quota comes back the step that was interrupted is re-sent. `continue` is the
wrong instruction at every step of a restart but the last one.

Background agents keep running across a restart, which is what you want and why
nothing here waits for them. Idle is measured on the session's own transcript,
and work started elsewhere writes its own.

## Writing your own phrases

The two phrases are yours. `{file}` is substituted in both and `{marker}` in the
folding one, and each must be a single line, because a newline submits it.

```sh
export CR_HANDOFF_MSG='Stop here, start nothing new, and write everything the next session needs into `{file}` because it will not remember this one. Last line of that file: {marker}, alone, written after the rest.'
export CR_RESUME_MSG='/my-skill continue from `{file}`'
```

If you write your own, keep three things in it. No new work, because the session
is about to end and anything started now is lost. "For a session that will not
remember this one", without which you get notes that only make sense to someone
who was there. And the marker as the last line of the file, written last: that
is the only thing standing between a half-written note and a cleared session.

Keep `{file}` in both, even in a phrase you are sure will only ever point at
one path: it is what lets the wrapper hand a session its own, possibly
suffixed, handoff path (see [Turning it on](#turning-it-on)) instead of a name
it made up itself. A phrase that drops `{file}` is flagged once at startup —
`CR_RESUME_MSG does not contain {file}; a per-session handoff path cannot be
passed to it` — precisely because it is easy to miss until two sessions in the
same project collide.

A slash command works as a resume phrase. It goes out with the extra care
described under [Commands, skills and mentions](#commands-skills-and-mentions)
below.

A command is not portable between agents, though — `/my-skill` above is a
Claude Code skill, and codex names the same convention differently (`$name`,
not `/name` — see below). Use the `{skill:NAME}` placeholder instead of
writing the prefix by hand:

```sh
export CR_RESUME_MSG='{skill:my-skill} continue from `{file}`'
```

which expands to `/my-skill` for claude and `$my-skill` for codex — one
phrase, both agents. Writing the prefix yourself and running both agents in
the same project still works via `CR_CLAUDE_HANDOFF_MSG`/`CR_CODEX_HANDOFF_MSG`
and `CR_CLAUDE_RESUME_MSG`/`CR_CODEX_RESUME_MSG` (`CR_HANDOFF_FILE` can be
shared between them — it is just a path): set for one agent only, each
overrides the shared phrase for that agent alone, and the other keeps using
it unchanged. `CR_CLAUDE_CLEAR_CMD`/`CR_CODEX_CLEAR_CMD` exist for the same
reason, though `/clear` itself works identically on both (verified live,
codex-cli 0.155.1).

## Choosing a threshold

Half the window is a good place to start for a threshold. It leaves the folding
turn plenty of room to think and to read files, which is exactly the
turn you do not want cramped. Much above 80% and the fold itself starts
struggling for space; much below 30% and you restart more often than you get
work done. Watch the badge for a day and move it.

`CR_CONTEXT_TOKENS` sets an absolute threshold instead (`500k` is fine), and it
beats the model's own row.

One model behaving differently from the rest of its own fleet does not need a
whole new threshold for everyone: `CR_CLAUDE_TOKENS_<SLUG>` and
`CR_CODEX_TOKENS_<SLUG>` set an absolute threshold for one model by name —
`<SLUG>` is that model's slug, uppercased, with anything that is not a letter
or digit turned into `_` (`claude-opus-5` → `CLAUDE_OPUS_5`, `gpt-5.6-sol` →
`GPT_5_6_SOL`). It outranks `CR_CONTEXT_TOKENS` and the model's own row both,
but only for that one model — every other model still reads off whichever of
those two is set.

```sh
export CR_CLAUDE_TOKENS_CLAUDE_HAIKU_4_5=90000   # this one model folds sooner
```

Putting all of the above together, the full order a threshold is resolved in,
top to bottom, is:

1. `CR_CLAUDE_TOKENS_<SLUG>` / `CR_CODEX_TOKENS_<SLUG>` — one exact model.
2. `CR_CONTEXT_TOKENS` / `CR_CODEX_CONTEXT_TOKENS` — an absolute number, never
   shared between agents (codex never inherits claude's).
3. the model's own row in the [profile table](#the-context-window), armed by
   `CR_CONTEXT_RESTART=1`, and only when the window matches what that row was
   written for.
4. the same `CR_CONTEXT_RESTART=1` flag, applied to a window no row
   describes instead — a model this build has never heard of, or one whose
   window was raised past what its row assumes: `DEFAULT_RESTART_PCT` (51%)
   of whatever window the session actually has for claude, the usable window
   minus `CR_CODEX_RESERVE_TOKENS` for codex. This is what keeps a brand-new
   model protected the day it ships, before this build has ever heard of it.
5. nothing — only when there is no window at all yet (codex before its first
   turn) does the trigger stay disarmed rather than guessing.

Whatever (3) or (4) answers is always capped under that row's `compact_at`
minus `CR_CODEX_RESERVE_TOKENS` (0 for claude), read live rather than trusted
frozen into the table.

A threshold is a fraction of the window, but the session it protects never
starts at zero: the system prompt, `CLAUDE.md`, MCP tool definitions and the
handoff file it just read back all cost real tokens before a fresh session
writes a word of its own. Against a small window, that baseline alone can eat
most of the threshold's own headroom — a 200k window at the default 51%
restarts at 102k, and a session that comes back from the fold already sitting
at 57k has only 45k left to work with before it folds again, sometimes inside
30-40 minutes. So after every restart the wrapper checks the room actually
left: if `threshold − baseline` falls under `CR_CONTEXT_MIN_HEADROOM` (default
`80k`), it raises the threshold for the rest of the session — `baseline +
CR_CONTEXT_MIN_HEADROOM`, but never past the model's own `compact_at` minus
its reserve, the same ceiling stage (3)/(4) above is held to — and says so in
the log. Against a window at or under 200k, the check itself uses whichever is
smaller of `CR_CONTEXT_MIN_HEADROOM` and 30% of the window, so a window too
small to ever spare 80k is not nagged over headroom it never had; the raise
that follows still reaches for the full `CR_CONTEXT_MIN_HEADROOM` where the
model's `compact_at` leaves room for it. When there is nowhere to raise it to,
the wrapper says so out loud instead of guessing — widening `CR_CONTEXT_TOKENS`
or moving to a bigger window is then a decision for a person, not the wrapper.

A threshold that still cannot hold is a different problem: more than
`CR_CONTEXT_MAX_PER_HOUR` restarts (default `3`) inside a rolling hour means
the trigger is grinding the session rather than protecting it, and it is
switched off for the rest of the session — the same permanent-off state a
failed unfold leaves behind, visible the same way (badge, notify).

## The context window

Every model this build knows about is one row in a profile table — window,
where the wrapper restarts by default, and where the agent folds the context
on its own (`compact_at`, used only for the capping in stages 3/4 above).
`--cr-models` prints it resolved against your actual environment:

```
$ CR_CONTEXT_RESTART=1 agent-retrier.sh --cr-models
agent   model              window  restart_at  compact_at  source
------  -----------------  ------  ----------  ----------  ------------------
claude  claude-opus-5      1.0M    510k        967k        CR_CONTEXT_RESTART
claude  claude-haiku-4-5   200k    102k        190k        CR_CONTEXT_RESTART
...
codex   gpt-5.6-sol        258k    194k        258k        CR_CONTEXT_RESTART
```

| models | window | restart_at | compact_at |
|---|---|---|---|
| opus/sonnet/fable/mythos 5, opus 4-6/4-7/4-8, sonnet 4-6 | 1M | 510k (51%) | ~967k (Claude Code's own auto-compact point) |
| opus 4-1/4-5, sonnet 4-5, haiku 4-5 | 200k | 102k (51%) | ~190k |
| codex 5.6 (sol/terra/luna/astra) and 5.5 | 258.4k (effective) | 194.4k (cap − 64k reserve) | 258.4k (hard cap) |

`CR_CONTEXT_WINDOW=auto` reads the model slug out of the transcript and
resolves it in the order `resolve_window` trusts most to least:

1. `CR_CONTEXT_WINDOW` itself, if it names a number rather than `auto`.
2. What the agent states outright — codex writes its window into every row of
   accounting; claude's own statusline does the same, relayed by the
   `--cr-statusline` proxy (see "`[1m]` vs 200k: the statusline proxy" below).
3. Claude Code's own environment: it narrows to 200k if
   `CLAUDE_CODE_DISABLE_1M_CONTEXT` or `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is set
   in the environment claude is started with.
4. The profile table above — including a same-family point release
   (`claude-fable-5-1` is whatever `claude-fable-5` is).
5. A window this build has learned off the network for a slug the table does
   not carry (below).
6. **An estimate** (new — see below): a slug nothing above sized gets a guess
   rather than a disarmed trigger.

It also widens on its own if the session is ever seen past the window it
assumed — see "Self-correction" below. Name a number if your setup narrows
the window some way the wrapper cannot see. (On codex the window is normally
read from the rollout every turn, which is why `CR_CONTEXT_WINDOW` is not
needed there — and why raising `model_context_window` in codex's
`config.toml` past what the profile table lists moves the window the wrapper
sees, and the table's row for that model stops applying; see stage 4 of
"Choosing a threshold" above.)

A model the built-in table does not list is looked up rather than guessed at
first: the Models API when `ANTHROPIC_API_KEY` is set — a Claude subscription
is not an API key, so most sessions skip this — and the published models table
otherwise. That happens in a worker thread, so nothing waits on it, and the
answer is cached in `CR_MODEL_CACHE` for a week. `CR_MODEL_LOOKUP=0` keeps the
wrapper entirely offline.

### An estimate is an estimate

Until the network answers (or if `CR_MODEL_LOOKUP=0` means it never will), the
trigger no longer sits disarmed on `cr window?`. A slug nobody shipped this
build knowing about is almost always a NEW model — which is to say a large
one — so it gets a guess instead: a claude slug from a family the table
already has (`claude-opus-6` when `claude-opus-5` is in it) gets that family's
newest window; an entirely new family gets the modal window of the table's own
newest generation; a codex slug is read from codex's own
`$CODEX_HOME/models_cache.json` if it is there, or that cache's modal window;
and if nothing above has any opinion at all, a flat 200k stands in as a
last, explicitly provisional resort. The corner marks a percentage built on a
guess with `~` (`cr ~51%`) so it is never mistaken for a confirmed reading, and
the log names the guess and where it came from
(`claude-opus-6: a 1.0M context window (same family) (estimated), restarting
at 510k`).

**Self-correction**, both ways:

- *Upward*: proven too small (a turn's usage lands past the assumed window) —
  200k escalates to 1M, and past that to codex's own advertised ceiling or a
  flat 50% bump, repeated until the number actually fits. Always was true even
  before an estimate could be wrong in this direction (a profile-table window
  can be proven wrong for one specific session too); T19 just lets it escalate
  more than once.
- *Downward*: the agent compacts the context on its own — claude's own
  `compact_boundary`, or codex's own `compacted` — which states or implies
  how many tokens it actually held right before deciding it was full. The
  window becomes `tokens / 0.92` (backing off the headroom both agents leave
  before their true ceiling) if that is smaller than what was assumed, and the
  restart threshold is recalculated from it. This also fixes "the table says
  1M, but this session's real ceiling is 200k" after the fact, and it is not
  marked `~`: it is something the session just told us, not a guess.
- A guess that survives past 60% of itself without compacting has earned the
  benefit of the doubt and loses its `~`.

This is the one thing in the wrapper that talks to the network, and only ever
about a model name: it sends a slug and reads back a number.

## `[1m]` vs 200k: the statusline proxy

The profile table above is a guess about the SLUG, not about this particular
session: whether it actually got a 1M window depends on `/model sonnet[1m]`,
the account plan (Max/Team/Enterprise vs Pro), `CLAUDE_CODE_DISABLE_1M_CONTEXT`
and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` — none of which show up in the bare slug
the transcript writes (`claude-sonnet-5`, `[1m]` suffix included or not, is
only ever "believe it if it's there," never proof of the opposite). Assume 1M
on a session that actually has 200k and the restart threshold sits past the
point Claude Code compacts the context on its own — the exact "invisible"
failure this whole feature exists to prevent.

Claude Code's own statusline is asked, instead of guessed at: once a restart
is armed (`CR_CONTEXT_TOKENS`/`CR_CONTEXT_RESTART`), the
wrapper passes claude a `--settings` naming
`<this file> --cr-statusline <path>` as its `statusLine` command. Claude Code
invokes that command with a JSON payload including
`context_window.context_window_size` (200000 or 1000000) and `model.id`; the
proxy records those into `~/.agent-retrier/status/<pid>.json` (one file per
wrapper, written atomically) and then runs YOUR OWN `statusLine` command, if
`settings.json`/`settings.local.json` name one, with the same stdin — so
whatever you already see in that corner keeps appearing exactly as before. No
statusLine configured at all means no output from the proxy either: it never
fabricates one.

The supervisor polls that file every `CR_POLL_SEC` and feeds
`context_window_size` into the same `context_window_hint` codex's own rollout
accounting already uses (stage 2 above) — the log names the source `claude's
status line` instead of `the transcript` — and `model.id` into an immediate
model-name update.

`CR_STATUSLINE_PROXY=0` turns the whole mechanism off: no `--settings` is
added at all, and the wrapper is back to guessing purely from the model
profile table (stages 3/4).

**Verified vs assumed** (2026-09-19, no live Claude Code session was
available while building this): the documented statusline payload shape
(`context_window.context_window_size`, `model.id`, `session_id`,
`transcript_path`, `exceeds_200k_tokens`) and the existence of `--settings
<file-or-json>` are taken from Claude Code's own documentation, not observed
live. Two things documentation does not state and this build could not test
against a real binary: whether `--settings` on the command line MERGES with
or REPLACES the user's own `settings.json`/`settings.local.json`, and exactly
how often (every render? every N seconds?) the statusline command is
invoked. This is coded against the safer assumption — **merge** — since a
proxy that silently discarded someone's other settings would be a worse
failure than this feature simply not helping; and against the poll cadence
being frequent enough that `CR_POLL_SEC` (not the statusline's own interval)
is the bottleneck. Full detail in
[T20's backlog entry](backlog/T20-claude-effective-window.md#verified).

## Commands, skills and mentions

`CR_SLASH_ENTER` exists because typing a `/` opens a command list on both
agents, and in a TUI that is a real hazard: Enter into an open list can pick
the highlighted entry instead of submitting what was typed. Measured against
Claude Code 2.1.222 and codex-cli 0.155.1, one Enter runs a recognized
command (`/clear`, `/new`, …) and there is no confirmation step. So a slash
command gets a longer pause (`CR_SLASH_GAP_SEC`), letting the list settle on
the exact match, and then two Enters (`CR_SLASH_ENTER`), the second purely as
insurance for a build that behaves differently. That second one costs
nothing, because an Enter into an empty input box submits nothing, which is
also measured. `CR_SLASH_ENTER=1` turns the extra Enter off.

Two other characters that look like they might need the same treatment
turned out not to (T15, live-driven on codex-cli 0.155.1 — see
[T15's results table](backlog/T15-codex-unfold-live-investigation.md#results)):

- **`$name`** — codex's own "invoke a skill" convention — is not a composer
  feature at all; it is a *model-level* instruction ("if the user names a
  skill with `$SkillName`, use it"). Typed as part of a phrase, it is
  delivered as plain text, no differently from anything else.
- **`@file`** (file mention, both agents) is likewise delivered as plain text
  when the whole phrase arrives in one write, which is how this wrapper
  always sends it.

The one real hazard found: an **unrecognized** `/word` is silently dropped by
codex — it prints "Unrecognized command" inline and never sends the text at
all, not even as a plain message. This is why a custom `CR_HANDOFF_MSG` /
`CR_RESUME_MSG` / `CR_CANCEL_MSG` that needs to name a skill/subagent should
use the `{skill:NAME}` placeholder instead of hand-writing a prefix: it
expands to `/NAME` for claude and `$NAME` for codex, e.g.

```sh
CR_RESUME_MSG='{skill:supervisor} continue from `{file}`'
```

unfolds both agents into a "supervisor" skill/subagent, instead of only one
of them (whichever one happens to match the prefix you picked by hand). See
[configuration.md](configuration.md#context-restart) for the per-agent
`CR_CLAUDE_*`/`CR_CODEX_*` overrides this placeholder works inside of.

## When nothing happens

Look in `~/.agent-retrier/log`. In order of likelihood:

- No `context restart armed` line at all: neither `CR_CONTEXT_RESTART` nor
  `CR_CONTEXT_TOKENS` reached the wrapper. Check that it is exported, and that
  your `--cmd` wrapper is not starting claude in a scrubbed environment.
- `restart step held: ...` is working as intended. It will not type over you
  mid-sentence, and it will not interrupt a turn that is still running. On
  codex, `a turn is still running` can last as long as the turn does, however
  quiet it is — that is a root waiting on its agents.
- `a 200k context window` for a model you expected to have 1M: the line says
  where the figure came from — most likely `CLAUDE_CODE_DISABLE_1M_CONTEXT`.
  Name the window yourself with `CR_CONTEXT_WINDOW=1M`.
- `cr window?` in the corner (codex only): the rollout has not stated its
  window yet, which normally clears itself on the next turn. If it never does,
  `CR_CONTEXT_WINDOW` or `CR_CONTEXT_TOKENS` gives it one directly.
- `(estimated)` in a `context window` line, or `~` in front of the percentage
  in the corner: this build has never heard of the model (or could not look it
  up) and is running on a guess — see "An estimate is an estimate" above for
  where the guess comes from and how it corrects itself. Name the window
  yourself with `CR_CONTEXT_WINDOW` if you already know it is wrong.
- `restart aborted at handoff_sent: ...` (or `handoff_ok`) means the fold
  produced nothing usable, and the message names which of the four checks
  failed. If the phrase never reached the session at all, that's it — the
  session is left alone. If it did (an echo was seen: the model was already
  told to wrap up and start nothing new), the next line is
  `restart cancelled; asking the session to carry on` — `CR_CANCEL_MSG` typed
  in to say the ask is off, so an unattended session does not sit on that
  instruction forever.
- The percentage in the badge never moves: the wrapper reads the figures off
  assistant rows, so it has nothing to show until the session answers something.
  A transcript that already existed when the wrapper started is seeded rather
  than replayed, so a `--continue` session reports its size from its next turn.

## Caveats

- The context restart reads its figures from one transcript at a time, the one
  this terminal's session writes. On Claude Code >= 2.1.273 that transcript is
  identified explicitly, through `~/.claude/sessions/<pid>.json`
  (`ClaudeSessionRegistry`) — never by guessing which file grew last, so
  another live session under the same working directory has no effect on it,
  even while it is briefly the busier of the two.
- Until that file is written — the first second or two of every launch —
  the wrapper follows no transcript, rather than the one that happens to be
  growing. It used to guess: bound for that moment to whichever session had
  started in the directory since it did, a wrapper started together with
  another read the other's context as its own.
- Without that file at all (an older Claude Code, `claude attach`, the agent
  roster, or two candidates the registry cannot tell apart), the watcher gives
  it `CR_SESSION_GRACE_SEC` and then falls back to its previous heuristic:
  stay with the transcript already being followed while it grows, and
  otherwise prefer whichever one did not exist when the session started. That
  fallback carries the same limitation as before — another live session under
  the same working directory can, briefly, be the file that grew last — and a
  `no session registry for pid … after 30s; falling back to newest transcript`
  line in the log says when it is in effect. It is logged when the fallback
  begins, not at every launch.

## Settings

Every context-restart setting — `CR_CONTEXT_RESTART`, `CR_CONTEXT_TOKENS`,
`CR_CONTEXT_WINDOW`, the codex thresholds, the model lookup, the handoff file
and phrases, the timeouts and `CR_SLASH_ENTER` — is in
[configuration.md](configuration.md#context-restart), and all of them do nothing
until `CR_CONTEXT_RESTART` or `CR_CONTEXT_TOKENS` is set.

See also: [codex](codex.md), [usage limits](usage-limits.md),
[troubleshooting](troubleshooting.md).
