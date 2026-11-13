"""Push rules for one hourly tick: the reference implementation of contract section 12.

``decide(now, items, notifications, runs, tz, cfg)`` says which snoozed items reopen and which
pushes go out. It is a pure function: it reads no clock and no files, imports no other repo
module, and never mutates its inputs. Local adapters (ntfy, Pushover) call it from ``ghosted run``;
the Cloudflare Worker ports the same rules to ``rules.js``. ``spec/push-rules-vectors.json``
holds the cases both engines must agree on, and ``spec/README.md`` spells out each rule.

The caller owns every side effect. It sets reopened items to open (rev + 1, ``snooze_until``
kept, so T5 still sees the item as reopened today on later ticks), and for each push it inserts
the notification row (unique ``dedup_key``) before sending, sends only when the insert happened,
then records ``ok``.

Text is read the same way the JS port reads it: ASCII digits and ASCII whitespace only, so the
two engines cannot disagree on an odd character.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone, tzinfo

try:  # stdlib from Python 3.9
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - only on interpreters older than the supported floor
    ZoneInfo = None  # type: ignore[assignment,misc]

__all__ = [
    "decide",
    "digest_body",
    "DEFAULTS",
    "BODY_LIMIT",
    "SEPARATOR",
    "CUT_MARK",
    "DIGEST_TITLE",
    "ADVANCE_TITLE",
    "DEADMAN_TITLE",
]

DEFAULTS = {
    "digest_hour": 9,
    "quiet_start": 22,
    "quiet_end": 8,
    "daily_budget": 3,
    "deadman_hours": 3,
    "enable_deadman": False,  # local adapters; the Worker port turns it on
}

DIGEST_WINDOW_HOURS = 3  # [digest_hour, digest_hour + 3): catch-up when a tick was missed
SOON_DAYS = 2  # T3, date-only items: due on today+1 or today+2
SOON_HOURS = 48  # T3, timed items: due within the next 48 hours
ADVANCE_HOURS = 24
REVIEW_EVERY_DAYS = 7  # T6: review_at, then every 7 days after it

BODY_LIMIT = 178  # code points (Python len); the JS port must not count UTF-16 units
SEPARATOR = " · "
CUT_MARK = " · …"

DIGEST_TITLE = "Ghosted"
ADVANCE_TITLE = "Due in 24 h"
DEADMAN_TITLE = "Mac stopped reporting"

# Digest groups in body order. The contract names them T2, T1, T3, T4, T5, T6.
OVERDUE, DUE_TODAY, DUE_SOON, NEW_INTERRUPTION, REOPENED, REVIEW = range(6)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WS = " \t\n\r\x0b\x0c"  # ASCII whitespace: what str.strip() and \s mean in both engines
_MAX_HOURS = 999999999 * 24  # the largest timedelta, in hours


def decide(now, items, notifications, runs, tz, cfg=None) -> dict:
    """Decide one tick and return ``{"reopen": [ids], "send": [push, ...]}``.

    ``now`` is an aware datetime or an ISO-8601 string (a naive value is UTC). ``items`` is a
    list of item dicts in the contract's section 4 shape (a dict of id -> item also works).
    ``notifications`` are rows with ``sent_at, kind, dedup_key, item_ids, ok, local_date``;
    ``runs`` are rows with ``run_id, finished_at, ok``. ``tz`` is an IANA name or a tzinfo, and
    every wall-clock rule runs in it. ``cfg`` is a dict (or an object with attributes) holding
    ``digest_hour, quiet_start, quiet_end, daily_budget, deadman_hours, enable_deadman``;
    missing keys take ``DEFAULTS``.

    ``reopen`` lists snoozed items whose ``snooze_until`` has passed, in id order. Each push is
    ``{kind, dedup_key, item_ids, title, body, local_date, priority}``, in send order:
    dead-man, digest, then advance pushes by due time. An advance is never sent for an item a
    digest already carried inside its last 24 h, nor for one this tick's digest carries.
    """
    zone = _zone(tz)
    now_utc = _instant(now)
    if now_utc is None:
        raise ValueError("now must be a datetime or an ISO-8601 timestamp, got %r" % (now,))
    local_now = _local(now_utc, zone)
    if local_now is None or not 2 <= local_now.year <= 9998:
        raise ValueError("now is outside the supported range, got %r" % (now,))
    conf = _settings(cfg)
    tick = _Tick(now_utc, local_now.date(), zone)
    hour = local_now.hour
    today = tick.today.isoformat()

    entries = _entries(items)
    notes = _rows(notifications)

    reopen = sorted(
        item_id
        for item_id, item in entries
        if item.get("status") == "snoozed" and _snooze_over(item, tick)
    )
    out = {"reopen": reopen, "send": []}
    if _quiet(hour, conf):
        return out  # quiet hours send nothing; reopening still happens

    reopened = set(reopen)
    active = [
        (item_id, item)
        for item_id, item in entries
        if item.get("status") == "open" or item_id in reopened
    ]

    keys = set()
    used = 0
    for note in notes:
        key = _note_key(note, zone)
        if key:
            keys.add(key)
        if _note_day(note, zone) == tick.today:
            used += 1  # any kind, any ok
    left = conf["daily_budget"] - used
    sends = out["send"]
    last_sent, carried = _delivered(notes)

    if conf["enable_deadman"] and left > 0 and conf["digest_hour"] <= hour < conf["quiet_start"]:
        push = _deadman(_rows(runs), keys, tick, conf["deadman_hours"], today)
        if push is not None:
            sends.append(push)
            left -= 1

    in_window = conf["digest_hour"] <= hour < conf["digest_hour"] + DIGEST_WINDOW_HOURS
    digest_key = "digest:" + today
    absorbed = set()  # items this tick's digest carries: none of them gets an advance too
    if left > 0 and in_window and digest_key not in keys:
        chosen = _digest_items(active, reopened, last_sent, tick)
        if chosen:
            ids = [item_id for item_id, _ in chosen]
            titles = [_title(item_id, item) for item_id, item in chosen]
            sends.append(_push("digest", digest_key, ids, DIGEST_TITLE, digest_body(titles), today))
            absorbed = set(ids)
            left -= 1

    for item_id, item in _advance_candidates(active, keys, tick, absorbed, carried):
        if left <= 0:
            break
        sends.append(_push("advance", "advance:" + item_id, [item_id],
                           ADVANCE_TITLE, _title(item_id, item), today))
        left -= 1
    return out


def digest_body(titles, limit=BODY_LIMIT) -> str:
    """``"{n} open: {t1} · {t2} · …"``, cut on a separator so the whole body fits in ``limit``.

    ``n`` counts every title, shown or not. When the titles do not all fit, as many whole
    titles as fit are kept and ``" · …"`` is appended; the result, mark included, is at most
    ``limit`` characters, counted as code points.
    """
    titles = [str(title) for title in titles]
    head = "%d open: " % len(titles)
    full = head + SEPARATOR.join(titles)
    if len(full) <= limit:
        return full
    for keep in range(len(titles) - 1, 0, -1):
        body = head + SEPARATOR.join(titles[:keep]) + CUT_MARK
        if len(body) <= limit:
            return body
    # Not even one whole title fits. Public titles are capped at 100 characters, so valid
    # items never get here; cut the first title rather than send an empty list.
    room = max(limit - len(head) - 1, 0)
    return head + titles[0][:room] + "…"


# ----------------------------------------------------------------------------- tick logic


class _Tick:
    __slots__ = ("now", "today", "zone")

    def __init__(self, now: datetime, today: date, zone: tzinfo) -> None:
        self.now = now  # aware UTC
        self.today = today  # local date
        self.zone = zone


def _delivered(notes):
    """What the delivered digests say: (sent_at of the latest one or None, {id: latest sent_at}).

    A digest counts as delivered with ``ok`` 1 or true and a readable ``sent_at``. T4 measures
    new interruptions from the latest one (a failed send reached nobody, so what it carried is
    still new). The per-item map feeds the no-double-push rule for advance pushes.
    """
    last = None
    carried = {}
    for note in notes:
        if _note_kind(note) != "digest" or not _true(note.get("ok")):
            continue
        sent = _instant(note.get("sent_at"))
        if sent is None:
            continue
        if last is None or sent > last:
            last = sent
        for item_id in _id_list(note.get("item_ids")):
            if isinstance(item_id, str):
                seen = carried.get(item_id)
                if seen is None or sent > seen:
                    carried[item_id] = sent
    return last, carried


def _digest_items(active, reopened, last_sent, tick):
    """The digest's items in body order, as (id, item) pairs."""
    ranked = []
    for item_id, item in active:
        group, due_utc = _group(item_id, item, reopened, last_sent, tick)
        if group is None:
            continue
        order = (group, due_utc is None, _EPOCH if due_utc is None else due_utc, item_id)
        ranked.append((order, item_id, item))
    ranked.sort(key=lambda row: row[0])
    return [(item_id, item) for _, item_id, item in ranked]


