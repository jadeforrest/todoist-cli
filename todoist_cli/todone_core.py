"""State and actions behind the Todone Mac app.

The app's helper process (todone_server) holds one `Todone`, and every
request the app sends is a method call on it. Everything that decides what a
task's size is, how much time a day has, what the plan proposes and what is at
risk comes from planner.py, so the app and `todoist plan` always agree. Writes
go through the same TodoistClient calls the CLI makes, and a confirmed plan is
logged and applied with the CLI's own functions.

All I/O comes in through `Services`, so tests (and `--fake` mode) swap in
in-memory versions.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from . import cli, planner, tracker

BUSY_CACHE_SECONDS = 15 * 60
NO_DATE = "no date"


@dataclass
class Services:
    client: Any
    config: planner.PlanConfig
    # (window_start, window_end) -> busy intervals; raises RuntimeError on failure.
    fetch_busy: Callable[[datetime, datetime], list[tuple[datetime, datetime]]]
    # (title, start, end, description, color_id) -> (ok, message)
    write_event: Callable[..., tuple[bool, str]]
    now: Callable[[], datetime]
    write_plan_log: Callable[..., Any] = cli._write_plan_log
    apply_plan: Callable[..., tuple] = cli._apply_plan
    # gog error message -> what to do about it, or None when unrecognized.
    calendar_help: Callable[[str], str | None] = lambda message: None


@dataclass
class Timer:
    task_id: str
    title: str
    started_at: datetime
    # The calendar event's description: the task's URL, or what the user typed
    # for a log timer (`todoist log`), which has no task behind it.
    description: str | None = None
    is_log: bool = False
    # Set by the first stop attempt, so retrying a failed calendar write logs
    # the session the user actually stopped rather than one that kept growing.
    stopped_at: datetime | None = None


@dataclass
class Draft:
    """A proposed plan being reviewed: the dates it would write and what it calls at risk."""

    final_dates: dict[str, date]
    original_dates: dict[str, date | None]
    at_risk_ids: set[str]
    # The planner's own split of each scheduled task across days, and the dates
    # it proposed. A task still on its proposed date shows exactly that split;
    # one dragged elsewhere is spread from its new day instead.
    chunks: dict[str, dict[date, int]] = field(default_factory=dict)
    proposed_dates: dict[str, date] = field(default_factory=dict)


@dataclass
class _Busy:
    fetched_at: datetime
    window: tuple[date, date]
    intervals: list[tuple[datetime, datetime]]


class TodoneError(Exception):
    """A request the app made that can't be carried out; the message is shown."""


