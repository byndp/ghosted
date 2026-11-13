"""The report's quote (contract 16.2): the one sentence of a session's ending a person sees.

``extract(summary, verdict, reason, limit=200) -> Optional[str]``

This is the one place transcript text reaches a person, so it is shown in the local terminal only
(the caller keeps it out of digest.md, items.json, state, logs, pushes and --json unless --quotes).
Every quote passes through ``core.sanitize.mask``. The sentence rule is ``core.classify``'s, so the
quote is exactly the sentence the classifier judged:

- ``usage_limit`` -> None (the report shows the reset time instead);
- ``ask_user`` / ``plan_approval`` -> the pending question box text (``summary.pending_question``),
  falling back to the final-text rule;
- a question reason (``trailing_q`` / ``offer_q``) -> the question sentence ending at the last real
  ``?``; a phrase reason -> the sentence holding the last phrase cue; anything else -> the
  question, the phrase, else the last sentence, in that order.

No I/O, no printing. ``summary`` is any object with ``final_text`` / ``pending_question``.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from core import classify as C
from core import sanitize

ELLIPSIS = "…"
DEFAULT_LIMIT = 200
# How far back a quote cut at the 200-character window may reach to take a whole token before
# masking (a key the window cut in half would no longer look like a key).
_TOKEN_REACH = 1024
# Longest collapsed input handed to sanitize.mask (it scans at most 4 KiB): ample for a 200-char
# quote, and the cut drops a partial word like any other.
_MAX_RAW = 2048

_WS_RE = re.compile(r"\s+")
# Trimmed at both ends: whitespace, emphasis, heading marks, list bullets, straight and curly quotes.
_EDGE = " *_#\"'“”‘’«»"
_LEAD_EDGE = _EDGE + "-+>•"
_QUESTION_REASONS = frozenset((C.TRAILING_Q, C.OFFER_Q, C.SAVE_OFFER))
_PHRASE_REASONS = frozenset((C.WANT_ME_TO, C.SAY_THE_WORD, C.YOUR_CALL))
_PENDING_REASONS = frozenset((C.ASK_USER, C.PLAN_APPROVAL))
_USABLE_RE = re.compile(r"[^\W_]")  # at least one letter or digit
_ENDS = ".!?:！？：。"
_CJK_ENDS = "。！？"
_CLOSING = "*_)]}\"'”’»"


def extract(summary: Any, verdict: Any, reason: Any, limit: int = DEFAULT_LIMIT,
            handoff_patterns: Any = None) -> Optional[str]:
    """The quote for one ending, masked and at most ``limit`` code points, or None. A hand-off
    quotes the sentence holding its last match (``handoff_patterns`` as the classifier used)."""
    if verdict == C.USAGE_LIMIT or reason in (C.LIMIT_META, C.LIMIT_TEXT, C.RATE_LIMIT):
        return None
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit < 1:
        return None
    if reason in _PENDING_REASONS:
        pending = getattr(summary, "pending_question", None)
        if isinstance(pending, str):
            found = _finish(pending, limit, keep_head=reason == C.PLAN_APPROVAL)
            if found is not None:
                return found
    text = getattr(summary, "final_text", None)
    if not isinstance(text, str):
        return None
    if reason == C.HANDOFF:
        span = C.handoff_sentence_span(text, handoff_patterns)
        found = _from_span(text, span, limit) if span is not None else None
        if found is not None:
            return found
    if reason in _QUESTION_REASONS:
        order: Tuple[Any, ...] = (C.question_sentence_span, C.phrase_sentence_span)
    elif reason in _PHRASE_REASONS:
        order = (C.phrase_sentence_span, C.question_sentence_span)
    else:
        order = (C.question_sentence_span, C.phrase_sentence_span, C.final_sentence_span)
    for find in order:
        span = find(text)
        if span is None:
            continue
        found = _from_span(text, span, limit)
        if found is not None:
            return found
    return None


def _from_span(text: str, span: Tuple[int, int], limit: int) -> Optional[str]:
    """The quote for ``text[start:end]``. A span that begins at the question window's start
    inside a sentence (no sentence end was found) is marked as cut: its first, partial word is
    dropped and ``…`` prefixed."""
    start, end = span
    cut = start > 0 and not _at_sentence_start(text, start)
    if cut:
        # Take the whole token the window cut, so masking sees all of it; it is dropped below.
        reach = start
        floor = max(0, start - _TOKEN_REACH)
        while reach > floor and not text[reach - 1].isspace():
            reach -= 1
        start = reach
    return _finish(text[start:end], limit, cut_head=cut)


def _at_sentence_start(text: str, i: int) -> bool:
    """True when ``text[i]`` starts a fresh sentence: only whitespace before it, a newline, or a
    sentence end (and its closing emphasis or quotes) then whitespace, right before it. The spans core.classify returns
    start either so or at the question window's start, which this tells apart."""
    j = i - 1
    if text[j] in _CJK_ENDS:
        return True
    if not text[j].isspace():
        return False
    while j >= 0 and text[j].isspace():
        if text[j] == "\n":
            return True
        j -= 1
    if j < 0:
        return True  # only whitespace before it: the text starts here
    closers = 0
    while j >= 0 and closers < 4 and text[j] in _CLOSING:
        j -= 1
        closers += 1
    return j >= 0 and text[j] in _ENDS


def _collapse(s: str) -> str:
    return _WS_RE.sub(" ", s).strip()


def _trim(s: str) -> str:
    return s.lstrip(_LEAD_EDGE).rstrip(_EDGE)


def _finish(raw: str, limit: int, keep_head: bool = False, cut_head: bool = False) -> Optional[str]:
    """Collapse, mask, trim and fit one raw span to ``limit`` code points.

    A very long input (a pasted plan) is cut to the end that will be kept *before* masking, since
    mask() keeps only its first 4 KiB and a tail quote must come from the real tail."""
    s = _collapse(raw)
    if len(s) > _MAX_RAW:
        if keep_head:
            s = s[:_MAX_RAW]
        else:
            s = s[-_MAX_RAW:]
            cut_head = True
    s = _collapse(sanitize.mask(s))
    if cut_head:
        space = s.find(" ")
        s = s[space + 1:] if space >= 0 else ""
    s = _trim(s)
    if not s or _USABLE_RE.search(s) is None:
        return None
    if cut_head:
        s = ELLIPSIS + s
    if len(s) <= limit:
        return s
    if limit == 1:
        return ELLIPSIS
    return _fit_head(s, limit) if keep_head else _fit_tail(s, limit)


def _fit_tail(s: str, limit: int) -> str:
    """The last ``limit - 1`` characters, cut forward to a word start, after ``…`` (the end of a
    question is the part that asks)."""
    if s.startswith(ELLIPSIS):
        s = s[1:]
    keep = s[len(s) - (limit - 1):]
    if not s[len(s) - limit].isspace():  # the cut fell inside a word: start at the next one
        space = keep.find(" ")
        if 0 <= space < len(keep) - 1:
            keep = keep[space + 1:]
    keep = keep.lstrip(_LEAD_EDGE) or keep
    return ELLIPSIS + keep


def _fit_head(s: str, limit: int) -> str:
    """The first ``limit - 1`` characters cut back to a word end, then ``…`` (a plan's first line
    is a title: its start says what it is)."""
    keep = s[:limit - 1]
    if not s[limit - 1].isspace():
        space = keep.rfind(" ")
        if space > 0:
            keep = keep[:space]
    keep = keep.rstrip(_EDGE) or keep
    return keep + ELLIPSIS