def _group(item_id, item, reopened, last_sent, tick):
    """(first matching trigger, due instant or None); the trigger is None when none holds.

    ``last_sent`` is the sent_at of the latest delivered digest, or None when there was none.
    """
    due = _wall(item.get("due_at"))
    due_utc = None if due is None else _wall_utc(due, tick.zone)
    if due_utc is not None:
        day, clock = due
        if clock is None:  # date-only: compare local dates
            if day < tick.today:
                return OVERDUE, due_utc
            if day == tick.today:
                return DUE_TODAY, due_utc
            if day <= tick.today + timedelta(days=SOON_DAYS):
                return DUE_SOON, due_utc
        else:  # timed: compare instants
            if due_utc < tick.now:
                return OVERDUE, due_utc
            if day == tick.today:
                return DUE_TODAY, due_utc
            if due_utc - tick.now <= timedelta(hours=SOON_HOURS):
                return DUE_SOON, due_utc
    # T4: an interruption that looks resolved (resumed and ended clean) is not news.
    if item.get("kind") == "interrupted" and not _true(item.get("looks_resolved")):
        if last_sent is None:
            return NEW_INTERRUPTION, due_utc
        created = _instant(item.get("created_at"))
        if created is not None and created > last_sent:
            return NEW_INTERRUPTION, due_utc
    if item_id in reopened:
        return REOPENED, due_utc
    if item.get("status") == "open":
        # Snooze expiry keeps snooze_until, so an item reopened at an earlier tick today (or by
        # the Worker's cron just before this call) is still "reopened today".
        snooze = _wall(item.get("snooze_until"))
        if snooze is not None and snooze[0] == tick.today:
            until = _wall_utc(snooze, tick.zone)
            if until is not None and until <= tick.now:
                return REOPENED, due_utc
    # T6 reads review_at whether or not the item also has a due date: `ghosted add --due X --review Y`
    # stores both, and the contract's T6 has no due_at condition.
    review = _wall(item.get("review_at"))
    if review is not None:
        age = (tick.today - review[0]).days
        if age >= 0 and age % REVIEW_EVERY_DAYS == 0:
            return REVIEW, due_utc
    return None, due_utc


