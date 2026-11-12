"""Unit tests for planner.py — pure scheduling logic, no I/O."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from todoist_cli import planner

TZ = ZoneInfo("America/Los_Angeles")


def make_config(**overrides):
    defaults = dict(
        default_task_minutes=30,
        horizon_days=7,
        eligibility_days=14,
        timezone=TZ,
        account="someone@example.com",
        calendars=["primary"],
        days_off=set(),
        working_hours={i: (planner._parse_hm("09:00"), planner._parse_hm("14:00")) for i in range(5)},
    )
    defaults.update(overrides)
    return planner.PlanConfig(**defaults)


def make_task(
    id="t1",
    content="Do the thing",
    priority=1,
    due_date=None,
    deadline=None,
    project_id=None,
    parent_id=None,
    duration=None,
    is_recurring=False,
    due_datetime=None,
):
    task = {"id": id, "content": content, "priority": priority}
    if project_id is not None:
        task["project_id"] = project_id
    if parent_id is not None:
        task["parent_id"] = parent_id
    if duration is not None:
        task["duration"] = duration
    if due_date is not None or is_recurring or due_datetime is not None:
        task["due"] = {"date": due_date or (due_datetime[:10] if due_datetime else None)}
        if is_recurring:
            task["due"]["is_recurring"] = True
        if due_datetime is not None:
            task["due"]["datetime"] = due_datetime
    if deadline is not None:
        task["deadline"] = {"date": deadline}
    return task


# ---------------------------------------------------------------------------
# parse_size
# ---------------------------------------------------------------------------


class TestParseSize:
    def test_bare_minutes(self):
        assert planner.parse_size(make_task(content="Watch video about evals 30m"), 30) == 30

    def test_bracketed_hours(self):
        assert planner.parse_size(make_task(content="Transfer my DNS [3h]"), 30) == 180

    def test_combined_hours_and_minutes_contiguous_run(self):
        assert planner.parse_size(make_task(content="Deep work 1h30m"), 30) == 90

    def test_no_hint_uses_default(self):
        assert planner.parse_size(make_task(content="Call the dentist"), 30) == 30
        assert planner.parse_size(make_task(content="Call the dentist"), 15) == 15

    def test_only_last_contiguous_run_counts(self):
        # "5m" and "2h" are separated by other text, so only the trailing "2h" counts.
        assert planner.parse_size(make_task(content="5m walk, then 2h drive"), 30) == 120

    def test_does_not_match_digit_glued_to_other_word_chars(self):
        assert planner.parse_size(make_task(content="Read 'Identity Theft' report v2house"), 30) == 30
        assert planner.parse_size(make_task(content="Q3h1 planning doc"), 30) == 30

    def test_glue_is_measured_against_the_previous_token_not_the_first(self):
        # "30m" is glued to "1h", which is the token before it — not "5m", the
        # first one. The run is [1h, 30m], and it is the last run that counts.
        assert planner.parse_size(make_task(content="Deep work 5m then 1h30m"), 30) == 90

    def test_last_run_of_three_wins(self):
        # Three separate runs; the trailing "1h30m" is the estimate, and the
        # glued "30m" must join that run rather than the first one.
        assert planner.parse_size(make_task(content="5m a 2h b 1h30m"), 30) == 90

    def test_decimal_hours_are_read_whole(self):
        # "3.5h" is 3.5 hours, not the "5h" glued to the decimal point.
        assert planner.parse_size(make_task(content="Review 3.5h doc"), 30) == 210
        assert planner.parse_size(make_task(content="Edit and post podcast 1.5h"), 30) == 90

    def test_decimal_minutes_round_to_nearest(self):
        assert planner.parse_size(make_task(content="Stretch 2.6m"), 30) == 3

    def test_leading_dot_fraction_is_not_a_size_hint(self):
        # ".5h" has no integer part; the "5h" is glued to the dot and dropped.
        assert planner.parse_size(make_task(content="Review .5h doc"), 30) == 30

    def test_native_duration_minute_unit_wins_over_title(self):
        task = make_task(content="Something 2h", duration={"amount": 45, "unit": "minute"})
        assert planner.parse_size(task, 30) == 45

    def test_native_duration_day_unit(self):
        task = make_task(duration={"amount": 1, "unit": "day"})
        assert planner.parse_size(task, 30) == 24 * 60

    def test_zero_amount_duration_falls_back_to_title(self):
        task = make_task(content="30m task", duration={"amount": 0, "unit": "minute"})
        assert planner.parse_size(task, 15) == 30


# ---------------------------------------------------------------------------
# build_items
# ---------------------------------------------------------------------------


class TestBuildItems:
    def test_eligible_by_due_date(self):
        tasks = [make_task(id="a", due_date="2026-07-27")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert [i.id for i in items] == ["a"]

    def test_eligible_by_deadline(self):
        tasks = [make_task(id="a", deadline="2026-07-27")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert [i.id for i in items] == ["a"]

    def test_eligible_by_inbox_project(self):
        tasks = [make_task(id="a", project_id="inbox-id")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert [i.id for i in items] == ["a"]

    def test_ineligible_when_undated_and_not_inbox(self):
        tasks = [make_task(id="a", project_id="other-project")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items == []

    def test_far_future_due_date_deferred(self):
        tasks = [make_task(id="a", due_date="2027-05-01")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config(eligibility_days=14))
        assert items == []

    def test_overdue_has_no_lower_bound(self):
        # eligibility_days is an upper bound only; anything already overdue stays eligible.
        tasks = [make_task(id="a", due_date="2020-01-01")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert [i.id for i in items] == ["a"]

    def test_deadline_within_horizon_but_far_future_due_still_eligible(self):
        tasks = [make_task(id="a", due_date="2027-05-01", deadline="2026-07-28")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config(eligibility_days=14))
        assert [i.id for i in items] == ["a"]

    def test_parent_of_open_child_sized_zero(self):
        tasks = [
            make_task(id="parent", due_date="2026-07-27", content="Parent"),
            make_task(id="child", due_date="2026-07-27", parent_id="parent", content="Child 1h"),
        ]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        by_id = {i.id: i for i in items}
        assert by_id["parent"].minutes == 0
        assert by_id["child"].minutes == 60

    def test_parent_title_estimate_overrides_container_zero(self):
        tasks = [
            make_task(id="parent", due_date="2026-07-27", content="Edit and post interview 1.5h"),
            make_task(id="child", due_date="2026-07-27", parent_id="parent", content="Child"),
        ]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        by_id = {i.id: i for i in items}
        assert by_id["parent"].minutes == 90
        assert by_id["child"].minutes == 30

    def test_parent_native_duration_overrides_container_zero(self):
        tasks = [
            make_task(id="parent", due_date="2026-07-27", duration={"amount": 90, "unit": "minute"}),
            make_task(id="child", due_date="2026-07-27", parent_id="parent"),
        ]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert {i.id: i.minutes for i in items}["parent"] == 90

    def test_date_only_recurring_task_is_movable_by_default(self):
        tasks = [make_task(id="a", due_date="2026-07-27", is_recurring=True)]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is False
        assert items[0].soft_recurring is True

    def test_recurring_task_keeps_its_rule(self):
        task = make_task(id="a", due_date="2026-07-27", is_recurring=True)
        task["due"]["string"] = "every! 3 days"
        items = planner.build_items([task], "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].recurrence == "every! 3 days"

    def test_recurring_task_without_a_rule_string_is_still_recurring(self):
        tasks = [make_task(id="a", due_date="2026-07-27", is_recurring=True)]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].recurrence == ""

    def test_one_off_task_has_no_rule(self):
        tasks = [make_task(id="a", due_date="2026-07-27")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].recurrence is None

    @pytest.mark.parametrize("label", ["fixed", "Fixed"])
    def test_fixed_label_pins_a_recurring_task(self, label):
        task = make_task(id="a", due_date="2026-07-27", is_recurring=True)
        task["labels"] = ["errand", label]
        items = planner.build_items([task], "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is True
        assert items[0].soft_recurring is False

    def test_fixed_label_pins_a_one_off_dated_task(self):
        task = make_task(id="a", due_date="2026-07-27")
        task["labels"] = ["fixed"]
        items = planner.build_items([task], "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is True

    def test_fixed_label_does_not_pin_an_undated_task(self):
        # Pinning means "stays on its day"; with no day there is nothing to stay on.
        task = make_task(id="a", project_id="inbox-id")
        task["labels"] = ["fixed"]
        items = planner.build_items([task], "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is False

    def test_flex_label_is_the_default(self):
        task = make_task(id="a", due_date="2026-07-27", is_recurring=True)
        task["labels"] = ["flex"]
        items = planner.build_items([task], "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is False

    def test_timed_recurring_task_is_pinned(self):
        task = make_task(id="a", due_datetime="2026-07-27T15:00:00", is_recurring=True)
        items = planner.build_items([task], "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is True

    def test_timed_due_task_is_pinned(self):
        tasks = [make_task(id="a", due_datetime="2026-07-27T15:00:00")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is True

    def test_plain_dated_task_is_not_pinned(self):
        tasks = [make_task(id="a", due_date="2026-07-27")]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].pinned is False

    def test_user_facing_priority_conversion(self):
        # API priority 4 (urgent) -> user-facing P1.
        tasks = [make_task(id="a", due_date="2026-07-27", priority=4)]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].priority == 1

    def test_missing_priority_is_treated_as_lowest(self):
        # The API omits `priority` on some payloads; absent means p1 (normal) -> P4.
        tasks = [{"id": "a", "content": "No priority field", "due": {"date": "2026-07-27"}}]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].priority == 4

    def test_date_exactly_on_the_eligibility_horizon_is_kept(self):
        # eligibility_days is inclusive: the last eligible day is still in.
        today = date(2026, 7, 25)
        tasks = [make_task(id="a", due_date=(today + timedelta(days=14)).isoformat())]
        items = planner.build_items(tasks, "inbox-id", today, make_config(eligibility_days=14))
        assert [i.id for i in items] == ["a"]

    def test_date_one_day_past_the_eligibility_horizon_is_dropped(self):
        today = date(2026, 7, 25)
        tasks = [make_task(id="a", due_date=(today + timedelta(days=15)).isoformat())]
        items = planner.build_items(tasks, "inbox-id", today, make_config(eligibility_days=14))
        assert items == []

    def test_timestamped_due_date_reduced_to_its_calendar_day(self):
        # The API returns `due.date` as a full timestamp for time-of-day tasks.
        tasks = [{"id": "a", "content": "Standup", "priority": 1, "due": {"date": "2026-07-27T15:00:00"}}]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].due_date == date(2026, 7, 27)

    def test_timestamped_deadline_reduced_to_its_calendar_day(self):
        tasks = [
            {"id": "a", "content": "Ship", "priority": 1, "deadline": {"date": "2026-07-27T15:00:00"}}
        ]
        items = planner.build_items(tasks, "inbox-id", date(2026, 7, 25), make_config())
        assert items[0].deadline == date(2026, 7, 27)


# ---------------------------------------------------------------------------
# sort_key / effective_priority
# ---------------------------------------------------------------------------


class TestSortKey:
    def _item(self, priority, deadline=None, minutes=30, content="x", due_date=None):
        return planner.Item(
            id=content, content=content, priority=priority, due_date=due_date,
            deadline=deadline, minutes=minutes, pinned=False,
        )

    def test_deadline_promotes_two_levels(self):
        p4_with_deadline = self._item(4, deadline=date(2026, 7, 30))
        assert p4_with_deadline.effective_priority == 2

    def test_due_date_promotes_one_level(self):
        p4_with_due = self._item(4, due_date=date(2026, 7, 30))
        assert p4_with_due.effective_priority == 3

    def test_due_date_and_deadline_promotes_two_not_three(self):
        both = self._item(4, due_date=date(2026, 7, 29), deadline=date(2026, 7, 30))
        assert both.effective_priority == 2

    def test_deadline_promotion_clamps_at_one(self):
        p2_with_deadline = self._item(2, deadline=date(2026, 7, 30))
        p3_with_deadline = self._item(3, deadline=date(2026, 7, 30))
        p1_with_deadline = self._item(1, deadline=date(2026, 7, 30))
        assert p2_with_deadline.effective_priority == 1
        assert p3_with_deadline.effective_priority == 1
        assert p1_with_deadline.effective_priority == 1

    def test_due_date_promotion_clamps_at_one(self):
        assert self._item(1, due_date=date(2026, 7, 30)).effective_priority == 1

    def test_p3_deadline_p2_due_and_bare_p1_all_rank_the_same(self):
        p3_deadline = self._item(3, deadline=date(2026, 7, 30))
        p2_due = self._item(2, due_date=date(2026, 7, 30))
        p1_bare = self._item(1)
        assert p3_deadline.effective_priority == p2_due.effective_priority == p1_bare.effective_priority == 1

    def test_no_dates_priority_unchanged(self):
        assert self._item(3).effective_priority == 3

    def test_clamped_ties_break_on_real_priority(self):
        items = [self._item(1), self._item(3, deadline=date(2026, 7, 30)), self._item(2, deadline=date(2026, 7, 30))]
        ordered = sorted(items, key=lambda i: i.sort_key())
        # All three tie at effective_priority 1; real priority breaks the tie: 1, 2, 3.
        assert [i.priority for i in ordered] == [1, 2, 3]

    def test_due_date_breaks_tie_when_the_deadlines_match(self):
        # Same deadline and priority: the one that also named a due date is
        # more specific about when it wants doing, so it sorts first. The item
        # without one has to compare as "no earlier than anything".
        undated = self._item(3, deadline=date(2026, 7, 30), content="a")
        with_due = self._item(3, deadline=date(2026, 7, 30), due_date=date(2026, 7, 28), content="b")
        assert sorted([undated, with_due], key=planner.Item.sort_key) == [with_due, undated]

    def test_deadline_breaks_tie_before_size(self):
        earlier = self._item(2, deadline=date(2026, 7, 27), minutes=30)
        later = self._item(2, deadline=date(2026, 7, 29), minutes=180)
        assert sorted([later, earlier], key=lambda i: i.sort_key()) == [earlier, later]

    def test_size_desc_breaks_tie_before_alpha(self):
        small = self._item(2, content="A small one", minutes=30)
        big = self._item(2, content="Z big one", minutes=180)
        assert sorted([small, big], key=lambda i: i.sort_key()) == [big, small]

    def test_alphabetical_final_tiebreak(self):
        b = self._item(2, content="Banana", minutes=30)
        a = self._item(2, content="apple", minutes=30)
        assert sorted([b, a], key=lambda i: i.sort_key()) == [a, b]


# ---------------------------------------------------------------------------
# weekday_buckets
# ---------------------------------------------------------------------------


class TestWeekdayBuckets:
    def test_from_tuesday_spans_six_weekdays(self):
        # Tue Jul 28 2026 -> Tue Wed Thu Fri Mon Tue.
        buckets = planner.weekday_buckets(date(2026, 7, 28), 7)
        assert buckets == [
            date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30),
            date(2026, 7, 31), date(2026, 8, 3), date(2026, 8, 4),
        ]

    def test_from_saturday_skips_to_monday(self):
        buckets = planner.weekday_buckets(date(2026, 7, 25), 7)
        assert buckets[0] == date(2026, 7, 27)
        assert all(d.weekday() < 5 for d in buckets)

    def test_from_friday(self):
        buckets = planner.weekday_buckets(date(2026, 7, 31), 7)
        assert buckets == [
            date(2026, 7, 31), date(2026, 8, 3), date(2026, 8, 4),
            date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7),
        ]


# ---------------------------------------------------------------------------
# free_minutes
# ---------------------------------------------------------------------------


class TestFreeMinutes:
    def test_no_busy_returns_full_window(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, [], now)
        assert minutes == 300

    def test_busy_interval_subtracted(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [(datetime(2026, 7, 28, 10, 0, tzinfo=TZ), datetime(2026, 7, 28, 11, 0, tzinfo=TZ))]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        assert minutes == 240

    def test_overlapping_busy_intervals_merged_not_double_subtracted(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [
            (datetime(2026, 7, 28, 10, 0, tzinfo=TZ), datetime(2026, 7, 28, 11, 0, tzinfo=TZ)),
            (datetime(2026, 7, 28, 10, 30, tzinfo=TZ), datetime(2026, 7, 28, 11, 30, tzinfo=TZ)),
        ]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        assert minutes == 300 - 90  # merged into one 10:00-11:30 block

    def test_busy_outside_window_ignored(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [(datetime(2026, 7, 28, 20, 0, tzinfo=TZ), datetime(2026, 7, 28, 21, 0, tzinfo=TZ))]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        assert minutes == 300

    def test_busy_partially_overlapping_window_clamped(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [(datetime(2026, 7, 28, 8, 0, tzinfo=TZ), datetime(2026, 7, 28, 9, 30, tzinfo=TZ))]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        assert minutes == 300 - 30  # only 09:00-09:30 overlaps the window

    def test_today_window_clamped_to_now(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 28, 11, 0, tzinfo=TZ)
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, [], now)
        assert minutes == 180  # 11:00 -> 14:00

    def test_now_after_window_end_returns_zero(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 28, 18, 0, tzinfo=TZ)
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, [], now)
        assert minutes == 0

    def test_second_block_merges_against_itself_not_the_first(self):
        # A gap splits the day into two blocks; the 11:30 meeting overlaps the
        # second one. Merging it against the *first* block would leave it
        # counted separately and over-subtract.
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [
            (datetime(2026, 7, 28, 9, 0, tzinfo=TZ), datetime(2026, 7, 28, 10, 0, tzinfo=TZ)),
            (datetime(2026, 7, 28, 11, 0, tzinfo=TZ), datetime(2026, 7, 28, 13, 0, tzinfo=TZ)),
            (datetime(2026, 7, 28, 11, 30, tzinfo=TZ), datetime(2026, 7, 28, 12, 0, tzinfo=TZ)),
        ]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        # 60 busy (09:00-10:00) + 120 busy (11:00-13:00, the 11:30 nested inside it).
        assert minutes == 300 - 180

    def test_nested_busy_interval_does_not_shorten_the_block(self):
        # The inner meeting ends before the outer one; the merged block must
        # keep the later end, not adopt the inner one.
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [
            (datetime(2026, 7, 28, 10, 0, tzinfo=TZ), datetime(2026, 7, 28, 13, 0, tzinfo=TZ)),
            (datetime(2026, 7, 28, 11, 0, tzinfo=TZ), datetime(2026, 7, 28, 12, 0, tzinfo=TZ)),
        ]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        assert minutes == 300 - 180

    def test_fully_booked_day_is_zero_not_negative(self):
        day = date(2026, 7, 28)
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        busy = [(datetime(2026, 7, 28, 8, 0, tzinfo=TZ), datetime(2026, 7, 28, 15, 0, tzinfo=TZ))]
        minutes = planner.free_minutes(day, planner._parse_hm("09:00"), planner._parse_hm("14:00"), TZ, busy, now)
        assert minutes == 0


class TestComputeCapacity:
    def test_days_off_zeroed(self):
        config = make_config(days_off={date(2026, 7, 28)})
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        now = datetime(2026, 7, 27, 8, 0, tzinfo=TZ)
        capacity = planner.compute_capacity(buckets, [], config, now)
        assert capacity[date(2026, 7, 28)] == 0
        assert capacity[date(2026, 7, 29)] == 300


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


def item(id, priority=2, minutes=30, due_date=None, deadline=None, pinned=False, recurrence=None):
    return planner.Item(
        id=id, content=f"Task {id}", priority=priority, due_date=due_date,
        deadline=deadline, minutes=minutes, pinned=pinned, recurrence=recurrence,
    )


class TestPack:
    def test_fits_within_one_day(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 120, date(2026, 7, 29): 120}
        items = [item("a", minutes=60)]
        result = planner.pack(items, capacity)
        assert [c.item.id for c in result.schedule[date(2026, 7, 28)]] == ["a"]
        assert result.schedule[date(2026, 7, 29)] == []

    def test_task_splits_across_day_boundary(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 30, date(2026, 7, 29): 120}
        items = [item("a", minutes=90)]
        result = planner.pack(items, capacity)
        day1_chunks = result.schedule[date(2026, 7, 28)]
        day2_chunks = result.schedule[date(2026, 7, 29)]
        assert [(c.item.id, c.minutes) for c in day1_chunks] == [("a", 30)]
        assert [(c.item.id, c.minutes) for c in day2_chunks] == [("a", 60)]

    def test_earliest_date_used_for_multiday_task(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 30, date(2026, 7, 29): 120}
        items = [item("a", minutes=90)]
        result = planner.pack(items, capacity)
        earliest = planner.earliest_scheduled_dates(result, buckets)
        assert earliest["a"] == date(2026, 7, 28)

    def test_higher_priority_fills_first(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 60}
        items = [item("low", priority=4, minutes=60), item("high", priority=1, minutes=60)]
        result = planner.pack(items, capacity)
        assert [c.item.id for c in result.schedule[date(2026, 7, 28)]] == ["high"]
        assert [i.id for i in result.unscheduled] == ["low"]

    def test_container_consumes_no_capacity(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 30}
        items = [item("container", minutes=0), item("real", minutes=30)]
        result = planner.pack(items, capacity)
        ids = [c.item.id for c in result.schedule[date(2026, 7, 28)]]
        assert "container" in ids
        assert "real" in ids

    def test_pinned_item_consumes_capacity_but_is_not_moved(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 60}
        pinned = item("p", minutes=45, due_date=date(2026, 7, 28), pinned=True)
        movable = item("m", minutes=30)
        result = planner.pack([pinned, movable], capacity)
        chunks = result.schedule[date(2026, 7, 28)]
        assert ("p", 45) in [(c.item.id, c.minutes) for c in chunks]
        # Only 15m left after the pinned item, so the movable 30m task doesn't fully fit.
        assert movable.id in [i.id for i in result.unscheduled] or any(
            c.item.id == "m" and c.minutes == 15 for c in chunks
        )

    def test_pinned_item_larger_than_its_day_cannot_borrow_capacity(self):
        # An overlong pinned item empties its day; it must not push the day's
        # remaining capacity negative and lend a minute to the next task.
        day = date(2026, 7, 28)
        capacity = {day: 30}
        pinned = item("p", minutes=60, due_date=day, pinned=True)
        movable = item("m", minutes=30)
        result = planner.pack([pinned, movable], capacity)
        assert [c.item.id for c in result.schedule[day]] == ["p"]
        assert [i.id for i in result.unscheduled] == ["m"]

    def test_splits_down_to_a_single_minute(self):
        # "Splitting by the minute" is literal: a 1-minute remainder is placed
        # rather than rounded away.
        d1, d2 = date(2026, 7, 28), date(2026, 7, 29)
        result = planner.pack([item("a", minutes=30)], {d1: 29, d2: 60})
        assert [(c.day, c.minutes) for c in result.schedule[d1] + result.schedule[d2]] == [
            (d1, 29),
            (d2, 1),
        ]
        assert result.at_risk == []
        assert result.unscheduled == []

    def test_a_day_with_one_free_minute_is_still_used(self):
        d1, d2 = date(2026, 7, 28), date(2026, 7, 29)
        result = planner.pack([item("a", minutes=30)], {d1: 1, d2: 60})
        assert [c.minutes for c in result.schedule[d1]] == [1]
        assert [c.minutes for c in result.schedule[d2]] == [29]

    def test_a_single_unplaced_minute_still_counts_as_at_risk(self):
        day = date(2026, 7, 28)
        result = planner.pack([item("a", minutes=30, deadline=day)], {day: 29})
        assert [c.minutes for c in result.schedule[day]] == [29]
        assert [i.id for i in result.at_risk] == ["a"]

    def test_pinned_item_outside_horizon_ignored(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 60}
        pinned = item("p", minutes=45, due_date=date(2026, 8, 15), pinned=True)
        result = planner.pack([pinned], capacity)
        assert result.schedule[date(2026, 7, 28)] == []
        # Not placed, but not silently dropped either.
        assert [i.id for i in result.unplanned] == ["p"]
        assert result.unscheduled == []
        assert result.at_risk == []

    def test_pinned_item_on_a_weekend_inside_the_horizon_is_unplanned(self):
        # Sat Aug 1 falls between the buckets: the plan can't place it and
        # won't move it, so it is reported rather than dropped.
        buckets = [date(2026, 7, 31), date(2026, 8, 3)]
        capacity = {d: 60 for d in buckets}
        pinned = item("p", minutes=45, due_date=date(2026, 8, 1), pinned=True)
        result = planner.pack([pinned], capacity)
        assert [i.id for i in result.unplanned] == ["p"]
        assert all(result.schedule[d] == [] for d in buckets)

    def test_pinned_item_the_plan_places_is_not_unplanned(self):
        day = date(2026, 7, 28)
        result = planner.pack([item("p", minutes=45, due_date=day, pinned=True)], {day: 60})
        assert result.unplanned == []

    def test_overdue_pinned_item_is_placed_not_unplanned(self):
        # The clamp to the first bucket happens before the unplanned check, so
        # overdue work is scheduled rather than merely reported.
        day = date(2026, 7, 28)
        pinned = item("p", minutes=45, due_date=date(2026, 7, 20), pinned=True)
        result = planner.pack([pinned], {day: 60})
        assert result.unplanned == []
        assert [c.item.id for c in result.schedule[day]] == ["p"]

    def test_pinned_item_without_a_due_date_is_unplanned(self):
        day = date(2026, 7, 28)
        result = planner.pack([item("p", minutes=45, pinned=True)], {day: 60})
        assert [i.id for i in result.unplanned] == ["p"]

    def test_no_buckets_leaves_every_pinned_item_unplanned(self):
        # The empty-horizon early return has its own PlanResult; it must carry
        # the pinned items too, or they vanish exactly as before.
        pinned = item("p", minutes=45, due_date=date(2026, 7, 28), pinned=True)
        movable = item("m", minutes=30)
        result = planner.pack([pinned, movable], {})
        assert [i.id for i in result.unplanned] == ["p"]
        assert [i.id for i in result.unscheduled] == ["m"]

    def test_unplanned_item_consumes_no_capacity(self):
        day = date(2026, 7, 28)
        pinned = item("p", minutes=45, due_date=date(2026, 8, 15), pinned=True)
        movable = item("m", minutes=60)
        result = planner.pack([pinned, movable], {day: 60})
        assert [(c.item.id, c.minutes) for c in result.schedule[day]] == [("m", 60)]


    def test_overdue_pinned_item_lands_on_the_first_bucket(self):
        # A missed recurring or timed task is still outstanding, and its own day
        # is gone: dropping it would hide it from the plan entirely and hand the
        # first day capacity it doesn't really have.
        d1, d2 = date(2026, 7, 28), date(2026, 7, 29)
        capacity = {d1: 60, d2: 60}
        pinned = item("p", minutes=45, due_date=date(2026, 7, 20), pinned=True)
        result = planner.pack([pinned], capacity)
        assert [(c.item.id, c.day, c.minutes) for c in result.schedule[d1]] == [("p", d1, 45)]
        assert result.schedule[d2] == []

    def test_overdue_pinned_item_consumes_first_bucket_capacity(self):
        d1 = date(2026, 7, 28)
        capacity = {d1: 60}
        pinned = item("p", minutes=45, due_date=date(2026, 7, 20), pinned=True)
        movable = item("m", minutes=30)
        result = planner.pack([pinned, movable], capacity)
        # Only 15m is left for the movable task, so it is split, not fully placed.
        assert [(c.item.id, c.minutes) for c in result.schedule[d1]] == [("p", 45), ("m", 15)]
        assert [i.id for i in result.unscheduled] == ["m"]

    def test_overdue_pinned_item_keeps_its_due_date(self):
        # Placed on the first bucket, but never re-dated: the date written back
        # would drop the recurrence pattern or the time of day.
        d1 = date(2026, 7, 28)
        pinned = item("p", minutes=45, due_date=date(2026, 7, 20), pinned=True)
        result = planner.pack([pinned], {d1: 60})
        assert pinned.due_date == date(2026, 7, 20)
        final = planner.earliest_scheduled_dates(result, [d1])
        assert planner.park_overdue_dates(final, [pinned], d1, d1) == {"p": d1}

    def test_unscheduled_without_deadline_is_not_at_risk(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        items = [item("a", minutes=30)]
        result = planner.pack(items, capacity)
        assert result.at_risk == []
        assert [i.id for i in result.unscheduled] == ["a"]

    def test_unscheduled_with_deadline_in_horizon_is_at_risk(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        items = [item("a", minutes=30, deadline=date(2026, 7, 28))]
        result = planner.pack(items, capacity)
        assert [i.id for i in result.at_risk] == ["a"]
        assert result.unscheduled == []

    def test_scheduled_but_finishing_after_deadline_is_at_risk(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 30, date(2026, 7, 29): 60}
        items = [item("a", minutes=90, deadline=date(2026, 7, 28))]
        result = planner.pack(items, capacity)
        assert [i.id for i in result.at_risk] == ["a"]

    def test_scheduled_finishing_on_or_before_deadline_not_at_risk(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 30, date(2026, 7, 29): 60}
        items = [item("a", minutes=90, deadline=date(2026, 7, 29))]
        result = planner.pack(items, capacity)
        assert result.at_risk == []

    def test_container_at_risk_if_anchor_falls_after_deadline(self):
        # Deadline already past: the anchor clamps forward to the first bucket,
        # which is later than the deadline, so the container can't make it.
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 60, date(2026, 7, 29): 60}
        items = [item("container", minutes=0, deadline=date(2026, 7, 24))]
        result = planner.pack(items, capacity)
        assert [i.id for i in result.at_risk] == ["container"]

    def test_container_lands_on_its_anchor_day(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {d: 60 for d in buckets}
        items = [item("container", minutes=0, deadline=date(2026, 7, 30))]
        result = planner.pack(items, capacity)
        # deadline - 1, and unaffected by whatever else is competing for capacity.
        assert [c.item.id for c in result.schedule[date(2026, 7, 29)]] == ["container"]
        assert result.at_risk == []

    def test_container_chunk_is_zero_minutes(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {d: 60 for d in buckets}
        result = planner.pack([item("container", minutes=0, deadline=date(2026, 7, 29))], capacity)
        assert [c.minutes for c in result.schedule[date(2026, 7, 28)]] == [0]

    def test_container_landing_exactly_on_its_deadline_is_not_at_risk(self):
        # The single bucket is the deadline day itself — late would be *after* it.
        day = date(2026, 7, 28)
        result = planner.pack([item("container", minutes=0, deadline=day)], {day: 60})
        assert [c.item.id for c in result.schedule[day]] == ["container"]
        assert result.at_risk == []


class TestUnplannedReason:
    BUCKETS = [date(2026, 7, 28), date(2026, 7, 31)]

    def test_past_the_horizon(self):
        reason = planner.unplanned_reason(item("p", due_date=date(2026, 8, 15)), self.BUCKETS)
        assert reason == "past the horizon"

    def test_day_after_the_last_bucket_is_past_the_horizon(self):
        reason = planner.unplanned_reason(item("p", due_date=date(2026, 8, 1)), self.BUCKETS)
        assert reason == "past the horizon"

    def test_non_working_day_inside_the_horizon(self):
        # Wed Jul 29 isn't a bucket here, but it is before the last one.
        reason = planner.unplanned_reason(item("p", due_date=date(2026, 7, 29)), self.BUCKETS)
        assert reason == "not a working day"

    def test_the_last_bucket_itself_is_not_past_the_horizon(self):
        reason = planner.unplanned_reason(item("p", due_date=date(2026, 7, 31)), self.BUCKETS)
        assert reason == "not a working day"

    def test_no_due_date(self):
        assert planner.unplanned_reason(item("p"), self.BUCKETS) == "no due date"

    def test_no_buckets(self):
        reason = planner.unplanned_reason(item("p", due_date=date(2026, 7, 28)), [])
        assert reason == "no days in the horizon"


class TestPackDeadlinePass:
    def test_lands_the_day_before_the_deadline_not_today(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {d: 120 for d in buckets}
        items = [item("a", minutes=60, deadline=date(2026, 7, 30))]
        result = planner.pack(items, capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == date(2026, 7, 29)

    def test_due_date_earlier_than_deadline_wins_as_anchor(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {d: 120 for d in buckets}
        items = [item("a", minutes=60, due_date=date(2026, 7, 28), deadline=date(2026, 7, 30))]
        result = planner.pack(items, capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == date(2026, 7, 28)

    def test_due_date_after_deadline_clamped_back_to_day_before(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {d: 120 for d in buckets}
        items = [item("a", minutes=60, due_date=date(2026, 7, 30), deadline=date(2026, 7, 30))]
        result = planner.pack(items, capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == date(2026, 7, 29)

    def test_spills_backward_when_the_anchor_day_is_too_small(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {date(2026, 7, 28): 120, date(2026, 7, 29): 30, date(2026, 7, 30): 120}
        items = [item("a", minutes=90, deadline=date(2026, 7, 30))]
        result = planner.pack(items, capacity)
        placed = {d: sum(c.minutes for c in result.schedule[d] if c.item.id == "a") for d in buckets}
        assert placed == {date(2026, 7, 28): 60, date(2026, 7, 29): 30, date(2026, 7, 30): 0}
        assert result.at_risk == []

    def test_uses_the_deadline_day_itself_before_spilling_past_it(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {date(2026, 7, 28): 0, date(2026, 7, 29): 30, date(2026, 7, 30): 60}
        items = [item("a", minutes=90, deadline=date(2026, 7, 30))]
        result = planner.pack(items, capacity)
        placed = {d: sum(c.minutes for c in result.schedule[d] if c.item.id == "a") for d in buckets}
        assert placed == {date(2026, 7, 28): 0, date(2026, 7, 29): 30, date(2026, 7, 30): 60}
        assert result.at_risk == []

    def test_spills_past_the_deadline_and_is_flagged_at_risk(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {date(2026, 7, 28): 0, date(2026, 7, 29): 30, date(2026, 7, 30): 60}
        items = [item("a", minutes=90, deadline=date(2026, 7, 29))]
        result = planner.pack(items, capacity)
        # Anchor is the 28th (deadline - 1), which is full; the 29th covers 30m,
        # and the last 60m lands on the 30th, past the deadline.
        assert [(c.item.id, c.minutes) for c in result.schedule[date(2026, 7, 30)]] == [("a", 60)]
        assert [i.id for i in result.at_risk] == ["a"]

    def test_weekend_anchor_snaps_back_to_friday(self):
        # Deadline Sunday Aug 2 -> anchor Sat Aug 1 -> snaps back to Fri Jul 31.
        buckets = planner.weekday_buckets(date(2026, 7, 27), 7)
        capacity = {d: 120 for d in buckets}
        items = [item("a", minutes=60, deadline=date(2026, 8, 2))]
        result = planner.pack(items, capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == date(2026, 7, 31)


class TestAnchorIndex:
    """Buckets are weekdays only, so a target can land on a day that isn't one."""

    BUCKETS = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]

    def test_target_before_the_horizon_clamps_to_the_first_bucket(self):
        assert planner._anchor_index(self.BUCKETS, date(2026, 7, 1), prefer="earlier") == 0

    def test_target_past_the_horizon_clamps_to_the_last_bucket(self):
        assert planner._anchor_index(self.BUCKETS, date(2026, 9, 1), prefer="later") == 2
        assert planner._anchor_index(self.BUCKETS, date(2026, 9, 1), prefer="earlier") == 2

    def test_exact_match_lands_on_its_own_bucket(self):
        assert planner._anchor_index(self.BUCKETS, date(2026, 7, 29), prefer="later") == 1
        assert planner._anchor_index(self.BUCKETS, date(2026, 7, 29), prefer="earlier") == 1

    def test_missing_day_snaps_by_preference(self):
        # Fri Jul 31 and Mon Aug 3 with the weekend between them.
        buckets = [date(2026, 7, 31), date(2026, 8, 3)]
        saturday = date(2026, 8, 1)
        assert planner._anchor_index(buckets, saturday, prefer="earlier") == 0
        assert planner._anchor_index(buckets, saturday, prefer="later") == 1


