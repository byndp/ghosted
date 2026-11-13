"""The pull report (contract 16.3): ``ghosted report [WINDOW]`` and the ``/ghosted`` skill.

``gather(cfg, ledger, now, window=...) -> Report`` collects, in the contract's order:

1. a detector scan under ``runner.run_lock`` (non-blocking: a held lock skips the scan and the
   stored state is read; ``scan="busy"``), with ``since_floor`` = the window start;
2. the hook's records (the latest ``stops.jsonl`` record per session): a newer ``StopFailure``
   usage limit makes the session a usage limit, a ``Stop`` that ended clean more than 60 s after
   the stored ending drops a flagged session;
3. live state from ``claude agents --json --all`` (``core.agents.live``, injectable): an active
   session is skipped and counted, a blocked one waits on you (``agent_blocked``) whatever the
   transcript says, an idle interactive one with a pid gets ``open_in_terminal``;
4. sessions the detector deferred as maybe active are only counted.

Rows come from ``state.sessions`` (flagged, ended in the window), usage-limit deferrals still
waiting for their reset, and due ``ghosted add`` items. A row's item must be open (done, dismissed and
still-snoozed items are counted as ``handled``). Git facts are read once per launch directory.

``render_text(report, quotes=True, soft=False, plain=False)`` and ``render_json(report,
quotes=False)`` print it. The quote is the one place transcript text reaches a person: it is
extracted at render time by re-parsing only the rows' transcripts (paths from one listing of the
projects folder), kept in memory on the Report, and never written anywhere (not to measure.jsonl,
state, logs, or JSON unless ``quotes=True``). ``gather`` appends a ``report`` record to
measure.jsonl with ids and counts only.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from core import classify, detector, evidence, timeutil, touches
from core import ledger as ledger_mod
from core import quote as quote_mod

DEFAULT_WINDOW = "7d"
FIRST_WINDOW = "30d"
ALL_WINDOW = "all"      # everything on disk (contract 16.15)
MAX_DAYS = 3650
OLDER_DAYS = 30         # in a longer window, rows older than this sit under their own heading
RECEIPT_ADDRESS = "github.com/byndp/ghosted"   # the last line of `ghosted receipt` (16.20)
# What an empty report says it looked for (the waiting tier, contract 16.1), in plain words.
ALL_CLEAR_LOOKED_FOR = ("unanswered questions, usage-limit cut-offs, plans waiting for approval, "
                        "blocked background sessions")
MEASURE_REPORT = "report"
VIA = ("cli", "skill")

HOOK_LIMIT_EVENT = "StopFailure"
HOOK_STOP_EVENT = "Stop"
CLEAN_GRACE_SECONDS = 60     # a clean Stop this long after the stored ending drops a flagged row
HOOK_SOURCE_SLACK = 5        # a record from 5 s before the ending still counts as the hook's
BLOCKED_ACT_SLACK = 2        # a blocked session's file this close to the user's act is not new activity
RULE_WIDTH = 52
DUE_AHEAD_DAYS = 2
MIXED_RESUMES = 3            # resume commands on a mixed row before "+N more" (contract 16.11)

KIND_QUESTION = "question"
KIND_ASK_USER = "ask_user"
KIND_PLAN = "plan_approval"
KIND_LIMIT = "usage_limit"
KIND_BLOCKED = "agent_blocked"
KIND_SOFT = "soft"
KIND_DUE = "due"
REASON_BLOCKED = "agent_blocked"

GLYPHS = {KIND_QUESTION: "?", KIND_ASK_USER: "?", KIND_PLAN: "▤", KIND_LIMIT: "⏸",
          KIND_BLOCKED: "●", KIND_DUE: "◷", KIND_SOFT: "·"}

_AUTO = object()
_WINDOW_RE = re.compile(r"(\d{1,4})d")

# The plain word for the save verb a save offer used (contract 16.1's list).
_SAVE_WORDS = (("commit", "commit"), ("push", "push"), ("merg", "merge"), ("redeploy", "deploy"),
               ("re-deploy", "deploy"), ("deploy", "deploy"), ("ship", "ship it"),
               ("republish", "publish"), ("re-publish", "publish"), ("publish", "publish"),
               ("rereleas", "release"), ("re-releas", "release"), ("releas", "release"),
               ("land", "land it"), ("check", "check it in"), ("pull request", "open a PR"),
               ("pr", "open a PR"))


# ---- the window ---------------------------------------------------------------------------------

def parse_window(text: Any) -> Tuple[str, int]:
    """``("today", 1)``, ``("7d", 7)`` or ``("all", MAX_DAYS)`` for ``today`` / ``Nd`` (1 <= N <=
    3650) / ``all`` (``gather`` then starts at the oldest session). ValueError else."""
    if not isinstance(text, str):
        raise ValueError("the window is today or Nd, e.g. 7d")
    value = text.strip().lower()
    if value == "today":
        return "today", 1
    if value == ALL_WINDOW:
        return ALL_WINDOW, MAX_DAYS
    m = _WINDOW_RE.fullmatch(value)
    if not m:
        raise ValueError("%r is not a window: use today, 7d, 30d (any Nd from 1d to %dd) or all"
                         % (text, MAX_DAYS))
    days = int(m.group(1))
    if not 1 <= days <= MAX_DAYS:
        raise ValueError("the window is 1d to %dd (got %s)" % (MAX_DAYS, value))
    return "%dd" % days, days


def session_mtimes(cfg: Any) -> List[float]:
    """When each session file was last written (stat calls only; nothing is read). Never raises."""
    try:
        return [st.st_mtime for _, _, st in detector.enumerate_sessions(cfg)]
    except Exception:  # noqa: BLE001 - no history to offer; the report still prints
        return []


def days_back_to(mtime: float, now: datetime, tz: str) -> int:
    """The window, in days, that starts on the local day of ``mtime`` (1..MAX_DAYS)."""
    try:
        day = timeutil.local_date(datetime.fromtimestamp(mtime, timezone.utc), tz)
    except (OverflowError, OSError, ValueError):
        return MAX_DAYS
    return max(1, min(MAX_DAYS, (timeutil.local_date(now, tz) - day).days + 1))


def window_start(days: int, now: datetime, tz: str) -> datetime:
    """Local midnight of ``today - (days - 1)``, as an aware UTC datetime."""
    first = timeutil.local_date(now, tz) - timedelta(days=days - 1)
    return timeutil.wall_to_utc(first, 0, 0, tz)


def is_first_report(ledger: Any) -> bool:
    """No ``report`` record in measure.jsonl yet."""
    return not any(rec.get("type") == MEASURE_REPORT for rec in ledger.measures())


# ---- small helpers ------------------------------------------------------------------------------

def _instant(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return timeutil.parse_iso(value)
    except ValueError:
        return None


def one_line(text: Any) -> str:
    """``text`` on one line: every control, format and line/paragraph separator character
    becomes a space and whitespace runs collapse (the report's one-line rule)."""
    if not isinstance(text, str):
        return ""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        out.append(" " if (cat[0] == "C" or cat in ("Zl", "Zp") or ch.isspace()) else ch)
    return " ".join("".join(out).split())


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _plural(n: int, word: str, many: Optional[str] = None) -> str:
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day_label(d: date, ref: Optional[date] = None) -> str:
    """``Thu 6`` (``Thu 6 Oct`` when ``d`` is not in ``ref``'s month, plus the year when not in its year)."""
    text = "%s %d" % (_DAY_ABBR[d.weekday()], d.day)
    if ref is None or (d.year, d.month) != (ref.year, ref.month):
        text += " " + _MONTH_ABBR[d.month - 1]
        if ref is not None and d.year != ref.year:
            text += " %d" % d.year
    return text


def range_label(start: date, end: date) -> str:
    """``Mon 2 – Sun 8 Nov``, ``Fri 30 Oct – Sun 8 Nov``, ``Today, Sun 8 Nov``."""
    if start == end:
        return "Today, " + day_label(end)
    head = "%s %d" % (_DAY_ABBR[start.weekday()], start.day)
    if (start.year, start.month) != (end.year, end.month):
        head += " " + _MONTH_ABBR[start.month - 1]
        if start.year != end.year:
            head += " %d" % start.year
    return "%s – %s" % (head, day_label(end))


def _time_label(when: datetime, now: datetime, tz: str) -> str:
    """``6:00 AM`` today, else ``Mon 9, 6:00 AM``."""
    local_day = timeutil.local_date(when, tz)
    text = timeutil.human_time(when, tz)
    if local_day != timeutil.local_date(now, tz):
        text = "%s, %s" % (day_label(local_day, timeutil.local_date(now, tz)), text)
    return text


def _age(ended: datetime, now: datetime, tz: str) -> Tuple[int, str]:
    days = (timeutil.local_date(now, tz) - timeutil.local_date(ended, tz)).days
    if days <= 0:
        minutes = max(0, int((now - ended).total_seconds() // 60))
        if minutes < 60:
            return 0, "%d min ago" % max(1, minutes)
        return 0, "%dh ago" % (minutes // 60)
    if days == 1:
        return 1, "yesterday"
    return days, "%dd ago" % days


def latest_stops(records: Any) -> Dict[str, Dict[str, Any]]:
    """The latest stops.jsonl record per ``session_id`` (by ``at``; a tie keeps the later line)."""
    out: Dict[str, Tuple[datetime, Dict[str, Any]]] = {}
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        sid = rec.get("session_id")
        at = _instant(rec.get("at"))
        if not isinstance(sid, str) or at is None:
            continue
        prior = out.get(sid)
        if prior is None or at >= prior[0]:
            out[sid] = (at, rec)
    return {sid: dict(rec, _at=at) for sid, (at, rec) in out.items()}


def save_word(text: Any) -> Optional[str]:
    """The plain verb of the first save word in ``text`` (``commit``, ``push``, ``open a PR``)."""
    rx = getattr(classify, "_SAVE_VERB_RE", None)
    if rx is None or not isinstance(text, str):
        return None
    m = rx.search(text)
    if not m:
        return None
    word = m.group(0).lower()
    for prefix, plain in _SAVE_WORDS:
        if word.startswith(prefix):
            return plain
    return None


# ---- the report ---------------------------------------------------------------------------------

class Report:
    """What one report found. ``rows`` = waiting rows then soft rows (dicts with the contract's
    fields, plus private ``_``-prefixed ones that never reach JSON). Quotes are loaded on demand
    by ``load_quotes`` and kept only in memory."""

    def __init__(self) -> None:
        self.generated_at: Optional[datetime] = None
        self.window_name = DEFAULT_WINDOW
        self.days = 7
        self.start: Optional[datetime] = None
        self.end: Optional[datetime] = None
        self.tz = "UTC"
        self.first_run = False
        # First run only: {"sessions": N, "since": iso} for session files older than the window.
        self.older: Optional[Dict[str, Any]] = None
        self.via = "cli"
        self.project: Optional[str] = None
        self.counts: Dict[str, int] = {k: 0 for k in ("checked", "clean", "waiting", "soft", "active",
                                                      "handled", "automated", "also_open", "unsaved")}
        self.sources: Dict[str, Any] = {"hook": False, "transcripts": "best-effort backfill", "agents": False}
        self.scan = "skipped"
        self.history_missing: Optional[str] = None      # projects_dir, when that folder does not exist
        self.scan_errors = 0
        self.drift: Optional[Dict[str, Any]] = None
        self.rows: List[Dict[str, Any]] = []
        self.quiet_projects = 0
        self.projects_checked = 0                       # projects with a checked session in the window
        self.unsaved: List[Dict[str, Any]] = []         # Unsaved work rows (contract 16.9)
        self.unsaved_notes: List[Tuple[str, str]] = []  # (project, note) for --debug
        self.shared_now: List[Dict[str, Any]] = []      # repos two active sessions work in (16.11 B)
        self.cfg: Any = None
        self._paths: Optional[Dict[str, Path]] = None
        self._summaries: Dict[str, Any] = {}
        self._quotes: Optional[Dict[str, Optional[str]]] = None

    @property
    def waiting(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["tier"] == classify.WAITING]

    @property
    def soft(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["tier"] == classify.SOFT]

    # -- transcripts, re-read only for the rows ------------------------------------------------------

    def path_of(self, uuid: str) -> Optional[Path]:
        if self._paths is None:
            self._paths = {}
            if self.cfg is not None:
                try:
                    for sid, path, _ in detector.enumerate_sessions(self.cfg):
                        self._paths[sid] = path
                except Exception:  # noqa: BLE001 - no paths, no quotes; the report still prints
                    pass
        return self._paths.get(uuid)

    def summary(self, uuid: Optional[str]) -> Any:
        if not isinstance(uuid, str):
            return None
        if uuid not in self._summaries:
            found = None
            path = self.path_of(uuid)
            if path is not None:
                try:
                    from core import transcript

                    found = transcript.parse_transcript(path)
                except Exception:  # noqa: BLE001 - a file that vanished or broke: no quote
                    found = None
            self._summaries[uuid] = found
        return self._summaries[uuid]

    def load_quotes(self, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Optional[str]]:
        """``{row key: quote or None}`` for ``rows`` (default: every row). Due rows have none
        (their title is shown), nor do usage limits (the reset time is)."""
        if self._quotes is None:
            self._quotes = {}
        quotes = self._quotes
        for row in (self.rows if rows is None else rows):
            if row["_key"] in quotes or row["kind"] in (KIND_DUE, KIND_LIMIT):
                continue
            summary = self.summary(row.get("session_uuid"))
            if summary is None:
                continue
            try:
                text = quote_mod.extract(summary, row.get("_verdict"), row.get("_quote_reason"),
                                         handoff_patterns=getattr(self.cfg, "handoff_patterns", None))
            except Exception:  # noqa: BLE001
                text = None
            quotes[row["_key"]] = one_line(text) if text else None
        return quotes

    def save_verb(self, row: Mapping[str, Any]) -> Optional[str]:
        """The save verb of a promoted save offer ("commit"), from its question sentence."""
        summary = self.summary(row.get("session_uuid"))
        text = getattr(summary, "final_text", None) if summary is not None else None
        if not isinstance(text, str):
            return None
        for find in (classify.question_sentence_span, classify.phrase_sentence_span):
            try:
                span = find(text)
            except Exception:  # noqa: BLE001
                span = None
            if span:
                word = save_word(text[span[0]:span[1]])
                if word:
                    return word
        return None


def _git_row_fields(facts: Optional[Mapping[str, Any]], cwd: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"uncommitted": None, "unpushed": None, "branch_no_upstream": False,
                           "not_git": False}
    if not isinstance(cwd, str) or not cwd:
        return out
    if not facts:
        out["not_git"] = True
        return out
    out["uncommitted"] = _int(facts.get("dirty"))
    out["unpushed"] = _int(facts.get("ahead"))  # no upstream: commits on no remote (evidence.git_facts)
    if not facts.get("upstream"):
        out["branch_no_upstream"] = bool(facts.get("branch")) and not facts.get("timed_out")
    return out


class _Gather:
    def __init__(self, cfg: Any, ledger: Any, now: datetime, report: Report,
                 live: Optional[Dict[str, Any]], project: Optional[str]) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.now = now
        self.r = report
        self.live = live
        self.project = project.strip().lower() if isinstance(project, str) and project.strip() else None
        self.tz = report.tz
        self.git_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self.direct = getattr(cfg, "direct_questions", classify.WAITING) or classify.WAITING
        self.active_checked = 0  # checked sessions skipped because they are running now
        self.active_deferred: Set[str] = set()

    def git(self, cwd: Optional[str]) -> Optional[Dict[str, Any]]:
        if not isinstance(cwd, str) or not cwd:
            return None
        key = os.path.normpath(cwd)
        if key not in self.git_cache:
            self.git_cache[key] = evidence.git_facts(key)
        return self.git_cache[key]

    def wanted(self, project: Any) -> bool:
        if self.project is None:
            return True
        return isinstance(project, str) and project.strip().lower() == self.project

    def in_window(self, when: Optional[datetime]) -> bool:
        return when is not None and self.r.start <= when <= self.r.end  # type: ignore[operator]

    def live_class(self, uuid: str) -> Optional[str]:
        if not self.live:
            return None
        from core import agents

        return agents.classify_live(self.live.get(uuid))

    def item_state(self, item: Optional[Mapping[str, Any]]) -> str:
        """open | handled for a row's item (None = no item yet: open)."""
        if not item:
            return "open"
        status = item.get("status")
        if status == "open":
            return "open"
        if status == "snoozed":
            until = item.get("snooze_until")
            try:
                instant = timeutil.due_instant(until, self.tz) if until else None
            except (ValueError, TypeError):
                instant = None
            return "open" if instant is not None and instant <= self.now else "handled"
        return "handled"

    def run(self) -> None:
        state = self.ledger.load_state()
        sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
        info = state.get("deferred_info") if isinstance(state.get("deferred_info"), dict) else {}
        deferred = state.get("deferred") if isinstance(state.get("deferred"), list) else []
        drift = state.get("drift")
        problem = detector.drift_problem(drift)
        self.r.drift = dict(drift, problem=problem) if problem and isinstance(drift, dict) else None

        items = self.ledger.items()
        by_session: Dict[str, Dict[str, Any]] = {}
        for item in items.values():
            sid = item.get("session_uuid")
            if item.get("kind") == "interrupted" and isinstance(sid, str) and sid not in by_session:
                by_session[sid] = item
        stops_raw = self.ledger.stops()
        self.r.sources["hook"] = bool(stops_raw)
        stops = latest_stops(stops_raw)

        limit_deferred: Dict[str, Dict[str, Any]] = {}
        active_deferred: Set[str] = set()
        for uuid in deferred:
            if not isinstance(uuid, str):
                continue
            entry = info.get(uuid) if isinstance(info.get(uuid), dict) else {"why": detector.WHY_ACTIVE}
            if entry.get("why") == detector.WHY_LIMIT:
                limit_deferred[uuid] = entry
            else:
                active_deferred.add(uuid)

        self.active_deferred = set(active_deferred)
        counts = self.r.counts
        active: Set[str] = set()
        projects_checked: Set[str] = set()
        rows: List[Dict[str, Any]] = []

        def item_for(uuid: str, rec: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
            iid = rec.get("item_id")
            if isinstance(iid, str) and iid in items:
                return items[iid]
            return by_session.get(uuid)

        # Sessions the detector deferred as maybe active: counted, unless the platform says the
        # session is blocked on you right now.
        for uuid in sorted(active_deferred):
            rec = sessions.get(uuid) if isinstance(sessions.get(uuid), dict) else {}
            if self.live_class(uuid) == "waiting":
                item = item_for(uuid, rec)
                row = self.blocked_row(uuid, rec, item, deferred_active=True)
                if row is not None and self.wanted(row["project"]):
                    counts["checked"] += 1
                    projects_checked.add(row["project"])
                    if self.blocked_handled(uuid, rec, item):
                        counts["handled"] += 1
                    else:
                        rows.append(row)
                continue
            if self.project is None or self.wanted(self.project_of(uuid, rec)):
                active.add(uuid)

        # Usage limits still waiting for their reset: no item yet, so no number.
        for uuid, entry in sorted(limit_deferred.items()):
            entry = self.limit_entry(uuid, entry, sessions.get(uuid))
            ended = _instant(entry.get("ended_at"))
            project = entry.get("project") if isinstance(entry.get("project"), str) else detector.UNKNOWN_PROJECT
            if not self.in_window(ended) or not self.wanted(project):
                continue
            counts["checked"] += 1
            projects_checked.add(project)
            hook = stops.get(uuid)
            if hook and hook.get("event") == HOOK_STOP_EVENT and hook.get("verdict") == classify.CLEAN \
                    and hook["_at"] > ended + timedelta(seconds=CLEAN_GRACE_SECONDS):
                continue  # it went on after the reset and ended clean
            if self.live_class(uuid) == "active":
                active.add(uuid)
                self.active_checked += 1
                continue
            cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else None
            rows.append(self.make_row(
                uuid=uuid, item=None, verdict=classify.USAGE_LIMIT,
                reason=entry.get("reason") or classify.LIMIT_META, kind=KIND_LIMIT,
                project=project, cwd=cwd, ended=ended, resets_at=entry.get("resets_at"),
                hook=hook, deferred_limit=True))

        for uuid, rec in sorted(sessions.items()):
            if not isinstance(rec, dict) or uuid in limit_deferred or uuid in active_deferred:
                continue
            verdict = rec.get("verdict")
            if verdict == classify.AUTOMATED:
                mtime = rec.get("mtime")
                if isinstance(mtime, (int, float)) and not isinstance(mtime, bool) and self.wanted(rec.get("project")):
                    try:
                        when = datetime.fromtimestamp(float(mtime), self.now.tzinfo)
                    except (OverflowError, OSError, ValueError):
                        when = None
                    if self.in_window(when):
                        counts["automated"] += 1
                continue
            ended = _instant(rec.get("ended_at"))
            if ended is None:
                continue
            reason = rec.get("reason")
            resets_at = rec.get("resets_at")
            hook = stops.get(uuid)
            if hook and hook.get("event") == HOOK_LIMIT_EVENT and hook.get("verdict") == classify.USAGE_LIMIT \
                    and hook["_at"] > ended and verdict != classify.USAGE_LIMIT:
                # The transcript can lag the hook: the hook saw the limit. When the transcript
                # already recorded the limit, its reason and reset time stay (the hook record
                # lands a second or two after the error entry and knows no reset time).
                verdict, reason, ended, resets_at = classify.USAGE_LIMIT, hook.get("reason") or classify.RATE_LIMIT, \
                    hook["_at"], None
            project = rec.get("project") if isinstance(rec.get("project"), str) else detector.UNKNOWN_PROJECT
            if not self.in_window(ended) or not self.wanted(project):
                continue
            counts["checked"] += 1
            projects_checked.add(project)
            live = self.live_class(uuid)
            if live == "active":
                active.add(uuid)
                self.active_checked += 1
                continue
            item = item_for(uuid, rec)
            if live == "waiting":
                if self.blocked_handled(uuid, rec, item):
                    counts["handled"] += 1
                    continue
                row = self.blocked_row(uuid, rec, item)
                if row is not None:
                    rows.append(row)
                continue
            if verdict not in classify.FLAGGED_VERDICTS:
                continue
            if hook and hook.get("event") == HOOK_STOP_EVENT and hook.get("verdict") == classify.CLEAN \
                    and hook["_at"] > ended + timedelta(seconds=CLEAN_GRACE_SECONDS):
                continue  # it went on and ended clean after what the transcript showed
            if self.item_state(item) == "handled":
                counts["handled"] += 1
                continue
            cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
            if verdict == classify.USAGE_LIMIT:
                kind = KIND_LIMIT
            elif reason == classify.ASK_USER:
                kind = KIND_ASK_USER
            elif reason == classify.PLAN_APPROVAL:
                kind = KIND_PLAN
            else:
                kind = KIND_QUESTION
            row = self.make_row(uuid=uuid, item=item, verdict=verdict, reason=reason, kind=kind,
                                project=project, cwd=cwd, ended=ended, resets_at=resets_at, hook=hook,
                                open_in_terminal=live == "open")
            rows.append(row)

        rows.extend(self.due_rows(items))

        counts["active"] = len(active)
        waiting_rows = [r for r in rows if r["tier"] == classify.WAITING]
        soft_rows = [r for r in rows if r["tier"] == classify.SOFT]
        counts["waiting"] = len(waiting_rows)
        counts["soft"] = len(soft_rows)
        session_rows = sum(1 for r in rows if r["kind"] != KIND_DUE)
        counts["clean"] = max(0, counts["checked"] - session_rows - counts["handled"] - self.active_checked)
        busy = {r["project"] for r in rows if r["kind"] != KIND_DUE}
        self.r.quiet_projects = len(projects_checked - busy)
        self.r.projects_checked = len(projects_checked)
        self.r.rows = order(waiting_rows) + order(soft_rows)

    def active_uuids(self) -> List[str]:
        """Every session running now: maybe-active deferrals (unless `claude agents` says it is
        blocked on you) and sessions `claude agents` reports busy; sorted."""
        uuids = {u for u in self.active_deferred if self.live_class(u) != "waiting"}
        for uuid in (self.live or {}):
            if self.live_class(uuid) == "active":
                uuids.add(uuid)
        return sorted(uuids)

    def active_cwds(self) -> List[str]:
        """Launch directories of every session running now (maybe-active deferrals and sessions
        `claude agents` reports busy), whatever the window or --project: the unsaved check skips
        their repos (contract 16.9). A deferral `claude agents` reports blocked is waiting on you,
        not working: its repo is still checked."""
        sessions = (self.ledger.load_state() or {}).get("sessions") or {}
        out: List[str] = []
        for uuid in self.active_uuids():
            rec = sessions.get(uuid) if isinstance(sessions.get(uuid), dict) else {}
            cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
            if cwd is None:
                summary = self.r.summary(uuid)
                cwd = getattr(summary, "launch_cwd", None) if summary is not None else None
            if isinstance(cwd, str) and cwd:
                out.append(cwd)
        return out

    def project_of(self, uuid: str, rec: Mapping[str, Any]) -> Optional[str]:
        """The stored project, else (a maybe-active session the detector never processed) the
        project of the transcript's launch directory, as blocked_row works it out."""
        project = rec.get("project")
        if isinstance(project, str):
            return project
        summary = self.r.summary(uuid)
        if summary is None:
            return None
        return detector.project_for(summary.launch_cwd, self.cfg)

    def limit_entry(self, uuid: str, entry: Mapping[str, Any], rec: Any) -> Mapping[str, Any]:
        """A limit deferral with what a row needs. A v1 deferral (written before contract 16.3)
        has no ``ended_at`` / ``project`` / ``cwd``: take them from the session's stored record,
        else from the transcript head (ended_at as the detector works it out), else the file's
        mtime, so the limit is still listed while it waits for its reset."""
        if _instant(entry.get("ended_at")) is not None:
            return entry
        out = dict(entry)
        rec = rec if isinstance(rec, dict) else {}
        for key in ("cwd", "project", "ended_at", "reason", "resets_at"):
            if out.get(key) is None and rec.get(key) is not None:
                out[key] = rec[key]
        if _instant(out.get("ended_at")) is None or not isinstance(out.get("cwd"), str):
            summary = self.r.summary(uuid)
            if summary is not None:
                if not isinstance(out.get("cwd"), str):
                    out["cwd"] = summary.launch_cwd
                if _instant(out.get("ended_at")) is None:
                    out["ended_at"] = timeutil.to_iso(detector.ended_at(summary, self.now))
                if out.get("resets_at") is None:
                    out["resets_at"] = detector.resets_iso(getattr(summary, "final_resets_at", None))
        if _instant(out.get("ended_at")) is None:
            mtime = out.get("mtime")
            if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
                try:
                    out["ended_at"] = timeutil.to_iso(datetime.fromtimestamp(float(mtime), self.now.tzinfo))
                except (OverflowError, OSError, ValueError):
                    pass
        if not isinstance(out.get("project"), str) and isinstance(out.get("cwd"), str):
            out["project"] = detector.project_for(out["cwd"], self.cfg)
        return out

    def blocked_handled(self, uuid: str, rec: Mapping[str, Any], item: Optional[Mapping[str, Any]]) -> bool:
        """A blocked session's item the user handled (done, dismissed, or snoozed and not yet
        due): left out and counted as handled, like any other row (contract 16.3 Rows). Unless
        the session file changed after that act: then it went on and is blocked on something new."""
        if self.item_state(item) != "handled":
            return False
        acted = _instant(item.get("acted_at")) or _instant(item.get("closed_at")) or _instant(item.get("updated_at"))
        if acted is None:
            return True
        mtime = None
        path = self.r.path_of(uuid)
        if path is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = None
        if mtime is None:
            stored = rec.get("mtime")
            mtime = float(stored) if isinstance(stored, (int, float)) and not isinstance(stored, bool) else None
        return mtime is None or mtime <= acted.timestamp() + BLOCKED_ACT_SLACK

    # -- rows ----------------------------------------------------------------------------------------

    def blocked_row(self, uuid: str, rec: Mapping[str, Any], item: Optional[Mapping[str, Any]],
                    deferred_active: bool = False) -> Optional[Dict[str, Any]]:
        """A session the platform says is blocked on you (tier waiting, whatever the transcript
        said). A session the detector deferred has no stored facts: its head is read for them."""
        cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
        project = rec.get("project") if isinstance(rec.get("project"), str) else None
        ended = _instant(rec.get("ended_at"))
        verdict, reason = rec.get("verdict"), rec.get("reason")
        if deferred_active or cwd is None:
            summary = self.r.summary(uuid)
            if summary is not None:
                cwd = cwd or summary.launch_cwd
                ended = detector.ended_at(summary, self.now)
                if deferred_active:
                    verdict, reason = classify.classify(summary, getattr(self.cfg, "ignore_tail_patterns", ()),
                                                        getattr(self.cfg, "handoff_patterns", None))
        if project is None:
            project = detector.project_for(cwd, self.cfg)
        return self.make_row(uuid=uuid, item=item, verdict=verdict, reason=REASON_BLOCKED, kind=KIND_BLOCKED,
                             project=project, cwd=cwd, ended=ended or self.now, resets_at=None, hook=None,
                             quote_reason=reason)

    def make_row(self, *, uuid: str, item: Optional[Mapping[str, Any]], verdict: Any, reason: Any, kind: str,
                 project: str, cwd: Optional[str], ended: datetime, resets_at: Any, hook: Optional[Mapping[str, Any]],
                 open_in_terminal: bool = False, deferred_limit: bool = False,
                 quote_reason: Any = None) -> Dict[str, Any]:
        facts = self.git(cwd)
        if kind in (KIND_LIMIT, KIND_BLOCKED, KIND_ASK_USER, KIND_PLAN):
            tier = classify.WAITING
        else:
            tier = classify.tier(verdict, reason, self.direct, unsaved=evidence.unsaved(facts)) or classify.WAITING
            if tier == classify.SOFT:
                kind = KIND_SOFT
        age_days, _ = _age(ended, self.now, self.tz)
        resume = item.get("resume") if item and isinstance(item.get("resume"), str) else detector.resume_command(cwd, uuid)
        source = "transcript"
        if hook is not None and hook["_at"] >= ended - timedelta(seconds=HOOK_SOURCE_SLACK):
            source = "hook"
        n = None
        item_id = None
        if item:
            item_id = item.get("id")
            n = ledger_mod.id_number(item_id)
        row: Dict[str, Any] = {
            "n": n, "id": item_id, "kind": kind, "tier": tier, "reason": reason, "project": project,
            "session_uuid": uuid, "ended_at": timeutil.to_iso(ended), "age_days": age_days,
        }
        row.update(_git_row_fields(facts, cwd))
        row.update({
            "resets_at": resets_at if isinstance(resets_at, str) else None,
            "resume": resume, "open_in_terminal": bool(open_in_terminal), "source": source,
            "due_at": None, "title": None,
            "_key": uuid, "_verdict": verdict, "_quote_reason": quote_reason if quote_reason is not None else reason,
            "_ended": ended, "_deferred_limit": deferred_limit,
        })
        return row

    def due_rows(self, items: Mapping[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        today = timeutil.local_date(self.now, self.tz)
        horizon = today + timedelta(days=DUE_AHEAD_DAYS)
        out: List[Dict[str, Any]] = []
        for item in items.values():
            if item.get("kind") not in ("promise", "decision") or not self.wanted(item.get("project")):
                continue
            if self.item_state(item) != "open" or item.get("status") not in ("open", "snoozed"):
                continue
            due = item.get("due_at")
            day = None
            if isinstance(due, str) and due:
                try:
                    day = timeutil.due_date(due)
                except (ValueError, TypeError):
                    day = None
            if day is None or day > horizon:
                self.r.counts["also_open"] += 1
                continue
            created = _instant(item.get("created_at")) or self.now
            out.append({
                "n": ledger_mod.id_number(item.get("id")),
                "id": item.get("id"), "kind": KIND_DUE, "tier": classify.WAITING, "reason": None,
                "project": item.get("project") or detector.UNKNOWN_PROJECT, "session_uuid": None,
                "ended_at": None, "age_days": None, "uncommitted": None, "unpushed": None,
                "branch_no_upstream": False, "not_git": False, "resets_at": None, "resume": None,
                "open_in_terminal": False, "source": "ghosted add", "due_at": due,
                "title": one_line(item.get("local_title") or item.get("title") or ""),
                "_key": item.get("id"), "_verdict": None, "_quote_reason": None, "_ended": created,
                "_created": created, "_due_day": day,
            })
        return out


def order(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Projects by their newest session row (projects with only due rows after them, soonest due
    first); inside a project, sessions newest first, then its due rows by due date."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["project"], []).append(row)

    def sessions_of(name: str) -> List[Dict[str, Any]]:
        return [r for r in groups[name] if r["kind"] != KIND_DUE]

    def due_of(name: str) -> List[Dict[str, Any]]:
        return sorted((r for r in groups[name] if r["kind"] == KIND_DUE),
                      key=lambda r: (r["_due_day"], str(r.get("due_at")), r.get("n") or 0))

    def key(name: str) -> Tuple[Any, ...]:
        sess = sessions_of(name)
        if sess:
            newest = max(r["_ended"] for r in sess)
            return (0, -newest.timestamp(), name.lower())
        first = min(r["_due_day"] for r in groups[name])
        return (1, first.toordinal(), name.lower())

    out: List[Dict[str, Any]] = []
    for name in sorted(groups, key=key):
        sess = sorted(sessions_of(name), key=lambda r: (-r["_ended"].timestamp(), r["session_uuid"] or ""))
        out.extend(sess)
        out.extend(due_of(name))
    return out


def gather(cfg: Any, ledger: Any, now: Any = None, window: Optional[str] = None, *, scan: bool = True,
           live: Any = _AUTO, project: Optional[str] = None, via: str = "cli", record: bool = True,
           live_timeout: float = 3.0, unsaved: bool = True) -> Report:
    """Collect one report (see the module doc). ``window`` None = ``7d``, or ``30d`` when no report
    was ever run. ``live`` = a ``{sessionId: LiveState}`` dict, None (no live signal), or omitted
    to ask ``claude agents`` (``core.agents.live``). Appends the measure record unless ``record``
    is False."""
    when = timeutil.now_utc() if now is None else timeutil.parse_iso(now)
    report = Report()
    report.cfg = cfg
    report.generated_at = when
    report.tz = getattr(cfg, "timezone", None) or "UTC"
    report.via = via if via in VIA else "cli"
    report.project = project.strip() if isinstance(project, str) and project.strip() else None
    report.first_run = is_first_report(ledger)
    name = window if window is not None else (FIRST_WINDOW if report.first_run else DEFAULT_WINDOW)
    report.window_name, report.days = parse_window(name)
    offer_older = report.first_run and window is None
    mtimes = session_mtimes(cfg) if (report.window_name == ALL_WINDOW or offer_older) else []
    if report.window_name == ALL_WINDOW:
        report.days = days_back_to(min(mtimes), when, report.tz) if mtimes else 1
    report.start = window_start(report.days, when, report.tz)
    report.end = when
    older = [m for m in mtimes if m < report.start.timestamp()] if offer_older else []
    if older:
        report.older = {"sessions": len(older),
                        "since": timeutil.to_iso(datetime.fromtimestamp(min(older), timezone.utc))}

    if not os.path.isdir(str(cfg.projects_dir)):
        # Not the same as an empty history: say where it looked, so a moved folder is not an all-clear.
        home = os.path.expanduser("~")
        shown = str(cfg.projects_dir)
        report.history_missing = "~" + shown[len(home):] if shown.startswith(home + os.sep) else shown

    if scan:
        from core import runner

        with runner.run_lock(cfg) as held:
            if held:
                result = detector.scan(cfg, ledger, when, since_floor=report.start)
                report.scan = "ok"
                report.scan_errors = len(getattr(result, "errors", None) or ())
            else:
                report.scan = "busy"
    else:
        report.scan = "skipped"

    if live is _AUTO:
        from core import agents

        live = agents.live(timeout=live_timeout)
    report.sources["agents"] = live is not None
    gathered = _Gather(cfg, ledger, when, report, live if isinstance(live, dict) else None, project)
    gathered.run()
    # The touch index (contract 16.11): brought up to date with the scan, else as last saved.
    index = touches.update(cfg, when) if scan else touches.load(cfg)
    report.shared_now = shared_now(cfg, when, gathered.active_uuids(), index, report.project)
    if unsaved:
        unsaved_section(cfg, ledger, when, report, gathered.active_cwds(), index)

    if record:
        ledger.append_measure(measure_record(report))
    return report


def _shown(item: Mapping[str, Any], now: datetime, tz: str) -> bool:
    status = item.get("status")
    if status == "open":
        return True
    if status == "snoozed":
        until = item.get("snooze_until")
        try:
            instant = timeutil.due_instant(until, tz) if until else None
        except (ValueError, TypeError):
            instant = None
        return instant is not None and instant <= now
    return False


def shared_now(cfg: Any, now: datetime, active: List[str], index: Any,
               project: Optional[str] = None) -> List[Dict[str, Any]]:
    """Repos two or more active sessions are working in right now (contract 16.11 part B):
    ``[{"project", "repo", "sessions"}]``. Working in = an edit-tool touch there in the last
    ``maybe_active_minutes``, or a touch there that lines up (within the attribution window) with
    an uncommitted file's mtime. Only looking around (ls, grep) never counts."""
    from core import unsaved as unsaved_mod

    floor = now.timestamp() - max(0, int(getattr(cfg, "maybe_active_minutes", 30) or 0)) * 60
    window = int(getattr(cfg, "mixed_window_seconds", touches.DEFAULT_WINDOW))
    wanted = project.lower() if isinstance(project, str) and project else None
    per_repo: Dict[str, Dict[str, Tuple[bool, List[int]]]] = {}
    for uuid in active:
        rec = index.sessions.get(uuid) if index is not None else None
        if not isinstance(rec, dict) or rec.get("auto") or not isinstance(rec.get("repos"), dict):
            continue
        edits = rec.get("edits") if isinstance(rec.get("edits"), dict) else {}
        for repo, stamps in rec["repos"].items():
            recent = [t for t in (stamps or ()) if isinstance(t, int) and t >= floor]
            if not recent:
                continue
            edited = any(isinstance(t, int) and t >= floor for t in (edits.get(repo) or ()))
            per_repo.setdefault(repo, {})[uuid] = (edited, recent)
    out: List[Dict[str, Any]] = []
    for repo in sorted(per_repo):
        who = per_repo[repo]
        name = os.path.basename(repo.rstrip(os.sep)) or repo
        if len(who) < 2 or (wanted is not None and name.lower() != wanted):
            continue
        working = {u for u, (edited, _) in who.items() if edited}
        rest = [u for u in who if u not in working]
        if rest:
            facts = unsaved_mod.repo_facts(repo, now)
            mtimes = [m for m in (facts.get("mtimes") or ()) if m is not None] if not facts.get("error") else []
            for uuid in rest:
                stamps = who[uuid][1]
                if any(touches.owner_of(m, {uuid: stamps}, window) == uuid for m in mtimes):
                    working.add(uuid)
        if len(working) >= 2:
            out.append({"project": name, "repo": repo, "sessions": len(working)})
    return out


def launch_cwd(ledger: Any, report: Report, uuid: str, sessions: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Where ``claude --resume`` must run for ``uuid``: the stored session's cwd, else the
    transcript head's."""
    if sessions is None:
        sessions = (ledger.load_state() or {}).get("sessions") or {}
    rec = sessions.get(uuid) if isinstance(sessions, Mapping) else None
    cwd = rec.get("cwd") if isinstance(rec, dict) else None
    if isinstance(cwd, str) and cwd:
        return cwd
    summary = report.summary(uuid)
    cwd = getattr(summary, "launch_cwd", None) if summary is not None else None
    return cwd if isinstance(cwd, str) and cwd else None


def unsaved_section(cfg: Any, ledger: Any, now: datetime, report: Report, active_cwds: List[str],
                    index: Any = None) -> None:
    """Check the workspace repos (plus, with a touch index, the repos sessions worked in), keep
    their unsaved-work items in step, and fill ``report.unsaved`` (contracts 16.9, 16.11). A repo
    that is active or that git could not read keeps its item as it is."""
    from core import unsaved as unsaved_mod

    wanted = report.project.lower() if report.project else None
    rows: List[Dict[str, Any]] = []
    notes: List[Tuple[str, str]] = []
    sessions: Optional[Mapping[str, Any]] = None
    for check in unsaved_mod.check(cfg, now, active_cwds, index=index):
        if wanted is not None and check.project.lower() != wanted:
            continue
        notes.extend((check.project, n) for n in check.notes)
        facts = check.facts
        if facts is None or facts.get("error"):
            continue
        repo = str(check.path)
        if not check.flags:
            ledger.close_unsaved(repo, now)
            continue
        item, _ = ledger.upsert_unsaved(repo, check.project, unsaved_mod.summary_of(check), now)
        if not _shown(item, now, report.tz):
            continue
        kept = item.get("unsaved") or {}
        flags = kept.get("flags") or {}
        owners = kept.get("owners") if flags.get("mixed") and isinstance(kept.get("owners"), dict) else None
        uncommitted = flags.get("uncommitted") or (kept.get("dirty") if owners else None)
        resumes: List[str] = []
        if owners:
            if sessions is None:
                sessions = (ledger.load_state() or {}).get("sessions") or {}
            for uuid, _ in touches.ranked(owners.get("sessions") or {}):
                resumes.append(detector.resume_command(launch_cwd(ledger, report, uuid, sessions), uuid))
        rows.append({
            "n": ledger_mod.id_number(item.get("id")), "id": item.get("id"), "project": check.project,
            "repo": repo, "uncommitted": uncommitted,
            "uncommitted_since": kept.get("uncommitted_since"), "unpushed": flags.get("unpushed"),
            "unpushed_since": kept.get("unpushed_since"), "unmerged": flags.get("unmerged"),
            "no_remote": bool(flags.get("no_remote")),
            "mixed": copy.deepcopy(owners) if owners else None,
            "_cmd": detector.cd_command(repo) + " && git status",
            "_resumes": resumes,
        })
    report.unsaved = rows
    report.unsaved_notes = notes
    report.counts["unsaved"] = len(rows)


def measure_record(report: Report) -> Dict[str, Any]:
    """The measure.jsonl record: ids and counts only (item id, else the session id)."""
    def ids(rows: List[Dict[str, Any]]) -> List[str]:
        return [str(r.get("id") or r.get("session_uuid")) for r in rows if r.get("id") or r.get("session_uuid")]

    return {"type": MEASURE_REPORT, "at": timeutil.to_iso(report.generated_at), "window": report.window_name,
            "via": report.via, "waiting": ids(report.waiting), "soft": ids(report.soft),
            "checked": report.counts["checked"], "unsaved": [str(r["id"]) for r in report.unsaved]}


# ---- text ---------------------------------------------------------------------------------------

def _rule(name: str) -> str:
    name = one_line(name) or detector.UNKNOWN_PROJECT
    return "%s %s" % (name, "─" * max(4, RULE_WIDTH - len(name) - 1))


def _window_phrase(report: Report) -> str:
    if report.window_name == "today":
        return "today"
    if report.window_name == ALL_WINDOW:
        return "in your whole history"
    return "in the last %s" % ("day" if report.days == 1 else "%d days" % report.days)


def _how(row: Mapping[str, Any], report: Report, plain: bool) -> str:
    kind = row["kind"]
    if kind == KIND_LIMIT:
        return "%s cut it off mid-task" % ("Claude's usage limit" if plain else "usage limit")
    if kind == KIND_ASK_USER:
        return "asked you in a question box"
    if kind == KIND_PLAN:
        return "waiting for you to approve a plan"
    if kind == KIND_BLOCKED:
        return "background session is blocked on you"
    if row.get("reason") == classify.SAVE_OFFER and row["tier"] == classify.WAITING:
        verb = report.save_verb(row) or "save the work"
        if plain:
            return "offered to %s; your work still isn't saved to GitHub" % verb
        return "offered to %s; the work still isn't saved" % verb
    if kind == KIND_SOFT:
        return "ended on a polite offer"
    return "ended on a question"


def _cost(row: Mapping[str, Any], report: Report, plain: bool) -> str:
    parts: List[str] = []
    now = report.generated_at
    if row.get("not_git"):
        parts.append("not tracked by git" if plain else "not a git repo")
    elif row.get("uncommitted") is not None:
        dirty = row["uncommitted"]
        if plain:
            parts.append("%s saved on your computer but not saved to GitHub yet" % _plural(dirty, "change")
                         if dirty else "no unsaved changes")
        else:
            parts.append("%d uncommitted" % dirty)
        unpushed = row.get("unpushed")
        if unpushed:
            parts.append("%d saved but not sent to GitHub yet" % unpushed if plain else "%d unpushed" % unpushed)
        if row.get("branch_no_upstream"):
            parts.append("this branch isn't on GitHub yet" if plain else "no upstream")
    if row["kind"] == KIND_LIMIT:
        resets = _instant(row.get("resets_at"))
        what = "Claude's usage limit" if plain else "limit"
        if row.get("_deferred_limit") and resets is not None and resets > now:
            parts.append("can't resume until %s" % _time_label(resets, now, report.tz))
        elif resets is not None and resets > now:
            parts.append("%s %s %s" % (what, "resets" if plain else "reset", _time_label(resets, now, report.tz)))
        elif resets is not None:
            parts.append("%s has reset" % what)
    if row.get("open_in_terminal"):
        parts.append("still open in a terminal")
    return " · ".join(one_line(p) for p in parts)


def _due_label(row: Mapping[str, Any], report: Report) -> str:
    now = report.generated_at
    tz = report.tz
    today = timeutil.local_date(now, tz)
    day = row["_due_day"]
    due = row.get("due_at") or ""
    timed = timeutil.is_timed(due)
    if day < today:
        return "overdue since %s" % day_label(day, today)
    if day == today:
        if timed:
            try:
                instant = timeutil.due_instant(due, tz)
            except (ValueError, TypeError):
                instant = None
            if instant is not None and instant < now:
                return "overdue since %s today" % timeutil.human_time(instant, tz)
            if instant is not None:
                return "due TODAY, %s" % timeutil.human_time(instant, tz)
        return "due TODAY"
    label = "due tomorrow" if day == today + timedelta(days=1) else "due %s" % day_label(day, today)
    if timed:
        try:
            label += ", %s" % timeutil.human_time(timeutil.due_instant(due, tz), tz)
        except (ValueError, TypeError):
            pass
    return label


def _row_lines(row: Mapping[str, Any], report: Report, width: int, quotes: Optional[Dict[str, Optional[str]]],
               plain: bool) -> List[str]:
    n = row.get("n")
    num = ("%*d" % (width, n)) if isinstance(n, int) else " " * width
    glyph = GLYPHS.get(row["kind"], "?")
    lead = "  %s " % num if plain else "  %s %s " % (num, glyph)
    indent = " " * len(lead)
    tz = report.tz
    now_day = timeutil.local_date(report.generated_at, tz)
    if row["kind"] == KIND_DUE:
        created = row.get("_created")
        added = day_label(timeutil.local_date(created, tz), now_day) if created else ""
        head = "%s%s · \"%s\"   (ghosted add%s)" % (lead, _due_label(row, report), one_line(row.get("title")),
                                             ", " + added if added else "")
        return [head]
    ended = row["_ended"]
    _, age = _age(ended, report.generated_at, tz)
    head = "%s%s · %s · %s" % (lead, day_label(timeutil.local_date(ended, tz), now_day), age,
                               _how(row, report, plain))
    lines = [head]
    if quotes is not None:
        text = quotes.get(row["_key"])
        if text:
            lines.append('%s"%s"' % (indent, one_line(text)))
    cost = _cost(row, report, plain)
    if cost:
        lines.append(indent + cost)
    resume = one_line(row.get("resume"))
    if resume:
        lines.append(indent + ("To pick it back up, paste: " if plain else "→ ") + resume)
    return lines


def _block(rows: List[Dict[str, Any]], report: Report, width: int, quotes: Optional[Dict[str, Optional[str]]],
           plain: bool) -> List[str]:
    lines: List[str] = []
    current = None
    for row in rows:
        if row["project"] != current:
            if lines:
                lines.append("")
            lines.append(_rule(row["project"]))
            current = row["project"]
        lines.extend(_row_lines(row, report, width, quotes, plain))
    return lines


def _command(report: Report, rest: str) -> str:
    """``ghosted report 30d --soft`` (or ``/ghosted 30d --soft`` from the skill)."""
    parts = ["/ghosted" if report.via == "skill" else "ghosted report"]
    if report.window_name != DEFAULT_WINDOW:
        parts.append(report.window_name)
    if report.project:
        parts.append("--project " + one_line(report.project))
    parts.append(rest)
    return " ".join(parts)


def header_notes(report: Report) -> List[str]:
    c = report.counts
    notes = []
    if c["active"]:
        notes.append("%d active now" % c["active"])
    if c["handled"]:
        notes.append("%d handled" % c["handled"])
    return notes


def _short_day(iso: Any, report: Report) -> Optional[Tuple[str, int]]:
    """("31 Jul", age in local days) for an ISO instant, or None."""
    when = _instant(iso)
    if when is None:
        return None
    day = timeutil.local_date(when, report.tz)
    today = timeutil.local_date(report.generated_at, report.tz)
    return "%d %s" % (day.day, _MONTH_ABBR[day.month - 1]), max(0, (today - day).days)


def unsaved_parts(row: Mapping[str, Any], report: Report, plain: bool = False) -> List[str]:
    """The facts of one Unsaved work row: counts and dates only (contract 16.9)."""
    parts: List[str] = []
    n = row.get("uncommitted")
    if n:
        when = _short_day(row.get("uncommitted_since"), report)
        since = " since %s (%dd)" % when if when else ""
        parts.append(("%s not saved in git%s" % (_plural(n, "change"), since)) if plain
                     else "%d uncommitted%s" % (n, since))
    mixed = row.get("mixed")
    if isinstance(mixed, Mapping) and mixed.get("sessions"):
        owned = [n for _, n in touches.ranked(mixed["sessions"])]
        if plain:
            parts.append("changes from %d different Claude sessions are mixed here; save each one's part "
                         "separately" % len(owned))
        else:
            other = int(mixed.get("ambiguous") or 0) + int(mixed.get("none") or 0)
            split = " + ".join(str(n) for n in owned) + (", %d other" % other if other else "")
            parts.append("from %d sessions (%s)" % (len(owned), split))
    n = row.get("unpushed")
    if n:
        when = _short_day(row.get("unpushed_since"), report)
        days = " (%dd)" % when[1] if when else ""
        parts.append(("%s not on GitHub yet%s" % (_plural(n, "commit"), days)) if plain
                     else "%d unpushed%s" % (n, days))
    n = row.get("unmerged")
    if n:
        parts.append(("%s never merged" % _plural(n, "branch", "branches")) if plain
                     else "%s not merged" % _plural(n, "branch", "branches"))
    if row.get("no_remote"):
        parts.append("no copy anywhere but this computer (no remote)" if plain else "no remote")
    return parts


def _unsaved_block(report: Report, width: int, plain: bool, debug: bool) -> List[str]:
    rows = report.unsaved
    notes = report.unsaved_notes if debug else []
    if not rows and not notes:
        return []
    lines = [_rule("Unsaved work: %s" % _plural(len(rows), "repo"))]
    for row in rows:
        n = row.get("n")
        num = ("%*d" % (width, n)) if isinstance(n, int) else " " * width
        lead = "  %s " % num if plain else "  %s ! " % num
        indent = " " * len(lead)
        lines.append("%s%s · %s" % (lead, one_line(row["project"]), " · ".join(unsaved_parts(row, report, plain))))
        lines.append(indent + ("To check it, paste: " if plain else "→ ") + one_line(row["_cmd"]))
        resumes = row.get("_resumes") or []
        if row.get("mixed") and isinstance(n, int):
            cmds = ["ghosted mixed %d" % n] + [one_line(r) for r in resumes[:MIXED_RESUMES]]
            if len(resumes) > MIXED_RESUMES:
                cmds.append("+%d more" % (len(resumes) - MIXED_RESUMES))
            lead_text = "To split them, paste: " if plain else "⚠ split before committing: "
            lines.append(indent + lead_text + " · ".join(cmds))
    for project, note in notes:
        lines.append("  (%s: %s)" % (one_line(project), note))
    lines.append("")
    return lines


def render_text(report: Report, quotes: bool = True, soft: bool = False, plain: bool = False,
                debug: bool = False) -> str:
    """The report for a terminal (contract 16.3; Unsaved work 16.9). Every line is one line."""
    c = report.counts
    tz = report.tz
    start_day = timeutil.local_date(report.start, tz)
    end_day = timeutil.local_date(report.end, tz)
    label = range_label(start_day, end_day)
    waiting = report.waiting
    soft_rows = report.soft
    shown = waiting + (soft_rows if soft else [])
    quote_map = report.load_quotes(shown) if quotes else None
    numbers = [r["n"] for r in shown + report.unsaved if isinstance(r.get("n"), int)]
    width = max([len(str(n)) for n in numbers] + [1])
    notes = header_notes(report)
    checked = _plural(c["checked"], "session") + " checked"
    project = " · %s" % one_line(report.project) if report.project else ""

    # An all-clear is only said when sessions were really checked and every one could be read.
    # Sessions and reminders are counted apart (16.3): a reminder someone logged with `ghosted add`
    # is not a session you left hanging.
    reminders = [r for r in waiting if r["kind"] == KIND_DUE]
    ghosted = [r for r in waiting if r["kind"] != KIND_DUE]
    all_clear = not ghosted and _can_vouch(report)

    lines: List[str] = []
    due = " · %s due" % _plural(len(reminders), "reminder") if reminders else ""
    if ghosted:
        counts = "(%s, %d clean%s)" % (checked, c["clean"], "".join(" · " + n for n in notes))
        lines.append("GHOSTED · %s%s · %d waiting on you%s   %s" % (label, project, len(ghosted), due, counts))
    else:
        lines.append("GHOSTED · %s%s%s" % (label, project, due))
    if report.drift and report.drift.get("problem"):
        lines.append("⚠ Claude Code's session format may have changed: %s. Results may be incomplete "
                     "(ghosted doctor)." % one_line(report.drift["problem"]))
    if report.history_missing:
        lines.append("⚠ No Claude Code history at %s. If yours is somewhere else, set projects_dir in "
                     "config.toml (ghosted doctor shows where)." % one_line(report.history_missing))
    if report.scan == "busy":
        lines.append("Another scan is running; this report uses what the last scan saved.")
    if report.scan_errors:
        lines.append("⚠ %s could not be read this time; the next report tries again."
                     % _plural(report.scan_errors, "session file"))
    for shared in report.shared_now:
        name = one_line(shared["project"])
        cmd = "git worktree add %s" % shlex.quote("../%s-2" % name)
        if plain:
            lines.append("%d Claude sessions are changing files in %s right now. To give one its own folder, "
                         "paste: %s" % (shared["sessions"], name, cmd))
        else:
            lines.append("⚠ %d sessions are working in %s right now. Give one its own folder: %s"
                         % (shared["sessions"], name, cmd))
    lines.append("")
    if not ghosted:
        extra = "".join(" · " + n for n in notes)
        if all_clear:
            # The all-clear (16.3): what was checked and what was looked for, so "nothing" means something.
            lines.append("✓ You didn't ghost anything %s." % _window_phrase(report))
            lines.append("%s in %s checked%s" % (_plural(c["checked"], "session"),
                                                 _plural(report.projects_checked, "project"), extra))
            lines.append("Looked for: %s." % ALL_CLEAR_LOOKED_FOR)
        else:
            lines.append("%s waiting on you %s (%s%s)." % ("No session is" if reminders else "Nothing",
                                                          _window_phrase(report), checked, extra))
        if reminders:
            lines.append("")
    if waiting:
        # A long window: last month first, the older rows under their own heading (16.15).
        long_window = report.days > OLDER_DAYS
        old_rows = [r for r in waiting if long_window and isinstance(r.get("age_days"), int)
                    and r["age_days"] > OLDER_DAYS]
        recent = [r for r in waiting if r not in old_rows]
        lines.extend(_block(recent, report, width, quote_map, plain))
        if old_rows:
            if recent:
                lines.append("")
            lines.append("Older than %d days: %d" % (OLDER_DAYS, len(old_rows)))
            lines.extend(_block(old_rows, report, width, quote_map, plain))
    lines.append("")
    lines.extend(_unsaved_block(report, width, plain, debug))
    if soft and soft_rows:
        lines.append("Soft (ended on a polite offer, probably fine): %d" % len(soft_rows))
        lines.extend(_block(soft_rows, report, width, quote_map, plain))
        lines.append("")
    elif soft_rows:
        lines.append("Soft (ended on a polite offer, probably fine): %d  → %s"
                     % (len(soft_rows), _command(report, "--soft")))
    if report.quiet_projects and not all_clear:         # the all-clear already names the projects
        lines.append("Quiet: %s, nothing hanging." % _plural(report.quiet_projects, "project"))
    if c["also_open"]:
        lines.append("Also open: %s with no date soon (ghosted list)." % _plural(c["also_open"], "item"))
    first = next((r["n"] for r in shown + report.unsaved if isinstance(r.get("n"), int)), None)
    if first is not None:
        if report.via == "skill":
            lines.append("Done with one? say: done %d · snooze %d 3d · dismiss %d" % (first, first, first))
        else:
            lines.append("Done with one? ghosted done %d · ghosted snooze %d 3d · ghosted dismiss %d" % (first, first, first))
    if report.first_run:
        lines.append("Your numbers only, safe to post: %s receipt"
                     % ("/ghosted" if report.via == "skill" else "ghosted"))
    if report.first_run and report.window_name == FIRST_WINDOW:
        cmd = "/ghosted" if report.via == "skill" else "ghosted report"
        lines.append("First run: this looked back 30 days. From now on %s shows the last 7 (%s today "
                     "for just today)." % (cmd, cmd))
        if report.older:
            since = _instant(report.older.get("since"))
            day = timeutil.local_date(since, tz) if since else None
            n = report.older["sessions"]
            lines.append("%d older %s back to %s: %s all reviews everything."
                         % (n, "session goes" if n == 1 else "sessions go",
                            "%d %s %d" % (day.day, _MONTH_ABBR[day.month - 1], day.year) if day else "earlier",
                            cmd))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(flat(line) for line in lines) + "\n"


def _can_vouch(report: Report) -> bool:
    """Whether a tick may be shown when nothing is waiting: this run scanned (not another scan's
    saved state, not --no-scan), sessions were checked, and every one could be read (16.3)."""
    return (report.scan == "ok" and report.counts["checked"] > 0 and not report.scan_errors
            and not (report.drift and report.drift.get("problem")))


def _span(report: Report) -> str:
    """How far back the report looked, in round words: ``today``, ``9 days``, ``11 weeks``."""
    if report.window_name == "today":
        return "today"
    days = max(1, report.days)
    if days < 14:
        return _plural(days, "day")
    return _plural(int(round(days / 7.0)), "week")


def render_receipt(report: Report) -> str:
    """The numbers of one report and nothing else, made to be posted (contract 16.20): no project
    name, no session id, no quote, no title, no path. Reminders are left out; soft rows too."""
    c = report.counts
    ghosted = [r for r in report.waiting if r["kind"] != KIND_DUE]
    start_day = timeutil.local_date(report.start, report.tz)
    end_day = timeutil.local_date(report.end, report.tz)
    span = _span(report)
    head = "GHOSTED · today in Claude Code" if span == "today" else "GHOSTED · %s of Claude Code" % span
    if start_day != end_day:
        head += " (%s)" % range_label(start_day, end_day)
    lines = [head, "%s in %s checked" % (_plural(c["checked"], "session"),
                                         _plural(report.projects_checked, "project"))]
    if ghosted:
        ages = [r["age_days"] for r in ghosted if isinstance(r.get("age_days"), int)]
        longest = " · longest wait %s" % _plural(max(ages), "day") if ages and max(ages) > 0 else ""
        lines.append("%d ghosted%s" % (len(ghosted), longest))
    elif _can_vouch(report):
        lines.append("0 ghosted ✓")
    else:
        lines.append("0 ghosted")
    files = sum(r["uncommitted"] for r in report.unsaved if isinstance(r.get("uncommitted"), int))
    commits = sum(r["unpushed"] for r in report.unsaved if isinstance(r.get("unpushed"), int))
    unsaved = []
    if files:
        unsaved.append("%s left uncommitted" % _plural(files, "file"))
    if commits:
        unsaved.append("%s left unpushed" % _plural(commits, "commit"))
    if unsaved:
        lines.append(" · ".join(unsaved))
    lines.append(RECEIPT_ADDRESS)
    return "\n".join(flat(line) for line in lines) + "\n"


def flat(text: str) -> str:
    """``text`` with every line break, control or format character replaced by a space; other
    spacing (indentation, column gaps) kept. The last guard of the one-line rule."""
    return "".join(" " if (unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp"))
                   else ch for ch in text).rstrip()


# ---- colour (contract 16.16) ----------------------------------------------------------------------

_RESET, _BOLD, _DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
_GREEN, _YELLOW, _BOLD_YELLOW, _BOLD_CYAN = "\x1b[32m", "\x1b[33m", "\x1b[1;33m", "\x1b[1;36m"
_GLYPH_COLORS = {"?": _BOLD_YELLOW, "⏸": "\x1b[1;31m", "▤": "\x1b[1;35m", "●": "\x1b[1;34m",
                 "◷": _BOLD_YELLOW, "!": _BOLD_YELLOW, "·": _DIM}
_HEADER_RE = re.compile(r"^(GHOSTED)(.*?)(\d+ waiting on you(?: · \d+ reminders? due)?|\d+ reminders? due)?"
                        r"(\s+\(.*\))?$")
_RULE_RE = re.compile(r"^(\S.*?) (─{4,})$")
_ROW_RE = re.compile(r"^(\s+\d+ )(\S)( .*)$")


def use_color(environ: Mapping[str, str], tty: bool) -> bool:
    """Colour for a terminal only. ``NO_COLOR`` (non-empty) always wins; ``FORCE_COLOR`` (anything
    but empty or 0) turns it on for a pipe; ``TERM=dumb`` turns it off."""
    if environ.get("NO_COLOR"):
        return False
    if environ.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    return bool(tty) and environ.get("TERM") != "dumb"


def _color_line(line: str) -> str:
    if not line.strip():
        return line
    m = _HEADER_RE.match(line)
    if m:
        out = _BOLD + m.group(1) + _RESET + m.group(2)
        if m.group(3):
            out += _BOLD_YELLOW + m.group(3) + _RESET
        if m.group(4):
            out += _DIM + m.group(4) + _RESET
        return out
    m = _RULE_RE.match(line)
    if m:
        return _BOLD_CYAN + m.group(1) + _RESET + " " + _DIM + m.group(2) + _RESET
    indent = line[: len(line) - len(line.lstrip())]
    body = line[len(indent):]
    if indent:
        m = _ROW_RE.match(line)
        if m and m.group(2) in _GLYPH_COLORS:
            return m.group(1) + _GLYPH_COLORS[m.group(2)] + m.group(2) + _RESET + m.group(3)
        if body.startswith("→"):
            return indent + _GREEN + body + _RESET
        if body.startswith("⚠"):
            return indent + _YELLOW + body + _RESET
        if body.startswith('"') and body.endswith('"'):
            return indent + _BOLD + body + _RESET
        return indent + _DIM + body + _RESET
    if body.startswith("⚠"):
        return _YELLOW + body + _RESET
    if body.startswith("Older than"):
        return _BOLD + body + _RESET
    if body.startswith(("Nothing waiting", "No session is waiting", "✓")):
        return _GREEN + body + _RESET
    return _DIM + body + _RESET


# ---- Markdown, for Claude Code (contract 16.17) ---------------------------------------------------

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~])")
_MD_ROW_RE = re.compile(r"^\s+(\d+) (?:[?⏸▤●◷·!] )?(.*)$")
_MD_PASTE = "To pick it back up, paste: "


def md_escape(text: str) -> str:
    """``text`` as literal Markdown: no emphasis, code, links, HTML or auto-links can start in it."""
    out = _MD_SPECIAL.sub(r"\\\1", text)
    return out.replace("://", "\\://").replace("www.", "www\\.")


def fenced(command: str) -> List[str]:
    """``command`` alone in a fenced block (the fence is longer than any run of backticks in it)."""
    longest = max([len(run) for run in re.findall(r"`+", command)] + [2])
    fence = "`" * (longest + 1)
    return [fence, command, fence]


def markdown(text: str) -> str:
    """``render_text`` output as Markdown, for a chat that renders it (the skill, contract 16.17).

    The wording is ``render_text``'s; only the shape changes, line by line: project rules become
    headings, a row becomes one line (its facts joined on) with the quote as a blockquote and the
    command alone in a fenced block, footers become a list. Nothing depends on a single line break
    surviving. Every word that is not ours is escaped (``md_escape``); commands are never altered.
    """
    out: List[str] = []
    row: Optional[Dict[str, Any]] = None
    lines = text.split("\n")
    # A row's number is right-aligned, so every row head's number ends in the same column; a fact
    # line that happens to start with a number ("23 uncommitted") starts to the right of it.
    ends = [m.end(1) for m in (_MD_ROW_RE.match(ln) for ln in lines) if m]
    number_end = min(ends) if ends else -1

    def blank() -> None:
        if out and out[-1] != "":
            out.append("")

    def flush() -> None:
        nonlocal row
        if row is None:
            return
        blank()
        out.append(" · ".join([row["head"]] + row["facts"]))
        for quote in row["quotes"]:
            blank()
            out.append("> " + quote)
        for command in row["commands"]:
            blank()
            out.extend(fenced(command))
        for note in row["notes"]:
            blank()
            out.append(note)
        blank()
        row = None

    for line in lines:
        body = line.strip()
        if not body:
            flush()
            blank()
            continue
        indented = line[:1] == " "
        if indented:
            m = _MD_ROW_RE.match(line)
            if m and m.end(1) == number_end:
                flush()
                row = {"head": "**%s** · %s" % (m.group(1), md_escape(" ".join(m.group(2).split()))),
                       "facts": [], "quotes": [], "commands": [], "notes": []}
            elif row is None:
                out.append(md_escape(body))
            elif body.startswith("→"):
                row["commands"].append(body[1:].strip())
            elif body.startswith(_MD_PASTE):
                row["commands"].append(body[len(_MD_PASTE):].strip())
            elif body.startswith("⚠"):
                row["notes"].append(md_escape(body))
            elif body.startswith('"') and body.endswith('"'):
                row["quotes"].append(md_escape(body))
            else:
                row["facts"].append(md_escape(body))
            continue
        flush()
        m = _HEADER_RE.match(body)
        if m:
            head = "**Ghosted**" + md_escape(m.group(2))
            if m.group(3):
                head += "**%s**" % m.group(3)
            if m.group(4):
                head += " " + md_escape(m.group(4).strip())
            out.append(head)
            continue
        m = _RULE_RE.match(body)
        if m:
            blank()
            out.append("### " + md_escape(m.group(1)))
            continue
        if body.startswith("Older than") or (body.startswith("Soft (") and "→" not in body):
            blank()
            out.append("**%s**" % md_escape(body))
        elif body.startswith(("Nothing waiting", "No session is waiting", "✓", "⚠", "Another scan")) or " Claude sessions are changing " in body:
            blank()
            out.append(md_escape(body))
            blank()
        else:
            out.append("- " + md_escape(body))       # footers: one list item each
    flush()
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def colorize(text: str) -> str:
    """``render_text`` output with ANSI colour added, line by line, by the shape of each line.
    Removing the codes gives back ``text`` exactly; the text itself holds no control characters
    (``flat``), so nothing from a session can smuggle a code in."""
    out = []
    for line in text.split("\n"):
        styled = _color_line(line)
        if styled != line and not styled.endswith(_RESET):
            styled += _RESET
        out.append(styled)
    return "\n".join(out)


# ---- JSON ---------------------------------------------------------------------------------------

ROW_FIELDS = ("n", "id", "kind", "tier", "reason", "project", "session_uuid", "ended_at", "age_days",
              "uncommitted", "unpushed", "branch_no_upstream", "not_git", "resets_at", "resume",
              "open_in_terminal", "source", "due_at", "title")


UNSAVED_FIELDS = ("n", "id", "project", "repo", "uncommitted", "uncommitted_since", "unpushed",
                  "unpushed_since", "unmerged", "no_remote", "mixed")


def as_dict(report: Report, quotes: bool = False) -> Dict[str, Any]:
    quote_map = report.load_quotes() if quotes else None
    rows = []
    for row in report.rows:
        out = {k: row.get(k) for k in ROW_FIELDS}
        if quote_map is not None:
            out["quote"] = quote_map.get(row["_key"])
        rows.append(out)
    counts = {k: report.counts.get(k, 0) for k in ("checked", "clean", "waiting", "soft", "active",
                                                   "handled", "automated", "also_open", "unsaved")}
    return {
        "version": 1,
        "generated_at": timeutil.to_iso(report.generated_at),
        "window": {"name": report.window_name, "start": timeutil.to_iso(report.start),
                   "end": timeutil.to_iso(report.end)},
        "first_run": report.first_run,
        "older": dict(report.older) if report.older else None,
        "project": report.project,
        "counts": counts,
        "sources": dict(report.sources),
        "scan": report.scan,
        "drift": report.drift,
        "rows": rows,
        "quiet_projects": report.quiet_projects,
        "unsaved": [{k: r.get(k) for k in UNSAVED_FIELDS} for r in report.unsaved],
        "shared_now": [{"project": s["project"], "repo": s["repo"], "sessions": s["sessions"]}
                       for s in report.shared_now],
    }


def render_json(report: Report, quotes: bool = False) -> str:
    return json.dumps(as_dict(report, quotes=quotes), indent=2, ensure_ascii=False) + "\n"
