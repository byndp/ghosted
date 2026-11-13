"""Machine facts about a session's working tree, for an interrupted item's evidence (contract 6.7).

``git_facts(path, timeout=5)`` asks git about the directory and never raises. ``git_text(facts)``
turns the facts into the short phrase the phone shows, and ``evidence_text(ending, facts)`` builds
the whole evidence string, guaranteed to pass ``core.sanitize.problems()``:

    "ended on usage limit · 91 uncommitted, 2 unpushed"
    "ended on an open question · 18 uncommitted on round-2, no upstream"
    "ended on an open question · clean tree on feat, 2 unpushed, no upstream"
    "ended on an open question · resumed 10-06, ended clean · clean tree"
    "ended on usage limit · not a git repo"

Evidence is uploaded, so it holds counts and a branch name only, never file names or transcript
text. The branch is named only when there is no upstream (the spec examples), and it is the first
thing dropped when the string would not pass the sanitizer (an email or a key in a branch name, or
a branch so long the string passes 100 characters).
"""
from __future__ import annotations

import re

import os
import subprocess
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from core import sanitize

ENDING_LIMIT = "ended on usage limit"
ENDING_QUESTION = "ended on an open question"
ENDINGS = {"usage_limit": ENDING_LIMIT, "open_question": ENDING_QUESTION}
SEP = " · "

NOT_A_REPO = "not a git repo"
CLEAN_TREE = "clean tree"
NO_UPSTREAM = "no upstream"
UNAVAILABLE = "git status unavailable"

GIT = "git"
DEFAULT_TIMEOUT = 5.0

# Read-only and non-interactive: no optional index lock (so a scan never blocks or races a session's
# own git commands), never a credential prompt.
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
# Variables that would point git at some other repository than the directory asked about.
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX")

class _Timeout(Exception):
    pass


def _env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_VARS}
    env.update(GIT_ENV)
    return env


def _git(path: str, args: Sequence[str], deadline: float, env: Mapping[str, str]) -> Optional[bytes]:
    """stdout of ``git -C path <args>``; None on a non-zero exit. Raises _Timeout or OSError."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Timeout()
    try:
        proc = subprocess.run([GIT, "-C", path] + list(args), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(env),
                              timeout=remaining, check=False)
    except subprocess.TimeoutExpired:
        raise _Timeout() from None
    return proc.stdout if proc.returncode == 0 else None


def _line(out: Optional[bytes]) -> Optional[str]:
    if out is None:
        return None
    text = out.decode("utf-8", "replace").strip()
    return text or None


def _count(out: Optional[bytes]) -> Optional[int]:
    text = _line(out)
    return int(text) if text is not None and text.isdigit() else None


def git_facts(path: Any, timeout: float = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """What git says about the working tree at ``path``. Never raises.

    None when ``path`` is not a directory inside a git work tree (or git is missing). Otherwise
    ``{"dirty": int|None, "branch": str|None, "upstream": str|None, "ahead": int|None,
    "timed_out": bool}``: ``dirty`` = entries in ``git status --porcelain`` (default untracked
    mode, so a new directory counts once), ``branch`` = the current branch (None when detached),
    ``upstream`` = e.g. ``origin/main`` (None when there is none), ``ahead`` = commits in
    ``@{u}..HEAD``. With no upstream in a repo that has a remote, ``ahead`` = commits on HEAD that
    are on no remote (``HEAD --not --remotes``: a local branch nobody pushed), else None.
    ``timeout`` bounds all the git calls together; on a timeout the facts found so far are
    returned with ``timed_out`` set.
    """
    try:
        p = os.fspath(path)
        if not isinstance(p, str) or not p or "\x00" in p or not os.path.isdir(p):
            return None
        budget = float(timeout)
        if budget != budget:  # NaN
            budget = DEFAULT_TIMEOUT
    except (TypeError, ValueError, OSError):
        return None
    deadline = time.monotonic() + max(budget, 0.0)
    env = _env()
    facts: Dict[str, Any] = {"dirty": None, "branch": None, "upstream": None, "ahead": None, "timed_out": False}
    try:
        status = _git(p, ["status", "--porcelain"], deadline, env)
        if status is None:
            return None  # not a work tree (or a repository git refuses to read)
        facts["dirty"] = sum(1 for line in status.split(b"\n") if line.strip())
        facts["branch"] = _line(_git(p, ["symbolic-ref", "--short", "-q", "HEAD"], deadline, env))
        facts["upstream"] = _line(_git(p, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                                       deadline, env))
        if facts["upstream"] is not None:
            facts["ahead"] = _count(_git(p, ["rev-list", "--count", "@{u}..HEAD"], deadline, env))
        elif _line(_git(p, ["remote"], deadline, env)) is not None:
            # No upstream, but the repo has a remote: commits that exist only on this machine.
            facts["ahead"] = _count(_git(p, ["rev-list", "--count", "HEAD", "--not", "--remotes"], deadline, env))
    except _Timeout:
        facts["timed_out"] = True
    except (OSError, ValueError, subprocess.SubprocessError):
        # git missing or not runnable: no facts at all unless status already answered
        if facts["dirty"] is None:
            return None
    return facts


def unsaved(facts: Optional[Mapping[str, Any]]) -> Optional[bool]:
    """True when the repo has uncommitted entries or unpushed commits, False when it has neither,
    None when unknown (not a git repo, git timed out). Feeds ``classify.tier``'s ``unsaved``."""
    if not facts:
        return None
    dirty = _int(facts.get("dirty"))
    if dirty is None:
        return None
    ahead = _int(facts.get("ahead"))
    if dirty > 0 or (ahead or 0) > 0:
        return True
    # A clean tree says nothing about unpushed commits when git timed out before counting them,
    # or when there is an upstream but the count failed. No upstream with every commit on a remote
    # (or no remote at all) counts as saved (git_text: "clean tree, no upstream"); commits on no
    # remote set ``ahead`` above.
    if facts.get("timed_out") or (facts.get("upstream") and ahead is None):
        return None
    return False


