"""Time helpers. Every function takes `now` explicitly; only `now_utc()` reads the wall clock.

Instants are stored as ISO UTC strings with seconds precision ("2026-10-06T14:02:00Z"). Due values,
snooze times and review dates are *local wall time* strings in the configured timezone:
"YYYY-MM-DD" (date-only) or "YYYY-MM-DDTHH:MM" (timed). `tz` arguments accept an IANA name or a
tzinfo; None means UTC.

DST: wall times are mapped with fold=0 (PEP 495). An ambiguous time on the fall-back day resolves
to its first (earlier) occurrence; a time inside the spring-forward gap maps to the instant it
would have had on the pre-transition offset (02:30 in America/Chicago on 2026-03-08 -> 08:30Z).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Dict, Optional, Union

from zoneinfo import ZoneInfo

UTC = timezone.utc
TzArg = Union[str, tzinfo, None]

_UTC_NAMES = ("UTC", "ETC/UTC", "Z", "GMT", "ETC/GMT", "UNIVERSAL", "ZULU")
_TZ_CACHE: Dict[str, tzinfo] = {}

_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

# Stored values (instants, due strings, snooze times, review dates) are read as ASCII only, the way
# core.schedule and the Worker read them: a value in other digits would pass here and then be
# unreadable to the push rules. What a person types (parse_due below) may use any digits; its
# output is always ASCII.
_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})"
    r"(?::(\d{2})(?:[.,](\d+))?)?"
    r"\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?\s*$", re.ASCII
)
_DUE_STORED_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$", re.ASCII)
_WALL_MINUTE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$", re.ASCII)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$", re.ASCII)
_TIME_SUFFIX_RE = re.compile(r"^(?P<day>.+?)(?:[ t](?P<h>\d{1,2}):(?P<m>\d{2}))?$")
_FULL_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_MMDD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_PLUS_DAYS_RE = re.compile(r"^\+(\d{1,4})d$")

_DUE_HELP = "use MM-DD, YYYY-MM-DD, today, tomorrow, +Nd or a weekday (mon..sun), optionally with HH:MM"


# ---- zones and instants ------------------------------------------------------------------------

def get_tz(tz: TzArg) -> tzinfo:
    """A tzinfo for an IANA name (cached). Raises ValueError for an unknown name."""
    if tz is None or tz == "":
        return UTC
    if isinstance(tz, tzinfo):
        return tz
    name = str(tz)
    zone = _TZ_CACHE.get(name)
    if zone is not None:
        return zone
    try:
        zone = ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, ValueError, OSError for odd keys
        if name.upper() in _UTC_NAMES:
            zone = UTC  # systems without tzdata still know UTC
        else:
            raise ValueError("unknown timezone %r" % name) from None
    _TZ_CACHE[name] = zone
    return zone


def is_valid_timezone(name: str) -> bool:
    try:
        get_tz(name)
    except ValueError:
        return False
    return bool(name)


def canonical_timezone(name: Optional[str]) -> Optional[str]:
    """The zone name to store: `name` itself when ZoneInfo can load it, "UTC" for a UTC alias it
    can't ("Z", "zulu" on a case-sensitive disk), else None. get_tz() is lenient when reading;
    this is strict, because the stored name is handed to ZoneInfo elsewhere (core.schedule) and
    to JavaScript's Intl (the Worker), which know nothing of our aliases."""
    if not isinstance(name, str) or not name.strip():
        return None
    text = name.strip()
    try:
        ZoneInfo(text)
    except Exception:  # ZoneInfoNotFoundError, ValueError, OSError for odd keys
        return "UTC" if text.upper() in _UTC_NAMES else None
    return text


def now_utc() -> datetime:
    """The only function here that reads the clock."""
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError("expected a datetime, got %s" % type(dt).__name__)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=UTC)  # naive = UTC
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """'2026-10-06T14:02:00Z': UTC, seconds precision (fractions are dropped). Naive = UTC."""
    u = _as_utc(dt)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (u.year, u.month, u.day, u.hour, u.minute, u.second)


