"""Thin HTTP client for the Todoist REST API v1. No business logic."""

import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# MIME type prefixes and file extensions treated as plain text.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst",
    ".csv", ".tsv",
    ".json", ".yaml", ".yml",
    ".log",
}


def is_text_attachment(attachment: dict) -> bool:
    """Return True if an attachment dict looks like a text-based file.

    Checks the file_type MIME type first, then falls back to the file
    extension in case the MIME type is missing or generic.
    """
    file_type = attachment.get("file_type", "")
    if any(file_type.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    ext = Path(attachment.get("file_name", "")).suffix.lower()
    return ext in _TEXT_EXTENSIONS


class TodoistAPIError(Exception):
    """Raised when the Todoist API returns a non-2xx response."""


# (connect timeout, read timeout) in seconds. Short connect so an unreachable
# API fails fast instead of hanging forever; longer read to tolerate
# slow-but-alive responses.
_DEFAULT_TIMEOUT = (5, 30)
# Attachments can be large; give downloads more room to finish reading.
_ATTACHMENT_TIMEOUT = (5, 120)


# Hosts that may receive the bearer token when fetching an attachment. Todoist
# serves uploaded files from files.todoist.com (which needs the token); every
# other host gets the request with no Authorization header at all.
_AUTH_ATTACHMENT_HOSTS = frozenset({"files.todoist.com", "api.todoist.com"})


def attachment_sends_auth(url: str) -> bool:
    """True only for https URLs on a Todoist-owned host that needs the token."""
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in _AUTH_ATTACHMENT_HOSTS


class TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter that applies a default timeout when the caller doesn't set one.

    ``requests`` has no timeout by default, so a stuck connection (e.g. a dead
    network) blocks forever. Mounting this adapter on a session makes every
    request through it fail fast unless a per-call timeout is explicitly given.
    """

    def __init__(self, *args, timeout=_DEFAULT_TIMEOUT, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


class TodoistClient:
    BASE_URL = "https://api.todoist.com/api/v1"

    def __init__(self, api_token: str, session: Optional[requests.Session] = None):
        self._token = api_token
        if session is None:
            session = requests.Session()
            # connect=1: don't multiply connect-timeout waits on a dead network.
            # Read/status retries are still useful for transient server errors.
            retry = Retry(
                total=3, connect=1, backoff_factor=2,
                status_forcelist=[500, 502, 503, 504],
            )
            adapter = TimeoutHTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
        self._session = session
        self._session.headers.update({"Authorization": f"Bearer {api_token}"})

    def _get(self, path: str, params: Optional[dict] = None) -> list | dict:
        try:
            resp = self._session.get(f"{self.BASE_URL}{path}", params=params)
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(f"Network error on GET {path}: {exc}") from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Todoist API error {resp.status_code} on GET {path}: {resp.text}"
            )
        return resp.json()

    def _post(self, path: str) -> None:
        try:
            resp = self._session.post(f"{self.BASE_URL}{path}")
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(f"Network error on POST {path}: {exc}") from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Todoist API error {resp.status_code} on POST {path}: {resp.text}"
            )

    def get_open_tasks_by_label(self, label: str) -> list[dict]:
        """Return all open (incomplete) tasks that have the given label.

        Todoist's /tasks endpoint accepts a label filter and returns only active
        (non-completed) tasks by default, but we also explicitly filter on
        is_completed to be safe. Follows pagination via next_cursor.
        """
        params: dict = {"label": label}
        tasks: list[dict] = []
        while True:
            data = self._get("/tasks", params=params)
            page = data["results"] if isinstance(data, dict) else data
            tasks.extend(t for t in page if not t.get("is_completed", False))
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break
            params = {"label": label, "cursor": next_cursor}
        return tasks

    def get_open_tasks_by_filter(self, query: str) -> list[dict]:
        """Return all open (incomplete) tasks matching a Todoist filter query.

        Hits the ``/tasks/filter`` endpoint, which evaluates Todoist filter
        expressions server-side (e.g. ``search: standup`` to match task content,
        or ``(due: today | deadline: today)`` for today's items). Follows
        pagination via next_cursor and applies the same is_completed safety
        filter as the other fetch methods.
        """
        params: dict = {"query": query}
        tasks: list[dict] = []
        while True:
            data = self._get("/tasks/filter", params=params)
            page = data["results"] if isinstance(data, dict) else data
            tasks.extend(t for t in page if not t.get("is_completed", False))
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break
            params = {"query": query, "cursor": next_cursor}
        return tasks

    def get_all_open_tasks(self) -> list[dict]:
        """Return all open (incomplete) tasks across every project.

        Mirrors :meth:`get_open_tasks_by_label` but applies no label filter,
        so it returns the full active task list. Follows pagination via
        next_cursor.
        """
        params: dict = {}
        tasks: list[dict] = []
        while True:
            data = self._get("/tasks", params=params)
            page = data["results"] if isinstance(data, dict) else data
            tasks.extend(t for t in page if not t.get("is_completed", False))
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break
            params = {"cursor": next_cursor}
        return tasks

    def get_projects(self) -> list[dict]:
        """Return all projects. Each has ``id``, ``name``, and ``inbox_project`` (bool).

        Follows pagination via next_cursor, mirroring the task fetch methods.
        """
        params: dict = {}
        projects: list[dict] = []
        while True:
            data = self._get("/projects", params=params)
            page = data["results"] if isinstance(data, dict) else data
            projects.extend(page)
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break
            params = {"cursor": next_cursor}
        return projects

    def get_sections(self, project_id: Optional[str] = None) -> list[dict]:
        """Return sections (board columns), optionally scoped to one project.

        Each section object has ``id``, ``project_id``, and ``name``. Follows
        pagination via next_cursor, mirroring the task fetch methods.
        """
        params: dict = {}
        if project_id is not None:
            params["project_id"] = project_id
        sections: list[dict] = []
        while True:
            data = self._get("/sections", params=params)
            page = data["results"] if isinstance(data, dict) else data
            sections.extend(page)
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not next_cursor:
                break
            params = dict(params)
            params["cursor"] = next_cursor
        return sections

    def move_task(
        self,
        task_id: str,
        *,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> dict:
        """Move a task to a different section, project, or parent.

        Exactly one of ``section_id``, ``project_id``, or ``parent_id`` should be
        given; moving to a section also moves the task into that section's
        project. Returns the parsed API response body, or ``{}`` if the response
        carries no JSON body.
        """
        payload: dict = {}
        if project_id is not None:
            payload["project_id"] = project_id
        if section_id is not None:
            payload["section_id"] = section_id
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if not payload:
            raise TodoistAPIError(
                "move_task requires one of project_id, section_id, or parent_id"
            )

        try:
            resp = self._session.post(
                f"{self.BASE_URL}/tasks/{task_id}/move", json=payload
            )
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(
                f"Network error on POST /tasks/{task_id}/move: {exc}"
            ) from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Todoist API error {resp.status_code} on POST /tasks/{task_id}/move: {resp.text}"
            )
        # The move endpoint's success body isn't used by callers, so tolerate a
        # missing or non-JSON body (e.g. 204 No Content) rather than letting a
        # JSONDecodeError escape as an uncaught, non-TodoistAPIError exception.
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def get_task_comments(self, task_id: str) -> list[dict]:
        """Return all comments on a task.

        Each comment may have an optional ``attachment`` dict with keys
        ``file_name``, ``file_type``, ``file_url``, and ``resource_type``.
        Use :func:`is_text_attachment` to filter to text-based files, then
        :meth:`fetch_attachment_text` to download the content.
        """
        data = self._get("/comments", params={"task_id": task_id})
        return data["results"] if isinstance(data, dict) else data

    def add_comment(self, task_id: str, content: str) -> dict:
        """Add a markdown comment to a task and return the created comment."""
        payload = {"task_id": task_id, "content": content}
        try:
            resp = self._session.post(f"{self.BASE_URL}/comments", json=payload)
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(f"Network error on POST /comments: {exc}") from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Todoist API error {resp.status_code} on POST /comments: {resp.text}"
            )
        return resp.json()

    def fetch_attachment_text(self, url: str) -> str:
        """Fetch and return the text content of an attachment URL.

        Only https URLs are fetched. The bearer token is sent only to
        Todoist-owned hosts (see ``_AUTH_ATTACHMENT_HOSTS``); for any other
        host the session's Authorization header is suppressed for this call.
        """
        if urlsplit(url).scheme != "https":
            raise TodoistAPIError(f"Refusing to fetch non-https attachment URL: {url}")
        kwargs: dict = {"timeout": _ATTACHMENT_TIMEOUT}
        if not attachment_sends_auth(url):
            # A None value removes the session-level header for this request.
            kwargs["headers"] = {"Authorization": None}
        try:
            resp = self._session.get(url, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(f"Network error fetching attachment: {exc}") from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Failed to fetch attachment ({resp.status_code}): {url}"
            )
        return resp.text

    def close_task(self, task_id: str) -> None:
        """Mark a task as complete (close it)."""
        self._post(f"/tasks/{task_id}/close")

    def create_task(
        self,
        content: str,
        *,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
        due_string: Optional[str] = None,
        deadline_date: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> dict:
        """Create a new task and return the created task object.

        Args:
            content: The task title (required).
            description: Optional longer description/notes.
            project_id: Todoist project ID to add the task to.
            labels: List of label names to apply.
            due_string: Human-readable due date, e.g. "tomorrow", "next Monday".
                This is *when the work is planned for*, not when it is owed.
            deadline_date: ISO date (YYYY-MM-DD) for when the task must be done
                by. Distinct from the due date: due is the plan, deadline is the
                commitment. The API takes no natural-language form here.
            priority: 1 (normal) to 4 (urgent).
        """
        payload: dict = {"content": content}
        if description is not None:
            payload["description"] = description
        if project_id is not None:
            payload["project_id"] = project_id
        if labels is not None:
            payload["labels"] = labels
        if due_string is not None:
            payload["due_string"] = due_string
        if deadline_date is not None:
            payload["deadline_date"] = deadline_date
        if priority is not None:
            payload["priority"] = priority

        try:
            resp = self._session.post(f"{self.BASE_URL}/tasks", json=payload)
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(f"Network error on POST /tasks: {exc}") from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Todoist API error {resp.status_code} on POST /tasks: {resp.text}"
            )
        return resp.json()

    def update_task(
        self,
        task_id: str,
        *,
        content: Optional[str] = None,
        description: Optional[str] = None,
        labels: Optional[list[str]] = None,
        due_string: Optional[str] = None,
        due_date: Optional[str] = None,
        deadline_date: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> dict:
        """Update fields on an existing task and return the updated task object.

        Only the fields passed (non-None) are changed; omitted fields are left
        as-is. ``labels`` replaces the full label set. ``priority`` uses the API
        scale (1=normal .. 4=urgent), matching :meth:`create_task`.

        Args:
            task_id: ID of the task to update (required).
            content: New task title.
            description: New description/notes.
            labels: Full replacement list of label names.
            due_string: Human-readable due date, e.g. "Jun 4 2026", "tomorrow".
            due_date: ISO date (YYYY-MM-DD). Prefer this over due_string for
                machine-generated dates — it skips Todoist's NL parser, so it
                can't misfire on an ambiguous phrase. Mutually exclusive with
                due_string; passing both is a caller error.
            deadline_date: ISO date (YYYY-MM-DD) for when the task must be done
                by. Distinct from the due date: due is when the work is planned,
                deadline is when it is owed. The API takes no natural-language
                form here.
            priority: 1 (normal) to 4 (urgent).
        """
        if due_string is not None and due_date is not None:
            raise ValueError("update_task: pass at most one of due_string, due_date")

        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if description is not None:
            payload["description"] = description
        if labels is not None:
            payload["labels"] = labels
        if due_string is not None:
            payload["due_string"] = due_string
        if due_date is not None:
            payload["due_date"] = due_date
        if deadline_date is not None:
            payload["deadline_date"] = deadline_date
        if priority is not None:
            payload["priority"] = priority

        try:
            resp = self._session.post(f"{self.BASE_URL}/tasks/{task_id}", json=payload)
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(
                f"Network error on POST /tasks/{task_id}: {exc}"
            ) from exc
        if not resp.ok:
            raise TodoistAPIError(
                f"Todoist API error {resp.status_code} on POST /tasks/{task_id}: {resp.text}"
            )
        return resp.json()

    def reschedule_recurring(self, task_id: str, due_date: str, rule: str) -> None:
        """Move one occurrence of a recurring task to `due_date`, keeping its rule.

        :meth:`update_task` can't do this: both `due_date` and `due_string`
        replace the whole due object, which drops the recurrence. The Sync API's
        ``item_update`` takes a full due object, so the date moves and `rule`
        ("every mon") stays. See docs/recurring-tasks.md for what a move does to
        later occurrences.

        Args:
            task_id: ID of the recurring task.
            due_date: ISO date (YYYY-MM-DD) for this occurrence.
            rule: The task's current recurrence string, from ``due.string``.
        """
        # Sync reports each command's outcome under its uuid, and a 200 can
        # still carry a failed command, so the status is checked per command.
        command_id = str(uuid.uuid4())
        command = {
            "type": "item_update",
            "uuid": command_id,
            "args": {"id": task_id, "due": {"date": due_date, "string": rule, "is_recurring": True}},
        }
        try:
            resp = self._session.post(f"{self.BASE_URL}/sync", json={"commands": [command]})
        except requests.exceptions.RequestException as exc:
            raise TodoistAPIError(f"Network error on POST /sync: {exc}") from exc
        if not resp.ok:
            raise TodoistAPIError(f"Todoist API error {resp.status_code} on POST /sync: {resp.text}")
        status = resp.json().get("sync_status", {}).get(command_id)
        if status != "ok":
            raise TodoistAPIError(f"Todoist sync item_update failed for {task_id}: {status}")
