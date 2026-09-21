"""Timezone handling.

The database stores naive UTC throughout - that stays true, because it is the
only thing that keeps arithmetic and comparisons unambiguous. Everything a
person reads or types is Indian Standard Time.

Before this existed, slot grids were built from the *server's* local date but
compared against UTC, so slots were marked past 5.5 hours early. Anything that
turns a wall clock into a stored value, or the other way round, must go
through here.

    to_local(utc_naive)    -> aware datetime in IST, for display
    from_local(ist_naive)  -> naive UTC, for storage
    local_now()            -> naive IST wall clock
    local_today()          -> today's date in IST
"""
from datetime import date, datetime, timezone

from flask import current_app

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

UTC = timezone.utc
DEFAULT_TZ = "Asia/Kolkata"


def tz():
    name = current_app.config.get("TIMEZONE", DEFAULT_TZ) if current_app else DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def tz_label():
    return current_app.config.get("TIMEZONE_LABEL", "IST") if current_app else "IST"


def to_local(dt):
    """Naive UTC (as stored) -> aware local. Passes non-datetimes through."""
    if dt is None or not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz())


def from_local(dt):
    """Naive local wall clock (as typed) -> naive UTC, for storage."""
    if dt is None or not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt.astimezone(UTC).replace(tzinfo=None)


def local_now():
    """The current local wall clock, naive - directly comparable to slot times."""
    return datetime.now(tz()).replace(tzinfo=None)


def local_today():
    return datetime.now(tz()).date()


def fmt(dt, pattern="%Y-%m-%d %H:%M"):
    """Format a stored UTC value in local time."""
    if dt is None:
        return "-"
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.strftime(pattern)
    return to_local(dt).strftime(pattern)
