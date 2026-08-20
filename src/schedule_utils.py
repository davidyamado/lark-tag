# src/schedule_utils.py
"""
Compute next-run timestamps from schedule spec dicts.
Zero external dependencies — uses only Python stdlib datetime / zoneinfo.

Supported spec types:
  {"type": "once",    "run_at": "<ISO-8601 string with tz offset>"}
  {"type": "daily",   "time": "HH:MM", "timezone": "Asia/Shanghai"}
  {"type": "weekly",  "day_of_week": 0-6 (Mon=0), "time": "HH:MM", "timezone": "..."}
  {"type": "monthly", "day_of_month": 1-28, "time": "HH:MM", "timezone": "..."}
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "Asia/Shanghai"


def compute_next_run(spec: dict, from_ms: int) -> int:
    """
    Return the next epoch-millisecond timestamp strictly after from_ms at which
    this job should fire, based on spec.

    Raises ValueError for unknown spec types or malformed data.
    """
    spec_type = spec.get("type", "")
    tz = ZoneInfo(spec.get("timezone", _DEFAULT_TZ))
    now = datetime.fromtimestamp(from_ms / 1000, tz=tz)

    if spec_type == "once":
        run_at_str = spec["run_at"]
        dt = datetime.fromisoformat(run_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return int(dt.timestamp() * 1000)

    # Parse "HH:MM" for recurring types
    time_str = spec.get("time", "00:00")
    h, m = _parse_hhmm(time_str)

    if spec_type == "daily":
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return int(candidate.timestamp() * 1000)

    if spec_type == "weekly":
        target_dow = int(spec["day_of_week"])  # Mon=0 … Sun=6
        days_ahead = (target_dow - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(
            hour=h, minute=m, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(weeks=1)
        return int(candidate.timestamp() * 1000)

    if spec_type == "monthly":
        target_day = int(spec["day_of_month"])
        candidate = _next_monthly(now, target_day, h, m)
        return int(candidate.timestamp() * 1000)

    raise ValueError(f"Unknown schedule spec type: {spec_type!r}")


def fmt_next_run(next_run_ms: int, tz_name: str = _DEFAULT_TZ) -> str:
    """Human-readable CST datetime string for display."""
    tz = ZoneInfo(tz_name)
    dt = datetime.fromtimestamp(next_run_ms / 1000, tz=tz)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _parse_hhmm(time_str: str) -> tuple[int, int]:
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time string: {time_str!r}")
    return int(parts[0]), int(parts[1])


def _next_monthly(now: datetime, target_day: int, h: int, m: int) -> datetime:
    """Return the next datetime on day `target_day` of the month at HH:MM."""
    # Clamp to 28 to avoid month-end edge cases
    target_day = min(target_day, 28)

    year, month = now.year, now.month
    tz = now.tzinfo

    candidate = now.replace(day=target_day, hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        # Advance one month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        candidate = candidate.replace(year=year, month=month)

    return candidate
