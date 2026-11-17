"""Interactive task selection, a live elapsed timer, and Google Calendar logging.

Kept separate from todoist_cli.py so the terminal/subprocess plumbing stays
isolated and unit-testable. No Todoist API logic lives here.
"""

import contextlib
import itertools
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tomllib
from datetime import date, datetime, timedelta

from todoist_cli import paths

try:  # POSIX-only; the interactive picker degrades gracefully without these.
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX fallback
    termios = None
    tty = None

def configured_account(env=None, config_path=None) -> str:
    """The Google account `start`, `log` and `plan` use: $GOG_ACCOUNT, else
    ``account`` from plan.toml in the config dir, else "" (unconfigured).

    Raises ValueError if plan.toml exists but can't be parsed, so the caller
    can say so instead of claiming no account is set.
    """
    env = os.environ if env is None else env
    if env.get("GOG_ACCOUNT"):
        return env["GOG_ACCOUNT"]
    path = config_path or paths.config_dir(env) / "plan.toml"
    try:
        with open(path, "rb") as f:
            account = tomllib.load(f).get("account")
    except FileNotFoundError:
        return ""
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    return account if isinstance(account, str) else ""


# Calendar time-tracking events are written to.
GOG_CALENDAR = "primary"

# Google Calendar event color IDs (1-11) with the hex backgrounds Google uses,
# so terminal swatches match what the event will actually look like. See
# `gog calendar colors`. ID None = no --event-color flag (calendar default).
EVENT_COLORS = [
    (None, "Default (calendar default)", None),
    (1, "Lavender", "#a4bdfc"),
    (2, "Sage", "#7ae7bf"),
    (3, "Grape", "#dbadff"),
    (4, "Flamingo", "#ff887c"),
    (5, "Banana", "#fbd75b"),
    (6, "Tangerine", "#ffb878"),
    (7, "Peacock", "#46d6db"),
    (8, "Graphite", "#e1e1e1"),
    (9, "Blueberry", "#5484ed"),
    (10, "Basil", "#51b749"),
    (11, "Tomato", "#dc2127"),
]