_UNSAVED_TEXT_RE = re.compile(r"\b([1-9]\d*) (?:uncommitted|unpushed)\b")


def unsaved_from_text(text: Any) -> Optional[bool]:
    """``unsaved`` read back from an evidence string this module wrote ("12 uncommitted, 2
    unpushed", "clean tree", "not a git repo"). None when the string says nothing either way."""
    if not isinstance(text, str):
        return None
    if _UNSAVED_TEXT_RE.search(text):
        return True
    if "0 uncommitted" in text or "clean tree" in text:
        return False
    return None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def git_text(facts: Optional[Mapping[str, Any]], branch: bool = True) -> str:
    """The git half of the evidence: ``"91 uncommitted, 2 unpushed"``, ``"18 uncommitted on
    round-2, no upstream"``, ``"clean tree on feat, 2 unpushed, no upstream"`` (commits on no
    remote), ``"clean tree"``, ``"not a git repo"``. With ``branch=False`` the branch name is left
    out (``"18 uncommitted, no upstream"``)."""
    if not facts:
        return NOT_A_REPO
    dirty = _int(facts.get("dirty"))
    if dirty is None:
        return UNAVAILABLE
    upstream = facts.get("upstream")
    if upstream:
        ahead = _int(facts.get("ahead")) or 0
        parts: List[str] = []
        if dirty:
            parts.append("%d uncommitted" % dirty)
        if ahead:
            parts.append("%d unpushed" % ahead)
        return ", ".join(parts) if parts else CLEAN_TREE
    head = "%d uncommitted" % dirty if dirty else CLEAN_TREE
    name = facts.get("branch") if branch else None
    if isinstance(name, str) and name.strip():
        head += " on " + name.strip()
    ahead = _int(facts.get("ahead"))
    if ahead:
        head += ", %d unpushed" % ahead
    return head + ", " + NO_UPSTREAM


def counts_text(facts: Optional[Mapping[str, Any]]) -> str:
    """Counts only (``"18 uncommitted, 2 unpushed"``), '' when there is nothing to count."""
    if not facts:
        return ""
    parts: List[str] = []
    dirty = _int(facts.get("dirty"))
    ahead = _int(facts.get("ahead"))
    if dirty:
        parts.append("%d uncommitted" % dirty)
    if ahead:
        parts.append("%d unpushed" % ahead)
    return ", ".join(parts)


def evidence_text(ending: str, facts: Optional[Mapping[str, Any]], resumed: Optional[str] = None) -> str:
    """``"{ending} · {git}"`` (``"{ending} · resumed MM-DD, ended clean · {git}"`` when resumed).

    The result always passes ``sanitize.problems()``: when the full string would not, the branch
    is dropped, then everything but the counts, then the git part altogether.
    """
    head = ending
    if resumed:
        head += SEP + "resumed %s, ended clean" % resumed
    for git in (git_text(facts, branch=True), git_text(facts, branch=False), counts_text(facts)):
        candidate = head + SEP + git if git else head
        if not sanitize.problems(candidate):
            return candidate
    if not sanitize.problems(head):
        return head
    return ending
