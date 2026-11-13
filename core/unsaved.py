"""Unsaved work across the workspace repos (contract 16.9).

`ghosted report` checks every git work tree under the workspace roots, whatever the sessions did: work
nobody committed, commits nobody pushed, branches nobody merged, repos with no remote. Read-only git,
no network (`ahead` uses the local remote-tracking refs, never `git fetch`). Counts and dates only:
file names, branch names and commit subjects never leave this module.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from core import evidence, timeutil, touches

REPO_TIMEOUT = 2.0        # all of one repo's git calls together
MAX_WORKERS = 8
DIR_FILE_CAP = 200        # files looked at inside one untracked directory
RULES = ("uncommitted", "mixed", "unpushed", "unmerged", "no_remote")

NOTE_UNAVAILABLE = evidence.UNAVAILABLE  # "git status unavailable"
NOTE_NO_DEFAULT = "no default branch: unmerged not checked"
NOTE_ACTIVE = "active: not checked"


class RepoCheck(NamedTuple):
    path: Path
    project: str
    facts: Optional[Dict[str, Any]]
    flags: Dict[str, int]
    notes: List[str]


# ---- which repos ------------------------------------------------------------------------------------

def _is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def repos(cfg: Any, index: Any = None, now: Any = None) -> List[Path]:
    """Each workspace root that is a repo, then each direct child directory that is one, plus
    (contract 16.11 part 0) every repo the touch index saw in the last 30 days; resolved, each
    once, sorted. A discovered repo that is gone (no ``.git``) is skipped."""
    seen: Dict[str, Path] = {}
    if index is not None:
        when = timeutil.parse_iso(now) if isinstance(now, str) else now
        floor = (when.timestamp() if when is not None else time.time()) - touches.DISCOVER_DAYS * 86400
        for found in index.repos_since(floor):
            try:
                path = Path(found).resolve()
                if path.is_dir() and _is_repo(path):
                    seen.setdefault(str(path), path)
            except (OSError, RuntimeError, ValueError):
                continue
    for root in getattr(cfg, "workspace_roots", None) or ():
        try:
            root = Path(root).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if not root.is_dir():
            continue
        if _is_repo(root):
            seen.setdefault(str(root), root)
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink() and _is_repo(child):
                    seen.setdefault(str(child), child)
            except OSError:
                continue
    return [seen[k] for k in sorted(seen)]


# ---- facts --------------------------------------------------------------------------------------

def _iso(ts: float) -> Optional[str]:
    try:
        return timeutil.to_iso(datetime.fromtimestamp(ts, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _dir_newest(path: Path) -> Optional[float]:
    newest: Optional[float] = None
    looked = 0
    for dirpath, dirnames, filenames in os.walk(str(path)):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            looked += 1
            try:
                t = os.lstat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            newest = t if newest is None or t > newest else newest
            if looked >= DIR_FILE_CAP:
                return newest
    return newest


def _status_entries(raw: bytes) -> List[str]:
    """Paths from `git status --porcelain -z` (a rename's original path is skipped)."""
    parts = raw.split(b"\0")
    out: List[str] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        i += 1
        if len(token) < 4:
            continue
        xy, path = token[:2], token[3:]
        if xy[:1] in (b"R", b"C"):
            i += 1  # the original path follows
        out.append(path.decode("utf-8", "surrogateescape"))
    return out


def _times(raw: Optional[bytes]) -> List[float]:
    out: List[float] = []
    for line in (raw or b"").decode("utf-8", "replace").split():
        if line.isdigit():
            out.append(float(line))
    return out


STATUS_ARGS = ("--porcelain", "-z", "--untracked-files=normal")


def _entries(path: Path, status: bytes) -> List[Tuple[str, Optional[float]]]:
    """(repo-relative path, mtime) per uncommitted entry. A nested repo's directory is left out
    (it is checked on its own); an untracked directory's mtime is the newest of up to
    DIR_FILE_CAP files inside it; a deleted path has none."""
    out: List[Tuple[str, Optional[float]]] = []
    for rel in _status_entries(status):
        full = path / rel
        t: Optional[float]
        if rel.endswith("/"):
            if _is_repo(full):
                continue
            t = _dir_newest(full)
        else:
            try:
                t = os.lstat(str(full)).st_mtime
            except OSError:
                t = None
        out.append((rel, t))
    return out


SPLIT_FILE_CAP = 2000    # files one new folder is split into for attribution; more = ambiguous


def _folder_files(full: Path, rel: str) -> Optional[List[Tuple[str, Optional[float]]]]:
    """Every file in an untracked folder (nested repos and .git skipped), or None past the cap."""
    out: List[Tuple[str, Optional[float]]] = []
    for dirpath, dirnames, filenames in os.walk(str(full)):
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and not _is_repo(Path(dirpath) / d))
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            try:
                t: Optional[float] = os.lstat(p).st_mtime
            except OSError:
                t = None
            out.append((rel + os.path.relpath(p, str(full)).replace(os.sep, "/"), t))
            if len(out) > SPLIT_FILE_CAP:
                return None
    return out


