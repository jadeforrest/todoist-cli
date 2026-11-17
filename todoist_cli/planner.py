"""Pure scheduling logic for `todoist plan`.

No network calls, subprocess use, or terminal I/O — everything here is a function
of its arguments so it can be unit-tested without mocking. Calendar/API access
lives in tracker.py and todoist_client.py; terminal interaction and printing
live in todoist_cli.py.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday"]

_DEFAULT_WORKING_HOURS = {"default": {"start": "09:00", "end": "17:00"}}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PlanConfig:
    default_task_minutes: int
    horizon_days: int
    eligibility_days: int
    timezone: ZoneInfo
    account: str | None
    calendars: list[str]
    days_off: set[date]
    # weekday index (0=Monday .. 4=Friday) -> (start, end)
    working_hours: dict[int, tuple[time, time]]


def local_timezone(env=os.environ, localtime: str = "/etc/localtime") -> ZoneInfo:
    """The machine's time zone: $TZ, else the zone /etc/localtime links to, else UTC."""
    name = (env.get("TZ") or "").lstrip(":")
    if not name:
        target = os.path.realpath(localtime)
        marker = "zoneinfo/"
        name = target[target.rfind(marker) + len(marker):] if marker in target else "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_hm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def load_config(path: Path | str) -> PlanConfig:
    """Load plan.toml, falling back to built-in defaults for anything absent.

    A missing file is not an error — every field has a sensible default so
    `plan` works out of the box and toml just overrides what you want changed.
    """
    data: dict = {
        "default_task_minutes": 30,
        "horizon_days": 7,
        "eligibility_days": 14,
        "timezone": None,
        "account": None,
        "calendars": ["primary"],
        "days_off": [],
    }
    working_hours_raw = dict(_DEFAULT_WORKING_HOURS)

    p = Path(path)
    if p.exists():
        with p.open("rb") as f:
            loaded = tomllib.load(f)
        if "working_hours" in loaded:
            working_hours_raw = dict(working_hours_raw)
            working_hours_raw.update(loaded.pop("working_hours"))
        data.update(loaded)

    default_range = working_hours_raw.get("default", _DEFAULT_WORKING_HOURS["default"])
    working_hours: dict[int, tuple[time, time]] = {}
    for idx, name in enumerate(WEEKDAY_NAMES):
        entry = working_hours_raw.get(name, default_range)
        start, end = _parse_hm(entry["start"]), _parse_hm(entry["end"])
        if end <= start:
            raise ValueError(
                f"working_hours.{name}: end ({entry['end']}) must be after start ({entry['start']})"
            )
        working_hours[idx] = (start, end)

    return PlanConfig(
        default_task_minutes=int(data["default_task_minutes"]),
        horizon_days=int(data["horizon_days"]),
        eligibility_days=int(data["eligibility_days"]),
        timezone=ZoneInfo(data["timezone"]) if data["timezone"] else local_timezone(),
        account=data["account"],
        calendars=list(data["calendars"]),
        days_off={date.fromisoformat(d) for d in data["days_off"]},
        working_hours=working_hours,
    )


# ---------------------------------------------------------------------------
# Task size ("30m", "[3h]", "1h30m", "1.5h" hints in the title; native duration wins)
# ---------------------------------------------------------------------------

# A number+unit not glued to a trailing letter (blocks "v2house"), found
# anywhere; whether it's glued to a *leading* letter (blocks "Q3h1") is
# checked separately below, since digits may legitimately glue two tokens
# together ("1h30m"). No internal space, per spec ("a number without a space
# + h").
_RAW_SIZE_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([hm])(?![A-Za-z])", re.IGNORECASE)


def title_size_minutes(content: str) -> int | None:
    """Minutes from the last contiguous run of size tokens in the title, or None.

    "1h30m" is one contiguous run (90m) — the second token is accepted even
    though it's glued to the first, specifically because it's glued to a
    *previously accepted* token rather than to arbitrary word characters.
    "5m walk, 2h drive" has two separate tokens with a gap between them; only
    the last ("2h") is used, so an incidental number earlier in the title
    can't corrupt the estimate. "Q3h1" matches nothing: "3h" is glued to the
    leading "Q" and isn't preceded by an accepted token, so it's dropped.
    Decimals are read whole ("1.5h" is 90m), rounded to the nearest minute.
    """
    run = _last_size_run(content)
    if run is None:
        return None
    return round(sum(num * 60 if unit == "h" else num for _, _, num, unit in run))


