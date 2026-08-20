# tests/test_schedule_utils.py
"""Tests for compute_next_run in schedule_utils."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from src.schedule_utils import compute_next_run, fmt_next_run

_CST = ZoneInfo("Asia/Shanghai")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _cst(year, month, day, hour, minute) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=_CST)


# ------------------------------------------------------------------
# once
# ------------------------------------------------------------------

def test_once_future():
    spec = {"type": "once", "run_at": "2099-06-15T10:00:00+08:00"}
    from_ms = _ms(_cst(2026, 4, 21, 9, 0))
    result = compute_next_run(spec, from_ms)
    expected = _ms(datetime(2099, 6, 15, 10, 0, tzinfo=_CST))
    assert result == expected


def test_once_naive_datetime_treated_as_cst():
    # Naive run_at → treated as Asia/Shanghai
    spec = {"type": "once", "run_at": "2099-06-15T10:00:00"}
    from_ms = _ms(_cst(2026, 4, 21, 9, 0))
    result = compute_next_run(spec, from_ms)
    expected = _ms(datetime(2099, 6, 15, 10, 0, tzinfo=_CST))
    assert result == expected


# ------------------------------------------------------------------
# daily
# ------------------------------------------------------------------

def test_daily_later_same_day():
    # now=08:00, target=09:00 → today
    from_ms = _ms(_cst(2026, 4, 21, 8, 0))
    spec = {"type": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 21, 9, 0))


def test_daily_past_time_today_advances_one_day():
    # now=10:00, target=09:00 → tomorrow
    from_ms = _ms(_cst(2026, 4, 21, 10, 0))
    spec = {"type": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 22, 9, 0))


def test_daily_exact_now_advances():
    # now == target → advance to next day
    from_ms = _ms(_cst(2026, 4, 21, 9, 0))
    spec = {"type": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 22, 9, 0))


# ------------------------------------------------------------------
# weekly
# ------------------------------------------------------------------

def test_weekly_next_occurrence():
    # 2026-04-21 is a Tuesday (weekday=1). Target=Monday (0) → next Monday 2026-04-27
    from_ms = _ms(_cst(2026, 4, 21, 8, 0))
    spec = {"type": "weekly", "day_of_week": 0, "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 27, 9, 0))


def test_weekly_same_day_later_time():
    # 2026-04-21 is Tuesday (1), target=Tuesday 10:00, now=09:00 → today
    from_ms = _ms(_cst(2026, 4, 21, 9, 0))
    spec = {"type": "weekly", "day_of_week": 1, "time": "10:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 21, 10, 0))


def test_weekly_same_day_past_time_advances_one_week():
    # 2026-04-21 is Tuesday (1), target=Tuesday 08:00, now=09:00 → next Tuesday
    from_ms = _ms(_cst(2026, 4, 21, 9, 0))
    spec = {"type": "weekly", "day_of_week": 1, "time": "08:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 28, 8, 0))


# ------------------------------------------------------------------
# monthly
# ------------------------------------------------------------------

def test_monthly_later_in_month():
    # now=April 5, target=April 15 → April 15
    from_ms = _ms(_cst(2026, 4, 5, 8, 0))
    spec = {"type": "monthly", "day_of_month": 15, "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 4, 15, 9, 0))


def test_monthly_past_advances_one_month():
    # now=April 20, target=day 15 → May 15
    from_ms = _ms(_cst(2026, 4, 20, 8, 0))
    spec = {"type": "monthly", "day_of_month": 15, "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2026, 5, 15, 9, 0))


def test_monthly_december_rolls_to_january():
    # now=December 20, target=day 15 → January 15 next year
    from_ms = _ms(_cst(2026, 12, 20, 8, 0))
    spec = {"type": "monthly", "day_of_month": 15, "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    assert result == _ms(_cst(2027, 1, 15, 9, 0))


def test_monthly_day_clamped_to_28():
    # day_of_month=31 should be clamped to 28
    from_ms = _ms(_cst(2026, 1, 1, 8, 0))
    spec = {"type": "monthly", "day_of_month": 31, "time": "09:00", "timezone": "Asia/Shanghai"}
    result = compute_next_run(spec, from_ms)
    # Jan 28
    assert result == _ms(_cst(2026, 1, 28, 9, 0))


# ------------------------------------------------------------------
# fmt_next_run
# ------------------------------------------------------------------

def test_fmt_next_run():
    ms = _ms(_cst(2026, 4, 22, 15, 30))
    s = fmt_next_run(ms)
    assert "2026-04-22" in s
    assert "15:30" in s


# ------------------------------------------------------------------
# error cases
# ------------------------------------------------------------------

def test_unknown_type_raises():
    with pytest.raises(ValueError, match="Unknown schedule spec type"):
        compute_next_run({"type": "hourly"}, int(datetime.now().timestamp() * 1000))
