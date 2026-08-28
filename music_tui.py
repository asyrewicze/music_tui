#!/usr/bin/env python3
import curses
import locale
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List


# How often we ask Music.app for the "truth" (AppleScript call).
# The UI can still tick smoothly in-between without additional polling.
POLL_INTERVAL_SEC = 10.0

# Color pair IDs
CP_DEFAULT = 1
CP_CYAN    = 2
CP_GREEN   = 3
CP_YELLOW  = 4
CP_RED     = 5
CP_ORANGE  = 6

# Layout ladder, checked in this order: full, then wide (too short for full but
# wide enough that the dot grid wastes the space), then compact, then a plea to
# resize. The wide floor is where all eight bindings fit on one labelled line.
MIN_COLS, MIN_ROWS = 40, 16
MIN_WIDE_COLS, MIN_WIDE_ROWS = 60, 5
MIN_COMPACT_COLS, MIN_COMPACT_ROWS = 20, 8


def _unicode_supported() -> bool:
    """Not every terminal/font renders the dot and symbol glyphs; MUSIC_TUI_ASCII=1 opts out."""
    if os.environ.get("MUSIC_TUI_ASCII"):
        return False
    enc = getattr(sys.stdout, "encoding", None) or locale.getpreferredencoding(False) or ""
    return "utf" in enc.lower()


UNICODE = _unicode_supported()

ELLIPSIS      = "…" if UNICODE else "~"
DASH          = "—" if UNICODE else "-"
ARROWS        = "↑↓" if UNICODE else "up/dn"
SPACE_KEY     = "␣" if UNICODE else "spc"
DOT_FILL      = "●" if UNICODE else "#"
DOT_EMPTY     = "·" if UNICODE else "."
GLYPH_SHUFFLE = "⇄" if UNICODE else "S"
GLYPH_REPEAT  = "↻" if UNICODE else "R"
GLYPH_STATE = (
    {"playing": "▶", "paused": "‖", "stopped": "■"} if UNICODE
    else {"playing": ">", "paused": "||", "stopped": "[]"}
)

# Hint ladder shared by the playlist and search-result lists.
LIST_HINTS = (
    f"  {ARROWS} move  enter play  b back  q quit",
    f"  {ARROWS} move  enter play  b back",
    f"  {ARROWS} b back",
    "",
)

# Every binding, four per row, for the two-row compact hint block.
COMPACT_KEYS = (
    ((SPACE_KEY, "play/pause", "play"), ("n", "next", "next"),
     ("p", "prev", "prev"), ("s", "search", "find")),
    (("f", "shuffle", "shuf"), ("r", "repeat", "rep"),
     ("l", "lists", "lists"), ("q", "quit", "quit")),
)

# Compact progress indicator: a 3x3 grid of dots that fills in reading order
# (left to right, top to bottom), one dot per ninth of the track.
DOTS_ROWS, DOTS_COLS = 3, 3
DOTS_GAP = 2  # columns between dot centres
DOTS_W = (DOTS_COLS - 1) * DOTS_GAP + 1  # rendered width of one dot row


