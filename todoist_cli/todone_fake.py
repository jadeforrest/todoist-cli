"""In-memory Todoist and calendar for running Todone without touching real data.

`todone_server.py --fake` uses this. The sample tasks are dated relative to
today so every section of the app (overdue, today, later days, the tray, at-risk,
pinned and locked tasks) has something in it whenever it's started.
"""

from __future__ import annotations

import copy
import itertools
import sys
from datetime import date, datetime, time, timedelta

from .client import TodoistAPIError


class FakeClient:
    """Implements the TodoistClient methods Todone calls, over in-memory dicts."""

    def __init__(self, tasks: list[dict], projects: list[dict], sections: list[dict], labels: list[str]):
        self.tasks = {t["id"]: t for t in tasks}
        self.projects = projects
        self.sections = sections
        self.labels = [{"id": f"l{i}", "name": n} for i, n in enumerate(labels)]
        self.comments: dict[str, list[dict]] = {}
        self.closed: list[str] = []
        self._ids = itertools.count(1)

    def _get_task(self, task_id: str) -> dict:
        if task_id not in self.tasks:
            raise TodoistAPIError(f"Todoist API error 404 on GET /tasks/{task_id}: not found")
        return self.tasks[task_id]

    def get_all_open_tasks(self) -> list[dict]:
        return copy.deepcopy(list(self.tasks.values()))

    def get_projects(self) -> list[dict]:
        return copy.deepcopy(self.projects)

    def get_sections(self, project_id=None) -> list[dict]:
        return [copy.deepcopy(s) for s in self.sections if project_id in (None, s["project_id"])]

    def get_labels(self) -> list[dict]:
        return copy.deepcopy(self.labels)

    def get_task(self, task_id: str) -> dict:
        return copy.deepcopy(self._get_task(task_id))

    def get_task_comments(self, task_id: str) -> list[dict]:
        return copy.deepcopy(self.comments.get(task_id, []))

    def add_comment(self, task_id: str, content: str) -> dict:
        comment = {"id": f"c{next(self._ids)}", "task_id": task_id, "content": content,
                   "posted_at": datetime.now().astimezone().isoformat()}
        self.comments.setdefault(task_id, []).append(comment)
        task = self.tasks.get(task_id)
        if task is not None:
            task["note_count"] = len(self.comments[task_id])
        return copy.deepcopy(comment)

    def update_comment(self, comment_id: str, content: str) -> dict:
        for comments in self.comments.values():
            for c in comments:
                if c["id"] == comment_id:
                    c["content"] = content
                    return copy.deepcopy(c)
        raise TodoistAPIError(f"Todoist API error 404 on POST /comments/{comment_id}: not found")

    def close_task(self, task_id: str) -> None:
        self._get_task(task_id)
        self.closed.append(task_id)
        del self.tasks[task_id]

    def move_task(self, task_id: str, *, project_id=None, section_id=None, parent_id=None) -> dict:
        task = self._get_task(task_id)
        if section_id is not None:
            section = next(s for s in self.sections if s["id"] == section_id)
            task["section_id"], task["project_id"] = section_id, section["project_id"]
        elif project_id is not None:
            task["project_id"], task["section_id"] = project_id, None
        return {}

    def update_task(self, task_id: str, **fields) -> dict:
        task = self._get_task(task_id)
        for key in ("content", "description", "labels", "priority"):
            if fields.get(key) is not None:
                task[key] = fields[key]
        self._update_dates(task, fields)
        if fields.get("duration_minutes") is not None:
            task["duration"] = {"amount": fields["duration_minutes"], "unit": "minute"}
        return copy.deepcopy(task)

    @staticmethod
    def _update_dates(task: dict, fields: dict) -> None:
        if fields.get("due_date"):
            task["due"] = {"date": fields["due_date"], "string": fields["due_date"], "is_recurring": False}
        due_string = fields.get("due_string")
        if due_string == "no date":
            task["due"] = None
        elif due_string:
            task["due"] = _parse_due_string(due_string)
        if fields.get("deadline_date"):
            task["deadline"] = {"date": fields["deadline_date"]}
        if fields.get("clear_deadline"):
            task["deadline"] = None

    def reschedule_recurring(self, task_id: str, due_date: str, rule: str) -> None:
        self._get_task(task_id)["due"] = {"date": due_date, "string": rule, "is_recurring": True}


def _parse_due_string(text: str) -> dict:
    """The handful of phrases the fake understands; anything else becomes a recurring rule."""
    today = date.today()
    words = {"today": 0, "tod": 0, "tomorrow": 1, "tom": 1}
    if text.lower() in words:
        d = today + timedelta(days=words[text.lower()])
        return {"date": d.isoformat(), "string": text, "is_recurring": False}
    try:
        return {"date": date.fromisoformat(text).isoformat(), "string": text, "is_recurring": False}
    except ValueError:
        return {"date": today.isoformat(), "string": text, "is_recurring": text.lower().startswith("every")}


