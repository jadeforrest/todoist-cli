"""Tests for todone_core (the Todone app's logic) and todone_server (its JSON transport)."""

import dataclasses
import io
import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings, strategies as st

from todoist_cli import planner, todone_core, todone_fake, todone_server
from todoist_cli.todone_core import Services, Todone, TodoneError, place_items

TZ = ZoneInfo("America/Los_Angeles")
MONDAY = date(2026, 11, 16)


class Clock:
    def __init__(self, when: datetime):
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def make_app(today: date = MONDAY, hour: int = 8, busy_error: str | None = None, client=None):
    # Pinned rather than read from defaults, so the expected capacities below
    # don't move with the package's default hours or the machine's time zone.
    config = dataclasses.replace(
        planner.load_config("/nonexistent/plan.toml"),
        timezone=TZ,
        working_hours={i: (planner._parse_hm("09:00"), planner._parse_hm("14:00")) for i in range(7)},
    )
    clock = Clock(datetime(today.year, today.month, today.day, hour, 0, tzinfo=TZ))
    busy_calls = []

    def fetch_busy(start, end):
        busy_calls.append((start, end))
        if busy_error:
            raise RuntimeError(busy_error)
        return todone_fake.fake_busy(TZ)(start, end)

    events = []

    def write_event(title, start, end, description=None, color=None):
        events.append((title, start, end, description, color))
        return True, "ok"

    log = MagicMock()
    services = Services(
        client=client or todone_fake.sample_data(today),
        config=config,
        fetch_busy=fetch_busy,
        write_event=write_event,
        now=clock,
        write_plan_log=log,
    )
    app = Todone(services)
    app.busy_calls, app.events, app.clock, app.log = busy_calls, events, clock, log
    return app


def ids(tasks):
    return [t["id"] for t in tasks]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_sections_and_sorting(self):
        snap = make_app().refresh()
        assert ids(snap["overdue"]) == ["t1", "t2"]  # by due date first
        # Today: by priority, then title; P1 first.
        assert ids(snap["today_tasks"]) == ["t5", "t4", "t3", "t6"]
        assert [c["date"] for c in snap["columns"]] == [
            "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"
        ]
        assert ids(snap["columns"][0]["tasks"]) == ["t7"]
        assert ids(snap["tray"]) == ["t11"]  # t10 is in the Inbox panel instead

    def test_header_uses_planner_capacity_and_sizes(self):
        snap = make_app(hour=8).refresh()
        # 09:00-14:00 minus the 10-11 and 12:30-13 meetings.
        assert snap["today_column"]["capacity_minutes"] == 300 - 90
        expected = sum(t["minutes"] for t in snap["overdue"] + snap["today_tasks"])
        assert snap["today_column"]["planned_minutes"] == expected

    def test_capacity_counts_down_from_now(self):
        snap = make_app(hour=13).refresh()
        assert snap["today_column"]["capacity_minutes"] == 60

    def test_weekend_has_no_capacity(self):
        snap = make_app(today=date(2026, 11, 21)).refresh()
        assert snap["today_column"]["capacity_minutes"] == 0

    def test_flags(self):
        snap = make_app().refresh()
        by_id = {t["id"]: t for t in snap["overdue"] + snap["today_tasks"]}
        assert by_id["t1"]["overdue"] and not by_id["t3"]["overdue"]
        assert by_id["t5"]["pinned"] and not by_id["t5"]["movable"]  # time of day
        assert by_id["t4"]["recurring"] and by_id["t4"]["movable"]  # anchored rule
        assert not by_id["t6"]["movable"]  # every 3 days would drift
        assert by_id["t3"]["minutes"] == 120 and by_id["t3"]["size_label"] == "2h"
        fixed = next(t for c in snap["columns"] for t in c["tasks"] if t["id"] == "t8")
        assert fixed["fixed"] and fixed["pinned"]

    def test_at_risk_is_the_planners(self):
        app = make_app()
        app.refresh()
        expected = {i.id for i in planner.pack(app._items(), app._capacity()).at_risk}
        snap = app.snapshot()
        flagged = {t["id"] for t in _all_tasks(snap) if t["at_risk"]}
        assert flagged == expected

    def test_calendar_error_leaves_capacity_unknown(self):
        snap = make_app(busy_error="token expired").refresh()
        assert snap["calendar_error"] == "token expired"
        assert snap["today_column"]["capacity_minutes"] is None
        assert not any(t["at_risk"] for t in _all_tasks(snap))


def _all_tasks(snap):
    return snap["overdue"] + snap["today_tasks"] + snap["tray"] + [
        t for c in snap["columns"] for t in c["tasks"]
    ]


