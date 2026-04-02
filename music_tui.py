#!/usr/bin/env python3
import curses
import subprocess
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
CP_DIM     = 6
CP_ORANGE  = 7


def run_osascript(script: str) -> str:
    """
    Execute AppleScript via osascript and return stdout (stripped).
    """
    try:
        p = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        return (p.stdout or "").strip()
    except Exception:
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


def get_now_playing() -> NowPlaying:
    # One call returns a tab-delimited row:
    # track \t artist \t album \t duration \t position \t state \t shuffleEnabled
    script = r'''
tell application "Music"
    set shuffleState to "false"
    set pstate to "stopped"
    try
        set pstate to (player state as string)
        set shuffleState to (shuffle enabled as string)
    on error
        return "\t\t\t0\t0\tstopped\tfalse"
    end try

    if pstate is "stopped" then
        return "\t\t\t0\t0\t" & pstate & "\t" & shuffleState
    end if

    try
        set t to current track
        set trackName to (name of t) as string
        set artistName to (artist of t) as string
        set albumName to (album of t) as string
        set dur to (duration of t) as string
        set pos to (player position) as string
        return trackName & tab & artistName & tab & albumName & tab & dur & tab & pos & tab & pstate & tab & shuffleState
    on error
        return "\t\t\t0\t0\t" & pstate & "\t" & shuffleState
    end try
end tell
'''.strip()

    out = run_osascript(script)
    parts = out.split("\t")
    while len(parts) < 7:
        parts.append("")

    track, artist, album, dur_s, pos_s, state, shuffle_s = parts[:7]

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

    return NowPlaying(
        track=track.strip(),
        artist=artist.strip(),
        album=album.strip(),
        duration=max(dur, 0.0),
        position=max(pos, 0.0),
        state=state,
        shuffle_enabled=shuffle_enabled,
    )


# --- Apple Music commands -----------------------------------------------------

def music_cmd_play():
    run_osascript('tell application "Music" to play')


def music_cmd_pause():
    run_osascript('tell application "Music" to pause')


def music_cmd_stop():
    run_osascript('tell application "Music" to stop')


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
    safe = name.replace('"', '\\"')
    run_osascript(f'tell application "Music" to play user playlist "{safe}"')


def music_cmd_toggle_play_pause(np: NowPlaying):
    if np.state == "playing":
        music_cmd_pause()
    else:
        music_cmd_play()


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


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


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
    curses.init_pair(CP_DIM, -1, -1)
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

    last_poll = 0.0
    # "Truth" snapshot captured from Music.app at the last poll.
    np_at_poll = NowPlaying()
    np_poll_time = time.time()
    # What we display (may be locally advanced between polls).
    now_playing = NowPlaying()

    status_msg = "q quit | space play/pause | s stop | n next | p prev | f shuffle | l playlists"

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
        if ch != -1:
            if mode == Mode.MAIN:
                if ch in (ord("q"), ord("Q")):
                    break
                elif ch == ord(" "):
                    music_cmd_toggle_play_pause(now_playing)
                    force_repoll()
                elif ch in (ord("s"), ord("S")):
                    music_cmd_stop()
                    force_repoll()
                elif ch in (ord("n"), ord("N")):
                    music_cmd_next()
                    # Give Music a brief moment to advance tracks before sampling.
                    force_repoll(0.15)
                elif ch in (ord("p"), ord("P")):
                    music_cmd_prev()
                    force_repoll(0.15)
                elif ch in (ord("f"), ord("F")):
                    np_at_poll.shuffle_enabled = music_cmd_toggle_shuffle()
                    force_repoll()
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
                        # Playlist selection is a user action; refresh immediately.
                        force_repoll(0.15)

        # Draw
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = "Apple Music TUI (because GUI is for the weak)"
        safe_addstr(stdscr, 0, 2, title, curses.A_BOLD)

        if mode == Mode.MAIN:
            # Box around now playing section
            box_x = 1
            box_y = 1
            box_w = w - 2
            box_h = 7
            draw_box(stdscr, box_y, box_x, box_h, box_w)

            cx = box_x + 2  # content x: inside border + 1 space padding

            if now_playing.track or now_playing.artist or now_playing.album:
                np_line = f"{now_playing.artist} — {now_playing.track}"
                if now_playing.album:
                    np_line += f"  [{now_playing.album}]"
            else:
                np_line = "(nothing playing… or Music is being dramatic)"

            safe_addstr(stdscr, 2, cx, "Now Playing:", curses.A_BOLD)
            safe_addstr(stdscr, 3, cx + 2, np_line, curses.color_pair(CP_CYAN) | curses.A_BOLD)

            # State label + colored value
            state_color = (
                curses.color_pair(CP_GREEN) if now_playing.state == "playing" else
                curses.color_pair(CP_YELLOW) if now_playing.state == "paused" else
                curses.color_pair(CP_RED)
            )
            safe_addstr(stdscr, 5, cx, "State: ")
            state_x = cx + len("State: ")
            safe_addstr(stdscr, 5, state_x, now_playing.state, state_color)

            # Shuffle label + colored value
            shuffle = "on" if now_playing.shuffle_enabled else "off"
            shuffle_color = curses.color_pair(CP_GREEN) if now_playing.shuffle_enabled else curses.color_pair(CP_DEFAULT)
            shuffle_x = state_x + len(now_playing.state) + 4
            safe_addstr(stdscr, 5, shuffle_x, "Shuffle: ")
            safe_addstr(stdscr, 5, shuffle_x + len("Shuffle: "), shuffle, shuffle_color)

            dur = now_playing.duration
            pos = now_playing.position
            left = max(0.0, dur - pos)

            time_line = f"{format_time(pos)} / {format_time(dur)}   (left: {format_time(left)})"
            safe_addstr(stdscr, 6, cx, time_line)

            bar_y = 9
            bar_x = 2
            bar_width = max(10, w - 6)
            safe_addstr(stdscr, bar_y, bar_x, "[")
            safe_addstr(stdscr, bar_y, bar_x + bar_width + 1, "]")
            draw_progress_bar(stdscr, bar_y, bar_x + 1, bar_width, pos=pos, dur=dur)

            safe_addstr(stdscr, h - 2, 2, status_msg, curses.color_pair(CP_ORANGE))

        elif mode == Mode.PLAYLISTS:
            safe_addstr(stdscr, 2, 2, "Playlists (Enter to play, b/Esc to back, q to quit):", curses.A_BOLD)

            if not playlists:
                safe_addstr(stdscr, 4, 4, "(no playlists found… which is a lie, but here we are)")
            else:
                list_top = 4
                list_height = max(3, h - list_top - 2)

                if pl_selected < pl_scroll:
                    pl_scroll = pl_selected
                if pl_selected >= pl_scroll + list_height:
                    pl_scroll = pl_selected - list_height + 1
                pl_scroll = clamp(pl_scroll, 0, max(0, len(playlists) - list_height))

                for i in range(list_height):
                    idx = pl_scroll + i
                    if idx >= len(playlists):
                        break
                    name = playlists[idx]
                    if idx == pl_selected:
                        attr = curses.color_pair(CP_CYAN) | curses.A_BOLD
                    else:
                        attr = curses.color_pair(CP_DEFAULT)
                    safe_addstr(stdscr, list_top + i, 4, name, attr)

                footer = f"{pl_selected + 1}/{len(playlists)}  (↑↓ or j/k)"
                safe_addstr(stdscr, h - 1, 2, footer, curses.color_pair(CP_ORANGE))

        stdscr.refresh()
        time.sleep(0.02)


def main():
    curses.wrapper(run_tui)


if __name__ == "__main__":
    main()