def parse_iso(value: Union[str, datetime]) -> datetime:
    """Parse an ISO instant into an aware UTC datetime.

    Accepts 'Z' or numeric offsets, fractional seconds (any number of digits, truncated to
    microseconds) and naive strings (taken as UTC). Raises ValueError otherwise.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        raise ValueError("expected an ISO timestamp string, got %s" % type(value).__name__)
    m = _ISO_RE.match(value)
    if not m:
        raise ValueError("not an ISO timestamp (expected e.g. 2026-10-06T14:02:00Z)")
    year, month, day, hour, minute = (int(m.group(k)) for k in range(1, 6))
    second = int(m.group(6) or 0)
    micro = int((m.group(7) or "0")[:6].ljust(6, "0"))
    offset = m.group(8)
    if offset in (None, "Z", "z"):
        zone: tzinfo = UTC
    else:
        sign = -1 if offset[0] == "-" else 1
        digits = offset[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
        if hours > 23 or minutes > 59:
            raise ValueError("timestamp has an invalid UTC offset")
        zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        dt = datetime(year, month, day, hour, minute, second, micro, tzinfo=zone)
    except ValueError:
        raise ValueError("timestamp has an out-of-range field") from None
    return dt.astimezone(UTC)


def local_dt(dt: datetime, tz: TzArg) -> datetime:
    """The instant `dt` (naive = UTC) as an aware datetime in `tz`."""
    return _as_utc(dt).astimezone(get_tz(tz))


def local_date(dt: datetime, tz: TzArg) -> date:
    """The local calendar date of the instant `dt` in `tz`."""
    return local_dt(dt, tz).date()


def mmdd(d: Union[date, datetime]) -> str:
    """'10-01' for October 1 (uses the fields as given; pass a local date)."""
    return "%02d-%02d" % (d.month, d.day)


def wall_to_utc(date_str: Union[str, date], hh: int, mm: int, tz: TzArg) -> datetime:
    """The UTC instant of local wall time `date_str hh:mm` in `tz` (fold=0, see module doc)."""
    if isinstance(date_str, date):
        d = date(date_str.year, date_str.month, date_str.day)
    else:
        d = parse_date(date_str)
    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        raise ValueError("time %02d:%02d is out of range" % (int(hh), int(mm)))
    local = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=get_tz(tz))
    return local.astimezone(UTC)


# ---- local date and due strings ----------------------------------------------------------------

def parse_date(value: str) -> date:
    """'YYYY-MM-DD' -> date. Raises ValueError."""
    m = _DATE_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise ValueError("not a date (expected YYYY-MM-DD)")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError("%s is not a real date" % value) from None


def check_date(value: str) -> str:
    """Validate a 'YYYY-MM-DD' string (review_at); returns it unchanged."""
    parse_date(value)
    return value


def check_due(value: str) -> str:
    """Validate a stored due string ('YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'); returns it unchanged."""
    m = _DUE_STORED_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise ValueError("due must be YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    _check_fields(m, value)
    return value


def check_wall_minute(value: str) -> str:
    """Validate a 'YYYY-MM-DDTHH:MM' local wall time (snooze_until); returns it unchanged."""
    m = _WALL_MINUTE_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise ValueError("expected a local time YYYY-MM-DDTHH:MM")
    _check_fields(m, value)
    return value


def _check_fields(m: "re.Match[str]", value: str) -> None:
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError("%s is not a real date" % value[:10]) from None
    if m.group(4) is not None and not (int(m.group(4)) <= 23 and int(m.group(5)) <= 59):
        raise ValueError("%s has an out-of-range time" % value)


def is_timed(due_at: Optional[str]) -> bool:
    """True for 'YYYY-MM-DDTHH:MM', False for date-only or None."""
    return bool(due_at) and "T" in str(due_at)


def due_date(due_at: str) -> date:
    """The local calendar date of a due string."""
    return parse_date(str(due_at)[:10])


def due_instant(due_at: Optional[str], tz: TzArg) -> Optional[datetime]:
    """UTC instant of a due string; date-only = local 00:00 that day (for ordering). None -> None."""
    if not due_at:
        return None
    check_due(due_at)
    d = parse_date(due_at[:10])
    if is_timed(due_at):
        return wall_to_utc(d, int(due_at[11:13]), int(due_at[14:16]), tz)
    return wall_to_utc(d, 0, 0, tz)


def parse_due(text: str, now: datetime, tz: TzArg) -> str:
    """Turn what a person typed into a due string, relative to the local date of `now` in `tz`.

    Accepts MM-DD and YYYY-MM-DD (each optionally with ' HH:MM' or 'THH:MM'), 'today',
    'tomorrow', '+Nd' and weekday names (next occurrence, never today); the relative forms also
    take a ' HH:MM' suffix. MM-DD takes the current local year unless that date is more than
    180 days in the past, then next year. Raises ValueError with a readable message.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no due date given: " + _DUE_HELP)
    s = re.sub(r"\s+", " ", text.strip().lower())
    m = _TIME_SUFFIX_RE.match(s)
    if not m:
        raise ValueError("can't read the due date %r: %s" % (text.strip(), _DUE_HELP))
    day_part = m.group("day").strip()
    clock: Optional[str] = None
    if m.group("h") is not None:
        hour, minute = int(m.group("h")), int(m.group("m"))
        if hour > 23 or minute > 59:
            raise ValueError("%02d:%02d is not a time of day (use 00:00 to 23:59)" % (hour, minute))
        clock = "%02d:%02d" % (hour, minute)
    today = local_date(now, tz)
    day = _due_day(day_part, today, text.strip())
    return day.isoformat() if clock is None else "%sT%s" % (day.isoformat(), clock)


