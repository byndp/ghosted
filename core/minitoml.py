"""A small TOML subset reader for Pythons without `tomllib` (3.9 and 3.10).

Supported: comments; bare, quoted and dotted keys; `[tables]` and `[dotted.tables]`; basic strings
with escapes; literal strings; integers (decimal with underscores, 0x/0o/0b); floats (fraction,
exponent, inf, nan); booleans; arrays (single- or multi-line, nested, trailing commas, comments
inside). For any document inside this subset the result equals `tomllib.loads` (tested).

Not supported, and refused with a `TOMLDecodeError` that names the line: multi-line strings, inline
tables, arrays of tables, dates and times. Error messages give line and column, never the content.
"""
from __future__ import annotations

import re
from typing import IO, Any, Dict, List, Set, Tuple

__all__ = ["TOMLDecodeError", "loads", "load"]


class TOMLDecodeError(ValueError):
    """Invalid TOML, or valid TOML outside the supported subset."""


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_SCALAR_TOKEN = re.compile(r"[0-9A-Za-z_+.:-]+")
_DEC_INT = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)\Z")
_HEX_INT = re.compile(r"0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*\Z")
_OCT_INT = re.compile(r"0o[0-7](?:_?[0-7])*\Z")
_BIN_INT = re.compile(r"0b[01](?:_?[01])*\Z")
_FLOAT = re.compile(
    r"[+-]?(?:0|[1-9](?:_?[0-9])*)"
    r"(?:\.[0-9](?:_?[0-9])*)?"
    r"(?:[eE][+-]?[0-9](?:_?[0-9])*)?\Z"
)
_DATE_OR_TIME = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}")
_SPECIAL_FLOATS = {
    "inf": float("inf"), "+inf": float("inf"), "-inf": float("-inf"),
    "nan": float("nan"), "+nan": float("nan"), "-nan": float("nan"),
}
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _is_control(ch: str) -> bool:
    """Characters TOML forbids inside strings and comments (tab is allowed)."""
    code = ord(ch)
    return (code < 0x20 and ch != "\t") or code == 0x7F


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.n = len(text)
        self.explicit: Set[Tuple[str, ...]] = set()  # tables opened by a [header]
        self.dotted: Set[Tuple[str, ...]] = set()  # tables created by dotted keys

    # ---- errors and low-level scanning -------------------------------------------------------

    def err(self, message: str, pos: int = -1) -> TOMLDecodeError:
        at = self.i if pos < 0 else pos
        line = self.s.count("\n", 0, at) + 1
        col = at - (self.s.rfind("\n", 0, at) + 1) + 1
        return TOMLDecodeError("%s (line %d, column %d)" % (message, line, col))

    def peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def skip_comment(self) -> None:
        if self.peek() != "#":
            return
        j = self.i + 1
        while j < self.n and self.s[j] not in "\r\n":
            if _is_control(self.s[j]):
                raise self.err("control character in a comment", j)
            j += 1
        self.i = j

    def skip_newline(self) -> bool:
        if self.s.startswith("\r\n", self.i):
            self.i += 2
            return True
        if self.peek() == "\n":
            self.i += 1
            return True
        return False

    def skip_blank(self) -> None:
        """Whitespace, comments and newlines (between statements and inside arrays)."""
        while self.i < self.n:
            self.skip_ws()
            self.skip_comment()
            if not self.skip_newline():
                if self.peek() == "\r":
                    raise self.err("carriage return without a line feed")
                return

    def end_of_line(self) -> None:
        self.skip_ws()
        self.skip_comment()
        if self.i >= self.n or self.skip_newline():
            return
        raise self.err("expected the end of the line after the value")

    # ---- document ---------------------------------------------------------------------------

    def parse(self) -> Dict[str, Any]:
        root: Dict[str, Any] = {}
        current = root
        current_path: Tuple[str, ...] = ()
        while True:
            self.skip_blank()
            if self.i >= self.n:
                return root
            if self.peek() == "[":
                if self.s.startswith("[[", self.i):
                    raise self.err("arrays of tables ([[...]]) are not supported by this TOML reader")
                self.i += 1
                self.skip_ws()
                path = tuple(self.parse_key())
                self.skip_ws()
                if self.peek() != "]":
                    raise self.err("expected ']' to close the table header")
                self.i += 1
                current = self.open_table(root, path)
                current_path = path
            else:
                start = self.i
                parts = self.parse_key()
                self.skip_ws()
                if self.peek() != "=":
                    raise self.err("expected '=' after the key")
                self.i += 1
                self.skip_ws()
                value = self.parse_value()
                self.assign(current, current_path, parts, value, start)
            self.end_of_line()

    def open_table(self, root: Dict[str, Any], path: Tuple[str, ...]) -> Dict[str, Any]:
        table = root
        for key in path:
            if key not in table:
                table[key] = {}
            nxt = table[key]
            if not isinstance(nxt, dict):
                raise self.err("table header [%s] conflicts with an existing value" % ".".join(path))
            table = nxt
        if path in self.explicit or path in self.dotted:
            raise self.err("table [%s] is defined more than once" % ".".join(path))
        self.explicit.add(path)
        return table

    def assign(self, table: Dict[str, Any], base: Tuple[str, ...], parts: List[str],
               value: Any, pos: int) -> None:
        for depth, key in enumerate(parts[:-1]):
            full = base + tuple(parts[: depth + 1])
            if key not in table:
                table[key] = {}
            nxt = table[key]
            # Dotted keys may not reach into a table that a [header] defined (as in tomllib).
            if not isinstance(nxt, dict) or full in self.explicit:
                raise self.err("dotted key conflicts with an existing value or table", pos)
            self.dotted.add(full)  # and that table can no longer get its own [header]
            table = nxt
        last = parts[-1]
        if last in table:
            raise self.err("duplicate key", pos)
        table[last] = value

    # ---- keys -------------------------------------------------------------------------------

    def parse_key(self) -> List[str]:
        parts = [self.parse_simple_key()]
        while True:
            self.skip_ws()
            if self.peek() != ".":
                return parts
            self.i += 1
            self.skip_ws()
            parts.append(self.parse_simple_key())

    def parse_simple_key(self) -> str:
        ch = self.peek()
        if ch == '"':
            return self.parse_basic_string()
        if ch == "'":
            return self.parse_literal_string()
        m = _BARE_KEY.match(self.s, self.i)
        if not m:
            raise self.err("expected a key")
        self.i = m.end()
        return m.group(0)

    # ---- values -----------------------------------------------------------------------------

    def parse_value(self) -> Any:
        ch = self.peek()
        if ch == '"':
            return self.parse_basic_string()
        if ch == "'":
            return self.parse_literal_string()
        if ch == "[":
            return self.parse_array()
        if ch == "{":
            raise self.err("inline tables ({...}) are not supported by this TOML reader")
        m = _SCALAR_TOKEN.match(self.s, self.i)
        if not m:
            raise self.err("expected a value")
        token = m.group(0)
        start = self.i
        self.i = m.end()
        if token == "true":
            return True
        if token == "false":
            return False
        if token in _SPECIAL_FLOATS:
            return _SPECIAL_FLOATS[token]
        if _DEC_INT.match(token):
            return int(token.replace("_", ""))
        for pattern, base in ((_HEX_INT, 16), (_OCT_INT, 8), (_BIN_INT, 2)):
            if pattern.match(token):
                return int(token[2:].replace("_", ""), base)
        if _FLOAT.match(token) and any(c in token for c in ".eE"):
            return float(token.replace("_", ""))
        if _DATE_OR_TIME.match(token):
            raise self.err("dates and times are not supported by this TOML reader", start)
        raise self.err("invalid value", start)

    def parse_array(self) -> List[Any]:
        self.i += 1  # the opening [
        out: List[Any] = []
        while True:
            self.skip_blank()
            if self.peek() == "]":
                self.i += 1
                return out
            if self.i >= self.n:
                raise self.err("unterminated array")
            out.append(self.parse_value())
            self.skip_blank()
            ch = self.peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == "]":
                self.i += 1
                return out
            raise self.err("expected ',' or ']' in the array")

    def parse_basic_string(self) -> str:
        if self.s.startswith('"""', self.i):
            raise self.err('multi-line strings (""") are not supported by this TOML reader')
        j = self.i + 1
        out: List[str] = []
        while True:
            if j >= self.n:
                raise self.err("unterminated string", self.i)
            ch = self.s[j]
            if ch == '"':
                self.i = j + 1
                return "".join(out)
            if ch == "\\":
                j = self.parse_escape(j, out)
                continue
            if ch in "\r\n":
                raise self.err("unterminated string", self.i)
            if _is_control(ch):
                raise self.err("control character in a string", j)
            out.append(ch)
            j += 1

    def parse_escape(self, j: int, out: List[str]) -> int:
        code = self.s[j + 1] if j + 1 < self.n else ""
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            return j + 2
        if code in ("u", "U"):
            width = 4 if code == "u" else 8
            digits = self.s[j + 2: j + 2 + width]
            if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise self.err("invalid unicode escape", j)
            value = int(digits, 16)
            if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise self.err("unicode escape is not a valid character", j)
            out.append(chr(value))
            return j + 2 + width
        raise self.err("invalid escape sequence in a string", j)

    def parse_literal_string(self) -> str:
        if self.s.startswith("'''", self.i):
            raise self.err("multi-line strings (''') are not supported by this TOML reader")
        j = self.i + 1
        while True:
            if j >= self.n:
                raise self.err("unterminated string", self.i)
            ch = self.s[j]
            if ch == "'":
                value = self.s[self.i + 1: j]
                self.i = j + 1
                return value
            if ch in "\r\n":
                raise self.err("unterminated string", self.i)
            if _is_control(ch):
                raise self.err("control character in a string", j)
            j += 1


def loads(text: str) -> Dict[str, Any]:
    """Parse a TOML document (within the supported subset) into plain dicts and lists."""
    if not isinstance(text, str):
        raise TypeError("loads() expects str, got %s" % type(text).__name__)
    return _Parser(text).parse()


def load(fp: IO[bytes]) -> Dict[str, Any]:
    """Parse a TOML document from a binary file object, like `tomllib.load`."""
    data = fp.read()
    if isinstance(data, str):
        raise TypeError("load() expects a binary file; open it with 'rb'")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TOMLDecodeError("the file is not valid UTF-8") from None
    return loads(text)
