"""The public-title sanitizer. It refuses; it never edits.

`problems(title)` returns plain-English reasons (an empty list means the title may be uploaded);
`check(title)` raises `Refused(problems)` and otherwise returns the title unchanged. Messages never
quote the offending text, since it may be a secret.

Classes (contract section 9): blank or longer than 100 characters, email addresses, money, secrets
(key prefixes, AWS ids, Slack/Google/GitHub tokens, JWTs, long hex, long mixed base64-ish runs),
street-address shapes (also lowercase ones with an unambiguous suffix: "123 main street") and phone
numbers. Project names get the same checks with a 60-character limit (`project_problems`, the
Worker's limit). Additions beyond section 9, all of them refusals the
Worker's `sanitize.js` may lag behind (the Mac is the first gate, so stricter here is safe):
control characters, line breaks (including U+2028/U+2029) and invisible format characters
(zero-width, bidi overrides), which could also hide an address or amount from the checks below;
a number followed by `EUR` or `GBP` signs ("480 €"); `USD` before a number; `dollar` singular;
directional prefixes and ordinal street names in addresses ("123 N Main St", "350 5th Ave").

The pattern checks also run on the NFKC form of the title, so fullwidth or other compatibility
characters ("＄480", "bob＠example.com") are judged like their plain forms. Only the first
SCAN_LIMIT characters are scanned: a longer title is refused for its length anyway, and the cap
keeps a pasted log from making the regexes slow.

`mask(text)` (contract 16.2) is the other direction, for the one place transcript text reaches a
person (the report's quote, local terminal only): it never refuses, it replaces each sensitive
span with a placeholder (`[key]`, `[email]`, `[address]`, `[phone]`, `[amount]`). It reuses the
refusal patterns, widened to cover the whole span (a refusal only needs to see a key's prefix; a
mask must hide the rest of it).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Optional, Tuple

__all__ = ["MAX_TITLE", "MAX_PROJECT", "Refused", "problems", "project_problems", "check", "mask"]

MAX_TITLE = 100
MAX_PROJECT = 60  # the Worker's limit too (sanitize.js MAX_PROJECT): one limit on both sides
SCAN_LIMIT = 4 * MAX_TITLE

MSG_BLANK = "is blank"
MSG_CONTROL = "contains a line break or control character"
MSG_EMAIL = "contains an email address"
MSG_MONEY = "contains a money amount"
MSG_SECRET = "contains something that looks like a key or token"
MSG_ADDRESS = "contains something that looks like a street address"
MSG_PHONE = "contains a phone number"
MSG_NOT_TEXT = "is not text"


class Refused(ValueError):
    """A title the sanitizer will not let through. `problems` holds the plain-English reasons."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) if self.problems else "refused")


# Cc = C0 and C1 controls, DEL; Cf = format characters (zero-width space/joiner, bidi controls,
# soft hyphen, BOM); Zl / Zp = the Unicode line and paragraph separators.
_CONTROL_CATEGORIES = frozenset(("Cc", "Cf", "Zl", "Zp"))

_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}")

_CURRENCY_SIGN = re.compile(r"[$€£]")  # $ EUR GBP
_MONEY_SIGN_BEFORE = re.compile(r"[$€£]\s?\d")  # $480, € 20, $12k
_MONEY_SIGN_AFTER = re.compile(r"\d\s?[€£]")  # 480€, 20 £ (not $: "2 $HOME")
_MONEY_WORD = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?\s?(?:usd|(?:us\s?)?dollars?)\b", re.IGNORECASE)
_MONEY_CODE_BEFORE = re.compile(r"\busd\s?\d", re.IGNORECASE)
_MONEY_K = re.compile(r"\d(?:[.,]\d+)?\s?k\b", re.IGNORECASE)  # only with a currency sign

_SECRETS = [
    re.compile(r"(?<![A-Za-z0-9])sk-"),  # sk-..., sk-ant-..., sk-proj-... (not "task-", "risk-")
    re.compile(r"shpat_|shpss_"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_"),
    re.compile(r"github_pat_"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abposr]-"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"[0-9A-Fa-f]{32,}"),
]
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"[0-9]")

_STREET_SUFFIX = r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Dr|Drive|Ln|Lane|Way|Ct|Court|Pl|Place|Pkwy|Hwy|Ter)"
_ADDRESS = re.compile(
    r"\d{1,6}\s+(?:[NSEW]\.?\s+)?"
    r"(?:(?:[A-Z][a-z]+|\d+(?:st|nd|rd|th))\.?\s+){1,4}"
    + _STREET_SUFFIX + r"\b"
)

