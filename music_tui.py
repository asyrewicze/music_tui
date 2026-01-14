#!/usr/bin/env python3
import curses
import subprocess
import time
from dataclasses import dataclass
from typing import List


POLL_INTERVAL_SEC = 0.5


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


def get_now_playing() -> NowPlaying:
    # One call returns a tab-delimited row:
    # track \t artist \t album \t duration \t position \t state
    script = r'''
tell application "Music"
    set pstate to "stopped"
    try
        set pstate to (player state as string)
    on error
        return "		0	0	unknown"
    end try

    if pstate is "stopped" then
        return "		0	0	" & pstate
    end if

    try
        set t to current track
        set trackName to (name of t) as string
        set artistName to (artist of t) as string
        set albumName to (album of t) as string
        set dur to (duration of t) as string
        set pos to (player position) as string
        return trackName & tab & artistName & tab & albumName & tab & dur & tab & pos & tab & pstate
    on error
        return "		0	0	" & pstate
    end try
end tell
'''.strip()

    out = run_osascript(script)
    parts = out.split("\t")
    while len(parts) < 6:
        parts.append("")

    track, artist, album, dur_s, pos_s, state = parts[:6]

    try:
        dur = float(dur_s) if dur_s else 0.0
    except ValueError:
        dur = 0.0

    try:
        pos = float(pos_s) if pos_s else 0.0
    except ValueError:
        pos = 0.0

    state = (state or "unknown").strip().lower()
    if state not in ("playing", "paused", "stopped"):
        state = "unknown"

    return NowPlaying(
        track=track.strip(),
        artist=artist.strip(),
        album=album.strip(),
        duration=max(dur, 0.0),
        position=max(pos, 0.0),
        state=state,
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
    run_osascript(f'tell application "Music" to play playlist "{safe}"')


def music_cmd_toggle_play_pause(np: NowPlaying):
    if np.state == "playing":
        music_cmd_pause()
    else:
        music_cmd_play()


# --- Shuffle ------------------------------------------------------------------

def music_get_shuffle_enabled() -> bool:
    """
    Returns True if shuffle is enabled, else False.
    AppleScript returns 'true'/'false'.
    """
    out = run_osascript('tell application "Music" to get shuffle enabled')
    return out.strip().lower() == "true"


def music_cmd_toggle_shuffle():
    """
    Toggle Apple Music shuffle enabled on/off.
    """
    script = r'''
tell application "Music"
    set shuffle enabled to not (shuffle enabled)
    return shuffle enabled as string
end tell
'''.strip()
    run_osascript(script)


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
                set outText to outText & (name of p as string) & linefeed
            end try
        end repeat
        return outText
    on error
        return ""
    end try
end tell
'''.strip()

    out = run_osascript(script)
    names = [line.strip() for line in out.splitlines() if line.strip()]

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
    # terminal default fg/bg below
    base = curses.color_pair(1)
    try:
        stdscr.addstr(y, x, s, attr | base)
    except curses.error:
        pass


def draw_progress_bar(stdscr, y: int, x: int, width: int, pos: float, dur: float):
    if width <= 0:
        return
    if dur <= 0:
        filled = 0
    else:
        filled = clamp(int((pos / dur) * width), 0, width)
    bar = ("#" * filled) + ("-" * (width - filled))
    safe_addstr(stdscr, y, x, bar)


# --- Main TUI -----------------------------------------------------------------

def run_tui(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()

    # Pair 1 = terminal defaults (fg + bg)
    curses.init_pair(1, -1, -1)
    BASE = curses.color_pair(1)

    # Use terminal theme background when clearing/redrawing
    stdscr.bkgd(' ', BASE)
    stdscr.bkgdset(' ', BASE)
    stdscr.erase()
    stdscr.nodelay(True)
    stdscr.keypad(True)

    mode = Mode.MAIN
    playlists: List[str] = []
    pl_selected = 0
    pl_scroll = 0

    last_poll = 0.0
    now_playing = NowPlaying()

    status_msg = "q quit | space play/pause | s stop | n next | p prev | f shuffle | l playlists"

    while True:
        # Poll now-playing
        t = time.time()
        if t - last_poll >= POLL_INTERVAL_SEC:
            now_playing = get_now_playing()
            last_poll = t

        # Input
        ch = stdscr.getch()
        if ch != -1:
            if mode == Mode.MAIN:
                if ch in (ord("q"), ord("Q")):
                    break
                elif ch == ord(" "):
                    music_cmd_toggle_play_pause(now_playing)
                elif ch in (ord("s"), ord("S")):
                    music_cmd_stop()
                elif ch in (ord("n"), ord("N")):
                    music_cmd_next()
                elif ch in (ord("p"), ord("P")):
                    music_cmd_prev()
                elif ch in (ord("f"), ord("F")):
                    music_cmd_toggle_shuffle()
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
                elif ch in (curses.KEY_ENTER, 10, 13):  # Enter -> play selected playlist
                    if playlists:
                        music_cmd_play_playlist(playlists[pl_selected])
                        mode = Mode.MAIN

        # Draw
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = "Apple Music TUI (because GUI is for the weak)"
        safe_addstr(stdscr, 0, 2, title, curses.A_BOLD)

        if mode == Mode.MAIN:
            # Now playing line
            if now_playing.track or now_playing.artist or now_playing.album:
                np_line = f"{now_playing.artist} — {now_playing.track}"
                if now_playing.album:
                    np_line += f"  [{now_playing.album}]"
            else:
                np_line = "(nothing playing… or Music is being dramatic)"

            safe_addstr(stdscr, 2, 2, "Now Playing:", curses.A_BOLD)
            safe_addstr(stdscr, 3, 4, np_line)

            # State + shuffle + times
            shuffle = "on" if music_get_shuffle_enabled() else "off"
            safe_addstr(stdscr, 5, 2, f"State: {now_playing.state}    Shuffle: {shuffle}")

            dur = now_playing.duration
            pos = now_playing.position
            left = max(0.0, dur - pos)

            time_line = f"{format_time(pos)} / {format_time(dur)}   (left: {format_time(left)})"
            safe_addstr(stdscr, 6, 2, time_line)

            # Progress bar
            bar_y = 8
            bar_x = 2
            bar_width = max(10, w - 6)  # bar content width (inside brackets)
            safe_addstr(stdscr, bar_y, bar_x, "[")
            safe_addstr(stdscr, bar_y, bar_x + bar_width + 1, "]")
            draw_progress_bar(stdscr, bar_y, bar_x + 1, bar_width, pos=pos, dur=dur)

            # Footer
            safe_addstr(stdscr, h - 2, 2, status_msg)

        elif mode == Mode.PLAYLISTS:
            safe_addstr(stdscr, 2, 2, "Playlists (Enter to play, b/Esc to back, q to quit):", curses.A_BOLD)

            if not playlists:
                safe_addstr(stdscr, 4, 4, "(no playlists found… which is a lie, but here we are)")
            else:
                list_top = 4
                list_height = max(3, h - list_top - 2)

                # Keep selection in view
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
                    attr = curses.A_REVERSE if idx == pl_selected else 0
                    safe_addstr(stdscr, list_top + i, 4, name, attr)

                footer = f"{pl_selected + 1}/{len(playlists)}  (↑↓ or j/k)"
                safe_addstr(stdscr, h - 1, 2, footer)

        stdscr.refresh()
        time.sleep(0.02)


def main():
    curses.wrapper(run_tui)


if __name__ == "__main__":
    main()