@dataclass
class Todone:
    services: Services
    tasks: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    capacity_overrides: dict[date, int] = field(default_factory=dict)
    timers: dict[str, Timer] = field(default_factory=dict)
    draft: Draft | None = None
    calendar_error: str | None = None
    _busy: _Busy | None = None
    _log_count: int = 0

    # -- loading ---------------------------------------------------------

    def refresh(self, force_calendar: bool = False) -> dict:
        """Reload tasks (and the calendar when stale or forced); return a snapshot."""
        client = self.services.client
        self.tasks = client.get_all_open_tasks()
        self.projects = client.get_projects()
        self.sections = client.get_sections()
        self._load_busy(force_calendar)
        return self.snapshot()

    def _today(self) -> tuple[datetime, date]:
        now = self.services.now().astimezone(self.services.config.timezone)
        return now, now.date()

    def _buckets(self) -> list[date]:
        _, today = self._today()
        return planner.weekday_buckets(today, self.services.config.horizon_days)

    def _load_busy(self, force: bool) -> None:
        now, today = self._today()
        tz = self.services.config.timezone
        window = (today, today + timedelta(days=self.services.config.horizon_days))
        cached = self._busy
        fresh = (
            cached is not None
            and cached.window == window
            and (now - cached.fetched_at).total_seconds() < BUSY_CACHE_SECONDS
        )
        if fresh and not force:
            return
        start = datetime.combine(window[0], time.min, tz)
        end = datetime.combine(window[1] + timedelta(days=1), time.min, tz)
        try:
            intervals = self.services.fetch_busy(start, end)
        except RuntimeError as exc:
            self.calendar_error = str(exc)
            self._busy = None
            return
        self.calendar_error = None
        self._busy = _Busy(now, window, intervals)

    def _capacity(self) -> dict[date, int] | None:
        """Free minutes per planned day, with the user's overrides; None without a calendar."""
        if self._busy is None:
            return None
        now, _ = self._today()
        buckets = self._buckets()
        capacity = planner.compute_capacity(buckets, self._busy.intervals, self.services.config, now)
        for day, minutes in self.capacity_overrides.items():
            if day in capacity:
                capacity[day] = minutes
        return capacity

    def _inbox_id(self) -> str | None:
        return next((p["id"] for p in self.projects if p.get("inbox_project")), None)

    def _items(self) -> list[planner.Item]:
        _, today = self._today()
        return planner.build_items(self.tasks, self._inbox_id(), today, self.services.config)

    def _inbox_items(self) -> list[planner.Item]:
        """Every open task in the Inbox itself, outside its sections, whatever its date."""
        _, today = self._today()
        inbox_id = self._inbox_id()
        tasks = [t for t in self.tasks if t.get("project_id") == inbox_id and not t.get("section_id")]
        # build_items drops far-dated work; the Inbox panel shows all of it.
        config = dataclasses.replace(self.services.config, eligibility_days=36500)
        return planner.build_items(tasks, inbox_id, today, config)

    # -- snapshot --------------------------------------------------------

    def snapshot(self) -> dict:
        now, today = self._today()
        items = self._items()
        capacity = self._capacity()
        buckets = self._buckets()
        at_risk = self._at_risk_ids(items, capacity)
        dates = self._display_dates(items)
        kids = children_index(self.tasks)
        views = {i.id: self._task_view(i, dates.get(i.id), today, at_risk, kids) for i in items}
        inbox = self._inbox_ids(views, today, at_risk, kids)
        inbox_ids = set(inbox)
        placement = place_items(items, dates, today, buckets)
        today_ids = placement["overdue"] + placement["today"]
        planned = self._planned(items, placement, today, buckets, capacity, views)

        def minutes_on(day: date) -> int:
            return planned.get(day, 0)

        return {
            "now": now.isoformat(),
            "today": today.isoformat(),
            "planning": self.draft is not None,
            "calendar_error": self.calendar_error,
            "today_column": self._column(today, today_ids, capacity, minutes_on(today)),
            "overdue": [views[i] for i in placement["overdue"]],
            "today_tasks": [views[i] for i in placement["today"]],
            "columns": [
                {**self._column(day, ids, capacity, minutes_on(day)), "tasks": [views[i] for i in ids]}
                for day, ids in placement["columns"]
            ],
            # Inbox tasks have their own panel, so the tray doesn't repeat them.
            "tray": [views[i] for i in placement["tray"] if i not in inbox_ids],
            "inbox": [views[i] for i in inbox],
            "timers": [
                {"task_id": t.task_id, "title": t.title, "started_at": t.started_at.isoformat(),
                 "is_log": t.is_log}
                for t in self.timers.values()
            ],
            # For the title editor's #project: the app resolves names itself.
            "projects": [{"id": p["id"], "name": p["name"]} for p in self.projects],
        }

    def _column(self, day: date, ids: list[str], capacity: dict[date, int] | None, planned: int) -> dict:
        free = None if capacity is None else capacity.get(day, 0)
        return {
            "date": day.isoformat(),
            "capacity_minutes": free,
            "capacity_overridden": day in self.capacity_overrides,
            "planned_minutes": planned,
            "task_count": len(ids),
        }

    def _inbox_ids(self, views: dict, today: date, at_risk: set[str], kids: dict) -> list[str]:
        """Inbox panel task ids by priority, adding views for far-dated ones the
        rest of the snapshot doesn't show."""
        inbox = sorted(self._inbox_items(), key=_priority_key)
        for item in inbox:
            if item.id not in views:
                views[item.id] = self._task_view(item, item.due_date, today, at_risk, kids)
        return [i.id for i in inbox]

    def _planned(self, items, placement, today, buckets, capacity, views) -> dict[date, int]:
        """Planned minutes per day, splitting tasks too big for their day; marks
        each split task's view with the last day it reaches."""
        by_id = {i.id: i for i in items}
        starts = {i: today for i in placement["overdue"] + placement["today"]}
        starts.update({i: day for day, ids in placement["columns"] for i in ids})
        alloc = {i: self._allocation(by_id[i], start, buckets, capacity) for i, start in starts.items()}
        for task_id, days in alloc.items():
            views[task_id]["spans_until"] = _iso(max(days)) if len(days) > 1 else None
        return planned_by_day(alloc)

    def _allocation(
        self, item: planner.Item, start: date, buckets: list[date], capacity: dict[date, int] | None
    ) -> dict[date, int]:
        """Minutes this task takes on each day: the plan's own split while it's on its
        proposed day, else spread forward from its day (see `spread`)."""
        draft = self.draft
        on_proposed_day = draft is not None and draft.final_dates.get(item.id) == draft.proposed_dates.get(item.id)
        if on_proposed_day and item.id in draft.chunks:
            return dict(draft.chunks[item.id])
        if item.pinned:
            # Pinned work happens on its day, as the planner places it.
            return {start: item.minutes} if item.minutes > 0 else {}
        return spread(item.minutes, start, buckets, capacity)

    def _at_risk_ids(self, items: list[planner.Item], capacity: dict[date, int] | None) -> set[str]:
        if self.draft is not None:
            return set(self.draft.at_risk_ids)
        if not capacity:
            return set()
        return {i.id for i in planner.pack(items, capacity).at_risk}

    def _display_dates(self, items: list[planner.Item]) -> dict[str, date | None]:
        dates = {i.id: i.due_date for i in items}
        if self.draft is not None:
            dates.update({k: v for k, v in self.draft.final_dates.items() if k in dates})
        return dates

    def _task_view(
        self, item: planner.Item, shown: date | None, today: date, at_risk: set[str], kids: dict
    ) -> dict:
        task = self._task(item.id)
        labels = list(task.get("labels") or [])
        due = task.get("due") or {}
        original = self.draft.original_dates.get(item.id) if self.draft else item.due_date
        return {
            "id": item.id,
            "content": item.content,
            "priority": item.priority,
            "due_date": _iso(shown),
            "original_due_date": _iso(original),
            "moved": self.draft is not None and shown != original,
            "due_time": _clock_time(due.get("datetime")),
            "deadline": _iso(item.deadline),
            "labels": labels,
            "project": self._project_name(task.get("project_id")),
            "minutes": item.minutes,
            "size_label": planner.format_minutes(item.minutes),
            "pinned": item.pinned,
            "fixed": any(l.lower() == planner.FIXED_LABEL for l in labels),
            "recurring": item.recurrence is not None,
            "movable": item.date_writable,
            "overdue": item.due_date is not None and item.due_date < today,
            "at_risk": item.id in at_risk,
            "spans_until": None,
            "running": item.id in self.timers,
            "url": cli._task_url(task),
            "description": task.get("description") or "",
            "comment_count": _comment_count(task),
            "subtasks": subtask_views(item.id, kids),
        }

    def _task(self, task_id: str) -> dict:
        for t in self.tasks:
            if t["id"] == task_id:
                return t
        raise TodoneError(f"task {task_id} isn't open any more; refresh and try again")

    def _item(self, task_id: str) -> planner.Item:
        for item in self._items() + self._inbox_items():
            if item.id == task_id:
                return item
        raise TodoneError(f"task {task_id} isn't one the planner can see")

    def _project_name(self, project_id: str | None) -> str | None:
        return next((p["name"] for p in self.projects if p["id"] == project_id), None)

    # -- capacity --------------------------------------------------------

    def set_capacity(self, day: str, minutes: str | None) -> dict:
        """Override a day's free time ("1h30m", "0"); empty or None resets it."""
        target = _parse_date(day)
        if target not in self._buckets():
            raise TodoneError(f"{day} isn't one of the planned days")
        if minutes is None or not minutes.strip():
            self.capacity_overrides.pop(target, None)
        else:
            try:
                self.capacity_overrides[target] = planner.parse_minutes(minutes)
            except ValueError as exc:
                raise TodoneError(str(exc)) from exc
        return self.snapshot()

    # -- moving ----------------------------------------------------------

    def move(self, task_id: str, day: str) -> dict:
        """Drag a task to `day`: edits the proposal while planning, else writes the due date."""
        target = _parse_date(day)
        _, today = self._today()
        if target < today:
            raise TodoneError("can't move a task into the past")
        item = self._item(task_id)
        current = self._display_dates([item]).get(task_id)
        if current == target:
            return self.snapshot()
        if not item.date_writable:
            raise TodoneError(f"{item.content!r} can't be moved: {_lock_reason(item)}")
        if self.draft is not None:
            self.draft.final_dates[task_id] = target
            return self.snapshot()
        _write_due(self.services.client, item, target)
        return self.refresh()

    # -- planning --------------------------------------------------------

    def propose_plan(self) -> dict:
        """Run the planner and show its proposal, as `todoist plan` would before approval."""
        capacity = self._capacity()
        if capacity is None:
            raise TodoneError(f"can't plan without the calendar: {self.calendar_error}")
        _, today = self._today()
        items = self._items()
        buckets = sorted(capacity)
        result = planner.pack(items, capacity)
        final = planner.park_overdue_dates(
            planner.earliest_scheduled_dates(result, buckets), items, today, buckets[-1]
        )
        self.draft = Draft(
            final_dates=final,
            original_dates={i.id: i.due_date for i in items},
            at_risk_ids={i.id for i in result.at_risk},
            chunks=schedule_chunks(result),
            proposed_dates=dict(final),
        )
        return self.snapshot()

    def cancel_plan(self) -> dict:
        self.draft = None
        return self.snapshot()

    def confirm_plan(self) -> dict:
        """Log and apply the reviewed plan against freshly fetched tasks.

        Re-fetching means a task closed, re-dated or pinned since the proposal
        is judged as it is now: `_apply_plan` skips anything no longer writable.
        """
        if self.draft is None:
            raise TodoneError("there's no plan to confirm")
        draft = self.draft
        client = self.services.client
        self.tasks = client.get_all_open_tasks()
        items = self._items()
        by_id = {i.id: i for i in items}
        final = {k: v for k, v in draft.final_dates.items() if k in by_id}
        labels = {t["id"]: list(t.get("labels") or []) for t in self.tasks}
        changes = cli._pending_label_changes(items, draft.at_risk_ids, labels)
        self.services.write_plan_log(final, by_id, changes, labels)
        updated, skipped, labels_changed, failed, _ = self.services.apply_plan(client, items, final, changes)
        self.draft = None
        snap = self.refresh()
        snap["result"] = {
            "updated": updated,
            "skipped": skipped,
            "labels_changed": labels_changed,
            "failed": failed,
        }
        return snap

    # -- task actions ----------------------------------------------------

    def complete(self, task_id: str) -> dict:
        if task_id in self.timers:
            raise TodoneError("stop the timer before completing this task")
        self.services.client.close_task(task_id)
        return self.refresh()

    def toggle_fixed(self, task_id: str) -> dict:
        labels = list(self._task(task_id).get("labels") or [])
        kept = [l for l in labels if l.lower() != planner.FIXED_LABEL]
        new = kept if len(kept) != len(labels) else labels + [planner.FIXED_LABEL]
        self.services.client.update_task(task_id, labels=new)
        return self.refresh()

    # -- timers ----------------------------------------------------------

    def start(self, task_id: str) -> dict:
        if task_id not in self.timers:
            task = self._task(task_id)
            url = cli._task_url(task)
            self.timers[task_id] = Timer(task_id, task.get("content", ""), self.services.now(), url)
        return self.snapshot()

    def start_log(self, title: str, description: str | None = None) -> dict:
        """Time work with no Todoist task behind it, like `todoist log`."""
        title = " ".join(title.split())
        if not title:
            raise TodoneError("give the session a title")
        self._log_count += 1
        key = f"log-{self._log_count}"
        self.timers[key] = Timer(key, title, self.services.now(), description or None, is_log=True)
        return self.snapshot()

    def stop(self, task_id: str, color: int | None = None) -> dict:
        """Stop a timer and log it to the calendar; on failure the timer is kept for a retry."""
        timer = self.timers.get(task_id)
        if timer is None:
            raise TodoneError("that task has no running timer")
        if timer.stopped_at is None:
            timer.stopped_at = self.services.now()
        ok, message = self.services.write_event(
            timer.title, timer.started_at, timer.stopped_at, timer.description, color
        )
        if not ok:
            help_text = self.services.calendar_help(message) or ""
            raise TodoneError(f"couldn't log to the calendar: {message}\n{help_text}".strip())
        del self.timers[task_id]
        return self.snapshot()

    def discard_timer(self, task_id: str) -> dict:
        self.timers.pop(task_id, None)
        return self.snapshot()

    # -- details ---------------------------------------------------------

    def details(self, task_id: str) -> dict:
        client = self.services.client
        task = client.get_task(task_id)
        return {
            "task": _detail_view(task, self.services.config.default_task_minutes),
            "comments": [_comment_view(c) for c in client.get_task_comments(task_id)],
            "projects": [{"id": p["id"], "name": p["name"]} for p in self.projects],
            "sections": [
                {"id": s["id"], "name": s["name"], "project_id": s["project_id"]} for s in self.sections
            ],
            "labels": sorted({l["name"] for l in client.get_labels()}),
        }

    def update_details(self, task_id: str, changes: dict) -> dict:
        """Write the fields the details form changed. Keys absent from `changes` are left alone."""
        self._write_changes(task_id, changes)
        self.refresh()
        return self.details(task_id)

    def quick_update(self, task_id: str, changes: dict) -> dict:
        """Like update_details, for edits made on the card itself (title, priority);
        returns a snapshot instead of the task's details."""
        self._write_changes(task_id, changes)
        return self.refresh()

    def _write_changes(self, task_id: str, changes: dict) -> None:
        client = self.services.client
        task = client.get_task(task_id)
        kwargs = _update_kwargs(task, changes)
        if kwargs:
            client.update_task(task_id, **kwargs)
        _move_project(client, task, changes)

    def add_comment(self, task_id: str, content: str) -> dict:
        self.services.client.add_comment(task_id, content)
        return self.details(task_id)

    def edit_comment(self, task_id: str, comment_id: str, content: str) -> dict:
        self.services.client.update_comment(comment_id, content)
        return self.details(task_id)

    def colors(self) -> list[dict]:
        return [{"id": cid, "name": name, "hex": hexv} for cid, name, hexv in tracker.EVENT_COLORS]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def place_items(
    items: list[planner.Item], dates: dict[str, date | None], today: date, buckets: list[date]
) -> dict:
    """Decide where each task shows: overdue, today, a later day's column, or the tray.

    Overdue and today are sorted by date then priority; columns and the tray by
    priority. A task dated past the last column is left out — it isn't this
    week's work — and so is an undated one outside the Inbox (build_items
    already dropped those).
    """
    later = [d for d in buckets if d > today]
    horizon_end = buckets[-1] if buckets else today
    slots: dict = {"overdue": [], "today": [], "tray": [], **{d: [] for d in later}}
    for item in items:
        slot = _slot(dates.get(item.id), today, horizon_end)
        if slot is not None:
            slots[slot].append(item)

    def by_day(key: str) -> list[str]:
        return [i.id for i in sorted(slots[key], key=lambda i: _day_key(i, dates))]

    def by_priority(key) -> list[str]:
        return [i.id for i in sorted(slots[key], key=_priority_key)]

    return {
        "overdue": by_day("overdue"),
        "today": by_day("today"),
        "columns": [(d, by_priority(d)) for d in later],
        "tray": by_priority("tray"),
    }