def _last_size_run(content: str) -> list[tuple[int, int, float, str]] | None:
    """The last contiguous run of accepted size tokens, as (start, end, number, unit)."""
    tokens: list[tuple[int, int, float, str]] = []
    for m in _RAW_SIZE_TOKEN_RE.finditer(content):
        start, end = m.start(), m.end()
        glued_to_prior_token = bool(tokens) and tokens[-1][1] == start
        prev_char = content[start - 1] if start > 0 else ""
        at_word_boundary = start == 0 or not (prev_char.isalnum() or prev_char == ".")
        if glued_to_prior_token or at_word_boundary:
            tokens.append((start, end, float(m.group(1)), m.group(2).lower()))

    if not tokens:
        return None

    runs: list[list[tuple[int, int, float, str]]] = [[tokens[0]]]
    for prev, tok in zip(tokens, tokens[1:]):
        if tok[0] == prev[1]:
            runs[-1].append(tok)
        else:
            runs.append([tok])
    return runs[-1]


def with_size_hint(content: str, minutes: int) -> str:
    """`content` with its size hint set to `minutes`: the last run of size tokens
    is replaced, or " [1h30m]" is appended when the title has none. The result
    always reads back as `minutes` through title_size_minutes."""
    hint = format_minutes(minutes)
    run = _last_size_run(content)
    if run is None:
        return f"{content.rstrip()} [{hint}]".lstrip()
    return content[: run[0][0]] + hint + content[run[-1][1] :]


def explicit_size(task: dict) -> int | None:
    """Task size in minutes from the native `duration` field, else a title hint, else None."""
    duration = task.get("duration")
    if duration and duration.get("amount"):
        amount = int(duration["amount"])
        return amount * 24 * 60 if duration.get("unit") == "day" else amount

    return title_size_minutes(task.get("content", ""))


def parse_size(task: dict, default_minutes: int) -> int:
    """Task size in minutes: an explicit estimate (see `explicit_size`), else the default."""
    minutes = explicit_size(task)
    return default_minutes if minutes is None else minutes


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


FIXED_LABEL = "fixed"

# Rules whose occurrences are anchored to the calendar (a weekday, a day of the
# month, a month name), so a moved occurrence snaps back to the pattern once
# completed. See docs/recurring-tasks.md for the observed API behavior.
# Names are spelled out rather than prefix-matched: "mon" must not match "month".
_ANCHORED_RULE_RE = re.compile(
    r"\b(mon|monday|tue|tues|tuesday|wed|weds|wednesday|thu|thur|thurs|thursday"
    r"|fri|friday|sat|saturday|sun|sunday|weekdays?|workdays?|weekends?"
    r"|jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august"
    r"|sep|sept|september|oct|october|nov|november|dec|december)\b"
    r"|\b\d+(st|nd|rd|th)\b|\blast day\b",
    re.IGNORECASE,
)
# An explicit interval ("every 3 days", "every other mon") counts from the
# current due date, so moving it would shift every later occurrence.
_INTERVAL_RULE_RE = re.compile(r"\bevery (\d+ |other )", re.IGNORECASE)
_DAILY_RULE_RE = re.compile(r"^\s*(every day|daily)\s*$", re.IGNORECASE)


def recurrence_writable(rule: str | None) -> bool:
    """Whether one occurrence of `rule` can be moved in Todoist without shifting
    the occurrences after it.

    `every!` counts from completion, a daily rule has no cadence to shift, and an
    anchored rule snaps back to its pattern. Any other `every` rule counts from
    the current due date, so moving it would drift the whole series — and an
    unrecognised rule is treated the same way, since leaving the date alone is
    the move that can't do damage.
    """
    if not rule:
        return False
    text = rule.strip().lower()
    if text.startswith("every!") or _DAILY_RULE_RE.match(text):
        return True
    if _INTERVAL_RULE_RE.search(text):
        return False
    return bool(_ANCHORED_RULE_RE.search(text))