def _advance_candidates(active, keys, tick, absorbed, carried):
    """Open timed items with due - 24 h <= now < due and no advance row yet, earliest due first.

    Skipped (no double push for one item): items the digest decided in this tick carries
    (``absorbed``), and items a delivered digest carried at or after due - 24 h (``carried``
    maps an id to the latest delivered digest that listed it).
    """
    found = []
    lead = timedelta(hours=ADVANCE_HOURS)
    for item_id, item in active:
        due = _wall(item.get("due_at"))
        if due is None or due[1] is None:
            continue  # date-only items get their warning through T3, never an advance push
        due_utc = _wall_utc(due, tick.zone)
        if due_utc is None:
            continue
        # due - 24 h <= now < due, written as due <= now + 24 h so a far-past due cannot underflow
        if not tick.now < due_utc <= tick.now + lead or "advance:" + item_id in keys:
            continue
        if item_id in absorbed:
            continue
        sent = carried.get(item_id)
        if sent is not None and sent >= due_utc - lead:  # due_utc > now, so no underflow
            continue
        found.append((due_utc, item_id, item))
    found.sort(key=lambda row: (row[0], row[1]))
    return [(item_id, item) for _, item_id, item in found]


def _deadman(runs, keys, tick, deadman_hours, today):
    last = _last_ok_run(runs)
    if last is None:
        key = "deadman:none"
        body = "No successful run reported yet"
    else:
        finished, run_id = last
        if tick.now - finished <= timedelta(hours=deadman_hours):
            return None
        key = "deadman:" + run_id
        body = "No successful run since " + _when(finished, tick)
    if key in keys:
        return None  # it cannot repeat until a new ok run changes the key
    return _push("deadman", key, [], DEADMAN_TITLE, body, today, priority="high")


def _last_ok_run(runs):
    """(finished_at, run_id) of the ok run that finished last, or None."""
    best = None
    for run in runs:
        if not _true(run.get("ok")):
            continue
        finished = _instant(run.get("finished_at"))
        if finished is None:
            continue
        run_id = _run_name(run.get("run_id"))
        if run_id is None:
            run_id = _run_name(run.get("id"))  # the D1 row id, when a mirror drops run_id
        if run_id is None:
            run_id = "unknown"
        if best is None or (finished, run_id) > best:
            best = (finished, run_id)
    return best


