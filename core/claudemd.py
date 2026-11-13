"""The two session rules, offered to every user (contract 16.11 part C).

Ghosted can only show what a session left behind. Two rules in ``~/.claude/CLAUDE.md`` make
sessions leave less behind: register what waits on the user with ``ghosted add`` before the last
message (the only fix for hand-offs buried mid-session), and edit files with the edit tools, which
refuse to overwrite a file another session changed since it was read. ``ghosted init`` shows each rule
word for word and asks; a yes writes it between ``ghosted`` markers. Nothing else in the file
is read back, printed or changed. The plugin itself never edits it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional, Set, Tuple

BEGIN = "<!-- ghosted:begin -->"
END = "<!-- ghosted:end -->"
COMPONENT = "claude-md"

GHOSTED_ADD = """\
## Ghosted

Before your last message: if it, a memory note or a handoff doc says something waits on me (a
decision, an answer, a check you promised for a date), run `ghosted add` for each item first:
`ghosted add "<what it is>" --public "<neutral title: no names, amounts or emails>" [--due MM-DD]`,
plus `--owner claude` when the next step is yours. When one is settled, run `ghosted done GS-xxxx`."""

EDIT_TOOLS = """\
## Editing files

Edit files with the Edit/Write tools, not `sed -i`, scripts or heredocs through Bash. Edit refuses
to overwrite a file that changed since you read it (another session may be working in the same
folder), and checkpoints can rewind it. Bash is fine for generated output (builds, scratch files)
and bulk mechanical changes across many files; say so when you do it."""

RULES: Tuple[Tuple[str, str], ...] = (("le_add", GHOSTED_ADD), ("edit_tools", EDIT_TOOLS))
KEYS = tuple(k for k, _ in RULES)


def text_of(key: str) -> str:
    for k, text in RULES:
        if k == key:
            return text
    raise ValueError("unknown rule %r (use %s)" % (key, ", ".join(KEYS)))


def claude_md_path(home: Any) -> Path:
    return Path(home) / ".claude" / "CLAUDE.md"


def _real(path: Path) -> Path:
    """A symlinked CLAUDE.md is written through to its target, never replaced."""
    return Path(os.path.realpath(str(path))) if path.is_symlink() else path


class Unbalanced(ValueError):
    """The markers are not exactly one begin before one end: editing could eat the user's text."""


def _span(text: str) -> Optional[Tuple[int, int]]:
    """(start, end) of our block, END included; None when there is none. Anything but zero
    markers or exactly one BEGIN before one END raises Unbalanced, so an
    orphan marker is never paired with one written later."""
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return None
    start, end = text.find(BEGIN), text.find(END)
    if begins != 1 or ends != 1 or end < start:
        raise Unbalanced("the ghosted markers in it are unbalanced")
    return start, end + len(END)


def _keys_in(block: str) -> Set[str]:
    return {k for k, text in RULES if text in block}


def _read(real: Path) -> str:
    """The file's text with its own line endings (read_text would turn CRLF into LF)."""
    return real.read_bytes().decode("utf-8")


def present(path: Any) -> Set[str]:
    """The rules already in the file's block (empty when the file or the block is missing or
    the markers are unbalanced)."""
    try:
        text = _read(_real(Path(path)))
        span = _span(text)
    except (OSError, UnicodeDecodeError, Unbalanced):
        return set()
    return _keys_in(text[span[0]:span[1]].replace("\r\n", "\n")) if span else set()


def render(keys: Iterable[str]) -> str:
    wanted = set(keys)
    return "\n".join([BEGIN, "\n\n".join(text for k, text in RULES if k in wanted), END])


def _write(real: Path, text: str) -> None:
    try:
        mode = os.stat(str(real)).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + real.name + ".", suffix=".tmp", dir=str(real.parent))
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(text.encode("utf-8"))
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, str(real))
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def add(ctx: Any, keys: Iterable[str]) -> Any:
    """Write the chosen rules into the block (the union with what is there). Idempotent."""
    from core import install

    keys = list(keys)
    for key in keys:
        text_of(key)  # unknown -> ValueError
    path = claude_md_path(ctx.home)
    real = _real(path)
    try:
        text = _read(real)
        existed = True
    except FileNotFoundError:
        text, existed = "", False
    except UnicodeDecodeError:
        return install.Step(COMPONENT, "refuse", str(path), "it is not UTF-8 text; nothing was changed", ok=False)
    try:
        span = _span(text)
    except Unbalanced as exc:
        return install.Step(COMPONENT, "refuse", str(path), "%s; fix them by hand, nothing was changed" % exc,
                            ok=False)
    have = _keys_in(text[span[0]:span[1]].replace("\r\n", "\n")) if span else set()
    want = have | set(keys)
    if span and want == have:
        return install.Step(COMPONENT, "exists", str(path), "the rules are already there")
    block = render(want)
    if span:
        new = text[:span[0]] + block + text[span[1]:]
    elif text:
        new = text + ("" if text.endswith("\n") else "\n") + "\n" + block + "\n"
    else:
        new = block + "\n"
    if existed:
        install._backup(ctx, real, name="CLAUDE.md")
    _write(real, new)
    names = " and ".join(k.replace("_", "-") for k in KEYS if k in want)
    return install.Step(COMPONENT, "update" if existed else "create", str(path), "rules: " + names, changed=True)


def remove(ctx: Any, dry_run: bool = False) -> Any:
    """Remove our block and the blank line before it; every other byte stays."""
    from core import install

    path = claude_md_path(ctx.home)
    real = _real(path)
    try:
        text = _read(real)
    except FileNotFoundError:
        return install.Step(COMPONENT, "absent", str(path))
    except UnicodeDecodeError:
        return install.Step(COMPONENT, "refuse", str(path), "it is not UTF-8 text; nothing was changed", ok=False)
    try:
        span = _span(text)
    except Unbalanced as exc:
        return install.Step(COMPONENT, "refuse", str(path), "%s; fix them by hand, nothing was changed" % exc,
                            ok=False)
    if span is None:
        return install.Step(COMPONENT, "absent", str(path), "no ghosted block")
    if dry_run:
        return install.Step(COMPONENT, "remove", str(path), "the ghosted block", changed=True)
    before, after = text[:span[0]], text[span[1]:]
    if after.startswith("\n"):
        after = after[1:]
    if before.endswith("\n\n"):
        before = before[:-1]
    new = before + after
    install._backup(ctx, real, name="CLAUDE.md")
    if new.strip() or path.is_symlink():
        _write(real, new)
    else:
        os.unlink(str(real))  # it held nothing but our block (we created it)
    return install.Step(COMPONENT, "remove", str(path), "the ghosted block", changed=True)