# Lowercase addresses ("ship to 123 main street"). Only suffixes that are not ordinary words:
# "way", "road", "drive", "lane", "place", "court" and "ter" would refuse plain sentences
# ("send 3 samples on the way").
_STREET_SUFFIX_LOWER = r"(?:street|st|avenue|ave|boulevard|blvd|rd|ln|parkway|pkwy|highway|hwy)"
_ADDRESS_LOWER = re.compile(r"\b\d{1,6}\s+(?:[a-z]+\s+){1,3}" + _STREET_SUFFIX_LOWER + r"\b")

_PHONE = re.compile(r"(?<!\d)(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")


def _has_control(text: str) -> bool:
    return any(unicodedata.category(ch) in _CONTROL_CATEGORIES for ch in text)


def _has_money(title: str) -> bool:
    if _MONEY_SIGN_BEFORE.search(title) or _MONEY_SIGN_AFTER.search(title):
        return True
    if _MONEY_WORD.search(title) or _MONEY_CODE_BEFORE.search(title):
        return True
    return bool(_CURRENCY_SIGN.search(title) and _MONEY_K.search(title))


def _has_secret(title: str) -> bool:
    if any(p.search(title) for p in _SECRETS):
        return True
    for m in _TOKEN_RUN.finditer(title):
        run = m.group(0)
        if _UPPER.search(run) and _LOWER.search(run) and _DIGIT.search(run):
            return True
    return False


_CHECKS = (
    (MSG_EMAIL, lambda t: bool(_EMAIL.search(t))),
    (MSG_MONEY, _has_money),
    (MSG_SECRET, _has_secret),
    (MSG_ADDRESS, lambda t: bool(_ADDRESS.search(t) or _ADDRESS_LOWER.search(t))),
    (MSG_PHONE, lambda t: bool(_PHONE.search(t))),
)


def problems(title: Any) -> List[str]:
    """Plain-English reasons this title can't be uploaded; [] when it is fine."""
    return _problems(title, MAX_TITLE)


def project_problems(project: Any) -> List[str]:
    """The same checks for a project name, with its own limit of MAX_PROJECT (60) characters,
    as the Worker applies (a longer one would be refused there and never reach the phone)."""
    return _problems(project, MAX_PROJECT)


def _problems(title: Any, limit: int) -> List[str]:
    if title is None:
        return [MSG_BLANK]
    if not isinstance(title, str):
        return [MSG_NOT_TEXT]
    if not title.strip():
        return [MSG_BLANK]
    found: List[str] = []
    if len(title) > limit:
        found.append("is %d characters long (the limit is %d)" % (len(title), limit))
    text = title[:SCAN_LIMIT]
    if _has_control(text):
        found.append(MSG_CONTROL)
    forms = [text]
    folded = unicodedata.normalize("NFKC", text)[: 4 * SCAN_LIMIT]
    if folded != text:
        forms.append(folded)
    for message, test in _CHECKS:
        if any(test(form) for form in forms):
            found.append(message)
    return found


def check(title: str) -> str:
    """Return `title` unchanged, or raise `Refused` with every problem found."""
    found = problems(title)
    if found:
        raise Refused(found)
    return title


# ---- masking (contract 16.2) --------------------------------------------------------------------

MASK_SCAN_LIMIT = 4096  # code points of the NFKC form; the caller passes <= 1 KiB

KEY = "[key]"
EMAIL = "[email]"
ADDRESS = "[address]"
PHONE = "[phone]"
AMOUNT = "[amount]"

# The rest of a token after a key prefix: a refusal stops at "sk-", a mask covers the whole key.
_TOKEN_TAIL = r"[A-Za-z0-9_\-]*"
_MASK_SECRETS = [
    re.compile(r"(?<![A-Za-z0-9])sk-" + _TOKEN_TAIL),
    re.compile(r"(?:shpat_|shpss_)" + _TOKEN_TAIL),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_" + _TOKEN_TAIL),
    re.compile(r"github_pat_" + _TOKEN_TAIL),
    re.compile(r"AKIA[0-9A-Z]{16}[0-9A-Za-z]*"),
    re.compile(r"xox[abposr]-" + _TOKEN_TAIL),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"[0-9A-Fa-f]{32,}"),
]
_NUMBER = r"\d(?:[\d,]*\d)?(?:\.\d+)?"
_MASK_MONEY = [
    re.compile(r"[$€£]\s?" + _NUMBER + r"(?:\s?[kKmM]\b)?"),          # $480, $12k, € 20
    re.compile(_NUMBER + r"\s?[€£]"),                                  # 480€ (not $: "2 $HOME")
    re.compile(_NUMBER + r"\s?(?:usd|(?:us\s?)?dollars?)\b", re.IGNORECASE),  # 480 usd, 20 dollars
    re.compile(r"\busd\s?" + _NUMBER, re.IGNORECASE),                  # USD 480
]


