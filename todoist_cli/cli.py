"""CLI for Todoist task management.

Usage:
    todoist create "Buy milk"
    todoist create "Review PR" --due "tomorrow" --labels work,review
    todoist create "Fix bug" --description "See ticket #123" --priority 2
    todoist create "Ship ladder" --due "tomorrow" --deadline 2026-08-17

    todoist list

    todoist update TASK_ID --due "Jun 4 2026"
    todoist update TASK_ID --deadline 2026-09-26
    todoist update TASK_ID --priority 2 --labels work,urgent

Two date fields, two meanings: --due is when you plan to do the work, --deadline
is when it has to be done by. A committed or externally-imposed date belongs in
--deadline (ISO YYYY-MM-DD only), with --due set to when you'll actually start.

    todoist search --label bills
    todoist search --label work --attachments

    todoist start "review the PR"   # quote multi-word search phrases
    todoist start                   # no phrase → priority lookahead: P1 due/deadline
                                    #   within 3 days, P2 within 2, others today (+overdue)

    todoist log client call         # like start, but no Todoist task behind it
    todoist log "Acme call" -d "intro with CTO"

    todoist close TASK_ID

    todoist plan                    # schedule the week around your calendar

Reads TODOIST_API_TOKEN (and GOG_ACCOUNT) from the environment or from
~/.config/todoist-cli/.env. See README.md for setup and the Todoist conventions
`plan` and `start` rely on.

`start`, `log` and `plan` read from / write to Google Calendar through `gog`. When
that account's token expires, each offers to run `gog-reauth` (installed alongside
`todoist`) and then retry, so a tracked session isn't lost.

Priority scale (user-facing): 1=highest/urgent, 2=high, 3=medium, 4=lowest/normal.
The API uses the inverse (4=urgent, 1=normal); this CLI accepts user-facing values
and converts them automatically.
"""

import argparse
import json
import os
import re
import shlex
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

from todoist_cli import paths

# Real environment variables win over the file (load_dotenv doesn't override).
load_dotenv(paths.config_dir() / ".env")

from todoist_cli import planner, tracker
from todoist_cli.client import TodoistClient, TodoistAPIError, is_text_attachment


