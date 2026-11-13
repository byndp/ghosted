"""The local ledger in GHOSTED_HOME: items.json, the public event inbox, acks and the append-only logs.

Rules this module enforces (contract §2, §4, §5, §8):

- Item mutations (`add`, `upsert_interrupted`, `set_status`, `reopen_expired`) run under `lock()`
  and append exactly one event per changed item to inbox.jsonl in the same critical section (a
  no-op upsert or a request for the state an item already has appends none and returns event
  None). `merge_server_items` applies the server's view and appends no event (echoing server
  state back would loop).
- Crash windows. A new item is saved to items.json before its creation event is appended (the
  other order could hand its id to a second item after a crash). A crash between the two leaves
  an unsynced item with no event: `items_missing_events()` finds it and `reemit_events()` queues
  it again (`ghosted doctor` / `ghosted doctor --fix`). A status change appends its event first, then saves
  the item: an event whose local save was lost is still applied by the server, and the next
  merge brings the Mac in line, so the change is never silently undone.
- Event payloads are built only from `public_fields(item)`, a whitelist: `local_title`, `reason`,
  `consequential` and `synced` can never reach the inbox; `observed_at` is public for
  interrupted items only. Titles and project names are sanitized here as well as in the CLI,
  and an interrupted title must be exactly the fixed template for the item's own project and a
  real MM-DD, so nothing derived from transcript text is queued for upload.
- Status writes follow the server's rules: asking for the state an item already has (same
  status, and for snoozed / dismissed the same snooze_until / reason) is a no-op; a different
  snooze_until (re-snooze) or reason is a change, rev + 1. Snooze expiry (`reopen_expired`) keeps
  snooze_until so the push rules still see the item as reopened today (contract 12); a manual
  reopen (`set_status(id, "open")`) clears it.
- `lock()` is an flock on GHOSTED_HOME/lock, re-entrant within one process (nested `with` blocks and
  helper calls under an outer lock are fine), waiting up to 10 s before raising `LedgerBusy`.
- JSON files are replaced atomically (temp file in the same directory, fsync, os.replace);
  jsonl files get one `os.write` per line on an O_APPEND descriptor, starting with a newline if
  a crash left the last line torn (so the torn line can't swallow the new one). Inbox appends
  are fsync'd like items.json. Directories this module creates are 0700 and every file it
  writes is 0600.
- Notification dedup is crash-safe (contract 12: the row goes in before the send). `claim_dedup`
  appends a claim row `{dedup_key, ok: null, ...}` to notifications.jsonl (fsync'd, under the
  lock) and returns True only when no row carried the key yet; after sending, the caller appends
  the outcome row (`append_notification`, same dedup_key, ok true/false). `notifications()` gives
  one row per dedup_key, the last one winning (fields of earlier rows fill in what it lacks), so
  every reader (the budget, `ghosted report`, the push rules, the remote mirror) sees a claim whose
  outcome was never recorded as `ok: null`: it dedups and counts against the budget, and it is
  never taken as delivered. Claims that an older version kept in GHOSTED_HOME/dedup.json are moved
  into notifications.jsonl by the next claim, then dedup.json is removed (until then
  `notifications()` reports them as `ok: null` rows).

Error messages name ids, files and fields, never item text.
"""
from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, Union

from core import sanitize, timeutil

KINDS = ("interrupted", "promise", "decision", "miss", "unsaved")
LOCAL_KINDS = ("unsaved",)  # Mac only: never an inbox event, never synced (contract 16.9)
ADD_KINDS = ("promise", "decision", "miss")
OWNERS = ("me", "claude")
STATUSES = ("open", "done", "snoozed", "dismissed")
OPEN_STATUSES = ("open", "snoozed")
CLOSED_STATUSES = ("done", "dismissed")
DISMISS_REASONS = ("not_real", "handled_elsewhere")
CLOSED_BY = ("phone", "session")
CLOSED_BY_SCAN = "scan"  # an unsaved item whose repo got saved (contract 16.9)
# "conflict": the server already holds that id for a different item (a reset ledger, or a second
# GHOSTED_HOME on the same Worker); the Mac renumbers its item (merge_server_items).
ACK_RESULTS = ("applied", "duplicate", "stale", "invalid", "conflict")

LOCK_TIMEOUT = 10.0

MAC_ONLY_FIELDS = ("local_title", "reason", "consequential", "synced", "reopened_at",
                   "consequential_set_at", "known_before_pilot")
PUBLIC_KEYS = (
    "id", "kind", "project", "title", "owner", "due_at", "review_at", "status", "snooze_until",
    "dismiss_reason", "evidence", "looks_resolved", "resume", "session_uuid", "content_version",
    "rev", "created_at", "updated_at", "closed_at", "closed_by", "interrupted_at", "acted_at",
    "acted_status",
)
ADD_EVENT_KEYS = ("id", "kind", "project", "title", "owner", "due_at", "review_at", "created_at")
UPSERT_EVENT_KEYS = (
    "id", "kind", "session_uuid", "content_version", "project", "title", "owner", "review_at",
    "evidence", "looks_resolved", "resume", "observed_at", "created_at", "interrupted_at",
)
# prior_*: the state the change was made from, so the server can refuse a change built on a
# state it never had, even when the rev happens to match (contract 14).
STATUS_EVENT_KEYS = ("id", "status", "rev", "snooze_until", "dismiss_reason", "closed_by",
                     "prior_status", "prior_snooze_until", "prior_dismiss_reason")
MAC_OWNED_FIELDS = ("title", "evidence", "looks_resolved", "content_version", "resume")
# acted_at / acted_status: when a person (the phone, or a session's `ghosted done/snooze/dismiss`)
# last changed the status, and to what. A snooze expiry is not an act, so they survive it
# (core.pull reads acted_at to tell whether a blocked session went on after the user handled it).
SERVER_OWNED_FIELDS = ("status", "snooze_until", "dismiss_reason", "closed_at", "closed_by", "rev",
                       "acted_at", "acted_status")
MEASURE_STATUS = "status"  # measure.jsonl: {"type": "status", "id", "status", "reason", "at"}

INTERRUPTED_TITLE_RE = re.compile(  # ASCII MM-DD, as the Worker's template check requires
    r"^(?P<project>.+): session (?P<mm>[0-9]{2})-(?P<dd>[0-9]{2}) ended on (?:usage limit|an open question)$")
MSG_TEMPLATE = ('does not follow the interrupted-title template '
                '"{project}: session MM-DD ended on usage limit" (or "... on an open question"), '
                'where {project} is the item\'s project and MM-DD a real date')

_ID_RE = re.compile(r"^GS-([0-9]+)$")
# "GS-0012", "gs-12", "12"; also the "LE-0012" form from before the rename (contract 16.19)
_LOOSE_ID_RE = re.compile(r"^\s*(?:GS|LE)?-?0*(\d+)\s*$", re.IGNORECASE)


class LedgerError(ValueError):
    """Bad input, or a ledger file that can't be read safely."""


class UnknownItem(LookupError):
    """No item with that id."""


class LedgerBusy(RuntimeError):
    """Another process held the ledger lock for longer than the wait."""


# ---- module helpers ------------------------------------------------------------------------------

def format_id(seq: int) -> str:
    return "GS-%04d" % int(seq)


