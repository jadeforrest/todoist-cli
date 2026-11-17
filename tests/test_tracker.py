"""Unit tests for tracker.py — terminal/subprocess plumbing isolated and mocked."""

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from todoist_cli import tracker

from tests.conftest import TEST_ACCOUNT as ACCOUNT


class TestKeyAction:
    def test_arrow_up(self):
        assert tracker._key_action(b"\x1b[A") == "up"
        assert tracker._key_action(b"\x1bOA") == "up"
        assert tracker._key_action(b"k") == "up"

    def test_arrow_down(self):
        assert tracker._key_action(b"\x1b[B") == "down"
        assert tracker._key_action(b"\x1bOB") == "down"
        assert tracker._key_action(b"j") == "down"

    def test_select(self):
        assert tracker._key_action(b"\r") == "select"
        assert tracker._key_action(b"\n") == "select"

    def test_cancel(self):
        assert tracker._key_action(b"\x1b") == "cancel"
        assert tracker._key_action(b"\x03") == "cancel"
        assert tracker._key_action(b"q") == "cancel"

    def test_unknown(self):
        assert tracker._key_action(b"x") == "none"


class TestFormatElapsed:
    def test_under_a_minute(self):
        assert tracker.format_elapsed(timedelta(seconds=5)) == "00:05"

    def test_under_an_hour_omits_hours(self):
        assert tracker.format_elapsed(timedelta(minutes=23, seconds=45)) == "23:45"

    def test_hours_minutes_seconds(self):
        assert tracker.format_elapsed(timedelta(hours=2, minutes=3, seconds=4)) == "02:03:04"


class TestFormatRow:
    def test_includes_priority_due_deadline_content(self):
        task = {
            "content": "Write report",
            "priority": 4,  # API urgent -> P1
            "due": {"date": "2026-06-17", "string": "today"},
            "deadline": {"date": "2026-06-20"},
        }
        row = tracker.format_row(task)
        assert "P1" in row
        assert "today" in row  # due string preferred over ISO date
        assert "deadline Jun 20" in row
        assert "Write report" in row

    def test_title_after_priority_with_date_in_parens(self):
        task = {
            "content": "Read article",
            "priority": 3,  # -> P2
            "due": {"string": "Jun 17"},
        }
        assert tracker.format_row(task) == "P2 Read article (Jun 17)"

    def test_bold_title_wraps_only_the_content_when_due_today(self):
        task = {"content": "Read article", "priority": 3, "due": {"string": "today"}}
        row = tracker.format_row(task, bold_title=True)
        assert row == "P2 \x1b[1mRead article\x1b[22m (today)"

    def test_bold_title_skipped_when_not_due_or_deadline_today(self):
        task = {"content": "Read article", "priority": 3, "due": {"string": "Jun 17"}}
        row = tracker.format_row(task, bold_title=True)
        assert row == "P2 Read article (Jun 17)"

    def test_bold_title_when_deadline_today(self):
        today = tracker.datetime.now().astimezone().date().isoformat()
        task = {"content": "Ship it", "priority": 1, "deadline": {"date": today}}
        row = tracker.format_row(task, bold_title=True)
        assert row.startswith("P4 \x1b[1mShip it\x1b[22m")

    def test_iso_deadline_only_is_formatted(self):
        task = {"content": "Loose task", "priority": 1, "deadline": {"date": "2026-06-20"}}
        assert tracker.format_row(task) == "P4 Loose task (deadline Jun 20)"

    def test_missing_due_and_deadline_omits_parens(self):
        task = {"content": "Loose task", "priority": 1}
        row = tracker.format_row(task)
        assert row == "P4 Loose task"
        assert "(" not in row

    def test_color_wraps_priority_tag_only(self):
        task = {"content": "Urgent", "priority": 4, "due": {"string": "today"}}
        row = tracker.format_row(task, color=True)
        # P1 -> red foreground, closed with default-foreground (not full reset).
        assert row == "\x1b[31mP1\x1b[39m Urgent (today)"

    def test_color_leaves_p4_untouched(self):
        task = {"content": "Normal", "priority": 1}  # API 1 -> P4
        assert tracker.format_row(task, color=True) == "P4 Normal"

    def test_deadline_today_is_red(self):
        today = datetime.now().astimezone().date()
        task = {"content": "Ship it", "priority": 1, "deadline": {"date": today.isoformat()}}
        row = tracker.format_row(task, color=True)
        assert f"\x1b[31mdeadline {tracker.format_date(today)}\x1b[39m" in row

    def test_past_deadline_is_red(self):
        past = datetime.now().astimezone().date() - timedelta(days=3)
        task = {"content": "Ship it", "priority": 1, "deadline": {"date": past.isoformat()}}
        row = tracker.format_row(task, color=True)
        assert f"\x1b[31mdeadline {tracker.format_date(past)}\x1b[39m" in row

    def test_future_deadline_is_not_red(self):
        future = datetime.now().astimezone().date() + timedelta(days=3)
        task = {"content": "Ship it", "priority": 1, "deadline": {"date": future.isoformat()}}
        row = tracker.format_row(task, color=True)
        assert row == f"P4 Ship it (deadline {tracker.format_date(future)})"

    def test_deadline_today_stays_plain_without_color(self):
        # The numbered fallback renders unstyled rows; red must not leak into it.
        today = datetime.now().astimezone().date()
        task = {"content": "Ship it", "priority": 1, "deadline": {"date": today.isoformat()}}
        assert "\x1b[" not in tracker.format_row(task)

    def test_red_deadline_composes_with_bold_title_and_colored_tag(self):
        today = datetime.now().astimezone().date()
        task = {
            "content": "Ship it", "priority": 4,
            "due": {"date": today.isoformat(), "string": "today"},
            "deadline": {"date": today.isoformat()},
        }
        row = tracker.format_row(task, bold_title=True, color=True)
        assert row == (
            f"\x1b[31mP1\x1b[39m \x1b[1mShip it\x1b[22m "
            f"(today, \x1b[31mdeadline {tracker.format_date(today)}\x1b[39m)"
        )


class TestPriorityTag:
    def test_pads_single_digit(self):
        assert tracker.priority_tag(1) == "P1"

    def test_color_wraps_p1_p2_p3(self):
        assert tracker.priority_tag(1, color=True) == "\x1b[31mP1\x1b[39m"
        assert tracker.priority_tag(2, color=True) == "\x1b[33mP2\x1b[39m"
        assert tracker.priority_tag(3, color=True) == "\x1b[34mP3\x1b[39m"

    def test_color_leaves_p4_uncolored(self):
        assert tracker.priority_tag(4, color=True) == "P4"

    def test_no_color_flag_never_colors(self):
        assert tracker.priority_tag(1, color=False) == "P1"


