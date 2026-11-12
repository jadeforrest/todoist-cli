# todoist-cli

Todoist from the terminal, built around three commands:

- **`todoist plan`** fits the next week of Todoist work into the free time on your Google Calendar, shows you the schedule, and writes the due dates back once you approve.
- **`todoist start`** picks a task, runs a timer, and logs the session to Google Calendar as an event.
- **`todoist log`** does the same for work that has no Todoist task.

It also has the everyday commands: `create`, `list`, `update`, `search`, `filter`, `comment` and `close`.

`plan` is only as good as the information in your tasks. Read [Todoist conventions](#todoist-conventions) before your first run: estimates, due dates versus deadlines, `@fixed`, and recurring rules all change what it does.

## Requirements

| What | Needed for | Notes |
|---|---|---|
| Python 3.11+ | everything | Tested on 3.11, 3.12 and 3.14. |
| macOS or Linux | `start`, `log` | The timer and the arrow-key picker use POSIX terminal APIs. Windows isn't supported. |
| A Todoist account and API token | all commands except `log` | Uses Todoist API v1. |
| [`gog`](#install), signed in to a Google account with Calendar access | `plan`, `start`, `log` | Reads free/busy and creates events. Developed against gog 0.12. |
| `$VISUAL` or `$EDITOR` | `plan`'s `e` option | Falls back to `vi`. |

Python packages, installed automatically:

| Package | Why |
|---|---|
| [`requests`](https://pypi.org/project/requests/) ≥ 2.31 | Todoist API calls |
| [`urllib3`](https://pypi.org/project/urllib3/) ≥ 1.26 | Retries on transient API errors (already a `requests` dependency, declared because it's imported directly) |
| [`python-dotenv`](https://pypi.org/project/python-dotenv/) ≥ 1.0 | Reading `~/.config/todoist-cli/.env` |

Development only (`pip install -e '.[dev]'`): `pytest` and `hypothesis`.

## Install

```bash
pipx install git+https://github.com/jadeforrest/todoist-cli.git
```

That installs `todoist`, plus `gog-reauth`, a helper that re-authorizes your Google account. From a clone, `pipx install .` (or `pip install .` inside a virtualenv) works too.

`plan`, `start` and `log` talk to Google Calendar through `gog`, a Google Workspace CLI. The other commands only need Todoist.

```bash
brew install gog
gog auth add you@example.com --services calendar
```

## Setup

1. **Todoist token.** Copy your API token from Todoist → Settings → Integrations → Developer. Then export it, or put it in `~/.config/todoist-cli/.env`:

   ```bash
   mkdir -p ~/.config/todoist-cli
   cp .env.example ~/.config/todoist-cli/.env   # then edit it
   ```

   A variable already set in your shell takes precedence over the file.

2. **Google account.** Set it with `GOG_ACCOUNT` (environment or `.env`), or with `account` in `plan.toml`. `start` and `log` use `GOG_ACCOUNT` first, then `plan.toml`. `plan` uses the `account` from the config file it loaded first, then `GOG_ACCOUNT`.

3. **Planner config (optional).** Defaults: 9:00–17:00 on weekdays, a 7-day horizon, your machine's time zone, and the `primary` calendar.

   ```bash
   cp plan.example.toml ~/.config/todoist-cli/plan.toml
   ```

The config directory follows `$XDG_CONFIG_HOME`; set `TODOIST_CLI_CONFIG_DIR` to put it somewhere else. Plan logs go to `~/.local/state/todoist-cli/plan_logs/`, which follows `$XDG_STATE_HOME`.

## `todoist plan`

```bash
todoist plan            # interactive: review, edit, approve
todoist plan --dry-run  # show the plan, write nothing
todoist plan --yes      # approve without prompting (needed when not in a terminal)
todoist plan --config path/to/plan.toml
```

What happens on a run:

1. **Capacity.** For each weekday from today through `horizon_days`, it takes your working hours and subtracts busy time on the configured calendars. Today's window starts at the current time, and days listed in `days_off` get zero. You see the table and can accept it, adjust it (`07-28=1h30m 07-31=0`), or quit.
2. **The pool.** It fetches every open task and keeps the ones the plan should consider (see [Which tasks get planned](#which-tasks-get-planned)), each sized by its [estimate](#estimates).
3. **Packing.** Pinned tasks claim time on their own day first. Everything else fills the remaining capacity in three passes:
   - **Deadlines inside the horizon**, highest priority first. Each one aims for the day before its deadline, or its due date if that's earlier, and spills into *earlier* days when that day is full.
   - **Due dates**, plus deadlines past the horizon. Each one aims for its due date and spills into *later* days first.
   - **Undated Inbox tasks**, which fill whatever is left.

   Tasks split across days by the minute, so a 3-hour task can be 1h on Tuesday and 2h on Wednesday. Its new due date is the first day it lands on.
4. **Review.** The schedule is printed day by day:

   ```
   Tuesday, July 28:
      P1 Draft board update 2h (deadline Jul 30) (Jul 27 -> Jul 28)
      P3 Renew passport 45m (Jul 28)
      P2 Weekly review 30m (Jul 28) (recurring)
   ```

   `(old -> new)` means the plan is moving the task. The markers mean:
   - `(pinned)`: the task isn't moved.
   - `(recurring)`: one occurrence moves.
   - `(recurring, plan only)`: it moves in this plan, but its date in Todoist is left alone.

   Below the days, three more sections may appear:
   - **At risk**: tasks that don't fit before their date, or that finish after their deadline.
   - **Not planned**: pinned or recurring work on a weekend or past the horizon, with the reason.
   - **Not scheduled**: a count of undated or far-off work that didn't fit.
5. **Approve, edit or quit.** `e` opens the plan in `$VISUAL`/`$EDITOR` as a tab-separated list with one task per line. Change the date column to move a task, or set it to `-` to leave the task's date alone.
6. **Apply.** Due dates are written back and the `atrisk` label is synced: added to tasks at risk now, removed from tasks that no longer are. Before anything is written, every change and its old value go to a log file in `plan_logs/`, so a run can be undone by hand. If an update fails, `plan` reports it and exits non-zero.

`plan` never writes deadlines, and never rewrites a pinned task. A due date left in the past is moved to the last day of the horizon: a due date says when you intend to do something, and yesterday isn't a plan. Missed deadlines are left as they are.

## `todoist start`

```bash
todoist start "board update"   # tasks whose title contains the phrase
todoist start                  # what's urgent right now
```

With a phrase, `start` searches your open tasks for it. With no phrase, it shows a priority lookahead: P1 tasks due or deadlined within 3 days, P2 within 2 days, everything else today, plus anything overdue. Results are sorted by the earlier of due date and deadline, then by priority.

Pick a task with the arrow keys, or skip the picker if there's only one match. The timer runs until you press Return. You then choose an event color, and the session is written to Google Calendar:
- **Title:** the task title.
- **Description:** a link to the task.
- **Availability:** **free**, so tracked time never blocks capacity in a later `plan`.

`start` checks calendar access before the timer starts. If the token expires while the timer is running, it offers to re-authorize and write the event anyway. If that fails, it prints the exact `gog` command to recreate the event, so the session isn't lost. Ctrl-C while the timer is running aborts without writing anything.

## `todoist log`

```bash
todoist log client call
todoist log "Acme call" -d "intro with their CTO"
```

`log` works like `start` for time that has no Todoist task behind it, and never touches Todoist. Quotes around the title are optional. `-d` sets the event description.

## Todoist conventions

### Estimates

Every task in the plan needs a size. It's taken from the first of these that applies:

1. **Todoist's own duration field**, if set.
2. **A hint in the title:** a number glued to `h` or `m`, such as `Write report 2h`, `Call the dentist 15m`, `Prep slides 1h30m`, `Review 1.5h` or `Inbox zero [45m]`.
   - There's no space between the number and the unit.
   - If a title has several separate hints, the last one wins: `5m walk, 2h drive` is 2h.
   - Hints glued to letters are ignored, so `v2h` and `Q3h1` don't count.
   - The hint stays in the title.
3. **`default_task_minutes`** from `plan.toml`, which is 30 by default.

A parent task with open subtasks counts as **0 minutes**, because its subtasks carry the work. Give the parent its own estimate only if it has work of its own beyond the subtasks.

### Due date vs. deadline

The two date fields mean different things, and `plan` treats them differently:

- **Due date**: when you intend to work on the task. It's a soft intention, and `plan` moves it freely: later first, earlier only if everything later is full.
- **Deadline**: when the task has to be finished. It's a commitment, and `plan` schedules backwards from it, aiming to be done the day before. `plan` never changes deadlines.

Put a committed or externally set date in the deadline, and set the due date to when you'll actually start. If a task has both, `plan` treats the days between them as slack: it tries the due date first, then the following days up to the day before the deadline. It keeps the deadline day itself in reserve.

The CLI follows the same split:
- `--due` accepts natural language: "tomorrow", "next Monday".
- `--deadline` takes only `YYYY-MM-DD`, because Todoist has no natural-language form for deadlines.

```bash
todoist create "Ship board deck" --due tomorrow --deadline 2026-08-17
```

### Priority

The CLI uses Todoist's labels: **P1** is the most urgent and **P4** is the default. Within each `plan` pass, a date makes a task more urgent:
- A deadline counts as two levels higher.
- A due date counts as one level higher.

So a P3 with a deadline ranks with a bare P1.

### Which tasks get planned

A task is in the plan if it's open **and** either:
- it has a due date or a deadline, or
- it's in your **Inbox**, dated or not.

Undated tasks in other projects are ignored, so keep someday/maybe work out of the Inbox. A task whose dates are all more than `eligibility_days` (default 14) away is left out. Overdue work is always included.

### Pinning: times of day and `@fixed`

A task is **pinned** if its due date has a **time of day**, or if it carries the **`@fixed`** label:
- It takes its time on its own day, and `plan` never moves or rewrites it.
- An overdue pinned task takes its time today, but its date isn't changed.

Use `@fixed` for date-only tasks that have to happen on a particular day.

### Recurring tasks

Recurring tasks with no time of day can move, but **only later**, never earlier. A recurring task scheduled on a weekend or past the horizon is reported under "Not planned" instead of being pulled into the week. Within its priority, a recurring task ranks just above one-off tasks.

Whether a move is written back to Todoist depends on the rule:

| Rule | Example | Move written back? |
|---|---|---|
| Counts from completion | `every! 3 days` | Yes |
| Daily | `every day` | Yes |
| Anchored to the calendar | `every mon`, `every 15th`, `every weekday` | Yes |
| Interval from the due date | `every 3 days`, `every week`, `every other fri` | No: plan only (`(recurring, plan only)`) |

Writing back an interval rule would shift every later occurrence. [docs/recurring-tasks.md](docs/recurring-tasks.md) covers the Todoist API behavior behind this. To keep a recurring task on its day, give it a time or `@fixed`.

### Labels `plan` writes

- **`atrisk`**: added to tasks the current plan can't finish in time, and removed once they're back on track. Filter on it in Todoist (`@atrisk`) to see what needs a decision.

### Calendar

- **Capacity**: busy time on the calendars in `calendars` comes out of your working hours.
- **All-day events**: usually marked free, so they don't block time. List vacation days in `days_off`.
- **Sessions from `start` and `log`**: written as free events, so they record the time you spent without blocking future capacity.

## Other commands

| Command | What it does |
|---|---|
| `todoist create "Title" [--due D] [--deadline YYYY-MM-DD] [--priority 1-4] [--labels a,b] [-d text] [-p PROJECT_ID]` | Create a task |
| `todoist list` | All open tasks, highest priority first |
| `todoist update TASK_ID [--content T] [--due D] [--deadline YYYY-MM-DD] [--priority N] [--labels a,b] [-d text]` | Change a task (`--labels` replaces the existing set) |
| `todoist search --label NAME [--attachments] [--json]` | Open tasks with a label, optionally printing text attachments |
| `todoist filter "today \| overdue" [--comments] [--json]` | Open tasks matching any Todoist filter query |
| `todoist comment TASK_ID "text"` | Add a Markdown comment to a task |
| `todoist close TASK_ID` | Complete a task |

`--json` prints the raw task objects, for scripting.

## Google re-authorization

When `gog`'s token expires, `start`, `log` and `plan` offer to run `gog-reauth <account>`:
- It re-runs Google's consent screen, keeping the scopes the token already had.
- It then checks that calendar access works.

The offer only appears in an interactive terminal. A cron or launchd run prints the instructions and exits.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python tools/mutate.py          # targeted mutation tests; see tools/mutants.toml
```

The tests mock all network and `gog` calls, and run against a scratch config directory, never your real one.

## License

MIT. See [LICENSE](LICENSE).
