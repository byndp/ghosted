"""digest.md: everything open, grouped the way the phone groups it (contract section 15, Layout).

``group(items, now, tz)`` sorts items into the phone's groups; ``render(...)`` writes the Markdown
digest (full LOCAL titles, falling back to the public title: it never leaves the Mac);
``live_waiting(stops, state, now, cfg)`` lists sessions whose latest Stop hook record is an open
question or a usage limit that the detector has not processed yet ("Waiting right now").

No transcript text ever reaches the digest: interrupted items carry template titles and machine
evidence, and stop records hold verdicts and reason codes only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core import timeutil

GROUPS = ("due", "interrupted", "waiting", "unsaved", "later", "snoozed")
GROUP_TITLES = {
    "due": "Due soon / overdue",
    "interrupted": "Interrupted",
    "waiting": "Waiting on you",
    "unsaved": "Unsaved work",
    "later": "Also open (owner claude)",
    "snoozed": "Snoozed",
}
SOON_HOURS = 48
SOON_DAYS = 2
LIVE_VERDICTS = ("open_question", "usage_limit")
SEP = " · "

_FAR = datetime.max.replace(tzinfo=timeutil.UTC) - timedelta(days=2)


def _now(now: Any) -> datetime:
    return timeutil.now_utc() if now is None else timeutil.parse_iso(now)


def _items(items: Any) -> List[Mapping[str, Any]]:
    if items is None:
        return []
    if isinstance(items, Mapping):
        items = items.values()
    return [it for it in items if isinstance(it, Mapping)]


def _due_instant(item: Mapping[str, Any], tz: Any) -> Optional[datetime]:
    try:
        return timeutil.due_instant(item.get("due_at"), tz)
    except (ValueError, TypeError, OverflowError):
        return None


def _due_date(item: Mapping[str, Any]) -> Optional[date]:
    try:
        return timeutil.due_date(item["due_at"]) if item.get("due_at") else None
    except (ValueError, TypeError):
        return None


def is_due_soon(item: Mapping[str, Any], now: datetime, tz: Any) -> bool:
    """Due within 48 h or past due. A date-only due D counts when D <= local today + 2."""
    due = item.get("due_at")
    if not due:
        return False
    instant = _due_instant(item, tz)
    if instant is None:
        return False
    if timeutil.is_timed(due):
        return instant - now <= timedelta(hours=SOON_HOURS)
    day = _due_date(item)
    return day is not None and day <= timeutil.local_date(now, tz) + timedelta(days=SOON_DAYS)


def _id_key(item: Mapping[str, Any]) -> Tuple[int, str]:
    raw = item.get("id")
    text = raw if isinstance(raw, str) else ""
    digits = text[3:] if text.startswith("GS-") else ""
    return (int(digits) if digits.isdigit() else 0, text)


def _due_review_key(item: Mapping[str, Any], tz: Any) -> Tuple[Any, ...]:
    due = _due_instant(item, tz)
    review = item.get("review_at") if isinstance(item.get("review_at"), str) else ""
    return (due is None, due or _FAR, review == "", review) + _id_key(item)


def _snooze_key(item: Mapping[str, Any], tz: Any) -> Tuple[Any, ...]:
    until = item.get("snooze_until")
    try:
        instant = timeutil.due_instant(until, tz) if isinstance(until, str) and until else None
    except (ValueError, TypeError, OverflowError):
        instant = None
    return (instant is None, instant or _FAR) + _id_key(item)


def group(items: Any, now: Any, tz: Any) -> Dict[str, List[Mapping[str, Any]]]:
    """The phone's groups, each sorted (contract 15 Layout).

    ``due``: open items due within 48 h or past (by due). ``interrupted``: other open interrupted
    items. ``waiting``: other open items owned by the user (by due, then review_at). ``later``: other
    open items owned by claude. ``snoozed``: snoozed items, by snooze_until. Done and dismissed
    items appear nowhere.
    """
    when = _now(now)
    out: Dict[str, List[Mapping[str, Any]]] = {g: [] for g in GROUPS}
    for item in _items(items):
        status = item.get("status")
        if status == "snoozed":
            out["snoozed"].append(item)
        elif status != "open":
            continue
        elif is_due_soon(item, when, tz):
            out["due"].append(item)
        elif item.get("kind") == "interrupted":
            out["interrupted"].append(item)
        elif item.get("kind") == "unsaved":
            out["unsaved"].append(item)
        elif item.get("owner") == "claude":
            out["later"].append(item)
        else:
            out["waiting"].append(item)
    for name in ("due", "interrupted", "waiting", "unsaved", "later"):
        out[name].sort(key=lambda it: _due_review_key(it, tz))
    out["snoozed"].sort(key=lambda it: _snooze_key(it, tz))
    return out


# ---- live waiting --------------------------------------------------------------------------------

def _instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return timeutil.parse_iso(value)
    except ValueError:
        return None


def live_waiting(stops: Any, state: Any, now: Any, cfg: Any) -> List[Dict[str, Any]]:
    """Sessions waiting right now: the latest stop record says open question or usage limit, and
    the detector has not processed the session since (it is deferred, or that stop is newer than
    the processed version). Newest first.

    Each entry: ``{"session_uuid", "project", "verdict", "reason", "at", "until"}`` (``until`` = the
    end of a usage-limit deferral, else None). A stop the detector has already run past without
    deferring it (its file is not under projects_dir, or is skipped) is left out, and so is one
    older than ``scan_since_days`` that the detector never saw.
    """
    when = _now(now)
    st = state if isinstance(state, Mapping) else {}
    sessions = st.get("sessions") if isinstance(st.get("sessions"), Mapping) else {}
    deferred = {u for u in st.get("deferred") or [] if isinstance(u, str)} if isinstance(st.get("deferred"), list) else set()
    info = st.get("deferred_info") if isinstance(st.get("deferred_info"), Mapping) else {}
    last_scan = _instant(st.get("last_scan"))
    horizon = when - timedelta(days=max(1, int(getattr(cfg, "scan_since_days", 14) or 14)))

    latest: Dict[str, Tuple[Tuple[datetime, int], Mapping[str, Any]]] = {}
    for index, row in enumerate(stops or []):
        if not isinstance(row, Mapping):
            continue
        sid = row.get("session_id")
        at = _instant(row.get("at"))
        if not isinstance(sid, str) or not sid or at is None:
            continue
        key = (at, index)
        if sid not in latest or key >= latest[sid][0]:
            latest[sid] = (key, row)

    out: List[Dict[str, Any]] = []
    for sid, ((at, _), row) in latest.items():
        verdict = row.get("verdict")
        if verdict not in LIVE_VERDICTS:
            continue
        rec = sessions.get(sid) if isinstance(sessions.get(sid), Mapping) else None
        if sid not in deferred:
            processed = _instant(rec.get("processed_at")) if rec else None
            if processed is not None and at <= processed:
                continue  # the detector has seen this ending
            if last_scan is not None and at <= last_scan:
                continue  # a scan ran after this stop and did not defer the session
            if at < horizon:
                continue
        project = row.get("cwd_project")
        if not isinstance(project, str) or not project.strip():
            project = (rec or {}).get("project") if isinstance((rec or {}).get("project"), str) else "unknown"
        reason = row.get("reason") if isinstance(row.get("reason"), str) else None
        until = None
        extra = info.get(sid) if isinstance(info.get(sid), Mapping) else None
        if sid in deferred and extra is not None and extra.get("why") == "limit":
            until = extra.get("until") if isinstance(extra.get("until"), str) else None
        out.append({"session_uuid": sid, "project": project, "verdict": verdict, "reason": reason,
                    "at": timeutil.to_iso(at), "until": until})
    out.sort(key=lambda e: (e["at"], e["session_uuid"]), reverse=True)
    return out


# ---- render --------------------------------------------------------------------------------------

def _one_line(text: Any) -> str:
    return " ".join(str(text).split())


def _title(item: Mapping[str, Any]) -> str:
    for key in ("local_title", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value)
    return str(item.get("id") or "?")


def _when(instant: datetime, tz: Any, today: date) -> str:
    local = timeutil.local_dt(instant, tz)
    clock = timeutil.human_time(local)
    return clock if local.date() == today else "%s, %s" % (timeutil.human_date(local.date()), clock)


def _due_text(item: Mapping[str, Any], now: datetime, tz: Any) -> Optional[str]:
    due = item.get("due_at")
    if not due:
        return None
    try:
        label = timeutil.human_due(due)
    except (ValueError, TypeError):
        return "due " + _one_line(due)
    instant = _due_instant(item, tz)
    today = timeutil.local_date(now, tz)
    day = _due_date(item)
    if timeutil.is_timed(due):
        late = instant is not None and instant < now
    else:
        late = day is not None and day < today
    if late:
        return "due %s (overdue)" % label
    if day == today:
        return "due %s (today)" % label
    return "due " + label


def _review_text(item: Mapping[str, Any]) -> Optional[str]:
    review = item.get("review_at")
    if not isinstance(review, str) or not review:
        return None
    try:
        return "review " + timeutil.human_date(timeutil.parse_date(review))
    except ValueError:
        return None


def _snooze_text(item: Mapping[str, Any]) -> Optional[str]:
    until = item.get("snooze_until")
    if not isinstance(until, str) or not until:
        return None
    try:
        return "until " + timeutil.human_due(until)
    except ValueError:
        return None


def _item_lines(name: str, item: Mapping[str, Any], now: datetime, tz: Any) -> List[str]:
    parts = ["%s%s%s" % (item.get("id") or "?", SEP, _title(item))]
    if name == "snoozed":
        extra = _snooze_text(item)
        if extra:
            parts.append(extra)
    due = _due_text(item, now, tz)
    if due:
        parts.append(due)
    elif name in ("waiting", "later", "snoozed"):
        review = _review_text(item)
        if review:
            parts.append(review)
    lines = ["- " + SEP.join(parts)]
    if item.get("kind") == "interrupted":
        ev = item.get("evidence")
        if isinstance(ev, str) and ev.strip():
            lines.append("  " + _one_line(ev))
        if item.get("looks_resolved"):
            lines.append("  looks resolved")
        resume = item.get("resume")
        if isinstance(resume, str) and resume.strip():
            lines.append("  `%s`" % _one_line(resume))
    return lines


def _run_text(run: Optional[Mapping[str, Any]], now: datetime, tz: Any) -> str:
    if not isinstance(run, Mapping):
        return "no run yet"
    ok = run.get("ok")
    word = "ok" if ok is True or ok == 1 else "failed"
    finished = _instant(run.get("finished_at")) or _instant(run.get("started_at"))
    if finished is None or abs((now - finished).total_seconds()) < 60:
        return "last run " + word
    return "last run %s (%s)" % (word, _when(finished, tz, timeutil.local_date(now, tz)))


def push_problem(push: Any) -> str:
    """The Worker's push block (SyncResult.push: {"subscriptions", "last_ok_at", "last_failure",
    "undeliverable"}) as one line: 'pushes failing since <time>: <error>', 'pushes cannot be
    delivered: <why>', or '' when pushes are reaching a phone."""
    if not isinstance(push, Mapping):
        return ""
    failure = push.get("last_failure")
    if isinstance(failure, Mapping):
        when = _instant(failure.get("sent_at"))
        why = _one_line(failure.get("error") or "unknown error")[:120]
        return "pushes failing since %s: %s" % (timeutil.to_iso(when) if when else "?", why)
    why = push.get("undeliverable")
    if isinstance(why, str) and why.strip():
        return "pushes cannot be delivered: %s" % _one_line(why)[:120]
    return ""


