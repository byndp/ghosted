"""Mac notification adapter: a local banner through ``osascript`` (contract 16.5).

For people who want a nudge but no phone app: ``display notification "<body>" with title
"<title>"``. Facts behind the choices:

- ``osascript`` runs with an argument list (never a shell), stdin closed, a 10 s timeout. The
  title and body are AppleScript string literals: ``\\`` and ``"`` are escaped, control
  characters (newlines too) become spaces, so no text can end the literal and run as script.
- The banner shows only on this Mac, only while it is awake, and macOS files it under
  "Script Editor": if that app's notifications are off in System Settings, nothing appears and
  osascript still exits 0. So ``send`` can only report that macOS accepted it; onboarding asks
  "Did it arrive?" and ``check()`` names the setting.
- Same public-title rule as every adapter: ``base.local_title_leak`` refuses a push whose text
  still holds a Mac-only ``local_title``. (The banner never leaves the Mac, but the rule is the
  same everywhere so a caller can't come to rely on a leaky channel.)
- ``reachable()`` is True when osascript exists; a Mac without it claims no push.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from notify import base
from notify.base import SendResult

NAME = "macos"
OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 10.0
TITLE_MAX_CHARS = 120   # Notification Center cuts long titles anyway
BODY_MAX_CHARS = 400
SETTING_HINT = ("if nothing appeared, allow notifications for Script Editor in System Settings > "
                "Notifications")

Runner = Callable[[Sequence[str], float], Tuple[int, str, str]]

_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")


def default_runner(argv: Sequence[str], timeout: float = TIMEOUT) -> Tuple[int, str, str]:
    """Run without a shell; (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 126, "", exc.strerror or type(exc).__name__
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def find_osascript(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """/usr/bin/osascript when it is there, else osascript from PATH, else None."""
    if os.path.isfile(OSASCRIPT) and os.access(OSASCRIPT, os.X_OK):
        return OSASCRIPT
    return which("osascript")


def applescript_string(text: str) -> str:
    """``text`` as an AppleScript string literal: control characters become one space, then
    backslash and double quote are escaped."""
    flat = " ".join(_CONTROL.sub(" ", text).split())
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def script(title: str, body: str) -> str:
    """The one-line AppleScript handed to ``osascript -e``."""
    return "display notification %s with title %s" % (applescript_string(body), applescript_string(title))


class MacAdapter(base.Adapter):
    name = NAME
    owns_schedule = False

    def __init__(self, cfg: Any, env: Optional[Mapping[str, str]] = None, *,
                 runner: Optional[Runner] = None, timeout: float = TIMEOUT,
                 osascript: Optional[str] = None,
                 which: Callable[[str], Optional[str]] = shutil.which) -> None:
        self.cfg = cfg
        self.timeout = float(timeout)
        self._runner = runner or default_runner
        self._osascript = osascript
        self._which = which

    def _binary(self) -> Optional[str]:
        return self._osascript if self._osascript is not None else find_osascript(self._which)

    def reachable(self) -> Tuple[bool, Optional[str]]:
        if self._binary():
            return True, None
        return False, "osascript not found (Mac notifications need macOS)"

    def send(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
             priority: str = "normal", click_url: Optional[str] = None) -> SendResult:
        rows = base.iter_items(items)
        head = base.push_title(title)
        text = base.push_body(rows, body, head)
        leak = base.local_title_leak(rows, head, text, click_url)
        if leak:
            return SendResult(False, NAME, leak)
        binary = self._binary()
        if not binary:
            return SendResult(False, NAME, "osascript not found (Mac notifications need macOS)")
        argv = [binary, "-e", script(base.cap_chars(head, TITLE_MAX_CHARS),
                                     base.cap_chars(text, BODY_MAX_CHARS))]
        try:
            code, _, err = self._runner(argv, self.timeout)
        except Exception as exc:  # noqa: BLE001 - an injected runner; the outcome is still a result
            return SendResult(False, NAME, "osascript failed (%s)" % type(exc).__name__)
        if code == 0:
            return SendResult(True, NAME)
        if code == 124:
            return SendResult(False, NAME, "osascript timed out after %d s" % int(self.timeout))
        detail = base.safe_text((err or "").strip().splitlines()[-1] if (err or "").strip() else "", 80)
        return SendResult(False, NAME, "osascript exited %d%s" % (code, ": " + detail if detail else ""))

    def check(self) -> Tuple[bool, str]:
        binary = self._binary()
        if not binary:
            return False, "macos: osascript not found (Mac notifications need macOS)"
        return True, "macos: notifications via %s; %s" % (binary, SETTING_HINT)
