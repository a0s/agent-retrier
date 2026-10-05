# T31 — The draft gate under the kitty keyboard protocol

Priority: P0 · Epic: B · Depends on: — · Size: S · Status: done (3.0.2)

## Problem

The draft gate ("unsent text in the prompt box") is a count of characters typed
since the last Enter/^C/^U/^W/Esc, kept from our own stdin. Claude Code asks the
terminal for the kitty keyboard protocol (`CSI > 5 u`) and modifyOtherKeys
(`CSI > 4 ; 2 m`) at startup (see `test/fixtures/*.bin`). A terminal that
grants them — Ghostty and cmux, kitty, WezTerm — sends exactly the keys that
empty the box as escape sequences: Esc as `CSI 27 u`, Ctrl+C as `CSI 99 ; 5 u`
(or `CSI 27 ; 5 ; 99 ~`), Alt+Backspace as `CSI 127 ; 3 u`. They were skipped
as anonymous escapes, the count survived a cleared box, and the gate held for
`CR_DRAFT_GRACE_SEC` (600 s) over nothing.

## Evidence

T30's incident: "restart step held: unsent text in the prompt box" from
20:26:50 to 20:33:11 with an empty box on screen, in cmux. No prompt was
submitted between 20:16 and 20:33 (transcript `b1ae85ba`), so the count came
from keys typed around 20:23–20:26 and cleared without a plain control byte.

## What was done

`Controller._escaped_key` decodes kitty `CSI code[:alt];mods[:event];text u`
and modifyOtherKeys `CSI 27;mods;code ~` into what the key does to the box:
Esc / Ctrl+C / Ctrl+U / Ctrl+W / Alt- or Ctrl-Backspace clear it (the same
convention the plain bytes already follow), Enter submits, Shift+Enter and a
plain character type, Backspace erases one; key releases, arrows and function
keys change nothing. Legacy `ESC DEL` (Alt+Backspace) clears too.

A screen-model cross-check was considered and left out: it needs a `Screen`
fed with every frame for the life of the session, which is exactly the cost
`CR_AGENTS_OVERLAY` stays off by default to avoid.

## Acceptance criteria

- [x] `TestTheKeyboardSpeaksKittyToo` in `test_controller.py`, one case per
      encoding above, plus a sequence split across two reads.
