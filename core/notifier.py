"""The Claude app notifier's state (contract 16.12): one background Claude Code session per
GHOSTED_HOME that sends pushes through the Claude app.

``GHOSTED_HOME/notifier.json`` (0600) holds ``{"session_id", "short_id", "created_at", "generations",
"sends", "last"}``; ``generations`` lists every notifier session ever created (forks included), so
the scan, the report and the touch index can skip them by id (``ids``). ``GHOSTED_HOME/notifier/`` is
the folder the notifier runs in. Nothing here holds transcript text, file names or push bodies.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Set, Tuple

_SHORT = re.compile(r"^[0-9a-f]{6,36}$")
STATE_FILE = "notifier.json"
LOCK_FILE = "notifier.lock"
FOLDER = "notifier"


def folder(home: Any) -> Path:
    return Path(home) / FOLDER


def lock_path(home: Any) -> Path:
    return Path(home) / LOCK_FILE


def empty() -> Dict[str, Any]:
    # ready: the first turn ended with the phone link up (a send never resumes an unready notifier).
    # pending_forks: short ids of forks whose full id is not known yet (skipped by prefix).
    return {"session_id": None, "short_id": None, "created_at": None, "generations": [],
            "sends": 0, "last": None, "ready": False, "pending_forks": []}


def load(home: Any) -> Dict[str, Any]:
    """The state, or an empty one when the file is missing or unreadable (never raises)."""
    out = empty()
    try:
        raw = json.loads((Path(home) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for key in ("session_id", "short_id", "created_at"):
        if isinstance(raw.get(key), str) and raw[key]:
            out[key] = raw[key]
    gens = raw.get("generations")
    if isinstance(gens, list):
        out["generations"] = [g for g in gens if isinstance(g, str) and g]
    if isinstance(raw.get("sends"), int) and not isinstance(raw.get("sends"), bool):
        out["sends"] = raw["sends"]
    out["ready"] = raw.get("ready") is True
    forks = raw.get("pending_forks")
    if isinstance(forks, list):
        out["pending_forks"] = [f for f in forks if isinstance(f, str) and _SHORT.match(f)]
    if isinstance(raw.get("last"), dict):
        out["last"] = {k: v for k, v in raw["last"].items() if k in ("at", "outcome") and isinstance(v, str)}
    return out


def save(home: Any, state: Dict[str, Any]) -> None:
    from core import ledger  # atomic 0600 writes live there

    data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    Path(home).mkdir(mode=0o700, parents=True, exist_ok=True)
    ledger.atomic_write_bytes(Path(home) / STATE_FILE, data)  # 0600


def ids(home: Any) -> Set[str]:
    """Every notifier session id (current and retired) to leave out of scans. Never raises."""
    state = load(home)
    out = set(state["generations"])
    if state["session_id"]:
        out.add(state["session_id"])
    return out


def skipped(home: Any) -> Tuple[Set[str], Set[str]]:
    """(full ids, short-id prefixes) of every notifier session to leave out of scans."""
    state = load(home)
    return ids(home), set(state["pending_forks"])


def is_notifier(uuid: str, full: Set[str], prefixes: Set[str]) -> bool:
    return uuid in full or any(uuid.startswith(p) for p in prefixes)


def ensure_folder(home: Any) -> Path:
    path = folder(home)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass
    return path