def _run_name(value):
    """A run id as text: a non-empty string, or an integer (D1's row id); else None."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _push(kind, key, item_ids, title, body, local_date, priority="normal"):
    return {
        "kind": kind,
        "dedup_key": key,
        "item_ids": list(item_ids),
        "title": title,
        "body": body,
        "local_date": local_date,
        "priority": priority,
    }


def _title(item_id, item):
    title = item.get("title")
    if isinstance(title, str) and _strip(title):
        return title  # the public title, verbatim: its project prefix stays
    return item_id


def _quiet(hour, conf):
    start, end = conf["quiet_start"], conf["quiet_end"]
    if start > end:  # the usual window across midnight, e.g. 22 -> 8
        return hour >= start or hour < end
    return start <= hour < end  # a same-day window; start == end means no quiet hours


def _snooze_over(item, tick):
    snooze = _wall(item.get("snooze_until"))
    if snooze is None:
        return False
    until = _wall_utc(snooze, tick.zone)
    return until is not None and until <= tick.now


# ----------------------------------------------------------------------------- input rows


def _entries(items):
    """(id, item) pairs, first occurrence of each id, skipping rows without a text id."""
    if isinstance(items, Mapping):
        pairs = []
        for key, item in items.items():
            if isinstance(item, Mapping):
                item_id = item.get("id")
                pairs.append((key if item_id is None or item_id == "" else item_id, item))
    else:
        pairs = [(item.get("id"), item) for item in _rows(items)]
    seen = set()
    out = []
    for item_id, item in pairs:
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            continue
        seen.add(item_id)
        out.append((item_id, item))
    return out


def _rows(rows):
    """The Mapping rows of a list (or another iterable); a string, a mapping or None holds none."""
    if rows is None or isinstance(rows, (str, bytes, Mapping)):
        return []
    try:
        rows = iter(rows)
    except TypeError:  # not iterable
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _id_list(value):
    """A row's item_ids as a list: a list or tuple, or a JSON array in a string (the D1 column)."""
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _no_constant(name):
    raise ValueError("JSON has no %s" % name)  # NaN and Infinity, as JSON.parse refuses them


def _note_kind(note):
    kind = note.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    key = note.get("dedup_key")
    if isinstance(key, str) and ":" in key:
        return key.split(":", 1)[0]
    return None


def _note_day(note, zone):
    """The row's local_date, else the local date of its sent_at."""
    stated = _wall(note.get("local_date"))
    if stated is not None and stated[1] is None:
        return stated[0]
    sent = _local(_instant(note.get("sent_at")), zone)
    return None if sent is None else sent.date()


def _note_key(note, zone):
    """The row's dedup_key. Rows without one (a mirror that drops the column) still dedup
    digests by kind + local date and advances by their single item id."""
    key = note.get("dedup_key")
    if isinstance(key, str) and key:
        return key
    kind = _note_kind(note)
    if kind == "digest":
        day = _note_day(note, zone)
        return None if day is None else "digest:" + day.isoformat()
    if kind == "advance":
        ids = _id_list(note.get("item_ids"))
        if len(ids) == 1 and isinstance(ids[0], str) and ids[0]:
            return "advance:" + ids[0]
    return None


def _true(value):
    """1 (D1 integers) or true (JSON); "1", 2, None and everything else are not. It makes a run
    a heartbeat, a digest row a delivered digest and an interruption resolved; the budget and
    dedup ignore ok."""
    return value is True or (type(value) in (int, float) and value == 1)


_INT_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)


def _settings(cfg):
    conf = {}
    for key, default in DEFAULTS.items():
        if cfg is None:
            value = None
        elif isinstance(cfg, Mapping):
            value = cfg.get(key)
        else:
            value = getattr(cfg, key, None)
        conf[key] = default if value is None else value
    for key in ("digest_hour", "quiet_start", "quiet_end", "daily_budget"):
        conf[key] = _whole(key, conf[key])
    conf["deadman_hours"] = _hours(conf["deadman_hours"])
    conf["enable_deadman"] = _flag(conf["enable_deadman"])
    return conf


