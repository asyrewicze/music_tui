# Contributing to music_tui

Thanks for your interest. This is a small, deliberately narrow project, so a short read
here will save you a rewrite later.

## Licensing

By submitting a contribution to this project, you agree that your contribution will be
licensed under the GNU General Public License v3.0 (GPLv3).

---

## Scope

music_tui is a keyboard-driven terminal controller for **Apple Music on macOS**, driven
entirely through AppleScript. It aims to be small, fast, and boring to run.

**In scope:** anything Music.app exposes through its scripting dictionary that makes
sense at a terminal — playback, playlists, search, track metadata, ratings, volume,
seeking.

**Out of scope:**

- The Apple Music **catalog**. Music.app's scripting dictionary has no catalog-search
  command, so the app can only see and play what is already in your library. This is a
  limit of the scripting interface, not of the implementation — switching to JXA,
  Scripting Bridge, or PyObjC will not change it.
- **Audio stream access**, and therefore visualizers. AppleScript does not expose it.
- **Non-macOS platforms.** The entire backend is Apple Events.
- **Third-party Python dependencies.** See below.

---

## Architecture you must preserve

These are not style preferences. Each one exists because the alternative was tried and
caused a real problem.

### 1. No third-party Python packages

The app runs on system Python with nothing but the standard library. If a change needs
`pip install`, it needs a different design. This is what makes the app a single file you
can drop on any Mac.

Python 3.9 or newer.

### 2. The decoupled timing model

This is the single most important design decision in the project. The app never asks
Music.app what time it is.

**Two independent mechanisms:**

- **The poll (slow, every 10 seconds, `POLL_INTERVAL_SEC`).** One osascript call fetches
  the authoritative player state. This is the only thing that talks to Music.app on a
  timer.
- **Local interpolation (fast, every frame).** Between polls the app advances the
  position counter in Python from the elapsed wall-clock time. The counter ticks every
  second with no AppleScript involved at all.

**Why:** earlier versions polled far more often and made several osascript calls per
poll. Each call is an Apple Event, which goes through macOS privacy machinery, and the
result was `tccd` pinned near the top of the CPU list and `loginwindow` burning several
percent continuously — for a program whose entire job is showing a text counter. See
issue #1. With the current model, `loginwindow` sits around 1–3% and `tccd` disappears
from the top consumers entirely.

**The state model.** Two variables, and the distinction between them matters:

- `np_at_poll` — the authoritative `NowPlaying` snapshot captured at the last poll,
  paired with `np_poll_time`. Treat it as immutable between polls.
- `now_playing` — what actually gets drawn. When the player is playing, this is a *copy*
  of `np_at_poll` with `position` advanced by the elapsed time.

Never advance `np_at_poll.position` in place. It is the fixed reference point that the
interpolation is measured from; mutating it makes the drift compound every frame instead
of being corrected at the next poll.

`NowPlaying` carries `track`, `artist`, `album`, `duration`, `position`, `state`,
`shuffle_enabled`, and `repeat`.

**One call per poll.** `get_now_playing()` returns all eight fields as a single
tab-delimited row from one osascript invocation. If you need new state on every frame,
add a field to that script and extend the parse. Do not add a second call.

**User actions refresh immediately.** Any keypress that changes player state calls
`force_repoll()` rather than waiting up to ten seconds for the next scheduled poll. Some
pass a small delay (`force_repoll(0.15)`) to give Music.app a moment to actually advance
before sampling. The UI must always reflect what the user just did.

**On-demand calls live outside the poll cycle.** Playlist enumeration (`l`) and library
search (`s`) each issue their own osascript call when invoked. They are affordable
precisely because they are user-initiated and infrequent. Search uses a longer timeout
since it walks the library, and blocks the UI while it runs.

**The rules that follow from all of this:**

- Do not lower `POLL_INTERVAL_SEC`.
- Do not add an osascript call to the poll path or to any per-frame drawing code.
- Do not move playlist enumeration or search onto the poll loop.
- Do not call osascript from inside a render function. Rendering reads cached state only.
- Prefer computing something locally over asking Music.app for it.

### 3. The UI must survive any terminal size

There are four layouts, selected fresh every frame from `stdscr.getmaxyx()`:

| Layout    | Floor | Notes |
|-----------|-------|-------|
| Full      | 40x16 | Boxed panel, horizontal bar, wrapped key menu |
| Wide      | 60x5  | Short but wide: bar plus a single control line |
| Compact   | 20x8  | 3x3 dot grid, two control rows |
| Too small | —     | A resize message |

They are tested in that order and the first that fits wins. Any new view must work in
all of them, and must survive resizing across every boundary in both directions —
including while a list or search is open.

---

## Code conventions

### Drawing

- **Always use `safe_addstr()`**, never `stdscr.addstr()` directly. It clips to the
  window and swallows the `curses.error` that writing to the last cell raises.
- **Never hardcode a row or column.** Derive positions from `h`/`w` each frame. If you
  find yourself writing `4`, ask what it should be relative to.
- **Run every width-dependent string through `truncate()`.** It appends an ellipsis
  rather than letting `safe_addstr` clip silently, so a reader can tell a name was cut.
- **Use the shared helpers** rather than reimplementing them: `clamp()`,
  `clamp_scroll()` for scroll windows, `pick_fitting()` for choosing the longest string
  that fits, `draw_full_list()` / `draw_compact_list()` for anything scrollable.

### Unicode and the ASCII fallback