def sync_text(sync: Any) -> str:
    """The header's sync part. None -> "local only"; a string is used as it is; a result with
    ``ok`` (a SyncResult or a dict) -> "synced" (with ", N rejected (ghosted sync shows why)" when the
    server refused events, and the push channel's problem when it has one) or "sync failed:
    <error>"."""
    if sync is None:
        return "local only"
    if isinstance(sync, str):
        return _one_line(sync) or "local only"
    get = (lambda k: sync.get(k)) if isinstance(sync, Mapping) else (lambda k: getattr(sync, k, None))
    ok, error = get("ok"), get("error")
    if ok:
        text = "synced"
        acks = get("acks")
        if isinstance(acks, Mapping):
            rejected = sum(1 for a in acks.values() if isinstance(a, Mapping) and a.get("result") == "invalid")
            if rejected:
                text += ", %d rejected (ghosted sync shows why)" % rejected
        problem = push_problem(get("push"))
        if problem:
            text += SEP + problem
        return text
    text = "sync failed"
    if error:
        text += ": " + _one_line(error)[:80]
    return text


def _live_lines(live: Iterable[Mapping[str, Any]], now: datetime, tz: Any) -> List[str]:
    today = timeutil.local_date(now, tz)
    out = []
    for entry in live:
        at = _instant(entry.get("at"))
        verdict = entry.get("verdict")
        what = "hit a usage limit" if verdict == "usage_limit" else "ended on an open question"
        sid = entry.get("session_uuid") or "?"
        parts = [_one_line(entry.get("project") or "unknown"), "session %s" % str(sid)[:8], what]
        if at is not None:
            parts.append(_when(at, tz, today))
        until = _instant(entry.get("until"))
        if until is not None:
            parts.append("deferred until " + _when(until, tz, today))
        out.append("- " + SEP.join(parts))
    return out