def _slot(shown: date | None, today: date, horizon_end: date):
    """"overdue", "today", "tray", a later day, or None when past the horizon.
    A weekend inside the horizon has no column, so it goes to the tray."""
    if shown is None:
        return "tray"
    if shown < today:
        return "overdue"
    if shown == today:
        return "today"
    if shown > horizon_end:
        return None
    return shown if shown.weekday() < 5 else "tray"


def spread(minutes: int, start: date, buckets: list[date], capacity: dict[date, int] | None) -> dict[date, int]:
    """Split a task's minutes across days from `start`: each day takes up to its
    free time, and what doesn't fit moves to the next planned day. Whatever is
    left at the end of the horizon stays on the last day reached, so the total is
    always kept. Without a calendar, or on a day that isn't planned, the whole
    task sits on `start`.

    Each task is measured against the whole day on its own, not against what
    other tasks already took: this is about a task too big for one day, while a
    day overloaded by several tasks should still look overloaded.
    """
    if minutes <= 0:
        return {}
    if capacity is None or start not in capacity:
        return {start: minutes}
    days = [d for d in buckets if d >= start]
    out: dict[date, int] = {}
    need = minutes
    for day in days:
        take = min(need, capacity.get(day, 0))
        if take > 0:
            out[day] = take
            need -= take
        if need == 0:
            return out
    last = max(out, default=start)
    out[last] = out.get(last, 0) + need
    return out