@dataclass
class Item:
    id: str
    content: str
    priority: int  # user-facing: 1=highest .. 4=lowest
    due_date: date | None
    deadline: date | None
    minutes: int
    pinned: bool  # time-of-day due or @fixed: consumes capacity on its day, never moved or rewritten
    # The recurrence rule ("every mon") of a recurring task, "" if Todoist gave
    # none; None for a one-off task.
    recurrence: str | None = None

    @property
    def soft_recurring(self) -> bool:
        """A recurring task the plan may move (later only), because it isn't pinned."""
        return self.recurrence is not None and not self.pinned

    @property
    def date_writable(self) -> bool:
        """Whether a new date from the plan may be written back to Todoist."""
        if self.pinned:
            return False
        return self.recurrence is None or recurrence_writable(self.recurrence)

    @property
    def marker(self) -> str:
        """Suffix naming how the plan treats this task's date."""
        if self.pinned:
            return " (pinned)"
        if self.recurrence is None:
            return ""
        return " (recurring)" if self.date_writable else " (recurring, plan only)"

    @property
    def effective_priority(self) -> int:
        """Priority used for sorting: a deadline promotes urgency by two levels, a
        due date by one, clamped at 1. A task carrying both is promoted by two, not
        three — the deadline is already the stronger of the two signals, so a p3 with
        a deadline, a p2 with a due date, and a bare p1 all rank the same.
        """
        if self.deadline is not None:
            return max(1, self.priority - 2)
        if self.due_date is not None:
            return max(1, self.priority - 1)
        return self.priority

    def sort_key(self) -> tuple:
        # A movable recurring task gets half a point on its own priority: it
        # outranks a one-off task of the same priority but never a higher one.
        return (
            self.effective_priority,
            self.priority - (0.5 if self.soft_recurring else 0),
            self.deadline or date.max,
            self.due_date or date.max,
            -self.minutes,
            self.content.lower(),
        )


def _task_date(field_value: dict | None) -> date | None:
    if not field_value or not field_value.get("date"):
        return None
    return date.fromisoformat(field_value["date"][:10])


def _in_window(
    t: dict, due_date: date | None, deadline: date | None, inbox_project_id: str | None, horizon_end: date
) -> bool:
    """Has a date (or lives in the Inbox), and not every date it carries is past `horizon_end`.

    The far-future upper bound only excludes a task when EVERY date it
    carries is beyond the horizon — a near deadline must still pull in
    a task whose due date alone is far out (e.g. a placeholder date).
    """
    eligible = bool(t.get("due") or t.get("deadline")) or t.get("project_id") == inbox_project_id
    if not eligible:
        return False
    relevant_dates = [d for d in (due_date, deadline) if d is not None]
    return not (relevant_dates and all(d > horizon_end for d in relevant_dates))


def _item_minutes(t: dict, is_container: bool, default_minutes: int) -> int:
    """A container is 0 unless it carries an explicit estimate; anything else uses parse_size."""
    if is_container:
        return explicit_size(t) or 0
    return parse_size(t, default_minutes)


def _pinning(t: dict) -> tuple[bool, str | None]:
    """Whether a task is pinned, and its recurrence rule (None if one-off).

    A time of day can't survive a date-only write, so it always pins; @fixed
    pins anything with a due date to keep it on.
    """
    due = t.get("due")
    if not due:
        return False, None
    fixed = any(label.lower() == FIXED_LABEL for label in t.get("labels") or [])
    pinned = bool(due.get("datetime")) or fixed
    recurrence = (due.get("string") or "") if due.get("is_recurring") else None
    return pinned, recurrence