def format_elapsed(delta: timedelta) -> str:
    """Render a timedelta as MM:SS, widening to HH:MM:SS once it hits an hour."""
    total = int(delta.total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# SGR codes: \x1b[1m turns on bold; \x1b[22m turns bold off without disturbing
# any other attribute (e.g. the picker's reverse-video on the selected row).
_BOLD_ON = "\x1b[1m"
_BOLD_OFF = "\x1b[22m"
_RESET = "\x1b[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Foreground color per user-facing priority (1 = most urgent). P4 (normal) is
# left uncolored. We close with \x1b[39m (default foreground) rather than a full
# reset so the color can't cancel the picker's reverse-video on a selected row.
_FG_DEFAULT = "\x1b[39m"
_FG_RED = "\x1b[31m"
_PRIORITY_COLOR = {
    1: _FG_RED,
    2: "\x1b[33m",  # yellow
    3: "\x1b[34m",  # blue
}


def priority_tag(user_priority: int, *, color: bool = False) -> str:
    """`P<n>` tag, padded to a constant visible width so titles line up.

    Optionally colored by urgency for TTY display. Public: shared between
    format_row (the `start` picker) and todoist_cli's `plan` display, so both
    render priority the same way.
    """
    tag = f"P{user_priority}".ljust(2)
    if color and user_priority in _PRIORITY_COLOR:
        return f"{_PRIORITY_COLOR[user_priority]}{tag}{_FG_DEFAULT}"
    return tag


def bold(text: str) -> str:
    """Wrap ``text`` so it renders bold, then fully resets styling."""
    return f"{_BOLD_ON}{text}{_RESET}"


def red(text: str) -> str:
    """Wrap ``text`` in red, closing with default-foreground rather than a full
    reset so it can't cancel the picker's reverse-video (see _PRIORITY_COLOR)."""
    return f"{_FG_RED}{text}{_FG_DEFAULT}"


def format_date(d: date) -> str:
    """Render a date as e.g. ``Jun 17``."""
    return d.strftime("%b %-d")


def dates_include_today(*dates: date | None, today: date | None = None) -> bool:
    """True if any of `dates` is today (defaults to the real local today).

    The date-object counterpart of `_due_or_deadline_today`, for callers that
    already have `date` values (e.g. planner.Item) rather than a raw Todoist
    task dict with due/deadline sub-objects and NL date strings.
    """
    today = today or datetime.now().astimezone().date()
    return any(d == today for d in dates if d is not None)


def deadline_is_due(deadline: date | None, today: date | None = None) -> bool:
    """True when `deadline` is today or already past (defaults to the real local today).

    The date-object counterpart of `_deadline_is_due`, for callers that already
    have `date` values (e.g. planner.Item) rather than a Todoist task dict.
    """
    today = today or datetime.now().astimezone().date()
    return deadline is not None and deadline <= today


def _fmt_date(value: str) -> str:
    """Render an ISO date (``2026-06-17``) as ``Jun 17``.

    Leaves non-ISO values (e.g. Todoist due strings like ``today``) untouched.
    """
    try:
        return format_date(datetime.strptime(value[:10], "%Y-%m-%d").date())
    except (ValueError, TypeError):
        return value


def _due_or_deadline_today(task: dict) -> bool:
    """True if the task is due today or has a deadline of today.

    Overdue and future dates count as not-today. Non-ISO due strings (e.g.
    Todoist's ``today``) fall back to a literal match.
    """
    today = datetime.now().astimezone().date().isoformat()
    if task.get("due"):
        date = task["due"].get("date") or ""
        string = task["due"].get("string") or ""
        if date[:10] == today or string.strip().lower() == "today":
            return True
    if task.get("deadline"):
        if (task["deadline"].get("date") or "")[:10] == today:
            return True
    return False


def _deadline_is_due(task: dict) -> bool:
    """True when the task's deadline is today or already past.

    The API returns ``"deadline": None`` for tasks without one, hence the `or {}`.
    """
    raw = (task.get("deadline") or {}).get("date") or ""
    try:
        parsed = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return deadline_is_due(parsed)


def _date_suffix(task: dict, *, color: bool = False) -> str:
    """Parenthesized due/deadline summary, or '' when the task has neither.

    Examples: ``(Jun 17)``, ``(deadline Jun 20)``, ``(Jun 17, deadline Jun 20)``.
    With ``color``, a deadline that is today or already past renders red.
    """
    parts = []
    if task.get("due"):
        due = task["due"].get("string") or task["due"].get("date", "")
        if due:
            parts.append(_fmt_date(due))
    if task.get("deadline"):
        deadline = task["deadline"].get("date", "")
        if deadline:
            text = f"deadline {_fmt_date(deadline)}"
            if color and _deadline_is_due(task):
                text = red(text)
            parts.append(text)
    return f" ({', '.join(parts)})" if parts else ""


def _truncate_visible(text: str, width: int) -> str:
    """Truncate to ``width`` visible columns, ignoring ANSI escapes for the count.

    A plain ``text[:width]`` would miscount escape sequences as visible width and
    could even slice one in half. Always resets styling at the end so a cut made
    mid-bold can't bleed into the rest of the screen.
    """
    out = []
    visible = 0
    i = 0
    styled = False
    while i < len(text) and visible < width:
        match = _ANSI_RE.match(text, i)
        if match:
            out.append(match.group())
            styled = True
            i = match.end()
        else:
            out.append(text[i])
            visible += 1
            i += 1
    result = "".join(out)
    return result + _RESET if styled else result


def format_row(task: dict, *, bold_title: bool = False, color: bool = False) -> str:
    """One-line task summary: ``P2 <title> (Jun 17)``.

    With ``bold_title`` the title is bold-wrapped only when the task is due today
    or its deadline is today; the priority tag can be colored for TTY display, and
    the due date (and deadline, when present) trails in parentheses. With ``color``
    a deadline that is today or already past renders red. Uses the same
    user-facing priority convention as cmd_list (API priority 4=urgent maps to P1).
    """
    user_priority = 5 - task.get("priority", 1)
    title = task["content"]
    if bold_title and _due_or_deadline_today(task):
        title = f"{_BOLD_ON}{title}{_BOLD_OFF}"
    tag = priority_tag(user_priority, color=color)
    return f"{tag} {title}{_date_suffix(task, color=color)}"


def _key_action(data: bytes) -> str:
    """Map a raw key/escape sequence to an action.

    Returns one of: 'up', 'down', 'select', 'cancel', 'none'.
    Pure function so it can be tested without a real terminal.
    """
    if data in (b"\x1b[A", b"\x1bOA", b"k"):
        return "up"
    if data in (b"\x1b[B", b"\x1bOB", b"j"):
        return "down"
    if data in (b"\r", b"\n"):
        return "select"
    if data in (b"\x1b", b"\x03", b"q", b"Q"):
        return "cancel"
    return "none"


def select_task(tasks: list[dict]):
    """Let the user pick one task. Returns the chosen task dict, or None if cancelled.

    Uses an arrow-key picker on a TTY; falls back to a numbered prompt otherwise
    (pipes, tests). Ctrl-C is treated as a cancel (returns None) so the caller can
    print a clean "Cancelled." instead of dumping a traceback; the picker's own
    ``finally`` still restores the terminal as the interrupt unwinds.
    """
    try:
        if termios is None or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return _select_numbered(tasks)
        return _select_arrows(tasks)
    except KeyboardInterrupt:
        return None


def _select_numbered(tasks: list[dict]):
    for i, task in enumerate(tasks, 1):
        print(f"  {i}. {format_row(task)}")
    while True:
        try:
            raw = input(f"Select task [1-{len(tasks)}] (q to cancel): ").strip()
        except EOFError:
            return None
        if raw.lower() in ("", "q"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(tasks):
            return tasks[int(raw) - 1]
        print("Invalid selection.")


def _next_index(idx: int, count: int, action: str) -> int:
    """Pure index transition for the picker; wraps around at both ends."""
    if action == "up":
        return (idx - 1) % count
    if action == "down":
        return (idx + 1) % count
    return idx


def _scroll_offset(idx: int, count: int, height: int, prev: int) -> int:
    """Top-of-window index so that `idx` stays visible in a `height`-row viewport.

    Edge-follow (fzf-style): only scrolls when the selection crosses the top or
    bottom edge, otherwise the window stays put. Pure, so it's unit-testable like
    `_next_index`. `prev` is the previous offset.
    """
    if count <= height:
        return 0
    offset = prev
    if idx < offset:
        offset = idx
    elif idx >= offset + height:
        offset = idx - height + 1
    return max(0, min(offset, count - height))


def _viewport_height() -> int:
    """Rows available for the task viewport.

    Terminal height minus three reserved lines: the "Select a task…" header
    printed above the list, the status line drawn below it, and a bottom margin
    so the final row's newline never forces the terminal to scroll (a scroll
    would desync the cursor-up count and smear the redraw).
    """
    return max(1, shutil.get_terminal_size((80, 24)).lines - 3)


def _status_line(idx: int, count: int, offset: int, height: int) -> str:
    """Position indicator shown below the viewport when the list is scrolled."""
    above = " ↑ more above" if offset > 0 else ""
    below = " ↓ more below" if offset + height < count else ""
    return f"  [{idx + 1}/{count}]{above}{below}"


def _render(tasks: list[dict], idx: int, offset: int, height: int, first: bool) -> int:
    """Draw a `height`-row window of `tasks` and return the number of lines drawn.

    Only the rows in `tasks[offset:offset+height]` are drawn (plus a status line
    when the list is taller than the window), so the drawn-line count stays a
    constant for a given list regardless of its length — that constant is what the
    cursor-up move (`\\x1b[{drawn}A`) relies on to overwrite cleanly without smear.
    Each line is truncated to the terminal width so a long title can't wrap and
    throw the count off.
    """
    cols = shutil.get_terminal_size((80, 24)).columns
    count = len(tasks)
    has_status = count > height
    drawn = min(count, height) + (1 if has_status else 0)
    if not first:
        sys.stdout.write(f"\x1b[{drawn}A")  # move cursor up to overwrite
    for local_i, task in enumerate(tasks[offset:offset + height]):
        abs_i = offset + local_i
        prefix = "> " if abs_i == idx else "  "
        line = _truncate_visible(
            prefix + format_row(task, bold_title=True, color=True), cols
        )
        sys.stdout.write("\r\x1b[K")  # clear line
        if abs_i == idx:
            sys.stdout.write(f"\x1b[7m{line}\x1b[0m\n")  # reverse video
        else:
            sys.stdout.write(f"{line}\n")
    if has_status:
        status = _truncate_visible(_status_line(idx, count, offset, height), cols)
        sys.stdout.write("\r\x1b[K")
        sys.stdout.write(f"{status}\n")
    sys.stdout.flush()
    return drawn


class _PickerState:
    """Mutable cursor state shared between the picker loop and the resize handler.

    The SIGWINCH handler needs to repaint with the live selection/window, so these
    can't be loop-locals; they live here where both can read and update them.
    """

    __slots__ = ("idx", "offset", "height", "last_drawn")

    def __init__(self, height: int):
        self.idx = 0
        self.offset = 0
        self.height = height
        self.last_drawn = 0


def _picker_loop(state: "_PickerState", tasks: list[dict], read_key, render):
    """Drive the picker given a key reader and a render callback.

    Separated from terminal setup so the navigation/select/cancel logic can be
    unit-tested without a real TTY. ``read_key`` returns raw key bytes; ``render``
    takes (first) and draws from ``state``. Selection and the scroll window live in
    ``state`` so a resize handler can repaint between key presses.
    """
    render(True)
    while True:
        data = read_key()
        if not data:  # stdin EOF (b""): cancel cleanly instead of busy-looping
            return None
        action = _key_action(data)
        if action == "select":
            return tasks[state.idx]
        if action == "cancel":
            return None
        state.idx = _next_index(state.idx, len(tasks), action)
        state.offset = _scroll_offset(state.idx, len(tasks), state.height, state.offset)
        render(False)


def _select_arrows(tasks: list[dict]):
    height = _viewport_height()
    if height < 2:
        # Too few rows for a usable viewport plus status line; the numbered prompt
        # avoids an unwinnable cursor-positioning fight in a tiny terminal.
        return _select_numbered(tasks)

    print("Select a task (↑/↓ to move, Enter to choose, Esc/q to cancel):")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    state = _PickerState(height)

    def render(first: bool) -> None:
        state.last_drawn = _render(
            tasks, state.idx, state.offset, state.height, first
        )

    def on_resize(signum, frame) -> None:
        # The old line count is now invalid, so erase the previous block (move up
        # over it and clear to end of screen) and repaint fresh from the new size.
        if state.last_drawn:
            sys.stdout.write(f"\x1b[{state.last_drawn}A\x1b[0J")
        state.height = _viewport_height()
        state.offset = _scroll_offset(
            state.idx, len(tasks), state.height, state.offset
        )
        render(True)

    has_winch = hasattr(signal, "SIGWINCH")
    prev_handler = signal.signal(signal.SIGWINCH, on_resize) if has_winch else None
    try:
        tty.setcbreak(fd)
        return _picker_loop(
            state,
            tasks,
            read_key=lambda: os.read(fd, 6),
            render=render,
        )
    finally:
        if has_winch:
            signal.signal(signal.SIGWINCH, prev_handler)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _color_swatch(hex_color: str | None) -> str:
    """A two-cell truecolor block for ``#rrggbb``, so the terminal preview matches
    the real Google Calendar color. Returns blank cells for ``None`` (the default
    entry has no color) so every row's label stays aligned.
    """
    if hex_color is None:
        return "  "
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f"\x1b[48;2;{r};{g};{b}m  \x1b[0m"


def _render_colors(idx: int, first: bool) -> None:
    """Draw the color list, overwriting the previous block on redraw.

    Mirrors `_render`'s cursor-up + clear-line approach. The list is short and
    fixed-length, so there's no viewport/scroll bookkeeping: the drawn-line count
    is always len(EVENT_COLORS).
    """
    if not first:
        sys.stdout.write(f"\x1b[{len(EVENT_COLORS)}A")  # move up to overwrite
    for i, (_cid, name, hex_color) in enumerate(EVENT_COLORS):
        marker = "❯ " if i == idx else "  "
        label = bold(name) if i == idx else name
        sys.stdout.write("\r\x1b[K")  # clear line
        sys.stdout.write(f"{marker}{_color_swatch(hex_color)} {label}\n")
    sys.stdout.flush()


def _color_loop(read_key, render):
    """Drive the color picker given a key reader and a render callback.

    Separated from terminal setup so the navigation/select/cancel logic is
    unit-testable without a real TTY, like `_picker_loop`. Returns the chosen
    color id (int) or None. None means "use the calendar default" and is returned
    for the default row, for cancel (Esc/q), and for any non-input — an idle
    timeout (``read_key`` returns None) or stdin EOF (``read_key`` returns b"").
    Both no-input cases keep the default rather than aborting or busy-looping, so
    a finished-but-unattended session still logs promptly and never wedges.
    """
    idx = 0
    render(idx, True)
    while True:
        data = read_key()
        if not data:  # idle timeout (None) or stdin EOF (b""): keep the default
            return None
        action = _key_action(data)
        if action == "select":
            return EVENT_COLORS[idx][0]
        if action == "cancel":
            return None
        idx = _next_index(idx, len(EVENT_COLORS), action)
        render(idx, False)


# After a session is stopped, don't trap the terminal forever waiting on a color
# choice: if no key is pressed within this many seconds, fall back to the default
# and log the event. Restores the old fire-and-forget feel of stopping the timer.
_COLOR_IDLE_TIMEOUT = 120.0


def select_event_color():
    """Let the user pick a Google Calendar event color after a tracked session.

    Returns the chosen color id (1-11) or None for the calendar default. On a
    non-TTY (pipes/tests) returns None silently so scripted use keeps the default.
    Ctrl-C, an idle timeout, or stdin EOF all resolve to the default (None) rather
    than aborting, so stopping the timer always logs the event.
    """
    if termios is None or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    print("Event color (↑/↓ to move, Enter to choose, Esc to keep default):")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def read_key():
        # Bounded wait so an unattended picker can't block the session forever;
        # None signals the idle timeout to _color_loop (which keeps the default).
        ready, _, _ = select.select([fd], [], [], _COLOR_IDLE_TIMEOUT)
        return os.read(fd, 6) if ready else None

    try:
        tty.setcbreak(fd)
        return _color_loop(read_key=read_key, render=_render_colors)
    except KeyboardInterrupt:
        # The work is already tracked; Ctrl-C here keeps the default and logs
        # rather than discarding a finished session with a traceback.
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


@contextlib.contextmanager
def spinner(message: str):
    """Show ``message`` with animated dots on stderr while the wrapped block runs.

    Gives visible feedback during a blocking network call so a slow or stuck
    request doesn't look like the CLI has frozen. No-ops when stderr isn't a
    TTY (e.g. piped/--json output) so non-interactive consumers stay clean.
    Runs on a daemon thread so a KeyboardInterrupt in the wrapped block still
    propagates normally; the spinner line is cleared before re-raising.
    """
    if not sys.stderr.isatty():
        yield
        return

    stop = threading.Event()

    def _spin():
        for dots in itertools.cycle(["", ".", "..", "..."]):
            if stop.is_set():
                break
            sys.stderr.write(f"\r{message}{dots}   ")
            sys.stderr.flush()
            stop.wait(0.3)

    thread = threading.Thread(target=_spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
        sys.stderr.write("\r\x1b[K")  # clear the spinner line
        sys.stderr.flush()


def run_timer(start: datetime) -> datetime:
    """Show a live HH:MM:SS timer until Return is pressed; return the end time.

    Polls stdin once per second so the displayed elapsed time keeps ticking
    while waiting for the user to press Return. Propagates KeyboardInterrupt
    (Ctrl-C) so the caller can abort without logging an event.
    """
    while True:
        elapsed = format_elapsed(datetime.now().astimezone() - start)
        sys.stdout.write(f"\rElapsed: {elapsed}   (press Return to stop)")
        sys.stdout.flush()
        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        if ready:
            sys.stdin.readline()
            break
    sys.stdout.write("\n")
    sys.stdout.flush()
    return datetime.now().astimezone()


def fetch_busy(
    account: str, calendars: list[str], start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Return busy intervals for `calendars` in [start, end) via `gog calendar freebusy`.

    Raises RuntimeError on a gog failure (bad calendar name, network, timeout) or when the
    API reports a per-calendar error (e.g. a stale/unsubscribed id) — treating an unreadable
    calendar as zero busy time would silently inflate the computed free capacity, which is the
    one failure mode this command must never make quietly. A calendar with no events at all is
    valid and returns no `busy` key; that's treated as zero intervals, not an error.
    """
    cmd = ["gog", "calendar", "freebusy", "-a", account]
    for cal in calendars:
        cmd += ["--cal", cal]
    cmd += ["--from", start.isoformat(), "--to", end.isoformat(), "-j", "--no-input"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("gog command not found on PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError("gog timed out fetching free/busy")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gog freebusy failed")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse gog freebusy output: {exc}") from exc

    busy: list[tuple[datetime, datetime]] = []
    for cal_id, cal_data in data.get("calendars", {}).items():
        errors = cal_data.get("errors")
        if errors:
            reasons = ", ".join(e.get("reason", "unknown") for e in errors)
            raise RuntimeError(f"free/busy error for calendar '{cal_id}': {reasons}")
        for block in cal_data.get("busy", []):
            busy.append((datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])))
    return busy


def open_in_editor(text: str, suffix: str = ".txt") -> str:
    """Round-trip `text` through the user's $VISUAL/$EDITOR (falling back to `vi`).

    Writes to a temp file, runs the editor against it, and returns the saved content.
    `shlex.split` lets the editor command carry flags (e.g. `code --wait`).
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    try:
        subprocess.run(shlex.split(editor) + [path], check=True)
        with open(path, encoding="utf-8") as f:
            return f.read()
    finally:
        os.unlink(path)


def gog_auth_ok(account: str) -> bool:
    """Probe whether gog can reach the calendar API for ``account``.

    Runs a cheap authenticated read and treats a non-zero exit or an auth-error
    in the output as a failure (e.g. an expired refresh token).
    """
    try:
        result = subprocess.run(
            ["gog", "calendar", "calendars", "-a", account, "--no-input"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    combined = (result.stdout + result.stderr).lower()
    return "invalid_grant" not in combined and "invalid_token" not in combined


# Error-text fragments that identify a gog/Google failure we can hand the user a
# concrete next step for. Matched case-insensitively against the whole error
# text (gog wraps the Google error, so the fragment can appear anywhere in it).
_SCOPE_ERROR_MARKERS = (
    "scope_insufficient",
    "insufficient authentication scopes",
    "insufficientpermissions",
    "request had insufficient authentication",
)
_AUTH_ERROR_MARKERS = (
    "invalid_grant",
    "invalid_token",
    "token has been expired",
    "token expired",
    "expired or revoked",
    "unauthorized_client",
    "invalid credentials",
    "invalid authentication credentials",
    "no refresh token",
    "no stored token",
    "not authenticated",
    "unauthenticated",
    "401",
)
_NETWORK_ERROR_MARKERS = (
    "timed out",
    "i/o timeout",
    "no such host",
    "connection refused",
    "network is unreachable",
    "dial tcp",
    "eof",
)
_QUOTA_ERROR_MARKERS = (
    "ratelimitexceeded",
    "rate limit",
    "quota exceeded",
    "usagelimits",
    "429",
)
_MISSING_GOG_MARKERS = ("not found on path",)


# Wrapper script (bin/gog-reauth, symlinked onto PATH) that re-authorizes the
# account while preserving its existing service scopes and verifies the result.
REAUTH_COMMAND = "gog-reauth"


def reauth_help(account: str) -> str:
    """The standard 'your Google token is dead, here's how to fix it' block."""
    return (
        f"Google auth for {account} has expired or been revoked.\n"
        "Fix it with:\n"
        f"    {REAUTH_COMMAND} {account}\n"
        f"(wraps `gog auth add {account}`, keeps the account's existing scopes,\n"
        " and re-checks calendar access)"
    )


def gog_error_kind(message: str) -> str | None:
    """Classify a gog error as 'scope' | 'missing' | 'auth' | 'quota' | 'network'.

    Returns None when we can't confidently diagnose it — callers must stay quiet
    rather than suggest a fix (or worse, run one) for a failure it won't help.
    Only 'auth' and 'scope' are re-authenticatable.
    """
    text = (message or "").lower()
    # Scope is checked before auth: a scope failure often carries an auth-ish
    # code too, and a plain reauth re-mints the same insufficient scopes.
    if any(m in text for m in _SCOPE_ERROR_MARKERS):
        return "scope"
    if any(m in text for m in _MISSING_GOG_MARKERS):
        return "missing"
    if any(m in text for m in _AUTH_ERROR_MARKERS):
        return "auth"
    if any(m in text for m in _QUOTA_ERROR_MARKERS):
        return "quota"
    if any(m in text for m in _NETWORK_ERROR_MARKERS):
        return "network"
    return None


# Kinds a re-authentication can actually fix; anything else must not trigger one.
REAUTHABLE_KINDS = ("auth", "scope")


def gog_error_help(message: str, account: str) -> str | None:
    """Map a gog error string to actionable next steps, or None if unrecognized.

    Callers print this *underneath* the raw error so the original text is never
    swallowed.
    """
    kind = gog_error_kind(message)
    if kind == "scope":
        return (
            f"The stored token for {account} lacks the Calendar scope.\n"
            "Re-authorize with a fresh consent screen, then re-run this command:\n"
            f"    {REAUTH_COMMAND} {account}\n"
            "If the scope is still missing afterwards, request it explicitly:\n"
            f"    gog auth add {account} --services calendar --force-consent"
        )
    if kind == "missing":
        return (
            "The `gog` CLI isn't installed or isn't on this shell's PATH.\n"
            "Install it with:\n"
            "    brew install gog\n"
            "If it is already installed, check with:\n"
            "    which gog"
        )
    if kind == "auth":
        return reauth_help(account)
    if kind == "quota":
        return "Google is rate-limiting this account. Wait a minute and re-run the command."
    if kind == "network":
        return (
            "Couldn't reach Google — this looks like a network problem, not an auth one.\n"
            "Check your connection and re-run the command."
        )
    return None


def run_reauth(account: str) -> bool:
    """Run the `gog-reauth` wrapper, handing it the terminal. True on success.

    Deliberately does not capture output or impose a timeout: this opens a
    browser consent screen and the user has to watch it and act. Callers are
    responsible for only invoking it interactively and with the user's say-so.
    """
    try:
        result = subprocess.run([REAUTH_COMMAND, account])
    except FileNotFoundError:
        _print_reauth_missing()
        return False
    except KeyboardInterrupt:
        print("\nRe-authentication cancelled.", file=sys.stderr)
        return False
    return result.returncode == 0


# How long a detached reauth waits for the user to approve the consent screen.
DETACHED_REAUTH_TIMEOUT = 10 * 60


def run_reauth_detached(account: str, timeout: float = DETACHED_REAUTH_TIMEOUT) -> bool:
    """Run `gog-reauth` with no terminal (for the Todone helper). True on success.

    gog opens the browser consent screen itself. The wrapper's output goes to
    stderr because the helper's stdout carries its JSON protocol, and stdin is
    closed so nothing can block on a prompt. A consent screen left unanswered
    times out rather than leaving the helper waiting forever.
    """
    try:
        result = subprocess.run(
            [REAUTH_COMMAND, account],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=timeout,
        )
    except FileNotFoundError:
        _print_reauth_missing()
        return False
    except subprocess.TimeoutExpired:
        print("Re-authentication timed out waiting for the consent screen.", file=sys.stderr)
        return False
    return result.returncode == 0


def _print_reauth_missing() -> None:
    print(
        f"`{REAUTH_COMMAND}` isn't on PATH. Installing todoist-cli with pip or pipx\n"
        f"puts it there; from a source checkout, link bin/{REAUTH_COMMAND} into ~/.local/bin.",
        file=sys.stderr,
    )


def calendar_event_command(
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    *,
    account: str,
    calendar: str = GOG_CALENDAR,
    event_color: int | None = None,
) -> list[str]:
    """The argv `create_calendar_event` runs.

    Split out so a failed write can be shown back to the user as a command they
    can paste once they've fixed the underlying problem, without the CLI and the
    runner drifting apart.
    """
    cmd = [
        "gog", "calendar", "create", calendar,
        "-a", account,
        "--summary", summary,
        "--from", start.isoformat(),
        "--to", end.isoformat(),
        "--transparency", "free",
        "--no-input",
    ]
    if description:
        cmd += ["--description", description]
    if event_color is not None:
        cmd += ["--event-color", str(event_color)]
    return cmd


def create_calendar_event(
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    *,
    account: str,
    calendar: str = GOG_CALENDAR,
    event_color: int | None = None,
) -> tuple[bool, str]:
    """Create a 'Free' (transparent) calendar event for the tracked interval.

    Returns (success, message) where message is gog's stdout on success or the
    error text on failure. Times are emitted as RFC3339 (tz-aware isoformat).
    ``--no-input`` makes gog fail fast rather than block on a prompt, and the
    call is bounded by a timeout so a hung gog can never wedge the command.
    """
    cmd = calendar_event_command(
        summary, start, end,
        description=description,
        account=account,
        calendar=calendar,
        event_color=event_color,
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, "gog command not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "gog timed out creating the event"
    if result.returncode != 0:
        return False, (result.stderr.strip() or result.stdout.strip() or "unknown error")
    return True, result.stdout.strip()