class TestFormatDate:
    def test_formats_month_day(self):
        assert tracker.format_date(date(2026, 6, 17)) == "Jun 17"

    def test_single_digit_day_no_leading_zero(self):
        assert tracker.format_date(date(2026, 6, 4)) == "Jun 4"


class TestDatesIncludeToday:
    def test_true_when_due_is_today(self):
        today = date(2026, 7, 28)
        assert tracker.dates_include_today(today, None, today=today) is True

    def test_true_when_deadline_is_today(self):
        today = date(2026, 7, 28)
        assert tracker.dates_include_today(None, today, today=today) is True

    def test_false_when_neither_is_today(self):
        today = date(2026, 7, 28)
        assert tracker.dates_include_today(date(2026, 7, 27), date(2026, 7, 29), today=today) is False

    def test_none_values_ignored(self):
        today = date(2026, 7, 28)
        assert tracker.dates_include_today(None, None, today=today) is False

    def test_defaults_to_real_today_when_unspecified(self):
        real_today = datetime.now().astimezone().date()
        assert tracker.dates_include_today(real_today, None) is True


class TestDeadlineIsDue:
    TODAY = date(2026, 7, 28)

    def test_today_is_due(self):
        assert tracker.deadline_is_due(self.TODAY, today=self.TODAY) is True

    def test_past_is_due(self):
        assert tracker.deadline_is_due(date(2026, 7, 27), today=self.TODAY) is True

    def test_future_is_not_due(self):
        assert tracker.deadline_is_due(date(2026, 7, 29), today=self.TODAY) is False

    def test_none_is_not_due(self):
        assert tracker.deadline_is_due(None, today=self.TODAY) is False

    def test_defaults_to_real_today_when_unspecified(self):
        assert tracker.deadline_is_due(datetime.now().astimezone().date()) is True


class TestDeadlineIsDueFromTask:
    def test_missing_deadline_key(self):
        assert tracker._deadline_is_due({"content": "x"}) is False

    def test_null_deadline_field(self):
        # The API sends an explicit null rather than omitting the key.
        assert tracker._deadline_is_due({"content": "x", "deadline": None}) is False

    def test_empty_deadline_date(self):
        assert tracker._deadline_is_due({"content": "x", "deadline": {"date": ""}}) is False

    def test_timestamped_deadline_today_is_due(self):
        today = datetime.now().astimezone().date().isoformat()
        task = {"content": "x", "deadline": {"date": f"{today}T15:00:00"}}
        assert tracker._deadline_is_due(task) is True

    def test_future_deadline_is_not_due(self):
        future = (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
        assert tracker._deadline_is_due({"content": "x", "deadline": {"date": future}}) is False


class TestSelectNumberedFallback:
    def test_numbered_selection(self, capsys):
        tasks = [{"content": "A", "priority": 1}, {"content": "B", "priority": 1}]
        with patch("builtins.input", return_value="2"):
            chosen = tracker._select_numbered(tasks)
        assert chosen is tasks[1]

    def test_cancel_with_q(self):
        tasks = [{"content": "A", "priority": 1}]
        with patch("builtins.input", return_value="q"):
            assert tracker._select_numbered(tasks) is None

    def test_reprompts_on_invalid(self, capsys):
        tasks = [{"content": "A", "priority": 1}]
        with patch("builtins.input", side_effect=["9", "1"]):
            chosen = tracker._select_numbered(tasks)
        assert chosen is tasks[0]


class TestSelectTaskInterrupt:
    def test_ctrl_c_returns_none(self):
        # Ctrl-C in either picker branch should cancel cleanly, not raise.
        tasks = [{"content": "A", "priority": 1}]
        with patch.object(tracker, "_select_arrows", side_effect=KeyboardInterrupt), \
             patch.object(tracker, "_select_numbered", side_effect=KeyboardInterrupt):
            assert tracker.select_task(tasks) is None


class TestNextIndex:
    def test_down_advances(self):
        assert tracker._next_index(0, 3, "down") == 1

    def test_down_wraps_at_end(self):
        assert tracker._next_index(2, 3, "down") == 0

    def test_up_wraps_at_start(self):
        assert tracker._next_index(0, 3, "up") == 2

    def test_unknown_action_keeps_index(self):
        assert tracker._next_index(1, 3, "none") == 1


class TestScrollOffset:
    def test_fits_returns_zero(self):
        # List shorter than the viewport never scrolls.
        assert tracker._scroll_offset(2, 3, 5, 0) == 0

    def test_scroll_down_past_bottom_edge(self):
        # idx moves below the window bottom -> window follows down by one.
        assert tracker._scroll_offset(5, 10, 5, 0) == 1

    def test_no_scroll_within_window(self):
        # idx still visible in the current window -> offset unchanged.
        assert tracker._scroll_offset(3, 10, 5, 1) == 1

    def test_scroll_up_past_top_edge(self):
        # idx moves above the window top -> window follows up to idx.
        assert tracker._scroll_offset(1, 10, 5, 4) == 1

    def test_clamped_at_bottom(self):
        # Offset can never exceed count - height.
        assert tracker._scroll_offset(9, 10, 5, 8) == 5

    def test_wrap_to_end_lands_on_last_window(self):
        # Wrapping up from the top (idx -> count-1) jumps to the final window.
        assert tracker._scroll_offset(9, 10, 5, 0) == 5


class TestTruncateVisible:
    def test_plain_text_cut_to_width(self):
        assert tracker._truncate_visible("abcdefgh", 3) == "abc"

    def test_shorter_than_width_is_untouched(self):
        assert tracker._truncate_visible("ab", 5) == "ab"

    def test_escapes_do_not_count_toward_the_width(self):
        # Three visible characters kept, and the escape that made them bold is
        # carried along rather than counted as width.
        text = f"{tracker._BOLD_ON}abcdefgh"
        assert tracker._truncate_visible(text, 3) == f"{tracker._BOLD_ON}abc{tracker._RESET}"

    def test_styled_text_is_reset_so_the_cut_cannot_bleed(self):
        # Cutting mid-bold would otherwise leave the rest of the screen bold.
        out = tracker._truncate_visible(f"{tracker._BOLD_ON}abcdef{tracker._BOLD_OFF}", 3)
        assert out.endswith(tracker._RESET)

    def test_unstyled_text_gets_no_trailing_reset(self):
        assert tracker._RESET not in tracker._truncate_visible("abcdef", 3)


class TestViewportHeight:
    def test_reserves_three_rows(self):
        # Header, status line, and a bottom margin.
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            assert tracker._viewport_height() == 21

    def test_never_drops_below_one_row(self):
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 2))):
            assert tracker._viewport_height() == 1