class TestBusyCache:
    def test_reuses_calendar_for_fifteen_minutes(self):
        app = make_app()
        app.refresh()
        app.clock.when += timedelta(minutes=14)
        app.refresh()
        assert len(app.busy_calls) == 1
        app.clock.when += timedelta(minutes=1)
        app.refresh()
        assert len(app.busy_calls) == 2

    def test_force_refetches(self):
        app = make_app()
        app.refresh()
        app.refresh(force_calendar=True)
        assert len(app.busy_calls) == 2

    def test_new_day_refetches(self):
        app = make_app(hour=23)
        app.refresh()
        app.clock.when += timedelta(hours=2)
        app.refresh()
        assert len(app.busy_calls) == 2


# ---------------------------------------------------------------------------
# place_items
# ---------------------------------------------------------------------------


def _item(i, due, priority=4):
    return planner.Item(id=str(i), content=f"task {i}", priority=priority, due_date=due,
                        deadline=None, minutes=30, pinned=False)


@st.composite
def placements(draw):
    today = MONDAY
    buckets = planner.weekday_buckets(today, 7)
    n = draw(st.integers(0, 25))
    offsets = st.one_of(st.none(), st.integers(-10, 12))
    items = [
        _item(i, None if (o := draw(offsets)) is None else today + timedelta(days=o), draw(st.integers(1, 4)))
        for i in range(n)
    ]
    return items, today, buckets


class TestPlaceItems:
    @given(placements())
    @settings(max_examples=300)
    def test_each_task_placed_at_most_once_and_where_it_belongs(self, data):
        items, today, buckets = data
        dates = {i.id: i.due_date for i in items}
        out = place_items(items, dates, today, buckets)
        placed = out["overdue"] + out["today"] + out["tray"] + [i for _, col in out["columns"] for i in col]
        assert len(placed) == len(set(placed))
        for item in items:
            d = item.due_date
            if d is None or d <= buckets[-1]:
                assert item.id in placed
            else:
                assert item.id not in placed
        assert all(dates[i] < today for i in out["overdue"])
        assert all(dates[i] == today for i in out["today"])
        for day, col in out["columns"]:
            assert day > today and all(dates[i] == day for i in col)

    @given(placements())
    def test_overdue_sorted_by_date_then_priority(self, data):
        items, today, buckets = data
        by_id = {i.id: i for i in items}
        out = place_items(items, {i.id: i.due_date for i in items}, today, buckets)
        keys = [(by_id[i].due_date, by_id[i].priority) for i in out["overdue"]]
        assert keys == sorted(keys)
        prios = [by_id[i].priority for i in out["today"]]
        assert prios == sorted(prios)

    def test_weekend_goes_to_tray(self):
        sat = date(2026, 11, 21)
        out = place_items([_item(1, sat)], {"1": sat}, MONDAY, planner.weekday_buckets(MONDAY, 7))
        assert out["tray"] == ["1"]

    def test_no_buckets(self):
        out = place_items([_item(1, MONDAY)], {"1": MONDAY}, MONDAY, [])
        assert out["today"] == ["1"] and out["columns"] == []


# ---------------------------------------------------------------------------
# Capacity overrides
# ---------------------------------------------------------------------------


class TestCapacity:
    def test_override_and_reset(self):
        app = make_app()
        app.refresh()
        snap = app.set_capacity("2026-11-17", "1h30m")
        col = snap["columns"][0]
        assert col["capacity_minutes"] == 90 and col["capacity_overridden"]
        snap = app.set_capacity("2026-11-17", "")
        assert snap["columns"][0]["capacity_minutes"] == 210
        assert not snap["columns"][0]["capacity_overridden"]

    def test_override_today_changes_header(self):
        app = make_app()
        app.refresh()
        assert app.set_capacity("2026-11-16", "0")["today_column"]["capacity_minutes"] == 0

    @pytest.mark.parametrize("day,value", [("2026-11-21", "1h"), ("2026-12-30", "1h"), ("nope", "1h"),
                                           ("2026-11-17", "lots")])
    def test_rejects_bad_input(self, day, value):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.set_capacity(day, value)

    def test_reset_of_unset_day_is_harmless(self):
        app = make_app()
        app.refresh()
        app.set_capacity("2026-11-17", None)
        assert app.capacity_overrides == {}


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