def render(items: Any, now: Any, tz: Any, run: Optional[Mapping[str, Any]] = None, sync: Any = None,
           live: Optional[Iterable[Mapping[str, Any]]] = None, notes: Optional[Iterable[str]] = None) -> str:
    """The digest as Markdown. Header: ``Ghosted · generated <time> · last run <ok|failed> ·
    <sync status>``; then "Waiting right now" (from ``live``), then the groups. ``notes`` are
    short machine lines shown under the header (e.g. a scan that was skipped)."""
    when = _now(now)
    today = timeutil.local_date(when, tz)
    generated = "%s, %s" % (timeutil.human_date(today), timeutil.human_time(when, tz))
    lines = ["# Ghosted%sgenerated %s%s%s%s%s" % (SEP, generated, SEP, _run_text(run, when, tz), SEP, sync_text(sync)), ""]
    for note in notes or []:
        if note:
            lines.append("_%s_" % _one_line(note))
            lines.append("")
    live_list = list(live or [])
    if live_list:
        lines.append("## Waiting right now (%d)" % len(live_list))
        lines.extend(_live_lines(live_list, when, tz))
        lines.append("")
    groups = group(items, when, tz)
    shown = False
    for name in GROUPS:
        members = groups[name]
        if not members:
            continue
        shown = True
        lines.append("## %s (%d)" % (GROUP_TITLES[name], len(members)))
        for item in members:
            lines.extend(_item_lines(name, item, when, tz))
        lines.append("")
    if not shown:
        lines.append("No open items." if live_list else "Nothing open.")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