def children_index(tasks: list[dict]) -> dict[str, list[dict]]:
    """Open tasks by parent id, each list in Todoist's own order."""
    kids: dict[str, list[dict]] = {}
    for t in tasks:
        if t.get("parent_id"):
            kids.setdefault(t["parent_id"], []).append(t)
    for children in kids.values():
        children.sort(key=lambda t: (t.get("child_order", 0), t["id"]))
    return kids


def subtask_views(parent_id: str, kids: dict[str, list[dict]], seen: frozenset = frozenset()) -> list[dict]:
    """The open subtasks under `parent_id`, nested all the way down. `seen` stops a
    (malformed) parent loop from recursing forever."""
    seen = seen | {parent_id}
    return [
        {
            "id": t["id"],
            "content": t.get("content", ""),
            "priority": 5 - t.get("priority", 1),
            "due_date": ((t.get("due") or {}).get("date") or "")[:10] or None,
            "labels": list(t.get("labels") or []),
            "comment_count": _comment_count(t),
            "url": cli._task_url(t),
            "subtasks": [] if t["id"] in seen else subtask_views(t["id"], kids, seen),
        }
        for t in kids.get(parent_id, [])
    ]


def _comment_count(task: dict) -> int:
    """API v1 calls it note_count; older payloads comment_count."""
    return int(task.get("note_count") or task.get("comment_count") or 0)


