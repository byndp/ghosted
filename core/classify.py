"""The ending classifier (contract section 7): what did a session end on?

``classify(summary, ignore_tail_patterns=()) -> Ending(verdict, reason)``

Rule order: empty, usage limit, other API error, pending question tool (rule 4), turn continued
(4b), the question test on the final text (5).

Verdicts: ``usage_limit`` and ``open_question`` are alerts; ``clean``, ``api_error`` (recorded,
never alerted) and ``empty`` are not. ``automated`` is the detector's verdict and never comes
from here. No LLM, no I/O, no printing: the classifier sees one SessionSummary and nothing else.

v2 (contract 16.1): a trailing question is split into a direct question to the user
(``trailing_q``, tier *waiting*) and a polite offer to do more work (``offer_q``, tier *soft*);
``tier()`` maps a verdict and reason to the report's tier. The sentence rule that picks the
question sentence lives here (``question_sentence_span``) so the quote module (``core.quote``)
shows exactly the sentence that was judged.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from functools import lru_cache
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

# Verdicts.
USAGE_LIMIT = "usage_limit"
OPEN_QUESTION = "open_question"
CLEAN = "clean"
API_ERROR = "api_error"
EMPTY = "empty"
AUTOMATED = "automated"  # set by the detector (entrypoint / first-prompt skip), never by classify()
FLAGGED_VERDICTS = frozenset((USAGE_LIMIT, OPEN_QUESTION))
VERDICTS = (USAGE_LIMIT, OPEN_QUESTION, CLEAN, API_ERROR, EMPTY, AUTOMATED)

# Reasons.
LIMIT_META = "limit_meta"            # API error entry: error == rate_limit, or carries quotaLimits
LIMIT_TEXT = "limit_text"            # final text is the usage-limit banner
TRAILING_Q = "trailing_q"            # a real "?" in the last 200 characters: a direct question
OFFER_Q = "offer_q"                  # ... whose sentence is a polite offer to do more work (v2)
SAVE_OFFER = "save_offer"            # an offer (any cue) to save the work: commit, push, merge, deploy (v2)
WANT_ME_TO = "want_me_to"
SAY_THE_WORD = "say_the_word"
YOUR_CALL = "your_call"
HANDOFF = "handoff"                  # the final message hands a decision to the user, anywhere in it (v3)
ASK_USER = "ask_user"                # last tool use is AskUserQuestion, unanswered
PLAN_APPROVAL = "plan_approval"      # last tool use is ExitPlanMode, unanswered
HUMAN_AFTER_FINAL = "human_after_final"
TOOL_USE_AFTER_FINAL = "tool_use_after_final"
IGNORED_TAIL = "ignored_tail"
NO_QUESTION = "no_question"
NO_FINAL_TEXT = "no_final_text"
GENERIC_API_ERROR = "api_error"      # api_error reason when the entry's error code is missing or odd
RATE_LIMIT = "rate_limit"            # the hook's StopFailure usage-limit reason (core.hook)

# v1 = everything before contract 16.1; v2 split trailing_q into trailing_q / offer_q. The detector
# re-processes stored open_question sessions once when its state was written by another version.
CLASSIFIER_VERSION = 3

# Tiers (contract 16.1). *Waiting* = the user is the blocker; *soft* = a text heuristic that
# probably needs nothing (counted, listed only with --soft).
WAITING = "waiting"
SOFT = "soft"
TIERS = (WAITING, SOFT)
# trailing_q is waiting under the default ``direct_questions = "waiting"``; tier() is the authority.
WAITING_REASONS = frozenset((LIMIT_META, LIMIT_TEXT, RATE_LIMIT, ASK_USER, PLAN_APPROVAL, TRAILING_Q))
SOFT_REASONS = frozenset((OFFER_Q, WANT_ME_TO, SAY_THE_WORD, YOUR_CALL, HANDOFF))
# save_offer is soft while the work is saved, waiting while it is not (tier's ``unsaved``).
CONDITIONAL_REASONS = frozenset((SAVE_OFFER,))

# An offer to save the work ("Want me to commit and push?"). Unanswered, it is polite noise only
# when the work got saved anyway; with the tree still dirty or commits unpushed it is the loose
# end itself (the 2026-10-01 pilot miss ended exactly this way). Matched on the unmasked
# sentence: "Want me to run `git commit`?" is a save offer too.
# Only the verbs' own inflections count: a bare ``\w*`` suffix also took "landscape", "landmark",
# "commitment", "shipment" and "pushover". "Check it in" must end the clause ("... check it in?",
# "check it in and push"), so "check it in the browser" / "check it in Safari" is not one.
_SAVE_VERB_RE = re.compile(
    r"\b(?:commit(?:s|ted|ting)?|push(?:es|ed|ing)?|merg(?:e|es|ed|ing)|(?:re-?)?deploy(?:s|ed|ing|ments?)?"
    r"|ship(?:s|ped|ping)?|(?:re-?)?publish(?:es|ed|ing)?|(?:re-?)?releas(?:e|es|ed|ing)|land(?:s|ed|ing)?)\b"
    r"|\bcheck(?:s|ed|ing)?\s+(?:it|this|these|them|that|those)\s+in"
    r"(?=\s*$|\s*[?.!,;:)\]]|\s+(?:and|to|now|too|for|first|then|yet|as|once|when|after)\b)"
    r"|\bpull\s+requests?\b|\bPRs?\b",
    re.IGNORECASE)

_EDGE_UNDERSCORE_RE = re.compile(r"(?<![^\W_])_+|_+(?![^\W_])")

# Rule 4: a question asked through Claude Code's UI that nobody answered (SessionSummary.pending_tool).
PENDING_TOOL_REASONS = {"AskUserQuestion": ASK_USER, "ExitPlanMode": PLAN_APPROVAL}

TAIL_CHARS = 200
# The limit wording is only trusted on a short final text. The banner is a one-line system
# message (every one on the pilot machine is <= 100 chars), while an assistant reply that merely
# discusses usage limits (this repo's own sessions; one 308-char reply on the pilot machine) must
# not become a usage_limit alert. Metadata (limit_meta) has no such cap.
LIMIT_TEXT_MAX_CHARS = 200

# Mirror of the config default ([detect] ignore_tail_patterns, contract section 2).
DEFAULT_IGNORE_TAIL_PATTERNS = (
    r"(?i)anything else\?\s*$",
    r"(?i)does (that|this) (help|make sense)\?\s*$",
)

_LIMIT_RE = re.compile(
    r"hit your (?:weekly|daily|session|5-hour|usage) limit"
    r"|usage limit reached"
    r"|limit reached .{0,3}resets"
    r"|you['\u2019]ve reached your .{0,30}limit",
    re.IGNORECASE,
)

_QUESTION_MARKS = ("?", "\uff1f")
_PHRASES = (
    (WANT_ME_TO, re.compile(r"\bwant\s+me\s+to\b", re.IGNORECASE)),
    (SAY_THE_WORD, re.compile(r"\bsay\s+the\s+word\b", re.IGNORECASE)),
    (YOUR_CALL, re.compile(r"\byour\s+call\b", re.IGNORECASE)),
)
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")

# Masking: characters inside code (fenced blocks, inline spans) and URLs are replaced by NUL so a
# "?" there is not a question and a phrase there is not a cue. Masks are computed on the whole
# final text, then the 200-character window is cut, so a span that opens before the window is
# still recognised.
_MASK = "\x00"
# A fence line: any indentation (fences inside nested list items sit deeper than CommonMark's 3
# spaces), then ``` or ~~~. A backtick fence's info string may not contain a backtick
# (CommonMark), so a one-line "```npm test```" is an inline span, not an unclosed fence that
# would hide the rest of the text. The backtick run is captured inside a lookahead (atomic in
# Python's re; 3.9 has no possessive quantifiers), so a failed info-string check cannot retry
# with a shorter run and rescan the line: quadratic on a line of many backticks otherwise.
_FENCE_OPEN_RE = re.compile(r"[ \t]*(?:(?=(`{3,}))\1(?!.*`)|(~{3,}))")
_PARAGRAPH_RE = re.compile(r"(?:[^\n]|\n(?![ \t]*(?:\n|\Z)))+")
_BACKTICKS_RE = re.compile(r"`+")
# Quantifiers are bounded (scheme <= 32 chars, <= 16 host labels of <= 63 chars, DNS limits) so
# the scan stays linear: with unbounded ``+``/``*`` every word start in a long dotted run without
# spaces rescanned the whole run (10K chars took 0.5 s, 40K about 8 s).
_URL_RE = re.compile(
    r"(?:\b[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://"
    r"|\bwww\."
    r"|\b(?:localhost|(?:[a-zA-Z0-9-]{1,63}\.){1,16}[a-zA-Z]{2,63})(?::\d{1,5})?/)"
    r"[^\s<>\"'`\x00]*"
)
_URL_TRAILING = ".,;:!?*_'\"\uff1f"
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_EMPHASIS = "*_"


# ---- hand-offs (contract 16.10) -----------------------------------------------------------------
# A summary that hands a decision to the user without asking it. Read over the whole final message
# (its last HANDOFF_CHARS), code and URLs masked: B1 found every real cue > 600 chars from the end.
HANDOFF_CHARS = 8000
DEFAULT_HANDOFF_PATTERNS = (
    # Generic, benchmark-tuned (16.10): only the labelled hand-off line ("Waiting on you: pick A or
    # B", or a "Waiting on you" heading). Looser second-person cues ("your call", "you pick", "up to
    # you", "needs your input") showed up in 22 of 107 plain summary endings, so they are not shipped.
    r"(?im)^[\W_]*(?:still )?waiting on you\b(?:[^\n]{0,6}[:—–-]|[ \t*_]*$)",
)
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,39}$")
# "nothing is waiting on you", "doesn't need your sign-off": a negation at most three words before
# the cue, in the same clause (no , ; : . ! ? between), cancels it.
_HANDOFF_NEGATION_RE = re.compile(
    r"(?i)\b(?:nothing|not|no|nobody|none|never|no longer|isn't|aren't|don't|doesn't|won't|"
    r"wouldn't|shouldn't|needn't|nothing's)\b(?:[^\w,;:.!?\n]+\w+){0,3}[^\w,;:.!?\n]*$")


def valid_handoff_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name.strip())) and name == name.strip()


def handoff_patterns_for(patterns: Iterable[str], names: Iterable[str] = ()) -> List[str]:
    """``patterns`` plus the opt-in name patterns ("Alex has not ruled", "waiting on Alex",
    "Alex's call", "Alex decides", a Next line naming Alex)."""
    out = list(patterns)
    for name in names:
        if not valid_handoff_name(name):
            continue
        n = re.escape(name)
        out.extend((
            r"(?i)\b%s (?:has ?n[o']t|has not|hasn't|still hasn't)(?: yet)? (?:ruled|decided|picked|chosen|approved)\b" % n,
            r"(?i)\bwait(?:s|ing)? (?:on|for) %s\b" % n,
            r"(?i)\b%s's (?:call|decision|pick|ruling)\b" % n,
            r"(?i)\b%s (?:rules|decides|picks|chooses|approves)\b" % n,
            r"(?im)^[\W_]*next\b[^\n]{0,80}\b%s\b" % n,
        ))
    return out


def _handoff_match(text: Any, patterns: Optional[Iterable[str]]) -> Optional[Tuple[str, str, int, int]]:
    """(t, masked, start, end) of the last hand-off match in the scanned part, or None."""
    if not isinstance(text, str):
        return None
    t = normalize_tail(text)
    if not t:
        return None
    pats = DEFAULT_HANDOFF_PATTERNS if patterns is None else tuple(patterns)
    if not pats:
        return None
    cut = max(0, len(t) - HANDOFF_CHARS)
    # Mask the whole message, then search only its tail: cropping first could
    # cut a fence's opening line off and unmask the code inside it.
    masked = masked_text(t)
    best: Optional[Tuple[int, int]] = None
    for pattern in pats:
        rx = _compile(pattern)
        if rx is None:
            continue
        for m in rx.finditer(masked, cut):
            if _HANDOFF_NEGATION_RE.search(masked[max(cut, m.start() - 48):m.start()]):
                continue
            if best is None or m.start() > best[0]:
                best = (m.start(), m.end())
    if best is None:
        return None
    return t, masked, best[0], best[1]


def handoff_cue(text: Any, patterns: Optional[Iterable[str]] = None) -> bool:
    """True when the final message hands a decision to the user (``patterns`` None = defaults)."""
    return _handoff_match(text, patterns) is not None


def handoff_sentence_span(text: Any, patterns: Optional[Iterable[str]] = None) -> Optional[Tuple[int, int]]:
    """``(start, end)`` of the sentence holding the last hand-off match (the quote's), or None."""
    found = _handoff_match(text, patterns)
    if found is None:
        return None
    t, masked, a, b = found
    lo = max(0, len(t) - HANDOFF_CHARS)
    return _sentence_start(masked, lo, a), _sentence_end(masked, b)


class Ending(NamedTuple):
    verdict: str
    reason: str

    @property
    def flagged(self) -> bool:
        return self.verdict in FLAGGED_VERDICTS


# --------------------------------------------------------------------------- helpers

def normalize_tail(text: str) -> str:
    """Strip trailing whitespace and trailing markdown emphasis (``*``, ``_``), in any mix.

    One backwards pass (repeated ``rstrip`` calls were quadratic on a long " * * *" tail).
    """
    end = len(text)
    while end > 0 and (text[end - 1].isspace() or text[end - 1] in _EMPHASIS):
        end -= 1
    return text[:end]


def is_limit_text(text: Optional[str]) -> bool:
    """True when ``text`` reads as the usage-limit banner (short text with the limit wording).

    The wording must sit outside code and URLs: a short reply such as "Added the `usage limit
    reached` pattern." discusses the banner, it is not one. Real banners are plain text.
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or len(s) > LIMIT_TEXT_MAX_CHARS:
        return False
    return _LIMIT_RE.search(masked_text(s)) is not None


def _fenced_spans(t: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    lines = t.split("\n")
    offsets: List[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group(1) or m.group(2)
        closer = re.compile(r"[ \t]*%s{%d,}[ \t\r]*$" % (re.escape(fence[0]), len(fence)))
        end_line = len(lines) - 1  # an unclosed fence runs to the end of the text
        for j in range(i + 1, len(lines)):
            if closer.match(lines[j]):
                end_line = j
                break
        spans.append((offsets[i], offsets[end_line] + len(lines[end_line])))
        i = end_line + 1
    return spans


def _inline_code_spans(t: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Backtick code spans in t[start:end], paired within a paragraph (equal-length runs)."""
    spans: List[Tuple[int, int]] = []
    for para in _PARAGRAPH_RE.finditer(t, start, end):
        runs = [(m.start(), m.end()) for m in _BACKTICKS_RE.finditer(t, para.start(), para.end())]
        by_length: Dict[int, List[int]] = {}
        for idx, (s, e) in enumerate(runs):
            by_length.setdefault(e - s, []).append(idx)
        k = 0
        while k < len(runs):
            same = by_length[runs[k][1] - runs[k][0]]
            nxt = bisect_right(same, k)  # the next run of the same length closes the span
            if nxt >= len(same):
                k += 1
                continue
            closer = same[nxt]
            spans.append((runs[k][0], runs[closer][1]))
            k = closer + 1
    return spans


def _trim_url(url: str) -> str:
    """Drop sentence punctuation that trails a URL ("... at https://x.dev/a?" asks a question).

    A trailing closer goes when the URL holds more closers than openers of that kind ("(see
    https://x.dev/a)"). Counts are taken once and updated as closers are dropped (linear).
    """
    opens = {o: url.count(o) for o in _CLOSERS.values()}
    closes = {c: url.count(c) for c in _CLOSERS}
    end = len(url)
    while end > 0:
        c = url[end - 1]
        if c in _URL_TRAILING:
            end -= 1
            continue
        opener = _CLOSERS.get(c)
        if opener is not None and opens[opener] < closes[c]:
            closes[c] -= 1
            end -= 1
            continue
        break
    return url[:end]


def masked_text(text: str) -> str:
    """``text`` with code (fenced and inline) and URLs replaced by NUL characters, same length."""
    chars = list(text)
    code: List[Tuple[int, int]] = []
    cursor = 0
    for s, e in _fenced_spans(text):
        code.extend(_inline_code_spans(text, cursor, s))
        code.append((s, e))
        cursor = e
    code.extend(_inline_code_spans(text, cursor, len(text)))
    for s, e in code:
        chars[s:e] = [_MASK] * (e - s)
    without_code = "".join(chars)
    for m in _URL_RE.finditer(without_code):
        url = _trim_url(m.group(0))
        if url:
            chars[m.start(): m.start() + len(url)] = [_MASK] * len(url)
    return "".join(chars)


def question_cue(text: Optional[str]) -> Optional[str]:
    """The open-question reason for a final text, or None.

    Looks at the last 200 characters (after stripping trailing whitespace and emphasis). A ``?``
    outside code and URLs makes it a question: ``offer_q`` when the question sentence (the one
    holding the last real ``?``, see ``question_sentence_span``) is a polite offer
    (``is_offer``), else ``trailing_q``. Without a ``?``: ``want me to`` / ``say the word`` /
    ``your call`` (case-insensitive, outside code and URLs), the reason of the last match, or
    ``save_offer`` when any phrase sentence is about saving the work (``_phrase_cue``).
    """
    if not isinstance(text, str):
        return None
    t = normalize_tail(text)
    if not t:
        return None
    masked = masked_text(t)
    lo = max(0, len(t) - TAIL_CHARS)
    span = _question_span(t, masked, lo)
    if span is not None:
        sentence = t[span[0]:span[1]]
        if not is_offer(sentence):
            return TRAILING_Q
        return SAVE_OFFER if is_save_offer(sentence) else OFFER_Q
    cue = _phrase_cue(t, masked, lo)
    return cue[0] if cue is not None else None


def _phrase_cue(t: str, masked: str, lo: int) -> Optional[Tuple[str, int, int]]:
    """``(reason, start, end)`` for the phrase cues in the window: the reason and the sentence
    come from the same match. The last phrase sentence that is a save offer gives ``save_offer``
    (so "Say the word and I'll commit and push. Your call on the naming." is a save offer, in
    either order); else the last phrase match gives both the reason and the sentence."""
    window = masked[lo:]
    matches: List[Tuple[int, int, str]] = []
    for reason, rx in _PHRASES:
        for m in rx.finditer(window):
            matches.append((lo + m.start(), lo + m.end(), reason))
    if not matches:
        return None
    matches.sort()
    spans = [(reason, _sentence_start(masked, lo, a), _sentence_end(masked, b)) for a, b, reason in matches]
    for reason, start, end in reversed(spans):
        if is_save_offer(t[start:end]):
            return SAVE_OFFER, start, end
    return spans[-1]


def is_save_offer(sentence: Any) -> bool:
    """True when an offer sentence is about saving the work (commit, push, merge, PR / pull
    request, deploy, ship, publish, release, land, "check it in"; inflections such as "pushed",
    "merging" count, unrelated words that merely start alike, such as "landscape" or
    "commitment", do not). Matched on the unmasked sentence, so ``git push`` in code counts.
    Call it only on a sentence already judged an offer."""
    if not isinstance(sentence, str):
        return False
    # Emphasis underscores are word characters, so "_Ship it?_" hid the verb from ``\b``; they
    # become spaces (an underscore inside a word, as in ``git_push``, stays). NFKC as in is_offer.
    s = unicodedata.normalize("NFKC", sentence[:2 * TAIL_CHARS])
    s = _EDGE_UNDERSCORE_RE.sub(" ", s)
    return _SAVE_VERB_RE.search(s) is not None


# --------------------------------------------------------------------------- sentences (16.1)

# A sentence ends at ".", "!", "?" or ":" followed by whitespace (closing emphasis, quotes or
# brackets may sit in between: "**Next:** do it"), at a newline, or at a CJK full stop / mark,
# which needs no space. All of it outside code and URLs: the scan runs on masked_text, where
# those are NUL.
_SENTENCE_PUNCT = ".!?:！？："
_CJK_ENDS = "。！？"
_CLOSING = "*_)]}\"'”’»"
_CLOSING_MAX = 4
# A "." after these is an abbreviation, not a sentence end ("use a lib, e.g. requests?").
_ABBREVIATIONS = ("e.g", "i.e", "vs", "cf")


def _is_abbreviation(masked: str, dot: int) -> bool:
    before = masked[max(0, dot - 4):dot].lower()
    for abbr in _ABBREVIATIONS:
        if before.endswith(abbr):
            k = dot - len(abbr)
            if k == 0 or not masked[k - 1].isalnum():
                return True
    return False


def _ends_sentence_at(masked: str, i: int) -> bool:
    """True when the punctuation at ``i`` ends a sentence (whitespace follows its closers)."""
    c = masked[i]
    if c in _CJK_ENDS:
        return True
    if c not in _SENTENCE_PUNCT:
        return False
    j = i + 1
    n = len(masked)
    while j < n and j - i <= _CLOSING_MAX and masked[j] in _CLOSING:
        j += 1
    if j < n and not masked[j].isspace():
        return False
    return not (c == "." and _is_abbreviation(masked, i))


def _sentence_start(masked: str, lo: int, pos: int) -> int:
    """Start of the sentence holding ``pos``: just after the previous sentence end in
    ``masked[lo:pos]`` (leading whitespace skipped), else ``lo``. Never before ``lo``."""
    start = lo
    i = pos - 1
    while i >= lo:
        c = masked[i]
        if c == "\n" or (c in _SENTENCE_PUNCT or c in _CJK_ENDS) and _ends_sentence_at(masked, i):
            start = i + 1
            if c != "\n":  # the previous sentence's closers ("**Next:** do it") stay with it
                while start < pos and start - i <= _CLOSING_MAX and masked[start] in _CLOSING:
                    start += 1
            break
        i -= 1
    while start < pos and masked[start].isspace():
        start += 1
    return start


def _sentence_end(masked: str, pos: int) -> int:
    """End (exclusive) of the sentence holding ``pos``: just after its closing punctuation, at a
    newline, else the end of the text."""
    n = len(masked)
    i = pos
    while i < n:
        c = masked[i]
        if c == "\n":
            return i
        if (c in _SENTENCE_PUNCT or c in _CJK_ENDS) and _ends_sentence_at(masked, i):
            return i + 1
        i += 1
    return n


def _question_span(t: str, masked: str, lo: int) -> Optional[Tuple[int, int]]:
    q = max(masked.rfind(mark, lo) for mark in _QUESTION_MARKS)
    if q < 0:
        return None
    return _sentence_start(masked, lo, q), q + 1


def window_start(text: Any) -> int:
    """Offset in ``text`` where the 200-character question window begins (0 for short text)."""
    if not isinstance(text, str):
        return 0
    return max(0, len(normalize_tail(text)) - TAIL_CHARS)


def question_sentence_span(text: Any) -> Optional[Tuple[int, int]]:
    """``(start, end)`` offsets into ``text`` of the question sentence, or None.

    ``end`` is just after the last real ``?`` (outside code and URLs) within the 200-character
    window of ``normalize_tail(text)``; ``start`` is just after the previous sentence end (outside
    code and URLs), else the window start, never before it. ``question_cue`` judges exactly this
    sentence, and ``core.quote`` shows it.
    """
    if not isinstance(text, str):
        return None
    t = normalize_tail(text)
    if not t:
        return None
    return _question_span(t, masked_text(t), max(0, len(t) - TAIL_CHARS))


def phrase_sentence_span(text: Any) -> Optional[Tuple[int, int]]:
    """``(start, end)`` of the sentence the phrase-cue reason came from (``want me to``, ``say
    the word``, ``your call`` in the 200-character window): the last phrase sentence that is a
    save offer, else the one holding the last phrase match; None without a cue. ``end`` = the
    next sentence end or the end of the normalised text."""
    if not isinstance(text, str):
        return None
    t = normalize_tail(text)
    if not t:
        return None
    cue = _phrase_cue(t, masked_text(t), max(0, len(t) - TAIL_CHARS))
    return (cue[1], cue[2]) if cue is not None else None


def final_sentence_span(text: Any) -> Optional[Tuple[int, int]]:
    """``(start, end)`` of the last sentence of the normalised text (inside the window), or None.
    The quote's fallback when an ending has neither a question nor a phrase cue (an unanswered
    question box whose text was a statement)."""
    if not isinstance(text, str):
        return None
    t = normalize_tail(text)
    if not t:
        return None
    masked = masked_text(t)
    lo = max(0, len(t) - TAIL_CHARS)
    end = len(t)
    # The text's own closing punctuation belongs to the last sentence, not before it.
    return _sentence_start(masked, lo, end - 1 if end - 1 >= lo else end), end


# --------------------------------------------------------------------------- offers (16.1)

_QUOTE_MAP = {ord("‘"): "'", ord("’"): "'", ord("‛"): "'", ord("′"): "'",
              ord("“"): '"', ord("”"): '"', ord("‟"): '"'}
# Leading list markers, quotes, emphasis, brackets, dashes, blockquote and heading marks.
_LEAD_CHARS = "-–—•·*_+>#\"'«»([{ \t"
_LEAD_NUMBER_RE = re.compile(r"\d{1,3}[.)]\s+")
_FILLER_RE = re.compile(r"(?:so|ok|okay|also|and|or|then|now|alright|great|cool|next)\b[,:]?\s*")
_FILLER_ROUNDS = 16  # each round removes a word; a sentence never needs more (bounded on junk)
_INNER_EMPHASIS_RE = re.compile(r"\*+|(?<![a-z0-9])_+|_+(?![a-z0-9])")

_OFFER_CONTAINS_RE = re.compile(
    r"\bwant\s+me\s+to\b"
    r"|\bwould\s+you\s+like\s+me\s+to\b"
    r"|\bdo\s+you\s+want\s+me\s+to\b"
    r"|\blike\s+me\s+to\b"
    r"|\bif\s+you(?:'d)?\s+(?:want|like)\b"
    r"|\bhappy\s+to\b"
    r"|\bi\s+can\s+also\b"
    r"|\bor\s+should\s+i\s+just\b"
)
_OFFER_STARTS_RE = re.compile(
    r"(?:should\s+i|shall\s+i|can\s+i|could\s+i|may\s+i"
    r"|ok(?:ay)?\s+(?:if\s+i|to)"
    r"|want\s+(?:to|the|a|an|it|that|this|those|these)"
    r"|ready\s+(?:for\s+me\s+)?to"
    r"|go\s+ahead|proceed|sounds?\s+good|looks?\s+good|good\s+to\s+go"
    r"|does\s+(?:that|this)\s+(?:sound|look|work)"
    r"|anything\s+else|shall\s+we|should\s+we|need\s+anything"
    r"|commit|push|merge|ship\s+it)\b"
)
# A real alternative: " or " / ", or " not followed by a word that makes it a filler ("or not",
# "or so", "a day or two", "or anything else"). Checked outside code spans.
# One whitespace (or a comma and its spaces) before "or", never "\s\s*": on a long run of spaces
# that rescans the run from every position (quadratic).
_CHOICE_RE = re.compile(
    r"(?:,\s*|\s)or\s+(?!(?:not|no|so|anything|something|else|two|more|less)\b)[^\s?]"
)


def _strip_leading(s: str) -> str:
    i = 0
    n = len(s)
    while True:
        while i < n and s[i] in _LEAD_CHARS:
            i += 1
        m = _LEAD_NUMBER_RE.match(s, i)
        if m is None:
            return s[i:]
        i = m.end()


def is_offer(sentence: Any) -> bool:
    """True when a question sentence is a polite offer to do more work (contract 16.1).

    After lowercasing (NFKC, curly quotes straightened, emphasis marks dropped) and dropping
    leading list markers, quotes, emphasis, brackets and filler words ("so", "ok,", "also", ...),
    the sentence is an offer when it *contains* an offer phrase ("want me to", "happy to", ...)
    or *starts with* one ("should I", "commit", "sounds good", ...). A sentence that only starts
    with one and holds a real alternative ("Should I keep A or switch to B?") is a choice the
    user has to make, so it is not an offer: "never hide a decision" beats "probably fine".
    """
    if not isinstance(sentence, str):
        return False
    s = unicodedata.normalize("NFKC", sentence).translate(_QUOTE_MAP).lower()
    s = _INNER_EMPHASIS_RE.sub("", s)
    if not s.strip():
        return False
    if _OFFER_CONTAINS_RE.search(masked_text(s)):  # a phrase inside code or a URL is not one
        return True
    rest = _strip_leading(s)
    starts = _OFFER_STARTS_RE.match(rest) is not None
    for _ in range(_FILLER_ROUNDS):
        if starts:
            break
        m = _FILLER_RE.match(rest)
        if m is None or not m.group(0):
            break
        rest = _strip_leading(rest[m.end():])
        starts = _OFFER_STARTS_RE.match(rest) is not None
    if not starts:
        return False
    return _CHOICE_RE.search(masked_text(rest)) is None


# --------------------------------------------------------------------------- tiers (16.1)

def tier(verdict: Any, reason: Any, direct_questions: str = WAITING,
         unsaved: Optional[bool] = None) -> Optional[str]:
    """The report tier for an ending: ``waiting``, ``soft`` or None (not flagged).

    ``usage_limit`` -> waiting; ``open_question`` with ``ask_user`` / ``plan_approval`` -> waiting;
    ``trailing_q`` -> ``direct_questions`` (config ``[detect] direct_questions``; an unknown value
    counts as waiting); the text heuristics ``offer_q`` / ``want_me_to`` / ``say_the_word`` /
    ``your_call`` -> soft; ``save_offer`` -> waiting when ``unsaved`` is True (the launch
    directory's repo has uncommitted or unpushed work now), else soft; any other ``open_question``
    reason -> waiting (never hide an unknown).
    """
    if verdict == USAGE_LIMIT:
        return WAITING
    if verdict != OPEN_QUESTION:
        return None
    if reason == TRAILING_Q:
        return SOFT if direct_questions == SOFT else WAITING
    if reason in SOFT_REASONS:
        return SOFT
    if reason == SAVE_OFFER:
        return WAITING if unsaved is True else SOFT
    return WAITING


@lru_cache(maxsize=128)
def _compile(pattern: str) -> Optional["re.Pattern[str]"]:
    try:
        return re.compile(pattern)
    except (re.error, TypeError, ValueError, OverflowError):
        return None  # a bad config pattern is ignored, never fatal (the hook must not crash)


def _patterns(patterns: Any) -> Iterable[Any]:
    """Config patterns as an iterable: None, one pattern, or a collection. Anything else is none."""
    if patterns is None:
        return ()
    if isinstance(patterns, str) or hasattr(patterns, "search"):
        return (patterns,)
    try:
        return tuple(patterns)
    except TypeError:
        return ()  # not iterable: ignored, never fatal (the hook must not crash)


def _regex(p: Any) -> Optional["re.Pattern[str]"]:
    if isinstance(p, str):
        return _compile(p)
    if hasattr(p, "search") and isinstance(getattr(p, "pattern", None), str):
        return p  # a compiled str pattern (a bytes pattern cannot search text)
    return None


def matches_ignore_tail(text: Optional[str], ignore_tail_patterns: Any = ()) -> bool:
    """True when the normalised 200-character tail matches one of the ignore patterns."""
    if not isinstance(text, str):
        return False
    tail = normalize_tail(text)[-TAIL_CHARS:]
    for p in _patterns(ignore_tail_patterns):
        rx = _regex(p)
        if rx is not None and rx.search(tail):
            return True
    return False


def _api_reason(error: Optional[str]) -> str:
    if isinstance(error, str) and _ERROR_CODE_RE.fullmatch(error):
        return error
    return GENERIC_API_ERROR


# --------------------------------------------------------------------------- classify

def classify(summary: Any, ignore_tail_patterns: Union[Iterable[Any], str, None] = (),
             handoff_patterns: Optional[Iterable[str]] = None) -> Ending:
    """Classify how a session ended (contract section 7, rules applied in order).

    1. No final text -> ``empty``.
    2. Usage limit -> ``usage_limit`` (``limit_meta`` from the API-error metadata, else
       ``limit_text`` from the banner wording); flagged even if a human turn follows.
    3. Any other API error -> ``api_error`` (reason = the error code).
    4. The last tool use is an unanswered AskUserQuestion / ExitPlanMode (``pending_tool``) ->
       ``open_question`` (``ask_user`` / ``plan_approval``).
    4b. A human turn or an assistant tool_use after the final text -> ``clean``.
    5. Question test on the tail -> ``open_question`` (unless an ignore pattern matches the tail:
       ``clean``/``ignored_tail``).
    6. A hand-off anywhere in the final message (``handoff_patterns``, None = the defaults, empty =
       off) and no `ghosted add` in the last turn -> ``open_question``/``handoff`` (soft, contract 16.10);
       else ``clean``/``no_question``.
    """
    text = getattr(summary, "final_text", None)
    if not isinstance(text, str) or not text.strip():
        return Ending(EMPTY, NO_FINAL_TEXT)

    is_api_error = bool(getattr(summary, "final_is_api_error", False))
    error = getattr(summary, "final_error", None)
    if error == "rate_limit" or (is_api_error and getattr(summary, "final_has_quota", False)):
        return Ending(USAGE_LIMIT, LIMIT_META)
    if is_limit_text(text):
        return Ending(USAGE_LIMIT, LIMIT_TEXT)
    if is_api_error:
        return Ending(API_ERROR, _api_reason(error))

    pending = getattr(summary, "pending_tool", None)
    if isinstance(pending, str) and pending in PENDING_TOOL_REASONS:
        return Ending(OPEN_QUESTION, PENDING_TOOL_REASONS[pending])

    if getattr(summary, "human_after_final", False):
        return Ending(CLEAN, HUMAN_AFTER_FINAL)
    if getattr(summary, "tool_use_after_final", False):
        return Ending(CLEAN, TOOL_USE_AFTER_FINAL)

    cue = question_cue(text)
    if cue is None:
        if not getattr(summary, "le_add_last_turn", False) and handoff_cue(text, handoff_patterns):
            return Ending(OPEN_QUESTION, HANDOFF)
        return Ending(CLEAN, NO_QUESTION)
    if matches_ignore_tail(text, ignore_tail_patterns):
        return Ending(CLEAN, IGNORED_TAIL)
    return Ending(OPEN_QUESTION, cue)