def _whole(key, value):
    """An integer setting: an int, a finite float (truncated) or a decimal string like "9"."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and _finite(value):
        return int(value)
    if isinstance(value, str) and _INT_RE.fullmatch(_strip(value)):
        return int(_strip(value))
    raise ValueError("%s must be a whole number, got %r" % (key, value))


def _hours(value):
    """deadman_hours: a finite number of hours (or a decimal string) within timedelta's range."""
    hours = None
    try:
        if isinstance(value, (bool, int, float)):
            hours = float(value)
        elif isinstance(value, str) and _FLOAT_RE.fullmatch(_strip(value)):
            hours = float(_strip(value))
    except OverflowError:  # an int too large for a float
        hours = None
    if hours is None or not _finite(hours) or abs(hours) > _MAX_HOURS:
        raise ValueError("deadman_hours must be a number of hours, got %r" % (value,))
    return hours


def _finite(number):
    return number == number and number not in (float("inf"), float("-inf"))


def _flag(value):
    if isinstance(value, str):
        return _strip(value).lower() in ("1", "true", "yes", "on")
    return bool(value)


def _strip(text):
    return text.strip(_WS)


# ----------------------------------------------------------------------------- time


_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
    r"\s*(?:([Zz])|([+-])(\d{2}):?(\d{2}))?",
    re.ASCII,
)
_WALL_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2}))?)?", re.ASCII)


def _zone(tz):
    if isinstance(tz, tzinfo):
        return tz
    if isinstance(tz, str) and _strip(tz):
        name = _strip(tz)
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc
        if ZoneInfo is None:  # pragma: no cover
            raise ValueError("zoneinfo is unavailable; pass a tzinfo instead of %r" % name)
        try:
            return ZoneInfo(name)
        except Exception as exc:  # ZoneInfoNotFoundError is a KeyError; malformed keys raise ValueError
            raise ValueError("unknown timezone %r" % name) from exc
    raise ValueError("tz must be an IANA timezone name or a tzinfo, got %r" % (tz,))


def _instant(value):
    """An ISO-8601 instant (Z, offsets, fractional seconds; naive = UTC) as aware UTC, or None."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return _local(value, timezone.utc)
    if not isinstance(value, str):
        return None
    match = _ISO_RE.fullmatch(_strip(value))
    if match is None:
        return None
    year, month, day, hh, mm, ss, frac, zulu, sign, off_h, off_m = match.groups()
    try:
        zone = timezone.utc
        if sign:
            offset = timedelta(hours=int(off_h), minutes=int(off_m))
            zone = timezone(offset if sign == "+" else -offset)
        micro = int((frac or "0")[:6].ljust(6, "0"))
        stamp = datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0),
                         int(ss or 0), micro, tzinfo=zone)
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):  # bad fields, or an instant outside datetime's range
        return None


def _local(instant, zone):
    """``instant`` in ``zone``; None when it is None or the zone would push it out of range."""
    if instant is None:
        return None
    try:
        return instant.astimezone(zone)
    except (ValueError, OverflowError):
        return None


def _wall(value):
    """A local wall time "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" as (date, (h, m, s) or None)."""
    if not isinstance(value, str):
        return None
    match = _WALL_RE.fullmatch(_strip(value))
    if match is None:
        return None
    year, month, day, hh, mm, ss = match.groups()
    try:
        when = date(int(year), int(month), int(day))
    except ValueError:
        return None
    if hh is None:
        return when, None
    clock = (int(hh), int(mm), int(ss or 0))
    if clock[0] > 23 or clock[1] > 59 or clock[2] > 59:
        return None
    return when, clock


def _wall_utc(wall, zone):
    """The instant of a local wall time, or None when it falls outside datetime's range.

    fold=0 (PEP 495, as core.timeutil does): an ambiguous fall-back time is its earlier
    instant, and a time inside the spring-forward gap maps on the pre-transition offset
    (02:30 on 2026-03-08 in America/Chicago is 08:30Z).
    """
    day, clock = wall
    hh, mm, ss = clock if clock is not None else (0, 0, 0)
    try:
        return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=zone).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _when(instant, tick):
    """'8:03 AM' for today, else 'Mon Oct 5, 11:02 PM' (local)."""
    local = _local(instant, tick.zone)
    if local is None:
        return _iso_utc(instant)
    hour = local.hour % 12 or 12
    clock = "%d:%02d %s" % (hour, local.minute, "AM" if local.hour < 12 else "PM")
    if local.date() == tick.today:
        return clock
    return "%s %s %d, %s" % (_WEEKDAYS[local.weekday()], _MONTHS[local.month - 1], local.day, clock)


def _iso_utc(instant):
    """'2026-10-06T14:02:00Z' (the fallback when an instant has no local rendering)."""
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (instant.year, instant.month, instant.day,
                                               instant.hour, instant.minute, instant.second)