_JOIN_MARK = "\uffff"  # a noncharacter: stands in for a masked span during the joined pass

_MASK_CLASSES = (
    [(rx, KEY) for rx in _MASK_SECRETS]
    + [(_TOKEN_RUN, None)]  # None: [key] only for a mixed-case run with digits
    + [(_EMAIL, EMAIL), (_ADDRESS, ADDRESS), (_ADDRESS_LOWER, ADDRESS), (_PHONE, PHONE)]
    + [(rx, AMOUNT) for rx in _MASK_MONEY]
)


def _placeholder(m: "re.Match[str]", placeholder: Optional[str]) -> Optional[str]:
    if placeholder is not None:
        return placeholder
    run = m.group(0)
    return KEY if (_UPPER.search(run) and _LOWER.search(run) and _DIGIT.search(run)) else None


def _deleted_before_matching(ch: str) -> bool:
    """A character the mask drops before any pattern runs: a spacing accent whose NFKC form is a
    space plus a combining mark (U+00B4, U+00A8, U+00B8, ...), which would otherwise split a token
    in two, and the join mark itself."""
    if ord(ch) < 0xA0:
        return False
    if ch == _JOIN_MARK:
        return True
    folded = unicodedata.normalize("NFKC", ch)
    return len(folded) >= 2 and folded[0] == " " and unicodedata.category(folded[1]) in ("Mn", "Mc", "Me")


def _mask_joined(t: str) -> str:
    """Mask every class on `t` with its format characters deleted (what a reader sees: a key
    split by a zero-width space or soft hyphen renders as one key), then put the text back with
    each masked span replaced by its placeholder and every other format character a space.
    Matches are replaced by join marks of the same length, so offsets stay valid and a later
    class never matches inside an earlier span."""
    keep = [i for i, ch in enumerate(t) if unicodedata.category(ch) != "Cf"]
    if len(keep) == len(t):
        return t
    joined = "".join(t[i] for i in keep)
    spans: List[Tuple[int, int, str]] = []
    for rx, placeholder in _MASK_CLASSES:
        def repl(m: "re.Match[str]", placeholder: Optional[str] = placeholder) -> str:
            ph = _placeholder(m, placeholder)
            if ph is None:
                return m.group(0)
            spans.append((m.start(), m.end(), ph))
            return _JOIN_MARK * (m.end() - m.start())
        joined = rx.sub(repl, joined)
    out: List[str] = []
    pos = 0
    for start, end, ph in sorted(spans):
        lo, hi = keep[start], keep[end - 1] + 1
        out.extend(" " if unicodedata.category(ch) == "Cf" else ch for ch in t[pos:lo])
        out.append(ph)
        pos = hi
    out.extend(" " if unicodedata.category(ch) == "Cf" else ch for ch in t[pos:])
    return "".join(out)


def mask(text: Any) -> str:
    """``text`` with secrets, emails, street addresses, phone numbers and money amounts replaced
    by placeholders. Never refuses; non-str gives "".

    Works on the NFKC form (a fullwidth "＄480" is masked like "$480"). Control characters and
    line/paragraph separators become spaces. Format characters (zero-width, soft hyphen, bidi
    overrides) are invisible, so the patterns first run on the text with them deleted (a key split
    by U+200B or U+00AD is one key to a reader and to the mask); every format character left
    outside a masked span then becomes a space and the patterns run again, so a zero-width
    character can neither hide a span nor reach the terminal. Spacing accents whose NFKC form is a
    space plus a combining mark (U+00B4) are deleted before anything else. Classes are applied in
    order (secrets, emails, addresses, phones, money) on the text the previous class left, so a
    span is masked once: a key holding digits becomes ``[key]``, never ``[phone]``; placeholders
    are never re-masked. Only the first MASK_SCAN_LIMIT code points are kept, which keeps the scan
    linear on any input.
    """
    if not isinstance(text, str):
        return ""
    t = "".join(ch for ch in text[: 2 * MASK_SCAN_LIMIT] if not _deleted_before_matching(ch))
    t = unicodedata.normalize("NFKC", t)[:MASK_SCAN_LIMIT]
    t = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in t)
    t = _mask_joined(t)
    for rx, placeholder in _MASK_CLASSES:
        t = rx.sub(lambda m, placeholder=placeholder: _placeholder(m, placeholder) or m.group(0), t)
    return t