def _due_day(part: str, today: date, original: str) -> date:
    if part == "today":
        return today
    if part == "tomorrow":
        return today + timedelta(days=1)
    m = _PLUS_DAYS_RE.match(part)
    if m:
        return today + timedelta(days=int(m.group(1)))
    if part in _WEEKDAYS:
        ahead = (_WEEKDAYS[part] - today.weekday()) % 7 or 7  # never today
        return today + timedelta(days=ahead)
    m = _FULL_DATE_RE.match(part)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise ValueError("%s is not a real date" % part) from None
    m = _MMDD_RE.match(part)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        label = "%02d-%02d" % (month, day)
        try:
            date(2000, month, day)  # exists in some year (2000 is a leap year)
        except ValueError:
            raise ValueError("%s is not a real date" % label) from None
        year = today.year
        candidate = _date_or_none(year, month, day)
        # Only 02-29 can be missing; it sits where Mar 1 does when choosing the year.
        position = candidate or date(year, 3, 1)
        if (today - position).days > 180:
            year += 1
            candidate = _date_or_none(year, month, day)
        if candidate is None:
            raise ValueError("%s is not a date in %d; give the full YYYY-MM-DD" % (label, year))
        return candidate
    raise ValueError("can't read the due date %r: %s" % (original, _DUE_HELP))


def _date_or_none(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def snooze_until(choice: str, now: datetime, tz: TzArg, hour: int = 9) -> str:
    """'YYYY-MM-DDT09:00' local: '1d' = today+1, '3d' = today+3, 'mon' = next Monday (a Monday
    snoozes to the following Monday)."""
    c = (choice or "").strip().lower()
    today = local_date(now, tz)
    if c == "1d":
        day = today + timedelta(days=1)
    elif c == "3d":
        day = today + timedelta(days=3)
    elif c in ("mon", "monday"):
        day = today + timedelta(days=(0 - today.weekday()) % 7 or 7)
    else:
        raise ValueError("snooze takes 1d, 3d or mon")
    if not 0 <= int(hour) <= 23:
        raise ValueError("snooze hour must be 0..23")
    return "%sT%02d:00" % (day.isoformat(), int(hour))


# ---- what people see ---------------------------------------------------------------------------

def human_date(d: Union[date, datetime]) -> str:
    """'Sun Oct 18' (English, no zero padding, independent of locale)."""
    return "%s %s %d" % (_DAY_ABBR[d.weekday()], _MONTH_ABBR[d.month - 1], d.day)


def human_time(dt: datetime, tz: TzArg = None) -> str:
    """'9:02 AM'. With `tz`, the instant (naive = UTC) is shown in that zone; without, as given."""
    t = local_dt(dt, tz) if tz is not None else dt
    hour12 = t.hour % 12 or 12
    return "%d:%02d %s" % (hour12, t.minute, "AM" if t.hour < 12 else "PM")


def human_due(due_at: Optional[str]) -> str:
    """'Sun Oct 11' or 'Sun Oct 11, 2:00 PM' for a due string; '' for None."""
    if not due_at:
        return ""
    check_due(due_at)
    label = human_date(due_date(due_at))
    if is_timed(due_at):
        hour, minute = int(due_at[11:13]), int(due_at[14:16])
        label += ", %d:%02d %s" % (hour % 12 or 12, minute, "AM" if hour < 12 else "PM")
    return label


# Aliases, so callers can use whichever name reads naturally.
fmt_date = format_date = human_date
fmt_time = format_time = human_time
