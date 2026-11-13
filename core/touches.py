"""The touch index: which session worked in which repo, and when (contract 16.11).

Two sessions in one folder share one set of files and one git index, so the next commit sweeps up
the other session's work. Telling them apart after the fact needs to know when each session did
something in each repo. Edit-tool calls name their file, but most writing happens through Bash
(measured about 6 to 1), so every finished tool call counts too, at the working folder its entry
carries. Matching an uncommitted file's mtime to those instants (within a few seconds) says which
session most likely changed it.

Built incrementally: a transcript only grows, so each session keeps the byte offset read so far and
a hash of the 64 KiB before it; a file that changed before the offset is read again from the start.
Only repo paths, ints and bools are stored (``GHOSTED_HOME/touches.json``, 0600, this Mac only): never
file names, commands, text or launch folders.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from core import detector, ledger as ledger_mod, timeutil, transcript

FILE_NAME = "touches.json"
INDEX_VERSION = 1
READ_DAYS = 30        # a session file never indexed is read only when modified this recently
KEEP_DAYS = 90        # touches older than this are dropped
DISCOVER_DAYS = 30    # Part 0: repos touched this recently are checked for unsaved work
HASH_BYTES = 64 * 1024
DEFAULT_WINDOW = 15
AMBIGUOUS = "ambiguous"
NONE = "none"
UNSPLIT = -1.0        # an entry's "mtime" when a new folder is too big to split: always ambiguous
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
# Scratch space: a repo in here is a test or a throwaway, never someone's unsaved work.
EPHEMERAL_ROOTS: Tuple[str, ...] = detector.TEMP_ROOTS


class Index:
    """The loaded index. ``sessions`` = ``{uuid: record}`` as stored; ``stats`` = what the last
    ``update`` did (``files_read``, ``bytes_read``, ``rebuilt``)."""

    def __init__(self, sessions: Optional[Dict[str, Any]] = None) -> None:
        self.sessions: Dict[str, Any] = sessions if isinstance(sessions, dict) else {}
        self.stats: Dict[str, int] = {"files_read": 0, "bytes_read": 0, "rebuilt": 0}

    def _live(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        for uuid, rec in self.sessions.items():
            if isinstance(rec, dict) and not rec.get("auto"):
                yield uuid, rec

    def _of(self, field: str, repo: Any) -> Dict[str, List[int]]:
        key = str(repo)
        out: Dict[str, List[int]] = {}
        for uuid, rec in self._live():
            got = (rec.get(field) or {}).get(key) if isinstance(rec.get(field), dict) else None
            if got:
                out[uuid] = list(got)
        return out

    def touches(self, repo: Any) -> Dict[str, List[int]]:
        """``{uuid: [t, ...]}``: every touch of ``repo`` (automated sessions left out)."""
        return self._of("repos", repo)

    def edits(self, repo: Any) -> Dict[str, List[int]]:
        """``{uuid: [t, ...]}``: the edit-tool touches of ``repo``."""
        return self._of("edits", repo)

    def repos_since(self, t: float) -> Set[str]:
        """Repos with a touch at or after epoch ``t``."""
        out: Set[str] = set()
        for _, rec in self._live():
            repos = rec.get("repos")
            if not isinstance(repos, dict):
                continue
            for repo, stamps in repos.items():
                if stamps and isinstance(stamps, list) and stamps[-1] >= t:
                    out.add(repo)
        return out


# ---- storage ----------------------------------------------------------------------------------------

def index_path(cfg: Any) -> Path:
    return Path(cfg.home) / FILE_NAME


def load(cfg: Any) -> Index:
    """The stored index; an empty one when missing or unreadable (the next update rebuilds)."""
    try:
        doc = json.loads(index_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return Index()
    if not isinstance(doc, dict) or doc.get("version") != INDEX_VERSION:
        return Index()
    return Index(doc.get("sessions"))


def save(cfg: Any, index: Index) -> None:
    doc = {"version": INDEX_VERSION, "sessions": index.sessions}
    data = json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode("ascii")
    ledger_mod.atomic_write_bytes(index_path(cfg), data)


# ---- reading one transcript ---------------------------------------------------------------------

def _epoch(value: Any) -> Optional[int]:
    if not isinstance(value, str) or len(value) < 19:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        try:
            when = timeutil.parse_iso(value)
        except ValueError:
            return None
    if when.tzinfo is None:
        return int(timeutil.parse_iso(value).timestamp())
    return int(when.timestamp())


class _Repos:
    """Directory -> resolved git toplevel (or None), cached for one update."""

    def __init__(self) -> None:
        self.cache: Dict[str, Optional[str]] = {}
        self.ephemeral = [os.path.realpath(r) for r in EPHEMERAL_ROOTS if r]

    def of_dir(self, directory: str) -> Optional[str]:
        if directory in self.cache:
            return self.cache[directory]
        top = detector.git_toplevel(directory)
        repo: Optional[str] = None
        if top:
            try:
                repo = os.path.realpath(top)
            except (OSError, ValueError):
                repo = None
        if repo is not None and any(repo == e or repo.startswith(e.rstrip(os.sep) + os.sep) for e in self.ephemeral):
            repo = None
        self.cache[directory] = repo
        return repo

    def of_file(self, path: str, cwd: Optional[str]) -> Optional[str]:
        if not os.path.isabs(path):
            if not cwd:
                return None
            path = os.path.join(cwd, path)
        return self.of_dir(os.path.dirname(os.path.normpath(path)))


def _hash_before(f: Any, offset: int) -> str:
    start = max(0, offset - HASH_BYTES)
    f.seek(start)
    data = f.read(offset - start)
    return "%d:%s" % (offset, hashlib.sha1(data).hexdigest())


def _blank_record() -> Dict[str, Any]:
    return {"offset": 0, "mtime": 0, "size": 0, "version": "0:", "head": {"n": 0, "ep": False, "turns": 0, "fp": None},
            "auto": False, "repos": {}, "edits": {}}


class _Reader:
    def __init__(self, cfg: Any, repos: _Repos, stats: Dict[str, int]) -> None:
        self.skip = set(getattr(cfg, "skip_entrypoints", None) or ())
        self.patterns = detector.compile_prompt_patterns(cfg)
        self.repos = repos
        self.stats = stats

    def head(self, rec: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> None:
        head = rec["head"]
        head["n"] = int(head.get("n") or 0) + 1
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            return
        if entry.get("type") not in transcript.CONVERSATION_TYPES:
            return
        ep = entry.get("entrypoint")
        if isinstance(ep, str) and ep.strip() in self.skip:
            head["ep"] = True
        turn = transcript.human_turn(entry)
        if turn is not None:
            head["turns"] = int(head.get("turns") or 0) + 1
            if head.get("fp") is None and turn[0] == "prompt":
                head["fp"] = any(rx.search(turn[1]) for rx in self.patterns)

    def touch(self, rec: Dict[str, Any], entry: Dict[str, Any], new: Dict[str, Dict[str, Set[int]]]) -> None:
        if entry.get("isSidechain"):
            return
        etype = entry.get("type")
        if etype not in ("user", "assistant"):
            return
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return
        t = _epoch(entry.get("timestamp"))
        if t is None:
            return
        raw_cwd = entry.get("cwd")
        cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None
        finished = False
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if etype == "assistant" and kind == "tool_use":
                name = block.get("name")
                key = EDIT_TOOLS.get(name) if isinstance(name, str) else None
                inp = block.get("input")
                target = inp.get(key) if key and isinstance(inp, dict) else None
                if isinstance(target, str) and target and "\x00" not in target:
                    repo = self.repos.of_file(target, cwd)
                    if repo:
                        new["repos"].setdefault(repo, set()).add(t)
                        new["edits"].setdefault(repo, set()).add(t)
            elif etype == "user" and kind == "tool_result":
                finished = True
        if finished and cwd and "\x00" not in cwd:
            repo = self.repos.of_dir(os.path.normpath(cwd))
            if repo:
                new["repos"].setdefault(repo, set()).add(t)

    def read(self, path: Path, rec: Optional[Dict[str, Any]], size: int, mtime: int) -> Dict[str, Any]:
        with open(str(path), "rb") as f:
            offset = int(rec.get("offset") or 0) if isinstance(rec, dict) else 0
            fresh = rec is None or not isinstance(rec, dict) or offset <= 0
            if not fresh and (offset > size or _hash_before(f, offset) != rec.get("version")):
                fresh = True
                self.stats["rebuilt"] += 1
            if fresh:
                rec = _blank_record()
                offset = 0
            else:
                rec = json.loads(json.dumps(rec))  # a private copy
                for field in ("repos", "edits"):
                    if not isinstance(rec.get(field), dict):
                        rec[field] = {}
                if not isinstance(rec.get("head"), dict):
                    rec["head"] = _blank_record()["head"]
            new: Dict[str, Dict[str, Set[int]]] = {"repos": {}, "edits": {}}
            f.seek(offset)
            consumed = 0
            limit = size - offset
            while consumed < limit:
                line = f.readline(limit - consumed)
                if not line or not line.endswith(b"\n"):
                    break  # a partial last line waits for the next update
                consumed += len(line)
                in_head = int(rec["head"].get("n") or 0) < transcript.HEAD_LINES
                if not in_head and b"tool_use" not in line:
                    continue  # neither a tool call nor a tool result
                try:
                    entry = json.loads(line)
                except ValueError:
                    entry = None
                # A malformed entry is skipped on its own; it never costs the
                # session's other touches (or blocks every later read at the same offset).
                try:
                    if in_head:
                        self.head(rec, entry)
                    if isinstance(entry, dict):
                        self.touch(rec, entry, new)
                except (TypeError, ValueError, AttributeError, KeyError):
                    continue
            end = offset + consumed
            rec["version"] = _hash_before(f, end) if end else "0:"
        self.stats["files_read"] += 1
        self.stats["bytes_read"] += consumed
        rec["offset"] = end
        rec["size"] = size
        rec["mtime"] = mtime
        head = rec["head"]
        rec["auto"] = bool((head.get("ep") and int(head.get("turns") or 0) <= 1) or head.get("fp") is True)
        for field in ("repos", "edits"):
            for repo, stamps in new[field].items():
                merged = set(rec[field].get(repo) or ()) | stamps
                rec[field][repo] = sorted(merged)
        return rec


def _prune(rec: Dict[str, Any], floor: float) -> bool:
    """Drop touches older than ``floor``; True when anything changed."""
    changed = False
    for field in ("repos", "edits"):
        table = rec.get(field)
        if not isinstance(table, dict):
            rec[field] = {}
            changed = True
            continue
        for repo in list(table):
            stamps = table[repo]
            if not isinstance(stamps, list):
                del table[repo]
                changed = True
                continue
            kept = [t for t in stamps if isinstance(t, int) and t >= floor]
            if len(kept) != len(stamps):
                changed = True
                if kept:
                    table[repo] = kept
                else:
                    del table[repo]
    return changed


def _has_touches(rec: Mapping[str, Any]) -> bool:
    return any(bool(v) for field in ("repos", "edits") for v in (rec.get(field) or {}).values())


def update(cfg: Any, now: Any = None, index: Optional[Index] = None, write: bool = True) -> Index:
    """Bring the index up to date with the session files (contract 16.11). Never raises: a file
    that cannot be read keeps its old record, an index that cannot be saved is still returned."""
    when = timeutil.now_utc() if now is None else (timeutil.parse_iso(now) if isinstance(now, str) else now)
    idx = index if index is not None else load(cfg)
    idx.stats = {"files_read": 0, "bytes_read": 0, "rebuilt": 0}
    now_ts = when.timestamp()
    read_floor = now_ts - READ_DAYS * 86400
    keep_floor = now_ts - KEEP_DAYS * 86400
    reader = _Reader(cfg, _Repos(), idx.stats)
    changed = False
    seen: Set[str] = set()
    try:
        files = detector.enumerate_sessions(cfg)
    except Exception:  # noqa: BLE001 - no projects dir readable: keep what we have
        files = []
    for uuid, path, st in files:
        seen.add(uuid)
        rec = idx.sessions.get(uuid)
        # Nanoseconds: a same-size rewrite within one second is still seen.
        size, mtime = int(st.st_size), int(st.st_mtime_ns)
        if isinstance(rec, dict) and rec.get("size") == size and rec.get("mtime") == mtime:
            continue
        if not isinstance(rec, dict) and st.st_mtime < read_floor:
            continue
        try:
            idx.sessions[uuid] = reader.read(path, rec if isinstance(rec, dict) else None, size, mtime)
        except (OSError, ValueError, TypeError, RecursionError):
            continue
        changed = True
    for uuid in list(idx.sessions):
        rec = idx.sessions[uuid]
        if not isinstance(rec, dict):
            del idx.sessions[uuid]
            changed = True
            continue
        if _prune(rec, keep_floor):
            changed = True
        if uuid not in seen and not _has_touches(rec):
            del idx.sessions[uuid]
            changed = True
    if changed and write:
        try:
            save(cfg, idx)
        except OSError:
            pass
    return idx


# ---- attribution --------------------------------------------------------------------------------

def _near(stamps: List[int], t: float, window: float) -> bool:
    i = bisect.bisect_left(stamps, t - window)
    return i < len(stamps) and stamps[i] <= t + window


def owner_of(mtime: float, by_session: Mapping[str, List[int]], window: float = DEFAULT_WINDOW) -> str:
    """The one session with a touch within ``±window`` s of ``mtime``, else ``ambiguous`` (several)
    or ``none``."""
    hits = [uuid for uuid, stamps in by_session.items() if stamps and _near(stamps, mtime, window)]
    if len(hits) == 1:
        return hits[0]
    return AMBIGUOUS if hits else NONE


def owner_list(mtimes: Iterable[Optional[float]], by_session: Mapping[str, List[int]],
               window: float = DEFAULT_WINDOW) -> List[Optional[str]]:
    """``owner_of`` per entry (None for an entry with no mtime; ``UNSPLIT`` = ambiguous)."""
    return [None if m is None else AMBIGUOUS if m == UNSPLIT else owner_of(m, by_session, window)
            for m in mtimes]


def owners(repo: Any, facts: Optional[Mapping[str, Any]], index: Index,
           window: float = DEFAULT_WINDOW) -> Dict[str, Any]:
    """``{"sessions": {uuid: n}, "ambiguous": n, "none": n}`` over the repo's uncommitted entries
    (``facts["mtimes"]``); an entry with no mtime (deleted) is not counted."""
    out: Dict[str, Any] = {"sessions": {}, "ambiguous": 0, "none": 0}
    by = index.touches(repo)
    for owner in owner_list((facts or {}).get("mtimes") or (), by, window):
        if owner is None:
            continue
        if owner in (AMBIGUOUS, NONE):
            out[owner] += 1
        else:
            out["sessions"][owner] = out["sessions"].get(owner, 0) + 1
    return out


def ranked(sessions: Mapping[str, int]) -> List[Tuple[str, int]]:
    """Owners biggest first (ties by id)."""
    return sorted(((u, int(n)) for u, n in sessions.items()), key=lambda p: (-p[1], p[0]))