def planned_by_day(alloc: dict[str, dict[date, int]]) -> dict[date, int]:
    totals: dict[date, int] = {}
    for days in alloc.values():
        for day, minutes in days.items():
            totals[day] = totals.get(day, 0) + minutes
    return totals


def schedule_chunks(result: planner.PlanResult) -> dict[str, dict[date, int]]:
    """Each scheduled task's minutes per day, as `planner.pack` split them."""
    chunks: dict[str, dict[date, int]] = {}
    for day, day_chunks in result.schedule.items():
        for chunk in day_chunks:
            if chunk.minutes > 0:
                per_day = chunks.setdefault(chunk.item.id, {})
                per_day[day] = per_day.get(day, 0) + chunk.minutes
    return chunks


def _priority_key(item: planner.Item) -> tuple:
    return (item.priority, item.content.lower(), item.id)


def _day_key(item: planner.Item, dates: dict[str, date | None]) -> tuple:
    return (dates.get(item.id) or date.max, *_priority_key(item))


def _lock_reason(item: planner.Item) -> str:
    if item.pinned:
        return "it's pinned (a time of day or @fixed)"
    return f"moving one occurrence of {item.recurrence!r} would shift the whole series"


def _write_due(client, item: planner.Item, target: date) -> None:
    if item.recurrence is None:
        client.update_task(item.id, due_date=target.isoformat())
    else:
        client.reschedule_recurring(item.id, target.isoformat(), item.recurrence)


