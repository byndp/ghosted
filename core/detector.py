"""The detector (contract section 6): turn quiet session transcripts into interrupted items.

``scan(cfg, ledger, now=None) -> ScanResult`` is one pass over ``projects_dir/*/*.jsonl``. It is a
work queue, not a high-water mark: ``state.json`` remembers, per session, the ``(size, mtime)`` and
content version it last processed, plus a ``deferred`` list of sessions it saw but could not process
yet. Nothing is ever skipped for being older than the last scan; a deferred session is retried every
run until it is processed, and only a deleted file leaves the list.

Per file, in order (the contract's steps):

1. Only ``<uuid>.jsonl`` at the top of a project folder (never ``<uuid>/subagents``); paths holding
   a ``skip.path_patterns`` substring are left out.
2. A file never seen before whose mtime is older than ``since`` is ignored (the first run sets
   ``since = now - scan_since_days``).
3. Same ``(size, mtime)`` as processed: skip without reading. Else hash the tail; same content
   version: record the new mtime, skip.
4. Modified within ``maybe_active_minutes``: deferred, not processed.
5. Parse. Automated (an ``skip.entrypoints`` entrypoint with at most one human turn in the head, or
   a first prompt matching ``skip.first_prompt_patterns``) -> verdict ``automated``.
5b. A usage limit whose final API error carries ``quotaLimits.resetsAt`` stays deferred until
   ``resetsAt + maybe_active_minutes``: nobody can resume it before the reset, and subscription
   sessions continue on their own after it.
6. Classify. ``usage_limit`` / ``open_question`` create the session's interrupted item or refresh its
   Mac-owned fields; ``clean`` with an item adds ``resumed MM-DD, ended clean`` and
   ``looks_resolved``. The detector never closes anything: status is the phone's. A flagged ending
   carries its timestamp (``interrupted_at``); one newer than the item's close reopens a done or
   dismissed item, and one newer than the last flagged ending un-resolves an item that looked
   resolved (the ledger and the server apply that rule, contract 6). So a session resumed after
   Done that stops on a question or a limit again is pushed again.
7. Every run also refreshes the git evidence of open interrupted items (an ``item.upsert`` only when
   the string changed). A session gets at most one upsert per run.

Transcripts are parsed without the ledger lock; the lock is held only inside the ledger's own
mutations and the final state save. One bad file never aborts a scan: its exception TYPE name goes
to ``ScanResult.errors`` (never a message, which could quote the transcript), and the file is
retried next run.

``state.json`` (contract section 6) with these additions, all local-only: per session ``cwd`` (the
launch directory, for evidence refreshes), ``project``, ``processed_at`` and ``resumed`` (MM-DD of a
clean ending after a flag); top-level ``deferred_info`` = ``{uuid: {"why": "active"|"limit",
"size", "mtime", "version", "until"}}`` so an unchanged deferred file is never re-hashed and a
usage-limit deferral knows its deadline without re-parsing.

v2 additions (contract 16.1, 16.3, 16.6), all local:

* ``scan(..., since_floor=...)`` lowers ``since`` to the floor when the floor is earlier, so a report
  over a longer window picks up files the first run left out as too old. Nothing else changes.
* Top-level ``classifier`` (int): when it is not ``classify.CLASSIFIER_VERSION``, every stored session
  whose verdict is ``open_question`` is processed once more even if its file is unchanged (both fast
  paths bypassed; a maybe-active one is still deferred, and its deferral remembers the pending
  re-classification). The save writes the current version, unless a forced session failed with an
  exception, in which case the old value is kept so the whole pass is retried next run.
* Per session ``resets_at`` (ISO) for a usage limit whose final error carries ``quotaLimits.resetsAt``;
  a ``why == "limit"`` deferral also keeps ``cwd``, ``project``, ``ended_at``, ``reason`` and
  ``resets_at``, so the report can list a limit still waiting for its reset.
* ``drift`` = the schema-drift counters over the files parsed this scan (a scan that parsed none keeps
  the previous value); ``drift_problem(drift)`` turns them into one warning line or None.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from core import classify, evidence, notifier, sanitize, timeutil, transcript
from core import ledger as ledger_mod

STATE_VERSION = 1
UNKNOWN_PROJECT = "unknown"
# A resetsAt further out than this is not believed (weekly limits reset within 7 days); the
# session is then processed at once instead of being parked on a bad value.
MAX_RESET_WAIT_SECONDS = 8 * 24 * 3600
AUTOMATED_ENTRYPOINT = "entrypoint"
AUTOMATED_FIRST_PROMPT = "first_prompt"
AUTOMATED_TEMP = "temp_folder"
# Scratch space: a session launched in here, or a repo that lives here, is a test or a throwaway
# (contract 16.13). The folder may be gone after a restart, so its resume command is not kept.
TEMP_ROOTS: Tuple[str, ...] = tuple(dict.fromkeys(
    (tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/tmp", "/private/var/folders")))
WHY_ACTIVE = "active"
WHY_LIMIT = "limit"
DRIFT_TOP_TYPES = 5
DRIFT_MIN_FILES = 3

_RESUMED_RE = re.compile(r"resumed (\d{2}-\d{2}), ended clean")


@dataclass
class ScanResult:
    """Counts from one scan. ``flagged`` = ``(uuid, verdict, reason, item_id)`` for every session
    processed this run that ended on a usage limit or an open question. ``errors`` = exception
    type names, one per file that failed (never messages)."""

    sessions_seen: int = 0
    sessions_scanned: int = 0
    deferred: int = 0
    skipped_automated: int = 0
    items_added: int = 0
    items_updated: int = 0
    flagged: List[Tuple[str, str, str, Optional[str]]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    # Extra detail (not in the contract's list)
    ignored_old: int = 0
    unchanged: int = 0
    deferred_limit: int = 0
    evidence_refreshed: int = 0
    items_reopened: int = 0  # closed or resolved items a newer flagged ending brought back
    reclassified: int = 0    # open_question sessions processed again for a new classifier version


# ---- session facts ---------------------------------------------------------------------------------

def _relative(path: str, root: str) -> Optional[str]:
    """``path`` relative to ``root`` ('' for the root itself), or None when it is not under it."""
    if path == root:
        return ""
    prefix = root if root.endswith(os.sep) else root + os.sep
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


def git_toplevel(path: str, max_depth: int = 64) -> Optional[str]:
    """The nearest ancestor of ``path`` (itself included) holding a ``.git`` entry. No subprocess
    (the hook calls ``project_for`` and has a sub-second budget)."""
    current = os.path.normpath(path)
    for _ in range(max_depth):
        try:
            if os.path.lexists(os.path.join(current, ".git")):
                return current
        except (OSError, ValueError):
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def _clean_name(name: str) -> str:
    return name.strip() or UNKNOWN_PROJECT


def project_for(cwd: Optional[str], cfg: Any) -> str:
    """The project a launch directory belongs to (contract 6.8).

    The first path component under a workspace root; the root's own basename when ``cwd`` is the
    root; else the git toplevel's basename; else the cwd's basename (``"unknown"`` without a cwd).
    """
    if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
        return UNKNOWN_PROJECT
    path = os.path.normpath(os.path.expanduser(cwd.strip()))
    roots = [os.path.normpath(str(r)) for r in (getattr(cfg, "workspace_roots", None) or [])]
    real_path: Optional[str] = None
    for root in roots:
        rel = _relative(path, root)
        if rel is None:  # symlinked roots: compare resolved paths too
            if real_path is None:
                real_path = os.path.realpath(path)
            rel = _relative(real_path, os.path.realpath(root))
        if rel is None:
            continue
        if rel == "":
            return _clean_name(os.path.basename(root) or root)
        return _clean_name(rel.split(os.sep)[0])
    top = git_toplevel(path)
    if top:
        return _clean_name(os.path.basename(top) or top)
    return _clean_name(os.path.basename(path) or path)


def resume_command(cwd: Optional[str], uuid: str, home: Optional[str] = None) -> str:
    """``cd ~/workspace/boathouse && claude --resume <uuid>`` (contract 6.8).

    ``claude --resume`` only finds a session from its launch directory, so the command starts with
    a ``cd`` there, written with ``~`` under HOME and shell-quoted only where needed.
    """
    tail = "claude --resume %s" % shlex.quote(uuid)
    if not isinstance(cwd, str) or not cwd.strip():
        return tail
    return "%s && %s" % (cd_command(cwd, home), tail)


def cd_command(cwd: str, home: Optional[str] = None) -> str:
    """``cd ~/workspace/boathouse``: ``~`` under HOME, shell-quoted only where needed."""
    path = os.path.normpath(cwd.strip())
    home_dir = home if home is not None else (os.environ.get("HOME") or os.path.expanduser("~"))
    home_dir = os.path.normpath(home_dir) if home_dir else ""
    if home_dir and home_dir != os.sep and path == home_dir:
        where = "~"
    elif home_dir and home_dir != os.sep and path.startswith(home_dir + os.sep):
        where = "~/" + shlex.quote(path[len(home_dir) + 1:])
    else:
        where = shlex.quote(path)
    return "cd %s" % where


def interrupted_title(project: str, ended_local_date: date, verdict: str) -> str:
    """The fixed public title: ``"{project}: session MM-DD ended on usage limit"`` (or ``"... on an
    open question"``). Nothing from the transcript goes in it."""
    what = "usage limit" if verdict == classify.USAGE_LIMIT else "an open question"
    return "%s: session %s ended on %s" % (project, timeutil.mmdd(ended_local_date), what)


def safe_title(project: str, ended_local_date: date, verdict: str) -> Tuple[str, str]:
    """(project, title) that the ledger will accept. A project name the sanitizer refuses (an
    email or key-like folder name, one over the 60-character project limit, or one too long for
    the 100-character title) is replaced by a neutral ``project-<6 hex>`` alias, stable per name."""
    title = interrupted_title(project, ended_local_date, verdict)
    try:
        ledger_mod.check_interrupted_title(title, project)
        return project, title
    except sanitize.Refused:
        alias = "project-" + hashlib.sha1(project.encode("utf-8", "surrogatepass")).hexdigest()[:6]
        return alias, interrupted_title(alias, ended_local_date, verdict)


def compile_prompt_patterns(cfg: Any) -> List[re.Pattern[str]]:
    out: List[re.Pattern[str]] = []
    for pattern in getattr(cfg, "skip_first_prompt_patterns", None) or []:
        try:
            out.append(re.compile(pattern))
        except (re.error, TypeError):
            continue  # config.py validates these; never fatal here
    return out


def is_automated(summary: Any, cfg: Any, patterns: Optional[List[re.Pattern[str]]] = None) -> Optional[str]:
    """``"entrypoint"`` or ``"first_prompt"`` when the session is automated (contract 6.5), else None.

    Entrypoint alone is not enough: Remote Control conversations also carry ``sdk-cli``, so the
    head must show at most one human turn (a one-prompt ``claude -p`` run).
    """
    skip = set(getattr(cfg, "skip_entrypoints", None) or ())
    entrypoints = set(getattr(summary, "entrypoints", None) or ())
    turns = getattr(summary, "head_human_turns", 0) or 0
    if entrypoints & skip and turns <= 1:
        return AUTOMATED_ENTRYPOINT
    text = getattr(summary, "first_human_text", None)
    if isinstance(text, str):
        for rx in (patterns if patterns is not None else compile_prompt_patterns(cfg)):
            if rx.search(text):
                return AUTOMATED_FIRST_PROMPT
    return None


def resolved_temp_roots() -> List[str]:
    """``TEMP_ROOTS`` with links resolved (``/tmp`` is ``/private/tmp`` on a Mac)."""
    out: List[str] = []
    for root in TEMP_ROOTS:
        if not isinstance(root, str) or not root:
            continue
        try:
            real = os.path.realpath(root)
        except (OSError, ValueError):
            continue
        if real not in out:
            out.append(real)
    return out


def in_temp_folder(cwd: Optional[str], roots: Optional[List[str]] = None) -> bool:
    """True when ``cwd`` is a temp root or lies under one (links resolved). Never raises."""
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        real = os.path.realpath(cwd)
    except (OSError, ValueError):
        return False
    for root in (resolved_temp_roots() if roots is None else roots):
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def reset_deadline(resets_at: Any, now_ts: float, active_seconds: float) -> Optional[float]:
    """Epoch seconds until which a usage-limit session stays deferred (contract 5b), or None.

    ``resetsAt`` is epoch seconds; a value in milliseconds is accepted. A deadline already passed,
    or implausibly far away, gives None (process now)."""
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    value = float(resets_at)
    if value != value or value <= 0:
        return None
    if value > 1e11:  # milliseconds
        value /= 1000.0
    until = value + active_seconds
    if now_ts >= until or until - now_ts > MAX_RESET_WAIT_SECONDS + active_seconds:
        return None
    return until


def ended_at(summary: Any, now: datetime) -> datetime:
    """When the session ended: the final assistant text's timestamp, else the file mtime."""
    ts = getattr(summary, "final_ts", None)
    if isinstance(ts, str):
        try:
            return timeutil.parse_iso(ts)
        except ValueError:
            pass
    mtime = getattr(summary, "mtime", None)
    if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
        try:
            return datetime.fromtimestamp(float(mtime), timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return now


def flagged_at(summary: Any) -> Optional[datetime]:
    """When the session's final assistant text was written, or None when the transcript gives no
    readable timestamp. Only a real timestamp may reopen a closed item: the mtime fallback of
    ``ended_at`` moves whenever anything is appended, which is not a new ending."""
    ts = getattr(summary, "final_ts", None)
    if not isinstance(ts, str):
        return None
    try:
        return timeutil.parse_iso(ts)
    except ValueError:
        return None


def ending_of(item: Mapping[str, Any]) -> str:
    """The original ending phrase of an interrupted item, read from its template title."""
    title = item.get("title")
    if isinstance(title, str) and title.endswith("ended on usage limit"):
        return evidence.ENDING_LIMIT
    return evidence.ENDING_QUESTION


def resets_iso(resets_at: Any) -> Optional[str]:
    """``quotaLimits.resetsAt`` (epoch seconds, or milliseconds) as an ISO instant, or None."""
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    value = float(resets_at)
    if value != value or value <= 0:
        return None
    if value > 1e11:  # milliseconds
        value /= 1000.0
    try:
        return timeutil.to_iso(datetime.fromtimestamp(value, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


# ---- schema drift (contract 16.6) ------------------------------------------------------------------

def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


class DriftCounter:
    """Adds up the drift counters of the summaries parsed in one scan."""

    def __init__(self) -> None:
        self.files = 0
        self.lines = 0
        self.zero_conversation = 0
        self.empty_final = 0
        self.no_cwd = 0
        self.unknown: Dict[str, int] = {}

    def add(self, summary: Any) -> None:
        self.files += 1
        self.lines += _count(getattr(summary, "lines_read", 0))
        if _count(getattr(summary, "conversation_entries", 0)) == 0:
            self.zero_conversation += 1
        elif not isinstance(getattr(summary, "launch_cwd", None), str):
            self.no_cwd += 1  # the touch index (contract 16.11) needs each entry's cwd
        final = getattr(summary, "final_text", None)
        if not isinstance(final, str) or not final.strip():
            self.empty_final += 1
        unknown = getattr(summary, "unknown_types", None)
        if isinstance(unknown, Mapping):
            for name, n in unknown.items():
                if isinstance(name, str):
                    self.unknown[name] = self.unknown.get(name, 0) + _count(n)

    def value(self, stamp: str) -> Optional[Dict[str, Any]]:
        """``state.drift``, or None when nothing was parsed (the caller keeps the old value)."""
        if not self.files:
            return None
        top = sorted(((n, name) for name, n in self.unknown.items() if n > 0), key=lambda t: (-t[0], t[1]))
        return {"at": stamp, "files": self.files, "lines": self.lines,
                "zero_conversation": self.zero_conversation, "empty_final": self.empty_final,
                "no_cwd": self.no_cwd, "unknown_lines": sum(self.unknown.values()),
                "unknown_types": {name: n for n, name in top[:DRIFT_TOP_TYPES]}}


def drift_problem(drift: Any) -> Optional[str]:
    """One line saying the transcript format may have moved, or None (contract 16.6).

    Needs at least 3 files. In order: ``zero_conversation >= max(2, files / 2)``; unknown entry
    lines over 25% of the lines read; ``empty_final >= files / 2``."""
    if not isinstance(drift, Mapping):
        return None
    files = _count(drift.get("files"))
    if files < DRIFT_MIN_FILES:
        return None
    zero = _count(drift.get("zero_conversation"))
    if zero >= max(2, files / 2.0):
        return "%d of %d session files had no messages Ghosted could read" % (zero, files)
    no_cwd = _count(drift.get("no_cwd"))
    if no_cwd >= max(2, files / 2.0):
        return "%d of %d session files had no working folder Ghosted could read" % (no_cwd, files)
    lines = _count(drift.get("lines"))
    unknown_lines = _count(drift.get("unknown_lines"))
    if lines > 0 and unknown_lines > 0.25 * lines:
        types = drift.get("unknown_types")
        names: List[str] = []
        if isinstance(types, Mapping):
            ranked = sorted(((_count(n), k) for k, n in types.items() if isinstance(k, str)),
                            key=lambda t: (-t[0], t[1]))
            names = [k for _, k in ranked]
        return "unknown entry types are most of the transcript: " + (", ".join(names) or "unnamed")
    if _count(drift.get("empty_final")) >= files / 2.0:
        return "most sessions had no final reply Ghosted could read"
    return None


# ---- enumeration -----------------------------------------------------------------------------------

def enumerate_sessions(cfg: Any, errors: Optional[List[str]] = None) -> List[Tuple[str, Path, os.stat_result]]:
    """``(uuid, path, stat)`` for every session file: ``projects_dir/*/<uuid>.jsonl``, path-pattern
    skips applied. A uuid present twice keeps its most recently modified file."""
    root = Path(cfg.projects_dir)
    patterns = [p for p in (getattr(cfg, "skip_path_patterns", None) or []) if isinstance(p, str) and p]
    home = getattr(cfg, "home", None)
    # The Claude app notifier's own sessions (contract 16.12), by id.
    notifier_ids, notifier_prefixes = notifier.skipped(home) if home else (set(), set())
    found: Dict[str, Tuple[Path, os.stat_result]] = {}
    try:
        folders = sorted(os.scandir(str(root)), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        if errors is not None:
            errors.append(type(exc).__name__)
        return []
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            entries = sorted(os.scandir(folder.path), key=lambda e: e.name)
        except OSError as exc:
            if errors is not None:
                errors.append(type(exc).__name__)
            continue
        for entry in entries:
            if not transcript.is_session_file(entry.name):
                continue
            path = Path(entry.path)
            if any(p in str(path) for p in patterns):
                continue
            try:
                if not entry.is_file():
                    continue
                st = os.stat(entry.path)
            except OSError:
                continue  # vanished between listing and stat
            uuid = entry.name[: -len(".jsonl")]
            if notifier.is_notifier(uuid, notifier_ids, notifier_prefixes):
                continue
            prior = found.get(uuid)
            if prior is None or st.st_mtime > prior[1].st_mtime:
                found[uuid] = (path, st)
    return [(uuid, path, st) for uuid, (path, st) in sorted(found.items())]


# ---- the scan --------------------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return timeutil.parse_iso(value).timestamp()
    except ValueError:
        return None


class _Scan:
    def __init__(self, cfg: Any, ledger: Any, now: datetime, since_floor: Optional[datetime] = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.now = now
        self.now_ts = now.timestamp()
        self.stamp = timeutil.to_iso(now)
        self.active_seconds = max(0, int(getattr(cfg, "maybe_active_minutes", 30) or 0)) * 60
        self.tz = getattr(cfg, "timezone", None) or "UTC"
        self.result = ScanResult()
        self.patterns = compile_prompt_patterns(cfg)
        self.ignore_tail = getattr(cfg, "ignore_tail_patterns", None) or ()
        self.handoff = getattr(cfg, "handoff_patterns", None)
        # None = the temp-folder skip is off ([skip] temp_folders = false).
        self.temp_roots = resolved_temp_roots() if getattr(cfg, "skip_temp_folders", True) else None
        self.git_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self.upserted: Set[str] = set()

        state = ledger.load_state()
        self.state = state if isinstance(state, dict) else ledger_mod.default_state()
        self.sessions: Dict[str, Dict[str, Any]] = {
            k: v for k, v in _as_dict(self.state.get("sessions")).items() if isinstance(v, dict)}
        deferred = self.state.get("deferred")
        self.prev_deferred: Set[str] = {u for u in deferred if isinstance(u, str)} if isinstance(deferred, list) else set()
        self.prev_info = _as_dict(self.state.get("deferred_info"))
        self.new_deferred: Dict[str, Dict[str, Any]] = {}
        since = _parse_ts(self.state.get("since"))
        if since is None:
            days = max(0, int(getattr(cfg, "scan_since_days", 14) or 0))
            since_dt = now - timedelta(days=days)
            self.state["since"] = timeutil.to_iso(since_dt)
            since = since_dt.timestamp()
        if since_floor is not None and since_floor.timestamp() < since:
            since = since_floor.timestamp()
            self.state["since"] = timeutil.to_iso(since_floor)
        self.since_ts = since
        stored_classifier = self.state.get("classifier")
        self.reclassify = stored_classifier != classify.CLASSIFIER_VERSION
        self.reclassify_failed = False
        self.drift = DriftCounter()
        # Interrupted items by session (only the detector creates them, and run.lock keeps scans
        # apart, so one read up front stays true; entries are replaced after each upsert).
        self.by_session: Dict[str, Dict[str, Any]] = {}
        for item in ledger.items().values():
            uuid = item.get("session_uuid")
            if item.get("kind") == "interrupted" and isinstance(uuid, str):
                prior = self.by_session.get(uuid)
                if prior is None or (ledger_mod.id_number(item.get("id")) or 0) < (ledger_mod.id_number(prior.get("id")) or 0):
                    self.by_session[uuid] = item

    # -- helpers -----------------------------------------------------------------------------------

    def git(self, cwd: Optional[str]) -> Optional[Dict[str, Any]]:
        if not isinstance(cwd, str) or not cwd:
            return None
        key = os.path.normpath(cwd)
        if key not in self.git_cache:
            self.git_cache[key] = evidence.git_facts(key)
        return self.git_cache[key]

    def defer(self, uuid: str, st: os.stat_result, version: Optional[str], why: str,
              until: Optional[float] = None, extra: Optional[Mapping[str, Any]] = None,
              reclassify: bool = False) -> None:
        entry: Dict[str, Any] = {
            "why": why, "size": st.st_size, "mtime": st.st_mtime, "version": version,
            "until": timeutil.to_iso(datetime.fromtimestamp(until, timezone.utc)) if until is not None else None,
        }
        if extra:
            entry.update(extra)
        if reclassify:
            entry["reclassify"] = True  # still owes a pass under the new classifier
        self.new_deferred[uuid] = entry

    def keep_deferred(self, uuid: str) -> None:
        info = self.prev_info.get(uuid)
        self.new_deferred[uuid] = dict(info) if isinstance(info, dict) else {"why": WHY_ACTIVE}

    def upsert(self, uuid: str, version: Optional[str], project: str, title: str, ev: str,
               looks_resolved: bool, resume: Optional[str], reason: Optional[str],
               interrupted_at: Optional[datetime] = None) -> Tuple[Dict[str, Any], bool]:
        """One upsert. Returns (item, applied): applied is False when the ledger ignored the
        observation (not newer than the stored one), so the caller must retry next run."""
        existed = uuid in self.by_session
        item, event = self.ledger.upsert_interrupted(
            uuid, version, project, title, ev, looks_resolved, resume, reason,
            observed_at=self.now, now=self.now, interrupted_at=interrupted_at)
        self.by_session[uuid] = item
        self.upserted.add(uuid)
        if not existed:
            self.result.items_added += 1
        elif event is not None:
            self.result.items_updated += 1
            if item.get("reopened_at") == self.stamp:
                self.result.items_reopened += 1
        applied = (item.get("title") == title and item.get("evidence") == ev
                   and bool(item.get("looks_resolved")) == bool(looks_resolved)
                   and item.get("resume") == resume and item.get("content_version") == version)
        return item, applied

    # -- per file ----------------------------------------------------------------------------------

    def forced(self, rec: Optional[Mapping[str, Any]], info: Mapping[str, Any]) -> bool:
        """True when this session owes a pass under the current classifier (contract 16.1)."""
        if info.get("reclassify") is True:
            return True
        if not self.reclassify or rec is None:
            return False
        if rec.get("verdict") == classify.OPEN_QUESTION:
            return True
        # v3 (contract 16.10): a summary ending may now be a hand-off; re-read the plain clean ones.
        return rec.get("verdict") == classify.CLEAN and rec.get("reason") == classify.NO_QUESTION

    def one(self, uuid: str, path: Path, st: os.stat_result) -> None:
        rec = self.sessions.get(uuid)
        was_deferred = uuid in self.prev_deferred
        info = _as_dict(self.prev_info.get(uuid)) if was_deferred else {}
        size, mtime = st.st_size, st.st_mtime
        force = self.forced(rec, info)
        try:
            self._one(uuid, path, st, rec, was_deferred, info, size, mtime, force)
        except Exception:
            if force:
                self.reclassify_failed = True
            raise

    def _one(self, uuid: str, path: Path, st: os.stat_result, rec: Optional[Dict[str, Any]],
             was_deferred: bool, info: Dict[str, Any], size: int, mtime: float, force: bool) -> None:
        if rec is None and not was_deferred and mtime < self.since_ts:
            self.result.ignored_old += 1
            return
        if (not force and rec is not None and not was_deferred
                and rec.get("size") == size and rec.get("mtime") == mtime):
            self.result.unchanged += 1
            return

        version: Optional[str] = None
        if info and info.get("size") == size and info.get("mtime") == mtime:
            # Unchanged since it was deferred: no re-hash. A usage-limit deferral waits for its
            # deadline without re-parsing.
            known = info.get("version")
            version = known if isinstance(known, str) else None
            if info.get("why") == WHY_LIMIT:
                until = _parse_ts(info.get("until"))
                if until is not None and self.now_ts < until:
                    self.keep_deferred(uuid)
                    self.result.deferred_limit += 1
                    return
        if version is None:
            version = transcript.content_version(path)
        if not force and rec is not None and rec.get("version") == version:
            rec["size"], rec["mtime"] = size, mtime
            self.result.unchanged += 1
            return
        if self.now_ts - mtime < self.active_seconds:
            self.defer(uuid, st, version, WHY_ACTIVE, reclassify=force)
            return

        summary = transcript.parse_transcript(path)
        self.drift.add(summary)
        self.result.sessions_scanned += 1
        if force:
            self.result.reclassified += 1
        base: Dict[str, Any] = {"size": size, "mtime": mtime, "version": version,
                                "item_id": (self.by_session.get(uuid) or {}).get("id"),
                                "processed_at": self.stamp}
        launch = summary.launch_cwd
        auto = is_automated(summary, self.cfg, self.patterns)
        if auto is None and self.temp_roots is not None and in_temp_folder(launch, self.temp_roots):
            auto = AUTOMATED_TEMP
        if auto is not None:
            self.result.skipped_automated += 1
            base.update(verdict=classify.AUTOMATED, reason=auto, ended_at=None, cwd=launch,
                        project=project_for(launch, self.cfg))
            self.sessions[uuid] = base
            return

        verdict, reason = classify.classify(summary, self.ignore_tail, self.handoff)
        resets = resets_iso(summary.final_resets_at) if verdict == classify.USAGE_LIMIT else None
        ended = ended_at(summary, self.now)
        project = project_for(launch, self.cfg)
        if verdict == classify.USAGE_LIMIT:
            until = reset_deadline(summary.final_resets_at, self.now_ts, self.active_seconds)
            if until is not None:
                # The report lists a limit still waiting for its reset (no item yet), so the
                # deferral keeps what a row needs (contract 16.3).
                self.defer(uuid, st, version, WHY_LIMIT, until, extra={
                    "cwd": launch, "project": project, "ended_at": timeutil.to_iso(ended),
                    "reason": reason, "resets_at": resets})
                self.result.deferred_limit += 1
                return

        ended_local = timeutil.local_date(ended, self.tz)
        base.update(verdict=verdict, reason=reason, ended_at=timeutil.to_iso(ended), cwd=launch, project=project)
        if resets is not None:
            base["resets_at"] = resets
        existing = self.by_session.get(uuid)

        if verdict in classify.FLAGGED_VERDICTS:
            project, title = safe_title(project, ended_local, verdict)
            ev = evidence.evidence_text(evidence.ENDINGS[verdict], self.git(launch))
            item, applied = self.upsert(uuid, version, project, title, ev, False,
                                        resume_command(launch, uuid), reason,
                                        interrupted_at=flagged_at(summary))
            self.result.flagged.append((uuid, verdict, reason, item.get("id")))
            if not applied:
                return  # the ledger had a newer observation: leave the record, retry next run
            base.update(item_id=item.get("id"), project=project)
        elif verdict == classify.CLEAN and existing is not None:
            resumed = timeutil.mmdd(ended_local)
            ev = evidence.evidence_text(ending_of(existing), self.git(launch), resumed=resumed)
            resume = existing.get("resume") if isinstance(existing.get("resume"), str) else resume_command(launch, uuid)
            item, applied = self.upsert(uuid, version, existing.get("project") or project,
                                        existing.get("title") or "", ev, True, resume, existing.get("reason"))
            if not applied:
                return
            base.update(item_id=item.get("id"), resumed=resumed)
        self.sessions[uuid] = base

    # -- evidence refresh --------------------------------------------------------------------------

    def refresh(self) -> None:
        for item in self.ledger.open_items():
            uuid = item.get("session_uuid")
            if item.get("kind") != "interrupted" or not isinstance(uuid, str) or uuid in self.upserted:
                continue
            rec = self.sessions.get(uuid) or {}
            cwd = rec.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                continue
            resumed = None
            if item.get("looks_resolved"):
                resumed = rec.get("resumed")
                if not isinstance(resumed, str):
                    m = _RESUMED_RE.search(item.get("evidence") or "")
                    resumed = m.group(1) if m else None
            ev = evidence.evidence_text(ending_of(item), self.git(cwd), resumed=resumed)
            if ev == item.get("evidence"):
                continue
            try:
                _, event = self.ledger.upsert_interrupted(
                    uuid, item.get("content_version"), item.get("project") or "", item.get("title") or "",
                    ev, bool(item.get("looks_resolved")), item.get("resume"), item.get("reason"),
                    observed_at=self.now, now=self.now)
            except Exception as exc:  # best effort: the state save below must still happen
                self.result.errors.append(type(exc).__name__)
                continue
            self.upserted.add(uuid)
            if event is not None:
                self.result.items_updated += 1
                self.result.evidence_refreshed += 1

    # -- the whole pass ----------------------------------------------------------------------------

    def run(self) -> ScanResult:
        seen: Set[str] = set()
        listing_errors = len(self.result.errors)
        found = enumerate_sessions(self.cfg, self.result.errors)
        listing_failed = len(self.result.errors) > listing_errors
        for uuid, path, st in found:
            seen.add(uuid)
            self.result.sessions_seen += 1
            try:
                self.one(uuid, path, st)
            except Exception as exc:  # one bad file never aborts the scan; the type name only
                self.result.errors.append(type(exc).__name__)
                if uuid in self.prev_deferred and uuid not in self.new_deferred:
                    self.keep_deferred(uuid)
        self.refresh()
        if self.reclassify and listing_failed and any(
                rec.get("verdict") == classify.OPEN_QUESTION and uuid not in seen
                for uuid, rec in self.sessions.items()):
            # A folder could not be listed: its open questions were never reached, so the debt
            # stays (a file that is really gone is not owed: with a clean listing it is not seen).
            self.reclassify_failed = True
        for uuid in list(self.sessions):
            if uuid not in seen and not self.sessions[uuid].get("item_id"):
                del self.sessions[uuid]  # the file is gone and nothing refers to it
        self.state.update(version=STATE_VERSION, sessions=self.sessions, deferred=sorted(self.new_deferred),
                          deferred_info=self.new_deferred, last_scan=self.stamp)
        if not self.reclassify_failed:
            self.state["classifier"] = classify.CLASSIFIER_VERSION
        drift = self.drift.value(self.stamp)
        if drift is not None:
            self.state["drift"] = drift
        self.ledger.save_state(self.state)
        self.result.deferred = len(self.new_deferred)
        return self.result


def scan(cfg: Any, ledger: Any, now: Any = None, since_floor: Any = None) -> ScanResult:
    """One detector pass (contract section 6). ``now`` = an aware datetime or ISO string (default:
    the clock). ``since_floor`` (same forms, optional) lowers ``state.since`` to it when it is
    earlier, so files never seen and older than the old ``since`` become eligible (contract 16.3).
    Raises only for problems with the ledger itself (a damaged items.json or state.json, a lock
    held too long); per-file problems land in ``ScanResult.errors``."""
    when = timeutil.now_utc() if now is None else timeutil.parse_iso(now)
    floor = None if since_floor is None else timeutil.parse_iso(since_floor)
    return _Scan(cfg, ledger, when, floor).run()
