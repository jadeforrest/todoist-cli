# Rescheduling recurring tasks through the Todoist API

Observed against API v1 on 2026-10-26 with throwaway test tasks (date-only
rules, no time of day). Re-check if Todoist changes the API.

## Moving one occurrence

| Call | Result |
|---|---|
| REST `POST /tasks/{id}` with `due_date` | Date moves, **recurrence is dropped** (`is_recurring: false`, `string` becomes the date) |
| REST `POST /tasks/{id}` with `due_string` set to a date | Same: **recurrence dropped** |
| REST `POST /tasks/{id}` with a `due` object | Rejected (400, "At least one of supported fields should be set") |
| Sync `POST /sync`, `item_update` with `due: {date, string: <original rule>, is_recurring: true}` | Date moves, **rule kept** — this is the only way to move an occurrence |

Setting `due_string` back to the rule re-arms the recurrence, but Todoist picks
the next occurrence itself, so it can't be used to choose a date.

## What happens on completion after a move

Each task was moved from Mon 10-26 to Wed 10-28, then completed on 10-26.

| Rule | Next occurrence | Meaning |
|---|---|---|
| `every mon` | Mon 11-02 | Anchored rules (weekdays, day of month) snap back to the pattern. A move is harmless, but moving onto or past the next occurrence merges the two. |
| `every 3 days` | 10-31 (moved date + 3) | Interval rules count from the current due date, so a move **permanently shifts the cadence**. |
| `every! 3 days` | 10-29 (completion date + 3) | `every!` counts from completion, so the stored date doesn't affect later occurrences. |

Control: `every 3 days` left at 10-24 and completed late on 10-26 became 10-27.
Leaving an interval rule's date alone keeps its cadence even when the work is
done late.

## How `todoist plan` uses this

- Date-only recurring tasks are movable unless labelled `@fixed`. Tasks with a
  time of day are always pinned.
- A move is written back (via Sync `item_update`) only when the rule can't
  drift: `every!` rules, daily rules, and rules anchored to a weekday or date.
  Other `every` rules (`every 3 days`, `every week`, `every other mon`, …) are
  moved in the plan only, and their Todoist date is left as it is.
- Recurring tasks only move later, never earlier: moving an anchored `every`
  occurrence earlier would add an extra occurrence.