def _detail_view(task: dict, default_minutes: int) -> dict:
    due = task.get("due") or {}
    duration = task.get("duration") or {}
    return {
        "id": task["id"],
        "content": task.get("content", ""),
        "description": task.get("description", ""),
        "priority": 5 - task.get("priority", 1),
        "due_string": due.get("string") or due.get("date") or "",
        "deadline": (task.get("deadline") or {}).get("date"),
        "labels": list(task.get("labels") or []),
        "size_minutes": planner.parse_size(task, default_minutes),
        "has_native_duration": bool(duration.get("amount")),
        "project_id": task.get("project_id"),
        "section_id": task.get("section_id"),
        "url": cli._task_url(task),
    }


def _comment_view(comment: dict) -> dict:
    attachment = comment.get("attachment") or comment.get("file_attachment")
    return {
        "id": comment["id"],
        "content": comment.get("content", ""),
        "posted_at": comment.get("posted_at"),
        "attachment": None
        if not attachment
        else {
            "file_name": attachment.get("file_name") or attachment.get("resource_type") or "attachment",
            "file_url": attachment.get("file_url") or attachment.get("url"),
            "file_type": attachment.get("file_type"),
        },
    }


def _update_kwargs(task: dict, changes: dict) -> dict:
    """Translate the form's changed fields into update_task keyword arguments."""
    kwargs: dict = {}
    for key in ("content", "description", "labels"):
        if key in changes:
            kwargs[key] = changes[key]
    if "priority" in changes:
        kwargs["priority"] = 5 - int(changes["priority"])
    if "due_string" in changes:
        kwargs["due_string"] = changes["due_string"].strip() or NO_DATE
    if "deadline" in changes:
        _deadline_kwargs(kwargs, changes["deadline"])
    if "size_minutes" in changes:
        _size_kwargs(kwargs, task, int(changes["size_minutes"]))
    return kwargs


