"""Tests for cli.py — all HTTP calls are mocked."""

import contextlib
import io
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from todoist_cli import cli, planner

from tests.conftest import TEST_ACCOUNT


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


CREATED_TASK = {
    "id": "task-100",
    "content": "Buy milk",
    "description": "",
    "labels": [],
    "is_completed": False,
    "url": "https://todoist.com/app/task/task-100",
}

OPEN_TASK_BILL = {
    "id": "task-001",
    "content": "Pay electric bill",
    "description": "Amount due: $120.00.",
    "is_completed": False,
    "labels": ["bills"],
}


# ---------------------------------------------------------------------------
# TestCreate
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_minimal(self, capsys):
        mock_session = MagicMock()
        mock_session.post.return_value = make_mock_response(CREATED_TASK)

        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.create_task.return_value = CREATED_TASK
                cli.main.__wrapped__ = None
                sys.argv = ["todoist", "create", "Buy milk"]
                cli.main()

        instance.create_task.assert_called_once_with(
            "Buy milk",
            description=None,
            project_id=None,
            labels=None,
            due_string=None,
            deadline_date=None,
            priority=None,
        )
        captured = capsys.readouterr()
        assert "task-100" in captured.out
        assert "Buy milk" in captured.out

    def test_create_with_labels(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.create_task.return_value = {**CREATED_TASK, "labels": ["work", "urgent"]}
                sys.argv = ["todoist", "create", "Review PR", "--labels", "work,urgent"]
                cli.main()

        instance.create_task.assert_called_once()
        _, kwargs = instance.create_task.call_args
        assert kwargs["labels"] == ["work", "urgent"]

    def test_create_priority_converts_to_api_value(self):
        """User priority 1 (highest) → API priority 4."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.create_task.return_value = CREATED_TASK
                sys.argv = ["todoist", "create", "Urgent task", "--priority", "1"]
                cli.main()

        _, kwargs = instance.create_task.call_args
        assert kwargs["priority"] == 4

    def test_create_priority_4_converts_to_1(self):
        """User priority 4 (lowest) → API priority 1."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.create_task.return_value = CREATED_TASK
                sys.argv = ["todoist", "create", "Low task", "--priority", "4"]
                cli.main()

        _, kwargs = instance.create_task.call_args
        assert kwargs["priority"] == 1

    def test_create_passes_due_and_deadline_separately(self, capsys):
        """--due is the plan date, --deadline the commitment date; both survive."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.create_task.return_value = {
                    **CREATED_TASK,
                    "due": {"date": "2026-08-13", "string": "Aug 13"},
                    "deadline": {"date": "2026-08-17"},
                }
                sys.argv = [
                    "todoist", "create", "Ship ladder",
                    "--due", "Aug 13 2026", "--deadline", "2026-08-17",
                ]
                cli.main()

        _, kwargs = instance.create_task.call_args
        assert kwargs["due_string"] == "Aug 13 2026"
        assert kwargs["deadline_date"] == "2026-08-17"
        captured = capsys.readouterr()
        assert "Deadline: 2026-08-17" in captured.out

    def test_create_rejects_natural_language_deadline(self, capsys):
        """The API takes ISO only for deadlines — fail here, not with an API 400."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                sys.argv = ["todoist", "create", "Ship it", "--deadline", "next Tuesday"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
                instance.create_task.assert_not_called()
        assert exc_info.value.code == 1
        assert "YYYY-MM-DD" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "bad",
        [
            "2026-02-30",       # well-shaped but no such calendar day
            "2026-8-17",        # strptime accepts unpadded; the API wants canonical
            "2026/08/17",
            "26-08-17",
            "2026-08-17T00:00",
            "2026-08-17 ",
        ],
    )
    def test_create_rejects_non_canonical_deadline(self, bad, capsys):
        """Only exact YYYY-MM-DD gets through to the API."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                sys.argv = ["todoist", "create", "Ship it", "--deadline", bad]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
                instance.create_task.assert_not_called()
        assert exc_info.value.code == 1

    def test_create_api_error_exits_1(self, capsys):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.create_task.side_effect = TodoistAPIError("401 Unauthorized")
                sys.argv = ["todoist", "create", "Buy milk"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
        assert exc_info.value.code == 1

    def test_create_missing_token_exits_1(self, capsys):
        env = {k: v for k, v in __import__("os").environ.items() if k != "TODOIST_API_TOKEN"}
        with patch.dict("os.environ", env, clear=True):
            sys.argv = ["todoist", "create", "Buy milk"]
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TestSearch
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_prints_tasks(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                sys.argv = ["todoist", "search", "--label", "bills"]
                cli.main()

        instance.get_open_tasks_by_label.assert_called_once_with("bills")
        captured = capsys.readouterr()
        assert "task-001" in captured.out
        assert "Pay electric bill" in captured.out

    def test_search_no_results(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = []
                sys.argv = ["todoist", "search", "--label", "nonexistent"]
                cli.main()

        captured = capsys.readouterr()
        assert "No open tasks" in captured.out

    def test_search_json_outputs_raw_tasks(self, capsys):
        import json
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [P1_WITH_DEADLINE]
                sys.argv = ["todoist", "search", "--label", "work", "--json"]
                cli.main()

        parsed = json.loads(capsys.readouterr().out)
        assert parsed == [P1_WITH_DEADLINE]
        assert parsed[0]["id"] == "task-200"
        assert parsed[0]["content"] == "Ship the report"
        assert parsed[0]["priority"] == 4
        assert parsed[0]["due"]["date"] == "2026-06-02"

    def test_search_json_empty_outputs_empty_array(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = []
                sys.argv = ["todoist", "search", "--label", "nonexistent", "--json"]
                cli.main()

        # Must be exactly `[]`, not prose, or a JSON consumer would choke.
        assert capsys.readouterr().out.strip() == "[]"

    def test_search_api_error_exits_1(self):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.side_effect = TodoistAPIError("500")
                sys.argv = ["todoist", "search", "--label", "bills"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TestList
# ---------------------------------------------------------------------------

P1_WITH_DEADLINE = {
    "id": "task-200",
    "content": "Ship the report",
    "description": "",
    "is_completed": False,
    "labels": ["work"],
    "priority": 4,  # API 4 = user-facing P1
    "due": {"date": "2026-06-02", "string": "Jun 2"},
    "deadline": {"date": "2026-06-02"},
}

P4_NO_DATES = {
    "id": "task-201",
    "content": "Tidy desk",
    "description": "",
    "is_completed": False,
    "labels": [],
    "priority": 1,  # API 1 = user-facing P4
}


class TestList:
    def test_list_prints_tasks(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_all_open_tasks.return_value = [P1_WITH_DEADLINE, P4_NO_DATES]
                sys.argv = ["todoist", "list"]
                cli.main()

        instance.get_all_open_tasks.assert_called_once_with()
        captured = capsys.readouterr()
        assert "Ship the report" in captured.out
        assert "Tidy desk" in captured.out

    def test_list_converts_priority_and_shows_deadline(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_all_open_tasks.return_value = [P1_WITH_DEADLINE]
                sys.argv = ["todoist", "list"]
                cli.main()

        captured = capsys.readouterr()
        assert "P1" in captured.out
        assert "deadline: 2026-06-02" in captured.out

    def test_list_sorts_highest_priority_first(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_all_open_tasks.return_value = [P4_NO_DATES, P1_WITH_DEADLINE]
                sys.argv = ["todoist", "list"]
                cli.main()

        captured = capsys.readouterr()
        assert captured.out.index("Ship the report") < captured.out.index("Tidy desk")

    def test_list_no_results(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_all_open_tasks.return_value = []
                sys.argv = ["todoist", "list"]
                cli.main()

        captured = capsys.readouterr()
        assert "No open tasks" in captured.out

    def test_list_api_error_exits_1(self):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_all_open_tasks.side_effect = TodoistAPIError("500")
                sys.argv = ["todoist", "list"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TestUpdate
# ---------------------------------------------------------------------------

UPDATED_TASK = {
    "id": "task-300",
    "content": "Check in with Acme",
    "description": "",
    "labels": [],
    "is_completed": False,
    "due": {"date": "2026-06-04", "string": "Jun 4 2026"},
    "url": "https://todoist.com/app/task/task-300",
}


class TestUpdate:
    def test_update_due_calls_client(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.update_task.return_value = UPDATED_TASK
                sys.argv = ["todoist", "update", "task-300", "--due", "Jun 4 2026"]
                cli.main()

        instance.update_task.assert_called_once_with(
            "task-300",
            content=None,
            description=None,
            labels=None,
            due_string="Jun 4 2026",
            deadline_date=None,
            priority=None,
        )
        captured = capsys.readouterr()
        assert "Updated task: task-300" in captured.out
        assert "Jun 4 2026" in captured.out

    def test_update_priority_converts_to_api_value(self):
        """User priority 1 (highest) → API priority 4."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.update_task.return_value = UPDATED_TASK
                sys.argv = ["todoist", "update", "task-300", "--priority", "1"]
                cli.main()

        _, kwargs = instance.update_task.call_args
        assert kwargs["priority"] == 4

    def test_update_labels_split(self):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.update_task.return_value = UPDATED_TASK
                sys.argv = ["todoist", "update", "task-300", "--labels", "work,urgent"]
                cli.main()

        _, kwargs = instance.update_task.call_args
        assert kwargs["labels"] == ["work", "urgent"]

    def test_update_deadline_alone_is_a_field(self, capsys):
        """--deadline on its own counts as a change; it must not trip the
        "nothing to update" guard."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.update_task.return_value = {
                    **UPDATED_TASK,
                    "deadline": {"date": "2026-09-26"},
                }
                sys.argv = ["todoist", "update", "task-300", "--deadline", "2026-09-26"]
                cli.main()

        _, kwargs = instance.update_task.call_args
        assert kwargs["deadline_date"] == "2026-09-26"
        assert kwargs["due_string"] is None
        assert "Deadline: 2026-09-26" in capsys.readouterr().out

    def test_update_rejects_natural_language_deadline(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                sys.argv = ["todoist", "update", "task-300", "--deadline", "friday"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
                instance.update_task.assert_not_called()
        assert exc_info.value.code == 1
        assert "YYYY-MM-DD" in capsys.readouterr().err

    def test_update_with_no_fields_exits_1(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                sys.argv = ["todoist", "update", "task-300"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
        assert exc_info.value.code == 1
        instance.update_task.assert_not_called()
        captured = capsys.readouterr()
        assert "nothing to update" in captured.err

    def test_update_api_error_exits_1(self):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.update_task.side_effect = TodoistAPIError("404")
                sys.argv = ["todoist", "update", "task-300", "--due", "tomorrow"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TestSearchAttachments
# ---------------------------------------------------------------------------

TEXT_COMMENT = {
    "id": "comment-001",
    "task_id": "task-001",
    "content": "Context",
    "attachment": {
        "file_name": "notes.txt",
        "file_type": "text/plain",
        "file_url": "https://cdn.todoist.com/notes.txt",
        "resource_type": "file",
    },
}

IMAGE_COMMENT = {
    "id": "comment-002",
    "task_id": "task-001",
    "content": "Screenshot",
    "attachment": {
        "file_name": "screen.png",
        "file_type": "image/png",
        "file_url": "https://cdn.todoist.com/screen.png",
        "resource_type": "file",
    },
}

NO_ATTACHMENT_COMMENT = {
    "id": "comment-003",
    "task_id": "task-001",
    "content": "Just a note",
}


class TestSearchAttachments:
    def test_attachments_flag_fetches_comments(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                instance.get_task_comments.return_value = [TEXT_COMMENT]
                instance.fetch_attachment_text.return_value = "Client note line 1\nLine 2"
                sys.argv = ["todoist", "search", "--label", "work", "--attachments"]
                cli.main()

        instance.get_task_comments.assert_called_once_with("task-001")
        instance.fetch_attachment_text.assert_called_once_with("https://cdn.todoist.com/notes.txt")
        captured = capsys.readouterr()
        assert "notes.txt" in captured.out
        assert "Client note line 1" in captured.out
        assert "Line 2" in captured.out

    def test_attachments_flag_skips_non_text(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                instance.get_task_comments.return_value = [IMAGE_COMMENT]
                sys.argv = ["todoist", "search", "--label", "work", "--attachments"]
                cli.main()

        instance.fetch_attachment_text.assert_not_called()

    def test_attachments_flag_skips_comments_without_attachment(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                instance.get_task_comments.return_value = [NO_ATTACHMENT_COMMENT]
                sys.argv = ["todoist", "search", "--label", "work", "--attachments"]
                cli.main()

        instance.fetch_attachment_text.assert_not_called()

    def test_without_attachments_flag_does_not_fetch_comments(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                sys.argv = ["todoist", "search", "--label", "work"]
                cli.main()

        instance.get_task_comments.assert_not_called()

    def test_comment_fetch_failure_prints_warning_continues(self, capsys):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                instance.get_task_comments.side_effect = TodoistAPIError("500")
                sys.argv = ["todoist", "search", "--label", "work", "--attachments"]
                cli.main()  # should not raise

        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_attachment_fetch_failure_prints_warning_continues(self, capsys):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.get_open_tasks_by_label.return_value = [OPEN_TASK_BILL]
                instance.get_task_comments.return_value = [TEXT_COMMENT]
                instance.fetch_attachment_text.side_effect = TodoistAPIError("403")
                sys.argv = ["todoist", "search", "--label", "work", "--attachments"]
                cli.main()  # should not raise

        captured = capsys.readouterr()
        assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# TestClose
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_calls_client(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.close_task.return_value = None
                sys.argv = ["todoist", "close", "task-001"]
                cli.main()

        instance.close_task.assert_called_once_with("task-001")
        captured = capsys.readouterr()
        assert "task-001" in captured.out

    def test_close_api_error_exits_1(self):
        from todoist_cli.client import TodoistAPIError
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                instance = MockClient.return_value
                instance.close_task.side_effect = TodoistAPIError("404")
                sys.argv = ["todoist", "close", "bad-id"]
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TestStart
# ---------------------------------------------------------------------------

from datetime import datetime, timezone

START_DT = datetime(2026, 6, 17, 9, 0, 0, tzinfo=timezone.utc)
END_DT = datetime(2026, 6, 17, 9, 30, 0, tzinfo=timezone.utc)

TASK_A = {"id": "t-a", "content": "Standup with team", "priority": 4,
          "due": {"date": "2026-06-17"}, "deadline": None}
TASK_B = {"id": "t-b", "content": "Standup notes", "priority": 1,
          "due": None, "deadline": None}


class TestStart:
    def test_unconfigured_account_exits_before_auth_probe(self, capsys, monkeypatch):
        monkeypatch.delenv("GOG_ACCOUNT")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}), \
             patch("todoist_cli.cli.TodoistClient") as MockClient, \
             patch("todoist_cli.cli.tracker.gog_auth_ok") as auth_ok:
            sys.argv = ["todoist", "start", "standup"]
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
        auth_ok.assert_not_called()
        MockClient.return_value.get_open_tasks_by_filter.assert_not_called()
        assert "GOG_ACCOUNT" in capsys.readouterr().err

    def test_unparseable_plan_toml_is_named_not_called_missing(self, capsys, monkeypatch, tmp_path):
        monkeypatch.delenv("GOG_ACCOUNT")
        monkeypatch.setenv("TODOIST_CLI_CONFIG_DIR", str(tmp_path))
        (tmp_path / "plan.toml").write_text("account = \n")
        with patch("todoist_cli.cli.tracker.gog_auth_ok") as auth_ok:
            sys.argv = ["todoist", "start", "standup"]
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
        auth_ok.assert_not_called()
        err = capsys.readouterr().err
        assert "could not read" in err and "no Google account" not in err

    def test_auth_failure_exits_before_fetch(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient:
                with patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=False):
                    sys.argv = ["todoist", "start", "standup"]
                    with pytest.raises(SystemExit) as exc:
                        cli.main()
        assert exc.value.code == 1
        instance = MockClient.return_value
        instance.get_open_tasks_by_filter.assert_not_called()
        err = capsys.readouterr().err
        assert cli.tracker.REAUTH_COMMAND in err

    def test_search_term_builds_query(self):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event", return_value=(True, "")) as mk, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                cli.main()
        instance.get_open_tasks_by_filter.assert_called_once_with("search: standup")
        mk.assert_called_once()
        assert mk.call_args[0][0] == "Standup with team"

    def test_search_term_metachars_neutralized(self):
        """A phrase with filter operators must not be parsed as filter syntax."""
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event", return_value=(True, "")), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "a | b (c)"]
                cli.main()
        instance.get_open_tasks_by_filter.assert_called_once_with("search: a   b  c")

    def test_calendar_event_includes_task_link(self):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event", return_value=(True, "")) as mk, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                cli.main()
        assert mk.call_args.kwargs["description"] == "https://app.todoist.com/app/task/t-a"

    def test_ctrl_c_during_timer_aborts_without_event(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", side_effect=KeyboardInterrupt), \
                 patch("todoist_cli.cli.tracker.create_calendar_event") as mk, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        mk.assert_not_called()
        assert "Aborted" in capsys.readouterr().err

    def test_no_term_prefetches_and_windows_by_priority(self):
        """No-term path prefetches the superset, then keeps only in-window tasks."""
        # today (from START_DT) = 2026-06-17.
        keep = {"id": "k", "content": "P1 in window", "priority": 4,
                "due": {"date": "2026-06-19"}, "deadline": None}  # today+2, P1 window 3
        drop = {"id": "d", "content": "P1 out of window", "priority": 4,
                "due": {"date": "2026-06-20"}, "deadline": None}  # today+3, outside
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.select_task") as mock_select, \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event", return_value=(True, "")) as mk, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [drop, keep]
                sys.argv = ["todoist", "start"]
                cli.main()
        instance.get_open_tasks_by_filter.assert_called_once_with(cli._START_PREFILTER)
        # Only the in-window task survives, so the picker is skipped and it is tracked.
        mock_select.assert_not_called()
        assert mk.call_args[0][0] == "P1 in window"

    def test_no_term_all_filtered_out_exits_1(self, capsys):
        out = {"id": "o", "content": "P3 tomorrow", "priority": 2,
               "due": {"date": "2026-06-18"}, "deadline": None}  # today+1, default window 1
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [out]
                sys.argv = ["todoist", "start"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "lookahead window" in capsys.readouterr().err

    def test_no_matches_exits_1(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True):
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = []
                sys.argv = ["todoist", "start", "nope"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "No open tasks matching 'nope'" in capsys.readouterr().err

    def test_single_match_skips_picker(self):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.select_task") as mock_select, \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event", return_value=(True, "")), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                cli.main()
        mock_select.assert_not_called()

    def test_multiple_matches_invoke_picker(self):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.select_task", return_value=TASK_B) as mock_select, \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event", return_value=(True, "")) as mk, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A, TASK_B]
                sys.argv = ["todoist", "start", "standup"]
                cli.main()
        mock_select.assert_called_once()
        assert mk.call_args[0][0] == "Standup notes"

    def test_cancelled_picker_exits_1(self):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.select_task", return_value=None):
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A, TASK_B]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1

    def test_calendar_failure_exits_1_and_prints_times(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(False, "boom")), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "boom" in err
        assert "log manually" in err

    def test_unrecognized_calendar_failure_suggests_no_reauth(self, capsys):
        # A reauth hint for a failure a reauth can't fix would send the user
        # down the wrong path, so only diagnosed errors get next steps.
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(False, "calendar is read-only")), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit):
                    cli.main()
        err = capsys.readouterr().err
        assert cli.tracker.REAUTH_COMMAND not in err

    def test_expired_token_midsession_prints_reauth_and_retry_command(self, capsys):
        # The up-front check can pass and the token still die during the timer;
        # this is the exact failure that motivated the hint.
        expired = (
            'Post "https://www.googleapis.com/calendar/v3/calendars/primary/events": '
            'round trip: base token source: oauth2: "invalid_grant" '
            '"Token has been expired or revoked."'
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(False, expired)), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert expired in err  # raw error is never swallowed
        assert cli.tracker.REAUTH_COMMAND in err
        # ...and the tracked session is still recoverable afterwards.
        assert "gog calendar create" in err
        assert TASK_A["content"] in err

    def test_expired_token_midsession_never_prompts_when_not_a_tty(self, capsys):
        # A scheduled or piped run has nobody to approve a browser consent
        # screen; it has to fail with instructions, not hang on a prompt.
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(False, "invalid_grant")), \
                 patch("todoist_cli.cli.tracker.run_reauth") as reauth, \
                 patch("builtins.input") as mock_input, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                MockClient.return_value.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit):
                    cli.main()
        mock_input.assert_not_called()
        reauth.assert_not_called()

    def test_reauth_at_the_prompt_writes_the_event_after_all(self, capsys):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.select_event_color", return_value=None), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       side_effect=[(False, "invalid_grant"), (True, "created")]) as create, \
                 patch("todoist_cli.cli.tracker.run_reauth", return_value=True) as reauth, \
                 patch("builtins.input", return_value="y"), \
                 patch("todoist_cli.cli.datetime") as mock_dt, \
                 stdin_p, stdout_p:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                MockClient.return_value.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                cli.main()  # no SystemExit: the session was salvaged
        reauth.assert_called_once()
        assert create.call_count == 2
        # The retry logs the same interval, not a fresh "now".
        assert create.call_args_list[0][0] == create.call_args_list[1][0]
        assert "Logged to calendar" in capsys.readouterr().out

    def test_no_reauth_offer_for_a_non_auth_failure(self):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.select_event_color", return_value=None), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(False, "calendar is read-only")), \
                 patch("todoist_cli.cli.tracker.run_reauth") as reauth, \
                 patch("builtins.input") as mock_input, \
                 patch("todoist_cli.cli.datetime") as mock_dt, \
                 stdin_p, stdout_p:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                MockClient.return_value.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit):
                    cli.main()
        mock_input.assert_not_called()
        reauth.assert_not_called()

    def test_upfront_auth_failure_offers_reauth_and_continues(self):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", side_effect=[False, True]), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.select_event_color", return_value=None), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(True, "created")), \
                 patch("todoist_cli.cli.tracker.run_reauth", return_value=True), \
                 patch("builtins.input", return_value="y"), \
                 patch("todoist_cli.cli.datetime") as mock_dt, \
                 stdin_p, stdout_p:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                MockClient.return_value.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                cli.main()
                MockClient.return_value.get_open_tasks_by_filter.assert_called_once()

    def test_failed_reauth_still_exits_1(self, capsys):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=False), \
                 patch("todoist_cli.cli.tracker.run_reauth", return_value=False), \
                 patch("builtins.input", return_value="y"), \
                 stdin_p, stdout_p:
                MockClient.return_value.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        MockClient.return_value.get_open_tasks_by_filter.assert_not_called()

    def test_reauth_that_does_not_restore_access_exits_1(self):
        # `gog-reauth` exiting 0 isn't proof the token works; re-probe before
        # continuing, or the command fails again a step later.
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=False) as auth_ok, \
                 patch("todoist_cli.cli.tracker.run_reauth", return_value=True), \
                 patch("builtins.input", return_value="y"), \
                 stdin_p, stdout_p:
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert auth_ok.call_count == 2  # probed again after the reauth
        MockClient.return_value.get_open_tasks_by_filter.assert_not_called()

    def test_retry_command_carries_the_chosen_color(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.select_event_color", return_value=7), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=(False, "invalid_grant")), \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                instance = MockClient.return_value
                instance.get_open_tasks_by_filter.return_value = [TASK_A]
                sys.argv = ["todoist", "start", "standup"]
                with pytest.raises(SystemExit):
                    cli.main()
        err = capsys.readouterr().err
        assert "--event-color 7" in err


class TestLog:
    """`log` shares start's timer/calendar path but never touches Todoist."""

    def _run(self, argv, create_result=(True, "")):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.run_timer", return_value=END_DT), \
                 patch("todoist_cli.cli.tracker.select_event_color", return_value=None), \
                 patch("todoist_cli.cli.tracker.create_calendar_event",
                       return_value=create_result) as mk, \
                 patch("todoist_cli.cli.datetime") as mock_dt:
                mock_dt.now.return_value.astimezone.return_value = START_DT
                sys.argv = ["todoist", "log", *argv]
                cli.main()
        return MockClient, mk

    def test_logs_unquoted_title_without_touching_todoist(self, capsys):
        MockClient, mk = self._run(["client", "call"])
        MockClient.assert_not_called()
        mk.assert_called_once_with(
            "client call", START_DT, END_DT, description=None, account=TEST_ACCOUNT,
            event_color=None,
        )
        assert "Logged to calendar" in capsys.readouterr().out

    def test_description_goes_on_the_event(self):
        _, mk = self._run(["Acme call", "-d", "intro with CTO"])
        assert mk.call_args[0][0] == "Acme call"
        assert mk.call_args.kwargs["description"] == "intro with CTO"

    def test_blank_title_exits_before_tracking(self, capsys):
        with patch("todoist_cli.cli.tracker.gog_auth_ok") as auth, \
             patch("todoist_cli.cli.tracker.run_timer") as timer:
            sys.argv = ["todoist", "log", "  "]
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
        auth.assert_not_called()
        timer.assert_not_called()
        assert "title" in capsys.readouterr().err

    def test_missing_title_is_a_usage_error(self):
        sys.argv = ["todoist", "log"]
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2

    def test_auth_failure_exits_before_timer(self, capsys):
        with patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=False), \
             patch("todoist_cli.cli.tracker.run_timer") as timer:
            sys.argv = ["todoist", "log", "client call"]
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
        timer.assert_not_called()
        assert cli.tracker.REAUTH_COMMAND in capsys.readouterr().err

    def test_ctrl_c_during_timer_aborts_without_event(self, capsys):
        with patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
             patch("todoist_cli.cli.tracker.run_timer", side_effect=KeyboardInterrupt), \
             patch("todoist_cli.cli.tracker.create_calendar_event") as mk, \
             patch("todoist_cli.cli.datetime") as mock_dt:
            mock_dt.now.return_value.astimezone.return_value = START_DT
            sys.argv = ["todoist", "log", "client call"]
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
        mk.assert_not_called()

    def test_calendar_failure_prints_recoverable_session(self, capsys):
        with pytest.raises(SystemExit) as exc:
            self._run(["client call", "-d", "notes"], create_result=(False, "boom"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "boom" in err
        assert "log manually" in err and "| client call" in err
        assert "gog calendar create" in err and "notes" in err


class TestSortKey:
    def test_soonest_date_wins_over_higher_priority(self):
        # The whole point of the date-first order: what's due now comes first,
        # even when something less urgent-by-label is scheduled further out.
        urgent_later = {"content": "u", "priority": 4, "due": {"date": "2026-06-20"}}
        normal_today = {"content": "n", "priority": 1, "due": {"date": "2026-06-17"}}
        tasks = [urgent_later, normal_today]
        tasks.sort(key=cli._sort_key)
        assert tasks == [normal_today, urgent_later]

    def test_priority_breaks_a_same_date_tie(self):
        normal = {"content": "n", "priority": 1, "due": {"date": "2026-06-17"}}
        urgent = {"content": "u", "priority": 4, "due": {"date": "2026-06-17"}}
        tasks = [normal, urgent]
        tasks.sort(key=cli._sort_key)
        assert tasks == [urgent, normal]

    def test_deadline_only_task_sorts_on_its_deadline(self):
        early_due = {"content": "a", "priority": 1, "due": {"date": "2026-06-15"}}
        deadline_only = {"content": "b", "priority": 1, "deadline": {"date": "2026-06-17"}}
        late_due = {"content": "c", "priority": 1, "due": {"date": "2026-06-20"}}
        tasks = [late_due, deadline_only, early_due]
        tasks.sort(key=cli._sort_key)
        assert tasks == [early_due, deadline_only, late_due]

    def test_earlier_deadline_outranks_the_tasks_own_due_date(self):
        # Due Jun 20 but the deadline lands Jun 15 → it sorts at Jun 15.
        squeezed = {
            "content": "a", "priority": 1,
            "due": {"date": "2026-06-20"}, "deadline": {"date": "2026-06-15"},
        }
        other = {"content": "b", "priority": 1, "due": {"date": "2026-06-17"}}
        tasks = [other, squeezed]
        tasks.sort(key=cli._sort_key)
        assert tasks == [squeezed, other]

    def test_dateless_tasks_sort_last(self):
        dateless = {"content": "a", "priority": 4}
        dated = {"content": "z", "priority": 1, "due": {"date": "2026-06-20"}}
        tasks = [dateless, dated]
        tasks.sort(key=cli._sort_key)
        assert tasks == [dated, dateless]

    def test_timestamped_due_ties_with_a_date_only_same_day(self):
        # Only the date portion counts, so priority decides — not the timestamp.
        timestamped = {"content": "a", "priority": 4, "due": {"date": "2026-06-17T15:00:00"}}
        date_only = {"content": "b", "priority": 1, "due": {"date": "2026-06-17"}}
        tasks = [date_only, timestamped]
        tasks.sort(key=cli._sort_key)
        assert tasks == [timestamped, date_only]

    def test_content_breaks_tie_case_insensitively(self):
        # Identical date/priority → fall back to case-insensitive content.
        a = {"content": "banana", "priority": 2}
        b = {"content": "Apple", "priority": 2}
        c = {"content": "cherry", "priority": 2}
        tasks = [a, b, c]
        tasks.sort(key=cli._sort_key)
        assert tasks == [b, a, c]

    def test_null_due_and_deadline_fields_are_treated_as_absent(self):
        # The API sends explicit nulls rather than omitting the keys.
        nulls = {"content": "a", "priority": 1, "due": None, "deadline": None}
        assert cli._effective_date(nulls) == "9999-12-31"


class TestWithinWindow:
    from datetime import date as _date
    TODAY = _date(2026, 6, 17)

    def _due(self, d):
        return {"content": "x", "due": {"date": d}, "deadline": None}

    def _deadline(self, d):
        return {"content": "x", "due": None, "deadline": {"date": d}}

    def test_p1_window_keeps_today_plus_2_drops_today_plus_3(self):
        assert cli._within_window(self._due("2026-06-19"), 3, self.TODAY)
        assert not cli._within_window(self._due("2026-06-20"), 3, self.TODAY)

    def test_p2_window_keeps_today_plus_1_drops_today_plus_2(self):
        assert cli._within_window(self._due("2026-06-18"), 2, self.TODAY)
        assert not cli._within_window(self._due("2026-06-19"), 2, self.TODAY)

    def test_default_window_keeps_today_drops_tomorrow(self):
        assert cli._within_window(self._due("2026-06-17"), 1, self.TODAY)
        assert not cli._within_window(self._due("2026-06-18"), 1, self.TODAY)

    def test_overdue_qualifies_in_every_window(self):
        overdue = self._due("2026-06-01")
        for days in (1, 2, 3):
            assert cli._within_window(overdue, days, self.TODAY)

    def test_deadline_alone_qualifies(self):
        assert cli._within_window(self._deadline("2026-06-17"), 1, self.TODAY)
        assert not cli._within_window(self._deadline("2026-06-18"), 1, self.TODAY)

    def test_datetime_valued_due_compares_date_portion(self):
        task = {"content": "x", "due": {"date": "2026-06-17T15:00:00"}, "deadline": None}
        assert cli._within_window(task, 1, self.TODAY)

    def test_no_due_or_deadline_never_qualifies(self):
        assert not cli._within_window(
            {"content": "x", "due": None, "deadline": None}, 3, self.TODAY
        )


# ---------------------------------------------------------------------------
# TestPlan
# ---------------------------------------------------------------------------

PLAN_TZ = ZoneInfo("America/Los_Angeles")
# Tuesday, before working hours, so today's capacity isn't clamped by "now".
PLAN_FIXED_NOW = datetime(2026, 7, 28, 8, 0, tzinfo=PLAN_TZ)


class _FixedDateTime(datetime):
    """A datetime subclass with `.now()` pinned, so cmd_plan's date math is
    deterministic while `combine`/arithmetic/comparisons stay real (inherited).
    """

    @classmethod
    def now(cls, tz=None):
        return PLAN_FIXED_NOW.astimezone(tz) if tz else PLAN_FIXED_NOW


# Tue Jul 28 -> Wed 29, Thu 30, Fri 31, Mon Aug 3, Tue Aug 4.
PLAN_BUCKETS = planner.weekday_buckets(PLAN_FIXED_NOW.date(), 7)


def _interactive():
    """Patches making cmd_plan see a TTY. Applied inline (not as a fixture) —
    capsys reinstalls its own stdout proxy at the start of the test's "call"
    phase, after fixture setup runs, which would silently discard a
    fixture-time monkeypatch of sys.stdout.isatty.
    """
    return (
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
    )


def _plan_task(
    id, content, priority=1, due_date=None, deadline=None, project_id=None, is_recurring=False,
    labels=None,
):
    task = {"id": id, "content": content, "priority": priority, "is_completed": False}
    if project_id is not None:
        task["project_id"] = project_id
    if due_date is not None or is_recurring:
        task["due"] = {"date": due_date}
        if is_recurring:
            task["due"]["is_recurring"] = True
    if deadline is not None:
        task["deadline"] = {"date": deadline}
    if labels is not None:
        task["labels"] = labels
    return task


def _fully_booked():
    """A busy list covering every planned day end to end, so nothing can be
    placed and every eligible task falls through to at-risk/unscheduled.
    """
    return [
        (
            datetime.combine(d, datetime.min.time(), PLAN_FIXED_NOW.tzinfo),
            datetime.combine(d, datetime.max.time(), PLAN_FIXED_NOW.tzinfo),
        )
        for d in PLAN_BUCKETS
    ]


def _plan_client(MockClient, tasks, projects=None):
    instance = MockClient.return_value
    instance.get_all_open_tasks.return_value = tasks
    instance.get_projects.return_value = projects or [
        {"id": "inbox-1", "name": "Inbox", "inbox_project": True}
    ]
    return instance


class TestTaskDueDeadline:
    def test_due_string_preferred_over_raw_date(self):
        task = {"due": {"string": "every Monday", "date": "2026-07-27"}}
        assert cli._task_due_deadline(task) == ("every Monday", "-")

    def test_falls_back_to_the_date_when_there_is_no_string(self):
        assert cli._task_due_deadline({"due": {"date": "2026-07-27"}})[0] == "2026-07-27"

    def test_deadline_date_shown(self):
        assert cli._task_due_deadline({"deadline": {"date": "2026-07-30"}})[1] == "2026-07-30"

    def test_absent_fields_render_as_dashes(self):
        assert cli._task_due_deadline({}) == ("-", "-")

    def test_empty_values_render_as_dashes(self):
        task = {"due": {"string": "", "date": ""}, "deadline": {"date": ""}}
        assert cli._task_due_deadline(task) == ("-", "-")


class TestFormatTaskRow:
    def _item(self, content, **kw):
        return planner.Item(
            id=kw.get("id", "t1"), content=content, priority=kw.get("priority", 2),
            due_date=kw.get("due_date"), deadline=kw.get("deadline"),
            minutes=kw.get("minutes", 30), pinned=kw.get("pinned", False),
        )

    def test_size_shown_when_the_title_has_no_hint(self):
        row = cli._format_task_row(self._item("Call the dentist", minutes=90), {}, date(2026, 7, 28))
        assert "1h30m" in row

    def test_size_omitted_when_the_title_already_carries_it(self):
        # "Call the dentist 15m" would otherwise print "15m 15m".
        row = cli._format_task_row(
            self._item("Call the dentist 15m", minutes=15), {}, date(2026, 7, 28)
        )
        assert row.count("15m") == 1

    def test_uncolored_by_default(self):
        # The default has to be plain: the non-interactive path relies on it.
        row = cli._format_task_row(
            self._item("Call the dentist", priority=1, due_date=date(2026, 7, 28),
                       deadline=date(2026, 7, 28)),
            {}, date(2026, 7, 28),
        )
        assert "\x1b[" not in row

    def test_deadline_today_is_red(self):
        # Same rule as the `start` picker (tracker._date_suffix).
        row = cli._format_task_row(
            self._item("Call the dentist", deadline=date(2026, 7, 28)),
            {}, date(2026, 7, 28), color=True,
        )
        assert "\x1b[31m(deadline Jul 28)\x1b[39m" in row

    def test_past_deadline_is_red(self):
        row = cli._format_task_row(
            self._item("Call the dentist", deadline=date(2026, 7, 25)),
            {}, date(2026, 7, 28), color=True,
        )
        assert "\x1b[31m(deadline Jul 25)\x1b[39m" in row

    def test_pinned_row_shows_its_own_date_not_an_arrow(self):
        # An overdue pinned task is scheduled on today so its capacity is
        # counted, but its date is never rewritten — an arrow would claim a
        # write that never happens.
        row = cli._format_task_row(
            self._item("Weekly review", due_date=date(2026, 7, 25), pinned=True),
            {"t1": date(2026, 7, 28)}, date(2026, 7, 28),
        )
        assert "->" not in row
        assert "(Jul 25)" in row
        assert "(pinned)" in row

    def test_future_deadline_is_not_red(self):
        row = cli._format_task_row(
            self._item("Call the dentist", deadline=date(2026, 7, 30)),
            {}, date(2026, 7, 28), color=True,
        )
        assert "(deadline Jul 30)" in row
        assert "\x1b[31m" not in row


class TestPlan:
    def test_auth_failure_exits_before_fetch(self, capsys):
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=False):
                sys.argv = ["todoist", "plan"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        MockClient.return_value.get_all_open_tasks.assert_not_called()
        assert cli.tracker.REAUTH_COMMAND in capsys.readouterr().err

    def test_non_interactive_without_yes_aborts(self, capsys, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True):
                sys.argv = ["todoist", "plan"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "--yes" in capsys.readouterr().err
        MockClient.return_value.get_all_open_tasks.assert_not_called()

    def test_calendar_fetch_error_exits_1(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", side_effect=RuntimeError("boom")), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 stdin_p, stdout_p:
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "boom" in err
        assert cli.tracker.REAUTH_COMMAND not in err  # undiagnosable → no hint
        MockClient.return_value.get_all_open_tasks.assert_not_called()

    def test_expired_token_on_freebusy_prints_reauth_and_offers_it(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient"), \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy",
                       side_effect=RuntimeError('oauth2: "invalid_grant" "Token has been '
                                                'expired or revoked."')), \
                 patch("todoist_cli.cli.tracker.run_reauth") as reauth, \
                 patch("builtins.input", return_value="n"), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 stdin_p, stdout_p:
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert cli.tracker.REAUTH_COMMAND in capsys.readouterr().err
        reauth.assert_not_called()  # declined at the prompt

    def test_reauth_at_the_prompt_retries_the_calendar_read(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        expired = RuntimeError('oauth2: "invalid_grant" "Token has been expired or revoked."')
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy",
                       side_effect=[expired, []]) as fetch, \
                 patch("todoist_cli.cli.tracker.run_reauth", return_value=True) as reauth, \
                 patch("builtins.input", side_effect=["y", "q"]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 stdin_p, stdout_p:
                MockClient.return_value.get_all_open_tasks.return_value = []
                MockClient.return_value.get_projects.return_value = []
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit):
                    cli.main()
        reauth.assert_called_once()
        assert fetch.call_count == 2  # the read is retried, not abandoned

    def test_quit_at_capacity_confirmation(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("builtins.input", side_effect=["q"]), \
                 stdin_p, stdout_p:
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        MockClient.return_value.get_all_open_tasks.assert_not_called()

    def test_quit_at_plan_approval(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Task", priority=4, due_date="2026-08-05", project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("builtins.input", side_effect=["", "q"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        instance.update_task.assert_not_called()

    def test_dry_run_computes_but_does_not_write(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Write report", priority=4, due_date="2026-08-05", project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--dry-run", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        instance.update_task.assert_not_called()
        assert "Dry run" in capsys.readouterr().out

    def test_approve_writes_computed_dates_and_skips_pinned(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task_p1 = _plan_task("p1", "Ship the thing", priority=4, due_date="2026-08-10", project_id="inbox-1")
        recurring = _plan_task(
            "rec", "Weekly review", priority=1,
            due_date=PLAN_BUCKETS[0].isoformat(), is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task_p1, recurring])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        # Due Aug 10 is past the end of the horizon, so the due-date pass anchors it
        # on the last bucket rather than dragging it forward to today.
        instance.update_task.assert_called_once_with("p1", due_date=PLAN_BUCKETS[-1].isoformat())

    def test_overdue_recurring_task_is_shown_on_today_and_not_rewritten(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        overdue_recurring = _plan_task(
            "rec", "Weekly review", priority=1,
            due_date="2026-07-21", is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[overdue_recurring])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        instance.update_task.assert_not_called()

    def test_overdue_recurring_task_appears_in_the_printed_plan(self, tmp_path, capsys):
        stdin_p, stdout_p = _interactive()
        overdue_recurring = _plan_task(
            "rec", "Weekly review", priority=1,
            due_date="2026-07-21", is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[overdue_recurring])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml"), "--dry-run"]
                cli.main()

        out = capsys.readouterr().out
        first_day = PLAN_BUCKETS[0].strftime("%A, %B %-d")
        after_first_day = out.split(first_day + ":", 1)[1]
        next_day = PLAN_BUCKETS[1].strftime("%A, %B %-d")
        assert "Weekly review" in after_first_day.split(next_day, 1)[0]

    def test_weekend_recurring_task_is_reported_not_dropped(self, tmp_path, capsys):
        # PLAN_BUCKETS runs Tue Jul 28 -> Tue Aug 4, so Sat Aug 1 sits inside
        # the horizon but isn't one of the planned days.
        stdin_p, stdout_p = _interactive()
        weekend = _plan_task(
            "sat", "Saturday chore", priority=1,
            due_date="2026-08-01", is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[weekend])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        out = capsys.readouterr().out
        assert "Not planned" in out
        assert "Saturday chore" in out
        assert "not a working day" in out
        # Reported only. Dates and labels both go through update_task, so no
        # call at all proves it was neither re-dated nor tagged at-risk.
        instance.update_task.assert_not_called()

    def test_pinned_task_past_the_horizon_is_reported_with_its_reason(self, tmp_path, capsys):
        stdin_p, stdout_p = _interactive()
        # Eligible (inside eligibility_days) but dated past the last bucket.
        later = _plan_task(
            "later", "Next week standup", priority=1,
            due_date="2026-08-10", is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[later])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        out = capsys.readouterr().out
        assert "Next week standup" in out
        assert "past the horizon" in out
        instance.update_task.assert_not_called()

    def test_no_unplanned_section_when_every_pinned_task_fits(self, tmp_path, capsys):
        stdin_p, stdout_p = _interactive()
        recurring = _plan_task(
            "rec", "Weekly review", priority=1,
            due_date=PLAN_BUCKETS[0].isoformat(), is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[recurring])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        assert "Not planned" not in capsys.readouterr().out

    def test_task_already_correctly_dated_is_not_rewritten(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Small task", priority=4, due_date=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        instance.update_task.assert_not_called()

    def test_edit_overrides_computed_date(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Edit me", priority=3, due_date="2026-08-05", project_id="inbox-1")
        override_date = PLAN_BUCKETS[2].isoformat()
        edited_text = f"t1\t{override_date}\tP2\t30m\tEdit me\n"
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.tracker.open_in_editor", return_value=edited_text) as mock_editor, \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "e", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        mock_editor.assert_called_once()
        instance.update_task.assert_called_once_with("t1", due_date=override_date)

    def test_update_failure_warns_and_continues(self, capsys, tmp_path):
        from todoist_cli.client import TodoistAPIError

        stdin_p, stdout_p = _interactive()
        t1 = _plan_task("t1", "Task one", priority=4, due_date="2026-08-05", project_id="inbox-1")
        t2 = _plan_task("t2", "Task two", priority=4, due_date="2026-08-06", project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[t1, t2])
                instance.update_task.side_effect = [TodoistAPIError("500 boom"), {"id": "t2"}]
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()

        assert exc.value.code == 1
        assert instance.update_task.call_count == 2  # the run finished
        err = capsys.readouterr().err
        assert "could not update" in err
        assert "1 of 2 update(s) failed" in err

    def test_inbox_only_task_included_other_project_excluded(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        inbox_task = _plan_task("in1", "Inbox item", priority=4, project_id="inbox-1")
        other_task = _plan_task("out1", "Other item", priority=4, project_id="proj-other")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(
                    MockClient,
                    tasks=[inbox_task, other_task],
                    projects=[
                        {"id": "inbox-1", "name": "Inbox", "inbox_project": True},
                        {"id": "proj-other", "name": "Other", "inbox_project": False},
                    ],
                )
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()

        instance.update_task.assert_called_once_with("in1", due_date=PLAN_BUCKETS[0].isoformat())

    def test_yes_flag_skips_all_prompts(self, tmp_path):
        task = _plan_task("t1", "Task", priority=4, due_date="2026-08-05", project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input") as mock_input:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--yes", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        mock_input.assert_not_called()
        instance.update_task.assert_called_once()

    def test_interactive_output_is_colored_and_bolded(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        # Due today (PLAN_FIXED_NOW's date), so the title should bold.
        task = _plan_task(
            "t1", "Urgent thing", priority=4, due_date=PLAN_FIXED_NOW.date().isoformat(), project_id="inbox-1"
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert "\x1b[31mP1\x1b[39m" in out  # P1 -> red foreground
        assert "\x1b[1mUrgent thing\x1b[0m" in out  # bold, due today
        assert "**" not in out  # no leftover markdown-style fake bold

    def test_non_interactive_output_has_no_escape_codes(self, capsys, tmp_path):
        task = _plan_task(
            "t1", "Urgent thing", priority=4, due_date=PLAN_FIXED_NOW.date().isoformat(), project_id="inbox-1"
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path):
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--yes", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert "\x1b[" not in out

    def test_at_risk_section_lists_scheduled_but_late_task(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        # 10h of work against a 5h/day calendar, deadline today: it gets scheduled,
        # but can't finish until tomorrow, so it belongs in At risk as well as on
        # its day.
        task = _plan_task(
            "t1", "Big late thing [10h]", priority=2,
            deadline=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert "At risk:" in out
        # Once under its scheduled day, once under At risk.
        assert out.count("Big late thing") == 2

    def test_at_risk_task_gets_atrisk_label_applied(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task(
            "t1", "Big late thing [10h]", priority=2,
            deadline=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        instance.update_task.assert_any_call("t1", labels=["atrisk"])

    def test_resolved_task_has_atrisk_label_removed(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task(
            "t1", "Small task", priority=4,
            due_date=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
            labels=["atrisk", "work"],
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        # Due date is already correct (no due-date rewrite), so the only
        # update_task call should be the label removal, preserving "work".
        instance.update_task.assert_called_once_with("t1", labels=["work"])

    def test_label_status_already_correct_is_not_touched(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task(
            "t1", "Big late thing [10h]", priority=2,
            deadline=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
            labels=["atrisk"],
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        # The due date may still be rewritten (this task has no due_date to
        # begin with), but since it's already labeled and already at risk,
        # no call should touch its labels.
        label_calls = [c for c in instance.update_task.call_args_list if "labels" in c.kwargs]
        assert label_calls == []

    def test_dry_run_previews_label_changes(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task(
            "t1", "Big late thing [10h]", priority=2,
            deadline=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--dry-run", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        instance.update_task.assert_not_called()
        out = capsys.readouterr().out
        assert "Label changes that would be made" in out
        assert "Add 'atrisk': Big late thing [10h]" in out

    def test_change_arrow_shown_for_moved_task(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Move me", priority=4, due_date="2026-08-05", project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert f"(Aug 5 -> {PLAN_BUCKETS[-1].strftime('%b %-d')})" in out

    def test_summary_counts_updates_and_skips(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        moved = _plan_task("moved", "Move me", priority=4, due_date="2026-08-05", project_id="inbox-1")
        already = _plan_task(
            "same", "Leave me", priority=4,
            due_date=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[moved, already])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert "Updated 1 task(s); 1 unchanged. Labels changed on 0 task(s)." in out

    def test_summary_counts_label_changes(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        # 10h of work against a 5h/day calendar with today's deadline: at risk,
        # so it earns the atrisk label.
        task = _plan_task(
            "t1", "Big late thing [10h]", priority=2,
            deadline=PLAN_FIXED_NOW.date().isoformat(), project_id="inbox-1", labels=[],
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        assert "Labels changed on 1 task(s)." in capsys.readouterr().out

    def test_day_with_no_work_says_so(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        # One small task lands on the first bucket; the later days stay empty.
        task = _plan_task(
            "t1", "Small task", priority=4,
            due_date=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert out.count("(nothing scheduled)") == len(PLAN_BUCKETS) - 1

    def test_unscheduled_summary_counts_by_priority(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        # A fully booked calendar, so undated Inbox work can't be placed at all.
        busy = [
            (
                datetime.combine(d, datetime.min.time(), PLAN_FIXED_NOW.tzinfo),
                datetime.combine(d, datetime.max.time(), PLAN_FIXED_NOW.tzinfo),
            )
            for d in PLAN_BUCKETS
        ]
        tasks = [
            _plan_task("a", "Undated one", priority=4, project_id="inbox-1"),
            _plan_task("b", "Undated two", priority=4, project_id="inbox-1"),
            _plan_task("c", "Undated three", priority=1, project_id="inbox-1"),
        ]
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=busy), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=tasks)
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        out = capsys.readouterr().out
        assert "Not scheduled: 3 task(s) — 2 P1, 1 P4" in out

    def test_calendar_window_covers_the_whole_last_bucket_day(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Small task", priority=4, project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]) as fetch, \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        _account, _cals, window_start, window_end = fetch.call_args[0]
        assert window_start.date() == PLAN_BUCKETS[0]
        # Exclusive end at midnight *after* the last day, so that day's events count.
        assert window_end.date() == PLAN_BUCKETS[-1] + timedelta(days=1)

    @staticmethod
    def _plan_with_config(tmp_path, account="work@example.com"):
        """Run an approved plan against a plan.toml naming `account`; return
        the (gog_auth_ok, fetch_busy) mocks."""
        stdin_p, stdout_p = _interactive()
        config = tmp_path / "plan.toml"
        config.write_text(f'account = "{account}"\n')
        task = _plan_task("t1", "Small task", priority=4, project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True) as auth_ok, \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]) as fetch, \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(config)]
                cli.main()
        return auth_ok, fetch

    def test_calendar_account_comes_from_the_config(self, tmp_path, monkeypatch):
        # No GOG_ACCOUNT and no default plan.toml: the --config file alone must
        # be enough, for both the up-front auth probe and the free/busy read.
        monkeypatch.delenv("GOG_ACCOUNT")
        auth_ok, fetch = self._plan_with_config(tmp_path)
        auth_ok.assert_called_once_with("work@example.com")
        assert fetch.call_args[0][0] == "work@example.com"

    def test_config_account_beats_gog_account_env(self, tmp_path):
        # conftest sets GOG_ACCOUNT; the file `plan` actually loaded still wins.
        auth_ok, fetch = self._plan_with_config(tmp_path)
        auth_ok.assert_called_once_with("work@example.com")
        assert fetch.call_args[0][0] == "work@example.com"

    def test_plan_without_any_account_exits_before_auth_probe(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("GOG_ACCOUNT")
        with patch("todoist_cli.cli.tracker.gog_auth_ok") as auth_ok:
            sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
        auth_ok.assert_not_called()
        assert "no Google account" in capsys.readouterr().err

    def test_calendar_account_falls_back_to_the_gog_default(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Small task", priority=4, project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]) as fetch, \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        assert fetch.call_args[0][0] == TEST_ACCOUNT

    def test_plan_log_records_the_scheduled_tasks(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Logged task", priority=4, due_date="2026-08-05", project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        logs = list(tmp_path.glob("plan-*.txt"))
        assert len(logs) == 1
        # new date, then the old due date, so a bad apply can be undone
        assert f"t1\t{PLAN_BUCKETS[-1].isoformat()}\t2026-08-05\tLogged task" in logs[0].read_text()

    def test_a_piped_stdout_alone_makes_the_run_non_interactive(self, capsys, monkeypatch):
        # Both streams must be a terminal; a redirected stdout is enough to
        # require --yes, since the approval prompt would go nowhere useful.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True):
                sys.argv = ["todoist", "plan"]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "--yes" in capsys.readouterr().err
        MockClient.return_value.get_all_open_tasks.assert_not_called()


class TestPlanOverdueDates:
    """A due date says when you plan to work on something, so no plan run may
    leave one in the past. Work the packer places is today-or-later already;
    these cover the tasks it can't place, against a fully booked calendar.
    """

    OVERDUE = "2026-07-20"
    # Where work with no deadline is parked: the first weekday past the horizon.
    BEYOND = planner.next_weekday(PLAN_BUCKETS[-1])

    def _run(self, tasks, tmp_path, *, argv_extra=(), inputs=("", "y"), projects=None, editor=None):
        stdin_p, stdout_p = _interactive()
        editor_p = (
            patch("todoist_cli.cli.tracker.open_in_editor", return_value=editor)
            if editor is not None
            else patch("todoist_cli.cli.tracker.open_in_editor")
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=_fully_booked()), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=list(inputs)), \
                 editor_p, stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=tasks, projects=projects)
                sys.argv = [
                    "todoist", "plan", *argv_extra, "--config", str(tmp_path / "nope.toml")
                ]
                cli.main()
        return instance

    def test_overdue_task_that_fits_is_scheduled_today_not_parked(self, tmp_path):
        # The common case, on a free calendar: the packer anchors overdue work
        # on the first bucket, and parking must not override that.
        stdin_p, stdout_p = _interactive()
        task = _plan_task("t1", "Overdue thing", priority=2, due_date=self.OVERDUE, project_id="inbox-1")
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[task])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        instance.update_task.assert_called_once_with("t1", due_date=PLAN_BUCKETS[0].isoformat())

    def test_overdue_task_that_fits_nowhere_is_parked_past_the_horizon(self, tmp_path):
        task = _plan_task("t1", "Overdue thing", priority=2, due_date=self.OVERDUE, project_id="inbox-1")
        instance = self._run([task], tmp_path)
        instance.update_task.assert_any_call("t1", due_date=self.BEYOND.isoformat())
        # Parked, not scheduled — it still carries the label saying so.
        instance.update_task.assert_any_call("t1", labels=["atrisk"])

    def test_overdue_task_with_a_deadline_is_parked_on_the_last_day(self, tmp_path):
        task = _plan_task(
            "t1", "Overdue thing", priority=2, due_date=self.OVERDUE,
            deadline="2026-08-20", project_id="inbox-1",
        )
        instance = self._run([task], tmp_path)
        instance.update_task.assert_any_call("t1", due_date=PLAN_BUCKETS[-1].isoformat())

    def test_parking_shows_an_arrow_and_writes_nothing_in_a_dry_run(self, capsys, tmp_path):
        task = _plan_task("t1", "Overdue thing", priority=2, due_date=self.OVERDUE, project_id="inbox-1")
        instance = self._run([task], tmp_path, argv_extra=["--dry-run"])
        instance.update_task.assert_not_called()
        assert f"(Jul 20 -> {self.BEYOND.strftime('%b %-d')})" in capsys.readouterr().out

    def test_editor_dash_still_parks_an_overdue_task(self, tmp_path):
        # "-" means "the plan isn't scheduling this", not "leave it in the past".
        task = _plan_task("t1", "Overdue thing", priority=2, due_date=self.OVERDUE, project_id="inbox-1")
        instance = self._run(
            [task], tmp_path, inputs=["", "e", "y"], editor="t1\t-\tP2\t30m\tOverdue thing\n"
        )
        instance.update_task.assert_any_call("t1", due_date=self.BEYOND.isoformat())

    def test_future_dated_task_that_fits_nowhere_keeps_its_date(self, tmp_path):
        task = _plan_task(
            "t1", "Later thing", priority=2,
            due_date=PLAN_BUCKETS[2].isoformat(), project_id="inbox-1",
        )
        instance = self._run([task], tmp_path)
        assert [c for c in instance.update_task.call_args_list if "due_date" in c.kwargs] == []

    def test_task_due_today_that_fits_nowhere_is_parked_too(self, tmp_path):
        # Left on today, it would show today holding work it has no time for.
        task = _plan_task(
            "t1", "Today thing", priority=2,
            due_date=PLAN_FIXED_NOW.date().isoformat(), project_id="inbox-1",
        )
        instance = self._run([task], tmp_path)
        instance.update_task.assert_any_call("t1", due_date=self.BEYOND.isoformat())

    def test_overdue_recurring_task_is_not_parked(self, tmp_path):
        # Writing due_date would drop the recurrence pattern, and parking it at
        # all would skip the occurrences in between. It fits nowhere, so it is
        # at risk — the label is the only change.
        task = _plan_task(
            "rec", "Weekly review", priority=1,
            due_date=self.OVERDUE, is_recurring=True, project_id="inbox-1",
        )
        task["due"]["string"] = "every! 3 days"
        instance = self._run([task], tmp_path)
        assert [c for c in instance.update_task.call_args_list if "due_date" in c.kwargs] == []
        instance.reschedule_recurring.assert_not_called()

    def test_overdue_task_outside_the_inbox_is_parked_too(self, tmp_path):
        # The overdue due date alone makes it eligible; the project is irrelevant.
        task = _plan_task("t1", "Overdue thing", priority=2, due_date=self.OVERDUE, project_id="proj-other")
        instance = self._run(
            [task], tmp_path,
            projects=[
                {"id": "inbox-1", "name": "Inbox", "inbox_project": True},
                {"id": "proj-other", "name": "Other", "inbox_project": False},
            ],
        )
        instance.update_task.assert_any_call("t1", due_date=self.BEYOND.isoformat())

    def test_undated_task_that_fits_nowhere_is_not_given_a_date(self, tmp_path):
        task = _plan_task("t1", "Someday thing", priority=2, project_id="inbox-1")
        instance = self._run([task], tmp_path)
        assert [c for c in instance.update_task.call_args_list if "due_date" in c.kwargs] == []

    def test_parked_date_is_recorded_in_the_plan_log(self, tmp_path):
        task = _plan_task("t1", "Overdue thing", priority=2, due_date=self.OVERDUE, project_id="inbox-1")
        self._run([task], tmp_path)
        log = next(iter(tmp_path.glob("plan-*.txt")))
        assert f"t1\t{self.BEYOND.isoformat()}\t{self.OVERDUE}\tOverdue thing" in log.read_text()


class TestPlanLogAndFailures:
    def _run(self, tmp_path, tasks, side_effect=None):
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=tasks)
                if side_effect is not None:
                    instance.update_task.side_effect = side_effect
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                try:
                    cli.main()
                    code = 0
                except SystemExit as e:
                    code = e.code
        return instance, code

    def test_label_failure_exits_nonzero(self, tmp_path, capsys):
        from todoist_cli.client import TodoistAPIError
        t = _plan_task("t1", "Small task", priority=4, due_date=PLAN_BUCKETS[0].isoformat(),
                       project_id="inbox-1", labels=["atrisk", "work"])
        instance, code = self._run(tmp_path, [t], side_effect=TodoistAPIError("boom"))
        assert code == 1
        assert "1 of 1 update(s) failed" in capsys.readouterr().err
        assert instance.update_task.called

    def test_plan_log_contains_old_labels_and_new_labels(self, tmp_path):
        t = _plan_task("t1", "Small task", priority=4, due_date=PLAN_BUCKETS[0].isoformat(),
                       project_id="inbox-1", labels=["atrisk", "work"])
        self._run(tmp_path, [t])
        text = next(iter(tmp_path.glob("plan-*.txt"))).read_text()
        label_lines = [l for l in text.splitlines() if l.startswith("LABELS\t")]
        assert label_lines, text
        _, tid, old, new, content = label_lines[0].split("\t")
        assert tid == "t1" and json.loads(old) == ["atrisk", "work"]
        assert json.loads(new) == ["work"]

    def test_log_line_is_one_line_even_with_tabs_in_title(self, tmp_path):
        item = planner.Item("t9", "a\tb\nc", 1, date(2026, 8, 1), None, 30, False)
        with patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path):
            path = cli._write_plan_log({"t9": date(2026, 8, 2)}, {"t9": item})
        assert "t9\t2026-08-02\t2026-08-01\ta b c" in path.read_text().splitlines()

    def test_no_old_due_is_recorded_as_dash(self, tmp_path):
        item = planner.Item("t9", "x", 1, None, None, 30, False)
        with patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path):
            path = cli._write_plan_log({"t9": date(2026, 8, 2)}, {"t9": item})
        assert "t9\t2026-08-02\t-\tx" in path.read_text().splitlines()

    def test_exit_if_failed(self, capsys):
        cli._exit_if_failed(0, 5)  # no exit
        with pytest.raises(SystemExit) as exc:
            cli._exit_if_failed(2, 5)
        assert exc.value.code == 1
        assert "2 of 5 update(s) failed" in capsys.readouterr().err


class TestPlanHelpers:
    def test_read_choice_auto_yes_skips_input(self):
        with patch("builtins.input", side_effect=AssertionError):
            assert cli._read_choice(True) == "y"

    def test_read_choice_eof_quits(self):
        with patch("builtins.input", side_effect=EOFError):
            assert cli._read_choice(False) == "q"

    def test_read_choice_normalises(self):
        with patch("builtins.input", return_value="  E "):
            assert cli._read_choice(False) == "e"

    def test_editor_edits_apply_remove_and_warn(self, capsys):
        item = planner.Item("t1", "x", 1, None, None, 30, False)
        final = {"t1": date(2026, 8, 1), "t2": date(2026, 8, 1)}
        with patch("todoist_cli.cli.planner.parse_editor_text", return_value=(
                {"t1": date(2026, 8, 3), "ghost": date(2026, 8, 3), "t2": None}, ["bad line"])):
            cli._apply_editor_edits("text", {"t1": item, "t2": item}, final)
        assert final == {"t1": date(2026, 8, 3)}
        err = capsys.readouterr().err
        assert "bad line" in err and "unknown task id 'ghost'" in err

    def test_invalid_choice_reprompts_then_approves(self, capsys):
        args = argparse.Namespace(yes=False)
        result = planner.PlanResult([], [], []) if False else MagicMock()
        with patch("todoist_cli.cli.planner.earliest_scheduled_dates", return_value={"t1": date(2026, 8, 3)}), \
             patch("todoist_cli.cli.planner.park_overdue_dates", side_effect=lambda d, *a: d), \
             patch("todoist_cli.cli._print_schedule"), \
             patch("builtins.input", side_effect=["zzz", "y"]):
            out = cli._approve_plan(args, False, [], {}, result, [date(2026, 8, 3)], {}, date(2026, 8, 3))
        assert out == {"t1": date(2026, 8, 3)}
        assert "Please answer y, e, or q." in capsys.readouterr().out

    def test_read_busy_retries_after_reauth(self):
        cfg = MagicMock(calendars=["c"])
        calls = [RuntimeError("auth"), ["busy"]]
        with patch("todoist_cli.cli.tracker.fetch_busy", side_effect=calls), \
             patch("todoist_cli.cli.tracker.gog_error_help", return_value="help"), \
             patch("todoist_cli.cli.tracker.gog_error_kind", return_value="k"), \
             patch("todoist_cli.cli.tracker.REAUTHABLE_KINDS", {"k"}), \
             patch("todoist_cli.cli._offer_reauth", return_value=True):
            assert cli._read_busy("a", cfg, None, None) == ["busy"]

    def test_read_busy_exits_when_retry_also_fails(self):
        cfg = MagicMock(calendars=["c"])
        with patch("todoist_cli.cli.tracker.fetch_busy", side_effect=RuntimeError("auth")), \
             patch("todoist_cli.cli.tracker.gog_error_help", return_value=""), \
             patch("todoist_cli.cli.tracker.gog_error_kind", return_value="k"), \
             patch("todoist_cli.cli.tracker.REAUTHABLE_KINDS", {"k"}), \
             patch("todoist_cli.cli._offer_reauth", return_value=True):
            with pytest.raises(SystemExit) as exc:
                cli._read_busy("a", cfg, None, None)
        assert exc.value.code == 1


class TestMainArgparse:
    def test_no_subcommand_is_an_error(self):
        sys.argv = ["todoist"]
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2

    def test_search_requires_a_label(self):
        sys.argv = ["todoist", "search"]
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2


class TestPlanExitPaths:
    def test_unreadable_config_exits_1(self, capsys, tmp_path):
        bad = tmp_path / "plan.toml"
        bad.write_text("this is not = valid toml [[[\n")
        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 stdin_p, stdout_p:
                sys.argv = ["todoist", "plan", "--config", str(bad)]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "could not load" in capsys.readouterr().err
        MockClient.return_value.get_all_open_tasks.assert_not_called()

    def test_horizon_with_no_weekdays_exits_1(self, capsys, tmp_path):
        # A zero-day horizon starting on a Saturday leaves no weekday buckets.
        config = tmp_path / "plan.toml"
        config.write_text("horizon_days = 0\n")
        saturday_now = datetime(2026, 8, 1, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        class _SaturdayNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return saturday_now.astimezone(tz) if tz else saturday_now

        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.datetime", _SaturdayNow), \
                 stdin_p, stdout_p:
                sys.argv = ["todoist", "plan", "--config", str(config)]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "no weekdays" in capsys.readouterr().err
        MockClient.return_value.get_all_open_tasks.assert_not_called()

    def test_task_fetch_error_exits_1(self, capsys, tmp_path):
        from todoist_cli.client import TodoistAPIError

        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("builtins.input", side_effect=[""]), \
                 stdin_p, stdout_p:
                MockClient.return_value.get_all_open_tasks.side_effect = TodoistAPIError("nope")
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "nope" in capsys.readouterr().err

    def test_pinned_tasks_are_counted_as_unchanged(self, capsys, tmp_path):
        stdin_p, stdout_p = _interactive()
        recurring = _plan_task(
            "rec", "Weekly review", priority=1,
            due_date=PLAN_BUCKETS[0].isoformat(), is_recurring=True, project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[recurring])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        assert "Updated 0 task(s); 1 unchanged." in capsys.readouterr().out

    def test_plan_log_is_ordered_by_date_not_task_id(self, tmp_path):
        stdin_p, stdout_p = _interactive()
        # "zzz" lands earlier than "aaa", so id order and date order disagree.
        early = _plan_task(
            "zzz", "Early task", priority=4,
            due_date=PLAN_BUCKETS[0].isoformat(), project_id="inbox-1",
        )
        later = _plan_task(
            "aaa", "Later task", priority=4,
            due_date=PLAN_BUCKETS[3].isoformat(), project_id="inbox-1",
        )
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                _plan_client(MockClient, tasks=[early, later])
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                cli.main()
        body = list(tmp_path.glob("plan-*.txt"))[0].read_text()
        assert body.index("Early task") < body.index("Later task")


# ---------------------------------------------------------------------------
# TestDeadlineValidatorProperties
#
# The parametrized cases above pin the interesting known-bad inputs. These
# properties cover the shape of the whole input space instead: whatever the
# validator lets through must be a real calendar date in exactly the form the
# API accepts, and everything else must exit(1) rather than reach the network.
# ---------------------------------------------------------------------------

from hypothesis import given, settings, strategies as st


def _validate(raw):
    """Run the validator, capturing stderr; return the result or None on exit."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            return cli._validated_deadline(raw)
        except SystemExit as exc:
            assert exc.code == 1
            return None


class TestDeadlineValidatorProperties:
    @given(st.text())
    @settings(max_examples=2000)
    def test_accepted_input_is_always_a_canonical_iso_date(self, raw):
        out = _validate(raw)
        if out is None:
            return
        assert out == raw, "validator must not rewrite what it accepts"
        assert datetime.strptime(out, "%Y-%m-%d").strftime("%Y-%m-%d") == out

    @given(st.dates())
    def test_every_real_date_round_trips(self, d):
        # No real calendar date should ever be rejected — a false negative here
        # would block a legitimate deadline.
        iso = d.isoformat()
        assert _validate(iso) == iso

    @given(st.dates())
    def test_unpadded_dates_are_rejected(self, d):
        # strptime accepts "2026-8-17"; the API wants the canonical form, so the
        # validator must not.
        unpadded = f"{d.year:04d}-{d.month}-{d.day}"
        if unpadded != d.isoformat():
            assert _validate(unpadded) is None

    @given(st.dates(), st.text(alphabet=" \t\n\r", min_size=1))
    def test_surrounding_whitespace_is_rejected(self, d, pad):
        iso = d.isoformat()
        assert _validate(pad + iso) is None
        assert _validate(iso + pad) is None


class TestPlanRecurringWriteBack:
    # Same harness as the log/failure tests: a free calendar, approve as-is.
    _run = TestPlanLogAndFailures._run

    def _overdue(self, rule, labels=None):
        task = _plan_task(
            "rec", "Weekly review", priority=2,
            due_date="2026-07-21", is_recurring=True, project_id="inbox-1", labels=labels,
        )
        task["due"]["string"] = rule
        return task

    def test_a_driftless_rule_is_moved_with_its_rule_kept(self, tmp_path):
        instance, code = self._run(tmp_path, [self._overdue("every mon")])
        assert code == 0
        instance.reschedule_recurring.assert_called_once_with(
            "rec", PLAN_BUCKETS[0].isoformat(), "every mon"
        )
        assert [c for c in instance.update_task.call_args_list if "due_date" in c.kwargs] == []

    def test_an_interval_rule_is_moved_in_the_plan_only(self, tmp_path, capsys):
        instance, _ = self._run(tmp_path, [self._overdue("every 3 days")])
        instance.reschedule_recurring.assert_not_called()
        instance.update_task.assert_not_called()
        out = capsys.readouterr().out
        assert "(recurring, plan only)" in out
        assert "Updated 0 task(s); 1 unchanged." in out

    def test_a_fixed_recurring_task_is_never_moved(self, tmp_path, capsys):
        instance, _ = self._run(tmp_path, [self._overdue("every! 3 days", labels=["fixed"])])
        instance.reschedule_recurring.assert_not_called()
        instance.update_task.assert_not_called()
        assert "(pinned)" in capsys.readouterr().out

    def test_a_failed_reschedule_is_reported_and_exits_nonzero(self, tmp_path, capsys):
        from todoist_cli.client import TodoistAPIError

        stdin_p, stdout_p = _interactive()
        with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
            with patch("todoist_cli.cli.TodoistClient") as MockClient, \
                 patch("todoist_cli.cli.tracker.gog_auth_ok", return_value=True), \
                 patch("todoist_cli.cli.tracker.fetch_busy", return_value=[]), \
                 patch("todoist_cli.cli.datetime", _FixedDateTime), \
                 patch("todoist_cli.cli._PLAN_LOG_DIR", tmp_path), \
                 patch("builtins.input", side_effect=["", "y"]), \
                 stdin_p, stdout_p:
                instance = _plan_client(MockClient, tasks=[self._overdue("every mon")])
                instance.reschedule_recurring.side_effect = TodoistAPIError("sync said no")
                sys.argv = ["todoist", "plan", "--config", str(tmp_path / "nope.toml")]
                with pytest.raises(SystemExit) as exc:
                    cli.main()
        assert exc.value.code == 1
        assert "sync said no" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# TestFilter / TestComment
# ---------------------------------------------------------------------------

FILTER_TASK = {
    "id": "t-shoes",
    "content": "Get shoes repaired",
    "description": "Brown boots",
    "labels": ["errand"],
    "priority": 3,
    "due": {"date": "2026-11-12", "string": "Nov 12"},
    "deadline": None,
}


def _run_cli(argv, setup):
    with patch.dict("os.environ", {"TODOIST_API_TOKEN": "test-token"}):
        with patch("todoist_cli.cli.TodoistClient") as MockClient:
            instance = MockClient.return_value
            setup(instance)
            sys.argv = ["todoist", *argv]
            cli.main()
    return instance


class TestFilter:
    def test_prints_matching_tasks(self, capsys):
        def setup(c):
            c.get_open_tasks_by_filter.return_value = [dict(FILTER_TASK)]
        instance = _run_cli(["filter", "today | overdue"], setup)

        instance.get_open_tasks_by_filter.assert_called_once_with("today | overdue")
        instance.get_task_comments.assert_not_called()
        out = capsys.readouterr().out
        assert "[t-shoes] P2 | due: Nov 12" in out
        assert "Brown boots" in out
        assert "labels: errand" in out

    def test_comments_flag_prints_comments(self, capsys):
        def setup(c):
            c.get_open_tasks_by_filter.return_value = [dict(FILTER_TASK)]
            c.get_task_comments.return_value = [{"content": "Call 555-0100"}]
        instance = _run_cli(["filter", "today", "--comments"], setup)

        instance.get_task_comments.assert_called_once_with("t-shoes")
        assert "comment: Call 555-0100" in capsys.readouterr().out

    def test_json_includes_comments(self, capsys):
        def setup(c):
            c.get_open_tasks_by_filter.return_value = [dict(FILTER_TASK)]
            c.get_task_comments.return_value = [{"content": "x"}]
        _run_cli(["filter", "today", "--comments", "--json"], setup)

        data = json.loads(capsys.readouterr().out)
        assert data[0]["id"] == "t-shoes"
        assert data[0]["comments"] == [{"content": "x"}]

    def test_json_empty_prints_list(self, capsys):
        _run_cli(["filter", "today", "--json"],
                 lambda c: setattr(c.get_open_tasks_by_filter, "return_value", []))
        assert json.loads(capsys.readouterr().out) == []

    def test_no_results(self, capsys):
        _run_cli(["filter", "today"],
                 lambda c: setattr(c.get_open_tasks_by_filter, "return_value", []))
        assert "No open tasks match 'today'" in capsys.readouterr().out

    def test_api_error_exits_1(self):
        from todoist_cli.client import TodoistAPIError

        def setup(c):
            c.get_open_tasks_by_filter.side_effect = TodoistAPIError("400")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(["filter", "bad("], setup)
        assert exc_info.value.code == 1


class TestComment:
    def test_adds_comment(self, capsys):
        instance = _run_cli(
            ["comment", "t-shoes", "**Cobbler:** 555-0100"],
            lambda c: setattr(c.add_comment, "return_value", {"id": "c-9"}),
        )
        instance.add_comment.assert_called_once_with("t-shoes", "**Cobbler:** 555-0100")
        assert "Added comment c-9 to task t-shoes" in capsys.readouterr().out

    def test_api_error_exits_1(self):
        from todoist_cli.client import TodoistAPIError

        def setup(c):
            c.add_comment.side_effect = TodoistAPIError("404")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(["comment", "nope", "x"], setup)
        assert exc_info.value.code == 1