def sample_data(today: date) -> FakeClient:
    def d(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    def due(offset: int, **extra) -> dict:
        return {"date": d(offset), "string": d(offset), "is_recurring": False, **extra}

    projects = [
        {"id": "inbox", "name": "Inbox", "inbox_project": True},
        {"id": "work", "name": "Consulting"},
        {"id": "home", "name": "Home"},
    ]
    sections = [
        {"id": "s-clients", "project_id": "work", "name": "Clients"},
        {"id": "s-admin", "project_id": "work", "name": "Admin"},
    ]
    tasks = [
        {"id": "t1", "content": "Send invoice to Acme [30m]", "priority": 4, "project_id": "work",
         "due": due(-2), "labels": ["admin"]},
        {"id": "t2", "content": "Review hiring plan draft 1h", "priority": 3, "project_id": "work",
         "due": due(-1), "deadline": {"date": d(1)}, "labels": ["client"]},
        {"id": "t3", "content": "Write blog post outline 2h", "priority": 2, "project_id": "work",
         "due": due(0), "labels": ["writing"], "description": "Topic: org design for 30-person teams.",
         "note_count": 2},
        {"id": "t4", "content": "Weekly review", "priority": 3, "project_id": "work",
         "due": {"date": d(0), "string": "every mon", "is_recurring": True}, "labels": []},
        {"id": "t5", "content": "Advisor call prep 45m", "priority": 4, "project_id": "work",
         "due": {"date": d(0), "datetime": f"{d(0)}T15:00:00", "string": "today 3pm", "is_recurring": False},
         "labels": ["client"]},
        {"id": "t6", "content": "Water plants", "priority": 1, "project_id": "home",
         "due": {"date": d(0), "string": "every 3 days", "is_recurring": True}, "labels": []},
        {"id": "t7", "content": "Draft proposal for Beta Corp 6h", "priority": 4, "project_id": "work",
         "section_id": "s-clients", "due": due(1), "deadline": {"date": d(2)}, "labels": ["client"]},
        {"id": "t8", "content": "Renew domain", "priority": 2, "project_id": "home", "due": due(2),
         "labels": ["fixed"]},
        {"id": "t9", "content": "Expense report 20m", "priority": 1, "project_id": "work",
         "section_id": "s-admin", "due": due(3), "labels": ["admin"]},
        {"id": "t10", "content": "Read 'Team Topologies' notes", "priority": 1, "project_id": "inbox",
         "labels": []},
        # Subtasks of the blog post, nested two deep, so the app has some to unfold.
        {"id": "t3a", "content": "Collect three examples", "priority": 3, "project_id": "work",
         "parent_id": "t3", "child_order": 1, "labels": []},
        {"id": "t3b", "content": "Sketch the structure", "priority": 1, "project_id": "work",
         "parent_id": "t3", "child_order": 2, "labels": ["writing"], "note_count": 1},
        {"id": "t3b1", "content": "Pick a title", "priority": 1, "project_id": "work",
         "parent_id": "t3b", "child_order": 1, "labels": []},
        {"id": "t11", "content": "Podcast episode edit 3h", "priority": 3, "project_id": "work",
         "deadline": {"date": d(5)}, "labels": ["podcast"]},
    ]
    client = FakeClient(tasks, projects, sections, ["admin", "client", "writing", "podcast", "fixed", "atrisk"])
    client.comments["t3"] = [
        {"id": "c-a", "task_id": "t3", "content": "Pull examples from past client work.",
         "posted_at": f"{d(-3)}T10:00:00Z"},
        {"id": "c-b", "task_id": "t3", "content": "Outline attached.", "posted_at": f"{d(-2)}T09:00:00Z",
         "attachment": {"file_name": "outline.md", "file_type": "text/markdown",
                        "file_url": "https://example.com/outline.md", "resource_type": "file"}},
    ]
    return client


def fake_busy(tz):
    """Two meetings on each weekday: 10:00-11:00 and 12:30-13:00."""

    def fetch(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        busy = []
        day = start.date()
        while datetime.combine(day, time.min, tz) < end:
            if day.weekday() < 5:
                busy.append((datetime.combine(day, time(10), tz), datetime.combine(day, time(11), tz)))
                busy.append((datetime.combine(day, time(12, 30), tz), datetime.combine(day, time(13), tz)))
            day += timedelta(days=1)
        return busy

    return fetch


def fake_write_event(title, start, end, description=None, color=None) -> tuple[bool, str]:
    print(f"[fake] calendar event {title!r} {start.isoformat()} -> {end.isoformat()} color={color}",
          file=sys.stderr)
    return True, "fake event"