def id_number(item_id: Any) -> Optional[int]:
    m = _ID_RE.match(item_id) if isinstance(item_id, str) else None
    return int(m.group(1)) if m else None


def normalize_id(text: Any) -> str:
    """'7', 'ghosted-7', 'GS-0007' -> 'GS-0007'. Raises LedgerError."""
    m = _LOOSE_ID_RE.match(str(text)) if text is not None else None
    if not m or int(m.group(1)) < 1:
        raise LedgerError("not an item id (expected GS-0001)")
    return format_id(int(m.group(1)))


def public_fields(item: Mapping[str, Any]) -> Dict[str, Any]:
    """The only projection used for event payloads: a whitelist of public fields.

    Never includes local_title, reason, consequential or synced; includes observed_at only for
    interrupted items (the server uses it to ignore older uploads)."""
    out = {k: copy.deepcopy(item[k]) for k in PUBLIC_KEYS if k in item}
    if item.get("kind") == "interrupted" and "observed_at" in item:
        out["observed_at"] = item["observed_at"]
    return out


def ensure_dir(path: Union[str, Path]) -> Path:
    """Create a directory (0700) if missing. Existing directories keep their mode."""
    p = Path(path)
    if p.is_dir():
        return p
    try:
        os.makedirs(str(p), mode=0o700)
    except FileExistsError:
        return p
    os.chmod(str(p), 0o700)  # makedirs is subject to the umask
    return p


def _fix_mode(fd: int) -> None:
    try:
        if os.fstat(fd).st_mode & 0o777 != 0o600:
            os.fchmod(fd, 0o600)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Union[str, Path], data: bytes) -> None:
    """Write `data` to `path` atomically: temp file in the same directory, fsync, os.replace."""
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, str(target))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(target.parent)


def atomic_write_json(path: Union[str, Path], obj: Any) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=True) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8", errors="replace"))