def _get_client() -> TodoistClient:
    token = os.getenv("TODOIST_API_TOKEN")
    if not token:
        print(
            "ERROR: TODOIST_API_TOKEN is not set. Export it, or put it in "
            f"{paths.config_dir() / '.env'}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return TodoistClient(token)


def _validated_deadline(raw: str | None) -> str | None:
    """Return an ISO deadline date, exiting with a clear error if malformed.

    The API accepts only YYYY-MM-DD for deadlines — there is no natural-language
    form as there is for due dates — so catch "next Tuesday" here rather than
    letting it come back as an opaque API error.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
        # strptime is lenient about zero-padding ("2026-8-17" parses), but the
        # API wants the canonical form — so round-trip and demand an exact match.
        canonical = parsed.strftime("%Y-%m-%d") == raw
    except ValueError:
        canonical = False
    if not canonical:
        print(
            f"ERROR: --deadline must be an ISO date (YYYY-MM-DD), got {raw!r}. "
            "Deadlines take no natural-language form; use --due for that.",
            file=sys.stderr,
        )
        sys.exit(1)
    return raw


def cmd_create(args: argparse.Namespace) -> None:
    labels = [l.strip() for l in args.labels.split(",")] if args.labels else None
    # User-facing priority: 1=highest. API priority: 4=highest. Convert here.
    api_priority = (5 - args.priority) if args.priority is not None else None
    deadline = _validated_deadline(args.deadline)

    client = _get_client()
    try:
        task = client.create_task(
            args.content,
            description=args.description,
            project_id=args.project_id,
            labels=labels,
            due_string=args.due,
            deadline_date=deadline,
            priority=api_priority,
        )
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Created task: {task['id']}")
    print(f"  Content:  {task['content']}")
    if task.get("description"):
        print(f"  Desc:     {task['description']}")
    if task.get("labels"):
        print(f"  Labels:   {', '.join(task['labels'])}")
    if task.get("due"):
        print(f"  Due:      {task['due'].get('string', task['due'].get('date', ''))}")
    if task.get("deadline"):
        print(f"  Deadline: {task['deadline'].get('date', '')}")
    print(f"  URL:      {task.get('url', 'n/a')}")


def cmd_update(args: argparse.Namespace) -> None:
    labels = [l.strip() for l in args.labels.split(",")] if args.labels else None
    # User-facing priority: 1=highest. API priority: 4=highest. Convert here.
    api_priority = (5 - args.priority) if args.priority is not None else None

    deadline = _validated_deadline(args.deadline)

    if all(
        v is None
        for v in (args.content, args.description, labels, args.due, deadline, api_priority)
    ):
        print(
            "ERROR: nothing to update; provide at least one field to change.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = _get_client()
    try:
        task = client.update_task(
            args.task_id,
            content=args.content,
            description=args.description,
            labels=labels,
            due_string=args.due,
            deadline_date=deadline,
            priority=api_priority,
        )
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Updated task: {task['id']}")
    print(f"  Content:  {task['content']}")
    if task.get("description"):
        print(f"  Desc:     {task['description']}")
    if task.get("labels"):
        print(f"  Labels:   {', '.join(task['labels'])}")
    if task.get("due"):
        print(f"  Due:      {task['due'].get('string', task['due'].get('date', ''))}")
    if task.get("deadline"):
        print(f"  Deadline: {task['deadline'].get('date', '')}")
    print(f"  URL:      {task.get('url', 'n/a')}")


def _task_due_deadline(task: dict) -> tuple[str, str]:
    """Return (due, deadline) display strings for a task, each "-" when absent."""
    due = "-"
    if task.get("due"):
        due = task["due"].get("string", task["due"].get("date", "")) or "-"
    deadline = "-"
    if task.get("deadline"):
        deadline = task["deadline"].get("date", "") or "-"
    return due, deadline


def cmd_list(args: argparse.Namespace) -> None:
    client = _get_client()
    try:
        tasks = client.get_all_open_tasks()
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not tasks:
        print("No open tasks.")
        return

    # Sort highest priority first. API priority: 4=urgent .. 1=normal.
    tasks.sort(key=lambda t: t.get("priority", 1), reverse=True)

    print(f"Found {len(tasks)} open task(s):")
    for task in tasks:
        # User-facing priority: 1=highest. API priority: 4=highest. Convert here.
        user_priority = 5 - task.get("priority", 1)
        due, deadline = _task_due_deadline(task)
        labels = ", ".join(task.get("labels", []))
        print(
            f"  [{task['id']}] P{user_priority} | due: {due} | deadline: {deadline}"
            f" | {task['content']}"
        )
        if task.get("description"):
            print(f"         {task['description']}")
        if labels:
            print(f"         labels: {labels}")


def cmd_search(args: argparse.Namespace) -> None:
    client = _get_client()
    try:
        tasks = client.get_open_tasks_by_label(args.label)
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        # Emit raw task objects for programmatic consumers.
        # Must come before the empty-result sentinel below so an empty label
        # prints `[]` rather than prose that would break a JSON parser.
        print(json.dumps(tasks))
        return

    if not tasks:
        print(f"No open tasks with label '{args.label}'.")
        return

    print(f"Found {len(tasks)} open task(s) with label '{args.label}':")
    for task in tasks:
        due = ""
        if task.get("due"):
            due = f"  due: {task['due'].get('string', task['due'].get('date', ''))}"
        labels = ", ".join(task.get("labels", []))
        print(f"  [{task['id']}] {task['content']}{due}")
        if task.get("description"):
            print(f"         {task['description']}")
        if labels:
            print(f"         labels: {labels}")

        if args.attachments:
            _print_text_attachments(client, task["id"])


def cmd_filter(args: argparse.Namespace) -> None:
    """List open tasks matching a Todoist filter query, e.g. "today | overdue"."""
    client = _get_client()
    try:
        tasks = client.get_open_tasks_by_filter(args.query)
        if args.comments:
            for task in tasks:
                task["comments"] = client.get_task_comments(task["id"])
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(tasks))
        return
    if not tasks:
        print(f"No open tasks match '{args.query}'.")
        return

    print(f"Found {len(tasks)} open task(s) matching '{args.query}':")
    for task in tasks:
        _print_filtered_task(task)


def _print_filtered_task(task: dict) -> None:
    user_priority = 5 - task.get("priority", 1)
    due, deadline = _task_due_deadline(task)
    print(
        f"  [{task['id']}] P{user_priority} | due: {due} | deadline: {deadline}"
        f" | {task['content']}"
    )
    if task.get("description"):
        print(f"         {task['description']}")
    if task.get("labels"):
        print(f"         labels: {', '.join(task['labels'])}")
    for comment in task.get("comments", []):
        print(f"         comment: {comment.get('content', '')}")


def cmd_comment(args: argparse.Namespace) -> None:
    client = _get_client()
    try:
        comment = client.add_comment(args.task_id, args.text)
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Added comment {comment.get('id', '?')} to task {args.task_id}")


def _print_text_attachments(client: TodoistClient, task_id: str) -> None:
    """Fetch comments for a task and print the content of any text attachments."""
    try:
        comments = client.get_task_comments(task_id)
    except TodoistAPIError as exc:
        print(f"         [attachments] WARNING: could not fetch comments: {exc}", file=sys.stderr)
        return

    for comment in comments:
        attachment = comment.get("attachment")
        if not attachment or not is_text_attachment(attachment):
            continue
        file_name = attachment.get("file_name", "attachment")
        file_url = attachment.get("file_url", "")
        try:
            text = client.fetch_attachment_text(file_url)
            print(f"         [attachment: {file_name}]")
            for line in text.splitlines():
                print(f"           {line}")
        except TodoistAPIError as exc:
            print(f"         [attachment: {file_name}] WARNING: could not fetch: {exc}", file=sys.stderr)


# Todoist filter operators. A raw search phrase is interpolated into a filter
# query (`search: <phrase>`), so these must be neutralized or the phrase would
# be parsed as filter syntax — e.g. "a | b" pulls in everything matching `b`,
# and "(foo" makes the API return HTTP 400.
_FILTER_METACHARS = re.compile(r"[()|&,]")


def _build_search_query(term: str) -> str:
    """Build a Todoist filter query that searches task content for ``term``.

    Filter metacharacters in the phrase are replaced with spaces so the phrase
    is treated as literal search text rather than query operators.
    """
    safe = _FILTER_METACHARS.sub(" ", term).strip()
    return f"search: {safe}"


def _task_url(task: dict) -> str:
    """Web URL for a task. The API objects carry no url field, so build it from id."""
    return f"https://app.todoist.com/app/task/{task['id']}"


def _effective_date(task: dict) -> str:
    """Earliest of due date and deadline; a high sentinel when the task has neither.

    `start` asks "what do I work on now", so a task with a near deadline is as
    urgent as one with a near due date. Date fields may be date-only or
    timestamped; we compare the date portion only, so a same-day timestamped due
    date ties with a date-only one instead of sorting after it.
    """
    due = task["due"].get("date") if task.get("due") else None
    deadline = task["deadline"].get("date") if task.get("deadline") else None
    dates = [d[:10] for d in (due, deadline) if d]
    return min(dates) if dates else "9999-12-31"


def _sort_key(task: dict) -> tuple:
    """Sort key: soonest date first, then priority desc, then content alpha.

    Dateless tasks sort last (high sentinel). ISO date strings sort correctly
    lexically; the API priority scale is 4=urgent..1=normal, so we negate it to
    put urgent first within a date. Content is the final, case-insensitive tiebreaker.
    """
    return (
        _effective_date(task),
        -task.get("priority", 1),
        task.get("content", "").lower(),
    )


# Today-anchored lookahead per priority for `start` with no search term, using the
# API priority scale (4=P1 .. 1=P4). A task qualifies if its due date OR deadline is
# on/before today+(days-1); overdue items (date in the past) always qualify.
_PRIORITY_WINDOW_DAYS = {4: 3, 3: 2}  # P1→3 days, P2→2 days; everything else→1 day

# Prefetch superset for the no-term `start` default: every task due or deadlined
# within the widest (3-day) window, including overdue. The Python window filter
# (_within_window) then narrows by priority. Verified against the live API:
# `deadline before:` is accepted and functional.
_START_PREFILTER = "(due before: +3 days | deadline before: +3 days)"


def _within_window(task: dict, days: int, today: date) -> bool:
    """True if the task's due date or deadline is on/before today+(days-1).

    ``days`` is the today-anchored window: 1=today (incl. overdue), 2=today+tomorrow,
    etc. Date fields may be date-only ("2026-06-20") or datetime ("2026-06-20T15:00");
    we compare the date portion only. ISO strings sort correctly lexically.
    """
    cutoff = (today + timedelta(days=days - 1)).isoformat()
    due = task["due"].get("date") if task.get("due") else None
    deadline = task["deadline"].get("date") if task.get("deadline") else None
    return any(d and d[:10] <= cutoff for d in (due, deadline))


def _offer_reauth(account: str) -> bool:
    """Offer to re-authenticate right here. True if auth works afterwards.

    Never prompts unless both ends are a terminal: a cron/launchd/piped run has
    nobody to approve a browser consent screen, so it must fail loudly with the
    printed instructions instead of blocking on a prompt. The reauth is opt-in
    every time rather than automatic — it opens a browser and re-consents real
    Google scopes, which is not something a `todoist` command should do on its
    own initiative.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input(f"Run {tracker.REAUTH_COMMAND} now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    if answer not in ("y", "yes"):
        return False
    if not tracker.run_reauth(account):
        print("Re-authentication did not complete.", file=sys.stderr)
        return False
    if not tracker.gog_auth_ok(account):
        print("Re-authentication finished but calendar access still fails.", file=sys.stderr)
        return False
    return True


def _resolve_account(config_account: str | None = None) -> str:
    """The Google account to use, or exit explaining how to configure one.

    `config_account` is the ``account`` from the plan.toml a command actually
    loaded (``plan --config`` may point somewhere other than the default).
    """
    try:
        account = config_account or tracker.configured_account()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if not account:
        print(
            "ERROR: no Google account configured. Set GOG_ACCOUNT (in the environment\n"
            f"or {paths.config_dir() / '.env'}), or add\n"
            f'`account = "you@example.com"` to {_PLAN_CONFIG_FILE}.',
            file=sys.stderr,
        )
        sys.exit(1)
    return account


def _require_calendar_auth(account: str) -> None:
    # Verify calendar auth up front so a session is never tracked only to fail
    # at the end when we try to write the event.
    if not tracker.gog_auth_ok(account):
        print(tracker.reauth_help(account), file=sys.stderr)
        if not _offer_reauth(account):
            sys.exit(1)


def cmd_start(args: argparse.Namespace) -> None:
    account = _resolve_account()
    _require_calendar_auth(account)

    client = _get_client()
    query = _build_search_query(args.term) if args.term else _START_PREFILTER
    try:
        with tracker.spinner("Fetching tasks"):
            tasks = client.get_open_tasks_by_filter(query)
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not args.term:
        # Narrow the prefetched superset by priority-aware lookahead window.
        today = datetime.now().astimezone().date()
        tasks = [
            t for t in tasks
            if _within_window(t, _PRIORITY_WINDOW_DAYS.get(t.get("priority", 1), 1), today)
        ]

    if not tasks:
        if args.term:
            print(f"No open tasks matching '{args.term}'.", file=sys.stderr)
        else:
            print(
                "No open tasks within the priority lookahead window "
                "(P1: 3 days, P2: 2 days, others: today).",
                file=sys.stderr,
            )
        sys.exit(1)

    tasks.sort(key=_sort_key)

    if len(tasks) == 1:
        task = tasks[0]
    else:
        task = tracker.select_task(tasks)
        if task is None:
            print("Cancelled.", file=sys.stderr)
            sys.exit(1)

    _track_and_log(
        task["content"],
        account=account,
        details=task.get("description"),
        event_description=_task_url(task),
    )


def cmd_log(args: argparse.Namespace) -> None:
    """Track time like `start`, but for work with no Todoist task behind it."""
    title = " ".join(args.title).strip()
    if not title:
        print("ERROR: give the session a title.", file=sys.stderr)
        sys.exit(1)
    account = _resolve_account()
    _require_calendar_auth(account)
    _track_and_log(title, account=account, event_description=args.description)


def _track_and_log(
    title: str,
    *,
    account: str,
    details: str | None = None,
    event_description: str | None = None,
) -> None:
    """Run the timer, then write the session to Google Calendar as ``title``.

    ``details`` is shown while tracking; ``event_description`` goes on the calendar event.
    """
    start = datetime.now().astimezone()
    print(f"\nTracking: {tracker.bold(title)}")
    if details:
        print(f"\n{details}\n")
    print(f"Started:  {start.strftime('%Y-%m-%d %H:%M:%S')}\n")

    try:
        end = tracker.run_timer(start)
    except KeyboardInterrupt:
        print("\nAborted — no calendar event created.", file=sys.stderr)
        sys.exit(1)

    print(f"Stopped:  {end.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Elapsed:  {tracker.bold(tracker.format_elapsed(end - start))}")

    color_id = tracker.select_event_color()
    ok, message = tracker.create_calendar_event(
        title, start, end, description=event_description, account=account, event_color=color_id
    )
    if not ok:
        print(f"ERROR creating calendar event: {message}", file=sys.stderr)
        # The token can go stale *during* a tracked session, so the up-front
        # check isn't enough. This is the costly failure — the session is
        # already spent — so offer to fix it in place and write the event
        # rather than making the user re-track or log it by hand.
        help_text = tracker.gog_error_help(message, account)
        if help_text:
            print(f"\n{help_text}", file=sys.stderr)
        if tracker.gog_error_kind(message) in tracker.REAUTHABLE_KINDS and _offer_reauth(account):
            ok, message = tracker.create_calendar_event(
                title, start, end, description=event_description, account=account, event_color=color_id
            )
            if not ok:
                print(f"ERROR creating calendar event: {message}", file=sys.stderr)

    if ok:
        print(f"📅 {tracker.bold('Logged to calendar')}")
    else:
        print(
            f"\nTracked times (log manually): {start.isoformat()} -> {end.isoformat()}"
            f"  | {title}",
            file=sys.stderr,
        )
        retry = tracker.calendar_event_command(
            title, start, end, description=event_description, account=account, event_color=color_id
        )
        print(
            f"Or re-create it directly once that's sorted:\n    {shlex.join(retry)}",
            file=sys.stderr,
        )
        sys.exit(1)


# Label applied to tasks in the current plan's at-risk set (planner.pack's
# PlanResult.at_risk) and stripped from tasks that aren't (or no longer are).
_AT_RISK_LABEL = "atrisk"


_PLAN_CONFIG_FILE = paths.config_dir() / "plan.toml"
_PLAN_LOG_DIR = paths.state_dir() / "plan_logs"


def _confirm_capacity(capacity: dict, buckets: list, *, auto_accept: bool) -> dict | None:
    """Show the capacity table; let the user accept, edit, or quit.

    Returns the (possibly edited) capacity, or None if the user quit.
    """
    current = dict(capacity)
    while True:
        print("\nAvailable time next week:")
        for day in buckets:
            print(f"  {day.strftime('%a %b %d')}: {planner.format_minutes(current[day])}")

        if auto_accept:
            return current
        try:
            raw = input(
                '\nEnter to accept, edits (e.g. "07-28=1h30m 07-31=0"), or q to quit: '
            ).strip()
        except EOFError:
            return current
        if raw == "":
            return current
        if raw.lower() in ("q", "quit"):
            return None
        try:
            current = planner.parse_capacity_edits(raw, current)
        except ValueError as exc:
            print(f"  {exc}", file=sys.stderr)


def _format_task_row(item: "planner.Item", final_dates: dict, today: date, *, color: bool = False) -> str:
    """Render one plan line using the same priority-tag/bold-title styling as
    `tracker.format_row` (the `start` picker) — including the red deadline when
    it's today or already past — plus the plan-specific bits `start` has no use
    for: the proposed size, an old -> new date arrow when the plan is moving the
    task, and a marker for a pinned or recurring task.
    """
    # Titles that already carry their own hint ("Call the dentist 15m") would
    # otherwise show the same duration twice, once raw from the title and
    # once reformatted (e.g. "90m 1h30m") — only show the computed size when
    # the title doesn't already make it visible.
    has_title_hint = planner.title_size_minutes(item.content) is not None
    size = "" if has_title_hint else f" {planner.format_minutes(item.minutes)}"

    title = item.content
    if color and tracker.dates_include_today(item.due_date, item.deadline, today=today):
        title = tracker.bold(title)
    tag = tracker.priority_tag(item.priority, color=color)
    change = _date_change(item, final_dates)

    if item.deadline:
        text = f"(deadline {tracker.format_date(item.deadline)})"
        if color and tracker.deadline_is_due(item.deadline, today=today):
            text = tracker.red(text)
        deadline = f" {text}"
    else:
        deadline = ""
    return f"{tag} {title}{size}{deadline}{change}{item.marker}"


def _date_change(item: "planner.Item", final_dates: dict) -> str:
    """The row's date: an old -> new arrow when the plan moves it, else the date."""
    # A pinned task is never rewritten, so it never gets an arrow: an overdue
    # one is shown on today because that is when it will be worked, not because
    # the plan is moving its date. A plan-only recurring task does get one — the
    # plan does move it — and its marker says Todoist keeps the old date.
    new_date = None if item.pinned else final_dates.get(item.id)
    if item.due_date and new_date and item.due_date != new_date:
        return f" ({tracker.format_date(item.due_date)} -> {tracker.format_date(new_date)})"
    if new_date:
        return f" ({tracker.format_date(new_date)})"
    if item.due_date:
        return f" ({tracker.format_date(item.due_date)})"
    return ""


def _print_schedule(
    result: "planner.PlanResult",
    buckets: list,
    earliest: dict,
    final_dates: dict,
    today: date,
    *,
    color: bool = False,
) -> None:
    for day in buckets:
        print(f"\n{day.strftime('%A, %B %-d')}:")
        seen: set = set()
        printed = False
        for chunk in result.schedule[day]:
            item = chunk.item
            if earliest.get(item.id) != day or item.id in seen:
                continue
            seen.add(item.id)
            printed = True
            print(f"   {_format_task_row(item, final_dates, today, color=color)}")
        if not printed:
            print("   (nothing scheduled)")

    # Everything at risk is listed here, including items that *were* scheduled
    # but only finish after their deadline — those appear twice on purpose
    # (once under their day, once here), since the whole point of the section is
    # that you can see every deadline in trouble without scanning the days.
    if result.at_risk:
        print("\nAt risk:")
        for item in sorted(result.at_risk, key=lambda i: i.sort_key()):
            print(f"   {_format_task_row(item, final_dates, today, color=color)}")

    if result.unplanned:
        print("\nNot planned (due on a day this plan doesn't cover):")
        for item in sorted(result.unplanned, key=lambda i: i.sort_key()):
            reason = planner.unplanned_reason(item, buckets)
            row = _format_task_row(item, final_dates, today, color=color)
            print(f"   {row} -- {reason}")

    if result.unscheduled:
        counts: dict = {}
        for item in result.unscheduled:
            counts[item.priority] = counts.get(item.priority, 0) + 1
        parts = ", ".join(f"{counts[p]} P{p}" for p in (1, 2, 3, 4) if counts.get(p))
        print(f"\nNot scheduled: {len(result.unscheduled)} task(s) — {parts}")


def _clean(text: str) -> str:
    """Collapse tabs/newlines so a title can't break the tab-separated log."""
    return " ".join(text.split())


def _write_plan_log(
    final_dates: dict,
    items_by_id: dict,
    label_changes: list | None = None,
    old_labels: dict | None = None,
) -> Path:
    """Record what an apply will change, with the old values so it can be undone.

    Date lines: ``id<TAB>new_date<TAB>old_due<TAB>content`` (old_due is ``-``
    when the task had none). Label lines: ``LABELS<TAB>id<TAB>old_json<TAB>new_json<TAB>content``.
    """
    _PLAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _PLAN_LOG_DIR / f"plan-{stamp}.txt"
    lines = [
        f"# todoist plan approved {datetime.now().isoformat()}",
        "# date lines: id, new_date, old_due, content",
        "# label lines: LABELS, id, old_labels, new_labels, content",
        "",
    ]
    for tid, new_date in sorted(final_dates.items(), key=lambda kv: kv[1]):
        item = items_by_id.get(tid)
        if item is None:
            continue
        old_due = item.due_date.isoformat() if item.due_date else "-"
        lines.append(f"{tid}\t{new_date.isoformat()}\t{old_due}\t{_clean(item.content)}")
    for item, new_labels, _action in label_changes or []:
        old = (old_labels or {}).get(item.id, [])
        lines.append(
            f"LABELS\t{item.id}\t{json.dumps(old)}\t{json.dumps(new_labels)}\t{_clean(item.content)}"
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def _exit_if_failed(failed: int, attempted: int) -> None:
    """Print an "N of M failed" summary and exit non-zero if anything failed."""
    if failed:
        print(f"ERROR: {failed} of {attempted} update(s) failed.", file=sys.stderr)
        sys.exit(1)


def _plan_preflight(args: argparse.Namespace, account: str) -> bool:
    """Fail fast on calendar auth and on a missing terminal; return interactive."""
    # Verify calendar auth up front, matching cmd_start's fail-fast pattern.
    _require_calendar_auth(account)

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and not args.yes:
        print(
            "ERROR: todoist plan needs a terminal to review and approve the schedule.\n"
            "Re-run with --yes to auto-approve non-interactively, or --dry-run to preview only.",
            file=sys.stderr,
        )
        sys.exit(1)
    return interactive


def _load_plan_config(args: argparse.Namespace):
    config_path = Path(args.config) if args.config else _PLAN_CONFIG_FILE
    try:
        return planner.load_config(config_path)
    except (ValueError, OSError) as exc:
        print(f"ERROR: could not load {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _read_busy(account, config, window_start, window_end):
    """Fetch calendar busy times, offering one re-auth retry; exit 1 on failure."""
    try:
        with tracker.spinner("Reading calendar"):
            return tracker.fetch_busy(account, config.calendars, window_start, window_end)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        # `account` comes from plan.toml and may differ from the account the
        # up-front check probed, so name the one that actually failed.
        help_text = tracker.gog_error_help(str(exc), account)
        if help_text:
            print(f"\n{help_text}", file=sys.stderr)
        if tracker.gog_error_kind(str(exc)) in tracker.REAUTHABLE_KINDS and _offer_reauth(account):
            try:
                with tracker.spinner("Reading calendar"):
                    return tracker.fetch_busy(
                        account, config.calendars, window_start, window_end
                    )
            except RuntimeError as retry_exc:
                print(f"ERROR: {retry_exc}", file=sys.stderr)
        sys.exit(1)


def _pending_label_changes(items, at_risk_ids, task_labels_by_id):
    changes: list[tuple[planner.Item, list[str], str]] = []
    for item in items:
        current = task_labels_by_id.get(item.id, [])
        should_have = item.id in at_risk_ids
        has_label = _AT_RISK_LABEL in current
        if should_have == has_label:
            continue
        if should_have:
            changes.append((item, current + [_AT_RISK_LABEL], "add"))
        else:
            changes.append((item, [l for l in current if l != _AT_RISK_LABEL], "remove"))
    return changes


def _read_choice(auto_yes: bool) -> str:
    if auto_yes:
        return "y"
    try:
        return input("\nApprove this plan (y/Enter), edit (e), or quit (q)? ").strip().lower()
    except EOFError:
        return "q"


def _apply_editor_edits(edited_text, items_by_id, final_dates) -> None:
    """Merge the edited plan text into final_dates, warning about bad input."""
    edits, warnings = planner.parse_editor_text(edited_text)
    for warning in warnings:
        print(f"  WARNING: {warning}", file=sys.stderr)
    for tid in list(edits):
        if tid not in items_by_id:
            print(f"  WARNING: unknown task id '{tid}' in edited plan, ignoring", file=sys.stderr)
            edits.pop(tid)
    for tid, new_date in edits.items():
        if new_date is None:
            final_dates.pop(tid, None)  # "-": leave this task's date unchanged
        else:
            final_dates[tid] = new_date


def _approve_plan(args, interactive, items, items_by_id, result, buckets, capacity, today):
    """Show the schedule until the user approves it; return the final dates."""
    earliest = planner.earliest_scheduled_dates(result, buckets)
    final_dates = dict(earliest)
    while True:
        # Re-applied every pass, not once up front: the editor branch pops a
        # date for every `-`, and an overdue task left behind that way still
        # needs parking. A parked task shows its old -> new arrow under "At
        # risk" but not under the parking day itself — it has no chunk there,
        # since it was re-dated rather than scheduled.
        final_dates = planner.park_overdue_dates(final_dates, items, today, buckets[-1])

        _print_schedule(result, buckets, earliest, final_dates, today, color=interactive)

        choice = _read_choice(args.yes)
        if choice in ("q", "quit"):
            print("Cancelled.", file=sys.stderr)
            sys.exit(1)
        if choice in ("", "y", "yes"):
            return final_dates
        if choice in ("e", "edit"):
            edited_text = tracker.open_in_editor(planner.render_editor_text(result, buckets, capacity))
            _apply_editor_edits(edited_text, items_by_id, final_dates)
            continue
        print("Please answer y, e, or q.")


def _print_dry_run(pending_label_changes) -> None:
    print("\nDry run — nothing written.")
    if pending_label_changes:
        print("\nLabel changes that would be made:")
        for item, _new_labels, action in pending_label_changes:
            verb = "Add" if action == "add" else "Remove"
            print(f"  {verb} '{_AT_RISK_LABEL}': {item.content}")


def _fetch_plan_inputs(client):
    try:
        with tracker.spinner("Fetching tasks"):
            return client.get_all_open_tasks(), client.get_projects()
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_plan(args: argparse.Namespace) -> None:
    config = _load_plan_config(args)
    account = _resolve_account(config.account)
    interactive = _plan_preflight(args, account)

    now = datetime.now(config.timezone)
    today = now.date()
    buckets = planner.weekday_buckets(today, config.horizon_days)
    if not buckets:
        print("ERROR: no weekdays in the planning horizon.", file=sys.stderr)
        sys.exit(1)

    window_start = datetime.combine(buckets[0], time.min, config.timezone)
    window_end = datetime.combine(buckets[-1] + timedelta(days=1), time.min, config.timezone)
    busy = _read_busy(account, config, window_start, window_end)

    capacity = planner.compute_capacity(buckets, busy, config, now)
    capacity = _confirm_capacity(capacity, buckets, auto_accept=args.yes)
    if capacity is None:
        print("Cancelled.", file=sys.stderr)
        sys.exit(1)

    client = _get_client()
    tasks, projects = _fetch_plan_inputs(client)
    task_labels_by_id = {t["id"]: list(t.get("labels", [])) for t in tasks}

    inbox_id = next((p["id"] for p in projects if p.get("inbox_project")), None)
    items = planner.build_items(tasks, inbox_id, today, config)
    items_by_id = {item.id: item for item in items}

    result = planner.pack(items, capacity)
    at_risk_ids = {item.id for item in result.at_risk}
    pending_label_changes = _pending_label_changes(items, at_risk_ids, task_labels_by_id)

    final_dates = _approve_plan(
        args, interactive, items, items_by_id, result, buckets, capacity, today
    )

    if args.dry_run:
        _print_dry_run(pending_label_changes)
        return

    log_path = _write_plan_log(final_dates, items_by_id, pending_label_changes, task_labels_by_id)
    print(f"\nPlan recorded to {log_path}")

    updated, skipped, labels_changed, failed, attempted = _apply_plan(
        client, items, final_dates, pending_label_changes
    )
    print(f"Updated {updated} task(s); {skipped} unchanged. Labels changed on {labels_changed} task(s).")
    _exit_if_failed(failed, attempted)


def _apply_plan(client, items, final_dates, pending_label_changes):
    """Apply date and label changes, continuing past failures.

    Returns (updated, skipped, labels_changed, failed, attempted).
    """
    updated = skipped = labels_changed = failed = attempted = 0
    for item in items:
        new_date = final_dates.get(item.id)
        # Pinned (timed or @fixed) tasks are never rewritten, and a recurring
        # one only when its rule can't drift (see planner.recurrence_writable).
        if not item.date_writable or new_date is None or new_date == item.due_date:
            skipped += 1
            continue
        attempted += 1
        try:
            if item.recurrence is None:
                client.update_task(item.id, due_date=new_date.isoformat())
            else:
                # update_task's due_date would drop the recurrence.
                client.reschedule_recurring(item.id, new_date.isoformat(), item.recurrence)
            updated += 1
        except TodoistAPIError as exc:
            failed += 1
            print(f"  WARNING: could not update {item.id}: {exc}", file=sys.stderr)

    for item, new_labels, _action in pending_label_changes:
        attempted += 1
        try:
            client.update_task(item.id, labels=new_labels)
            labels_changed += 1
        except TodoistAPIError as exc:
            failed += 1
            print(f"  WARNING: could not update labels for {item.id}: {exc}", file=sys.stderr)
    return updated, skipped, labels_changed, failed, attempted


def cmd_close(args: argparse.Namespace) -> None:
    client = _get_client()
    try:
        client.close_task(args.task_id)
    except TodoistAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Closed task: {args.task_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="todoist",
        description="Manage Todoist tasks from the command line",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- create ---
    p_create = subparsers.add_parser("create", help="Create a new task")
    p_create.add_argument("content", help="Task title")
    p_create.add_argument("--description", "-d", help="Task description/notes")
    p_create.add_argument("--project-id", "-p", dest="project_id", help="Todoist project ID")
    p_create.add_argument(
        "--labels", "-l",
        help="Comma-separated label names, e.g. work,urgent",
    )
    p_create.add_argument(
        "--due",
        help='When you plan to do the work, e.g. "tomorrow", "next Monday"',
    )
    p_create.add_argument(
        "--deadline",
        help="When the task must be done by, as YYYY-MM-DD (no natural language)",
    )
    p_create.add_argument(
        "--priority",
        type=int,
        choices=[1, 2, 3, 4],
        help="Priority: 1=highest/urgent, 4=lowest/normal",
    )

    # --- list ---
    subparsers.add_parser(
        "list",
        help="List all open tasks with priority and deadline",
    )

    # --- update ---
    p_update = subparsers.add_parser("update", help="Update fields on an existing task")
    p_update.add_argument("task_id", help="Todoist task ID")
    p_update.add_argument("--content", "-c", help="New task title")
    p_update.add_argument("--description", "-d", help="New description/notes")
    p_update.add_argument(
        "--labels", "-l",
        help="Comma-separated label names (replaces existing labels)",
    )
    p_update.add_argument(
        "--due",
        help='When you plan to do the work, e.g. "Jun 4 2026", "tomorrow"',
    )
    p_update.add_argument(
        "--deadline",
        help="When the task must be done by, as YYYY-MM-DD (no natural language)",
    )
    p_update.add_argument(
        "--priority",
        type=int,
        choices=[1, 2, 3, 4],
        help="Priority: 1=highest/urgent, 4=lowest/normal",
    )

    # --- search ---
    p_search = subparsers.add_parser("search", help="Search open tasks by label")
    p_search.add_argument("--label", "-l", required=True, help="Label name to filter by")
    p_search.add_argument(
        "--attachments", "-a",
        action="store_true",
        help="Fetch and print the content of any text-based attachments on each task",
    )
    p_search.add_argument(
        "--json",
        action="store_true",
        help="Output raw task objects as JSON (empty result prints [])",
    )

    # --- filter ---
    p_filter = subparsers.add_parser(
        "filter", help='List open tasks matching a Todoist filter, e.g. "today | overdue"',
    )
    p_filter.add_argument("query", help="Todoist filter query")
    p_filter.add_argument(
        "--comments", action="store_true", help="Also fetch and print each task's comments",
    )
    p_filter.add_argument(
        "--json", action="store_true", help="Output raw task objects as JSON (empty prints [])",
    )

    # --- comment ---
    p_comment = subparsers.add_parser("comment", help="Add a comment to a task")
    p_comment.add_argument("task_id", help="Todoist task ID")
    p_comment.add_argument("text", help="Comment text (markdown)")

    # --- close ---
    p_close = subparsers.add_parser("close", help="Close (complete) a task by ID")
    p_close.add_argument("task_id", help="Todoist task ID")

    # --- plan ---
    p_plan = subparsers.add_parser(
        "plan", help="Build a capacity-based schedule for the next week from Google Calendar + Todoist"
    )
    p_plan.add_argument(
        "--config", help=f"Path to plan.toml (default: {_PLAN_CONFIG_FILE})"
    )
    p_plan.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Compute and display the plan without writing any dates",
    )
    p_plan.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Auto-approve the computed plan without prompting (required when not run in a terminal)",
    )

    # --- start ---
    p_start = subparsers.add_parser(
        "start", help="Track time on a task and log it to Google Calendar"
    )
    p_start.add_argument(
        "term",
        nargs="?",
        help="Search phrase matched against task content (quote multi-word phrases, "
        'e.g. "review the PR"); omit for the priority lookahead window '
        "(P1 due/deadline within 3 days, P2 within 2, others today, plus overdue)",
    )

    # --- log ---
    p_log = subparsers.add_parser(
        "log", help="Track time with no backing task and log it to Google Calendar"
    )
    p_log.add_argument(
        "title", nargs="+", help="Calendar event title, e.g. todoist log client call"
    )
    p_log.add_argument("--description", "-d", help="Calendar event description")

    args = parser.parse_args()

    # Looked up at call time so tests that patch a cmd_* function still take effect.
    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "update": cmd_update,
        "search": cmd_search,
        "filter": cmd_filter,
        "comment": cmd_comment,
        "close": cmd_close,
        "plan": cmd_plan,
        "start": cmd_start,
        "log": cmd_log,
    }
    try:
        commands[args.command](args)
    except KeyboardInterrupt:
        # Outer safety net: catches Ctrl-C anywhere that isn't already handled
        # locally (e.g. during a network call), so it prints a clean message
        # instead of dumping a traceback. _track_and_log still handles Ctrl-C during
        # the timer phase itself, to avoid logging an aborted session.
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