Every non-ASCII character used in the UI goes through a module-level constant —
`ELLIPSIS`, `DASH`, `ARROWS`, `SPACE_KEY`, `DOT_FILL`, `DOT_EMPTY`, `GLYPH_SHUFFLE`,
`GLYPH_REPEAT`, `GLYPH_STATE`. These flip to ASCII when the terminal encoding is not
UTF-8, or when `MUSIC_TUI_ASCII=1` is set.

Do not write a bare `…`, `—`, or box glyph into a string literal. Add a constant.
Test both paths.

### AppleScript

- **Escape every interpolated value with `applescript_quote()`.** It handles backslashes
  as well as quotes. Never build a script with a raw f-string of user data.
- **Wrap fallible work in `try` / `on error`** and return a safe default. A missing
  track or a stopped player must not produce a traceback.
- **Prefer persistent IDs to names.** Playlist selection is still name-based and
  therefore ambiguous when names collide; search results are not — they resolve by
  `persistent ID`, which survives renames. New code should follow search, not playlists.
- **Keep the parse defensive.** Pad short rows, coerce with `try`/`except`, clamp to
  sane ranges. Music.app returns surprising things.

### Adding a key binding

There are four places to touch. Missing one leaves the key working but undiscoverable:

1. The input handler in `run_tui()`, under the right `Mode`.
2. `status_msg` — the full layout's menu.
3. `COMPACT_KEYS` — the compact and wide hint blocks. Supply a long label *and* a short
   one; the ladder picks whichever fits.
4. The Keyboard Controls table in `README.md`.

If the binding opens a new view, add a `Mode` constant and handle it in the input
dispatch and in **both** the full and compact/wide render paths.

Note that in `Mode.SEARCH_INPUT` every printable key is query text — `q` types a `q`
rather than quitting. Any future text-entry mode must do the same.

---

## Development

```bash
git clone https://github.com/asyrewicze/music_tui.git
cd music_tui
chmod +x music_tui.py
./music_tui.py
```

On first run macOS will ask permission for your terminal to control Music. Without it,
AppleScript commands fail silently — see the Permissions section of the README.

### Testing

There is no test suite. The app drives a real application and paints a real terminal,
which makes both halves awkward to assert on. What works well in practice:

**Stub out AppleScript.** Replace `run_osascript` with a function returning canned
tab-delimited rows. You can then exercise the whole UI without launching Music.app or
touching anyone's playback state. Do this by default; a test that changes what is
playing is a bad test.

**Drive the UI in a pty.** Fork with `pty.fork()`, set the window size with
`TIOCSWINSZ`, and write keystrokes to the master fd. Resizing this way exercises the
real `KEY_RESIZE` path. To read the screen back exactly, wrap `stdscr` in a proxy object
that records `win.instr(y, 0, w * 4)` for each row on every `refresh()` — parsing the
escape-sequence stream yourself is lossy and will mislead you.

**Syntax-check AppleScript without running it:**

```bash
osacompile -o /tmp/check.scpt -e 'tell application "Music" to ...'
```

This validates both syntax and terminology against Music.app's real dictionary, so an
invented property or command fails here rather than silently at runtime. It does not
execute anything. Note that compiling resolves the app's dictionary, which may launch
Music if it is not already running.

**Cover the boundaries.** At minimum, check both sides of every layout threshold — 40x16
and 39x15, 60x5 and 59x5, 20x8 and 19x8 — plus a resize round trip with a list open, and
one pass with `MUSIC_TUI_ASCII=1`.

**Check the CPU cost if you touched anything near the poll path.** Leave the app running
for a minute with music playing and watch `top -o cpu`. `loginwindow` should sit around
1–3% and `tccd` should not appear near the top at all. If either is climbing, something
is calling osascript more often than it should.

### Using an AI assistant

Assistants work well on this codebase, but they violate the timing model by default: the
instinctive fix for a stale-looking UI is to poll more often, or to fetch fresh state
inside a render function. Both are wrong here, for the reasons in Architecture above.

If you use one, give it this file, and check the resulting diff for:

- new `run_osascript` call sites, especially inside `draw_*` functions
- a lowered `POLL_INTERVAL_SEC`
- in-place mutation of `np_at_poll` instead of copying to `now_playing`
- string literals containing `…`, `—`, or box glyphs instead of the shared constants
- raw `stdscr.addstr()` instead of `safe_addstr()`

---

## Pull requests

- **Open an issue before building a feature.** Feature ideas live in the issue tracker,
  not in a roadmap section, so that is where scope gets agreed. It also stops two people
  building the same thing. Bug fixes and small corrections do not need one first.
- One logical change per PR. A UI change and an AppleScript change are two PRs.
- Say which terminal and macOS version you tested on, and which layouts you exercised.
- Update `README.md` in the same PR if you changed behavior, keys, or layout thresholds.
- Update the **How It Works** section of the README if you changed an architectural
  invariant. That section is what stops the next contributor from undoing a deliberate
  decision, so it matters more than it looks.

### Commit messages

Follow the existing history: a short sentence-case subject, a blank line, then a body
explaining *why* when the change is not self-evident.

```
Add compact layout for small terminal windows

Introduce a third rendering path so the app stays usable below the
previous 40x16 minimum: full, compact, then "terminal too small".

Closes #3
```

`Closes #N` / `Fixes #N` on its own line closes the issue automatically once the commit
lands on `main`. Keep messages free of automated attribution trailers.

---

## Reporting bugs

Open an issue with:

- macOS version and terminal emulator (Terminal.app, iTerm2, Kitty, …)
- Terminal size in columns x rows, since behavior is size-dependent
- Whether Music.app had Automation permission granted
- What you expected and what happened

For rendering problems a screenshot is worth far more than a description — include the
window size in the issue text, because it determines which of the four layouts you were
looking at.