def append_jsonl(path: Union[str, Path], obj: Any, fsync: bool = False) -> None:
    """Append one JSON line with a single os.write on an O_APPEND fd (safe without a lock).

    If the file does not end in a newline (a line torn by a crash or a full disk), the record
    starts with one, so the torn line cannot swallow it; read_jsonl skips the blank and torn
    lines. With `fsync`, the line is on disk before this returns."""
    data = (json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    target = Path(path)
    ensure_dir(target.parent)
    fd = os.open(str(target), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _fix_mode(fd)
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_size and os.pread(fd, 1, info.st_size - 1) != b"\n":
            data = b"\n" + data
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(errno.EIO, "short write appending to %s" % target.name)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Every JSON object line in the file, oldest first. Torn or non-object lines are skipped."""
    try:
        with open(str(path), "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    out: List[Dict[str, Any]] = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(str(path), "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return copy.deepcopy(default)
    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError("%s is damaged (invalid JSON at line %d, column %d); move it aside"
                          % (path.name, exc.lineno, exc.colno)) from None
    except (UnicodeDecodeError, ValueError):
        raise LedgerError("%s is damaged (not UTF-8 JSON); move it aside" % path.name) from None


def default_state() -> Dict[str, Any]:
    return {"version": 1, "since": None, "sessions": {}, "deferred": [], "last_scan": None}


def _real_month_day(mm: str, dd: str) -> bool:
    try:
        date(2000, int(mm), int(dd))  # 2000 is a leap year, so 02-29 counts
    except ValueError:
        return False
    return True


def check_interrupted_title(title: Any, project: Optional[str] = None) -> None:
    """Raise sanitize.Refused unless `title` passes the sanitizer and is exactly the fixed
    template. With `project`, the template's {project} must be that project, so no other text
    can ride along in front of the ': session MM-DD' part."""
    found = sanitize.problems(title)
    if project is not None:
        found.extend("the project name " + p for p in sanitize.project_problems(project))
    if isinstance(title, str) and title.strip():
        m = INTERRUPTED_TITLE_RE.match(title)
        if (m is None or not _real_month_day(m.group("mm"), m.group("dd"))
                or (project is not None and m.group("project") != project)):
            found.append(MSG_TEMPLATE)
    if found:
        raise sanitize.Refused(found)


def _instant_or_none(value: Any) -> Optional[datetime]:
    """parse_iso, or None for a missing or unreadable value (so it can't wedge a comparison)."""
    if not value:
        return None
    try:
        return timeutil.parse_iso(value)
    except ValueError:
        return None


def _utc(now: Optional[datetime]) -> datetime:
    return timeutil.now_utc() if now is None else timeutil.parse_iso(now)


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError("%s is blank" % what)
    return value


def _subset(fields: Mapping[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    return {k: fields[k] for k in keys if k in fields}


def _event(kind: str, stamp: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"event_id": str(uuid.uuid4()), "type": kind, "at": stamp, "item": payload}


def _blank_item(item_id: str, kind: str, stamp: str) -> Dict[str, Any]:
    return {
        "id": item_id, "kind": kind, "project": "", "local_title": "", "title": "",
        "owner": "me", "due_at": None, "review_at": None, "status": "open",
        "snooze_until": None, "dismiss_reason": None, "evidence": None, "looks_resolved": False,
        "resume": None, "session_uuid": None, "content_version": None, "reason": None,
        "consequential": False, "rev": 1, "created_at": stamp, "updated_at": stamp,
        "closed_at": None, "closed_by": None, "observed_at": None, "synced": False,
    }


def _find_session(items: Mapping[str, Dict[str, Any]], session_uuid: str) -> Optional[Dict[str, Any]]:
    matches = [it for it in items.values()
               if it.get("kind") == "interrupted" and it.get("session_uuid") == session_uuid]
    if not matches:
        return None
    return min(matches, key=lambda it: id_number(it.get("id")) or 0)


def _follow(doc: Mapping[str, Any], key: str) -> str:
    """A renumbered id leads to the item's new id (a session may still hold the old one)."""
    seen = set()
    table = doc.get("renumbered") if isinstance(doc.get("renumbered"), dict) else {}
    while key in table and key not in seen and isinstance(table[key], str):
        seen.add(key)
        key = table[key]
    return key


def _same_state(item: Mapping[str, Any], status: str, snooze_until: Optional[str],
                dismiss_reason: Optional[str]) -> bool:
    """The Worker's rule: same status, and for snoozed / dismissed the same snooze_until /
    reason. Anything else is a change (a re-snooze to another time, a corrected reason)."""
    if item.get("status") != status:
        return False
    if status == "snoozed":
        return item.get("snooze_until") == snooze_until
    if status == "dismissed":
        return item.get("dismiss_reason") == dismiss_reason
    return True


def _prior(item: Mapping[str, Any]) -> Dict[str, Any]:
    """The state a status change starts from (carried as the event's prior_* fields)."""
    return {"status": item.get("status"), "snooze_until": item.get("snooze_until"),
            "dismiss_reason": item.get("dismiss_reason")}


_FRESH = {"status": "open", "snooze_until": None, "dismiss_reason": None}  # the server's new item


def _status_event(item: Mapping[str, Any], pre_rev: int, stamp: str,
                  prior: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    payload = _subset(public_fields(item), STATUS_EVENT_KEYS)
    payload["rev"] = pre_rev
    if prior is not None:
        payload["prior_status"] = prior.get("status")
        payload["prior_snooze_until"] = prior.get("snooze_until")
        payload["prior_dismiss_reason"] = prior.get("dismiss_reason")
    return _event("item.status", stamp, payload)


def _creation_event(item: Mapping[str, Any], stamp: str) -> Dict[str, Any]:
    if item.get("kind") == "interrupted":
        return _event("item.upsert", stamp, _subset(public_fields(item), UPSERT_EVENT_KEYS))
    return _event("item.add", stamp, _subset(public_fields(item), ADD_EVENT_KEYS))


def _creation_events(item: Mapping[str, Any], stamp: str) -> List[Dict[str, Any]]:
    """What re-creates an item on a server that lacks it: the creation event, plus (the server
    makes a new item open at rev 1) an item.status at rev 1 carrying any other status on.
    A Mac-only kind (contract 16.9) has none: it is never sent."""
    if item.get("kind") in LOCAL_KINDS:
        return []
    out = [_creation_event(item, stamp)]
    if item.get("status") != "open":
        out.append(_status_event(item, 1, stamp, _FRESH))
    return out


def _later(a: Optional[datetime], b: Optional[datetime]) -> bool:
    """a is a real instant and strictly after b (or b is unknown)."""
    return a is not None and (b is None or a > b)


def _collides(mine: Mapping[str, Any], srv: Mapping[str, Any]) -> bool:
    """The server's item with this id is a different item from the local one: another kind,
    another creation time, or (interrupted) another session. Creation facts never change on
    either side, so any difference means two items were given one id."""
    kind = srv.get("kind")
    if kind in KINDS and mine.get("kind") in KINDS and kind != mine.get("kind"):
        return True
    a, b = _instant_or_none(mine.get("created_at")), _instant_or_none(srv.get("created_at"))
    if a is not None and b is not None and a != b:
        return True
    sa, sb = mine.get("session_uuid"), srv.get("session_uuid")
    return isinstance(sa, str) and isinstance(sb, str) and bool(sa) and bool(sb) and sa != sb


def _legacy_row(key: str, at: Any) -> Dict[str, Any]:
    """A notification row for a claim an older version kept in dedup.json: claimed, outcome
    never recorded (ok null). Kind, local date and item come from the key where it holds them."""
    row: Dict[str, Any] = {"dedup_key": key, "ok": None, "migrated_from": "dedup.json"}
    stamp = _instant_or_none(at)
    if stamp is not None:
        row["sent_at"] = timeutil.to_iso(stamp)
    kind, _, rest = key.partition(":")
    if kind in ("digest", "advance", "deadman"):
        row["kind"] = kind
    if kind == "digest":
        try:
            row["local_date"] = timeutil.check_date(rest)
        except ValueError:
            pass
    elif kind == "advance" and id_number(rest) is not None:
        row["item_ids"] = [rest]
    return row


def _valid_server_value(key: str, value: Any) -> bool:
    if key == "status":
        return value in STATUSES
    if key == "dismiss_reason":
        return value is None or value in DISMISS_REASONS
    if key == "closed_by":
        return value is None or value in CLOSED_BY
    if key == "rev":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if key == "acted_status":
        return value is None or value in STATUSES
    return value is None or isinstance(value, str)  # snooze_until, closed_at, acted_at


def _acted_record(item_id: str, srv: Mapping[str, Any], mine: Optional[Mapping[str, Any]]
                  ) -> Optional[Dict[str, Any]]:
    """The measure.jsonl record for a person's status change the server reports and this Mac
    has not recorded yet (its acted_at / acted_status differ from the local copy's), else None."""
    at, status = srv.get("acted_at"), srv.get("acted_status")
    if not (_valid_server_value("acted_status", status) and status) or _instant_or_none(at) is None:
        return None
    if mine is not None and mine.get("acted_at") == at and mine.get("acted_status") == status:
        return None
    rec: Dict[str, Any] = {"type": MEASURE_STATUS, "id": item_id, "status": status,
                           "at": timeutil.to_iso(timeutil.parse_iso(at))}
    if status == "dismissed" and srv.get("dismiss_reason") in DISMISS_REASONS:
        rec["reason"] = srv["dismiss_reason"]
    return rec


# ---- in-process half of the lock ---------------------------------------------------------------

class _LockState:
    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: Optional[int] = None


_REGISTRY_GUARD = threading.Lock()
_REGISTRY: Dict[str, _LockState] = {}


def _lock_state(path: Path) -> _LockState:
    key = os.path.realpath(str(path))
    with _REGISTRY_GUARD:
        state = _REGISTRY.get(key)
        if state is None:
            state = _REGISTRY[key] = _LockState()
        return state


def _forget_locks_after_fork() -> None:
    """A forked child does not hold its parent's lock: start it with an empty registry, so its
    first lock() takes a fresh flock (and waits for the parent) instead of trusting copied state."""
    global _REGISTRY_GUARD, _REGISTRY
    _REGISTRY_GUARD = threading.Lock()
    _REGISTRY = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_locks_after_fork)


def _busy(wait: float) -> LedgerBusy:
    return LedgerBusy("another ghosted process is holding the ledger lock (waited %.1f s)" % wait)


def _flock(path: Path, deadline: float, wait: float) -> int:
    ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fix_mode(fd)
        delay = 0.005
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, errno.EINTR):
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _busy(wait)
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.1)
    except BaseException:
        os.close(fd)
        raise


# ---- the ledger ----------------------------------------------------------------------------------

class Ledger:
    """All reads come fresh from disk; all writes go through the lock."""

    def __init__(self, cfg: Any, lock_timeout: float = LOCK_TIMEOUT) -> None:
        self.cfg = cfg
        self.home = Path(cfg.home)
        self.lock_timeout = lock_timeout
        self.items_path = self.home / "items.json"
        self.inbox_path = self.home / "inbox.jsonl"
        self.acks_path = self.home / "acks.json"
        self.state_path = self.home / "state.json"
        self.runs_path = self.home / "runs.jsonl"
        self.notifications_path = self.home / "notifications.jsonl"
        self.stops_path = self.home / "stops.jsonl"
        self.measure_path = self.home / "measure.jsonl"
        self.digest_path = self.home / "digest.md"
        self.dedup_path = self.home / "dedup.json"
        self.lock_path = self.home / "lock"
        self.run_lock_path = self.home / "run.lock"

    # -- locking -----------------------------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, timeout: Optional[float] = None) -> Iterator["Ledger"]:
        """Hold the ledger lock (re-entrant in this process). Raises LedgerBusy after the wait."""
        wait = self.lock_timeout if timeout is None else max(0.0, float(timeout))
        state = _lock_state(self.lock_path)
        deadline = time.monotonic() + wait
        if not state.rlock.acquire(timeout=wait):
            raise _busy(wait)
        try:
            if state.depth == 0:
                state.fd = _flock(self.lock_path, deadline, wait)
            state.depth += 1
            try:
                yield self
            finally:
                state.depth -= 1
                if state.depth == 0:
                    fd, state.fd = state.fd, None
                    if fd is not None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            os.close(fd)
        finally:
            state.rlock.release()

    # -- items.json ------------------------------------------------------------------------------

    def _load_doc(self) -> Dict[str, Any]:
        doc = _read_json(self.items_path, None)
        if doc is None:
            # A fresh items.json (first use, or the old one moved aside after damage) starts
            # after every id this home has already queued or been acked for, so a reset ledger
            # can't hand out an id the server already holds for another item.
            return {"version": 1, "next_id": self._seen_seq() + 1, "items": {}}
        if not isinstance(doc, dict) or not isinstance(doc.get("items"), dict):
            raise LedgerError("items.json has an unexpected shape; move it aside")
        if doc.get("version", 1) != 1:
            raise LedgerError("items.json was written by a newer version of ghosted")
        next_id = doc.get("next_id")
        if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
            doc["next_id"] = 1
        return doc

    def _save_doc(self, doc: Dict[str, Any]) -> None:
        ordered = sorted(doc["items"].items(), key=lambda kv: (id_number(kv[0]) or 0, kv[0]))
        doc["items"] = dict(ordered)
        atomic_write_json(self.items_path, doc)

    @staticmethod
    def _next_seq(doc: Dict[str, Any]) -> int:
        seqs = [id_number(k) or 0 for k in doc["items"]]
        seqs.extend(id_number(k) or 0 for k in (doc.get("renumbered") or {}))
        return max([int(doc.get("next_id") or 1)] + [s + 1 for s in seqs])

    def _seen_seq(self) -> int:
        """The highest LE number in inbox.jsonl and acks.json (0 when there is none)."""
        best = 0
        for ev in read_jsonl(self.inbox_path):
            payload = ev.get("item")
            if isinstance(payload, dict):
                best = max(best, id_number(payload.get("id")) or 0)
        try:
            acks = _read_json(self.acks_path, None)
        except LedgerError:
            acks = None
        events = acks.get("events") if isinstance(acks, dict) else None
        if isinstance(events, dict):
            for rec in events.values():
                if isinstance(rec, dict):
                    best = max(best, id_number(rec.get("item_id")) or 0)
        return best

    def _review_at(self, now: datetime) -> str:
        today = timeutil.local_date(now, self.cfg.timezone)
        return (today + timedelta(days=int(self.cfg.review_days))).isoformat()

    def _append_event(self, event: Dict[str, Any]) -> None:
        # fsync: items.json was just fsync'd, so without this a power loss could keep the item
        # and drop its only event, and the item would never reach the server.
        append_jsonl(self.inbox_path, event, fsync=True)

    # -- reads -------------------------------------------------------------------------------------

    def items(self) -> Dict[str, Dict[str, Any]]:
        """id -> item, fresh from disk, ordered by id."""
        return self._load_doc()["items"]

    def get(self, item_id: Any) -> Optional[Dict[str, Any]]:
        try:
            key = normalize_id(item_id)
        except LedgerError:
            return None
        return self._load_doc()["items"].get(key)

    def find_by_session(self, session_uuid: str) -> Optional[Dict[str, Any]]:
        """The interrupted item for a session (lowest id if, unexpectedly, there are several)."""
        return _find_session(self._load_doc()["items"], session_uuid)

    def open_items(self) -> List[Dict[str, Any]]:
        """Items with status open or snoozed, as a list ordered by id."""
        rows = [it for it in self.items().values() if it.get("status") in OPEN_STATUSES]
        return sorted(rows, key=lambda it: id_number(it.get("id")) or 0)

    def has_server_view(self) -> bool:
        """True once a sync has told this ledger the server's max_seq (so ids it assigns from now
        on start after every id the server holds)."""
        return "server_max_seq" in self._load_doc()

    def renumbered(self) -> Dict[str, str]:
        """{old id: new id} for items this Mac renumbered because the server already held
        their id for another item (see merge_server_items)."""
        found = self._load_doc().get("renumbered")
        return dict(found) if isinstance(found, dict) else {}

    # -- mutations ---------------------------------------------------------------------------------

    def add(self, kind: str, project: str, local_title: str, title: str, owner: str = "me",
            due_at: Optional[str] = None, review_at: Optional[str] = None,
            consequential: bool = False, now: Optional[datetime] = None,
            known_before_pilot: bool = False
            ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Create a promise/decision/miss and queue its item.add event. Refuses (sanitize.Refused)
        a title or a project name the sanitizer flags: both are uploaded.

        An open or snoozed item of the same kind, project and public title is not created twice
        (a daily automated job that keeps promising the same check, or a session repeating
        itself): that item comes back with event None, whatever its due date.
        ``consequential`` and ``known_before_pilot`` are Mac-only fields, never uploaded."""
        now = _utc(now)
        if kind not in ADD_KINDS:
            raise LedgerError("kind must be promise, decision or miss (interrupted items come from "
                              "the detector)")
        if owner not in OWNERS:
            raise LedgerError("owner must be me or claude")
        _text(project, "project")
        _text(local_title, "local title")
        found = sanitize.project_problems(project)
        if found:
            raise sanitize.Refused(["the project name " + p for p in found])
        sanitize.check(title)
        if due_at is not None:
            timeutil.check_due(due_at)
        if review_at is not None:
            timeutil.check_date(review_at)
        elif due_at is None:
            review_at = self._review_at(now)
        stamp = timeutil.to_iso(now)
        with self.lock():
            doc = self._load_doc()
            for other in doc["items"].values():
                if (other.get("status") in OPEN_STATUSES and other.get("kind") == kind
                        and other.get("project") == project and other.get("title") == title):
                    return copy.deepcopy(other), None
            seq = self._next_seq(doc)
            item = _blank_item(format_id(seq), kind, stamp)
            item.update(project=project, local_title=local_title, title=title, owner=owner,
                        due_at=due_at, review_at=review_at, consequential=bool(consequential))
            if consequential:
                item["consequential_set_at"] = stamp
            if known_before_pilot:
                item["known_before_pilot"] = True
            doc["items"][item["id"]] = item
            doc["next_id"] = seq + 1
            event = _event("item.add", stamp, _subset(public_fields(item), ADD_EVENT_KEYS))
            self._save_doc(doc)
            self._append_event(event)
        return copy.deepcopy(item), event

    def upsert_interrupted(self, session_uuid: str, content_version: Optional[str], project: str,
                           title: str, evidence: Optional[str], looks_resolved: bool,
                           resume: Optional[str], reason: Optional[str],
                           observed_at: Union[str, datetime, None],
                           now: Optional[datetime] = None,
                           interrupted_at: Union[str, datetime, None] = None,
                           ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Insert the session's interrupted item, or refresh its Mac-owned fields.

        Returns (item, None) when none of title, evidence, looks_resolved, content_version, resume
        changed and no newer flagged ending came in; observed_at is still advanced. An
        observation older than (or as old as) the stored one changes nothing, mirroring the
        server.

        ``interrupted_at`` is when the session's flagged ending happened (the final assistant
        text's timestamp; None for a clean ending or an evidence refresh). A flagged ending
        newer than the item's close reopens a done or dismissed item (status open, rev + 1,
        close fields cleared); one newer than the stored ending un-resolves an open item that
        looked resolved. Both stamp ``reopened_at`` (Mac only), which the local push rules read
        as the item's T4 time, so the new interruption reaches the next digest. The server
        applies the same rule to the same upsert (contract 6 and 14). Nothing else ever
        touches status or rev here."""
        now = _utc(now)
        _text(session_uuid, "session uuid")
        _text(project, "project")
        check_interrupted_title(title, project)
        observed = timeutil.to_iso(now if observed_at is None else timeutil.parse_iso(observed_at))
        ended = None if interrupted_at is None else timeutil.to_iso(timeutil.parse_iso(interrupted_at))
        stamp = timeutil.to_iso(now)
        incoming = {"title": title, "evidence": evidence, "looks_resolved": bool(looks_resolved),
                    "content_version": content_version, "resume": resume}
        with self.lock():
            doc = self._load_doc()
            item = _find_session(doc["items"], session_uuid)
            if item is None:
                seq = self._next_seq(doc)
                item = _blank_item(format_id(seq), "interrupted", stamp)
                item.update(incoming)
                item.update(project=project, local_title=title, session_uuid=session_uuid,
                            reason=reason, observed_at=observed, review_at=self._review_at(now),
                            interrupted_at=ended)
                doc["items"][item["id"]] = item
                doc["next_id"] = seq + 1
                event: Optional[Dict[str, Any]] = _creation_event(item, stamp)
                self._save_doc(doc)
                self._append_event(event)
                return copy.deepcopy(item), event

            stored = _instant_or_none(item.get("observed_at"))
            if stored is not None and timeutil.parse_iso(observed) <= stored:
                return copy.deepcopy(item), None  # not newer: the server would ignore it too
            item["observed_at"] = observed
            item["reason"] = reason
            item.setdefault("interrupted_at", None)
            end_at = _instant_or_none(ended)
            newer_end = _later(end_at, _instant_or_none(item.get("interrupted_at")))
            event = None
            if newer_end or any(item.get(k) != v for k, v in incoming.items()):
                reflag = self._reflag(item, end_at, bool(looks_resolved))
                item.update(incoming)
                item.update(project=project, local_title=title, updated_at=stamp)
                if newer_end:
                    item["interrupted_at"] = ended
                if reflag == "reopen":
                    item.update(status="open", rev=int(item.get("rev") or 1) + 1, snooze_until=None,
                                dismiss_reason=None, closed_at=None, closed_by=None)
                if reflag is not None:
                    item["reopened_at"] = stamp
                event = _creation_event(item, stamp)
            self._save_doc(doc)
            if event is not None:
                self._append_event(event)
            return copy.deepcopy(item), event

    @staticmethod
    def _reflag(item: Mapping[str, Any], ended: Optional[datetime], looks_resolved: bool) -> Optional[str]:
        """'reopen' / 'unresolve' when a flagged ending at ``ended`` is news for ``item`` (see
        upsert_interrupted), else None. The server's item.upsert applies the same test."""
        if ended is None or looks_resolved:
            return None
        if item.get("status") in CLOSED_STATUSES:
            closed = _instant_or_none(item.get("closed_at")) or _instant_or_none(item.get("updated_at"))
            return "reopen" if _later(ended, closed) else None
        if item.get("looks_resolved") and _later(ended, _instant_or_none(item.get("interrupted_at"))):
            return "unresolve"
        return None

    # -- unsaved work (contract 16.9): Mac-only items, one per repo, no events ------------------------

    @staticmethod
    def _find_repo(items: Mapping[str, Dict[str, Any]], repo: str) -> Optional[Dict[str, Any]]:
        found = [it for it in items.values() if it.get("kind") == "unsaved" and it.get("repo") == repo]
        return min(found, key=lambda it: id_number(it.get("id")) or 0) if found else None

    def find_by_repo(self, repo: str) -> Optional[Dict[str, Any]]:
        return self._find_repo(self._load_doc()["items"], repo)

    def upsert_unsaved(self, repo: str, project: str, summary: Mapping[str, Any],
                       now: Optional[datetime] = None) -> Tuple[Dict[str, Any], Optional[str]]:
        """The repo has unsaved work (``summary`` = ``unsaved.summary_of``: flags + ages). Creates
        the repo's item, refreshes it, or reopens it (a done item; a dismissed one only when some
        count went above what was dismissed). Returns (item, "created" | "reopened" | "updated" |
        None). Never an inbox event."""
        now = _utc(now)
        stamp = timeutil.to_iso(now)
        flags = dict(summary.get("flags") or {})
        kept = {"flags": flags, "uncommitted_since": summary.get("uncommitted_since"),
                "unpushed_since": summary.get("unpushed_since")}
        for key in ("dirty", "owners"):  # mixed dirt (contract 16.11): counts and session ids only
            if summary.get(key) is not None:
                kept[key] = copy.deepcopy(summary[key])
        with self.lock():
            doc = self._load_doc()
            item = self._find_repo(doc["items"], repo)
            change: Optional[str] = None
            if item is None:
                seq = self._next_seq(doc)
                item = _blank_item(format_id(seq), "unsaved", stamp)
                title = "%s: unsaved work" % project
                item.update(project=project, local_title=title, title=title, repo=repo,
                            unsaved=kept, dismissed_counts=None)
                doc["items"][item["id"]] = item
                doc["next_id"] = seq + 1
                change = "created"
            else:
                reopen = False
                if item.get("status") == "done":
                    reopen = True
                elif item.get("status") == "dismissed":
                    before = item.get("dismissed_counts") or {}
                    reopen = any(int(v) > int(before.get(k) or 0) for k, v in flags.items())
                if reopen:
                    item.update(status="open", rev=int(item.get("rev") or 1) + 1, closed_at=None,
                                closed_by=None, dismiss_reason=None, snooze_until=None,
                                dismissed_counts=None, reopened_at=stamp)
                    change = "reopened"
                if item.get("unsaved") != kept:
                    item["unsaved"] = kept
                    change = change or "updated"
                if change:
                    item["updated_at"] = stamp
            if change:
                self._save_doc(doc)
            return copy.deepcopy(item), change

    def close_unsaved(self, repo: str, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """The repo has nothing unsaved now: its open, snoozed or dismissed item becomes done
        (closed_by "scan"), so the next unsaved state is a new loose end. Returns the item closed."""
        now = _utc(now)
        stamp = timeutil.to_iso(now)
        with self.lock():
            doc = self._load_doc()
            item = self._find_repo(doc["items"], repo)
            if item is None or item.get("status") == "done":
                return None
            item.update(status="done", rev=int(item.get("rev") or 1) + 1, closed_at=stamp,
                        closed_by=CLOSED_BY_SCAN, snooze_until=None, dismiss_reason=None,
                        dismissed_counts=None, updated_at=stamp)
            self._save_doc(doc)
            return copy.deepcopy(item)

    def set_status(self, item_id: Any, status: str, now: Optional[datetime] = None,
                   snooze_until: Optional[str] = None, dismiss_reason: Optional[str] = None,
                   closed_by: str = "session"
                   ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Apply a status transition locally (rev + 1) and queue item.status with the PRE-change
        rev. done/dismissed set closed_at/closed_by; any other status clears them; snooze_until
        and dismiss_reason are kept only for snoozed/dismissed, so a manual reopen clears a kept
        snooze_until. Asking for the state the item already has (same status, and for snoozed /
        dismissed the same snooze_until / reason) is a no-op returning (item, None), as the
        server treats it as a duplicate; a re-snooze to another time or a corrected dismiss
        reason is a change (rev + 1), as on the server. The event is appended before items.json
        is saved (see the module doc). A Mac-only kind (``unsaved``, contract 16.9) gets no inbox
        event: the second value is then ``{"local": True, "status": status}``."""
        now = _utc(now)
        if status not in STATUSES:
            raise LedgerError("status must be one of %s" % ", ".join(STATUSES))
        if status == "snoozed":
            if not snooze_until:
                raise LedgerError("snoozing needs snooze_until (local YYYY-MM-DDTHH:MM)")
            timeutil.check_wall_minute(snooze_until)
        elif snooze_until is not None:
            raise LedgerError("snooze_until only applies to status snoozed")
        if status == "dismissed":
            if dismiss_reason not in DISMISS_REASONS:
                raise LedgerError("dismissing needs a reason: not_real or handled_elsewhere")
        elif dismiss_reason is not None:
            raise LedgerError("dismiss_reason only applies to status dismissed")
        if status in CLOSED_STATUSES and closed_by not in CLOSED_BY:
            raise LedgerError("closed_by must be phone or session")
        key = normalize_id(item_id)
        stamp = timeutil.to_iso(now)
        with self.lock():
            doc = self._load_doc()
            key = _follow(doc, key)
            item = doc["items"].get(key)
            if item is None:
                raise UnknownItem("%s not found" % key)
            if _same_state(item, status, snooze_until, dismiss_reason):
                return copy.deepcopy(item), None
            pre_rev = int(item.get("rev") or 1)
            prior = _prior(item)
            closing = status in CLOSED_STATUSES
            item.update(status=status, rev=pre_rev + 1, updated_at=stamp,
                        snooze_until=snooze_until if status == "snoozed" else None,
                        dismiss_reason=dismiss_reason if status == "dismissed" else None,
                        closed_at=stamp if closing else None,
                        closed_by=closed_by if closing else None,
                        acted_at=stamp, acted_status=status)
            if item.get("kind") in LOCAL_KINDS:
                # Mac only: no event. The status change still counts (rev, acted_*), and a
                # dismissal remembers what it dismissed, so a bigger count brings it back.
                if status == "dismissed":
                    item["dismissed_counts"] = dict((item.get("unsaved") or {}).get("flags") or {})
                else:
                    item["dismissed_counts"] = None
                self._save_doc(doc)
                return copy.deepcopy(item), {"local": True, "status": status}
            event = _status_event(item, pre_rev, stamp, prior)
            self._append_event(event)
            self._save_doc(doc)
        return copy.deepcopy(item), event

    def resolve_id(self, item_id: Any) -> Tuple[str, Optional[str]]:
        """(the id to act on, the id that was asked for when it was renumbered, else None)."""
        key = normalize_id(item_id)
        doc = self._load_doc()
        target = _follow(doc, key)
        return target, (key if target != key else None)

    def reopen_expired(self, ids: Optional[Iterable[Any]] = None, now: Optional[datetime] = None
                       ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Snooze expiry (contract 12): every snoozed item whose snooze_until has passed (local
        wall time in cfg.timezone, PEP 495 fold=0) goes back to open, rev + 1, and KEEPS
        snooze_until, so the push rules still count it as reopened today at later ticks (T5).
        A manual reopen (`set_status(id, "open")`) clears it instead.

        `ids` limits this to those items (what core.schedule.decide listed in `reopen`); None
        means every expired snooze. Each item is checked again under the lock, so one snoozed
        again in the meantime stays snoozed. One item.status event per reopened item, carrying
        the pre-change rev (the Worker accepts the kept snooze_until with status open). Returns
        [(item, event)] in id order."""
        now = _utc(now)
        stamp = timeutil.to_iso(now)
        wanted: Optional[set] = None
        if ids is not None:
            wanted = set()
            for raw in ids:
                try:
                    wanted.add(normalize_id(raw))
                except LedgerError:
                    continue
        out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        with self.lock():
            doc = self._load_doc()
            for key, item in doc["items"].items():
                if (wanted is not None and key not in wanted) or item.get("status") != "snoozed":
                    continue
                until = item.get("snooze_until")
                try:
                    due = timeutil.due_instant(until, self.cfg.timezone) if until else None
                except (ValueError, OverflowError):
                    due = None
                if due is None or due > now:
                    continue
                pre_rev = int(item.get("rev") or 1)
                prior = _prior(item)
                item.update(status="open", rev=pre_rev + 1, updated_at=stamp, dismiss_reason=None,
                            closed_at=None, closed_by=None)  # snooze_until stays; not an act
                out.append((item, _status_event(item, pre_rev, stamp, prior)))
            if out:
                for item, event in out:
                    if item.get("kind") not in LOCAL_KINDS:  # Mac-only items make no events
                        self._append_event(event)
                self._save_doc(doc)
        return [(copy.deepcopy(item), event) for item, event in out]

    def merge_server_items(self, items: Iterable[Mapping[str, Any]], max_seq: Optional[int] = None,
                           now: Optional[datetime] = None, conflicts: Iterable[str] = ()) -> Dict[str, Any]:
        """Take the server's status fields (status, snooze_until, dismiss_reason, closed_at,
        closed_by, rev, acted_at, acted_status) and keep everything else local. Unknown server
        items are added with local_title = title. next_id moves past max_seq, which is kept as
        ``server_max_seq`` (has_server_view). Appends no item events.

        Two items with one id: a server item whose kind, created_at or session differs from the
        local item with its id (or a local item whose creation the server answered "conflict",
        listed in ``conflicts``) means the server already held that id for another item (a
        reset ledger, or a second GHOSTED_HOME on the same Worker). The local item is never
        given the other item's status: it moves to a fresh id past max_seq (``renumbered``, so
        `ghosted done <old id>` still finds it), its queued events under the old id are dropped
        (recorded as conflict acks, never sent), and its creation is queued again under the new
        id. The server's item then comes in under the old id like any unknown item.

        A person's status change (acted_at/acted_status changed on the server) is logged to
        measure.jsonl as ``{"type": "status", "id", "status", "reason", "at"}`` for `ghosted report`.
        Returns {"added": [ids], "changed": [ids], "next_id": n, "renumbered": [(old, new)]}."""
        now = _utc(now)
        stamp = timeutil.to_iso(now)
        added: List[str] = []
        changed: List[str] = []
        moved: List[Tuple[str, str]] = []
        refused = {c for c in conflicts if isinstance(c, str)}
        floor = max_seq if isinstance(max_seq, int) and not isinstance(max_seq, bool) else 0
        rows = [srv for srv in (items or []) if isinstance(srv, dict) and id_number(srv.get("id")) is not None]
        with self.lock():
            doc = self._load_doc()
            local = doc["items"]
            floor = max([floor] + [id_number(srv["id"]) or 0 for srv in rows])
            clash = set(refused & set(local))
            for srv in rows:
                mine = local.get(srv["id"])
                if mine is not None and _collides(mine, srv):
                    clash.add(srv["id"])
            new_events: List[Dict[str, Any]] = []
            for old_id in sorted(clash, key=lambda i: (id_number(i) or 0, i)):
                new_id, events = self._renumber(doc, old_id, floor, stamp)
                moved.append((old_id, new_id))
                new_events.extend(events)
            records: List[Dict[str, Any]] = []
            for srv in rows:
                sid = srv["id"]
                mine = local.get(sid)
                if mine is None:
                    local[sid] = self._item_from_server(srv, stamp)
                    added.append(sid)
                    rec = _acted_record(sid, srv, None)
                    if rec is not None:
                        records.append(rec)
                    continue
                rec = _acted_record(sid, srv, mine)
                if rec is not None:
                    records.append(rec)
                diff = False
                for k in SERVER_OWNED_FIELDS:
                    if k in srv and _valid_server_value(k, srv[k]) and mine.get(k) != srv[k]:
                        mine[k] = srv[k]
                        diff = True
                mine["synced"] = True
                if diff:
                    mine["updated_at"] = stamp
                    changed.append(sid)
            seqs = [id_number(k) or 0 for k in local]
            candidates = [int(doc.get("next_id") or 1)] + [s + 1 for s in seqs]
            if isinstance(max_seq, int) and not isinstance(max_seq, bool):
                candidates.append(max_seq + 1)
                doc["server_max_seq"] = max(max_seq, int(doc.get("server_max_seq") or 0))
            doc["next_id"] = max(candidates)
            if moved:
                self._drop_events_for([old for old, _ in moved], moved, stamp)
            self._save_doc(doc)
            for event in new_events:
                self._append_event(event)
            for rec in records:
                self.append_measure(rec)
            return {"added": added, "changed": changed, "next_id": doc["next_id"], "renumbered": moved}

    def _renumber(self, doc: Dict[str, Any], old_id: str, floor: int, stamp: str
                  ) -> Tuple[str, List[Dict[str, Any]]]:
        """Move ``old_id`` to a fresh id past ``floor`` (call under the lock, then save)."""
        item = doc["items"].pop(old_id)
        seq = max(self._next_seq(doc), floor + 1, (id_number(old_id) or 0) + 1)
        new_id = format_id(seq)
        doc["next_id"] = seq + 1
        if item.get("kind") in LOCAL_KINDS:
            # Mac only (contract 16.9): a new id, nothing for the server, the status history kept.
            item.update(id=new_id, updated_at=stamp)
        else:
            # The server will create it open at rev 1, then apply its status (rev 2) if any other.
            item.update(id=new_id, synced=False, updated_at=stamp,
                        rev=1 if item.get("status") == "open" else 2)
        doc["items"][new_id] = item
        table = doc.get("renumbered") if isinstance(doc.get("renumbered"), dict) else {}
        for key, value in list(table.items()):
            if value == old_id:
                table[key] = new_id
        table[old_id] = new_id
        doc["renumbered"] = table
        return new_id, _creation_events(item, stamp)

    def _drop_events_for(self, old_ids: Iterable[str], moved: Iterable[Tuple[str, str]], stamp: str) -> None:
        """Record a local conflict ack for every queued event of the old ids (never sent)."""
        to = dict(moved)
        doc_acks = self._load_acks()
        acked = doc_acks["events"]
        wanted = set(old_ids)
        touched = False
        for ev in self.events():
            payload = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            item_id = payload.get("id")
            if ev["event_id"] in acked or item_id not in wanted:
                continue
            acked[ev["event_id"]] = {"result": "conflict", "item_id": item_id, "rev": None, "at": stamp,
                                     "error": "the server holds this id for another item; now %s" % to.get(item_id)}
            touched = True
        if touched:
            atomic_write_json(self.acks_path, doc_acks)

    @staticmethod
    def _item_from_server(srv: Mapping[str, Any], stamp: str) -> Dict[str, Any]:
        kind = srv.get("kind") if srv.get("kind") in KINDS else "promise"
        item = _blank_item(srv["id"], kind, stamp)
        for k in PUBLIC_KEYS:
            if k in srv and k not in ("id", "kind"):
                if k in SERVER_OWNED_FIELDS and not _valid_server_value(k, srv[k]):
                    continue
                item[k] = copy.deepcopy(srv[k])
        observed = _instant_or_none(srv.get("observed_at")) if kind == "interrupted" else None
        if observed is not None:
            item["observed_at"] = timeutil.to_iso(observed)
        ended = _instant_or_none(srv.get("interrupted_at")) if kind == "interrupted" else None
        item["interrupted_at"] = timeutil.to_iso(ended) if ended is not None else None
        if kind != "interrupted":
            item.pop("interrupted_at", None)
        title = srv.get("title")
        item["local_title"] = title if isinstance(title, str) else ""
        item["synced"] = True
        return item

    # -- inbox and acks ----------------------------------------------------------------------------

    def events(self) -> List[Dict[str, Any]]:
        """Every inbox event, oldest first (one per event_id)."""
        seen = set()
        out = []
        for ev in read_jsonl(self.inbox_path):
            eid = ev.get("event_id")
            if isinstance(eid, str) and eid not in seen:
                seen.add(eid)
                out.append(ev)
        return out

    def items_missing_events(self) -> List[str]:
        """Ids of unsynced items with no item.add / item.upsert event in the inbox, in id order.

        Only a crash between saving items.json and appending the creation event leaves one (see
        the module doc); such an item would never reach the server. `ghosted doctor` reports them and
        `ghosted doctor --fix` queues them again (`reemit_events`)."""
        created = set()
        for ev in self.events():
            payload = ev.get("item")
            if ev.get("type") in ("item.add", "item.upsert") and isinstance(payload, dict):
                if isinstance(payload.get("id"), str):
                    created.add(payload["id"])
        return [item_id for item_id, item in self.items().items()
                if not item.get("synced") and item_id not in created
                and item.get("kind") not in LOCAL_KINDS]

    def reemit_events(self, ids: Iterable[Any], now: Optional[datetime] = None,
                      force: bool = False) -> List[Dict[str, Any]]:
        """Queue a creation event (item.add, or item.upsert for an interrupted item) for each of
        `ids` that `items_missing_events()` still lists; others are left alone. The server
        creates an item open at rev 1, so an item whose local status is no longer open also gets
        an item.status event with rev 1 that carries its status on (the merge after the next
        sync then takes the server's rev). Returns the events appended, oldest first.

        ``force`` (`ghosted resync`): queue them for any of `ids`, synced or not, and mark those
        items unsynced: the server lost them (a D1 restore, a recreated database)."""
        now = _utc(now)
        stamp = timeutil.to_iso(now)
        wanted = set()
        for raw in ids:
            try:
                wanted.add(normalize_id(raw))
            except LedgerError:
                continue
        appended: List[Dict[str, Any]] = []
        with self.lock():
            doc = self._load_doc()
            items = doc["items"]
            if force:
                chosen = {i for i in wanted if i in items and items[i].get("kind") not in LOCAL_KINDS}
                for item_id in chosen:
                    items[item_id]["synced"] = False
                if chosen:
                    self._save_doc(doc)
            else:
                chosen = set(self.items_missing_events()) & wanted
            for item_id in sorted(chosen, key=lambda i: (id_number(i) or 0, i)):
                appended.extend(_creation_events(items[item_id], stamp))
            for event in appended:
                self._append_event(event)
        return appended

    def _load_acks(self) -> Dict[str, Any]:
        acks = _read_json(self.acks_path, {"events": {}, "runs": []})
        if not isinstance(acks, dict):
            raise LedgerError("acks.json has an unexpected shape; move it aside")
        if not isinstance(acks.get("events"), dict):
            acks["events"] = {}
        if not isinstance(acks.get("runs"), list):
            acks["runs"] = []
        return acks

    def acks(self) -> Dict[str, Any]:
        """The acks.json document: {"events": {event_id: {result, item_id, rev, at}}, "runs": [...]}."""
        return self._load_acks()

    def unacked_events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Inbox events the server has not acknowledged, oldest first."""
        acked = self._load_acks()["events"]
        out = [ev for ev in self.events() if ev["event_id"] not in acked]
        return out[:limit] if limit is not None else out

    def record_acks(self, acks: Mapping[str, Mapping[str, Any]], now: Optional[datetime] = None) -> None:
        """Store the server's acks. An applied/duplicate ack of an item.add or item.upsert marks
        that item synced. Acked events (any result, including stale and invalid) are not resent."""
        if not acks:
            return
        stamp = timeutil.to_iso(_utc(now))
        with self.lock():
            doc_acks = self._load_acks()
            by_id = {ev["event_id"]: ev for ev in self.events()}
            synced: List[str] = []
            for event_id, ack in acks.items():
                if not isinstance(event_id, str) or not isinstance(ack, dict):
                    continue
                rec: Dict[str, Any] = {"result": ack.get("result"), "item_id": ack.get("item_id"),
                                       "rev": ack.get("rev"), "at": stamp}
                if ack.get("error"):
                    rec["error"] = str(ack["error"])[:200]
                doc_acks["events"][event_id] = rec
                ev = by_id.get(event_id)
                if (ev is not None and ev.get("type") in ("item.add", "item.upsert")
                        and ack.get("result") in ("applied", "duplicate")):
                    item_id = (ev.get("item") or {}).get("id")
                    if isinstance(item_id, str):
                        synced.append(item_id)
            atomic_write_json(self.acks_path, doc_acks)
            if synced:
                doc = self._load_doc()
                touched = False
                for item_id in synced:
                    item = doc["items"].get(item_id)
                    if item is not None and not item.get("synced"):
                        item["synced"] = True
                        touched = True
                if touched:
                    self._save_doc(doc)

    def unacked_runs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        acked = set(self._load_acks()["runs"])
        out = [r for r in self.runs() if r.get("run_id") not in acked]
        return out[:limit] if limit is not None else out

    def record_runs_acked(self, ids: Iterable[str]) -> None:
        ids = [i for i in ids if isinstance(i, str)]
        if not ids:
            return
        with self.lock():
            doc_acks = self._load_acks()
            known = set(doc_acks["runs"])
            for run_id in ids:
                if run_id not in known:
                    doc_acks["runs"].append(run_id)
                    known.add(run_id)
            atomic_write_json(self.acks_path, doc_acks)

    # -- append-only logs --------------------------------------------------------------------------

    def append_run(self, run: Mapping[str, Any]) -> None:
        append_jsonl(self.runs_path, dict(run))

    def runs(self) -> List[Dict[str, Any]]:
        return read_jsonl(self.runs_path)

    def append_notification(self, notification: Mapping[str, Any]) -> None:
        """Append a notification row: a send's outcome after `claim_dedup` (same dedup_key), or a
        row mirrored from the server."""
        append_jsonl(self.notifications_path, dict(notification))

    def notification_rows(self) -> List[Dict[str, Any]]:
        """notifications.jsonl exactly as written: claim rows and outcome rows alike."""
        return read_jsonl(self.notifications_path)

    def notifications(self) -> List[Dict[str, Any]]:
        """One row per notification, oldest first (contract 12). Rows sharing a dedup_key are
        one notification: the last row wins, and fields it lacks come from the earlier ones; so
        a claim whose outcome was never written reads as ok null. Rows without a dedup_key are
        kept as they are. Claims still waiting in a dedup.json from an older version are listed
        as ok-null rows (nothing is written here)."""
        out: List[Dict[str, Any]] = []
        at: Dict[str, int] = {}
        for row in read_jsonl(self.notifications_path):
            key = row.get("dedup_key")
            if not isinstance(key, str) or not key:
                out.append(row)
            elif key in at:
                out[at[key]].update(row)
            else:
                at[key] = len(out)
                out.append(row)
        try:
            legacy = self._legacy_claims()
        except LedgerError:
            legacy = None  # claim_dedup reports a damaged dedup.json; reads stay usable
        for key, when in (legacy or {}).items():
            if key not in at:
                out.append(_legacy_row(key, when))
        return out

    def claim_dedup(self, key: str, now: Optional[datetime] = None,
                    row: Optional[Mapping[str, Any]] = None) -> bool:
        """Claim a notification before sending it: True exactly once per key, and only then may
        the caller send (contract 12). The claim is a notifications.jsonl row, `row`'s fields
        plus `{"dedup_key": key, "ok": null}` (and `sent_at` = now unless given), appended and
        fsync'd under the lock, so a crash after the claim can never send the push twice. After
        sending, record the outcome with `append_notification` (same dedup_key, ok true/false).
        A key is taken once any row carries it. See the module doc for dedup.json."""
        if not isinstance(key, str) or not key:
            raise LedgerError("dedup key must be a non-empty string")
        stamp = timeutil.to_iso(_utc(now))
        claim = dict(row or {})
        claim.update(dedup_key=key, ok=None)
        if not claim.get("sent_at"):
            claim["sent_at"] = stamp
        with self.lock():
            self._migrate_dedup()
            if any(n.get("dedup_key") == key for n in read_jsonl(self.notifications_path)):
                return False
            append_jsonl(self.notifications_path, claim, fsync=True)
            return True

    def _legacy_claims(self) -> Optional[Dict[str, Any]]:
        """dedup.json's {key: claimed_at} (written by versions before the claim rows), or None
        when there is no such file. Raises LedgerError for a damaged one."""
        doc = _read_json(self.dedup_path, None)
        if doc is None:
            return None
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), dict):
            raise LedgerError("dedup.json has an unexpected shape; move it aside")
        return {k: v for k, v in doc["keys"].items() if isinstance(k, str) and k}

    def _migrate_dedup(self) -> None:
        """Move dedup.json's claims into notifications.jsonl as ok-null rows (call under the
        lock), then remove dedup.json. Safe to repeat after a crash at any point."""
        legacy = self._legacy_claims()
        if legacy is None:
            return
        have = {n.get("dedup_key") for n in read_jsonl(self.notifications_path)}
        for key, when in legacy.items():
            if key not in have:
                append_jsonl(self.notifications_path, _legacy_row(key, when), fsync=True)
                have.add(key)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(self.dedup_path))
        _fsync_dir(self.home)

    def append_measure(self, rec: Mapping[str, Any]) -> None:
        append_jsonl(self.measure_path, dict(rec))

    def measures(self) -> List[Dict[str, Any]]:
        return read_jsonl(self.measure_path)

    def stops(self) -> List[Dict[str, Any]]:
        """stops.jsonl rows (written lock-free by the hook)."""
        return read_jsonl(self.stops_path)

    # -- detector state and digest -----------------------------------------------------------------

    def load_state(self) -> Dict[str, Any]:
        state = _read_json(self.state_path, None)
        if state is None:
            return default_state()
        if not isinstance(state, dict):
            raise LedgerError("state.json has an unexpected shape; move it aside")
        for key, value in default_state().items():
            state.setdefault(key, value)
        return state

    def save_state(self, state: Mapping[str, Any]) -> None:
        with self.lock():
            atomic_write_json(self.state_path, dict(state))

    def write_digest(self, text: str) -> None:
        atomic_write_text(self.digest_path, text)