class TestMove:
    def test_writes_due_date_outside_planning(self):
        app = make_app()
        app.refresh()
        snap = app.move("t3", "2026-11-18")
        assert app.services.client.tasks["t3"]["due"]["date"] == "2026-11-18"
        assert "t3" in ids(snap["columns"][1]["tasks"])

    def test_recurring_moves_one_occurrence_keeping_rule(self):
        app = make_app()
        app.refresh()
        app.move("t4", "2026-11-17")
        due = app.services.client.tasks["t4"]["due"]
        assert due == {"date": "2026-11-17", "string": "every mon", "is_recurring": True}

    @pytest.mark.parametrize("task_id", ["t5", "t6", "t8"])
    def test_locked_tasks_refuse(self, task_id):
        app = make_app()
        app.refresh()
        before = dict(app.services.client.tasks[task_id])
        with pytest.raises(TodoneError, match="can't be moved"):
            app.move(task_id, "2026-11-19")
        assert app.services.client.tasks[task_id] == before

    def test_past_refused(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError, match="past"):
            app.move("t3", "2026-11-15")

    def test_unknown_task(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.move("nope", "2026-11-17")

    def test_planning_move_edits_draft_only(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        before = json.dumps(app.services.client.tasks, sort_keys=True)
        snap = app.move("t3", "2026-11-23")
        assert json.dumps(app.services.client.tasks, sort_keys=True) == before
        moved = next(t for t in snap["columns"][-1]["tasks"] if t["id"] == "t3")
        assert moved["moved"] and moved["original_due_date"] == "2026-11-16"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestPlanning:
    def test_proposal_matches_cli_final_dates(self):
        app = make_app()
        app.refresh()
        items, capacity = app._items(), app._capacity()
        buckets = sorted(capacity)
        result = planner.pack(items, capacity)
        expected = planner.park_overdue_dates(
            planner.earliest_scheduled_dates(result, buckets), items, MONDAY, buckets[-1]
        )
        snap = app.propose_plan()
        assert snap["planning"]
        assert app.draft.final_dates == expected
        assert app.draft.at_risk_ids == {i.id for i in result.at_risk}

    def test_cannot_plan_without_calendar(self):
        app = make_app(busy_error="down")
        app.refresh()
        with pytest.raises(TodoneError, match="calendar"):
            app.propose_plan()

    def test_cancel_discards(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        assert not app.cancel_plan()["planning"]

    def test_confirm_without_plan(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.confirm_plan()

    def test_confirm_logs_and_applies(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        app.move("t3", "2026-11-23")
        snap = app.confirm_plan()
        client = app.services.client
        assert not snap["planning"]
        assert client.tasks["t3"]["due"]["date"] == "2026-11-23"
        # Locked tasks are never written even though the plan moved them.
        assert client.tasks["t6"]["due"]["date"] == "2026-11-16"
        assert client.tasks["t5"]["due"]["datetime"].startswith("2026-11-16")
        assert snap["result"]["failed"] == 0 and snap["result"]["updated"] >= 1
        final, by_id, changes, labels = app.log.call_args.args
        assert final["t3"] == date(2026, 11, 23)
        assert set(by_id) >= {"t3"}

    def test_confirm_sets_atrisk_labels(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        risky = set(app.draft.at_risk_ids)
        app.services.client.tasks["t9"]["labels"] = ["admin", "atrisk"]  # stale label
        app.confirm_plan()
        for tid, task in app.services.client.tasks.items():
            assert ("atrisk" in task["labels"]) == (tid in risky), tid

    def test_confirm_skips_tasks_closed_since_proposal(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        app.services.client.close_task("t1")
        app.confirm_plan()
        final = app.log.call_args.args[0]
        assert "t1" not in final


# ---------------------------------------------------------------------------
# Task actions and timers
# ---------------------------------------------------------------------------


class TestActions:
    def test_complete(self):
        app = make_app()
        app.refresh()
        snap = app.complete("t3")
        assert "t3" not in ids(snap["today_tasks"])

    def test_complete_running_refused(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        with pytest.raises(TodoneError, match="timer"):
            app.complete("t3")

    def test_toggle_fixed_adds_and_removes(self):
        app = make_app()
        app.refresh()
        app.toggle_fixed("t3")
        assert app.services.client.tasks["t3"]["labels"] == ["writing", "fixed"]
        app.toggle_fixed("t3")
        assert app.services.client.tasks["t3"]["labels"] == ["writing"]

    def test_toggle_fixed_matches_case_insensitively(self):
        app = make_app()
        app.services.client.tasks["t3"]["labels"] = ["Fixed", "writing"]
        app.refresh()
        app.toggle_fixed("t3")
        assert app.services.client.tasks["t3"]["labels"] == ["writing"]

    def test_colors(self):
        colors = make_app().colors()
        assert colors[0]["id"] is None and len(colors) == 12


class TestTimers:
    def test_several_run_at_once(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        snap = app.start("t1")
        assert {t["task_id"] for t in snap["timers"]} == {"t1", "t3"}
        assert next(t for t in snap["today_tasks"] if t["id"] == "t3")["running"]

    def test_start_twice_keeps_first_start(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        first = app.timers["t3"].started_at
        app.clock.when += timedelta(minutes=5)
        app.start("t3")
        assert app.timers["t3"].started_at == first

    def test_stop_logs_event(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        app.clock.when += timedelta(minutes=25)
        snap = app.stop("t3", color=5)
        title, start, end, description, color = app.events[0]
        assert title == "Write blog post outline 2h"
        assert end - start == timedelta(minutes=25)
        assert description == "https://app.todoist.com/app/task/t3" and color == 5
        assert snap["timers"] == []

    def test_failed_stop_keeps_timer_and_end_time(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        app.clock.when += timedelta(minutes=10)
        app.services.write_event = lambda *a: (False, "invalid_grant")
        with pytest.raises(TodoneError, match="couldn't log"):
            app.stop("t3")
        stopped = app.timers["t3"].stopped_at
        app.clock.when += timedelta(minutes=30)
        seen = []
        app.services.write_event = lambda title, start, end, *a: seen.append(end) or (True, "")
        app.stop("t3")
        assert seen == [stopped]

    def test_stop_without_timer(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.stop("t3")

    def test_discard(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        assert app.discard_timer("t3")["timers"] == []
        assert app.events == []


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


class TestDetails:
    def test_details_view(self):
        app = make_app()
        app.refresh()
        d = app.details("t3")
        assert d["task"]["priority"] == 3 and d["task"]["size_minutes"] == 120
        assert d["task"]["due_string"] == "2026-11-16"
        assert [c["id"] for c in d["comments"]] == ["c-a", "c-b"]
        assert d["comments"][1]["attachment"]["file_name"] == "outline.md"
        assert d["comments"][0]["attachment"] is None
        assert "client" in d["labels"] and {"id": "work", "name": "Consulting"} in d["projects"]

    def test_update_fields(self):
        app = make_app()
        app.refresh()
        app.update_details("t3", {"content": "Outline post", "description": "d", "priority": 1,
                                  "labels": ["writing", "client"], "deadline": "2026-11-20"})
        t = app.services.client.tasks["t3"]
        assert (t["content"], t["description"], t["priority"], t["labels"]) == (
            "Outline post", "d", 4, ["writing", "client"])
        assert t["deadline"] == {"date": "2026-11-20"}

    def test_empty_due_clears_and_empty_deadline_clears(self):
        app = make_app()
        app.refresh()
        app.update_details("t2", {"due_string": "  ", "deadline": None})
        t = app.services.client.tasks["t2"]
        assert t["due"] is None and t["deadline"] is None

    def test_size_writes_title_hint(self):
        app = make_app()
        app.refresh()
        app.update_details("t3", {"size_minutes": 90})
        assert app.services.client.tasks["t3"]["content"] == "Write blog post outline 1h30m"

    def test_size_with_new_title_applies_to_new_title(self):
        app = make_app()
        app.refresh()
        app.update_details("t10", {"content": "Read notes", "size_minutes": 45})
        assert app.services.client.tasks["t10"]["content"] == "Read notes [45m]"

    def test_size_writes_native_duration_when_present(self):
        app = make_app()
        app.services.client.tasks["t10"]["duration"] = {"amount": 20, "unit": "minute"}
        app.refresh()
        app.update_details("t10", {"size_minutes": 50})
        t = app.services.client.tasks["t10"]
        assert t["duration"]["amount"] == 50 and t["content"] == "Read 'Team Topologies' notes"

    def test_negative_size_refused(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.update_details("t3", {"size_minutes": -5})

    def test_bad_deadline_refused(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.update_details("t3", {"deadline": "friday"})

    def test_move_to_section_then_out_of_it(self):
        app = make_app()
        app.refresh()
        app.update_details("t3", {"section_id": "s-admin"})
        assert app.services.client.tasks["t3"]["section_id"] == "s-admin"
        app.update_details("t3", {"section_id": None})
        t = app.services.client.tasks["t3"]
        assert t["section_id"] is None and t["project_id"] == "work"

    def test_move_to_other_project(self):
        app = make_app()
        app.refresh()
        app.update_details("t3", {"project_id": "home", "section_id": None})
        assert app.services.client.tasks["t3"]["project_id"] == "home"

    def test_no_changes_writes_nothing(self):
        client = todone_fake.sample_data(MONDAY)
        client.update_task = MagicMock()
        client.move_task = MagicMock()
        app = make_app(client=client)
        app.refresh()
        app.update_details("t3", {})
        client.update_task.assert_not_called()
        client.move_task.assert_not_called()

    def test_same_project_and_section_does_not_move(self):
        client = todone_fake.sample_data(MONDAY)
        client.move_task = MagicMock()
        app = make_app(client=client)
        app.refresh()
        app.update_details("t7", {"project_id": "work", "section_id": "s-clients"})
        client.move_task.assert_not_called()

    def test_comments(self):
        app = make_app()
        app.refresh()
        d = app.add_comment("t3", "new note")
        cid = d["comments"][-1]["id"]
        d = app.edit_comment("t3", cid, "edited")
        assert d["comments"][-1]["content"] == "edited"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class TestServer:
    def test_unknown_method(self):
        assert todone_server.handle(make_app(), {"id": 1, "method": "__init__"})["error"].startswith("unknown")

    def test_domain_error_is_returned(self):
        app = make_app()
        app.refresh()
        out = todone_server.handle(app, {"id": 2, "method": "stop", "params": {"task_id": "t3"}})
        assert out == {"id": 2, "error": "that task has no running timer"}

    def test_bad_params_are_returned(self):
        out = todone_server.handle(make_app(), {"id": 3, "method": "move", "params": {"nope": 1}})
        assert "unexpected keyword" in out["error"]

    def test_internal_error_keeps_serving(self, capsys):
        app = make_app()
        app.services.client.get_all_open_tasks = MagicMock(side_effect=KeyError("boom"))
        out = todone_server.handle(app, {"id": 4, "method": "refresh"})
        assert out["error"].startswith("internal error")

    def test_serve_round_trip(self):
        stdin = io.StringIO('{"id": 1, "method": "refresh"}\n\nnot json\n{"id": 2, "method": "colors"}\n')
        stdout = io.StringIO()
        todone_server.serve(make_app(), stdin=stdin, stdout=stdout)
        lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
        assert [l["id"] for l in lines] == [1, None, 2]
        assert "overdue" in lines[0]["result"] and "bad request" in lines[1]["error"]

    def test_fake_services_build(self, tmp_path):
        services = todone_server.fake_services(tmp_path / "missing.toml")
        app = Todone(services)
        assert app.refresh()["calendar_error"] is None
        services.write_plan_log({}, {}, [], {})

    def test_real_services_use_the_config_account(self, tmp_path, monkeypatch):
        config = tmp_path / "plan.toml"
        config.write_text('account = "me@example.com"\ncalendars = ["primary", "team"]\n')
        calls = []
        monkeypatch.setattr(todone_server.cli, "_get_client", lambda: "client")
        monkeypatch.setattr(todone_server.tracker, "fetch_busy", lambda *a: calls.append(a) or [])
        monkeypatch.setattr(
            todone_server.tracker, "create_calendar_event",
            lambda *a, **k: calls.append(k) or (True, "ok"),
        )
        services = todone_server.real_services(config)
        assert services.client == "client"
        assert services.fetch_busy(1, 2) == []
        assert calls[0] == ("me@example.com", ["primary", "team"], 1, 2)
        assert services.write_event("t", 1, 2, color=3) == (True, "ok")
        assert calls[1] == {"description": None, "account": "me@example.com", "event_color": 3}
        assert "me@example.com" in services.calendar_help("insufficient authentication scopes")
        assert services.now().tzinfo is not None

    def test_stop_failure_includes_calendar_help(self):
        app = make_app()
        app.services.write_event = lambda *a: (False, "boom")
        app.services.calendar_help = lambda message: f"fix {message}"
        app.refresh()
        app.start("t1")
        with pytest.raises(TodoneError, match="fix boom"):
            app.stop("t1")


class TestSmallHelpers:
    def test_clock_time_floating(self):
        assert todone_core._clock_time("2026-11-16T15:00:00") == "15:00"

    def test_clock_time_utc_shown_locally(self):
        expected = datetime(2026, 11, 16, 23, 0, tzinfo=ZoneInfo("UTC")).astimezone().strftime("%H:%M")
        assert todone_core._clock_time("2026-11-16T23:00:00Z") == expected

    def test_clock_time_none(self):
        assert todone_core._clock_time(None) is None


class TestMoveNoop:
    def test_same_day_drop_writes_nothing(self):
        client = todone_fake.sample_data(MONDAY)
        client.update_task = MagicMock()
        app = make_app(client=client)
        app.refresh()
        app.move("t3", "2026-11-16")
        client.update_task.assert_not_called()

    def test_same_day_drop_of_locked_task_is_quiet(self):
        app = make_app()
        app.refresh()
        app.move("t5", "2026-11-16")  # no "can't be moved" error for a no-op

    def test_same_day_drop_while_planning_keeps_draft(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        before = dict(app.draft.final_dates)
        day = before["t3"].isoformat()
        app.move("t3", day)
        assert app.draft.final_dates == before


class TestLogTimers:
    def test_log_timer_logs_title_and_description(self):
        app = make_app()
        app.refresh()
        snap = app.start_log("  client   call ", "intro with CTO")
        timer = snap["timers"][0]
        assert timer["title"] == "client call" and timer["is_log"]
        app.clock.when += timedelta(minutes=40)
        app.stop(timer["task_id"], color=None)
        title, start, end, description, color = app.events[0]
        assert (title, description, end - start) == ("client call", "intro with CTO", timedelta(minutes=40))

    def test_several_logs_get_distinct_ids(self):
        app = make_app()
        app.refresh()
        app.start_log("a")
        snap = app.start_log("b")
        assert len({t["task_id"] for t in snap["timers"]}) == 2

    def test_blank_description_is_none(self):
        app = make_app()
        app.refresh()
        app.start_log("a", "")
        assert next(iter(app.timers.values())).description is None

    def test_blank_title_refused(self):
        app = make_app()
        app.refresh()
        with pytest.raises(TodoneError):
            app.start_log("   ")

    def test_task_timer_description_is_task_url(self):
        app = make_app()
        app.refresh()
        app.start("t3")
        assert app.timers["t3"].description == "https://app.todoist.com/app/task/t3"
        assert not app.timers["t3"].is_log

    def test_server_exposes_start_log(self):
        app = make_app()
        app.refresh()
        out = todone_server.handle(app, {"id": 1, "method": "start_log", "params": {"title": "x"}})
        assert out["result"]["timers"][0]["title"] == "x"


# ---------------------------------------------------------------------------
# Multi-day tasks
# ---------------------------------------------------------------------------

BUCKETS = planner.weekday_buckets(MONDAY, 7)


@st.composite
def spreads(draw):
    capacity = {d: draw(st.integers(0, 600)) for d in BUCKETS}
    start = draw(st.sampled_from(BUCKETS))
    return draw(st.integers(0, 6000)), start, capacity


class TestSpread:
    @given(spreads())
    @settings(max_examples=500)
    def test_keeps_total_and_fills_days_in_order(self, data):
        minutes, start, capacity = data
        out = todone_core.spread(minutes, start, BUCKETS, capacity)
        assert sum(out.values()) == minutes
        assert all(d >= start for d in out)
        days = sorted(out)
        # Every day before the last one used is filled to its free time, and
        # no day with free time is skipped along the way.
        for day in days[:-1]:
            assert out[day] == capacity[day]
        if days:
            skipped = [d for d in BUCKETS if start <= d < days[-1] and d not in out]
            assert all(capacity[d] == 0 for d in skipped)

    @given(spreads())
    def test_fits_in_a_day_stays_whole(self, data):
        minutes, start, capacity = data
        if 0 < minutes <= capacity[start]:
            assert todone_core.spread(minutes, start, BUCKETS, capacity) == {start: minutes}

    def test_spills_into_next_day(self):
        cap = {d: 240 for d in BUCKETS}
        assert todone_core.spread(360, MONDAY, BUCKETS, cap) == {MONDAY: 240, date(2026, 11, 17): 120}

    def test_past_horizon_stays_on_last_day(self):
        cap = {d: 60 for d in BUCKETS}
        out = todone_core.spread(1000, BUCKETS[-2], BUCKETS, cap)
        assert out == {BUCKETS[-2]: 60, BUCKETS[-1]: 940}

    def test_no_calendar_or_unplanned_day_keeps_it_whole(self):
        assert todone_core.spread(500, MONDAY, BUCKETS, None) == {MONDAY: 500}
        sat = date(2026, 11, 21)
        assert todone_core.spread(500, sat, BUCKETS, {d: 60 for d in BUCKETS}) == {sat: 500}

    def test_all_days_full_keeps_it_on_start(self):
        assert todone_core.spread(90, MONDAY, BUCKETS, {d: 0 for d in BUCKETS}) == {MONDAY: 90}

    def test_zero_minutes(self):
        assert todone_core.spread(0, MONDAY, BUCKETS, {d: 60 for d in BUCKETS}) == {}


class TestMultiDayPlanned:
    def test_big_task_spills_into_later_columns(self):
        app = make_app()
        app.refresh()
        snap = app.move("t7", "2026-11-19")  # 6h on a 3h30m day (09-14 less 1h30m)
        cols = {c["date"]: c for c in snap["columns"]}
        assert cols["2026-11-19"]["planned_minutes"] == 210 + 20  # t7's share plus the 20m t9
        assert cols["2026-11-20"]["planned_minutes"] == 150
        card = next(t for t in cols["2026-11-19"]["tasks"] if t["id"] == "t7")
        assert card["spans_until"] == "2026-11-20"

    def test_pinned_task_is_never_spread(self):
        app = make_app()
        app.refresh()
        snap = app.set_capacity("2026-11-18", "10m")
        wed = next(c for c in snap["columns"] if c["date"] == "2026-11-18")
        # t8 (@fixed, 30m) stays whole; the other 10m is Tuesday's 6h t7 spilling over.
        assert wed["planned_minutes"] == 30 + 10
        t8 = next(t for t in wed["tasks"] if t["id"] == "t8")
        assert t8["spans_until"] is None

    def test_tasks_that_each_fit_still_overload_a_day(self):
        app = make_app()
        app.refresh()
        app.move("t3", "2026-11-19")  # 2h, joining the 20m t9
        snap = app.set_capacity("2026-11-19", "2h")
        thu = next(c for c in snap["columns"] if c["date"] == "2026-11-19")
        assert thu["planned_minutes"] == 140 > thu["capacity_minutes"]

    def test_header_counts_only_todays_share(self):
        app = make_app()
        app.refresh()
        snap = app.move("t7", "2026-11-16")
        today_cap = snap["today_column"]["capacity_minutes"]
        others = sum(t["minutes"] for t in snap["overdue"] + snap["today_tasks"] if t["id"] != "t7")
        assert snap["today_column"]["planned_minutes"] == others + min(360, today_cap)

    def test_planning_uses_the_plans_own_split(self):
        app = make_app()
        app.refresh()
        snap = app.propose_plan()
        items, capacity = app._items(), app._capacity()
        expected = todone_core.planned_by_day(todone_core.schedule_chunks(planner.pack(items, capacity)))
        assert snap["today_column"]["planned_minutes"] == expected.get(MONDAY, 0)
        for col in snap["columns"]:
            assert col["planned_minutes"] == expected.get(date.fromisoformat(col["date"]), 0), col["date"]

    def test_planning_move_respreads_from_new_day(self):
        app = make_app()
        app.refresh()
        app.propose_plan()
        snap = app.move("t7", "2026-11-23")
        mon = next(c for c in snap["columns"] if c["date"] == "2026-11-23")
        t7 = next(t for t in mon["tasks"] if t["id"] == "t7")
        assert t7["spans_until"] is None  # last day: the rest stays here
        assert mon["planned_minutes"] >= 360

    def test_schedule_chunks_skip_zero_minute_containers(self):
        item = _item("c", MONDAY)
        result = planner.PlanResult(schedule={MONDAY: [planner.ScheduledChunk(item, MONDAY, 0)]})
        assert todone_core.schedule_chunks(result) == {}


class TestInboxPanel:
    def _app(self):
        app = make_app()
        client = app.services.client
        client.sections.append({"id": "s-hasdate", "project_id": "inbox", "name": "Has date"})
        client.tasks["t20"] = {"id": "t20", "content": "Sectioned inbox task", "priority": 1,
                               "project_id": "inbox", "section_id": "s-hasdate", "labels": []}
        client.tasks["t21"] = {"id": "t21", "content": "Far-off inbox task", "priority": 4,
                               "project_id": "inbox", "labels": [],
                               "due": {"date": "2027-03-01", "string": "Mar 1", "is_recurring": False}}
        client.tasks["t22"] = {"id": "t22", "content": "Dated inbox task", "priority": 1,
                               "project_id": "inbox", "labels": [],
                               "due": {"date": "2026-11-17", "string": "tomorrow", "is_recurring": False}}
        app.refresh()
        return app

    def test_lists_unsectioned_inbox_tasks_by_priority(self):
        snap = self._app().snapshot()
        assert ids(snap["inbox"]) == ["t21", "t22", "t10"]  # P1, then P4s by title

    def test_sectioned_inbox_tasks_left_out(self):
        assert "t20" not in ids(self._app().snapshot()["inbox"])

    def test_inbox_tasks_not_repeated_in_tray(self):
        snap = self._app().snapshot()
        assert not set(ids(snap["tray"])) & set(ids(snap["inbox"]))

    def test_dated_inbox_task_also_on_its_day(self):
        snap = self._app().snapshot()
        assert "t22" in ids(snap["columns"][0]["tasks"])

    def test_far_off_inbox_task_can_be_dragged_onto_a_day(self):
        app = self._app()
        app.move("t21", "2026-11-18")
        assert app.services.client.tasks["t21"]["due"]["date"] == "2026-11-18"

    def test_no_inbox_project(self):
        app = make_app()
        app.services.client.projects = [p for p in app.services.client.projects if not p.get("inbox_project")]
        assert app.refresh()["inbox"] == []


# ---------------------------------------------------------------------------
# Subtasks, comment counts and card edits
# ---------------------------------------------------------------------------


def _find(snap, task_id):
    tasks = snap["overdue"] + snap["today_tasks"] + snap["tray"] + snap["inbox"]
    tasks += [t for c in snap["columns"] for t in c["tasks"]]
    return next(t for t in tasks if t["id"] == task_id)


class TestSubtasks:
    def test_nested_subtasks_in_order(self):
        view = _find(make_app().refresh(), "t3")
        assert [s["id"] for s in view["subtasks"]] == ["t3a", "t3b"]
        sketch = view["subtasks"][1]
        assert sketch["priority"] == 4 and sketch["labels"] == ["writing"] and sketch["comment_count"] == 1
        assert [s["id"] for s in sketch["subtasks"]] == ["t3b1"]
        assert sketch["subtasks"][0]["subtasks"] == []
        assert sketch["url"].endswith("/t3b")

    def test_task_without_children_has_none(self):
        assert _find(make_app().refresh(), "t1")["subtasks"] == []

    def test_view_carries_description_and_comment_count(self):
        snap = make_app().refresh()
        t3 = _find(snap, "t3")
        assert t3["description"].startswith("Topic") and t3["comment_count"] == 2
        assert _find(snap, "t1")["description"] == "" and _find(snap, "t1")["comment_count"] == 0

    def test_subtask_due_date_is_the_day(self):
        kids = todone_core.children_index([
            {"id": "c", "parent_id": "p", "due": {"date": "2026-11-18T09:00:00"}},
            {"id": "d", "parent_id": "p", "due": None},
        ])
        views = todone_core.subtask_views("p", kids)
        assert [v["due_date"] for v in views] == ["2026-11-18", None]

    def test_child_order_then_id(self):
        kids = todone_core.children_index([
            {"id": "b", "parent_id": "p", "child_order": 2},
            {"id": "z", "parent_id": "p", "child_order": 1},
            {"id": "a", "parent_id": "p", "child_order": 2},
            {"id": "top"},
        ])
        assert [t["id"] for t in kids["p"]] == ["z", "a", "b"] and "top" not in kids

    def test_parent_loop_terminates(self):
        kids = todone_core.children_index([
            {"id": "a", "parent_id": "b"},
            {"id": "b", "parent_id": "a"},
        ])
        views = todone_core.subtask_views("a", kids)
        assert views[0]["id"] == "b" and views[0]["subtasks"][0]["id"] == "a"
        assert views[0]["subtasks"][0]["subtasks"] == []

    def test_comment_count_keys(self):
        assert todone_core._comment_count({"note_count": 3}) == 3
        assert todone_core._comment_count({"comment_count": 2}) == 2
        assert todone_core._comment_count({}) == 0

    def test_completing_a_subtask_drops_it(self):
        app = make_app()
        app.refresh()
        snap = app.complete("t3b1")
        assert _find(snap, "t3")["subtasks"][1]["subtasks"] == []

    def test_adding_a_comment_counts_it(self):
        app = make_app()
        app.refresh()
        app.add_comment("t1", "hi")
        assert _find(app.refresh(), "t1")["comment_count"] == 1

    @settings(max_examples=200, deadline=None)
    @given(st.lists(st.tuples(st.integers(0, 15), st.integers(-1, 15)), max_size=16, unique_by=lambda p: p[0]))
    def test_every_child_appears_once_under_its_parent(self, pairs):
        """Property: in a parent forest, each task shows exactly once under the root it hangs from."""
        # Only parent pointers to a smaller id, so the input is a forest (no loops).
        tasks = [{"id": str(i), **({"parent_id": str(p)} if 0 <= p < i else {})} for i, p in pairs]
        kids = todone_core.children_index(tasks)
        ids = {t["id"] for t in tasks}
        roots = [t["id"] for t in tasks if "parent_id" not in t or t["parent_id"] not in ids]

        def walk(views):
            for v in views:
                yield v["id"]
                yield from walk(v["subtasks"])

        seen = [i for r in roots for i in walk(todone_core.subtask_views(r, kids))]
        attached = [t["id"] for t in tasks if t.get("parent_id") in ids]
        assert sorted(seen) == sorted(attached)


class TestQuickUpdate:
    def test_returns_a_snapshot_with_the_change(self):
        app = make_app()
        app.refresh()
        snap = app.quick_update("t3", {"content": "Outline the post", "priority": 1, "labels": ["writing", "deep"]})
        view = _find(snap, "t3")
        assert (view["content"], view["priority"], view["labels"]) == ("Outline the post", 1, ["writing", "deep"])

    def test_due_and_deadline_and_project(self):
        app = make_app()
        app.refresh()
        app.quick_update("t1", {"due_string": "tomorrow", "deadline": "2026-11-20", "project_id": "home"})
        t = app.services.client.tasks["t1"]
        assert t["due"]["date"] == (date.today() + timedelta(days=1)).isoformat()
        assert t["deadline"] == {"date": "2026-11-20"} and t["project_id"] == "home"

    def test_server_exposes_it(self):
        app = make_app()
        app.refresh()
        out = todone_server.handle(app, {"id": 9, "method": "quick_update",
                                         "params": {"task_id": "t1", "changes": {"priority": 2}}})
        assert _find(out["result"], "t1")["priority"] == 2

    def test_snapshot_lists_projects(self):
        snap = make_app().refresh()
        assert {"id": "home", "name": "Home"} in snap["projects"]
