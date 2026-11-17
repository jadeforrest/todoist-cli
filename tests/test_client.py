"""Unit tests for todoist_client.py — all HTTP calls are mocked."""

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, strategies as st
import requests
from requests.adapters import HTTPAdapter

from todoist_cli.client import (
    _AUTH_ATTACHMENT_HOSTS,
    attachment_sends_auth,
    TodoistClient,
    TodoistAPIError,
    TimeoutHTTPAdapter,
    is_text_attachment,
    _ATTACHMENT_TIMEOUT,
    _DEFAULT_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_response(json_data, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.ok = status_code < 400
    mock_resp.json.return_value = json_data
    mock_resp.text = str(json_data)
    return mock_resp


OPEN_TASK_BILL = {
    "id": "task-001",
    "content": "Pay electric bill",
    "description": "Amount due: $120.00.",
    "is_completed": False,
    "labels": ["bills"],
}

OPEN_TASK_BILL_2 = {
    "id": "task-002",
    "content": "Pay water bill",
    "description": "Amount due: $45.00.",
    "is_completed": False,
    "labels": ["bills"],
}

COMPLETED_TASK_BILL = {
    "id": "task-003",
    "content": "Pay last month's bill",
    "description": "Amount due: $30.00.",
    "is_completed": True,
    "labels": ["bills"],
}

UNRELATED_TASK = {
    "id": "task-004",
    "content": "Buy groceries",
    "description": "",
    "is_completed": False,
    "labels": ["shopping"],
}


# ---------------------------------------------------------------------------
# TestGetOpenTasksByLabel
# ---------------------------------------------------------------------------

class TestGetOpenTasksByLabel:
    def test_returns_open_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([OPEN_TASK_BILL])
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")

        mock_session.get.assert_called_once_with(
            "https://api.todoist.com/api/v1/tasks",
            params={"label": "bills"},
        )
        assert len(tasks) == 1
        assert tasks[0]["id"] == "task-001"

    def test_filters_out_completed_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            [OPEN_TASK_BILL, COMPLETED_TASK_BILL]
        )
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")

        assert len(tasks) == 1
        assert tasks[0]["id"] == "task-001"

    def test_returns_multiple_open_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            [OPEN_TASK_BILL, OPEN_TASK_BILL_2]
        )
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")

        assert len(tasks) == 2

    def test_returns_empty_list_when_no_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([])
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")

        assert tasks == []

    def test_returns_empty_list_when_all_completed(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([COMPLETED_TASK_BILL])
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")

        assert tasks == []

    def test_follows_pagination_preserving_the_label_filter(self):
        # The second page must still be scoped to the label, or it silently
        # returns the whole task list.
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            make_mock_response({"results": [OPEN_TASK_BILL], "next_cursor": "abc"}),
            make_mock_response({"results": [OPEN_TASK_BILL_2], "next_cursor": None}),
        ]
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")

        assert len(tasks) == 2
        _, second_kwargs = mock_session.get.call_args_list[1]
        assert second_kwargs["params"] == {"label": "bills", "cursor": "abc"}

    def test_passes_label_as_query_param(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([])
        client = TodoistClient("test-token", session=mock_session)

        client.get_open_tasks_by_label("my-custom-label")

        _, kwargs = mock_session.get.call_args
        assert kwargs["params"]["label"] == "my-custom-label"

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("bad-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.get_open_tasks_by_label("bills")
        assert "401" in str(exc_info.value)

    def test_raises_on_500(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=500)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.get_open_tasks_by_label("bills")
        assert "500" in str(exc_info.value)

    def test_raises_on_403(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=403)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.get_open_tasks_by_label("bills")

    def test_task_missing_is_completed_field_treated_as_open(self):
        """Tasks without is_completed default to open (False)."""
        task_no_field = {
            "id": "task-005",
            "content": "Task without is_completed",
            "description": "Amount due: $10.00.",
            "labels": ["bills"],
            # no is_completed key
        }
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([task_no_field])
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_label("bills")
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# TestGetAllOpenTasks
# ---------------------------------------------------------------------------

class TestGetAllOpenTasks:
    def test_returns_all_open_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            [OPEN_TASK_BILL, UNRELATED_TASK]
        )
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_all_open_tasks()

        assert len(tasks) == 2
        assert {t["id"] for t in tasks} == {"task-001", "task-004"}

    def test_sends_no_label_filter(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([])
        client = TodoistClient("test-token", session=mock_session)

        client.get_all_open_tasks()

        mock_session.get.assert_called_once_with(
            "https://api.todoist.com/api/v1/tasks",
            params={},
        )

    def test_filters_out_completed_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            [OPEN_TASK_BILL, COMPLETED_TASK_BILL]
        )
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_all_open_tasks()

        assert len(tasks) == 1
        assert tasks[0]["id"] == "task-001"

    def test_follows_pagination(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            make_mock_response({"results": [OPEN_TASK_BILL], "next_cursor": "abc"}),
            make_mock_response({"results": [UNRELATED_TASK], "next_cursor": None}),
        ]
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_all_open_tasks()

        assert len(tasks) == 2
        assert mock_session.get.call_count == 2
        _, second_kwargs = mock_session.get.call_args_list[1]
        assert second_kwargs["params"] == {"cursor": "abc"}

    def test_returns_empty_list_when_no_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([])
        client = TodoistClient("test-token", session=mock_session)

        assert client.get_all_open_tasks() == []


# ---------------------------------------------------------------------------
# TestGetOpenTasksByFilter
# ---------------------------------------------------------------------------

class TestGetOpenTasksByFilter:
    def test_passes_query_param(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({"results": [], "next_cursor": None})
        client = TodoistClient("test-token", session=mock_session)

        client.get_open_tasks_by_filter("search: standup")

        _, kwargs = mock_session.get.call_args
        assert kwargs["params"] == {"query": "search: standup"}
        called_url = mock_session.get.call_args[0][0]
        assert called_url.endswith("/tasks/filter")

    def test_filters_out_completed_tasks(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            {"results": [OPEN_TASK_BILL, COMPLETED_TASK_BILL], "next_cursor": None}
        )
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_filter("search: cc")

        assert tasks == [OPEN_TASK_BILL]

    def test_follows_pagination(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            make_mock_response({"results": [OPEN_TASK_BILL], "next_cursor": "abc"}),
            make_mock_response({"results": [UNRELATED_TASK], "next_cursor": None}),
        ]
        client = TodoistClient("test-token", session=mock_session)

        tasks = client.get_open_tasks_by_filter("(due: today | deadline: today)")

        assert len(tasks) == 2
        assert mock_session.get.call_count == 2
        _, second_kwargs = mock_session.get.call_args_list[1]
        assert second_kwargs["params"] == {
            "query": "(due: today | deadline: today)",
            "cursor": "abc",
        }

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.get_open_tasks_by_filter("search: x")


# ---------------------------------------------------------------------------
# TestGetSections
# ---------------------------------------------------------------------------

SECTION_REPRIORITIZE = {"id": "sec-1", "project_id": "proj-inbox", "name": "Reprioritize"}
SECTION_REALISTIC = {"id": "sec-2", "project_id": "proj-inbox", "name": "Realistic"}
SECTION_BOOKS = {"id": "sec-3", "project_id": "proj-books", "name": "Books to read"}


class TestGetSections:
    def test_calls_sections_endpoint_without_filter(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([SECTION_REPRIORITIZE])
        client = TodoistClient("test-token", session=mock_session)

        client.get_sections()

        mock_session.get.assert_called_once_with(
            "https://api.todoist.com/api/v1/sections",
            params={},
        )

    def test_passes_project_id_filter(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([SECTION_REPRIORITIZE])
        client = TodoistClient("test-token", session=mock_session)

        client.get_sections(project_id="proj-inbox")

        _, kwargs = mock_session.get.call_args
        assert kwargs["params"] == {"project_id": "proj-inbox"}

    def test_returns_sections_from_plain_list(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            [SECTION_REPRIORITIZE, SECTION_REALISTIC]
        )
        client = TodoistClient("test-token", session=mock_session)

        sections = client.get_sections()

        assert [s["id"] for s in sections] == ["sec-1", "sec-2"]

    def test_handles_paginated_results_dict(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            {"results": [SECTION_REPRIORITIZE], "next_cursor": None}
        )
        client = TodoistClient("test-token", session=mock_session)

        sections = client.get_sections()

        assert len(sections) == 1
        assert sections[0]["name"] == "Reprioritize"

    def test_follows_pagination_preserving_project_filter(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            make_mock_response({"results": [SECTION_REPRIORITIZE], "next_cursor": "abc"}),
            make_mock_response({"results": [SECTION_REALISTIC], "next_cursor": None}),
        ]
        client = TodoistClient("test-token", session=mock_session)

        sections = client.get_sections(project_id="proj-inbox")

        assert len(sections) == 2
        assert mock_session.get.call_count == 2
        _, second_kwargs = mock_session.get.call_args_list[1]
        assert second_kwargs["params"] == {"project_id": "proj-inbox", "cursor": "abc"}

    def test_returns_empty_list_when_no_sections(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([])
        client = TodoistClient("test-token", session=mock_session)

        assert client.get_sections() == []

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("bad-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.get_sections()


# ---------------------------------------------------------------------------
# TestGetProjects
# ---------------------------------------------------------------------------

PROJECT_INBOX = {"id": "proj-inbox", "name": "Inbox", "inbox_project": True}
PROJECT_OTHER = {"id": "proj-other", "name": "Programming", "inbox_project": False}


class TestGetProjects:
    def test_calls_projects_endpoint(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([PROJECT_INBOX])
        client = TodoistClient("test-token", session=mock_session)

        client.get_projects()

        mock_session.get.assert_called_once_with(
            "https://api.todoist.com/api/v1/projects", params={}
        )

    def test_returns_inbox_flag(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([PROJECT_INBOX, PROJECT_OTHER])
        client = TodoistClient("test-token", session=mock_session)

        projects = client.get_projects()

        inbox = next(p for p in projects if p["inbox_project"])
        assert inbox["id"] == "proj-inbox"

    def test_follows_pagination(self):
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            make_mock_response({"results": [PROJECT_INBOX], "next_cursor": "abc"}),
            make_mock_response({"results": [PROJECT_OTHER], "next_cursor": None}),
        ]
        client = TodoistClient("test-token", session=mock_session)

        projects = client.get_projects()

        assert len(projects) == 2
        assert mock_session.get.call_count == 2

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("bad-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.get_projects()


# ---------------------------------------------------------------------------
# TestMoveTask
# ---------------------------------------------------------------------------

MOVED_TASK = {"id": "task-001", "section_id": "sec-1", "content": "Overdue thing"}


class TestMoveTask:
    def test_posts_to_move_endpoint_with_section_id(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(MOVED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.move_task("task-001", section_id="sec-1")

        args, kwargs = mock_session.post.call_args
        assert args[0] == "https://api.todoist.com/api/v1/tasks/task-001/move"
        assert kwargs["json"] == {"section_id": "sec-1"}

    def test_sends_project_id(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(MOVED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.move_task("task-001", project_id="proj-9")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {"project_id": "proj-9"}

    def test_sends_parent_id(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(MOVED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.move_task("task-001", parent_id="task-parent")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {"parent_id": "task-parent"}

    def test_returns_response_body(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(MOVED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        result = client.move_task("task-001", section_id="sec-1")

        assert result["section_id"] == "sec-1"

    def test_no_destination_raises_and_does_not_post(self):
        mock_session = MagicMock()
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.move_task("task-001")

        assert "requires one of" in str(exc_info.value)
        mock_session.post.assert_not_called()

    def test_returns_empty_dict_when_no_content(self):
        mock_session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 204
        resp.content = b""
        mock_session.post.return_value = resp
        client = TodoistClient("test-token", session=mock_session)

        assert client.move_task("task-001", section_id="sec-1") == {}
        resp.json.assert_not_called()

    def test_returns_empty_dict_on_non_json_body(self):
        mock_session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.content = b"OK"
        resp.json.side_effect = ValueError("not JSON")
        mock_session.post.return_value = resp
        client = TodoistClient("test-token", session=mock_session)

        # A non-JSON 2xx body must not escape as an uncaught JSONDecodeError.
        assert client.move_task("task-001", section_id="sec-1") == {}

    def test_raises_on_404(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=404)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.move_task("nope", section_id="sec-1")
        assert "404" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestUpdateTask
# ---------------------------------------------------------------------------

UPDATED_TASK = {
    "id": "task-300",
    "content": "Check in with Acme",
    "description": "",
    "labels": [],
    "is_completed": False,
    "due": {"date": "2026-06-04", "string": "Jun 4 2026"},
}


class TestUpdateTask:
    def test_posts_to_task_id_endpoint(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300", due_string="Jun 4 2026")

        args, kwargs = mock_session.post.call_args
        assert args[0] == "https://api.todoist.com/api/v1/tasks/task-300"
        assert kwargs["json"] == {"due_string": "Jun 4 2026"}

    def test_returns_updated_task(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        task = client.update_task("task-300", due_string="Jun 4 2026")

        assert task["id"] == "task-300"
        assert task["due"]["date"] == "2026-06-04"

    def test_only_sends_provided_fields(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300", priority=4, labels=["work"])

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {"priority": 4, "labels": ["work"]}

    def test_empty_update_sends_empty_payload(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {}

    def test_sends_content_and_description(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300", content="New title", description="New notes")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {"content": "New title", "description": "New notes"}

    def test_sends_due_date_as_iso_date(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300", due_date="2026-07-28")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {"due_date": "2026-07-28"}

    def test_sends_deadline_date_as_iso_date(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300", deadline_date="2026-09-26")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"] == {"deadline_date": "2026-09-26"}

    def test_deadline_does_not_disturb_the_due_date(self):
        # Setting a commitment date must leave the plan date alone — omitted
        # fields stay as-is, so the payload carries deadline_date only.
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(UPDATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.update_task("task-300", deadline_date="2026-09-26")

        _, kwargs = mock_session.post.call_args
        assert "due_date" not in kwargs["json"]
        assert "due_string" not in kwargs["json"]

    def test_due_date_and_due_string_together_raises_without_a_request(self):
        mock_session = MagicMock()
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(ValueError):
            client.update_task("task-300", due_string="tomorrow", due_date="2026-07-28")
        mock_session.post.assert_not_called()

    def test_raises_on_404(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=404)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.update_task("nonexistent", due_string="tomorrow")


# ---------------------------------------------------------------------------
# TestCloseTask
# ---------------------------------------------------------------------------

class TestCloseTask:
    def test_calls_correct_endpoint(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=204)
        client = TodoistClient("test-token", session=mock_session)

        client.close_task("task-001")

        mock_session.post.assert_called_once_with(
            "https://api.todoist.com/api/v1/tasks/task-001/close"
        )

    def test_success_returns_none(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=204)
        client = TodoistClient("test-token", session=mock_session)

        result = client.close_task("task-001")
        assert result is None

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("bad-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.close_task("task-001")
        assert "401" in str(exc_info.value)

    def test_raises_on_404(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=404)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.close_task("does-not-exist")
        assert "404" in str(exc_info.value)

    def test_raises_on_500(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=500)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.close_task("task-001")

    def test_uses_task_id_in_url(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=204)
        client = TodoistClient("test-token", session=mock_session)

        client.close_task("my-unique-task-id-999")

        url = mock_session.post.call_args[0][0]
        assert "my-unique-task-id-999" in url


# ---------------------------------------------------------------------------
# TestCreateTask
# ---------------------------------------------------------------------------

CREATED_TASK = {
    "id": "task-100",
    "content": "Buy milk",
    "description": "",
    "labels": [],
    "is_completed": False,
    "url": "https://todoist.com/app/task/task-100",
}


class TestCreateTask:
    def test_posts_to_tasks_endpoint(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk")

        url = mock_session.post.call_args[0][0]
        assert url == "https://api.todoist.com/api/v1/tasks"

    def test_returns_created_task(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        result = client.create_task("Buy milk")

        assert result["id"] == "task-100"
        assert result["content"] == "Buy milk"

    def test_sends_content_in_payload(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["content"] == "Buy milk"

    def test_optional_fields_omitted_when_none(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk")

        _, kwargs = mock_session.post.call_args
        payload = kwargs["json"]
        assert "description" not in payload
        assert "project_id" not in payload
        assert "labels" not in payload
        assert "due_string" not in payload
        assert "deadline_date" not in payload
        assert "priority" not in payload

    def test_sends_description_when_provided(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", description="Oat milk from Trader Joe's")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["description"] == "Oat milk from Trader Joe's"

    def test_sends_labels_when_provided(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", labels=["shopping", "urgent"])

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["labels"] == ["shopping", "urgent"]

    def test_sends_due_string_when_provided(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", due_string="tomorrow")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["due_string"] == "tomorrow"

    def test_sends_deadline_date_when_provided(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", deadline_date="2026-08-17")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["deadline_date"] == "2026-08-17"

    def test_due_and_deadline_are_sent_as_separate_fields(self):
        # The whole point of the flag: a plan date and a commitment date coexist
        # on one task without either overwriting the other.
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", due_string="tomorrow", deadline_date="2026-08-17")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["due_string"] == "tomorrow"
        assert kwargs["json"]["deadline_date"] == "2026-08-17"

    def test_sends_priority_when_provided(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", priority=4)

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["priority"] == 4

    def test_sends_project_id_when_provided(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)
        client = TodoistClient("test-token", session=mock_session)

        client.create_task("Buy milk", project_id="proj-42")

        _, kwargs = mock_session.post.call_args
        assert kwargs["json"]["project_id"] == "proj-42"

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("bad-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.create_task("Buy milk")
        assert "401" in str(exc_info.value)

    def test_raises_on_403(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=403)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.create_task("Buy milk")

    def test_raises_on_500(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=500)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.create_task("Buy milk")


# ---------------------------------------------------------------------------
# TestGetTaskComments
# ---------------------------------------------------------------------------

COMMENT_WITH_TEXT_ATTACHMENT = {
    "id": "comment-001",
    "task_id": "task-001",
    "content": "Here's the context",
    "posted_at": "2026-04-10T10:00:00Z",
    "attachment": {
        "file_name": "notes.txt",
        "file_type": "text/plain",
        "file_url": "https://cdn.todoist.com/notes.txt",
        "resource_type": "file",
    },
}

COMMENT_WITHOUT_ATTACHMENT = {
    "id": "comment-002",
    "task_id": "task-001",
    "content": "Just a text comment",
    "posted_at": "2026-04-10T11:00:00Z",
}

COMMENT_WITH_IMAGE_ATTACHMENT = {
    "id": "comment-003",
    "task_id": "task-001",
    "content": "Screenshot",
    "posted_at": "2026-04-10T12:00:00Z",
    "attachment": {
        "file_name": "screenshot.png",
        "file_type": "image/png",
        "file_url": "https://cdn.todoist.com/screenshot.png",
        "resource_type": "file",
    },
}


class TestGetTaskComments:
    def test_calls_comments_endpoint_with_task_id(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([COMMENT_WITH_TEXT_ATTACHMENT])
        client = TodoistClient("test-token", session=mock_session)

        client.get_task_comments("task-001")

        mock_session.get.assert_called_once_with(
            "https://api.todoist.com/api/v1/comments",
            params={"task_id": "task-001"},
        )

    def test_returns_list_of_comments(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            [COMMENT_WITH_TEXT_ATTACHMENT, COMMENT_WITHOUT_ATTACHMENT]
        )
        client = TodoistClient("test-token", session=mock_session)

        comments = client.get_task_comments("task-001")

        assert len(comments) == 2

    def test_returns_empty_list_when_no_comments(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response([])
        client = TodoistClient("test-token", session=mock_session)

        comments = client.get_task_comments("task-001")

        assert comments == []

    def test_handles_paginated_results_dict(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response(
            {"results": [COMMENT_WITH_TEXT_ATTACHMENT], "next_cursor": None}
        )
        client = TodoistClient("test-token", session=mock_session)

        comments = client.get_task_comments("task-001")

        assert len(comments) == 1
        assert comments[0]["id"] == "comment-001"

    def test_raises_on_404(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=404)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.get_task_comments("no-such-task")
        assert "404" in str(exc_info.value)

    def test_raises_on_401(self):
        mock_session = MagicMock()
        mock_session.get.return_value = make_mock_response({}, status_code=401)
        client = TodoistClient("bad-token", session=mock_session)

        with pytest.raises(TodoistAPIError):
            client.get_task_comments("task-001")


# ---------------------------------------------------------------------------
# TestFetchAttachmentText
# ---------------------------------------------------------------------------

class TestFetchAttachmentText:
    def test_returns_text_content(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "Client: Acme Corp\nMeeting: discussed roadmap"
        mock_session.get.return_value = mock_resp
        client = TodoistClient("test-token", session=mock_session)

        text = client.fetch_attachment_text("https://files.todoist.com/notes.txt")

        assert text == "Client: Acme Corp\nMeeting: discussed roadmap"
        # Attachments use the longer read timeout, set explicitly per-call
        # (see _ATTACHMENT_TIMEOUT) rather than the session default.
        mock_session.get.assert_called_once_with(
            "https://files.todoist.com/notes.txt", timeout=_ATTACHMENT_TIMEOUT
        )

    def test_raises_on_failure(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_session.get.return_value = mock_resp
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.fetch_attachment_text("https://files.todoist.com/secret.txt")
        assert "403" in str(exc_info.value)


class TestAttachmentAuthIsolation:
    """The bearer token must never reach a non-Todoist or non-https host."""

    @staticmethod
    def _sent_headers(url):
        """Run a real Session through fetch_attachment_text; return the headers sent."""
        session = requests.Session()
        sent = {}

        def fake_send(request, **kwargs):
            sent["headers"] = dict(request.headers)
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b"hi"
            return resp

        session.send = fake_send
        TodoistClient("secret-token", session=session).fetch_attachment_text(url)
        return sent["headers"]

    def test_todoist_files_host_gets_the_token(self):
        assert "Authorization" in self._sent_headers("https://files.todoist.com/a.txt")

    def test_other_https_host_gets_no_token(self):
        assert "Authorization" not in self._sent_headers("https://evil.example.com/a.txt")

    def test_lookalike_hosts_get_no_token(self):
        for url in ("https://files.todoist.com.evil.com/a", "https://evil.com/files.todoist.com",
                    "https://files.todoist.com@evil.com/a"):
            assert "Authorization" not in self._sent_headers(url)

    def test_http_is_refused_without_any_request(self):
        mock_session = MagicMock()
        client = TodoistClient("secret-token", session=mock_session)
        with pytest.raises(TodoistAPIError):
            client.fetch_attachment_text("http://files.todoist.com/a.txt")
        mock_session.get.assert_not_called()

    def test_non_todoist_host_request_suppresses_the_session_header(self):
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(ok=True, text="x")
        TodoistClient("t", session=mock_session).fetch_attachment_text("https://example.com/a")
        _, kwargs = mock_session.get.call_args
        assert kwargs["headers"] == {"Authorization": None}
        assert kwargs["timeout"] == _ATTACHMENT_TIMEOUT

    def test_api_calls_still_carry_the_token(self):
        assert TodoistClient("secret-token")._session.headers["Authorization"] == "Bearer secret-token"

    @given(
        scheme=st.sampled_from(["http", "https", "ftp", ""]),
        host=st.one_of(
            st.sampled_from(sorted(_AUTH_ATTACHMENT_HOSTS)),
            st.from_regex(r"[a-z0-9]([a-z0-9.-]{0,20}[a-z0-9])?", fullmatch=True),
        ),
        path=st.from_regex(r"(/[a-zA-Z0-9._-]{0,10}){0,3}", fullmatch=True),
    )
    def test_property_auth_only_for_https_and_allowlisted_host(self, scheme, host, path):
        url = f"{scheme}://{host}{path}"
        expected = scheme == "https" and host in _AUTH_ATTACHMENT_HOSTS
        assert attachment_sends_auth(url) is expected
        if scheme != "https":
            mock_session = MagicMock()
            with pytest.raises(TodoistAPIError):
                TodoistClient("t", session=mock_session).fetch_attachment_text(url)
            mock_session.get.assert_not_called()
        else:
            mock_session = MagicMock()
            mock_session.get.return_value = MagicMock(ok=True, text="x")
            TodoistClient("t", session=mock_session).fetch_attachment_text(url)
            has_override = "headers" in mock_session.get.call_args.kwargs
            assert has_override is (not expected)


# ---------------------------------------------------------------------------
# TestIsTextAttachment
# ---------------------------------------------------------------------------

class TestIsTextAttachment:
    def test_text_plain_mime_type(self):
        assert is_text_attachment({"file_name": "f.txt", "file_type": "text/plain"})

    def test_text_markdown_mime_type(self):
        assert is_text_attachment({"file_name": "f.md", "file_type": "text/markdown"})

    def test_text_csv_mime_type(self):
        assert is_text_attachment({"file_name": "f.csv", "file_type": "text/csv"})

    def test_image_png_is_not_text(self):
        assert not is_text_attachment({"file_name": "f.png", "file_type": "image/png"})

    def test_application_pdf_is_not_text(self):
        assert not is_text_attachment({"file_name": "f.pdf", "file_type": "application/pdf"})

    def test_txt_extension_fallback(self):
        """Falls back to extension when file_type is missing."""
        assert is_text_attachment({"file_name": "notes.txt"})

    def test_md_extension_fallback(self):
        assert is_text_attachment({"file_name": "README.md"})

    def test_json_extension_fallback(self):
        assert is_text_attachment({"file_name": "data.json"})

    def test_yaml_extension_fallback(self):
        assert is_text_attachment({"file_name": "config.yaml"})

    def test_yml_extension_fallback(self):
        assert is_text_attachment({"file_name": "config.yml"})

    def test_png_extension_not_text(self):
        assert not is_text_attachment({"file_name": "photo.png"})

    def test_empty_attachment_is_not_text(self):
        assert not is_text_attachment({})

    def test_generic_octet_stream_falls_back_to_extension(self):
        assert is_text_attachment({
            "file_name": "notes.txt",
            "file_type": "application/octet-stream",
        })


# ---------------------------------------------------------------------------
# TestClientAuth
# ---------------------------------------------------------------------------

class TestClientAuth:
    def test_bearer_token_set_in_headers(self):
        mock_session = MagicMock()
        TodoistClient("my-api-token", session=mock_session)

        mock_session.headers.update.assert_called_once_with(
            {"Authorization": "Bearer my-api-token"}
        )

    def test_default_session_is_requests_session(self):
        client = TodoistClient("test-token")
        assert isinstance(client._session, requests.Session)


# ---------------------------------------------------------------------------
# Session hardening: the default timeout and retry policy
# ---------------------------------------------------------------------------


class TestTimeoutHTTPAdapter:
    def _adapter(self):
        adapter = TimeoutHTTPAdapter()
        adapter.send = MagicMock()
        return adapter

    def test_default_timeout_applied_when_the_caller_gives_none(self):
        # requests has no timeout by default, so a dead network would hang forever.
        adapter = TimeoutHTTPAdapter()
        with patch.object(HTTPAdapter, "send") as parent_send:
            adapter.send(MagicMock(), timeout=None)
        assert parent_send.call_args.kwargs["timeout"] == _DEFAULT_TIMEOUT

    def test_default_timeout_applied_when_the_caller_omits_it(self):
        adapter = TimeoutHTTPAdapter()
        with patch.object(HTTPAdapter, "send") as parent_send:
            adapter.send(MagicMock())
        assert parent_send.call_args.kwargs["timeout"] == _DEFAULT_TIMEOUT

    def test_explicit_timeout_is_left_alone(self):
        adapter = TimeoutHTTPAdapter()
        with patch.object(HTTPAdapter, "send") as parent_send:
            adapter.send(MagicMock(), timeout=(1, 2))
        assert parent_send.call_args.kwargs["timeout"] == (1, 2)

    def test_default_timeout_is_a_short_connect_and_longer_read(self):
        connect, read = _DEFAULT_TIMEOUT
        assert (connect, read) == (5, 30)

    def test_attachment_timeout_allows_a_longer_read(self):
        connect, read = _ATTACHMENT_TIMEOUT
        assert (connect, read) == (5, 120)
        assert read > _DEFAULT_TIMEOUT[1]  # downloads get more room than API calls


class TestSessionRetryPolicy:
    def _adapters(self):
        client = TodoistClient("test-token")
        return client._session.adapters

    def test_https_gets_the_timeout_adapter_and_http_is_not_mounted_with_it(self):
        adapters = self._adapters()
        assert isinstance(adapters["https://"], TimeoutHTTPAdapter)
        # Plain http is never fetched, so it has no retrying adapter.
        assert not isinstance(adapters["http://"], TimeoutHTTPAdapter)

    def test_retries_transient_server_errors_but_not_client_errors(self):
        retry = self._adapters()["https://"].max_retries
        assert list(retry.status_forcelist) == [500, 502, 503, 504]
        assert 404 not in retry.status_forcelist  # a bad id will never succeed

    def test_retry_budget_and_backoff(self):
        retry = self._adapters()["https://"].max_retries
        assert retry.total == 3
        # connect=1: don't multiply connect-timeout waits on a dead network.
        assert retry.connect == 1
        assert retry.backoff_factor == 2

    def test_a_caller_supplied_session_is_left_unconfigured(self):
        # Tests (and callers with their own policy) pass a session in; the
        # client must not stomp on it beyond adding the auth header.
        session = requests.Session()
        client = TodoistClient("test-token", session=session)
        assert client._session is session
        assert session.headers["Authorization"] == "Bearer test-token"


class TestRescheduleRecurring:
    def _client(self, response):
        mock_session = MagicMock()
        # A function builds the reply from the payload (it needs the command
        # uuid); anything else is the reply itself. MagicMock is callable, so
        # this can't be a callable() check.
        if isinstance(response, MagicMock):
            mock_session.post.return_value = response
        else:
            mock_session.post.side_effect = lambda url, json: response(json)
        return TodoistClient("test-token", session=mock_session), mock_session

    @staticmethod
    def _ok(payload):
        return make_mock_response({"sync_status": {payload["commands"][0]["uuid"]: "ok"}})

    def test_sends_one_item_update_with_the_rule_kept(self):
        client, session = self._client(self._ok)
        client.reschedule_recurring("task-9", "2026-10-28", "every mon")
        args, kwargs = session.post.call_args
        assert args[0] == "https://api.todoist.com/api/v1/sync"
        (command,) = kwargs["json"]["commands"]
        assert command["type"] == "item_update"
        assert command["args"] == {
            "id": "task-9",
            "due": {"date": "2026-10-28", "string": "every mon", "is_recurring": True},
        }

    def test_each_call_uses_a_fresh_command_uuid(self):
        client, session = self._client(self._ok)
        client.reschedule_recurring("a", "2026-10-28", "every mon")
        client.reschedule_recurring("b", "2026-10-28", "every mon")
        uuids = {c.kwargs["json"]["commands"][0]["uuid"] for c in session.post.call_args_list}
        assert len(uuids) == 2

    def test_a_failed_command_inside_a_200_raises(self):
        # Sync answers 200 even when the command itself was rejected.
        def rejected(payload):
            cmd_id = payload["commands"][0]["uuid"]
            return make_mock_response({"sync_status": {cmd_id: {"error": "Invalid date"}}})

        client, _ = self._client(rejected)
        with pytest.raises(TodoistAPIError, match="Invalid date"):
            client.reschedule_recurring("task-9", "2026-10-28", "every mon")

    def test_a_missing_status_raises(self):
        client, _ = self._client(make_mock_response({"sync_status": {}}))
        with pytest.raises(TodoistAPIError, match="task-9"):
            client.reschedule_recurring("task-9", "2026-10-28", "every mon")

    def test_http_error_raises(self):
        client, _ = self._client(make_mock_response({}, status_code=403))
        with pytest.raises(TodoistAPIError, match="403"):
            client.reschedule_recurring("task-9", "2026-10-28", "every mon")

    def test_network_error_raises(self):
        mock_session = MagicMock()
        mock_session.post.side_effect = requests.exceptions.ConnectionError("down")
        client = TodoistClient("test-token", session=mock_session)
        with pytest.raises(TodoistAPIError, match="Network error"):
            client.reschedule_recurring("task-9", "2026-10-28", "every mon")


# ---------------------------------------------------------------------------
# TestAddComment
# ---------------------------------------------------------------------------

class TestAddComment:
    def test_posts_task_id_and_content(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({"id": "c-1", "content": "hi"})
        client = TodoistClient("test-token", session=mock_session)

        result = client.add_comment("task-001", "hi")

        mock_session.post.assert_called_once_with(
            "https://api.todoist.com/api/v1/comments",
            json={"task_id": "task-001", "content": "hi"},
        )
        assert result == {"id": "c-1", "content": "hi"}

    def test_raises_on_http_error(self):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({}, status_code=403)
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.add_comment("task-001", "hi")
        assert "403" in str(exc_info.value)

    def test_raises_on_network_error(self):
        mock_session = MagicMock()
        mock_session.post.side_effect = requests.exceptions.ConnectionError("down")
        client = TodoistClient("test-token", session=mock_session)

        with pytest.raises(TodoistAPIError) as exc_info:
            client.add_comment("task-001", "hi")
        assert "Network error" in str(exc_info.value)

    @given(task_id=st.text(min_size=1), content=st.text())
    def test_payload_round_trips_any_text(self, task_id, content):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response({"id": "c"})
        client = TodoistClient("test-token", session=mock_session)

        client.add_comment(task_id, content)

        sent = mock_session.post.call_args.kwargs["json"]
        assert sent == {"task_id": task_id, "content": content}


class TestTodoneClientAdditions:
    """update_task duration/clear_deadline, update_comment, get_task, get_labels."""

    def _client(self, body=None, status=200):
        session = MagicMock()
        session.post.return_value = make_mock_response(body or {"id": "x"}, status_code=status)
        session.get.return_value = make_mock_response(body or {"id": "x"}, status_code=status)
        return TodoistClient("test-token", session=session), session

    def test_duration_sends_minutes_and_unit(self):
        client, session = self._client()
        client.update_task("t1", duration_minutes=45)
        assert session.post.call_args.kwargs["json"] == {"duration": 45, "duration_unit": "minute"}

    def test_clear_deadline_sends_null(self):
        client, session = self._client()
        client.update_task("t1", clear_deadline=True)
        assert session.post.call_args.kwargs["json"] == {"deadline_date": None}

    def test_clear_deadline_with_a_deadline_is_a_caller_error(self):
        client, session = self._client()
        with pytest.raises(ValueError):
            client.update_task("t1", deadline_date="2026-01-01", clear_deadline=True)
        session.post.assert_not_called()

    def test_update_comment_posts_content(self):
        client, session = self._client({"id": "c1", "content": "new"})
        result = client.update_comment("c1", "new")
        session.post.assert_called_once_with(
            "https://api.todoist.com/api/v1/comments/c1", json={"content": "new"}
        )
        assert result["content"] == "new"

    def test_update_comment_raises_on_http_error(self):
        client, _ = self._client({}, status=404)
        with pytest.raises(TodoistAPIError) as exc_info:
            client.update_comment("c1", "new")
        assert "404 on POST /comments/c1" in str(exc_info.value)

    def test_get_task_reads_one_task(self):
        client, session = self._client({"id": "t9", "content": "x"})
        assert client.get_task("t9")["id"] == "t9"
        assert session.get.call_args.args[0] == "https://api.todoist.com/api/v1/tasks/t9"

    def test_get_labels_follows_pagination(self):
        session = MagicMock()
        session.get.side_effect = [
            make_mock_response({"results": [{"name": "a"}], "next_cursor": "n"}),
            make_mock_response({"results": [{"name": "b"}], "next_cursor": None}),
        ]
        client = TodoistClient("test-token", session=session)
        assert [l["name"] for l in client.get_labels()] == ["a", "b"]
        assert session.get.call_args_list[1].kwargs["params"] == {"cursor": "n"}
