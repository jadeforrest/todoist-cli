# Todone

A Mac app for working Todoist's today view and the weekly planner, built on the
same logic as the `todoist` CLI.

## Architecture

- **SwiftUI app** (`todone/`, a SwiftPM package, bundled by `build-app.sh` into an
  ad-hoc-signed `Todone.app`; no developer account needed for local use).
- **Python helper** (`todoist_cli/todone_server.py`) started by the app as
  `.venv/bin/python -m todoist_cli.todone_server`, using the venv of the repo
  checkout the app was built from. One JSON request per line on stdin, one
  response per line on stdout. All the logic lives in `todone_core.py`, which
  reuses `planner.py` (sizing, capacity, packing, at-risk, recurrence rules),
  `client.py` and `tracker.py` (calendar busy times and event writes).
  The CLI and the app compute everything the same way because they call the
  same functions.
- `--fake FIXTURE.json` runs the helper against an in-memory Todoist and
  calendar, so the app can be developed and demoed with no production writes.

## Today view (default)

- Tasks with a due date of today or earlier, in **Overdue** (hidden when empty)
  and **Today**, sorted by due date, then priority (P1 first), then title.
- Header: time left today (`planner.compute_capacity` for today, or 0 on a day
  that isn't a planned weekday) and time scheduled (sum of planner sizes of the
  overdue and today tasks).
- Calendar busy times are cached 15 minutes; capacity is recomputed against the
  current time on every refresh. Tasks reload after every mutation, every 15
  minutes, and on the refresh button (which also refetches the calendar).
- Each row: title, priority (colored flag + leading bar), deadline, labels, and
  icons for overdue, at risk (the planner's own `PlanResult.at_risk` over the
  horizon), pinned (time of day or `@fixed`), and "can't be moved" (a recurring
  rule that would drift — `planner.recurrence_writable`).
- Complete sits alone at the card's left edge, away from the other row buttons: start/stop timer, toggle `@fixed`, details, and a link that opens the task in Todoist.
- Badges on a folded card: open subtasks (count of all levels), comments, and a description.

## Unfolding and editing in place

- Clicking a card (or Return on the selected one) unfolds it: the description
  (inline Markdown), every open subtask nested all the way down (each can be
  completed, retitled, or opened in Todoist), and its comments (fetched on
  unfold), with a field to add one. Click again or Return to fold.
- On an unfolded card, clicking the title edits it. Todoist's quick-add syntax
  is read as you type and shown as chips: `p1`–`p4` priority, `@label` (added
  to the task's labels), `#Project` (longest project name; spaces optional),
  `{deadline}` (a date, `{}` clears it), and a due phrase (`tomorrow 3pm`,
  `next fri`, `nov 20`, `in 3 days`, `every mon`). The due phrase goes to
  Todoist as `due_string`, so Todoist decides what it means; click its chip to
  keep it as title text. Tokens already in the old title stay text, so editing
  "Call Tom" doesn't make "Tom" a date. Return saves, Esc cancels.
- Written with the helper's `quick_update`, which returns a snapshot.

## Optimistic changes

- Completing, priority, title edits and `@fixed` show on the card at once:
  the check fills and bounces, the card fades and slides out, and a spinner
  marks anything still on its way to Todoist. If the call fails, the card goes
  back to how Todoist has it and the error shows in the banner.

## Keyboard

- `j`/`k` (or ↓/↑) select cards in reading order (Inbox, overdue, today, each
  day, the tray), scrolling them into view; Return folds/unfolds; `e` edits the
  title; `1`–`4` set the priority; Esc folds, then clears the selection. Keys
  pass through while typing, in sheets and popovers, and with ⌘/⌃/⌥.

## Timers

- **Log** (header) times work with no task, like `todoist log`: type what it is
  (and optional notes for the event), then stop it from its chip in the header.
- Several can run at once. Stop asks for a Google Calendar event color, then
  writes the event like `todoist start` does. Completing a running task stops it
  first. Each running timer shows in the header with a stop button; right-click
  to discard without logging. Timers live in the helper's memory; quitting mid-timer loses them.

## Planner mode

- When the planner is wider than the window, the whole board (Inbox, Today,
  the day columns and the tray) scrolls sideways.
- The Planner toggle also resizes the window: wide enough for the whole
  planner when it opens, back to the today column's width when it closes,
  keeping the window on screen.
- Toggle opens one column per planned weekday after today, out to
  `horizon_days`, plus an **Unplanned** tray (eligible tasks with no date on a
  shown column: undated Inbox work, deadline-only tasks, weekend dates). The
  today column stays as it is.
- An **Inbox** panel left of Today lists every open task in the Inbox project
  itself (not in one of its sections), whatever its date. Those tasks aren't
  repeated in the tray; a dated one still shows on its day too.
- Planned time splits a task too big for its day: it fills that day's free
  time and the rest moves to the following days (the card says "through …").
  Pinned tasks stay whole on their day. While planning, a task still on its
  proposed day shows the planner's own split.
- Each column header shows free time and planned time. Clicking it edits that
  day's capacity (`1h30m`, `0`, empty to reset). Overrides last until the app
  quits.
- **Plan** runs `planner.pack` and shows the proposal (the CLI's final dates,
  including parking work left on today or earlier that found no room: on the
  last day if it has a deadline, past the horizon if not). The window is tinted and a
  "Planning" banner shows until Confirm or Cancel; moved tasks show old → new.
- Confirm writes a plan log (same format as the CLI), applies dates and the
  `atrisk` label changes. Dates are applied only to tasks that are still
  writable when re-fetched at confirm time.

## Moving tasks

- Drag between columns (or onto Today). Outside planning it writes the due date
  immediately; while planning it edits the proposal only.
- Pinned tasks and recurring tasks whose rule would drift can't be dragged and
  show a lock. Movable recurring tasks move one occurrence via the Sync API.
- Dragging within a column does nothing. The tray is a drag source only.

## Details

- Editable: title, description, priority, due (natural language, e.g. `every
  mon`), deadline (date or none), labels, size, project/section. Size writes the
  native duration if the task has one, otherwise a title hint (`[1h30m]`).
- Comments: list, add, edit. Attachments are listed with links; not editable.
- Laid out as cards on the window background (Description, Schedule,
  Organize, Comments) under a header band tinted with the priority color,
  with boxed fields, flag buttons for priority and filled/outlined label chips.

## Appearance

- **View → Text Size**: Small, Default, Large, Larger, Largest (⌘+ / ⌘− / ⌘0).
  Scales every font, the column and panel widths, the window widths and the
  details sheet; controls go a size up at Larger and Largest. Remembered between
  launches (`textSize` default; `-textSize largest` on the command line overrides).
- App icon: a white check on a green rounded square, drawn by `make-icon.swift`
  at build time.

## Later

- Weekend columns. Attachment upload. Persisting timers across restarts.

## Running it

```bash
todone/build-app.sh                      # builds todone/Todone.app
open -n todone/Todone.app --args --fake --planner   # sample data, planner open, no writes
open todone/Todone.app                   # real data (same config as the CLI: ~/.config/todoist-cli; gog for the calendar)
```

Shortcuts: j/k select, Return unfold, e edit title, 1–4 priority, Esc back out, ⌘R refresh, ⌘P planner, ⇧⌘P plan, ⌘↩ confirm plan, ⌘+/⌘−/⌘0 text size.