def _split(path: Path, entries: List[Tuple[str, Optional[float]]]) -> List[Tuple[str, Optional[float]]]:
    """The entries for attribution (contract 16.11, review fix): git lists a new folder as one
    entry, but two sessions can each add files to it, so a folder is split into its files; one
    too big to split stays whole and counts as ambiguous (``touches.UNSPLIT``)."""
    out: List[Tuple[str, Optional[float]]] = []
    for rel, t in entries:
        if not rel.endswith("/"):
            out.append((rel, t))
            continue
        files = _folder_files(path / rel, rel)
        if files is None:
            out.append((rel, touches.UNSPLIT))
        else:
            out.extend(files)
    return out


def dirty_entries(path: Any, timeout: Optional[float] = None) -> Optional[List[Tuple[str, Optional[float]]]]:
    """The uncommitted entries with their names, new folders split into files, for
    ``ghosted mixed --files`` only (contract 16.11). None when git cannot read the repo."""
    path = Path(path)
    limit = REPO_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    try:
        if not path.is_dir():
            return None
        status = evidence._git(str(path), ["status"] + list(STATUS_ARGS), deadline, evidence._env())
    except (evidence._Timeout, OSError):
        return None
    return None if status is None else _split(path, _entries(path, status))


def repo_facts(path: Any, now: Any = None, timeout: Optional[float] = None) -> Dict[str, Any]:
    """What git says about the repo at ``path`` (contract 16.9). Never raises; ``error`` is None,
    ``"timeout"`` or ``"git"``. ``now`` is unused by the facts themselves (ages are judged by
    ``flags``) and kept for symmetry."""
    del now
    path = Path(path)
    facts: Dict[str, Any] = {"path": str(path), "uncommitted": 0, "uncommitted_since": None,
                             "unpushed": 0, "unpushed_since": None, "has_remote": False,
                             "default_branch": None, "unmerged": [], "common_dir": None,
                             "worktree_of": None, "error": None, "mtimes": [], "owners": None}
    limit = REPO_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    env = evidence._env()

    def git(*args: str) -> Optional[bytes]:
        return evidence._git(str(path), list(args), deadline, env)

    try:
        if not path.is_dir() or git("rev-parse", "--show-toplevel") is None:
            facts["error"] = "git"
            return facts
        common = git("rev-parse", "--git-common-dir")
        if common and common.strip():
            raw = common.decode("utf-8", "surrogateescape").strip()
            facts["common_dir"] = os.path.realpath(raw if os.path.isabs(raw) else str(path / raw))
        status = git("status", *STATUS_ARGS)
        if status is None:
            facts["error"] = "git"
            return facts
        entries = _entries(path, status)
        times = [t for _, t in entries if t is not None]
        facts["uncommitted"] = len(entries)
        facts["uncommitted_since"] = _iso(min(times)) if times else None
        # Per-file mtimes for attribution (contract 16.11; new folders split); names stay here.
        facts["mtimes"] = [t for _, t in _split(path, entries)]

        # A query that fails is "unavailable", never an empty "saved" answer,
        # or the scan would close an item whose work is still unsaved.
        remotes = git("remote")
        if remotes is None:
            facts["error"] = "git"
            return facts
        facts["has_remote"] = bool(remotes.strip())
        if facts["has_remote"]:
            log = git("log", "--branches", "--not", "--remotes", "--format=%ct")
            if log is None:
                facts["error"] = "git"
                return facts
            times = _times(log)
            facts["unpushed"] = len(times)
            facts["unpushed_since"] = _iso(min(times)) if times else None

        default_ref = None
        head = git("symbolic-ref", "-q", "refs/remotes/origin/HEAD")
        if head and head.strip():
            default_ref = head.decode("utf-8", "replace").strip()
        else:
            for name in ("main", "master"):
                if git("rev-parse", "--verify", "-q", "refs/heads/" + name) is not None:
                    default_ref = "refs/heads/" + name
                    break
        if default_ref:
            short = default_ref
            for prefix in ("refs/heads/", "refs/remotes/origin/"):
                if default_ref.startswith(prefix):
                    short = default_ref[len(prefix):]
                    break
            facts["default_branch"] = short
            out = git("for-each-ref", "--no-merged=" + default_ref,
                      "--format=%(refname)%00%(committerdate:unix)", "refs/heads")
            if out is None:
                facts["error"] = "git"
                return facts
            tips: List[str] = []
            for line in out.decode("utf-8", "replace").splitlines():
                ref, _, stamp = line.partition("\0")
                if ref == "refs/heads/" + short or not stamp.strip().isdigit():
                    continue
                iso = _iso(float(stamp.strip()))
                if iso:
                    tips.append(iso)
            facts["unmerged"] = sorted(tips)
    except evidence._Timeout:
        facts["error"] = "timeout"
    except OSError:
        facts["error"] = "git"
    return facts