class TestStatusLine:
    def test_position_is_one_based(self):
        assert tracker._status_line(0, 10, 0, 5).startswith("  [1/10]")

    def test_top_of_a_scrollable_list_offers_only_below(self):
        line = tracker._status_line(0, 10, 0, 5)
        assert "↑ more above" not in line
        assert "↓ more below" in line

    def test_bottom_of_a_scrollable_list_offers_only_above(self):
        line = tracker._status_line(6, 10, 5, 5)
        assert line == "  [7/10] ↑ more above"

    def test_middle_of_a_scrollable_list_offers_both(self):
        line = tracker._status_line(4, 12, 2, 5)
        assert "↑ more above" in line
        assert "↓ more below" in line


def _make_state(height=10):
    return tracker._PickerState(height)


class TestPickerLoop:
    def test_select_returns_current(self):
        tasks = [{"content": "A"}, {"content": "B"}, {"content": "C"}]
        keys = iter([b"\x1b[B", b"\x1b[B", b"\r"])  # down, down, enter -> index 2
        chosen = tracker._picker_loop(_make_state(), tasks,
                                      read_key=lambda: next(keys),
                                      render=lambda first: None)
        assert chosen is tasks[2]

    def test_wraparound_up_from_top(self):
        tasks = [{"content": "A"}, {"content": "B"}]
        keys = iter([b"\x1b[A", b"\r"])  # up from 0 wraps to 1
        chosen = tracker._picker_loop(_make_state(), tasks,
                                      read_key=lambda: next(keys),
                                      render=lambda first: None)
        assert chosen is tasks[1]

    def test_cancel_returns_none(self):
        tasks = [{"content": "A"}]
        keys = iter([b"\x1b"])  # esc
        chosen = tracker._picker_loop(_make_state(), tasks,
                                      read_key=lambda: next(keys),
                                      render=lambda first: None)
        assert chosen is None

    def test_first_render_flag(self):
        state = _make_state()
        tasks = [{"content": "A"}]
        renders = []
        keys = iter([b"\r"])
        tracker._picker_loop(state, tasks, read_key=lambda: next(keys),
                             render=lambda first: renders.append((state.idx, first)))
        assert renders == [(0, True)]

    def test_only_the_opening_paint_is_flagged_first(self):
        # Every repaint after a keypress must overwrite the previous block; a
        # second `first=True` would print a fresh copy below it.
        state = _make_state()
        tasks = [{"content": "A"}, {"content": "B"}]
        renders = []
        keys = iter([b"\x1b[B", b"\x1b[B", b"\r"])
        tracker._picker_loop(state, tasks, read_key=lambda: next(keys),
                             render=lambda first: renders.append(first))
        assert renders == [True, False, False]

    def test_eof_returns_none_without_spinning(self):
        # Empty read = stdin EOF (closed pipe/terminal). Must return, not loop.
        tasks = [{"content": "A"}]
        chosen = tracker._picker_loop(_make_state(), tasks,
                                      read_key=lambda: b"",
                                      render=lambda first: None)
        assert chosen is None


def _strip_ansi(text):
    import re as _re
    return _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).replace("\r", "")


class TestRenderTruncation:
    def test_long_title_truncated_to_terminal_width(self, capsys):
        task = {"content": "X" * 500, "priority": 1}
        with patch("todoist_cli.tracker.shutil.get_terminal_size",
                   return_value=__import__("os").terminal_size((40, 24))):
            with patch("sys.stdout.isatty", return_value=True):
                tracker._render([task], 0, 0, 24, first=True)
        out = capsys.readouterr().out
        # No printed visible line (after stripping ANSI) should exceed the width.
        for line in out.split("\n"):
            assert len(_strip_ansi(line)) <= 40