def applescript_quote(s: str) -> str:
    """Escape a Python string for interpolation into an AppleScript literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run_osascript(script: str, timeout: float = 5.0) -> str:
    """
    Execute AppleScript via osascript and return stdout (stripped).
    """
    try:
        p = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return (p.stdout or "").strip()
    except (Exception, subprocess.TimeoutExpired):
        return ""


@dataclass
class NowPlaying:
    track: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0  # seconds
    position: float = 0.0  # seconds
    state: str = "stopped"  # playing|paused|stopped|unknown
    shuffle_enabled: bool = False
    repeat: str = "off"  # off|one|all


def get_now_playing() -> NowPlaying:
    # One call returns a tab-delimited row:
    # track \t artist \t album \t duration \t position \t state \t shuffleEnabled \t repeat
    script = r'''
tell application "Music"
    set shuffleState to "false"
    set pstate to "stopped"
    set repeatState to "off"
    try
        set pstate to (player state as string)
        set shuffleState to (shuffle enabled as string)
        set repeatState to (song repeat as string)
    on error
        return "\t\t\t0\t0\tstopped\tfalse\toff"
    end try

    if pstate is "stopped" then
        return "\t\t\t0\t0\t" & pstate & "\t" & shuffleState & "\t" & repeatState
    end if

    try
        set t to current track
        set trackName to (name of t) as string
        set artistName to (artist of t) as string
        set albumName to (album of t) as string
        set dur to (duration of t) as string
        set pos to (player position) as string
        return trackName & tab & artistName & tab & albumName & tab & dur & tab & pos & tab & pstate & tab & shuffleState & tab & repeatState
    on error
        return "\t\t\t0\t0\t" & pstate & "\t" & shuffleState & "\t" & repeatState
    end try
end tell
'''.strip()

    out = run_osascript(script)
    parts = out.split("\t")
    while len(parts) < 8:
        parts.append("")

    track, artist, album, dur_s, pos_s, state, shuffle_s, repeat_s = parts[:8]

    try:
        dur = float(dur_s) if dur_s else 0.0
    except ValueError:
        dur = 0.0

    try:
        pos = float(pos_s) if pos_s else 0.0
    except ValueError:
        pos = 0.0

    state = (state or "stopped").strip().lower()
    if state not in ("playing", "paused", "stopped"):
        state = "stopped"

    shuffle_enabled = (shuffle_s or "false").strip().lower() == "true"

    repeat_raw = (repeat_s or "off").strip().lower()
    repeat_map = {"off": "off", "one": "one", "all": "all"}
    repeat = repeat_map.get(repeat_raw, "off")

    return NowPlaying(
        track=track.strip(),
        artist=artist.strip(),
        album=album.strip(),
        duration=max(dur, 0.0),
        position=max(pos, 0.0),
        state=state,
        shuffle_enabled=shuffle_enabled,
        repeat=repeat,
    )


# --- Apple Music commands -----------------------------------------------------

def music_cmd_play():
    run_osascript('tell application "Music" to play')


def music_cmd_pause():
    run_osascript('tell application "Music" to pause')


def music_cmd_next():
    run_osascript('tell application "Music" to next track')


def music_cmd_prev():
    run_osascript('tell application "Music" to previous track')


def music_cmd_play_playlist(name: str):
    """
    Play a playlist by its name.
    Note: if you have duplicate playlist names, Music may choose the first match.
    (We can upgrade to persistent IDs later if needed.)
    """
    safe = applescript_quote(name)
    run_osascript(f'tell application "Music" to play user playlist "{safe}"')


def music_cmd_toggle_play_pause(np: NowPlaying):
    if np.state == "playing":
        music_cmd_pause()
    else:
        music_cmd_play()


# --- Repeat -------------------------------------------------------------------

def music_cmd_set_repeat(mode: str):
    """
    Set repeat mode directly: off | one | all.
    Driving the cycle locally avoids reading stale state from Apple Music.
    """
    as_value = {"off": "off", "one": "one", "all": "all"}.get(mode, "off")
    run_osascript(f'tell application "Music" to set song repeat to {as_value}')


# --- Shuffle ------------------------------------------------------------------

def music_cmd_toggle_shuffle() -> bool:
    """
    Toggle Apple Music shuffle enabled on/off.
    Returns the new shuffle state.
    """
    script = r'''
tell application "Music"
    set shuffle enabled to not (shuffle enabled)
    return shuffle enabled as string
end tell
'''.strip()
    out = run_osascript(script)
    return out.strip().lower() == "true"


# --- Search -------------------------------------------------------------------

# Cap results so a broad query on a large library stays responsive to render.
SEARCH_LIMIT = 200


@dataclass
class SearchResult:
    persistent_id: str = ""
    track: str = ""
    artist: str = ""
    album: str = ""


def search_library(query: str) -> List[SearchResult]:
    """
    Search the library for tracks matching `query` by song name or artist.

    One osascript call runs both searches; duplicates are removed here rather
    than in AppleScript, where de-duping a list is quadratic.
    """
    query = " ".join(query.split())
    if not query:
        return []

    safe = applescript_quote(query)
    script = f'''
tell application "Music"
    set outText to ""
    set res to {{}}
    try
        set res to res & (search library playlist 1 for "{safe}" only songs)
    end try
    try
        set res to res & (search library playlist 1 for "{safe}" only artists)
    end try
    set n to 0
    repeat with t in res
        if n >= {SEARCH_LIMIT * 2} then exit repeat
        try
            set outText to outText & (persistent ID of t) & tab & (name of t as string) & tab & (artist of t as string) & tab & (album of t as string) & linefeed
            set n to n + 1
        end try
    end repeat
    return outText
end tell
'''.strip()

    out = run_osascript(script, timeout=20.0)

    results: List[SearchResult] = []
    seen = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pid, track, artist, album = (p.strip() for p in parts[:4])
        if not pid or pid in seen:
            continue
        seen.add(pid)
        results.append(SearchResult(pid, track, artist, album))
        if len(results) >= SEARCH_LIMIT:
            break
    return results


def music_cmd_play_track(persistent_id: str):
    """Play one library track by its persistent ID, which survives renames."""
    safe = applescript_quote(persistent_id)
    run_osascript(
        'tell application "Music" to play (first track of library playlist 1 '
        f'whose persistent ID is "{safe}")'
    )


# --- Playlists ----------------------------------------------------------------

def get_playlists() -> List[str]:
    """
    Return user playlist names, one per line, using manual concatenation to avoid
    AppleScript list-to-text coercion weirdness.
    """
    script = r'''
tell application "Music"
    try
        set outText to ""
        repeat with p in user playlists
            try
                if class of p is not folder playlist then
                    set outText to outText & (name of p as string) & linefeed
                end if
            end try
        end repeat
        return outText
    on error
        return ""
    end try
end tell
'''.strip()

    out = run_osascript(script)
    names = [line for line in out.splitlines() if line.strip()]

    # De-dupe while preserving order
    seen = set()
    uniq = []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


# --- UI helpers ---------------------------------------------------------------

class Mode:
    MAIN = "main"
    PLAYLISTS = "playlists"
    SEARCH_INPUT = "search_input"
    SEARCH_RESULTS = "search_results"


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def truncate(s: str, max_width: int) -> str:
    """Clip string to max_width, replacing the last char with ELLIPSIS if truncated."""
    if max_width <= 0:
        return ""
    if len(s) <= max_width:
        return s
    return s[:max_width - 1] + ELLIPSIS


def clamp_scroll(selected: int, scroll: int, total: int, height: int) -> int:
    """Slide a scroll window just far enough to keep `selected` visible."""
    if selected < scroll:
        scroll = selected
    if selected >= scroll + height:
        scroll = selected - height + 1
    return clamp(scroll, 0, max(0, total - height))


def safe_addstr(stdscr, y: int, x: int, s: str, attr: int = 0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    s = s[: max(0, w - x - 1)]
    # If no color pair is already specified in attr, apply the default pair.
    if not (attr & curses.A_COLOR):
        attr |= curses.color_pair(CP_DEFAULT)
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass


def draw_box(stdscr, y: int, x: int, height: int, width: int, attr: int = 0):
    if not (attr & curses.A_COLOR):
        attr |= curses.color_pair(CP_DEFAULT)
    h, w = stdscr.getmaxyx()
    try:
        stdscr.addch(y, x, curses.ACS_ULCORNER, attr)
        stdscr.addch(y, x + width - 1, curses.ACS_URCORNER, attr)
        stdscr.addch(y + height - 1, x, curses.ACS_LLCORNER, attr)
        # Bottom-right corner can raise in some terminals; guard it
        if y + height - 1 < h - 1 or x + width - 1 < w - 1:
            stdscr.addch(y + height - 1, x + width - 1, curses.ACS_LRCORNER, attr)
        stdscr.hline(y, x + 1, curses.ACS_HLINE, width - 2, attr)
        stdscr.hline(y + height - 1, x + 1, curses.ACS_HLINE, width - 2, attr)
        stdscr.vline(y + 1, x, curses.ACS_VLINE, height - 2, attr)
        stdscr.vline(y + 1, x + width - 1, curses.ACS_VLINE, height - 2, attr)
    except curses.error:
        pass


def draw_progress_bar(stdscr, y: int, x: int, width: int, pos: float, dur: float):
    if width <= 0:
        return
    if dur <= 0:
        filled = 0
    else:
        filled = clamp(int((pos / dur) * width), 0, width)

    empty = width - filled

    if filled == 0:
        # Nothing played yet — all empty
        safe_addstr(stdscr, y, x, "-" * width, curses.color_pair(CP_DEFAULT) | curses.A_DIM)
    elif filled == width:
        # Fully played — all hashes, no room for marker
        safe_addstr(stdscr, y, x, "#" * width, curses.color_pair(CP_GREEN))
    else:
        # Normal case: hashes + playhead marker + empty
        if filled > 1:
            safe_addstr(stdscr, y, x, "#" * (filled - 1), curses.color_pair(CP_GREEN))
        safe_addstr(stdscr, y, x + filled - 1, ">", curses.color_pair(CP_GREEN) | curses.A_BOLD)
        safe_addstr(stdscr, y, x + filled, "-" * empty, curses.color_pair(CP_DEFAULT) | curses.A_DIM)


# --- Compact layout -----------------------------------------------------------

def draw_progress_dots(stdscr, y: int, x: int, pos: float, dur: float):
    """Compact counterpart to draw_progress_bar: a DOTS_ROWS x DOTS_COLS dot grid."""
    segments = DOTS_ROWS * DOTS_COLS
    if dur > 0:
        done = clamp(int((pos / dur) * segments), 0, segments)
        # The dot currently being played, so the grid is never blank mid-track.
        playhead = min(done, segments - 1)
    else:
        done, playhead = 0, -1

    for i in range(segments):
        row, col = divmod(i, DOTS_COLS)  # reading order: across, then down
        if i < done or i == playhead:
            glyph = DOT_FILL
            attr = curses.color_pair(CP_GREEN)
            if i == playhead:
                attr |= curses.A_BOLD
        else:
            glyph = DOT_EMPTY
            attr = curses.color_pair(CP_DEFAULT) | curses.A_DIM
        safe_addstr(stdscr, y + row, x + col * DOTS_GAP, glyph, attr)


def compact_hint_rows(w: int) -> List[str]:
    """Two rows covering every binding, in the most legible form that fits.

    Degrades label length, then spacing, then drops to bare keys. Both rows use
    the same form so the columns stay aligned.
    """
    budget = w - 2
    for label_idx, gap in ((1, 2), (1, 1), (2, 2), (2, 1), (None, 2), (None, 1)):
        rows = [
            (" " * gap).join(
                item[0] if label_idx is None else f"{item[0]} {item[label_idx]}"
                for item in row
            )
            for row in COMPACT_KEYS
        ]
        if max(len(r) for r in rows) <= budget:
            return rows
    return rows  # narrowest form; safe_addstr clips anything still over


def draw_compact_main(stdscr, h: int, w: int, np: NowPlaying, footer: str = ""):
    """Minimised now-playing view.

    Eight rows: title, blank, three dot-grid rows carrying the times and state
    symbols alongside, blank, then two bottom-anchored hint rows listing every
    binding. A taller-but-narrow terminal just grows the gap in the middle.
    """
    if np.track or np.artist or np.album:
        line = f"{np.artist} {DASH} {np.track}" if np.artist else (np.track or np.album)
    else:
        line = "(nothing playing)"
    safe_addstr(stdscr, 0, 1, truncate(line, w - 2), curses.color_pair(CP_CYAN) | curses.A_BOLD)

    grid_y, grid_x = 2, 1  # row 1 stays blank
    draw_progress_dots(stdscr, grid_y, grid_x, np.position, np.duration)

    text_x = grid_x + DOTS_W + 2
    text_w = w - text_x - 1
    if text_w > 0:
        times = f"{format_time(np.position)} / {format_time(np.duration)}"
        safe_addstr(stdscr, grid_y, text_x, truncate(times, text_w))

        state_attr = (
            curses.color_pair(CP_GREEN) if np.state == "playing" else
            curses.color_pair(CP_YELLOW) if np.state == "paused" else
            curses.color_pair(CP_RED)
        )
        glyph = GLYPH_STATE.get(np.state, GLYPH_STATE["stopped"])
        safe_addstr(stdscr, grid_y + 1, text_x, truncate(glyph, text_w),
                    state_attr | curses.A_BOLD)

        flags = []
        if np.shuffle_enabled:
            flags.append(GLYPH_SHUFFLE)
        if np.repeat == "all":
            flags.append(GLYPH_REPEAT)
        elif np.repeat == "one":
            flags.append(GLYPH_REPEAT + "1")
        if flags:
            flags_x = text_x + len(glyph) + 1
            safe_addstr(stdscr, grid_y + 1, flags_x, truncate(" ".join(flags), w - flags_x - 1),
                        curses.color_pair(CP_GREEN))

    hint_rows = compact_hint_rows(w)
    if footer:
        # Flash takes the first hint row, matching the full layout.
        safe_addstr(stdscr, h - len(hint_rows), 1, truncate(footer, w - 2),
                    curses.color_pair(CP_CYAN) | curses.A_BOLD)
    else:
        for i, row in enumerate(hint_rows):
            safe_addstr(stdscr, h - len(hint_rows) + i, 1, row, curses.color_pair(CP_ORANGE))


def pick_fitting(options, budget: int) -> str:
    """First option that fits in `budget` columns, else the shortest one."""
    for option in options:
        if len(option) <= budget:
            return option
    return options[-1]


def draw_compact_list(stdscr, h: int, w: int, header: str, items: List[str],
                      selected: int, scroll: int, empty_msg: str) -> int:
    """Stripped scrolling list for the compact layout. Returns the adjusted scroll."""
    safe_addstr(stdscr, 0, 1, truncate(header, w - 2), curses.A_BOLD)

    if not items:
        safe_addstr(stdscr, 1, 1, truncate(empty_msg, w - 2))
        return scroll

    # Derived from the header/footer actually drawn above and below.
    list_top = 1
    list_height = max(1, h - list_top - 1)
    scroll = clamp_scroll(selected, scroll, len(items), list_height)

    marker_w = 2
    name_w = w - 2 - marker_w
    for i in range(list_height):
        idx = scroll + i
        if idx >= len(items):
            break
        if idx == selected:
            marker, attr = "> ", curses.color_pair(CP_CYAN) | curses.A_BOLD
        else:
            marker, attr = "  ", curses.color_pair(CP_DEFAULT)
        safe_addstr(stdscr, list_top + i, 1, marker + truncate(items[idx], name_w), attr)

    count = f"{selected + 1}/{len(items)}"
    footer = count + pick_fitting(LIST_HINTS, w - 2 - len(count))
    safe_addstr(stdscr, h - 1, 1, truncate(footer, w - 2), curses.color_pair(CP_ORANGE))
    return scroll


def draw_full_list(stdscr, h: int, w: int, header: str, items: List[str],
                   selected: int, scroll: int, empty_msg: str) -> int:
    """Scrolling list for the full layout. Returns the adjusted scroll."""
    safe_addstr(stdscr, 2, 2, truncate(header, w - 3), curses.A_BOLD)

    if not items:
        safe_addstr(stdscr, 4, 4, truncate(empty_msg, w - 5))
        return scroll

    list_top = 4
    list_height = max(3, h - list_top - 2)
    scroll = clamp_scroll(selected, scroll, len(items), list_height)

    for i in range(list_height):
        idx = scroll + i
        if idx >= len(items):
            break
        if idx == selected:
            attr = curses.color_pair(CP_CYAN) | curses.A_BOLD
        else:
            attr = curses.color_pair(CP_DEFAULT)
        safe_addstr(stdscr, list_top + i, 4, truncate(items[idx], w - 5), attr)

    count = f"{selected + 1}/{len(items)}"
    footer = count + pick_fitting(LIST_HINTS, w - 3 - len(count))
    safe_addstr(stdscr, h - 1, 2, truncate(footer, w - 3), curses.color_pair(CP_ORANGE))
    return scroll


def search_input_line(query: str, width: int) -> str:
    """Query plus a cursor block, scrolled to keep the tail visible."""
    text = query + "_"
    if len(text) <= width:
        return text
    return ELLIPSIS + text[-(width - 1):]


def wide_hint(w: int) -> str:
    """Every binding on one line, in the most legible form that fits."""
    budget = w - 2
    for label_idx, gap in ((1, 2), (1, 1), (2, 2), (2, 1), (None, 2), (None, 1)):
        items = [
            item[0] if label_idx is None else f"{item[0]} {item[label_idx]}"
            for row in COMPACT_KEYS for item in row
        ]
        line = (" " * gap).join(items)
        if len(line) <= budget:
            return line
    return line


def draw_wide_main(stdscr, h: int, w: int, np: NowPlaying, footer: str = ""):
    """Short-but-wide now-playing view.

    Five rows: title, horizontal progress bar, one status line, a gap, then a
    single bottom-anchored control line. Used when the terminal is too short for
    the full layout but wide enough that the compact dot grid wastes the width.
    """
    if np.track or np.artist or np.album:
        if np.artist and np.track:
            line = f"{np.artist} {DASH} {np.track}"
        else:
            line = np.track or np.artist
        if np.album:
            line += f"  [{np.album}]"
    else:
        line = f"(nothing playing{ELLIPSIS} or Music is being dramatic)"
    safe_addstr(stdscr, 0, 1, truncate(line, w - 2), curses.color_pair(CP_CYAN) | curses.A_BOLD)

    bar_x = 1
    bar_width = max(10, w - 5)
    safe_addstr(stdscr, 1, bar_x, "[")
    safe_addstr(stdscr, 1, bar_x + bar_width + 1, "]")
    draw_progress_bar(stdscr, 1, bar_x + 1, bar_width, pos=np.position, dur=np.duration)

    state_attr = (
        curses.color_pair(CP_GREEN) if np.state == "playing" else
        curses.color_pair(CP_YELLOW) if np.state == "paused" else
        curses.color_pair(CP_RED)
    )
    left = max(0.0, np.duration - np.position)
    times = f"{format_time(np.position)} / {format_time(np.duration)}"
    remaining = f"(left: {format_time(left)})"
    flags = []
    if np.shuffle_enabled:
        flags.append("shuffle")
    if np.repeat != "off":
        flags.append(f"repeat {np.repeat}")
    flag_text = "  ".join(flags)

    # Drop the least important segments first as the width shrinks.
    green = curses.color_pair(CP_GREEN)
    for parts in (
        [(times, 0), (remaining, curses.A_DIM), (np.state, state_attr), (flag_text, green)],
        [(times, 0), (np.state, state_attr), (flag_text, green)],
        [(times, 0), (np.state, state_attr)],
        [(times, 0)],
    ):
        parts = [seg for seg in parts if seg[0]]
        if sum(len(t) for t, _ in parts) + 4 * (len(parts) - 1) <= w - 2:
            break
    x = 1
    for text, attr in parts:
        safe_addstr(stdscr, 2, x, text, attr)
        x += len(text) + 4

    if footer:
        safe_addstr(stdscr, h - 1, 1, truncate(footer, w - 2),
                    curses.color_pair(CP_CYAN) | curses.A_BOLD)
    else:
        safe_addstr(stdscr, h - 1, 1, wide_hint(w), curses.color_pair(CP_ORANGE))


def draw_compact_search_input(stdscr, h: int, w: int, query: str, status: str):
    """Query prompt for the compact layout."""
    safe_addstr(stdscr, 0, 1, truncate("Search:", w - 2), curses.A_BOLD)
    safe_addstr(stdscr, 1, 1, search_input_line(query, w - 2),
                curses.color_pair(CP_CYAN) | curses.A_BOLD)
    if status:
        safe_addstr(stdscr, 3, 1, truncate(status, w - 2), curses.color_pair(CP_YELLOW))
    hint = pick_fitting(("enter search  esc back", "enter  esc back", "ent esc"), w - 2)
    safe_addstr(stdscr, h - 1, 1, hint, curses.color_pair(CP_ORANGE))


def draw_centered_message(stdscr, msg: str):
    """Immediate full-screen notice, used while a blocking call runs."""
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    msg = truncate(msg, w - 1)
    safe_addstr(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, curses.A_BOLD)
    stdscr.refresh()


# --- Main TUI -----------------------------------------------------------------

def run_tui(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(CP_DEFAULT, -1, -1)
    curses.init_pair(CP_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(CP_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(CP_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(CP_RED, curses.COLOR_RED, -1)
    orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    curses.init_pair(CP_ORANGE, orange, -1)

    stdscr.bkgd(' ', curses.color_pair(CP_DEFAULT))
    stdscr.bkgdset(' ', curses.color_pair(CP_DEFAULT))
    stdscr.erase()
    stdscr.nodelay(True)
    stdscr.keypad(True)

    mode = Mode.MAIN
    playlists: List[str] = []
    pl_selected = 0
    pl_scroll = 0

    search_query = ""
    search_status = ""
    search_results: List[SearchResult] = []
    search_lines: List[str] = []
    search_selected = 0
    search_scroll = 0

    last_poll = 0.0
    # "Truth" snapshot captured from Music.app at the last poll.
    np_at_poll = NowPlaying()
    np_poll_time = time.time()
    # What we display (may be locally advanced between polls).
    now_playing = NowPlaying()

    status_msg = "q quit | space play/pause | s search | n next | p prev | f shuffle | r repeat | l playlists"
    flash_msg = ""
    flash_until = 0.0
    menu_lines: List[str] = []
    menu_lines_width = 0  # terminal width at last pack

    def flash(msg: str, duration: float = 2.0):
        nonlocal flash_msg, flash_until
        flash_msg = msg
        flash_until = time.time() + duration

    def force_repoll(delay_sec: float = 0.0):
        """Immediately refresh the authoritative snapshot from Music.

        Background polling is intentionally slow to avoid hammering Apple Events.
        User actions, however, should update the UI immediately.
        """
        nonlocal np_at_poll, np_poll_time, last_poll
        if delay_sec > 0:
            time.sleep(delay_sec)
        t_now = time.time()
        np_at_poll = get_now_playing()
        np_poll_time = t_now
        last_poll = t_now

    while True:
        # Poll now-playing (truth) on a slow cadence.
        t = time.time()
        if t - last_poll >= POLL_INTERVAL_SEC:
            np_at_poll = get_now_playing()
            np_poll_time = t
            last_poll = t

        # Locally advance position each tick so the timer updates every second
        # without an AppleScript call. np_at_poll is the authoritative snapshot
        # from Music.app; we copy it here to avoid mutating the cached state.
        # The slow poll (~10s) will re-sync position with the real player state.
        now_playing = np_at_poll
        if now_playing.state == "playing":
            now_playing = NowPlaying(**now_playing.__dict__)
            elapsed = max(0.0, t - np_poll_time)
            now_playing.position = min(now_playing.duration, now_playing.position + elapsed)

        # Input
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
        elif ch != -1:
            if mode == Mode.MAIN:
                if ch in (ord("q"), ord("Q")):
                    break
                elif ch == ord(" "):
                    music_cmd_toggle_play_pause(now_playing)
                    force_repoll()
                    flash("Paused" if now_playing.state == "playing" else "Playing")
                elif ch in (ord("s"), ord("S")):
                    mode = Mode.SEARCH_INPUT
                    search_query = ""
                    search_status = ""
                elif ch in (ord("n"), ord("N")):
                    music_cmd_next()
                    # Give Music a brief moment to advance tracks before sampling.
                    force_repoll(0.15)
                    flash("Next track")
                elif ch in (ord("p"), ord("P")):
                    music_cmd_prev()
                    force_repoll(0.15)
                    flash("Previous track")
                elif ch in (ord("f"), ord("F")):
                    np_at_poll.shuffle_enabled = not np_at_poll.shuffle_enabled
                    music_cmd_toggle_shuffle()
                    flash(f"Shuffle: {'on' if np_at_poll.shuffle_enabled else 'off'}")
                elif ch in (ord("r"), ord("R")):
                    next_repeat = {"off": "one", "one": "all", "all": "off"}.get(np_at_poll.repeat, "off")
                    np_at_poll.repeat = next_repeat
                    music_cmd_set_repeat(next_repeat)
                    flash(f"Repeat: {next_repeat}")
                elif ch in (ord("l"), ord("L")):
                    playlists = get_playlists()
                    mode = Mode.PLAYLISTS
                    pl_selected = 0
                    pl_scroll = 0

            elif mode == Mode.PLAYLISTS:
                if ch in (ord("q"), ord("Q")):
                    break
                elif ch in (ord("b"), ord("B"), 27):  # b or ESC
                    mode = Mode.MAIN
                elif ch in (curses.KEY_UP, ord("k")):
                    pl_selected = clamp(pl_selected - 1, 0, max(0, len(playlists) - 1))
                elif ch in (curses.KEY_DOWN, ord("j")):
                    pl_selected = clamp(pl_selected + 1, 0, max(0, len(playlists) - 1))
                elif ch in (curses.KEY_ENTER, 10, 13):
                    if playlists:
                        music_cmd_play_playlist(playlists[pl_selected])
                        mode = Mode.MAIN
                        flash(f"Playing: {playlists[pl_selected].strip()}")
                        # Playlist selection is a user action; refresh immediately.
                        force_repoll(0.15)

            elif mode == Mode.SEARCH_INPUT:
                # Every printable key is query text here, so only Enter, Esc and
                # Backspace are treated as controls -- q must not quit.
                if ch in (curses.KEY_ENTER, 10, 13):
                    if search_query.strip():
                        draw_centered_message(stdscr, f"Searching{ELLIPSIS}")
                        search_results = search_library(search_query)
                        search_lines = [
                            f"{r.track} {DASH} {r.artist}" if r.artist else r.track
                            for r in search_results
                        ]
                        search_selected = 0
                        search_scroll = 0
                        curses.flushinp()  # drop keys typed during the blocking call
                        if search_results:
                            search_status = ""
                            mode = Mode.SEARCH_RESULTS
                        else:
                            search_status = "No matches"
                elif ch == 27:  # ESC
                    mode = Mode.MAIN
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    search_query = search_query[:-1]
                    search_status = ""
                elif 32 <= ch <= 126:
                    search_query += chr(ch)
                    search_status = ""

            elif mode == Mode.SEARCH_RESULTS:
                if ch in (ord("q"), ord("Q")):
                    break
                elif ch in (ord("b"), ord("B"), 27):  # back to refine the query
                    mode = Mode.SEARCH_INPUT
                elif ch in (curses.KEY_UP, ord("k")):
                    search_selected = clamp(search_selected - 1, 0, max(0, len(search_results) - 1))
                elif ch in (curses.KEY_DOWN, ord("j")):
                    search_selected = clamp(search_selected + 1, 0, max(0, len(search_results) - 1))
                elif ch in (curses.KEY_ENTER, 10, 13):
                    if search_results:
                        chosen = search_results[search_selected]
                        music_cmd_play_track(chosen.persistent_id)
                        mode = Mode.MAIN
                        flash(f"Playing: {chosen.track}")
                        force_repoll(0.15)

        # Draw
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        # Layout ladder: full -> wide -> compact -> "too small". Re-evaluated every
        # frame so resizing switches layouts seamlessly in every direction.
        if w >= MIN_COLS and h >= MIN_ROWS:
            layout = "full"
        elif w >= MIN_WIDE_COLS and h >= MIN_WIDE_ROWS:
            layout = "wide"
        elif w >= MIN_COMPACT_COLS and h >= MIN_COMPACT_ROWS:
            layout = "compact"
        else:
            layout = "tiny"

        if layout == "tiny":
            for msg in (f"Terminal too small {DASH} please resize", "Terminal too small", "too small"):
                if len(msg) <= w - 1:
                    break
            msg = truncate(msg, w - 1)
            safe_addstr(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg, curses.A_BOLD)
            stdscr.refresh()
            time.sleep(0.02)
            continue

        if layout in ("wide", "compact"):
            # Only the now-playing view differs; the list views are already
            # height-efficient and adapt to whatever width they are given.
            if mode == Mode.MAIN:
                draw_main = draw_wide_main if layout == "wide" else draw_compact_main
                draw_main(stdscr, h, w, now_playing,
                          flash_msg if t < flash_until else "")
            elif mode == Mode.PLAYLISTS:
                pl_scroll = draw_compact_list(stdscr, h, w, "Playlists:", playlists,
                                              pl_selected, pl_scroll, "(no playlists found)")
            elif mode == Mode.SEARCH_INPUT:
                draw_compact_search_input(stdscr, h, w, search_query, search_status)
            elif mode == Mode.SEARCH_RESULTS:
                search_scroll = draw_compact_list(stdscr, h, w, "Results:", search_lines,
                                                  search_selected, search_scroll, "(no matches)")
            stdscr.refresh()
            time.sleep(0.02)
            continue

        # Title — truncate with ellipsis if terminal is narrow
        title_full = "Apple Music TUI (because GUI is for the weak)"
        safe_addstr(stdscr, 1, 2, truncate(title_full, w - 3), curses.A_BOLD)

        if mode == Mode.MAIN:
            # Box around now playing section
            box_x = 1
            box_y = 2
            box_w = w - 2
            box_h = 7
            draw_box(stdscr, box_y, box_x, box_h, box_w)

            cx = box_x + 2  # content x: inside border + 1 space padding

            if now_playing.track or now_playing.artist or now_playing.album:
                np_line = f"{now_playing.artist} — {now_playing.track}"
                if now_playing.album:
                    np_line += f"  [{now_playing.album}]"
            else:
                np_line = f"(nothing playing{ELLIPSIS} or Music is being dramatic)"

            # inner_w: usable columns inside the box (borders + 1 space padding each side)
            inner_w = box_w - 4
            safe_addstr(stdscr, 2, cx, "Now Playing:", curses.A_BOLD)
            safe_addstr(stdscr, 3, cx + 2, truncate(np_line, inner_w - 2), curses.color_pair(CP_CYAN) | curses.A_BOLD)

            # State label + colored value
            state_color = (
                curses.color_pair(CP_GREEN) if now_playing.state == "playing" else
                curses.color_pair(CP_YELLOW) if now_playing.state == "paused" else
                curses.color_pair(CP_RED)
            )
            safe_addstr(stdscr, 5, cx, "State: ")
            state_x = cx + len("State: ")
            safe_addstr(stdscr, 5, state_x, now_playing.state, state_color)

            # Shuffle + Repeat: show progressively less on narrow terminals
            shuffle = "on" if now_playing.shuffle_enabled else "off"
            shuffle_color = curses.color_pair(CP_GREEN) if now_playing.shuffle_enabled else curses.color_pair(CP_DEFAULT)
            shuffle_x = state_x + len(now_playing.state) + 4
            repeat_color = curses.color_pair(CP_GREEN) if now_playing.repeat != "off" else curses.color_pair(CP_DEFAULT)
            repeat_x = shuffle_x + len("Shuffle: ") + len(shuffle) + 4
            full_repeat_end = repeat_x + len("Repeat: ") + len(now_playing.repeat)

            if full_repeat_end < w - cx:
                # Enough room for shuffle + repeat
                safe_addstr(stdscr, 5, shuffle_x, "Shuffle: ")
                safe_addstr(stdscr, 5, shuffle_x + len("Shuffle: "), shuffle, shuffle_color)
                safe_addstr(stdscr, 5, repeat_x, "Repeat: ")
                safe_addstr(stdscr, 5, repeat_x + len("Repeat: "), now_playing.repeat, repeat_color)
            elif shuffle_x + len("Shuffle: ") + len(shuffle) < w - cx:
                # Only enough room for shuffle
                safe_addstr(stdscr, 5, shuffle_x, "Shuffle: ")
                safe_addstr(stdscr, 5, shuffle_x + len("Shuffle: "), shuffle, shuffle_color)

            dur = now_playing.duration
            pos = now_playing.position
            left = max(0.0, dur - pos)

            time_line = f"{format_time(pos)} / {format_time(dur)}   (left: {format_time(left)})"
            safe_addstr(stdscr, 6, cx, truncate(time_line, inner_w))

            # Progress bar position derived from box bottom, not hardcoded
            bar_y = box_y + box_h + 1
            bar_x = 2
            bar_width = max(10, w - 6)
            if bar_y < h - 3:
                safe_addstr(stdscr, bar_y, bar_x, "[")
                safe_addstr(stdscr, bar_y, bar_x + bar_width + 1, "]")
                draw_progress_bar(stdscr, bar_y, bar_x + 1, bar_width, pos=pos, dur=dur)

            if t < flash_until:
                safe_addstr(stdscr, h - 2, 2, flash_msg, curses.color_pair(CP_CYAN) | curses.A_BOLD)
            else:
                # Repack menu lines only when terminal width changes.
                if w != menu_lines_width:
                    items = status_msg.split(" | ")
                    menu_lines = []
                    current = ""
                    for item in items:
                        candidate = item if not current else current + " | " + item
                        if len(candidate) <= w - 4:
                            current = candidate
                        else:
                            if current:
                                menu_lines.append(current)
                            current = item
                    if current:
                        menu_lines.append(current)
                    menu_lines_width = w
                for i, line in enumerate(reversed(menu_lines)):
                    row = h - 2 - i * 2
                    if row > bar_y + 1:
                        safe_addstr(stdscr, row, 2, line, curses.color_pair(CP_ORANGE))

        elif mode == Mode.PLAYLISTS:
            pl_scroll = draw_full_list(
                stdscr, h, w,
                "Playlists (Enter to play, b/Esc to back, q to quit):",
                playlists, pl_selected, pl_scroll,
                f"(no playlists found{ELLIPSIS} which is a lie, but here we are)",
            )

        elif mode == Mode.SEARCH_INPUT:
            safe_addstr(stdscr, 2, 2, truncate("Search library (Enter to run, Esc to cancel):", w - 3),
                        curses.A_BOLD)
            safe_addstr(stdscr, 4, 4, search_input_line(search_query, w - 6),
                        curses.color_pair(CP_CYAN) | curses.A_BOLD)
            if search_status:
                safe_addstr(stdscr, 6, 4, truncate(search_status, w - 5), curses.color_pair(CP_YELLOW))
            safe_addstr(stdscr, h - 1, 2, truncate("Matches song and artist names", w - 3),
                        curses.color_pair(CP_ORANGE))

        elif mode == Mode.SEARCH_RESULTS:
            search_scroll = draw_full_list(
                stdscr, h, w,
                "Search results (Enter to play, b/Esc to refine, q to quit):",
                search_lines, search_selected, search_scroll,
                "(no matches)",
            )

        stdscr.refresh()
        time.sleep(0.02)


def main():
    curses.wrapper(run_tui)


if __name__ == "__main__":
    main()