def _deadline_kwargs(kwargs: dict, deadline: str | None) -> None:
    if deadline:
        kwargs["deadline_date"] = _parse_date(deadline).isoformat()
    else:
        kwargs["clear_deadline"] = True


def _size_kwargs(kwargs: dict, task: dict, minutes: int) -> None:
    """A native duration wins over a title hint, so write whichever the task uses."""
    if minutes < 0:
        raise TodoneError("size can't be negative")
    if (task.get("duration") or {}).get("amount"):
        kwargs["duration_minutes"] = minutes
    else:
        kwargs["content"] = planner.with_size_hint(kwargs.get("content", task.get("content", "")), minutes)


def _move_project(client, task: dict, changes: dict) -> None:
    """Move to the chosen section, or to the project itself when the section was cleared."""
    if "section_id" not in changes and "project_id" not in changes:
        return
    section = changes.get("section_id", task.get("section_id"))
    project = changes.get("project_id", task.get("project_id"))
    if section:
        if section != task.get("section_id"):
            client.move_task(task["id"], section_id=section)
    elif project != task.get("project_id") or task.get("section_id"):
        client.move_task(task["id"], project_id=project)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TodoneError(f"not a date: {value!r}") from exc


def _clock_time(value: str | None) -> str | None:
    """"15:00" from a due datetime ("2026-11-16T15:00:00", floating or UTC); None without one.

    A UTC timestamp (trailing Z) is shown in local time; a floating one as written."""
    if not value:
        return None
    if value.endswith("Z"):
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M")
    return value[11:16]


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