class TestPackDeadlineSlack:
    """An item carrying both dates named the day it wants to be worked on and the
    day it must be done, so the days between the two are used before the days back
    toward today. Mon Aug 3 .. Fri Aug 7, then Mon Aug 10.
    """

    MON, TUE, WED, THU, FRI, MON2 = (date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10))

    def placed(self, result, id):
        return {
            d: sum(c.minutes for c in chunks if c.item.id == id)
            for d, chunks in result.schedule.items()
            if any(c.item.id == id for c in chunks)
        }

    def caps(self, **overrides):
        capacity = {d: 240 for d in (self.MON, self.TUE, self.WED, self.THU, self.FRI, self.MON2)}
        capacity.update({getattr(self, name): minutes for name, minutes in overrides.items()})
        return capacity

    def both(self, minutes=90):
        return item("a", minutes=minutes, due_date=self.TUE, deadline=self.FRI)

    def test_spills_into_the_slack_before_crowding_today(self):
        capacity = self.caps(TUE=30)
        result = planner.pack([self.both()], capacity)
        assert self.placed(result, "a") == {self.TUE: 30, self.WED: 60}
        assert result.at_risk == []

    def test_the_due_date_survives_the_write_back(self):
        # The overflow going forward instead of backward is what keeps the anchor
        # the earliest chunk — and the earliest chunk is what gets written to Todoist.
        capacity = self.caps(TUE=30)
        result = planner.pack([self.both()], capacity)
        buckets = sorted(capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == self.TUE

    def test_the_day_before_the_deadline_counts_as_slack(self):
        # Slack runs up to and including the day before the deadline. With WED
        # empty, THU is still slack and is preferred over crowding back to MON.
        capacity = self.caps(TUE=30, WED=0)
        result = planner.pack([self.both()], capacity)
        assert self.placed(result, "a") == {self.TUE: 30, self.THU: 60}
        assert result.at_risk == []

    def test_falls_back_toward_today_before_spending_the_deadline_day(self):
        # Slack gone, but both today and the deadline day are free. The deadline
        # day is the last day that isn't late, so it stays in reserve while there
        # is still room earlier in the week.
        capacity = self.caps(TUE=30, WED=0, THU=0)
        result = planner.pack([self.both()], capacity)
        assert self.placed(result, "a") == {self.MON: 60, self.TUE: 30}
        assert result.at_risk == []

    def test_uses_the_deadline_day_only_once_everything_earlier_is_full(self):
        capacity = self.caps(MON=0, TUE=30, WED=0, THU=0)
        result = planner.pack([self.both()], capacity)
        assert self.placed(result, "a") == {self.TUE: 30, self.FRI: 60}
        assert result.at_risk == []

    def test_spills_past_the_deadline_last_and_is_flagged_at_risk(self):
        capacity = self.caps(MON=0, TUE=30, WED=0, THU=0, FRI=0)
        result = planner.pack([self.both()], capacity)
        assert self.placed(result, "a") == {self.TUE: 30, self.MON2: 60}
        assert [i.id for i in result.at_risk] == ["a"]

    def test_deadline_without_a_due_date_still_spills_backward(self):
        # No due date means no stated preference about which day, so the deadline
        # is a pure ceiling and the old count-backwards behavior stands.
        capacity = self.caps(THU=30)
        result = planner.pack([item("a", minutes=90, deadline=self.FRI)], capacity)
        assert self.placed(result, "a") == {self.WED: 60, self.THU: 30}
        assert result.at_risk == []

    def test_due_date_at_the_deadline_leaves_no_slack(self):
        # due == deadline: the anchor is already deadline - 1, so there is nothing
        # between the two and the item keeps spilling backward.
        capacity = self.caps(THU=30)
        result = planner.pack(
            [item("a", minutes=90, due_date=self.FRI, deadline=self.FRI)], capacity
        )
        assert self.placed(result, "a") == {self.WED: 60, self.THU: 30}


class TestPackDuePass:
    def test_lands_on_its_due_date(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {d: 120 for d in buckets}
        items = [item("a", minutes=60, due_date=date(2026, 7, 29))]
        result = planner.pack(items, capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == date(2026, 7, 29)

    def test_spills_later_before_earlier(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {date(2026, 7, 28): 120, date(2026, 7, 29): 30, date(2026, 7, 30): 120}
        items = [item("a", minutes=90, due_date=date(2026, 7, 29))]
        result = planner.pack(items, capacity)
        placed = {d: sum(c.minutes for c in result.schedule[d] if c.item.id == "a") for d in buckets}
        assert placed == {date(2026, 7, 28): 0, date(2026, 7, 29): 30, date(2026, 7, 30): 60}

    def test_falls_back_to_earlier_days_once_later_ones_are_full(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)]
        capacity = {date(2026, 7, 28): 120, date(2026, 7, 29): 30, date(2026, 7, 30): 0}
        items = [item("a", minutes=90, due_date=date(2026, 7, 29))]
        result = planner.pack(items, capacity)
        placed = {d: sum(c.minutes for c in result.schedule[d] if c.item.id == "a") for d in buckets}
        assert placed == {date(2026, 7, 28): 60, date(2026, 7, 29): 30, date(2026, 7, 30): 0}
        assert result.at_risk == []

    def test_landing_after_the_due_date_is_not_at_risk(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 0, date(2026, 7, 29): 60}
        items = [item("a", minutes=60, due_date=date(2026, 7, 28))]
        result = planner.pack(items, capacity)
        assert result.at_risk == []
        assert result.unscheduled == []

    def test_due_only_item_that_does_not_fit_is_at_risk(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        items = [item("a", minutes=30, due_date=date(2026, 7, 28))]
        result = planner.pack(items, capacity)
        assert [i.id for i in result.at_risk] == ["a"]
        assert result.unscheduled == []

    def test_weekend_due_date_snaps_forward_to_monday(self):
        # Due Sunday Aug 2 -> snaps forward to Mon Aug 3.
        buckets = planner.weekday_buckets(date(2026, 7, 27), 7)
        capacity = {d: 120 for d in buckets}
        items = [item("a", minutes=60, due_date=date(2026, 8, 2))]
        result = planner.pack(items, capacity)
        assert planner.earliest_scheduled_dates(result, buckets)["a"] == date(2026, 8, 3)


class TestPackBeyondHorizon:
    def test_far_deadline_does_not_outrank_work_due_now(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 60, date(2026, 7, 29): 60}
        far = item("far", priority=3, minutes=60, deadline=date(2026, 8, 20))
        due_now = item("due-now", priority=3, minutes=60, due_date=date(2026, 7, 28))
        result = planner.pack([far, due_now], capacity)
        # Both are in pass 2, so the anchor decides: due-now takes its own day.
        assert [c.item.id for c in result.schedule[date(2026, 7, 28)]] == ["due-now"]
        assert [c.item.id for c in result.schedule[date(2026, 7, 29)]] == ["far"]

    def test_far_deadline_that_does_not_fit_is_not_at_risk(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        items = [item("a", minutes=30, deadline=date(2026, 8, 20))]
        result = planner.pack(items, capacity)
        # There are days after this plan in which to do it — nothing is in trouble.
        assert result.at_risk == []
        assert [i.id for i in result.unscheduled] == ["a"]

    def test_far_due_date_that_does_not_fit_is_not_at_risk(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        items = [item("a", minutes=30, due_date=date(2026, 8, 20))]
        result = planner.pack(items, capacity)
        assert result.at_risk == []
        assert [i.id for i in result.unscheduled] == ["a"]

    def test_deadline_on_the_last_day_is_still_urgent(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 0, date(2026, 7, 29): 0}
        items = [item("a", minutes=30, deadline=date(2026, 7, 29))]
        result = planner.pack(items, capacity)
        assert [i.id for i in result.at_risk] == ["a"]

    def test_far_deadline_with_a_due_date_inside_the_horizon_is_at_risk(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        items = [item("a", minutes=30, due_date=date(2026, 7, 28), deadline=date(2026, 8, 20))]
        result = planner.pack(items, capacity)
        assert [i.id for i in result.at_risk] == ["a"]

    def test_no_buckets_leaves_everything_unscheduled(self):
        result = planner.pack([item("a", minutes=30, deadline=date(2026, 7, 28))], {})
        assert result.at_risk == []
        assert [i.id for i in result.unscheduled] == ["a"]


class TestPackPassOrdering:
    def test_deadline_item_beats_a_higher_priority_due_item_for_the_same_day(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 60, date(2026, 7, 29): 60}
        # Both want the 28th; the p4 deadline item is in pass 1 so it wins,
        # even though the p1 due item has the better effective priority.
        deadline_item = item("late-p4", priority=4, minutes=60, deadline=date(2026, 7, 29))
        due_item = item("early-p1", priority=1, minutes=60, due_date=date(2026, 7, 28))
        result = planner.pack([due_item, deadline_item], capacity)
        assert [c.item.id for c in result.schedule[date(2026, 7, 28)]] == ["late-p4"]
        assert [c.item.id for c in result.schedule[date(2026, 7, 29)]] == ["early-p1"]

    def test_undated_work_only_gets_what_the_dated_passes_leave(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 60, date(2026, 7, 29): 30}
        dated = item("dated", priority=4, minutes=60, due_date=date(2026, 7, 28))
        loose = item("loose", priority=1, minutes=60)
        result = planner.pack([loose, dated], capacity)
        assert [c.item.id for c in result.schedule[date(2026, 7, 28)]] == ["dated"]
        # "loose" is half-placed: it holds a 30m chunk AND counts as unscheduled,
        # because _consume commits what it can before giving up. That dual state
        # is a wart, not a design goal — it's asserted here to pin current
        # behavior, not to bless it.
        assert [(c.item.id, c.minutes) for c in result.schedule[date(2026, 7, 29)]] == [("loose", 30)]
        assert [i.id for i in result.unscheduled] == ["loose"]
        assert result.at_risk == []

    def test_chunks_within_a_day_are_ordered_by_priority(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 120}
        low = item("low", priority=4, minutes=30, due_date=date(2026, 7, 28))
        high = item("high", priority=1, minutes=30, due_date=date(2026, 7, 28))
        result = planner.pack([low, high], capacity)
        assert [c.item.id for c in result.schedule[date(2026, 7, 28)]] == ["high", "low"]


class TestParkOverdueDates:
    TODAY = date(2026, 7, 28)
    LAST = date(2026, 8, 4)
    PAST = date(2026, 7, 20)

    def park(self, final_dates, items):
        return planner.park_overdue_dates(final_dates, items, self.TODAY, self.LAST)

    def test_overdue_item_with_no_slot_is_parked(self):
        # The whole point: it fit nowhere, so it kept a date in the past.
        overdue = item("a", due_date=self.PAST)
        assert self.park({}, [overdue]) == {"a": self.LAST}

    def test_item_due_today_with_no_slot_is_left_alone(self):
        # Today is not the past — there is nothing to correct, so no write.
        assert self.park({}, [item("a", due_date=self.TODAY)]) == {}

    def test_overdue_item_the_packer_placed_keeps_its_bucket_date(self):
        placed = {"a": date(2026, 7, 29)}
        assert self.park(placed, [item("a", due_date=self.PAST)]) == placed

    def test_overdue_item_the_packer_placed_on_today_keeps_today(self):
        # The common case: overdue work that fits is anchored on the first
        # bucket. Parking it would push work that IS scheduled to the end of
        # the horizon.
        placed = {"a": self.TODAY}
        assert self.park(placed, [item("a", due_date=self.PAST)]) == placed

    def test_overdue_pinned_item_is_left_alone(self):
        # Recurring/timed: the only writable field would drop the recurrence.
        assert self.park({}, [item("a", due_date=self.PAST, pinned=True)]) == {}

    def test_undated_item_with_no_slot_is_left_alone(self):
        assert self.park({}, [item("a")]) == {}

    def test_item_with_only_a_past_deadline_is_left_alone(self):
        # A deadline legitimately sits in the past, and is never written anyway.
        assert self.park({}, [item("a", deadline=self.PAST)]) == {}

    def test_explicit_past_date_from_the_editor_is_clamped(self):
        assert self.park({"a": self.PAST}, [item("a", due_date=self.PAST)]) == {"a": self.LAST}

    def test_input_dict_is_not_mutated(self):
        original = {"a": self.PAST}
        self.park(original, [item("a", due_date=self.PAST)])
        assert original == {"a": self.PAST}

    def test_other_items_dates_are_preserved(self):
        placed = {"b": date(2026, 7, 29)}
        parked = self.park(placed, [item("a", due_date=self.PAST), item("b", due_date=self.PAST)])
        assert parked == {"a": self.LAST, "b": date(2026, 7, 29)}


# ---------------------------------------------------------------------------
# format/parse minutes, capacity edits
# ---------------------------------------------------------------------------


class TestFormatMinutes:
    @pytest.mark.parametrize(
        "minutes,expected",
        [
            (0, "0m"),
            (1, "1m"),
            (30, "30m"),
            (60, "1h"),
            (65, "1h05m"),  # minutes are zero-padded so the column lines up
            (90, "1h30m"),
            (150, "2h30m"),
            (-5, "0m"),
        ],
    )
    def test_format(self, minutes, expected):
        assert planner.format_minutes(minutes) == expected


class TestParseMinutes:
    @pytest.mark.parametrize(
        "text,expected", [("0", 0), ("30m", 30), ("2h", 120), ("1h30m", 90), ("0m", 0)]
    )
    def test_parse(self, text, expected):
        assert planner.parse_minutes(text) == expected

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="can't parse duration: 'banana'"):
            planner.parse_minutes("banana")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="can't parse duration"):
            planner.parse_minutes("")


class TestParseCapacityEdits:
    def test_single_edit_applied(self):
        capacity = {date(2026, 7, 28): 300, date(2026, 7, 29): 60}
        updated = planner.parse_capacity_edits("07-28=1h30m", capacity)
        assert updated[date(2026, 7, 28)] == 90
        assert updated[date(2026, 7, 29)] == 60  # untouched

    def test_multiple_edits(self):
        capacity = {date(2026, 7, 28): 300, date(2026, 7, 31): 60}
        updated = planner.parse_capacity_edits("07-28=1h30m 07-31=0", capacity)
        assert updated[date(2026, 7, 28)] == 90
        assert updated[date(2026, 7, 31)] == 0

    def test_unknown_date_raises(self):
        capacity = {date(2026, 7, 28): 300}
        with pytest.raises(ValueError, match="not one of the planned days"):
            planner.parse_capacity_edits("07-29=1h", capacity)

    def test_bad_format_raises(self):
        capacity = {date(2026, 7, 28): 300}
        with pytest.raises(ValueError, match="bad edit 'nonsense', expected MM-DD=DURATION"):
            planner.parse_capacity_edits("nonsense", capacity)

    def test_unparseable_date_raises(self):
        capacity = {date(2026, 7, 28): 300}
        with pytest.raises(ValueError, match="bad date 'xx-yy', expected MM-DD"):
            planner.parse_capacity_edits("xx-yy=1h", capacity)

    def test_unknown_date_names_the_key(self):
        capacity = {date(2026, 7, 28): 300}
        with pytest.raises(ValueError, match="07-29 is not one of the planned days"):
            planner.parse_capacity_edits("07-29=1h", capacity)

    def test_month_day_repeated_across_the_horizon_is_ambiguous(self):
        # Keyed by MM-DD, so a horizon long enough to wrap a year has two
        # candidates and no way to tell which one was meant.
        capacity = {date(2026, 7, 28): 300, date(2027, 7, 28): 300}
        with pytest.raises(ValueError, match="07-28 is ambiguous across the horizon"):
            planner.parse_capacity_edits("07-28=1h", capacity)


# ---------------------------------------------------------------------------
# $EDITOR render/parse round trip
# ---------------------------------------------------------------------------


class TestEditorRoundTrip:
    def _sample_result(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 60, date(2026, 7, 29): 120}
        items = [item("a", minutes=30, priority=1), item("b", minutes=90, deadline=date(2026, 7, 20))]
        result = planner.pack(items, capacity)
        return result, buckets, capacity

    def test_render_lists_multiday_task_once_at_earliest_date(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 30, date(2026, 7, 29): 120}
        result = planner.pack([item("a", minutes=90)], capacity)
        text = planner.render_editor_text(result, buckets, capacity)
        assert text.count("a\t") == 1
        assert "a\t2026-07-28\t" in text

    def test_render_row_has_all_five_tab_separated_columns(self):
        # The renderer's columns are a contract with the human reading it, not
        # just with parse_editor_text, which only reads the first two.
        day = date(2026, 7, 28)
        result = planner.pack([item("a", minutes=90, priority=1)], {day: 120})
        text = planner.render_editor_text(result, [day], {day: 120})
        assert "a\t2026-07-28\tP1\t1h30m\tTask a" in text

    def test_render_day_header_names_the_day_and_its_free_time(self):
        day = date(2026, 7, 28)  # a Tuesday
        result = planner.pack([], {day: 90})
        text = planner.render_editor_text(result, [day], {day: 90})
        assert "# Tue Jul 28 -- 1h30m free" in text

    def test_render_at_risk_row_has_all_five_columns(self):
        day = date(2026, 7, 28)
        result = planner.pack([item("a", minutes=30, priority=1, deadline=day)], {day: 0})
        text = planner.render_editor_text(result, [day], {day: 0})
        assert "a\t-\tP1\t30m\tTask a (deadline 2026-07-28)" in text

    def test_render_lists_unplanned_pinned_work_as_comments(self):
        # Visible, but never an editable row: a pinned date is never written,
        # so a DATE column here would be a control that does nothing.
        buckets = [date(2026, 7, 31), date(2026, 8, 3)]
        capacity = {d: 60 for d in buckets}
        weekend = item("wk", minutes=30, due_date=date(2026, 8, 1), pinned=True)
        result = planner.pack([weekend], capacity)
        text = planner.render_editor_text(result, buckets, capacity)
        assert "Not planned" in text
        assert "not a working day" in text
        edits, warnings = planner.parse_editor_text(text)
        assert "wk" not in edits

    def test_render_omits_the_unplanned_block_when_there_is_none(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 60}
        result = planner.pack([item("a", minutes=30, due_date=date(2026, 7, 28))], capacity)
        assert "Not planned" not in planner.render_editor_text(result, buckets, capacity)

    def test_render_marks_pinned(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 60}
        result = planner.pack([item("a", minutes=30, due_date=date(2026, 7, 28), pinned=True)], capacity)
        text = planner.render_editor_text(result, buckets, capacity)
        assert "(pinned)" in text

    def test_render_includes_at_risk_section_with_dash_date(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        result = planner.pack([item("a", minutes=30, deadline=date(2026, 7, 28))], capacity)
        text = planner.render_editor_text(result, buckets, capacity)
        assert "At risk" in text
        assert "a\t-\t" in text

    def test_render_names_the_due_date_when_there_is_no_deadline(self):
        buckets = [date(2026, 7, 28)]
        capacity = {date(2026, 7, 28): 0}
        result = planner.pack([item("a", minutes=30, due_date=date(2026, 7, 28))], capacity)
        text = planner.render_editor_text(result, buckets, capacity)
        assert "(due 2026-07-28)" in text
        assert "deadline -" not in text

    def test_render_lists_scheduled_but_late_item_as_a_comment(self):
        buckets = [date(2026, 7, 28), date(2026, 7, 29)]
        capacity = {date(2026, 7, 28): 30, date(2026, 7, 29): 60}
        result = planner.pack([item("a", minutes=90, deadline=date(2026, 7, 28))], capacity)
        text = planner.render_editor_text(result, buckets, capacity)
        assert "At risk" in text
        assert "-- scheduled 2026-07-28, edit above" in text
        # One editable row (the day section), and no second one under At risk.
        assert text.count("a\t") == 1
        edits, warnings = planner.parse_editor_text(text)
        assert edits == {"a": date(2026, 7, 28)}
        assert warnings == []

    def test_parse_reads_id_and_date(self):
        text = "abc123\t2026-07-28\tP1\t30m\tSome task"
        edits, warnings = planner.parse_editor_text(text)
        assert edits == {"abc123": date(2026, 7, 28)}
        assert warnings == []

    def test_parse_dash_means_explicit_none(self):
        text = "abc123\t-\tP1\t30m\tSome task"
        edits, warnings = planner.parse_editor_text(text)
        assert edits == {"abc123": None}

    def test_parse_ignores_comments_and_blank_lines(self):
        text = "# a comment\n\nabc123\t2026-07-28\tP1\t30m\tSome task\n"
        edits, warnings = planner.parse_editor_text(text)
        assert edits == {"abc123": date(2026, 7, 28)}

    def test_parse_deleted_line_simply_absent(self):
        text = "kept\t2026-07-28\tP1\t30m\tKept task"
        edits, _ = planner.parse_editor_text(text)
        assert "deleted" not in edits

    def test_parse_accepts_a_row_trimmed_down_to_id_and_date(self):
        # Only the first two columns are authoritative, so a human who deleted
        # the trailing ones still gets their edit applied.
        edits, warnings = planner.parse_editor_text("abc123\t2026-07-28")
        assert edits == {"abc123": date(2026, 7, 28)}
        assert warnings == []

    def test_parse_skips_a_line_with_no_date_column(self):
        edits, warnings = planner.parse_editor_text("abc123")
        assert edits == {}
        assert warnings == []

    def test_duplicate_id_last_wins_with_warning(self):
        text = "abc123\t2026-07-28\tP1\t30m\tFirst\nabc123\t2026-07-29\tP1\t30m\tSecond"
        edits, warnings = planner.parse_editor_text(text)
        assert edits == {"abc123": date(2026, 7, 29)}
        assert warnings == ["line 2: duplicate task abc123, using this line's value"]

    def test_unparseable_date_warns_and_is_skipped(self):
        text = "abc123\tnot-a-date\tP1\t30m\tSome task"
        edits, warnings = planner.parse_editor_text(text)
        assert "abc123" not in edits
        assert warnings == ["line 1: can't parse date 'not-a-date' for task abc123, skipping"]

    def test_warning_line_numbers_count_every_line(self):
        # Comments and blanks are skipped, but they still occupy a line number
        # so the warning points at the right row in the editor.
        text = "# header\n\nabc123\tnot-a-date\tP1\t30m\tSome task"
        _, warnings = planner.parse_editor_text(text)
        assert warnings == ["line 3: can't parse date 'not-a-date' for task abc123, skipping"]

    def test_full_round_trip(self):
        result, buckets, capacity = self._sample_result()
        text = planner.render_editor_text(result, buckets, capacity)
        edits, warnings = planner.parse_editor_text(text)
        assert warnings == []
        # "b" is overdue (deadline Jul 20), so the deadline pass claims the 28th
        # and the undated "a" takes what's left on the 29th.
        assert edits.get("b") == date(2026, 7, 28)
        assert edits.get("a") == date(2026, 7, 29)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_missing_file_uses_defaults(self, tmp_path):
        config = planner.load_config(tmp_path / "does-not-exist.toml")
        assert config.default_task_minutes == 30
        assert config.horizon_days == 7
        assert config.eligibility_days == 14
        assert config.calendars == ["primary"]
        assert config.working_hours[0] == (planner._parse_hm("09:00"), planner._parse_hm("17:00"))
        assert config.timezone == planner.local_timezone()

    def test_overrides_apply(self, tmp_path):
        path = tmp_path / "plan.toml"
        path.write_text(
            'default_task_minutes = 15\ncalendars = ["primary", "other"]\ndays_off = ["2026-09-01"]\n'
        )
        config = planner.load_config(path)
        assert config.default_task_minutes == 15
        assert config.calendars == ["primary", "other"]
        assert config.days_off == {date(2026, 9, 1)}

    def test_per_weekday_working_hours_override(self, tmp_path):
        path = tmp_path / "plan.toml"
        path.write_text(
            '[working_hours]\ndefault = { start = "09:00", end = "14:00" }\n'
            'friday = { start = "09:00", end = "12:00" }\n'
        )
        config = planner.load_config(path)
        assert config.working_hours[4] == (planner._parse_hm("09:00"), planner._parse_hm("12:00"))
        assert config.working_hours[0] == (planner._parse_hm("09:00"), planner._parse_hm("14:00"))

    def test_each_weekday_name_maps_to_its_own_index(self, tmp_path):
        # A distinct end time per day, so a name landing on the wrong index (or
        # silently falling back to the default) shows up.
        path = tmp_path / "plan.toml"
        path.write_text(
            "[working_hours]\n"
            'default = { start = "09:00", end = "23:00" }\n'
            'monday = { start = "09:00", end = "10:00" }\n'
            'tuesday = { start = "09:00", end = "11:00" }\n'
            'wednesday = { start = "09:00", end = "12:00" }\n'
            'thursday = { start = "09:00", end = "13:00" }\n'
            'friday = { start = "09:00", end = "14:00" }\n'
        )
        config = planner.load_config(path)
        assert [config.working_hours[i][1].hour for i in range(5)] == [10, 11, 12, 13, 14]

    def test_end_before_start_raises(self, tmp_path):
        path = tmp_path / "plan.toml"
        path.write_text('[working_hours]\ndefault = { start = "14:00", end = "09:00" }\n')
        with pytest.raises(
            ValueError, match=r"working_hours\.monday: end \(09:00\) must be after start \(14:00\)"
        ):
            planner.load_config(path)

    def test_zero_length_working_day_raises(self, tmp_path):
        # end == start is not a valid window either: it is a day with no time in it.
        path = tmp_path / "plan.toml"
        path.write_text('[working_hours]\ndefault = { start = "09:00", end = "09:00" }\n')
        with pytest.raises(ValueError, match="must be after start"):
            planner.load_config(path)

    def test_local_timezone_from_tz_env(self):
        assert str(planner.local_timezone({"TZ": ":Europe/Berlin"})) == "Europe/Berlin"

    def test_local_timezone_from_localtime_link(self, tmp_path):
        zone = tmp_path / "usr/share/zoneinfo/Asia/Tokyo"
        zone.parent.mkdir(parents=True)
        zone.write_text("")
        link = tmp_path / "localtime"
        link.symlink_to(zone)
        assert str(planner.local_timezone({}, str(link))) == "Asia/Tokyo"

    def test_local_timezone_unrecognized_link_is_utc(self, tmp_path):
        plain = tmp_path / "localtime"
        plain.write_text("")
        assert str(planner.local_timezone({}, str(plain))) == "UTC"

    def test_local_timezone_bad_tz_is_utc(self):
        assert str(planner.local_timezone({"TZ": "Not/AZone"})) == "UTC"

    def test_timezone_parsed(self, tmp_path):
        path = tmp_path / "plan.toml"
        path.write_text('timezone = "America/New_York"\n')
        config = planner.load_config(path)
        assert str(config.timezone) == "America/New_York"


# ---------------------------------------------------------------------------
# TestPinnedPartitionProperties
#
# The cases above pin specific days (a weekend, a day past the last bucket, an
# overdue one). The property covers the whole space of pinned dates at once:
# whatever day a pinned item carries, it must come out of pack() exactly once —
# either placed on a real bucket or named in `unplanned`. Falling through both
# is the bug this section exists to prevent, and it is invisible by definition.
# ---------------------------------------------------------------------------

from hypothesis import given, settings, strategies as st

_BUCKETS = [
    date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31),
    date(2026, 8, 3), date(2026, 8, 4),
]


class TestPinnedPartitionProperties:
    @given(st.dates(min_value=date(2026, 6, 1), max_value=date(2026, 10, 1)))
    @settings(max_examples=500)
    def test_a_pinned_item_is_either_placed_or_reported(self, due):
        pinned = item("p", minutes=30, due_date=due, pinned=True)
        result = planner.pack([pinned], {d: 60 for d in _BUCKETS})
        placed = [d for d in _BUCKETS if any(c.item.id == "p" for c in result.schedule[d])]
        reported = [i.id for i in result.unplanned]
        assert len(placed) + len(reported) == 1, (due, placed, reported)

    @given(st.dates(min_value=date(2026, 6, 1), max_value=date(2026, 10, 1)))
    @settings(max_examples=500)
    def test_a_placed_pinned_item_never_lands_before_today(self, due):
        # The whole point of the clamp: a pinned item may be placed on its own
        # day or on the first bucket, and never on a day already gone.
        pinned = item("p", minutes=30, due_date=due, pinned=True)
        result = planner.pack([pinned], {d: 60 for d in _BUCKETS})
        for d in _BUCKETS:
            if any(c.item.id == "p" for c in result.schedule[d]):
                assert d >= _BUCKETS[0]
                assert d == due or d == _BUCKETS[0]

    @given(st.dates(min_value=date(2026, 6, 1), max_value=date(2026, 10, 1)))
    @settings(max_examples=500)
    def test_an_unplanned_item_always_has_a_reason(self, due):
        pinned = item("p", minutes=30, due_date=due, pinned=True)
        result = planner.pack([pinned], {d: 60 for d in _BUCKETS})
        for i in result.unplanned:
            reason = planner.unplanned_reason(i, _BUCKETS)
            assert reason in ("past the horizon", "not a working day")
            # The reason must match the day it actually carries.
            if reason == "past the horizon":
                assert i.due_date > _BUCKETS[-1]
            else:
                assert _BUCKETS[0] <= i.due_date <= _BUCKETS[-1]

    @given(st.dates(min_value=date(2026, 6, 1), max_value=date(2026, 10, 1)))
    @settings(max_examples=500)
    def test_a_pinned_item_never_has_its_due_date_rewritten(self, due):
        pinned = item("p", minutes=30, due_date=due, pinned=True)
        planner.pack([pinned], {d: 60 for d in _BUCKETS})
        assert pinned.due_date == due


# ---------------------------------------------------------------------------
# Title size hints — properties
#
# Whatever words surround it, a trailing size hint must parse to exactly the
# minutes it spells: integer hours+minutes, or a one-place decimal of hours.
# ---------------------------------------------------------------------------

_WORDS = st.lists(st.sampled_from(["Edit", "post", "review", "the", "doc", "Q3", "v2"]), max_size=4)


class TestTitleSizeProperties:
    @given(_WORDS, st.integers(0, 20), st.integers(0, 59))
    @settings(max_examples=300)
    def test_hours_and_minutes_run_sums(self, words, h, m):
        title = " ".join(words + [f"{h}h{m}m"])
        assert planner.title_size_minutes(title) == h * 60 + m

    @given(_WORDS, st.integers(0, 20), st.integers(0, 9))
    @settings(max_examples=300)
    def test_decimal_hours_read_whole(self, words, whole, tenth):
        title = " ".join(words + [f"{whole}.{tenth}h"])
        assert planner.title_size_minutes(title) == whole * 60 + tenth * 6


# ---------------------------------------------------------------------------
# Recurring tasks: soft pinning, the half-point bonus, and write-back safety
# ---------------------------------------------------------------------------

TUE, WED, THU, FRI, MON, TUE2 = _BUCKETS


class TestRecurrenceWritable:
    @pytest.mark.parametrize(
        "rule",
        [
            "every! 3 days", "Every! week", "every!2 weeks", "every day", "daily", "Every Day",
            "every mon", "every monday", "every tue, thu", "every weekday", "every workday",
            "every 15th", "every 2nd mon", "every last day", "every jan 1", "every September 3rd",
        ],
    )
    def test_rules_that_cannot_drift_are_writable(self, rule):
        assert planner.recurrence_writable(rule) is True

    @pytest.mark.parametrize(
        "rule",
        [
            "every 3 days", "every 2 weeks", "every other mon", "every other week",
            "every week", "every month", "every year", "every two weeks", "", None,
        ],
    )
    def test_interval_and_unknown_rules_are_plan_only(self, rule):
        # "every month" is the one to watch: "mon" must not match inside it.
        assert planner.recurrence_writable(rule) is False

    @given(st.integers(2, 30), st.sampled_from(["days", "weeks", "months", "years"]))
    def test_any_explicit_interval_drifts_unless_banged(self, n, unit):
        assert planner.recurrence_writable(f"every {n} {unit}") is False
        assert planner.recurrence_writable(f"every! {n} {unit}") is True


class TestItemRecurrenceFlags:
    def test_markers(self):
        assert item("a").marker == ""
        assert item("a", pinned=True, recurrence="every mon").marker == " (pinned)"
        assert item("a", recurrence="every mon").marker == " (recurring)"
        assert item("a", recurrence="every 3 days").marker == " (recurring, plan only)"

    def test_date_writable(self):
        assert item("a").date_writable is True
        assert item("a", pinned=True).date_writable is False
        assert item("a", recurrence="every! 3 days").date_writable is True
        assert item("a", recurrence="every 3 days").date_writable is False
        assert item("a", pinned=True, recurrence="every! 3 days").date_writable is False


class TestRecurringHalfPoint:
    def _winner(self, *contenders):
        result = planner.pack(list(contenders), {TUE: 60, WED: 0})
        return [c.item.id for c in result.schedule[TUE]]

    def test_recurring_beats_a_one_off_of_the_same_priority(self):
        rec = item("rec", priority=2, minutes=60, due_date=TUE, recurrence="every tue")
        one_off = item("one", priority=2, minutes=60, due_date=TUE)
        assert self._winner(one_off, rec) == ["rec"]

    def test_recurring_loses_to_a_higher_priority_one_off(self):
        rec = item("rec", priority=2, minutes=60, due_date=TUE, recurrence="every tue")
        one_off = item("one", priority=1, minutes=60, due_date=TUE)
        assert self._winner(rec, one_off) == ["one"]

    def test_low_priority_recurring_no_longer_preempts_p1(self):
        # The old behavior this replaces: any recurring task took its day first.
        rec = item("rec", priority=4, minutes=60, due_date=TUE, recurrence="every tue")
        p1 = item("p1", priority=1, minutes=60, due_date=TUE)
        assert self._winner(rec, p1) == ["p1"]

    def test_a_fixed_recurring_task_still_preempts(self):
        rec = item("rec", priority=4, minutes=60, due_date=TUE, pinned=True, recurrence="every tue")
        p1 = item("p1", priority=1, minutes=60, due_date=TUE)
        assert self._winner(rec, p1) == ["rec"]

    def test_bonus_only_applies_to_movable_recurring_tasks(self):
        fixed = item("f", priority=2, pinned=True, recurrence="every tue")
        assert fixed.sort_key()[1] == 2
        assert item("r", priority=2, recurrence="every tue").sort_key()[1] == 1.5


class TestPackSoftRecurring:
    def _days(self, result, item_id):
        return [d for d in _BUCKETS if any(c.item.id == item_id for c in result.schedule[d])]

    def test_spills_later_when_its_day_is_full(self):
        cap = {d: 60 for d in _BUCKETS} | {WED: 0}
        rec = item("rec", minutes=30, due_date=WED, recurrence="every wed")
        assert self._days(planner.pack([rec], cap), "rec") == [THU]

    def test_never_moves_earlier_than_its_day(self):
        # Every later day is full: an ordinary due-dated task would fall back to
        # Tuesday; a recurring one is at risk instead.
        cap = {TUE: 60, WED: 0, THU: 0, FRI: 0, MON: 0, TUE2: 0}
        rec = item("rec", minutes=30, due_date=WED, recurrence="every wed")
        result = planner.pack([rec], cap)
        assert self._days(result, "rec") == []
        assert [i.id for i in result.at_risk] == ["rec"]

    def test_overdue_lands_today(self):
        rec = item("rec", minutes=30, due_date=date(2026, 7, 20), recurrence="every mon")
        assert self._days(planner.pack([rec], {d: 60 for d in _BUCKETS}), "rec") == [TUE]

    def test_weekend_occurrence_is_unplanned_not_moved(self):
        rec = item("rec", minutes=30, due_date=date(2026, 8, 1), recurrence="every sat")
        result = planner.pack([rec], {d: 60 for d in _BUCKETS})
        assert self._days(result, "rec") == []
        assert [i.id for i in result.unplanned] == ["rec"]

    def test_next_weeks_occurrence_is_not_pulled_in(self):
        rec = item("rec", minutes=30, due_date=date(2026, 8, 10), recurrence="every mon")
        result = planner.pack([rec], {d: 60 for d in _BUCKETS})
        assert self._days(result, "rec") == []
        assert planner.unplanned_reason(result.unplanned[0], _BUCKETS) == "past the horizon"

    def test_no_buckets_leaves_it_unplanned(self):
        rec = item("rec", due_date=TUE, recurrence="every tue")
        result = planner.pack([rec], {})
        assert [i.id for i in result.unplanned] == ["rec"]
        assert result.unscheduled == []

    def test_container_rides_on_its_own_day(self):
        rec = item("rec", minutes=0, due_date=THU, recurrence="every thu")
        assert self._days(planner.pack([rec], {d: 0 for d in _BUCKETS}), "rec") == [THU]

    def test_overdue_recurring_task_is_never_parked(self):
        rec = item("rec", due_date=date(2026, 7, 20), recurrence="every! 3 days")
        assert planner.park_overdue_dates({}, [rec], TUE, TUE2) == {}

    def test_editor_marks_plan_only_rows(self):
        rec = item("rec", minutes=30, due_date=TUE, recurrence="every 3 days")
        cap = {d: 60 for d in _BUCKETS}
        text = planner.render_editor_text(planner.pack([rec], cap), _BUCKETS, cap)
        assert "Task rec (recurring, plan only)" in text


class TestSoftRecurringProperties:
    @given(
        st.dates(min_value=date(2026, 7, 1), max_value=date(2026, 8, 20)),
        st.lists(st.integers(0, 90), min_size=len(_BUCKETS), max_size=len(_BUCKETS)),
        st.integers(1, 120),
    )
    @settings(max_examples=500)
    def test_lands_on_or_after_its_day_and_is_accounted_for_once(self, due, caps, minutes):
        rec = item("rec", minutes=minutes, due_date=due, recurrence="every mon")
        result = planner.pack([rec], dict(zip(_BUCKETS, caps)))
        days = [d for d in _BUCKETS if any(c.item.id == "rec" for c in result.schedule[d])]
        floor = max(due, _BUCKETS[0])
        assert all(d >= floor for d in days), (due, days)
        placed_fully = sum(c.minutes for d in days for c in result.schedule[d] if c.item.id == "rec") == minutes
        outcomes = [placed_fully, rec in result.at_risk, rec in result.unplanned]
        assert outcomes.count(True) == 1, (due, caps, outcomes)
        assert rec not in result.unscheduled