# ---- rules ----------------------------------------------------------------------------------------

def _days_old(iso: Any, now: datetime) -> Optional[float]:
    if not isinstance(iso, str):
        return None
    try:
        return (now - timeutil.parse_iso(iso)).total_seconds() / 86400.0
    except ValueError:
        return None


def _same_path(a: Any, b: Any) -> bool:
    try:
        return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))
    except (OSError, ValueError):
        return False


def flags(facts: Optional[Mapping[str, Any]], cfg: Any, now: Any) -> Dict[str, int]:
    """``{rule: count}`` for the rules that fire, in display order (contract 16.9)."""
    if not facts or facts.get("error"):
        return {}
    when = timeutil.parse_iso(now) if isinstance(now, str) else now
    out: Dict[str, int] = {}
    n = int(facts.get("uncommitted") or 0)
    old = _days_old(facts.get("uncommitted_since"), when)
    if n > 0 and old is not None and old >= getattr(cfg, "unsaved_stale_days", 3):
        out["uncommitted"] = n
    owners = facts.get("owners")
    if n > 0 and isinstance(owners, Mapping):
        many = sum(1 for v in (owners.get("sessions") or {}).values() if int(v or 0) > 0)
        if many >= 2:
            out["mixed"] = many  # any age: the risk is the next commit, not staleness (16.11)
    if facts.get("worktree_of"):
        return out  # a linked worktree: branches, commits and remotes are its main repo's rows
    n = int(facts.get("unpushed") or 0)
    old = _days_old(facts.get("unpushed_since"), when)
    if facts.get("has_remote") and n > 0 and old is not None and old >= getattr(cfg, "unsaved_unpushed_days", 1):
        out["unpushed"] = n
    if facts.get("default_branch"):
        limit = getattr(cfg, "unsaved_unmerged_days", 14)
        stale = [t for t in facts.get("unmerged") or () if (_days_old(t, when) or 0) >= limit]
        if stale:
            out["unmerged"] = len(stale)
    if not facts.get("has_remote"):
        ok = getattr(cfg, "unsaved_no_remote_ok", None) or ()
        if not any(_same_path(facts.get("path"), p) for p in ok):
            out["no_remote"] = 1
    return out


def notes(facts: Optional[Mapping[str, Any]]) -> List[str]:
    """Debug notes for a repo that was skipped or only partly checked."""
    if not facts or facts.get("error"):
        return [NOTE_UNAVAILABLE]
    if not facts.get("default_branch"):
        return [NOTE_NO_DEFAULT]
    return []