class TestRenderWindow:
    def _tasks(self, n):
        return [{"content": f"task {i}", "priority": 1} for i in range(n)]

    def test_highlight_follows_absolute_index_when_scrolled(self, capsys):
        # 10 tasks, viewport of 4, scrolled so the window shows tasks[3:7].
        # idx=5 should be highlighted at visible row 5 - 3 = 2 (0-based).
        with patch("todoist_cli.tracker.shutil.get_terminal_size",
                   return_value=__import__("os").terminal_size((80, 24))):
            tracker._render(self._tasks(10), 5, 3, 4, first=True)
        rows = [r for r in capsys.readouterr().out.split("\n") if r]
        task_rows = rows[:4]  # the 4 viewport rows, before the status line
        highlighted = [i for i, r in enumerate(task_rows) if "\x1b[7m" in r]
        assert highlighted == [2]
        assert "task 5" in _strip_ansi(task_rows[2])

    def test_drawn_count_invariant_across_renders(self, capsys):
        # The drawn-line count (what the cursor-up move relies on) must be the same
        # for the first paint and a later repaint of the same list.
        tasks = self._tasks(10)
        with patch("todoist_cli.tracker.shutil.get_terminal_size",
                   return_value=__import__("os").terminal_size((80, 24))):
            first = tracker._render(tasks, 0, 0, 4, first=True)
            later = tracker._render(tasks, 6, 3, 4, first=False)
        assert first == later == 5  # 4 viewport rows + 1 status line

    def test_returned_count_matches_the_lines_actually_written(self, capsys):
        # The cursor-up move is driven by the return value, so drawing a
        # different number of lines than it claims would smear the redraw.
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            drawn = tracker._render(self._tasks(10), 5, 3, 4, first=True)
        rows = [r for r in capsys.readouterr().out.split("\n") if r]
        assert len(rows) == drawn

    def test_list_exactly_filling_the_viewport_has_no_status_line(self, capsys):
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            drawn = tracker._render(self._tasks(4), 0, 0, 4, first=True)
        rows = [r for r in capsys.readouterr().out.split("\n") if r]
        assert drawn == 4
        assert not any("more below" in r for r in rows)

    def test_selected_row_is_marked_and_the_others_are_indented(self, capsys):
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            tracker._render(self._tasks(3), 1, 0, 3, first=True)
        rows = [_strip_ansi(r) for r in capsys.readouterr().out.split("\n") if r]
        assert rows[0].startswith("  ")
        assert rows[1].startswith("> ")
        assert rows[2].startswith("  ")

    def test_first_paint_does_not_move_the_cursor_up(self, capsys):
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            tracker._render(self._tasks(3), 0, 0, 3, first=True)
        assert "\x1b[3A" not in capsys.readouterr().out

    def test_repaint_moves_the_cursor_up_over_the_previous_block(self, capsys):
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            tracker._render(self._tasks(3), 0, 0, 3, first=False)
        assert capsys.readouterr().out.startswith("\x1b[3A")

    def test_rows_are_bolded_and_colored_for_the_terminal(self, capsys):
        # The picker asks format_row for bold_title and color; a task due today
        # in a colorable priority should come out styled.
        today = date.today().isoformat()
        task = {"content": "Due today", "priority": 4, "due": {"date": today}}
        with patch("todoist_cli.tracker.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            tracker._render([task], 0, 0, 3, first=True)
        out = capsys.readouterr().out
        assert tracker._BOLD_ON in out  # bold_title=True
        assert tracker.priority_tag(1, color=True) in out  # color=True


class TestRunTimer:
    def test_returns_end_time_and_prints_elapsed(self, capsys):
        start = datetime(2026, 6, 17, 9, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 9, 0, 30, tzinfo=timezone.utc)
        fake_now = MagicMock()
        fake_now.now.return_value.astimezone.side_effect = [start, end]
        with patch("todoist_cli.tracker.datetime", fake_now), \
             patch("todoist_cli.tracker.select.select", return_value=([sys_stdin()], [], [])), \
             patch("sys.stdin.readline", return_value="\n"):
            result = tracker.run_timer(start)
        assert result is end
        assert "Elapsed:" in capsys.readouterr().out


def sys_stdin():
    import sys
    return sys.stdin


class TestConfiguredAccount:
    def test_env_var_wins(self, tmp_path):
        cfg = tmp_path / "plan.toml"
        cfg.write_text('account = "toml@example.com"\n')
        env = {"GOG_ACCOUNT": "env@example.com"}
        assert tracker.configured_account(env, cfg) == "env@example.com"

    def test_falls_back_to_plan_toml(self, tmp_path):
        cfg = tmp_path / "plan.toml"
        cfg.write_text('account = "toml@example.com"\n')
        assert tracker.configured_account({"GOG_ACCOUNT": ""}, cfg) == "toml@example.com"

    def test_default_path_is_the_config_dir(self, tmp_path):
        (tmp_path / "plan.toml").write_text('account = "dir@example.com"\n')
        env = {"TODOIST_CLI_CONFIG_DIR": str(tmp_path)}
        assert tracker.configured_account(env) == "dir@example.com"

    def test_plan_toml_without_account(self, tmp_path):
        cfg = tmp_path / "plan.toml"
        cfg.write_text("horizon_days = 3\n")
        assert tracker.configured_account({}, cfg) == ""

    def test_non_string_account_is_ignored(self, tmp_path):
        cfg = tmp_path / "plan.toml"
        cfg.write_text("account = 123\n")
        assert tracker.configured_account({}, cfg) == ""

    def test_missing_file(self, tmp_path):
        assert tracker.configured_account({}, tmp_path / "nope.toml") == ""

    def test_unparseable_file_raises(self, tmp_path):
        cfg = tmp_path / "plan.toml"
        cfg.write_text("account = \n")
        with pytest.raises(ValueError, match="could not read"):
            tracker.configured_account({}, cfg)

    def test_unreadable_path_raises(self, tmp_path):
        with pytest.raises(ValueError):
            tracker.configured_account({}, tmp_path)  # a directory, not a file

class TestGogAuthOk:
    def test_ok_on_zero_exit(self):
        result = MagicMock(returncode=0, stdout="primary\n", stderr="")
        with patch("subprocess.run", return_value=result):
            assert tracker.gog_auth_ok(ACCOUNT) is True

    def test_fail_on_nonzero(self):
        result = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("subprocess.run", return_value=result):
            assert tracker.gog_auth_ok(ACCOUNT) is False

    def test_fail_on_invalid_grant_text(self):
        result = MagicMock(returncode=0, stdout="", stderr="oauth2: invalid_grant")
        with patch("subprocess.run", return_value=result):
            assert tracker.gog_auth_ok(ACCOUNT) is False

    def test_fail_when_gog_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert tracker.gog_auth_ok(ACCOUNT) is False


class TestGogErrorHelp:
    # The verbatim failure `todoist start` hit after a tracked session.
    EXPIRED = (
        'ERROR creating calendar event: Post "https://www.googleapis.com/calendar/v3/'
        'calendars/primary/events?alt=json&prettyPrint=false": round trip: base token '
        'source: oauth2: "invalid_grant" "Token has been expired or revoked."'
    )

    def test_expired_token_gets_the_reauth_command(self):
        help_text = tracker.gog_error_help(self.EXPIRED, ACCOUNT)
        assert help_text is not None
        assert tracker.REAUTH_COMMAND in help_text

    @pytest.mark.parametrize("message", [
        "oauth2: invalid_grant",
        "invalid_token",
        "Token has been expired or revoked.",
        "unauthorized_client",
        "Request had invalid authentication credentials",
        "no refresh token for account",
        "googleapi: Error 401: Unauthorized",
    ])
    def test_auth_failures_are_recognized(self, message):
        assert tracker.REAUTH_COMMAND in (tracker.gog_error_help(message, ACCOUNT) or "")

    def test_account_is_named_in_the_hint(self):
        assert "someone@example.com" in tracker.gog_error_help(
            "invalid_grant", "someone@example.com"
        )

    def test_scope_failure_asks_for_calendar_scopes_not_a_plain_reauth(self):
        # A plain reauth re-mints the same insufficient scopes, so this case has
        # to be distinguished from an expired token even though both are 4xx.
        help_text = tracker.gog_error_help(
            "Request had insufficient authentication scopes (ACCESS_TOKEN_SCOPE_INSUFFICIENT)", ACCOUNT
        )
        assert "--services calendar" in help_text
        assert "scope" in help_text.lower()

    def test_missing_binary_points_at_install(self):
        help_text = tracker.gog_error_help("gog command not found on PATH", ACCOUNT)
        assert "brew install gog" in help_text
        assert tracker.REAUTH_COMMAND not in help_text

    @pytest.mark.parametrize("message", [
        "gog timed out creating the event",
        "dial tcp 142.250.72.10:443: connect: network is unreachable",
        "lookup www.googleapis.com: no such host",
    ])
    def test_network_failures_are_not_blamed_on_auth(self, message):
        help_text = tracker.gog_error_help(message, ACCOUNT)
        assert "network" in help_text.lower()
        assert tracker.REAUTH_COMMAND not in help_text

    def test_rate_limit_says_wait(self):
        help_text = tracker.gog_error_help("googleapi: Error 429: rateLimitExceeded", ACCOUNT)
        assert "rate-limit" in help_text.lower()

    @pytest.mark.parametrize("message", [
        "calendar not writable",
        "invalid event color",
        "",
    ])
    def test_undiagnosable_errors_get_no_hint(self, message):
        # Better silence than a confident wrong instruction.
        assert tracker.gog_error_help(message, ACCOUNT) is None

    def test_matching_is_case_insensitive(self):
        assert tracker.gog_error_help("OAUTH2: INVALID_GRANT", ACCOUNT) is not None


class TestGogErrorKind:
    @pytest.mark.parametrize("message,kind", [
        ('oauth2: "invalid_grant" "Token has been expired or revoked."', "auth"),
        ("ACCESS_TOKEN_SCOPE_INSUFFICIENT", "scope"),
        ("gog command not found on PATH", "missing"),
        ("googleapi: Error 429: rateLimitExceeded", "quota"),
        ("dial tcp: i/o timeout", "network"),
        ("calendar is read-only", None),
    ])
    def test_classification(self, message, kind):
        assert tracker.gog_error_kind(message) == kind

    def test_only_auth_and_scope_are_reauthable(self):
        # What the CLI keys the "offer to re-authenticate" prompt off of.
        assert set(tracker.REAUTHABLE_KINDS) == {"auth", "scope"}
        assert tracker.gog_error_kind("network is unreachable") not in tracker.REAUTHABLE_KINDS
        assert tracker.gog_error_kind("calendar is read-only") not in tracker.REAUTHABLE_KINDS


class TestRunReauth:
    def test_runs_the_wrapper_with_the_account_and_keeps_the_terminal(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            assert tracker.run_reauth("someone@example.com") is True
        assert mock_run.call_args[0][0] == [tracker.REAUTH_COMMAND, "someone@example.com"]
        # The OAuth flow is interactive: capturing its output or timing it out
        # would hide the consent URL or kill the flow mid-approval.
        assert "capture_output" not in mock_run.call_args.kwargs
        assert "timeout" not in mock_run.call_args.kwargs

    def test_nonzero_exit_is_a_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert tracker.run_reauth(ACCOUNT) is False

    def test_missing_wrapper_explains_how_to_install_it(self, capsys):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert tracker.run_reauth(ACCOUNT) is False
        err = capsys.readouterr().err
        assert "pip" in err and f"bin/{tracker.REAUTH_COMMAND}" in err

    def test_ctrl_c_during_consent_is_not_a_crash(self):
        with patch("subprocess.run", side_effect=KeyboardInterrupt()):
            assert tracker.run_reauth(ACCOUNT) is False


class TestRunReauthDetached:
    def test_keeps_stdout_clean_and_stdin_closed(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            assert tracker.run_reauth_detached(ACCOUNT, timeout=5) is True
        kwargs = mock_run.call_args.kwargs
        assert mock_run.call_args[0][0] == [tracker.REAUTH_COMMAND, ACCOUNT]
        # The Todone helper's stdout is its JSON protocol; the wrapper must not write there.
        assert kwargs["stdout"] is sys.stderr and kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 5

    def test_nonzero_exit_is_a_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert tracker.run_reauth_detached(ACCOUNT) is False

    def test_unanswered_consent_times_out(self, capsys):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gog-reauth", 1)):
            assert tracker.run_reauth_detached(ACCOUNT) is False
        assert "timed out" in capsys.readouterr().err

    def test_missing_wrapper_explains_how_to_install_it(self, capsys):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert tracker.run_reauth_detached(ACCOUNT) is False
        assert f"bin/{tracker.REAUTH_COMMAND}" in capsys.readouterr().err


class TestReauthHelp:
    def test_names_the_account_and_the_wrapper(self):
        text = tracker.reauth_help("someone@example.com")
        assert "someone@example.com" in text
        assert tracker.REAUTH_COMMAND in text

    def test_defaults_to_the_configured_account(self):
        assert ACCOUNT in tracker.reauth_help(ACCOUNT)


class TestCalendarEventCommand:
    def test_matches_what_create_calendar_event_runs(self):
        # The retry command printed on failure is only useful if it is byte-for-byte
        # what the CLI would have run itself.
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            tracker.create_calendar_event("X", start, end, description="d", event_color=3, account=ACCOUNT)
        assert mock_run.call_args[0][0] == tracker.calendar_event_command(
            "X", start, end, description="d", event_color=3, account=ACCOUNT
        )


class TestCreateCalendarEvent:
    def test_builds_free_event_command(self):
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=0, stdout="created", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            ok, message = tracker.create_calendar_event("Write report", start, end, account=ACCOUNT)

        assert ok is True
        assert message == "created"
        cmd = mock_run.call_args[0][0]
        assert cmd[:4] == ["gog", "calendar", "create", tracker.GOG_CALENDAR]
        assert "--transparency" in cmd and "free" in cmd
        assert "--summary" in cmd and "Write report" in cmd
        assert "--no-input" in cmd  # fail fast instead of hanging on a prompt
        assert start.isoformat() in cmd
        assert end.isoformat() in cmd
        assert ACCOUNT in cmd
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 30  # bounded so it can't wedge
        # Output is captured and decoded, so stdout/stderr can be reported back
        # instead of leaking onto the user's terminal.
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    def test_missing_gog_is_reported_as_a_failure(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            ok, message = tracker.create_calendar_event(
                "Write report",
                datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc), account=ACCOUNT,
            )
        assert ok is False
        assert "not found" in message

    def test_timeout_is_reported_as_a_failure(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gog", timeout=30)):
            ok, message = tracker.create_calendar_event(
                "Write report",
                datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc), account=ACCOUNT,
            )
        assert ok is False
        assert "timed out" in message

    def test_nonzero_exit_is_reported_as_a_failure(self):
        result = MagicMock(returncode=1, stdout="", stderr="calendar not writable")
        with patch("subprocess.run", return_value=result):
            ok, message = tracker.create_calendar_event(
                "Write report",
                datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc), account=ACCOUNT,
            )
        assert ok is False
        assert message == "calendar not writable"

    def test_description_passed_through(self):
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            tracker.create_calendar_event(
                "X", start, end, description="https://app.todoist.com/app/task/abc", account=ACCOUNT
            )
        cmd = mock_run.call_args[0][0]
        assert "--description" in cmd
        assert "https://app.todoist.com/app/task/abc" in cmd

    def test_no_description_flag_when_absent(self):
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            tracker.create_calendar_event("X", start, end, account=ACCOUNT)
        assert "--description" not in mock_run.call_args[0][0]

    def test_event_color_passed_through(self):
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            tracker.create_calendar_event("X", start, end, event_color=5, account=ACCOUNT)
        cmd = mock_run.call_args[0][0]
        assert "--event-color" in cmd
        assert "5" in cmd

    def test_no_event_color_flag_when_default(self):
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            tracker.create_calendar_event("X", start, end, event_color=None, account=ACCOUNT)
        assert "--event-color" not in mock_run.call_args[0][0]


class TestFetchBusy:
    def test_parses_busy_blocks(self):
        payload = {
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-07-27T16:00:00Z", "end": "2026-07-27T18:00:00Z"},
                        {"start": "2026-07-28T14:00:00Z", "end": "2026-07-28T14:40:00Z"},
                    ]
                }
            }
        }
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            busy = tracker.fetch_busy(
                "someone@example.com",
                ["primary"],
                datetime(2026, 7, 27, tzinfo=timezone.utc),
                datetime(2026, 7, 28, tzinfo=timezone.utc),
            )
        assert len(busy) == 2
        assert busy[0] == (
            datetime.fromisoformat("2026-07-27T16:00:00+00:00"),
            datetime.fromisoformat("2026-07-27T18:00:00+00:00"),
        )
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["gog", "calendar", "freebusy"]
        assert "--no-input" in cmd
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 30  # a hung gog must not wedge the plan
        assert kwargs.get("capture_output") is True  # stdout is parsed as JSON
        assert kwargs.get("text") is True

    def test_calendar_with_no_events_yields_no_busy_blocks(self):
        # Confirmed live shape: an empty calendar has no "busy" key at all.
        payload = {"calendars": {"some-cal@group.calendar.google.com": {}}}
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("subprocess.run", return_value=result):
            busy = tracker.fetch_busy(
                "acct", ["some-cal"], datetime(2026, 7, 27), datetime(2026, 7, 28)
            )
        assert busy == []

    def test_multiple_calendars_merge_into_one_list(self):
        payload = {
            "calendars": {
                "primary": {"busy": [{"start": "2026-07-27T16:00:00Z", "end": "2026-07-27T17:00:00Z"}]},
                "other": {"busy": [{"start": "2026-07-27T18:00:00Z", "end": "2026-07-27T19:00:00Z"}]},
            }
        }
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("subprocess.run", return_value=result):
            busy = tracker.fetch_busy(
                "acct", ["primary", "other"], datetime(2026, 7, 27), datetime(2026, 7, 28)
            )
        assert len(busy) == 2

    def test_per_calendar_error_raises_rather_than_treating_as_zero_busy(self):
        # A stale/unsubscribed calendar id must not silently inflate free capacity.
        payload = {
            "calendars": {
                "stale-cal@group.calendar.google.com": {
                    "errors": [{"domain": "global", "reason": "notFound"}]
                }
            }
        }
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("subprocess.run", return_value=result):
            with pytest.raises(RuntimeError, match="notFound"):
                tracker.fetch_busy(
                    "acct", ["stale-cal"], datetime(2026, 7, 27), datetime(2026, 7, 28)
                )

    def test_nonzero_exit_raises(self):
        result = MagicMock(returncode=2, stdout="", stderr="unrecognized calendar name(s): bogus")
        with patch("subprocess.run", return_value=result):
            with pytest.raises(RuntimeError, match="unrecognized calendar"):
                tracker.fetch_busy("acct", ["bogus"], datetime(2026, 7, 27), datetime(2026, 7, 28))

    def test_gog_missing_raises(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="not found"):
                tracker.fetch_busy("acct", ["primary"], datetime(2026, 7, 27), datetime(2026, 7, 28))

    def test_timeout_raises(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gog", timeout=30)):
            with pytest.raises(RuntimeError, match="timed out"):
                tracker.fetch_busy("acct", ["primary"], datetime(2026, 7, 27), datetime(2026, 7, 28))


def _fake_editor_run(cmd, **kwargs):
    """Stand in for subprocess.run: touch the temp file the way a real editor would."""
    with open(cmd[-1], "w", encoding="utf-8") as f:
        f.write("hello")
    return MagicMock(returncode=0)


class TestOpenInEditor:
    def test_uses_visual_over_editor(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "true")
        monkeypatch.setenv("EDITOR", "false")
        with patch("subprocess.run", side_effect=_fake_editor_run) as mock_run:
            tracker.open_in_editor("hello")
        assert mock_run.call_args[0][0][0] == "true"

    def test_falls_back_to_editor_then_vi(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "nano")
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            tracker.open_in_editor("hello")
        assert mock_run.call_args[0][0][0] == "nano"

    def test_no_env_vars_falls_back_to_vi(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            tracker.open_in_editor("hello")
        assert mock_run.call_args[0][0][0] == "vi"

    def test_editor_command_with_flags_is_split(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "code --wait")
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            tracker.open_in_editor("hello")
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["code", "--wait"]

    def test_editor_failure_is_not_swallowed(self, monkeypatch):
        # check=True: a non-zero editor exit must raise rather than silently
        # returning the unedited text as if the edit had gone through.
        monkeypatch.setenv("EDITOR", "vi")
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            tracker.open_in_editor("hello")
        assert mock_run.call_args.kwargs.get("check") is True

    def test_returns_edited_content(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "vi")

        def fake_run(cmd, **kwargs):
            path = cmd[-1]
            with open(path, "w", encoding="utf-8") as f:
                f.write("edited content\n")
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            result = tracker.open_in_editor("original content")
        assert result == "edited content\n"

    def test_temp_file_removed_after(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "vi")
        captured_path = {}

        def fake_run(cmd, **kwargs):
            captured_path["path"] = cmd[-1]
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            tracker.open_in_editor("content")
        import os

        assert not os.path.exists(captured_path["path"])


class TestColorSwatch:
    def test_hex_becomes_truecolor_block(self):
        # #ff887c (Flamingo) -> rgb(255, 136, 124)
        assert tracker._color_swatch("#ff887c") == "\x1b[48;2;255;136;124m  \x1b[0m"

    def test_none_is_aligned_blank(self):
        # Two blank cells so the default row's label lines up with colored rows.
        assert tracker._color_swatch(None) == "  "


class TestRenderColors:
    def test_selected_row_is_marked_and_bolded(self, capsys):
        tracker._render_colors(2, first=True)
        rows = [r for r in capsys.readouterr().out.split("\n") if r]
        assert len(rows) == len(tracker.EVENT_COLORS)
        assert "❯ " in rows[2]
        assert tracker.bold(tracker.EVENT_COLORS[2][1]) in rows[2]
        assert "❯ " not in rows[0]
        assert tracker.bold(tracker.EVENT_COLORS[0][1]) not in rows[0]

    def test_first_paint_does_not_move_the_cursor_up(self, capsys):
        tracker._render_colors(0, first=True)
        assert f"\x1b[{len(tracker.EVENT_COLORS)}A" not in capsys.readouterr().out

    def test_repaint_moves_the_cursor_up_over_the_previous_block(self, capsys):
        tracker._render_colors(0, first=False)
        assert capsys.readouterr().out.startswith(f"\x1b[{len(tracker.EVENT_COLORS)}A")


class TestSelectArrowsFallback:
    def test_tiny_terminal_falls_back_to_the_numbered_prompt(self):
        # One row leaves no space for a viewport plus a status line.
        tasks = [{"content": "A", "priority": 1}]
        with patch("todoist_cli.tracker._viewport_height", return_value=1), \
             patch("todoist_cli.tracker._select_numbered", return_value="fallback") as numbered:
            assert tracker._select_arrows(tasks) == "fallback"
        numbered.assert_called_once_with(tasks)

    def test_two_rows_is_enough_for_the_picker(self):
        tasks = [{"content": "A", "priority": 1}]
        with patch("todoist_cli.tracker._viewport_height", return_value=2), \
             patch("todoist_cli.tracker._select_numbered") as numbered, \
             patch("todoist_cli.tracker.termios"), patch("todoist_cli.tracker.tty"), \
             patch("todoist_cli.tracker._picker_loop", return_value=tasks[0]), \
             patch("sys.stdin.fileno", return_value=0):
            assert tracker._select_arrows(tasks) is tasks[0]
        numbered.assert_not_called()


class TestSelectEventColor:
    def test_only_the_opening_paint_is_flagged_first(self):
        keys = iter([b"\x1b[B", b"\r"])
        renders = []
        tracker._color_loop(read_key=lambda: next(keys),
                            render=lambda idx, first: renders.append((idx, first)))
        assert renders == [(0, True), (1, False)]

    def test_enter_on_default_returns_none(self):
        keys = iter([b"\r"])  # enter on index 0 (default)
        chosen = tracker._color_loop(read_key=lambda: next(keys),
                                     render=lambda idx, first: None)
        assert chosen is None

    def test_down_four_then_enter_returns_flamingo(self):
        # index 0 default, 1 Lavender, 2 Sage, 3 Grape, 4 Flamingo (id 4)
        keys = iter([b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r"])
        chosen = tracker._color_loop(read_key=lambda: next(keys),
                                     render=lambda idx, first: None)
        assert chosen == 4

    def test_cancel_returns_none(self):
        keys = iter([b"\x1b[B", b"\x1b"])  # move then esc
        chosen = tracker._color_loop(read_key=lambda: next(keys),
                                     render=lambda idx, first: None)
        assert chosen is None

    def test_eof_returns_none_without_spinning(self):
        # Empty read = stdin EOF (closed pipe/terminal). Must return, not loop.
        chosen = tracker._color_loop(read_key=lambda: b"",
                                     render=lambda idx, first: None)
        assert chosen is None

    def test_idle_timeout_returns_none(self):
        # read_key returning None = the select() idle timeout: keep the default.
        chosen = tracker._color_loop(read_key=lambda: None,
                                     render=lambda idx, first: None)
        assert chosen is None

    def test_returns_error_on_nonzero(self):
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        result = MagicMock(returncode=1, stdout="", stderr="auth failed")
        with patch("subprocess.run", return_value=result):
            ok, message = tracker.create_calendar_event("X", start, end, account=ACCOUNT)
        assert ok is False
        assert "auth failed" in message

    def test_timeout_returns_error(self):
        import subprocess as _sp
        start = datetime(2026, 6, 17, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 17, 14, 30, 0, tzinfo=timezone.utc)
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired("gog", 30)):
            ok, message = tracker.create_calendar_event("X", start, end, account=ACCOUNT)
        assert ok is False
        assert "timed out" in message


class TestEventColorTable:
    def test_ids_match_the_google_calendar_palette(self):
        # The id is sent straight to Google as --event-color, so a shifted id
        # silently paints every event the wrong color.
        assert tracker.EVENT_COLORS[0][:2] == (None, "Default (calendar default)")
        assert [(cid, name) for cid, name, _hex in tracker.EVENT_COLORS[1:]] == [
            (1, "Lavender"),
            (2, "Sage"),
            (3, "Grape"),
            (4, "Flamingo"),
            (5, "Banana"),
            (6, "Tangerine"),
            (7, "Peacock"),
            (8, "Graphite"),
            (9, "Blueberry"),
            (10, "Basil"),
            (11, "Tomato"),
        ]


class TestTimestampedDates:
    def test_date_suffix_trims_a_timestamped_due_date(self):
        task = {"content": "x", "priority": 1, "due": {"date": "2026-06-17T15:00:00"}}
        assert tracker._date_suffix(task) == " (Jun 17)"

    def test_timestamped_due_today_still_counts_as_today(self):
        today = date.today()
        task = {"content": "x", "priority": 1, "due": {"date": f"{today.isoformat()}T15:00:00"}}
        assert tracker._due_or_deadline_today(task) is True

    def test_timestamped_deadline_today_still_counts_as_today(self):
        today = date.today()
        task = {"content": "x", "priority": 1, "deadline": {"date": f"{today.isoformat()}T15:00:00"}}
        assert tracker._due_or_deadline_today(task) is True

    def test_due_string_today_counts_as_today(self):
        task = {"content": "x", "priority": 1, "due": {"date": "", "string": "Today"}}
        assert tracker._due_or_deadline_today(task) is True


class TestFormatRowMissingPriority:
    def test_absent_priority_renders_as_p4(self):
        assert tracker.format_row({"content": "No priority"}).startswith("P4")


class TestSelectNumberedBounds:
    def test_rows_are_numbered_from_one(self, capsys):
        tasks = [{"content": "A", "priority": 1}, {"content": "B", "priority": 1}]
        with patch("builtins.input", return_value="q"):
            tracker._select_numbered(tasks)
        out = capsys.readouterr().out
        assert "  1. " in out and "  2. " in out
        assert "  0. " not in out

    def test_zero_is_rejected(self, capsys):
        tasks = [{"content": "A", "priority": 1}]
        with patch("builtins.input", side_effect=["0", "1"]):
            assert tracker._select_numbered(tasks) is tasks[0]
        assert "Invalid selection." in capsys.readouterr().out

    def test_past_the_end_is_rejected(self, capsys):
        tasks = [{"content": "A", "priority": 1}]
        with patch("builtins.input", side_effect=["2", "1"]):
            assert tracker._select_numbered(tasks) is tasks[0]
        assert "Invalid selection." in capsys.readouterr().out


class TestSelectTaskTtyGuard:
    def test_non_tty_uses_the_numbered_prompt(self):
        tasks = [{"content": "A", "priority": 1}]
        with patch("sys.stdin.isatty", return_value=False), \
             patch("sys.stdout.isatty", return_value=True), \
             patch("todoist_cli.tracker._select_numbered", return_value="numbered") as numbered, \
             patch("todoist_cli.tracker._select_arrows") as arrows:
            assert tracker.select_task(tasks) == "numbered"
        numbered.assert_called_once_with(tasks)
        arrows.assert_not_called()

    def test_both_ttys_use_the_arrow_picker(self):
        tasks = [{"content": "A", "priority": 1}]
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout.isatty", return_value=True), \
             patch("todoist_cli.tracker.termios"), \
             patch("todoist_cli.tracker._select_numbered") as numbered, \
             patch("todoist_cli.tracker._select_arrows", return_value="arrows") as arrows:
            assert tracker.select_task(tasks) == "arrows"
        numbered.assert_not_called()
        arrows.assert_called_once_with(tasks)

    def test_missing_termios_falls_back_to_the_numbered_prompt(self):
        tasks = [{"content": "A", "priority": 1}]
        with patch("todoist_cli.tracker.termios", None), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout.isatty", return_value=True), \
             patch("todoist_cli.tracker._select_numbered", return_value="numbered"), \
             patch("todoist_cli.tracker._select_arrows") as arrows:
            assert tracker.select_task(tasks) == "numbered"
        arrows.assert_not_called()


class TestSpinner:
    def test_no_op_when_stderr_is_not_a_tty(self, capsys):
        with patch("sys.stderr.isatty", return_value=False), \
             patch("todoist_cli.tracker.threading.Thread") as thread:
            with tracker.spinner("Reading calendar"):
                pass
        thread.assert_not_called()
        assert capsys.readouterr().err == ""

    def test_runs_on_a_daemon_thread_and_clears_the_line(self, capsys):
        with patch("sys.stderr.isatty", return_value=True), \
             patch("todoist_cli.tracker.threading.Thread") as Thread:
            with tracker.spinner("Reading calendar"):
                pass
        assert Thread.call_args.kwargs.get("daemon") is True
        assert capsys.readouterr().err.endswith("\r\x1b[K")


class TestGogAuthOkCall:
    def test_probe_is_captured_and_bounded(self):
        result = MagicMock(returncode=0, stdout="primary\n", stderr="")
        with patch("subprocess.run", return_value=result) as mock_run:
            tracker.gog_auth_ok(ACCOUNT)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 30  # can't wedge the CLI at startup
        assert kwargs.get("capture_output") is True  # output is scanned for auth errors
        assert kwargs.get("text") is True

    def test_fail_on_invalid_token_text(self):
        result = MagicMock(returncode=0, stdout="", stderr="oauth2: invalid_token")
        with patch("subprocess.run", return_value=result):
            assert tracker.gog_auth_ok(ACCOUNT) is False

    def test_timeout_is_a_failure(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gog", timeout=30)):
            assert tracker.gog_auth_ok(ACCOUNT) is False


# ---------------------------------------------------------------------------
# The classifier reads text produced elsewhere — gog wrapping a Google API
# error — so its input space is effectively arbitrary. The cases above pin the
# errors we've actually seen; these cover the shape of everything else: it must
# never raise, never disagree with the help it drives, and never quietly stop
# recognizing the failure that motivated all of this.
# ---------------------------------------------------------------------------

from hypothesis import given, settings, strategies as st


class TestGogErrorKindProperties:
    @given(st.text())
    @settings(max_examples=2000)
    def test_never_raises_and_returns_a_known_kind(self, message):
        kind = tracker.gog_error_kind(message)
        assert kind in (None, "auth", "scope", "missing", "quota", "network")

    @given(st.text())
    @settings(max_examples=2000)
    def test_help_exists_exactly_when_the_error_is_diagnosed(self, message):
        # A kind with no help would print a diagnosis-shaped blank; help with no
        # kind would advise a fix the CLI won't offer to run.
        assert (tracker.gog_error_kind(message) is None) == (
            tracker.gog_error_help(message, ACCOUNT) is None
        )

    # URL/transport noise: gog wraps the Google error in a varying amount of it.
    # Deliberately excludes letters so the noise can't spell another marker —
    # "invalid_grant ... not found on PATH" really is a missing-binary error.
    _NOISE = st.text(alphabet='0123456789 :/".,-_?&=()')

    @given(_NOISE, _NOISE)
    @settings(max_examples=1000)
    def test_expired_token_is_recognized_wherever_it_appears(self, prefix, suffix):
        message = f"{prefix}invalid_grant{suffix}"
        assert tracker.gog_error_kind(message) in tracker.REAUTHABLE_KINDS

    @given(st.text(min_size=1))
    @settings(max_examples=1000)
    def test_classification_ignores_case(self, message):
        assert tracker.gog_error_kind(message) == tracker.gog_error_kind(message.upper())

    @given(st.text(), st.emails())
    def test_a_diagnosed_error_never_leaks_a_stale_account(self, message, account):
        help_text = tracker.gog_error_help(message, account)
        if help_text and tracker.gog_error_kind(message) in tracker.REAUTHABLE_KINDS:
            assert account in help_text
            assert ACCOUNT not in help_text or account == ACCOUNT
