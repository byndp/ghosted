"""Read a Claude Code session transcript (JSONL) into a SessionSummary (contract section 7).

Privacy: transcript text is held in memory only. Nothing in this module prints or logs it, and the
two text fields of SessionSummary (``final_text``, ``first_human_text``) are left out of its repr, so
an accidental print or a failing test assertion cannot leak them. Use ``SessionSummary.safe_dict()``
for diagnostics.

Two readers, one result:

* ``parse_transcript(path)`` reads the head (first HEAD_LINES lines) and an expanding tail window
  (TAIL_START_BYTES, doubled until the window holds the final assistant text, or the file start is
  reached). Cheap on huge files.
* ``parse_lines(lines, session_uuid, path)`` parses an in-memory list (fixtures, a line cut).

Both return the same summary for the same content: every tail field is a "last occurrence" (or is
determined by what follows the final text), and the window is always a suffix of whole lines, so once
the window holds the final text it holds everything the summary needs.

The schema-drift counters (``lines_read``, ``conversation_entries``, ``unknown_types``; contract 16.6)
describe the lines ``parse_transcript`` actually decodes: the head plus the final tail window, each
line once. ``parse_lines`` sees every line, so it works out which lines that would be (the same window
arithmetic on the lines' byte lengths) and counts only those; the two readers agree on them too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from bisect import bisect_left
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

HEAD_LINES = 400                 # head: launch cwd, entrypoints, first human prompt, version
TAIL_START_BYTES = 256 * 1024    # first tail window; doubled until the final text is inside it
VERSION_TAIL_BYTES = 64 * 1024   # content_version hashes this many trailing bytes

# Entry types that carry conversation state. Everything else (mode, last-prompt, ai-title,
# queue-operation, file-history-*, artifact-*, cost-state, agent-*, ...) is metadata.
CONVERSATION_TYPES = frozenset(("user", "assistant", "attachment", "system"))

# Assistant blocks that mean "the turn kept going after the final text".
TOOL_USE_TYPES = frozenset(("tool_use", "server_tool_use", "mcp_tool_use"))
# A Bash call that registers a loose end: `ghosted add` in command position (also `./bin/ghosted add`,
# `X=1 ghosted add`, after `;` `&&` `||` `|` `(`). Mentioning it (`echo ghosted add`, `grep 'ghosted add'`) is not
# running it. Unbalanced quotes fall back to GHOSTED_ADD_RE at a command start.
GHOSTED_ADD_RE = re.compile(r"(?:^|[;&|(]\s*)(?:[A-Za-z_]\w*=\S*\s+)*(?:\S*/)?ghosted\s+add(?:\s|$)")
_SHELL_SEPARATORS = frozenset((";", "&&", "||", "|", "&", "(", ")", ";;", "|&"))
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*=")


def ran_le_add(command: Any) -> bool:
    """True when the shell command runs `ghosted add` (contract 16.10)."""
    if not isinstance(command, str) or "ghosted" not in command:
        return False
    for line in command.splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            if GHOSTED_ADD_RE.search(line.strip()):
                return True
            continue
        at_start = True
        for i, token in enumerate(tokens):
            if token in _SHELL_SEPARATORS:
                at_start = True
                continue
            if at_start and _ASSIGNMENT_RE.match(token):
                continue  # FOO=1 ghosted add
            if at_start and (token == "ghosted" or token.endswith("/ghosted")) and tokens[i + 1:i + 2] == ["add"]:
                return True
            at_start = False
    return False

# Tools that put a question to the person through Claude Code's UI (contract section 7 rule 4). A
# session whose last tool use is one of these, with no tool_result for it, ended waiting on an answer.
PENDING_QUESTION_TOOLS = frozenset(("AskUserQuestion", "ExitPlanMode"))

# Entry types Claude Code writes that are known metadata (observed 2026-11-08, contract 16.6). A type
# that is neither conversation nor listed here (or under a listed prefix) is counted in
# ``SessionSummary.unknown_types``: a burst of those is the first sign the transcript format moved.
KNOWN_METADATA_TYPES = frozenset((
    "last-prompt", "atis-latch", "bridge-session", "mode", "ai-title", "api-request", "api-request-blob",
    "api-request-shape", "frame-link", "permission-mode", "session-owner", "file-history-snapshot",
    "file-history-delta", "queue-operation", "artifact-autoreact-ledger", "artifact-comment-monitor",
    "tools-used", "cost-state", "agent-name", "custom-title", "summary",
))
KNOWN_METADATA_PREFIXES = ("artifact-", "agent-", "file-history-", "api-request")

# Unknown type names end up in state.json and in `ghosted doctor` output, so only schema-looking names are
# kept verbatim; anything else (long, spaces, punctuation) is counted under UNKNOWN_TYPE_OTHER.
_TYPE_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
UNKNOWN_TYPE_OTHER = "(other)"

# What counts as a blank line for ``lines_read``: ASCII whitespace only, for bytes and str alike, so
# the str and bytes forms of the same line agree.
_BLANK_CHARS = " \t\r\n\x0b\x0c"
_BLANK_BYTES = _BLANK_CHARS.encode("ascii")

# Non-text user blocks that count as human content (a pasted screenshot or PDF).
HUMAN_MEDIA_TYPES = frozenset(("image", "document"))

# Wrapper blocks removed (tag and contents) before deciding whether user text is human.
WRAPPER_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "command-name",
    "command-message",
    "command-args",
    "task-notification",
    "bash-input",
    "bash-stdout",
    "bash-stderr",
    "user-prompt-submit-hook",
)

# A string that STARTS with one of these complete blocks is system-generated as a whole, even when
# plain text follows the block (e.g. a task notification followed by "Read the output file ...").
# system-reminder and user-prompt-submit-hook are injected around human text, so they are only
# stripped, never used to disqualify the rest of the string.
SYSTEM_LEADING_TAGS = tuple(t for t in WRAPPER_TAGS if t not in ("system-reminder", "user-prompt-submit-hook"))

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Optional attributes inside a wrapper tag. Bounded: an unbounded ``[^>]*`` rescans to the end of the
# string for every ``<tag `` that never closes, which is quadratic (90K chars of ``<system-reminder a``
# took 7.7 s). Claude Code's wrapper tags carry no attributes at all.
_ATTRS = r"(?:\s[^>]{0,256})?"


def _block_re(tags: Tuple[str, ...]) -> "re.Pattern[str]":
    """Innermost ``<tag ...>...</tag>`` blocks (content holds no opener of the same tag), or ``<tag/>``.

    Applied repeatedly, so nested blocks of the same name go from the inside out.
    """
    names = "|".join(re.escape(t) for t in tags)
    return re.compile(
        r"<(%s)%s>(?:(?!<\1[\s>/]).)*?</\1\s*>|<(?:%s)%s/>" % (names, _ATTRS, names, _ATTRS),
        re.DOTALL,
    )


_INJECTED_RE = _block_re(("system-reminder", "user-prompt-submit-hook"))
_WRAPPERS_RE = _block_re(WRAPPER_TAGS)
_LEADING_OPEN_RE = re.compile(r"\s*<(%s)%s>" % ("|".join(re.escape(t) for t in SYSTEM_LEADING_TAGS), _ATTRS))
_TAG_TOKEN_RE = {t: re.compile(r"<(/?)%s%s>" % (re.escape(t), _ATTRS)) for t in SYSTEM_LEADING_TAGS}
_INTERRUPT_RE = re.compile(r"\[Request interrupted by user[^\]\n]*\]")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def _starts_with_system_block(text: str) -> bool:
    """True when ``text`` opens with a complete (properly closed, nesting-aware) system block."""
    m = _LEADING_OPEN_RE.match(text)
    if not m:
        return False
    depth = 1
    for tok in _TAG_TOKEN_RE[m.group(1)].finditer(text, m.end()):
        depth += -1 if tok.group(1) else 1
        if depth == 0:
            return True
    return False


@dataclass
class SessionSummary:
    """What a transcript says about how its session ended. Text fields are not in the repr.

    Head fields (first HEAD_LINES lines, main-chain conversation entries only):
      launch_cwd        first ``cwd`` (fallback: the cwd of the final text's entry)
      entrypoints       every ``entrypoint`` value ("cli", "sdk-cli")
      first_human_text  the first human prompt, wrappers removed ("" when image-only; None if none)
      version           first ``version``
      head_human_turns  human turns (prompts and interrupt markers) among those lines
    Tail fields:
      final_*           the entry holding the last non-blank assistant ``text`` block (API errors too)
      final_resets_at   ``quotaLimits.resetsAt`` of that entry as int epoch seconds (else None)
      git_branch        ``gitBranch`` of that entry (fallback: the first one in the head)
      human_after_final / tool_use_after_final   a human turn / assistant tool use after the final text
      pending_tool      "AskUserQuestion" or "ExitPlanMode" when that is the last assistant tool use
                        and no tool_result with its id follows, else None. Only tool uses after the
                        final text count (a text block written after a question tool means the
                        conversation moved past it), which also keeps the two readers equal: the tail
                        window always holds everything after the final text.
      last_ts           ``timestamp`` of the last main-chain conversation entry
      pending_question  the question behind ``pending_tool`` (contract 16.2), set only when
                        ``pending_tool`` is: AskUserQuestion ``questions[0].question`` plus
                        " (+N more)", or legacy ``question``; ExitPlanMode the first plan line with its
                        leading ``#``/``-``/``*``/spaces stripped. None when absent or not text.
    Drift counters (contract 16.6), over the lines decoded (head + final tail window, each once):
      lines_read           non-blank physical lines, bad JSON included
      conversation_entries entries whose type is in CONVERSATION_TYPES (sidechains included)
      unknown_types        ``{type: count}`` for types neither conversation nor known metadata
    ``size`` is the byte count parsed; ``mtime`` the file's mtime (None without a file).
    """

    session_uuid: str
    path: Optional[str] = None
    size: int = 0
    mtime: Optional[float] = None
    launch_cwd: Optional[str] = None
    entrypoints: Set[str] = field(default_factory=set)
    first_human_text: Optional[str] = field(default=None, repr=False)
    git_branch: Optional[str] = None
    version: Optional[str] = None
    final_text: Optional[str] = field(default=None, repr=False)
    final_ts: Optional[str] = None
    final_is_api_error: bool = False
    final_error: Optional[str] = None
    final_status: Optional[int] = None
    final_has_quota: bool = False
    human_after_final: bool = False
    tool_use_after_final: bool = False
    last_ts: Optional[str] = None
    pending_tool: Optional[str] = None
    head_human_turns: int = 0
    final_resets_at: Optional[int] = None
    pending_question: Optional[str] = field(default=None, repr=False)
    # A Bash `ghosted add` ran after the last human turn (contract 16.10): the hand-off was registered.
    le_add_last_turn: bool = False
    # compare=False: the drift counters describe how much was read, which depends on the window size
    # (TAIL_START_BYTES, HEAD_LINES, head=False), while ``==`` means "the same account of how the
    # session ended" and is window-independent (tests compare readers across window sizes). The
    # readers still agree on the counters for the same constants; tests check that explicitly.
    lines_read: int = field(default=0, compare=False)
    conversation_entries: int = field(default=0, compare=False)
    unknown_types: Dict[str, int] = field(default_factory=dict, compare=False)

    def safe_dict(self) -> Dict[str, Any]:
        """Content-free facts only (no text, no paths, no branch): safe to print or log."""
        return {
            "session_uuid": self.session_uuid,
            "size": self.size,
            "entrypoints": sorted(self.entrypoints),
            "version": self.version,
            "has_first_human_text": self.first_human_text is not None,
            "final_text_len": len(self.final_text) if self.final_text is not None else None,
            "final_ts": self.final_ts,
            "final_is_api_error": self.final_is_api_error,
            "final_error": (self.final_error if self.final_error is None or _ERROR_CODE_RE.fullmatch(self.final_error)
                            else "other"),
            "final_status": self.final_status,
            "final_has_quota": self.final_has_quota,
            "human_after_final": self.human_after_final,
            "tool_use_after_final": self.tool_use_after_final,
            "last_ts": self.last_ts,
            "pending_tool": (self.pending_tool if isinstance(self.pending_tool, str)
                             and self.pending_tool in PENDING_QUESTION_TOOLS else None),
            "head_human_turns": self.head_human_turns,
            "final_resets_at": self.final_resets_at,
            "has_pending_question": self.pending_question is not None,
            "lines_read": self.lines_read,
            "conversation_entries": self.conversation_entries,
            "unknown_types": dict(self.unknown_types),
        }


# --------------------------------------------------------------------------- content version

def content_version_of(data: bytes) -> str:
    """``"<size>:<sha1 hex of the last 64 KiB>"`` for an in-memory transcript."""
    return "%d:%s" % (len(data), hashlib.sha1(data[-VERSION_TAIL_BYTES:]).hexdigest())


def content_version(path: Union[str, "os.PathLike[str]"]) -> str:
    """``"<size>:<sha1 hex of the last 64 KiB>"`` (contract section 4). Change detection only."""
    with open(os.fspath(path), "rb") as f:
        size = os.fstat(f.fileno()).st_size
        start = max(0, size - VERSION_TAIL_BYTES)
        f.seek(start)
        data = f.read(size - start)
    return "%d:%s" % (size, hashlib.sha1(data).hexdigest())


def is_session_file(path: Union[str, "os.PathLike[str]"]) -> bool:
    """True when the file name is ``<uuid>.jsonl``."""
    name = os.path.basename(os.fspath(path))
    return name.endswith(".jsonl") and bool(_UUID_RE.match(name[: -len(".jsonl")]))


# --------------------------------------------------------------------------- human turns

def _strip_blocks(rx: "re.Pattern[str]", text: str) -> str:
    for _ in range(8):  # repeat so nested or adjacent blocks all go
        stripped = rx.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text


def human_text(text: Any) -> Optional[str]:
    """The human part of one user string, or None when it is system-generated or blank.

    Removes wrapper blocks (tag and contents). A string that starts with a complete system block
    (task notification, slash-command wrapper, local-command output, bash-mode wrapper) is
    system-generated as a whole. ``<pasted_content ...>`` is human text and is kept.
    """
    if not isinstance(text, str):
        return None
    s = _strip_blocks(_INJECTED_RE, text)
    if _starts_with_system_block(s):
        return None
    s = _strip_blocks(_WRAPPERS_RE, s).strip()
    return s or None


def _type(obj: Any) -> Optional[str]:
    """``obj["type"]`` when ``obj`` is a dict and the value is a string, else None.

    Membership tests on a raw value would raise TypeError for a list or dict ``type`` (valid JSON,
    malformed entry), and one such line must not abort the whole parse.
    """
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    return t if isinstance(t, str) else None


def _human_origin(origin: Any) -> bool:
    """An ``origin`` that may carry a human turn: absent/null or ``{"kind": "human"}``.

    Peer messages, task notifications and auto-continuations are not human (contract section 7).
    """
    return origin is None or (isinstance(origin, dict) and origin.get("kind") == "human")


def _human_content(content: Any) -> Optional[Tuple[str, str]]:
    texts: List[str] = []
    media = False
    if isinstance(content, str):
        h = human_text(content)
        if h:
            texts.append(h)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                h = human_text(block)
            elif isinstance(block, dict):
                btype = _type(block)
                if btype == "text":
                    h = human_text(block.get("text"))
                else:
                    if btype in HUMAN_MEDIA_TYPES:
                        media = True
                    h = None  # tool_result and anything else contribute nothing
            else:
                h = None
            if h:
                texts.append(h)
    if not texts and not media:
        return None
    text = "\n".join(texts)
    if not media and _INTERRUPT_RE.fullmatch(text):
        return ("interrupt", text)
    return ("prompt", text)


def human_turn(entry: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """``("prompt", text)`` or ``("interrupt", text)`` when the entry is a human turn, else None.

    A ``user`` entry counts unless it is meta, a compact summary, transcript-only, a tool result, or
    carries a non-human ``origin`` (peer, task-notification, auto-continuation), and only when real
    text (or an image) is left after removing wrappers. A ``queued_command`` attachment in prompt
    mode with a null or human origin is a human turn; task notifications and peer origins are not.
    ``[Request interrupted by user...]`` counts as human activity.
    Sidechain filtering is the caller's job.
    """
    etype = _type(entry)
    if etype == "user":
        if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
            return None
        if "toolUseResult" in entry or not _human_origin(entry.get("origin")):
            return None
        msg = entry.get("message")
        return _human_content(msg.get("content") if isinstance(msg, dict) else None)
    if etype == "attachment":
        att = entry.get("attachment")
        if _type(att) != "queued_command":
            return None
        if att.get("commandMode") != "prompt" or not _human_origin(att.get("origin")):
            return None
        return _human_content(att.get("prompt"))
    return None


# --------------------------------------------------------------------------- scanners

def _decode(raw: Union[bytes, str]) -> Optional[Dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.strip()
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        return None
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _conversation(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The entry when it is a main-chain conversation entry, else None (metadata, sidechain, junk)."""
    if entry is None or _type(entry) not in CONVERSATION_TYPES or entry.get("isSidechain"):
        return None
    return entry


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _assistant_blocks(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _status(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _resets_at(quota: Any) -> Optional[int]:
    """``quotaLimits.resetsAt`` as int epoch seconds (an int, a whole float or a digit string)."""
    if not isinstance(quota, dict):
        return None
    value = quota.get("resetsAt")
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        value = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and value > 0:
        return value
    return None


def _tool_use_ref(block: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    name = block.get("name")
    tid = block.get("id")
    return (name if isinstance(name, str) else "", tid if isinstance(tid, str) and tid else None)


def _answers(entry: Dict[str, Any], tool_id: Optional[str]) -> bool:
    """True when this user entry holds a tool_result for ``tool_id`` (any tool_result when the
    tool use carried no id, which only happens in malformed lines)."""
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return False
    for block in content:
        if _type(block) == "tool_result" and (tool_id is None or block.get("tool_use_id") == tool_id):
            return True
    return False


def pending_question_text(tool: Optional[str], tool_input: Any) -> Optional[str]:
    """The question a pending AskUserQuestion / ExitPlanMode puts to the person (contract 16.2).

    AskUserQuestion: ``questions[0].question``, plus `` (+N more)`` for N further questions; the
    legacy single ``question`` string is the fallback. ExitPlanMode: the first plan line that is not
    blank once its leading markdown (``#``, ``-``, ``*``) and spaces are stripped. None otherwise.
    """
    if not isinstance(tool_input, dict):
        return None
    if tool == "AskUserQuestion":
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            q = _nonblank_str(first.get("question")) if isinstance(first, dict) else None
            if q is not None:
                more = len(questions) - 1
                return q.strip() + (" (+%d more)" % more if more > 0 else "")
        legacy = _nonblank_str(tool_input.get("question"))
        return legacy.strip() if legacy is not None else None
    if tool == "ExitPlanMode":
        plan = tool_input.get("plan")
        if not isinstance(plan, str):
            return None
        for line in plan.splitlines():
            text = line.lstrip("#-* \t").rstrip()
            if text:
                return text
    return None


def _blank(raw: Any) -> bool:
    if isinstance(raw, bytes):
        return not raw.strip(_BLANK_BYTES)
    if isinstance(raw, str):
        return not raw.strip(_BLANK_CHARS)
    return True


class _Drift:
    """Schema-drift counters over the lines one reader decoded (contract 16.6)."""

    __slots__ = ("lines", "conversation", "unknown")

    def __init__(self) -> None:
        self.lines = 0
        self.conversation = 0
        self.unknown: Dict[str, int] = {}

    def feed(self, raw: Union[bytes, str], entry: Optional[Dict[str, Any]]) -> None:
        """Count one line. Bad JSON (or a JSON non-object) is still a line read; blank is not."""
        if entry is not None:
            self.count(entry.get("type"))
        elif not _blank(raw):
            self.count(None)

    def count(self, kind: Any) -> None:
        """Count one non-blank line whose entry ``type`` is ``kind`` (None for an undecodable line).

        Split from ``feed`` so ``parse_transcript`` can keep just the type values while it reads a
        window and count once, for the window it ends on (the doubling passes before it are wasted).
        """
        self.lines += 1
        if not isinstance(kind, str):
            return
        if kind in CONVERSATION_TYPES:
            self.conversation += 1
        elif kind not in KNOWN_METADATA_TYPES and not kind.startswith(KNOWN_METADATA_PREFIXES):
            key = kind if _TYPE_NAME_RE.fullmatch(kind) else UNKNOWN_TYPE_OTHER
            self.unknown[key] = self.unknown.get(key, 0) + 1


class _Head:
    __slots__ = ("cwd", "entrypoints", "version", "branch", "first_human", "human_turns")

    def __init__(self) -> None:
        self.cwd: Optional[str] = None
        self.entrypoints: Set[str] = set()
        self.version: Optional[str] = None
        self.branch: Optional[str] = None
        self.first_human: Optional[str] = None
        self.human_turns = 0

    def feed(self, raw_entry: Optional[Dict[str, Any]]) -> None:
        entry = _conversation(raw_entry)
        if entry is None:
            return
        if self.cwd is None:
            self.cwd = _nonblank_str(entry.get("cwd"))
        ep = _nonblank_str(entry.get("entrypoint"))
        if ep:
            self.entrypoints.add(ep)
        if self.version is None:
            self.version = _nonblank_str(entry.get("version"))
        if self.branch is None:
            self.branch = _nonblank_str(entry.get("gitBranch"))
        turn = human_turn(entry)
        if turn is not None:
            self.human_turns += 1
            if self.first_human is None and turn[0] == "prompt":
                self.first_human = turn[1]


class _Tail:
    __slots__ = ("final", "human_after", "tool_after", "last_ts", "tool", "tool_open", "tool_input", "le_add",
                 "human_seen", "le_adds")

    def __init__(self) -> None:
        self.final: Optional[Dict[str, Any]] = None
        self.human_after = False
        self.tool_after = False
        self.last_ts: Optional[str] = None
        # The last assistant tool use since the final text (name, id), and whether no tool_result
        # for it has followed yet. A new final text forgets it (see SessionSummary.pending_tool).
        self.tool: Optional[Tuple[str, Optional[str]]] = None
        self.tool_open = False
        self.tool_input: Any = None  # that tool use's ``input`` (read only for pending_question)
        self.le_add = False  # a Bash `ghosted add` since the last human turn seen
        self.human_seen = False  # any human turn in the window (the last turn's start is in it)
        self.le_adds = 0  # `ghosted add` calls seen (parse_lines locates the last one)

    def feed(self, raw_entry: Optional[Dict[str, Any]]) -> None:
        entry = _conversation(raw_entry)
        if entry is None:
            return
        ts = _nonblank_str(entry.get("timestamp"))
        if ts:
            self.last_ts = ts
        etype = _type(entry)
        if etype == "assistant":
            for block in _assistant_blocks(entry):
                btype = _type(block)
                if btype == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        self.final = {
                            "text": text,
                            "ts": ts,
                            "is_api_error": bool(entry.get("isApiErrorMessage")),
                            "error": _nonblank_str(entry.get("error")),
                            "status": _status(entry.get("apiErrorStatus")),
                            "has_quota": entry.get("quotaLimits") is not None,
                            "resets_at": _resets_at(entry.get("quotaLimits")),
                            "cwd": _nonblank_str(entry.get("cwd")),
                            "branch": _nonblank_str(entry.get("gitBranch")),
                        }
                        self.human_after = False
                        self.tool_after = False
                        self.tool = None
                        self.tool_open = False
                        self.tool_input = None
                elif btype in TOOL_USE_TYPES:
                    if self.final is not None:
                        self.tool_after = True
                    if block.get("name") == "Bash" and isinstance(block.get("input"), dict):
                        command = block["input"].get("command")
                        if ran_le_add(command):
                            self.le_add = True
                            self.le_adds += 1
                    self.tool = _tool_use_ref(block)
                    self.tool_open = True
                    self.tool_input = block.get("input")
            return
        if etype == "user" and self.tool_open and self.tool is not None and _answers(entry, self.tool[1]):
            self.tool_open = False
        if etype in ("user", "attachment") and human_turn(entry) is not None:
            self.le_add = False
            self.human_seen = True
            if self.final is not None:
                self.human_after = True

    def pending_tool(self) -> Optional[str]:
        if self.tool_open and self.tool is not None and self.tool[0] in PENDING_QUESTION_TOOLS:
            return self.tool[0]
        return None

    def pending_question(self) -> Optional[str]:
        tool = self.pending_tool()
        return pending_question_text(tool, self.tool_input) if tool is not None else None

    def complete(self) -> bool:
        """Enough read: a final text, and either the last turn's start (a human turn) or a
        registration already seen in it. Without the turn's start, an `ghosted add` early in a long last
        turn would be missed, so the window keeps growing."""
        return self.final is not None and self.last_ts is not None and (self.human_seen or self.le_add)


def _summary(uuid: str, path: Optional[str], size: int, mtime: Optional[float], head: _Head, tail: _Tail,
             drift: _Drift) -> SessionSummary:
    final = tail.final or {}
    return SessionSummary(
        session_uuid=uuid,
        path=path,
        size=size,
        mtime=mtime,
        launch_cwd=head.cwd or final.get("cwd"),
        entrypoints=set(head.entrypoints),
        first_human_text=head.first_human,
        git_branch=final.get("branch") or head.branch,
        version=head.version,
        final_text=final.get("text"),
        final_ts=final.get("ts"),
        final_is_api_error=bool(final.get("is_api_error")),
        final_error=final.get("error"),
        final_status=final.get("status"),
        final_has_quota=bool(final.get("has_quota")),
        human_after_final=bool(tail.final is not None and tail.human_after),
        tool_use_after_final=bool(tail.final is not None and tail.tool_after),
        last_ts=tail.last_ts,
        pending_tool=tail.pending_tool(),
        head_human_turns=head.human_turns,
        final_resets_at=final.get("resets_at"),
        pending_question=tail.pending_question(),
        le_add_last_turn=bool(tail.le_add),
        lines_read=drift.lines,
        conversation_entries=drift.conversation,
        unknown_types=dict(drift.unknown),
    )


# --------------------------------------------------------------------------- readers

def split_lines(data: Union[bytes, str]) -> List[Union[bytes, str]]:
    """Split on newline only (never on U+2028 or CR), keeping each line's terminator."""
    nl = b"\n" if isinstance(data, bytes) else "\n"
    parts = data.split(nl)  # type: ignore[arg-type]
    out = [p + nl for p in parts[:-1]]  # type: ignore[operator]
    if parts[-1]:
        out.append(parts[-1])
    return out  # type: ignore[return-value]


def _stat_mtime(path: Optional[str]) -> Optional[float]:
    if path is None:
        return None
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def parse_lines(
    lines: Iterable[Union[bytes, str]],
    session_uuid: str,
    path: Optional[Union[str, "os.PathLike[str]"]] = None,
    *,
    head: bool = True,
) -> SessionSummary:
    """Parse an in-memory list of transcript lines (fixtures, a line cut).

    Lines are split on newline only. ``size`` is the byte count of the lines as given; ``mtime``
    comes from ``path`` when it names an existing file. A line given without its newline is treated
    as having one (except the last), for the drift counters' window arithmetic.
    """
    if isinstance(lines, (bytes, str)):
        lines = split_lines(lines)
    line_list = list(lines)
    p = os.fspath(path) if path is not None else None
    byte_lens = [len(x) if isinstance(x, bytes) else len(x.encode("utf-8")) for x in line_list]
    size = sum(byte_lens)
    entries = [_decode(raw) for raw in line_list]
    h = _Head()
    if head:
        for entry in entries[:HEAD_LINES]:
            h.feed(entry)
    t = _Tail()
    last_final = last_ts = -1  # the last line holding a final text / a main-chain timestamp
    last_human = last_le_add = -1  # the last human turn / the last `ghosted add` after it
    for i, entry in enumerate(entries):
        before = t.final
        adds_before = t.le_adds
        t.feed(entry)
        if t.final is not before:
            last_final = i
        conv = _conversation(entry)
        if conv is not None and _nonblank_str(conv.get("timestamp")):
            last_ts = i
        if t.le_adds != adds_before:
            last_le_add = i
        if conv is not None and _type(conv) in ("user", "attachment") and human_turn(conv) is not None:
            last_human = i
    # _Tail.complete also needs the last turn's start (or an `ghosted add` inside that turn) in the window.
    turn = last_le_add if last_le_add > last_human else last_human
    needed = min(last_final, last_ts, turn) if min(last_final, last_ts, turn) >= 0 else -1
    first_tail = _tail_window_first_line(line_list, byte_lens, needed)
    head_n = min(HEAD_LINES, len(line_list)) if head else 0
    d = _Drift()
    for i in range(head_n):
        d.feed(line_list[i], entries[i])
    for i in range(max(head_n, first_tail), len(line_list)):
        d.feed(line_list[i], entries[i])
    return _summary(session_uuid, p, size, _stat_mtime(p), h, t, d)


def _ends_with_newline(raw: Union[bytes, str]) -> bool:
    return raw.endswith(b"\n") if isinstance(raw, bytes) else raw.endswith("\n")


def _tail_window_first_line(line_list: List[Union[bytes, str]], byte_lens: List[int], needed: int) -> int:
    """Index of the first line in the tail window ``parse_transcript`` would end on for this content.

    Mirrors its loop: the window starts at TAIL_START_BYTES and doubles; the window holds the lines
    that start at or after ``file size - window``; it stops once that suffix holds a final text and a
    timestamp (``_Tail.complete``), which is exactly when its first line index is <= ``needed``, or
    when it reaches the file start.
    """
    n = len(line_list)
    if n == 0:
        return 0
    offsets: List[int] = []
    pos = 0
    for raw, length in zip(line_list, byte_lens):
        offsets.append(pos)
        pos += length if _ends_with_newline(raw) else length + 1
    fsize = pos if _ends_with_newline(line_list[-1]) else pos - 1
    window = max(1, int(TAIL_START_BYTES))
    while True:
        start = max(0, fsize - window)
        if start == 0:
            return 0
        first = bisect_left(offsets, start)
        if first <= needed:
            return first
        window *= 2


def _read_head_lines(f: Any, size: int, limit: int) -> List[bytes]:
    f.seek(0)
    out: List[bytes] = []
    remaining = size
    while len(out) < limit and remaining > 0:
        line = f.readline(remaining)
        if not line:
            break
        remaining -= len(line)
        out.append(line)
    return out


def _read_window(f: Any, start: int, size: int) -> List[bytes]:
    """Whole lines in bytes [start, size). A line cut by ``start`` is dropped."""
    if start <= 0:
        f.seek(0)
        data = f.read(size)
    else:
        f.seek(start - 1)  # one byte early: if it is a newline, the line at ``start`` is whole
        data = f.read(size - start + 1)
        nl = data.find(b"\n")
        if nl < 0:
            return []
        data = data[nl + 1:]
    return data.split(b"\n")


def _head_overlap(lines: List[bytes], start: int, size: int, head_end: int) -> int:
    """How many lines at the front of a tail window the head already read (and counted).

    The window and the head both end on line boundaries, so this is a whole number of lines. Only
    small files (or a huge head) overlap; otherwise the window starts past the head and this is 0.
    """
    if start >= head_end:
        return 0
    pos = size - (sum(len(x) for x in lines) + len(lines) - 1)  # byte offset of the window's first line
    n = 0
    for raw in lines:
        if pos >= head_end:
            break
        pos += len(raw) + 1
        n += 1
    return n


def parse_transcript(
    path: Union[str, "os.PathLike[str]"],
    max_lines: Optional[int] = None,
    *,
    head: bool = True,
    max_tail_bytes: Optional[int] = None,
) -> SessionSummary:
    """Summarise one transcript file: the head plus an expanding tail window.

    ``max_lines``: treat the file as if it ended after that many lines (a line cut; the whole
    prefix is parsed, ``size`` is the prefix's byte count).
    ``head=False``: tail-only mode for the hook (head fields stay empty; ``launch_cwd`` and
    ``git_branch`` then come from the final text's entry).
    ``max_tail_bytes``: stop growing the window at this size even if no final text was found
    (bounded cost for the hook); the summary may then differ from ``parse_lines``.
    """
    p = os.fspath(path)
    name = os.path.basename(p)
    uuid = name[: -len(".jsonl")] if name.endswith(".jsonl") else os.path.splitext(name)[0]
    with open(p, "rb") as f:
        st = os.fstat(f.fileno())
        size = st.st_size
        if max_lines is not None:
            lines = _read_head_lines(f, size, max(0, int(max_lines)))
            return parse_lines(lines, uuid, p, head=head)
        # Drift: the entry ``type`` of every line read (None: undecodable; blank lines skipped),
        # counted once at the end. Each window's lines are a suffix of the next (bigger) window's, so
        # a pass collects only the lines in front of the previous window, minus those the head read:
        # every line is collected once however often the window doubles.
        kinds: List[Any] = []
        append = kinds.append
        h = _Head()
        head_end = 0  # byte offset just past the head's last line
        if head:
            for raw in _read_head_lines(f, size, HEAD_LINES):
                entry = _decode(raw)
                h.feed(entry)
                head_end += len(raw)
                if entry is not None:
                    append(entry.get("type"))
                elif raw.strip(_BLANK_BYTES):
                    append(None)
        seen = 0  # lines in the previous window
        window = max(1, int(TAIL_START_BYTES))
        while True:
            start = max(0, size - window)
            t = _Tail()
            lines = _read_window(f, start, size)
            fresh = max(0, len(lines) - seen)
            overlap = min(fresh, _head_overlap(lines, start, size, head_end))
            for raw in lines[:overlap]:  # the head already counted these
                t.feed(_decode(raw))
            for raw in lines[overlap:fresh]:
                entry = _decode(raw)
                t.feed(entry)
                if entry is not None:
                    append(entry.get("type"))
                elif raw.strip(_BLANK_BYTES):
                    append(None)
            for raw in lines[fresh:]:  # collected by an earlier pass
                t.feed(_decode(raw))
            seen = len(lines)
            if t.complete() or start == 0:
                break
            if max_tail_bytes is not None and window >= max_tail_bytes:
                break
            window *= 2
    d = _Drift()
    for kind in kinds:
        d.count(kind)
    return _summary(uuid, p, size, st.st_mtime, h, t, d)
