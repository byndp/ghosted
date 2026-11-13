"""Live agent state from Claude Code itself (contract 16.3 "Live state").

``live(timeout=3.0) -> Optional[Dict[sessionId, LiveState]]`` runs ``claude agents --json --all``
(no shell, ``claude`` from PATH) and reads its JSON array. Two entry shapes are known:

* interactive: ``{sessionId, cwd, kind: "interactive", startedAt, name, pid, status: "idle"|"busy"}``
* background: ``{id, sessionId, cwd, kind: "background", startedAt, name, state: "blocked"|"done"|"working"...}``

Only ``kind``, the normalised state (``status`` for interactive, ``state`` for background) and ``pid``
are kept. ``name`` is never read: Claude Code can derive it from the conversation, so it is
transcript text. ``cwd`` is not needed and not kept either.

``classify_live(entry)``: ``"active"`` (``ACTIVE_STATES``: the report skips the row and counts it as
active), ``"waiting"`` (``WAITING_STATES``: tier waiting, reason ``agent_blocked``, even when the
transcript ended clean), ``"open"`` (an interactive ``idle`` entry with a pid: the session is open in
a terminal), else None. ``WAITING_STATES`` / ``ACTIVE_STATES`` are the single place to extend when
the platform ships a "waiting on you" status.

``live`` never raises: ``claude`` missing, a non-zero exit, a timeout, output that is not a JSON array,
or any other failure gives None (no live signal), which callers treat as "unknown", not "nothing".
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

ACTIVE = "active"
WAITING = "waiting"
OPEN = "open"

WAITING_STATES = frozenset(("blocked", "waiting", "needs_input", "needs-input", "awaiting_input"))
ACTIVE_STATES = frozenset(("busy", "working", "running", "starting"))
IDLE = "idle"

KIND_INTERACTIVE = "interactive"
KIND_BACKGROUND = "background"

DEFAULT_TIMEOUT = 3.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # a listing larger than this is not believed
MAX_SESSION_ID = 128
_MAX_STATE = 64
COMMAND = ("agents", "--json", "--all")

# Which signal wins when one session has several entries: a platform "waiting on you" first,
# then "still working", then "open in a terminal".
_RANK = {WAITING: 3, ACTIVE: 2, OPEN: 1, None: 0}


@dataclass(frozen=True)
class LiveState:
    """One session's live entry: ``kind`` (``interactive`` / ``background`` / whatever the
    platform sends, normalised), ``state`` (normalised status, or None), ``pid`` (int or None)."""

    kind: Optional[str]
    state: Optional[str]
    pid: Optional[int] = None


def _norm(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or len(text) > _MAX_STATE:
        return None
    return text


def _pid(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _session_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_SESSION_ID or any(c.isspace() for c in text):
        return None
    return text


def state_of(entry: Mapping[str, Any]) -> LiveState:
    """The LiveState of one raw entry (``name`` and ``cwd`` are not read)."""
    kind = _norm(entry.get("kind"))
    if kind == KIND_INTERACTIVE:
        state = _norm(entry.get("status"))
    elif kind == KIND_BACKGROUND:
        state = _norm(entry.get("state"))
    else:
        state = _norm(entry.get("state")) or _norm(entry.get("status"))
    return LiveState(kind=kind, state=state, pid=_pid(entry.get("pid")))


def classify_live(entry: Any) -> Optional[str]:
    """``"active"`` | ``"waiting"`` | ``"open"`` | None for a LiveState or a raw entry dict."""
    if isinstance(entry, Mapping):
        entry = state_of(entry)
    if not isinstance(entry, LiveState):
        return None
    if entry.state in WAITING_STATES:
        return WAITING
    if entry.state in ACTIVE_STATES:
        return ACTIVE
    if entry.kind == KIND_INTERACTIVE and entry.state == IDLE and entry.pid is not None:
        return OPEN
    return None


def parse(data: Any) -> Optional[Dict[str, LiveState]]:
    """``{sessionId: LiveState}`` from the decoded JSON, or None when it is not a list. Entries
    that are not objects or carry no usable ``sessionId`` are skipped; when a session has several
    entries the strongest signal wins (waiting, then active, then open)."""
    if not isinstance(data, list):
        return None
    out: Dict[str, LiveState] = {}
    for raw in data:
        if not isinstance(raw, Mapping):
            continue
        sid = _session_id(raw.get("sessionId"))
        if sid is None:
            continue
        state = state_of(raw)
        prior = out.get(sid)
        if prior is None or _RANK[classify_live(state)] > _RANK[classify_live(prior)]:
            out[sid] = state
    return out


def _default_runner(argv: Sequence[str], timeout: float) -> Any:
    return subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=timeout, check=False)


def live(timeout: float = DEFAULT_TIMEOUT, runner: Optional[Callable[..., Any]] = None,
         which: Callable[[str], Optional[str]] = shutil.which) -> Optional[Dict[str, LiveState]]:
    """Run ``claude agents --json --all`` and parse it; None on any failure (never raises).

    ``runner(argv, timeout)`` returns an object with ``returncode`` and ``stdout`` (bytes or str),
    like ``subprocess.run``; ``which`` finds ``claude`` on PATH. Both are injectable for tests."""
    try:
        exe = which("claude")
        if not exe:
            return None
        argv: List[str] = [exe] + list(COMMAND)
        res = (runner or _default_runner)(argv, timeout)
        if getattr(res, "returncode", 1) != 0:
            return None
        out = getattr(res, "stdout", None)
        if isinstance(out, bytes):
            if len(out) > MAX_OUTPUT_BYTES:
                return None
            text = out.decode("utf-8", errors="replace")
        elif isinstance(out, str):
            if len(out) > MAX_OUTPUT_BYTES:
                return None
            text = out
        else:
            return None
        return parse(json.loads(text))
    except Exception:  # noqa: BLE001 - a timeout, a missing binary, bad JSON: no live signal
        return None