def remaining(facts: Optional[Mapping[str, Any]], shown: Iterable[str], cfg: Any, now: Any) -> Optional[Dict[str, int]]:
    """What still keeps an item open for `ghosted done` (ages ignored): uncommitted entries, unpushed
    commits, and any other rule the item showed that still holds. None when git could not say."""
    if not facts or facts.get("error"):
        return None
    out: Dict[str, int] = {}
    if facts.get("uncommitted"):
        out["uncommitted"] = int(facts["uncommitted"])
    if facts.get("has_remote") and facts.get("unpushed"):
        out["unpushed"] = int(facts["unpushed"])
    shown = set(shown)
    # Ages ignored: a branch shown as unmerged still counts while it is
    # unmerged, even if a fresh commit made its tip young again.
    if "unmerged" in shown and facts.get("default_branch") and facts.get("unmerged"):
        out["unmerged"] = len(facts["unmerged"])
    if "no_remote" in shown and "no_remote" in flags(facts, cfg, now):
        out["no_remote"] = 1
    return out


# ---- the check --------------------------------------------------------------------------------------

def _active_repos(paths: List[Path], cwds: Iterable[Any]) -> set:
    """The deepest repo holding each active session's launch directory."""
    hit = set()
    for cwd in cwds:
        if not isinstance(cwd, str) or not cwd:
            continue
        try:
            real = os.path.realpath(os.path.expanduser(cwd))
        except (OSError, ValueError):
            continue
        best = None
        for p in paths:
            s = str(p)
            if real == s or real.startswith(s.rstrip(os.sep) + os.sep):
                if best is None or len(s) > len(best):
                    best = s
        if best is not None:
            hit.add(best)
    return hit


def check(cfg: Any, now: Any, active_cwds: Iterable[Any] = (), timeout: Optional[float] = None,
          index: Any = None) -> List[RepoCheck]:
    """Check every repo (in parallel); repos with an active session are skipped. With a touch
    index (contract 16.11), the repos sessions worked in are checked too and each dirty repo's
    entries are attributed to sessions (``facts["owners"]``)."""
    when = timeutil.parse_iso(now) if isinstance(now, str) else now
    paths = repos(cfg, index, when)
    busy = _active_repos(paths, active_cwds)
    todo = [p for p in paths if str(p) not in busy]
    limit = REPO_TIMEOUT if timeout is None else timeout
    found: Dict[str, Dict[str, Any]] = {}
    if todo:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as pool:
            for p, facts in zip(todo, pool.map(lambda q: repo_facts(q, when, limit), todo)):
                found[str(p)] = facts
    # Linked worktrees share their main repo's branches and remotes: when the main repo is
    # checked too, a worktree only reports its own uncommitted tree.
    mains = {f.get("common_dir"): q for q, f in found.items()
             if f.get("common_dir") and os.path.realpath(os.path.join(q, ".git")) == f.get("common_dir")}
    for q, f in found.items():
        main = mains.get(f.get("common_dir"))
        if main is not None and main != q:
            f["worktree_of"] = main
        if index is not None and not f.get("error") and f.get("uncommitted"):
            f["owners"] = touches.owners(q, f, index, getattr(cfg, "mixed_window_seconds", touches.DEFAULT_WINDOW))
    out: List[RepoCheck] = []
    for p in paths:
        if str(p) in busy:
            out.append(RepoCheck(p, p.name, None, {}, [NOTE_ACTIVE]))
            continue
        facts = found[str(p)]
        out.append(RepoCheck(p, p.name, facts, flags(facts, cfg, when), notes(facts)))
    return out


def summary_of(check_: RepoCheck) -> Dict[str, Any]:
    """What an item keeps: the flags plus the two ages (counts and dates only)."""
    facts = check_.facts or {}
    mixed = "mixed" in check_.flags
    out = {"flags": dict(check_.flags),
           "uncommitted_since": facts.get("uncommitted_since") if ("uncommitted" in check_.flags or mixed) else None,
           "unpushed_since": facts.get("unpushed_since") if "unpushed" in check_.flags else None}
    if mixed:
        # Mixed dirt shows its whole count even when it is fresh (contract 16.11); uuids + counts only.
        owners = facts.get("owners") or {}
        out["dirty"] = int(facts.get("uncommitted") or 0)
        out["owners"] = {"sessions": {str(u): int(n) for u, n in (owners.get("sessions") or {}).items()},
                         "ambiguous": int(owners.get("ambiguous") or 0), "none": int(owners.get("none") or 0)}
    return out