def build_items(
    tasks: list[dict], inbox_project_id: str | None, today: date, config: PlanConfig
) -> list[Item]:
    """Filter the open-task list down to the eligible, schedulable pool.

    Eligible: has a due date or deadline, OR lives in the Inbox project. Then
    dropped if its only relevant date is further out than `eligibility_days`
    (an upper bound only — overdue items have no lower bound and stay in).
    A task with any open child is a container: it is kept (so it can still be
    displayed/repinned) but sized at 0 minutes, since its children carry the
    actual work — unless it carries an explicit estimate (native duration or a
    title hint), which is honored as-is.
    """
    container_ids = {t["parent_id"] for t in tasks if t.get("parent_id")}
    horizon_end = today + timedelta(days=config.eligibility_days)

    items: list[Item] = []
    for t in tasks:
        due = t.get("due")
        due_date = _task_date(due)
        deadline = _task_date(t.get("deadline"))
        if not _in_window(t, due_date, deadline, inbox_project_id, horizon_end):
            continue

        minutes = _item_minutes(t, t["id"] in container_ids, config.default_task_minutes)
        pinned, recurrence = _pinning(t)

        items.append(
            Item(
                id=t["id"],
                content=t.get("content", ""),
                priority=5 - t.get("priority", 1),
                due_date=due_date,
                deadline=deadline,
                minutes=minutes,
                pinned=pinned,
                recurrence=recurrence,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Calendar capacity
# ---------------------------------------------------------------------------


def weekday_buckets(today: date, horizon_days: int) -> list[date]:
    """Weekdays in [today, today + horizon_days], inclusive. Skips Sat/Sun."""
    return [
        d
        for d in (today + timedelta(days=n) for n in range(horizon_days + 1))
        if d.weekday() < 5
    ]


def free_minutes(
    day: date,
    work_start: time,
    work_end: time,
    tz: ZoneInfo,
    busy: list[tuple[datetime, datetime]],
    now: datetime,
) -> int:
    """Unblocked minutes in the working window on `day`, clamped to not start before `now`."""
    window_start = datetime.combine(day, work_start, tz)
    window_end = datetime.combine(day, work_end, tz)
    if now > window_start:
        window_start = min(now, window_end)
    if window_start >= window_end:
        return 0

    clamped = sorted(
        (max(s, window_start), min(e, window_end)) for s, e in busy if e > window_start and s < window_end
    )
    merged: list[tuple[datetime, datetime]] = []
    for s, e in clamped:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    total = (window_end - window_start).total_seconds() // 60
    busy_minutes = sum((e - s).total_seconds() // 60 for s, e in merged)
    return max(0, int(total - busy_minutes))


def compute_capacity(
    buckets: list[date],
    busy: list[tuple[datetime, datetime]],
    config: PlanConfig,
    now: datetime,
) -> dict[date, int]:
    capacity: dict[date, int] = {}
    for day in buckets:
        if day in config.days_off:
            capacity[day] = 0
            continue
        start, end = config.working_hours[day.weekday()]
        capacity[day] = free_minutes(day, start, end, config.timezone, busy, now)
    return capacity


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------


@dataclass
class ScheduledChunk:
    item: Item
    day: date
    minutes: int


@dataclass
class PlanResult:
    schedule: dict[date, list[ScheduledChunk]]
    at_risk: list[Item] = field(default_factory=list)
    unscheduled: list[Item] = field(default_factory=list)
    # Pinned work sitting on a day this plan doesn't cover — a weekend, or a
    # day past the horizon. Distinct from `unscheduled`, which is work that
    # didn't fit: these were never candidates for a slot, since a pinned item
    # is only ever placed on its own day and that day isn't in the plan.
    unplanned: list[Item] = field(default_factory=list)


def _anchor_index(buckets: list[date], target: date, *, prefer: str) -> int:
    """Index of the bucket an item anchored on `target` should start from.

    Buckets are weekdays only, so a target can land on a day that isn't in the
    list at all; `prefer` says which way to snap. "earlier" takes the latest
    bucket at or before the target (a Saturday deadline snaps back to Friday —
    the safe direction when a date is a ceiling); "later" takes the earliest
    bucket at or after it (a Sunday due date snaps forward to Monday). A target
    before the horizon clamps to the first bucket, so overdue work starts today.
    """
    if target <= buckets[0]:
        return 0
    if target >= buckets[-1]:
        return len(buckets) - 1
    if prefer == "earlier":
        return max(i for i, d in enumerate(buckets) if d <= target)
    return min(i for i, d in enumerate(buckets) if d >= target)


def _day_order(
    buckets: list[date], anchor: int, *, spill: str, slack_end: date | None = None
) -> list[date]:
    """Days to try, in order: the anchor first, then outward in the preferred
    spill direction, then the remaining days in the other direction.

    Deadline work spills "earlier" (anchor, back toward today, and only then
    forward into the days past the deadline). Due-date work spills "later"
    (anchor, forward, and only then back toward today).

    `slack_end` refines spilling "earlier" only: it is the last day the item may
    use before falling back toward today. It is set for an item carrying both a
    due date and a deadline, which named the day it wants to be worked on *and*
    the day it must be finished — the days between the two are slack it granted
    itself, so they are tried right after the anchor rather than crowding the
    days back toward today. That anchor is a preference, not merely a ceiling.
    It is the day *before* the deadline, so the deadline day itself falls in with
    the late days and is spent only once everything earlier is full: it is the
    last day that isn't actually late, and is held in reserve as such.
    """
    if spill == "earlier":
        backward = list(reversed(buckets[:anchor]))
        after = buckets[anchor + 1 :]
        if slack_end is None:
            return [buckets[anchor]] + backward + after
        slack = [d for d in after if d <= slack_end]
        late = [d for d in after if d > slack_end]
        return [buckets[anchor]] + slack + backward + late
    return buckets[anchor:] + list(reversed(buckets[:anchor]))


def _consume(
    item: Item,
    order: list[date],
    remaining: dict[date, int],
    schedule: dict[date, list[ScheduledChunk]],
) -> tuple[int, date | None]:
    """Take capacity for `item` from the days in `order`, splitting by the minute.

    Returns the minutes still unplaced (0 if it fully fit) and the latest day
    touched — the latest, not the last visited, because bidirectional spilling
    means the days are not consumed in chronological order, and it's the latest
    day that decides whether a deadline was missed.
    """
    need = item.minutes
    latest: date | None = None
    for day in order:
        if need <= 0:
            break
        avail = remaining[day]
        if avail <= 0:
            continue
        take = min(avail, need)
        schedule[day].append(ScheduledChunk(item, day, take))
        remaining[day] -= take
        need -= take
        if latest is None or day > latest:
            latest = day
    return need, latest


def _place_pinned(
    pinned: list[Item],
    buckets: list[date],
    remaining: dict[date, int],
    schedule: dict[date, list[ScheduledChunk]],
) -> list[Item]:
    """Put each pinned item on its own day, and return the ones whose day isn't
    one of the planned days."""
    unplanned: list[Item] = []
    for item in pinned:
        # An overdue pinned item is still outstanding work, and today is the
        # earliest it can be done, so it lands on the first bucket rather than
        # falling off the plan with its day. Its due date is still never
        # written back (see park_overdue_dates) — this only makes it visible
        # and lets it consume the capacity it is going to consume anyway.
        day = item.due_date
        if day is not None and buckets and day < buckets[0]:
            day = buckets[0]
        if day in remaining:
            remaining[day] = max(0, remaining[day] - item.minutes)
            schedule[day].append(ScheduledChunk(item, day, item.minutes))
        else:
            # Its day is a weekend, or past the horizon. The plan can't place
            # it (a pinned item only ever sits on its own day) and won't move
            # it, but it is reported rather than dropped: silently missing
            # work is indistinguishable from work that doesn't exist.
            unplanned.append(item)
    return unplanned


def _off_plan(item: Item, buckets: list[date]) -> bool:
    """A movable recurring task whose day isn't a planned day — a weekend, or past
    the horizon. It is reported like a pinned one rather than placed: pulling
    next week's occurrence into this week, or a Saturday chore onto a weekday,
    would do it on a day it was never meant for. An overdue one is on the plan.
    """
    if not item.soft_recurring:
        return False
    due = item.due_date
    return not buckets or due is None or (due >= buckets[0] and due not in buckets)


def _partition(
    items: list[Item], buckets: list[date]
) -> tuple[list[Item], list[Item], list[Item]]:
    """Split items into pinned, movable, and movable-but-off-plan (see _off_plan)."""
    pinned: list[Item] = []
    movable: list[Item] = []
    off_plan: list[Item] = []
    for item in items:
        if item.pinned:
            pinned.append(item)
        else:
            (off_plan if _off_plan(item, buckets) else movable).append(item)
    return pinned, movable, off_plan


def _dated_within_horizon(item: Item, horizon_end: date) -> bool:
    """Carries a date the plan can miss — the trigger for calling it at risk.
    An item dated entirely past the horizon isn't in trouble; there are days
    after this plan in which to do it."""
    return (item.deadline is not None and item.deadline <= horizon_end) or (
        item.due_date is not None and item.due_date <= horizon_end
    )


def _passes(movable: list[Item], horizon_end: date) -> tuple[list[Item], list[Item], list[Item]]:
    """Split movable items into the deadline, due-date and undated passes, each
    sorted by priority. A deadline counts as urgent only if this plan is
    actually responsible for meeting it."""

    def urgent_deadline(i: Item) -> bool:
        return i.deadline is not None and i.deadline <= horizon_end

    with_deadline = [i for i in movable if urgent_deadline(i)]
    with_due = [
        i
        for i in movable
        if not urgent_deadline(i) and (i.due_date is not None or i.deadline is not None)
    ]
    undated = [i for i in movable if i.deadline is None and i.due_date is None]
    return tuple(sorted(p, key=lambda i: i.sort_key()) for p in (with_deadline, with_due, undated))


def _item_day_order(item: Item, buckets: list[date], spill: str) -> list[date]:
    """The days to try for `item`, in order."""
    if item.soft_recurring:
        # Later only, from its own day (today if overdue). Moving an anchored
        # `every` occurrence earlier would add an extra occurrence, and its due
        # day is always a bucket or overdue (see _off_plan).
        return buckets[_anchor_index(buckets, item.due_date, prefer="later") :]
    slack_end: date | None = None
    if item.deadline is not None:
        target = item.deadline - timedelta(days=1)
        if item.due_date is not None:
            slack_end = target  # the deadline day itself stays in reserve
            target = min(item.due_date, target)
    elif item.due_date is not None:
        target = item.due_date
    else:
        target = buckets[0]
    return _day_order(
        buckets, _anchor_index(buckets, target, prefer=spill), spill=spill, slack_end=slack_end
    )


def _place(
    item: Item,
    order: list[date],
    remaining: dict[date, int],
    schedule: dict[date, list[ScheduledChunk]],
    horizon_end: date,
) -> str | None:
    """Schedule `item` along `order`. Returns "at_risk", "unscheduled", or None
    when it fit in time."""
    if item.minutes == 0:
        # Container: rides along on its anchor day without consuming capacity.
        day = order[0]
        schedule[day].append(ScheduledChunk(item, day, 0))
        return "at_risk" if item.deadline is not None and day > item.deadline else None

    need, latest = _consume(item, order, remaining, schedule)
    if need > 0:
        return "at_risk" if _dated_within_horizon(item, horizon_end) else "unscheduled"
    if item.deadline is not None and latest is not None and latest > item.deadline:
        return "at_risk"
    return None


def pack(items: list[Item], capacity: dict[date, int]) -> PlanResult:
    """Fill days with items in three date-aware passes, splitting tasks across
    day boundaries by the minute. Pinned items consume capacity on their
    existing due date first and are never moved (an overdue one consumes it
    today, the earliest day it can still be done).

    The passes claim capacity in order, so a deadline item beats a due-dated one
    for a contested day even if the due-dated one has the higher priority:

    1. Deadlines falling inside the horizon. Anchored at the due date if there
       is one, else the day before the deadline — a deadline usually means "done
       by the day before" — and spilling into EARLIER days, since the point is
       to land in time. An item with both dates is the exception: it said when it
       wants to be worked on as well as when it is due, so it first spills
       FORWARD into the slack between the two (stopping short of the deadline
       day) and only crowds the days back toward today once that is full.
    2. Due dates, plus deadlines that fall *past* the horizon. A due date is a
       rough intention rather than a commitment, so it spills into LATER days
       first and only falls back to earlier ones once everything later is full.
       A deadline a fortnight out belongs here too: there are days after this
       plan in which to do it, so it shouldn't outrank work due tomorrow.
    3. Everything else (undated Inbox work), filling whatever gaps are left.

    A recurring task that isn't pinned competes in these passes like any other,
    with half a point of priority (see Item.sort_key), but only ever moves later
    from its own day; one whose day isn't planned is reported, not placed.

    A task is "at risk" if it carries a date inside the horizon and doesn't
    fully fit, or if it has a deadline and its last chunk lands after it.
    Everything else that doesn't fit — undated work, and work whose dates all
    fall past the horizon, which this plan was never going to reach — is left
    unscheduled (reported as a summary, not called out per-task). Pinned work
    whose day isn't one of the planned days is "unplanned": a third outcome,
    because it never competed for a slot at all.
    """
    buckets = sorted(capacity.keys())
    remaining = dict(capacity)
    schedule: dict[date, list[ScheduledChunk]] = {d: [] for d in buckets}

    pinned, movable, off_plan = _partition(items, buckets)
    unplanned = _place_pinned(pinned, buckets, remaining, schedule) + off_plan
    result = PlanResult(schedule=schedule, unplanned=unplanned)
    outcomes = {"at_risk": result.at_risk, "unscheduled": result.unscheduled}

    if not buckets:
        # No horizon at all, so nothing can be this week's problem: everything
        # is simply unscheduled.
        result.unscheduled = movable
        return result

    horizon_end = buckets[-1]
    for pass_items, spill in zip(_passes(movable, horizon_end), ("earlier", "later", "later")):
        for item in pass_items:
            order = _item_day_order(item, buckets, spill)
            outcome = _place(item, order, remaining, schedule, horizon_end)
            if outcome is not None:
                outcomes[outcome].append(item)

    # Chunks land out of order once items spill backward, so sort each day for
    # display; both renderers print in list order.
    for day in buckets:
        schedule[day].sort(key=lambda c: c.item.sort_key())

    return result


def unplanned_reason(item: Item, buckets: list[date]) -> str:
    """Why a pinned item's day isn't one of the planned days.

    Worth naming per-item rather than lumping them under one heading: "it's on
    a Saturday" and "it's next week" prompt completely different reactions, and
    without the reason the entry just looks like the plan lost the task.
    """
    if not buckets:
        return "no days in the horizon"
    if item.due_date is None:
        return "no due date"
    if item.due_date > buckets[-1]:
        return "past the horizon"
    return "not a working day"


def earliest_scheduled_dates(result: PlanResult, buckets: list[date]) -> dict[str, date]:
    """First day each item has any chunk on — the date written back to Todoist."""
    earliest: dict[str, date] = {}
    for day in buckets:
        for chunk in result.schedule[day]:
            earliest.setdefault(chunk.item.id, day)
    return earliest


def park_overdue_dates(
    final_dates: dict[str, date], items: list[Item], today: date, parking_day: date
) -> dict[str, date]:
    """Move any date still landing before `today` onto `parking_day`.

    A due date says when you intend to work on something, so yesterday is not a
    plan — unlike a deadline, which is a commitment and stays in the past once
    missed (and which this command never writes at all). Anything the packing
    passes placed already holds a bucket date, which is today or later by
    construction, so this covers the two ways an overdue date survives a run:
    an item that got no slot because every bucket was full, and an explicit past
    date typed into the editor. Both are parked on `parking_day` — the last day
    of the horizon — since neither is work the plan found room for; parking them
    on today would only crowd the day that was already full.

    Pinned items are skipped: their due dates are never written back, because
    the only field available is `due_date`, which would drop a time of day.
    Recurring items are skipped too: parking one a week out would skip its
    occurrences in between, which is a decision, not a correction. An item with
    no due date at all is skipped as well — there is nothing in the past to
    correct.
    """
    parked = dict(final_dates)
    for item in items:
        if item.pinned or item.recurrence is not None or item.due_date is None or item.due_date >= today:
            continue
        current = parked.get(item.id)
        if current is None or current < today:
            parked[item.id] = parking_day
    return parked


# ---------------------------------------------------------------------------
# Capacity-table edits ("07-28=1h30m 07-31=0")
# ---------------------------------------------------------------------------


def format_minutes(minutes: int) -> str:
    if minutes <= 0:
        return "0m"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h{mins:02d}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?$", re.IGNORECASE)


def parse_minutes(text: str) -> int:
    text = text.strip()
    if text == "0":
        return 0
    m = _DURATION_RE.fullmatch(text)
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"can't parse duration: {text!r}")
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def parse_capacity_edits(raw: str, capacity: dict[date, int]) -> dict[date, int]:
    """Apply "MM-DD=DURATION" edits, e.g. "07-28=1h30m 07-31=0". Keyed by date,
    not weekday name, since the horizon can span the same weekday twice.
    """
    updated = dict(capacity)
    by_month_day: dict[tuple[int, int], list[date]] = {}
    for d in capacity:
        by_month_day.setdefault((d.month, d.day), []).append(d)

    for token in raw.split():
        if "=" not in token:
            raise ValueError(f"bad edit {token!r}, expected MM-DD=DURATION")
        key, value = token.split("=", 1)
        try:
            month, day_num = (int(x) for x in key.split("-"))
        except ValueError:
            raise ValueError(f"bad date {key!r}, expected MM-DD")
        matches = by_month_day.get((month, day_num))
        if not matches:
            raise ValueError(f"{key} is not one of the planned days")
        if len(matches) > 1:
            raise ValueError(f"{key} is ambiguous across the horizon")
        updated[matches[0]] = parse_minutes(value)
    return updated


# ---------------------------------------------------------------------------
# $EDITOR round-trip
# ---------------------------------------------------------------------------


def render_editor_text(result: PlanResult, buckets: list[date], capacity: dict[date, int]) -> str:
    """Plain-text, tab-separated plan for $EDITOR. Only columns 1 (id) and 2
    (date) are parsed back; the rest is for the human. A multi-day task is
    listed once, on its earliest scheduled day, per spec.
    """
    earliest = earliest_scheduled_dates(result, buckets)
    lines = [
        "# todoist plan -- the DATE column is authoritative. Save and quit to apply.",
        "# Set a date to `-`, or delete the line, to leave that task's date unchanged.",
        "# An overdue task is moved to the last day of the horizon either way: a due",
        "# date says when you plan to work on something, so it is never left in the past.",
        "",
    ]

    for day in buckets:
        lines.append(f"# {day.strftime('%a %b %d')} -- {format_minutes(capacity[day])} free")
        seen: set[str] = set()
        for chunk in result.schedule[day]:
            item = chunk.item
            if earliest.get(item.id) != day or item.id in seen:
                continue
            seen.add(item.id)
            lines.append(
                f"{item.id}\t{day.isoformat()}\tP{item.priority}\t"
                f"{format_minutes(item.minutes)}\t{item.content}{item.marker}"
            )
        lines.append("")

    lines.extend(_render_at_risk(result.at_risk, earliest))

    # Comments only, never editable rows: these dates are never written back
    # (a pinned task's never is, and a recurring one isn't moved off its own
    # day), so offering a DATE column here would be a control that does nothing.
    if result.unplanned:
        lines.append("# Not planned (due on a day this plan doesn't cover) -- shown so they")
        lines.append("# aren't invisible; their dates are never rewritten:")
        for item in sorted(result.unplanned, key=lambda i: i.sort_key()):
            when = item.due_date.isoformat() if item.due_date else "no date"
            lines.append(
                f"#   P{item.priority}\t{format_minutes(item.minutes)}\t{item.content} "
                f"({when} -- {unplanned_reason(item, buckets)})"
            )
        lines.append("")

    return "\n".join(lines)


def _render_at_risk(at_risk: list[Item], earliest: dict[str, date]) -> list[str]:
    """Editor lines for the at-risk section, or none if nothing is at risk.

    Every at-risk item is listed, but only the ones that never got a slot get
    an editable row. An item that *was* scheduled already has an authoritative
    row above, under the day it's on; repeating its id here would give
    parse_editor_text the same task twice with two different dates, so it is
    shown as a comment instead — visible, but not a second source of truth.
    """
    if not at_risk:
        return []
    lines = [
        "# At risk (won't fit in time) -- dates left as `-` are not rescheduled,",
        "# except overdue ones, which move to the last day of the horizon:",
    ]
    for item in sorted(at_risk, key=lambda i: i.sort_key()):
        # Name the date that's actually in trouble. An at-risk item without a
        # deadline has a due date instead; printing "(deadline -)" for it read
        # like the deadline had been cleared.
        if item.deadline is not None:
            when = f"deadline {item.deadline.isoformat()}"
        elif item.due_date is not None:
            when = f"due {item.due_date.isoformat()}"
        else:
            when = "no date"
        scheduled = earliest.get(item.id)
        if scheduled is not None:
            lines.append(
                f"#   P{item.priority}\t{format_minutes(item.minutes)}\t{item.content} "
                f"({when}) -- scheduled {scheduled.isoformat()}, edit above"
            )
        else:
            lines.append(
                f"{item.id}\t-\tP{item.priority}\t{format_minutes(item.minutes)}\t"
                f"{item.content} ({when})"
            )
    lines.append("")
    return lines


def parse_editor_text(text: str) -> tuple[dict[str, date | None], list[str]]:
    """Parse the edited text back into {task_id: new_date_or_None}, plus warnings.

    None means "explicitly cleared" (`-`); a task id absent from the result was
    either deleted or untouched, and the caller should leave it alone either way.
    A duplicate id is last-line-wins (with a warning); an unparseable date warns
    and is skipped rather than aborting the whole edit.
    """
    edits: dict[str, date | None] = {}
    warnings: list[str] = []
    seen: set[str] = set()

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        task_id, date_str = parts[0].strip(), parts[1].strip()
        if not task_id:
            continue
        if task_id in seen:
            warnings.append(f"line {lineno}: duplicate task {task_id}, using this line's value")
        seen.add(task_id)

        if date_str == "-":
            edits[task_id] = None
            continue
        try:
            edits[task_id] = date.fromisoformat(date_str)
        except ValueError:
            warnings.append(f"line {lineno}: can't parse date {date_str!r} for task {task_id}, skipping")

    return edits, warnings
