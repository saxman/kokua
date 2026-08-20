from datetime import datetime

import pytest

from kokua import scheduling


def test_next_fire_once_future_and_past():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert scheduling.next_fire({"type": "once", "at": "2026-07-15T12:00:30"}, now) == 30.0
    assert scheduling.next_fire({"type": "once", "at": "2026-07-15T11:59:30"}, now) is None


def test_next_fire_interval():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert scheduling.next_fire({"type": "interval", "seconds": 90}, now) == 90.0


def test_next_fire_daily_rolls_to_tomorrow():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert scheduling.next_fire({"type": "daily", "at": "13:00"}, now) == 3600.0
    assert scheduling.next_fire({"type": "daily", "at": "11:00"}, now) == 23 * 3600.0


def test_next_fire_weekly_rolls_within_and_across_week():
    now = datetime(2026, 7, 15, 12, 0, 0)  # a Wednesday (weekday()==2)
    assert scheduling.next_fire({"type": "weekly", "day": "thu", "at": "12:00"}, now) == 24 * 3600.0
    assert scheduling.next_fire({"type": "weekly", "day": "wed", "at": "11:00"}, now) == 7 * 24 * 3600.0


@pytest.mark.parametrize(
    "schedule",
    [
        {"type": "interval", "seconds": 0},
        {"type": "daily", "at": "25:00"},
        {"type": "daily", "at": "oops"},
        {"type": "weekly", "day": "funday", "at": "09:00"},
        {"type": "once", "at": "not-a-date"},
        {"type": "once"},
        {"type": "bogus"},
    ],
)
def test_next_fire_rejects_malformed(schedule):
    with pytest.raises(ValueError):
        scheduling.next_fire(schedule, datetime(2026, 7, 15, 12, 0, 0))
