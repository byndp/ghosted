"""`ghosted init` on a terminal: the guided setup (contract 16.5, spec 6).

``run(ctx, io, effects=None) -> exit code``. ``ghosted report`` already works with nothing installed,
so every step here is optional and says what it adds:

0. config.toml: written with the defaults when missing (the workspace root is guessed as
   ~/workspace when that folder exists, and asked once); an existing one is edited in place.
1. "Keep a live record going forward?" -> ``install.apply(["hook"])``.
2. The channel picker, in order: Claude app (selectable when Claude Code's ``claude`` is found;
   contract 16.12), ntfy, Pushover (selectable only with both keys in .env), Mac notification only
   (macOS with osascript), None. The Claude app and ntfy show their pros and cons, so the user
   picks between them. Enter keeps the current setting.
3. Every channel sends a test push and asks "Did it arrive? [y/n]"; a no (or a failed send)
   offers the next selectable option.
4. With a push channel: the hourly job (``install.apply(["launchd"])``, its lines printed as they
   are, refusals included) and an optional watchdog ping URL. Ends with what was installed and
   ``ghosted uninstall --all``.

Why it is built this way:
- ``io`` is ``{ask(prompt, default) -> str, say(text)}`` (an object or a dict) and ``effects``
  holds every side effect outside config.toml (install, the adapter, platform checks, the
  clipboard, the topic generator), so tests script every path without a terminal or a system.
- Config changes are line edits (``set_value``): every other line and comment is kept, a missing
  table is appended, and the result must parse (tomllib and core.minitoml) as a valid config
  before it is written. Each write is atomic and happens only when a step is settled (the
  adapter only after the test push arrived), so Ctrl-C or EOF at any question leaves either the
  old file or a complete new one: never half a step. Exit code 1 then.
- The ntfy topic is the password of a public topic: it is never printed in full (a short masked
  prefix only). It goes on the clipboard when pbcopy works, and it is in config.toml either way.
- The watchdog URL is a credential too: only its host is echoed.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from core import config as config_mod

TOPIC_PREFIX = "ghosted-"
TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
MAX_ROUNDS = 8      # picker rounds before giving up (a scripted io that never says anything usable)
MAX_TRIES = 3       # re-asks of a yes/no or URL question
TEST_TITLE = "Ghosted"
TEST_BODY = "Test push: Ghosted can reach this device."
CRON_HINT = "2 7-23 * * *"  # when the hourly job does its work (core.install, cli RUN_HOURS)

HOOK_WHAT = "a small script Claude Code runs when a session stops"


class OnboardError(Exception):
    """A config edit that would not leave a valid config.toml (nothing is written)."""


# ---- io ------------------------------------------------------------------------------------------

class ConsoleIO:
    """The terminal: ``input()`` and stdout. EOFError / KeyboardInterrupt propagate to ``run``."""

    def ask(self, prompt: str, default: str = "") -> str:
        answer = input(prompt + " ")
        return answer.strip() or default

    def say(self, text: str = "") -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


class _IO:
    """``io`` as an object or as a ``{"ask", "say"}`` dict."""

    def __init__(self, io: Any) -> None:
        if isinstance(io, Mapping):
            self._ask, self._say = io["ask"], io["say"]
        else:
            self._ask, self._say = io.ask, io.say

    def ask(self, prompt: str, default: str = "") -> str:
        answer = self._ask(prompt, default)
        return (answer if isinstance(answer, str) else "").strip()

    def say(self, text: str = "") -> None:
        self._say(text)


# ---- side effects --------------------------------------------------------------------------------

@dataclass
class Effects:
    """Everything onboarding does outside config.toml. ``install(names)`` returns install.Step
    objects (``.ok``, ``.line(home)``); ``adapter_for(cfg, env)`` returns an adapter or None."""

    install: Callable[[List[str]], List[Any]]
    adapter_for: Callable[[Any, Mapping[str, str]], Any]
    mac_available: Callable[[], bool]
    hook_installed: Callable[[], bool]
    launchd_installed: Callable[[], bool]
    copy: Callable[[str], bool]
    new_topic: Callable[[], str]
    # Our hook is registered but runs another copy of Ghosted (a moved clone, an old plugin
    # version folder): `ghosted doctor`'s "points elsewhere".
    hook_stale: Callable[[], bool] = lambda: False
    # The session rules in ~/.claude/CLAUDE.md (contract 16.11 part C). None = the step is skipped.
    claude_md_present: Optional[Callable[[], Any]] = None
    claude_md_add: Optional[Callable[[List[str]], Any]] = None
    # The absolute path of Claude Code's `claude`, or None (the Claude app channel needs it).
    find_claude: Callable[[], Optional[str]] = lambda: None


def new_topic() -> str:
    """The v1 topic: "ghosted-" + 32 url-safe characters (192 random bits)."""
    return TOPIC_PREFIX + secrets.token_urlsafe(24)


def _install_ctx(environ: Mapping[str, str], now: Any) -> Any:
    from core import install

    return install.make_context(cfg=config_mod.load_config(environ=environ), environ=environ, now=now)


def default_effects(environ: Mapping[str, str], now: Any = None) -> Effects:
    """The real side effects. Each one reloads config.toml, so it sees the edits made so far."""

    def do_install(names: List[str]) -> List[Any]:
        from core import install

        return install.apply(_install_ctx(environ, now), names)

    def adapter_for(cfg: Any, env: Mapping[str, str]) -> Any:
        import notify

        return notify.get_adapter(cfg, env)

    def mac_available() -> bool:
        if sys.platform != "darwin":
            return False
        try:
            from notify import macos
        except ImportError:
            return False
        return macos.find_osascript() is not None

    def status(check_loaded: bool) -> Dict[str, Dict[str, Any]]:
        from core import install

        return install.status(_install_ctx(environ, now), check_loaded=check_loaded)

    def hook_installed() -> bool:
        """Registered and pointing at this copy (a stale handler is not "installed")."""
        try:
            hook = status(False)["hook"]
            return hook.get("state") == "registered" and bool(hook.get("matches_repo"))
        except Exception:  # noqa: BLE001 - unknown means "ask"
            return False

    def hook_stale() -> bool:
        try:
            hook = status(False)["hook"]
            return hook.get("state") == "registered" and not hook.get("matches_repo")
        except Exception:  # noqa: BLE001
            return False

    def launchd_installed() -> bool:
        try:
            job = status(True)["launchd"]
            return bool(job.get("plist") and job.get("matches") and job.get("loaded"))
        except Exception:  # noqa: BLE001
            return False

    def claude_md_present() -> Any:
        from core import claudemd

        return claudemd.present(claudemd.claude_md_path(_install_ctx(environ, now).home))

    def claude_md_add(keys: List[str]) -> Any:
        from core import claudemd

        return claudemd.add(_install_ctx(environ, now), keys)

    def find_claude() -> Optional[str]:
        try:
            from notify import claudeapp
        except ImportError:
            return None
        found = claudeapp.find_claude("", environ)
        # Not realpath: ~/.local/bin/claude is a stable link to a versioned binary that updates replace.
        return os.path.abspath(found) if found else None

    return Effects(install=do_install, adapter_for=adapter_for, mac_available=mac_available,
                   hook_installed=hook_installed, launchd_installed=launchd_installed,
                   copy=copy_to_clipboard, new_topic=new_topic, hook_stale=hook_stale,
                   claude_md_present=claude_md_present, claude_md_add=claude_md_add,
                   find_claude=find_claude)


def copy_to_clipboard(text: str) -> bool:
    """pbcopy on macOS; False anywhere else or on any failure. The text never reaches a terminal."""
    if sys.platform != "darwin":
        return False
    try:
        proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ---- config line edits ---------------------------------------------------------------------------

_HEADER = re.compile(r"^\s*\[(\[?)([^\[\]]*)\]?\]\s*(#.*)?$")


def _header_name(line: str) -> Optional[str]:
    """The table a header line opens ('notify.ntfy'), '[[x]]' for an array table, else None."""
    m = _HEADER.match(line)
    if not m:
        return None
    name = "".join(m.group(2).split())
    return "[[%s]]" % name if m.group(1) else name


def _regions(lines: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    """table name -> (index of its header or -1 for the top level, index just past its region)."""
    out: Dict[str, Tuple[int, int]] = {}
    start, name = -1, ""
    for i, line in enumerate(lines):
        header = _header_name(line)
        if header is not None:
            out.setdefault(name, (start, i))
            start, name = i, header
    out.setdefault(name, (start, len(lines)))
    return out


def _value_end(text: str) -> Optional[int]:
    """Where a TOML value at the start of ``text`` ends (string, or a bare scalar), else None."""
    if text.startswith(('"""', "'''")):
        return None
    if text.startswith('"'):
        i = 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return i + 1
            i += 1
        return None
    if text.startswith("'"):
        end = text.find("'", 1)
        return end + 1 if end > 0 else None
    if text.startswith("["):
        return None  # arrays may span lines; onboarding never edits one
    m = re.search(r"\s#", text)
    return m.start() if m else len(text.rstrip())


def set_value(text: str, table: str, key: str, literal: str) -> str:
    """config.toml text with ``key = literal`` in ``[table]`` ('' = the top level). The key's
    line is replaced (its trailing comment kept); a missing key goes after the table's last
    setting; a missing table is appended. Every other line stays byte for byte."""
    lines = text.split("\n")
    regions = _regions(lines)
    key_re = re.compile(r"^(\s*)" + re.escape(key) + r"\s*=\s*(.*)$")
    if table in regions:
        start, end = regions[table]
        for i in range(start + 1, end):
            m = key_re.match(lines[i])
            if not m:
                continue
            rest = m.group(2)
            cut = _value_end(rest)
            if cut is None:
                raise OnboardError("%s.%s is written in a form this setup does not edit" % (table, key))
            tail = rest[cut:]
            keep = tail if re.match(r"^\s*(#.*)?$", tail) else ""
            lines[i] = "%s%s = %s%s" % (m.group(1), key, literal, keep)
            return "\n".join(lines)
        at = start + 1
        for i in range(start + 1, end):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith("#"):
                at = i + 1
        lines.insert(at, "%s = %s" % (key, literal))
        return "\n".join(lines)
    body = text if text.endswith("\n") or not text else text + "\n"
    if body and not body.endswith("\n\n"):
        body += "\n"
    return body + "[%s]\n%s = %s\n" % (table, key, literal)


def toml_string(value: str) -> str:
    """A TOML basic string (the same escaping as ``ghosted init``)."""
    out = ['"']
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def checked_config(text: str, home: Path, environ: Mapping[str, str], was_minitoml_ok: bool = True) -> Any:
    """The Config ``text`` gives, or OnboardError. When the old file was readable by
    core.minitoml (the hourly job's reader before Python 3.11), the new one must be too."""
    try:
        cfg = config_mod.build_config(config_mod.parse_config_text(text), home, environ)
    except config_mod.ConfigError as exc:
        raise OnboardError(str(exc)) from None
    if was_minitoml_ok:
        problem = config_mod.minitoml_problem(text)
        if problem:
            raise OnboardError("the hourly job could not read the result: %s" % problem)
    return cfg


def mask_topic(topic: str) -> str:
    """'ghosted-Ab…': enough to recognise the topic, not enough to subscribe to it."""
    shown = len(TOPIC_PREFIX) + 2
    return topic[:shown] + "…" if len(topic) > shown else "…"


def origin_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower() or "the URL"
    except ValueError:
        return "the URL"


# ---- the flow ------------------------------------------------------------------------------------

@dataclass
class Channel:
    key: str            # adapter name
    label: str
    note: str           # shown after the label
    selectable: bool
    pros: Tuple[str, ...] = ()   # shown under the label, so the user can weigh the channels
    cons: Tuple[str, ...] = ()


# Contract 16.12: the two phone channels side by side; the user decides.
CLAUDE_APP_PROS = ("no new app or account: it uses the Claude app already on your phone",
                   "pushes stay within Anthropic")
CLAUDE_APP_CONS = ("needs a claude.ai sign-in (not an API key)",
                   "each push is a short Haiku turn on your Claude plan, about 10 s, and a usage limit stops it",
                   "relies on undocumented Claude Code behaviour that an update can change",
                   "one \"Remote control enabled\" push when it starts")
NTFY_PROS = ("free, no account, works with any Claude sign-in",
             "instant and documented")
NTFY_CONS = ("install the ntfy app",
             "pushes pass through ntfy.sh, and anyone with your topic can read them (titles are kept neutral)")


class _Session:
    """One onboarding run: the config text as it is on disk now, and what got installed."""

    def __init__(self, ctx: Any, io: _IO, fx: Effects) -> None:
        self.env: Mapping[str, str] = getattr(ctx, "env", None) or os.environ
        self.io = io
        self.fx = fx
        self.home = config_mod.resolve_home(None, self.env)
        self.path = self.home / "config.toml"
        self.user_home = Path(self.env.get("HOME") or os.path.expanduser("~"))
        self.text = ""
        self.cfg: Any = None
        self.minitoml_ok = True
        self.installed: List[str] = []
        self.claude_path: Optional[str] = None  # set by channels()

    # -- helpers -----------------------------------------------------------------------------------

    def tilde(self, path: Any) -> str:
        text, base = str(path), str(self.user_home).rstrip("/")
        if base and text == base:
            return "~"
        if base and text.startswith(base + "/"):
            return "~" + text[len(base):]
        return text

    def yes(self, prompt: str, default: Optional[bool]) -> bool:
        """y/n; a blank answer is ``default``, or asked again when there is none (then no)."""
        for _ in range(MAX_TRIES):
            answer = self.io.ask(prompt, "").lower()
            if not answer and default is not None:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.io.say("Please answer y or n.")
        return False

    def write(self, text: str) -> None:
        """Validate, then write config.toml atomically (through a symlink to its target)."""
        from core import ledger

        cfg = checked_config(text, self.home, self.env, self.minitoml_ok)
        ledger.ensure_dir(self.home)
        target = Path(os.path.realpath(str(self.path))) if self.path.is_symlink() else self.path
        ledger.atomic_write_text(target, text)
        self.text, self.cfg = text, cfg

    def edit(self, table: str, key: str, literal: str) -> str:
        return set_value(self.text, table, key, literal)

    def show_steps(self, steps: Sequence[Any]) -> bool:
        for step in steps:
            try:
                self.io.say("  " + step.line(self.user_home))
            except Exception:  # noqa: BLE001 - a step without line(): say what we can
                self.io.say("  " + str(step))
        return bool(steps) and all(getattr(step, "ok", False) for step in steps)

    # -- step 0 ------------------------------------------------------------------------------------

    def load_or_create(self) -> bool:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            data = None
        except OSError as exc:
            self.io.say("Cannot read %s: %s." % (self.tilde(self.path), exc.strerror or exc))
            return False
        if data is not None:
            try:
                self.text = data.decode("utf-8")
                self.cfg = config_mod.build_config(config_mod.parse_config_text(self.text), self.home, self.env)
            except (UnicodeDecodeError, config_mod.ConfigError) as exc:
                self.io.say("%s has a problem: %s" % (self.tilde(self.path), exc))
                self.io.say("Fix it (or move it aside), then run ghosted init again.")
                return False
            self.minitoml_ok = config_mod.minitoml_problem(self.text) is None
            self.io.say("Using your settings in %s." % self.tilde(self.path))
            return True
        roots: List[str] = []
        guess = self.user_home / "workspace"
        if guess.is_dir():
            if self.yes("Your projects live in ~/workspace? [Y/n]", True):
                roots = ["~/workspace"]
        else:
            folder = self.io.ask("Which folder holds your projects? (e.g. ~/code; Enter to skip)", "")
            if folder:
                if config_mod.expand_path(folder, self.env).is_dir():
                    roots = [folder]
                else:
                    self.io.say("%s is not a folder here; skipping it." % folder)
        from core.cli import render_config

        text = render_config("", "none", roots, "", self.fx.new_topic(),
                             env_file=self.tilde(self.home / ".env"))
        self.write(text)
        self.io.say("Wrote %s." % self.tilde(self.path))
        return True

    # -- step 1 ------------------------------------------------------------------------------------

    def live_record(self) -> None:
        self.io.say("")
        if self.fx.hook_installed():
            self.io.say("Live record: on (the hook is already installed).")
            return
        if self.fx.hook_stale():
            self.io.say("Live record: the hook is installed but runs another copy of Ghosted "
                        "(it moved, or an older version), so it may have stopped recording.")
            if not self.yes("Point it at this copy? [Y/n]", True):
                self.io.say("Left as it is. Refresh it later with ghosted install --hook.")
                return
            if self.show_steps(self.fx.install(["hook"])):
                self.installed.append("the hook (refreshed)")
            return
        self.io.say("Live record: a hook (%s) tells Ghosted about each ending as it happens. "
                    "Without it, ghosted report reads your past sessions instead." % HOOK_WHAT)
        if not self.yes("Keep a live record going forward? [Y/n]", True):
            self.io.say("Skipped. Add it later with ghosted install --hook.")
            return
        if self.show_steps(self.fx.install(["hook"])):
            self.installed.append("the hook")

    # -- step 1b: the session rules (contract 16.11 part C) ------------------------------------------

    RULE_PROMPTS = {
        "le_add": "Add this rule, so sessions register what waits on you and ghosted report can show it? [y/N]",
        "edit_tools": "Add this rule, so one session's edits can't silently overwrite another's? [y/N]",
    }

    def session_rules(self) -> None:
        if self.fx.claude_md_present is None or self.fx.claude_md_add is None:
            return
        from core import claudemd

        where = "~/.claude/CLAUDE.md"
        self.io.say("")
        try:
            have = set(self.fx.claude_md_present() or ())
        except Exception:  # noqa: BLE001 - an unreadable file: skip the step, change nothing
            self.io.say("Session rules: could not read %s, so it was left alone." % where)
            return
        missing = [(k, t) for k, t in claudemd.RULES if k not in have]
        if not missing:
            self.io.say("Session rules: both are already in %s." % where)
            return
        self.io.say("Session rules: Claude Code reads %s in every session. Two short rules there help "
                    "Ghosted. A yes adds the rule between ghosted markers (ghosted uninstall --claude-md "
                    "removes them); nothing else in the file changes." % where)
        chosen: List[str] = []
        for key, text in missing:
            self.io.say("")
            for line in text.splitlines():
                self.io.say("    " + line)
            if self.yes(self.RULE_PROMPTS[key], False):
                chosen.append(key)
        if not chosen:
            self.io.say("Skipped. docs/guide.md has the same text if you want to paste it yourself.")
            return
        if self.show_steps([self.fx.claude_md_add(chosen)]):
            self.installed.append("the session rules in %s" % where)

    # -- step 2 and 3 ------------------------------------------------------------------------------

    def channels(self) -> List[Channel]:
        try:
            env = config_mod.load_env(self.cfg)
        except config_mod.ConfigError:
            env = {}
        keys = "PUSHOVER_TOKEN" in env and "PUSHOVER_USER" in env
        mac = bool(self.fx.mac_available())
        self.claude_path = self.fx.find_claude()
        return [
            Channel("claude-app", "Claude app",
                    "pushes to the Claude app already on your phone" if self.claude_path
                    else "needs Claude Code's claude command", bool(self.claude_path),
                    CLAUDE_APP_PROS, CLAUDE_APP_CONS),
            Channel("ntfy", "ntfy", "free, no account: a phone app", True, NTFY_PROS, NTFY_CONS),
            Channel("pushover", "Pushover", "$5 phone app" if keys else
                    "$5 phone app; add PUSHOVER_TOKEN and PUSHOVER_USER to %s first"
                    % self.tilde(self.cfg.env_file), keys),
            Channel("macos", "Mac notification only", "a banner on this Mac, no phone" if mac else
                    "needs macOS", mac),
            Channel("none", "None", "no pushes; run ghosted report when you want the list", True),
        ]

    def current_label(self) -> str:
        """'off', 'on ntfy', 'on Mac notifications': reads after "Pushes are" / "keeps pushes"."""
        adapter = self.cfg.adapter
        return "off" if adapter == "none" else "on " + {
            "remote": "your Worker (remote)", "macos": "Mac notifications",
            "pushover": "Pushover", "claude-app": "the Claude app"}.get(adapter, adapter)

    def pick(self, options: List[Channel], answer: str) -> Optional[Channel]:
        text = answer.strip().lower()
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1]
        aliases = {"mac": "macos", "off": "none", "claude app": "claude-app", "claude": "claude-app"}
        text = aliases.get(text, text)
        for option in options:
            if option.key == text:
                return option
        return None

    def nudges(self) -> None:
        self.io.say("")
        self.io.say("Nudges: pushes follow fixed rules: a 09:00 digest, at most 3 a day, nothing "
                    "from 22:00 to 08:00, and only what is waiting on you.")
        options = self.channels()
        for _ in range(MAX_ROUNDS):
            for n, option in enumerate(options, 1):
                self.io.say("  %d. %s: %s" % (n, option.label, option.note))
                for line in option.pros:
                    self.io.say("       + " + line)
                for line in option.cons:
                    self.io.say("       - " + line)
            answer = self.io.ask("Pick a number (Enter keeps pushes %s):" % self.current_label(), "")
            if not answer:
                self.io.say("Pushes stay %s." % self.current_label())
                return
            option = self.pick(options, answer)
            if option is None:
                self.io.say("Type a number from 1 to %d." % len(options))
                continue
            if not option.selectable:
                self.io.say("%s is not available yet: %s." % (option.label, option.note))
                continue
            if self.try_from(options, option):
                return
        self.io.say("Pushes stay %s." % self.current_label())

    def try_from(self, options: List[Channel], option: Channel) -> bool:
        """Try ``option``; on a no, offer the next selectable one. True when settled, False to go
        back to the picker."""
        while True:
            if option.key == "none":
                self.set_adapter("none")
                return True
            if self.trial(option):
                return True
            later = options[options.index(option) + 1:]
            following = next((o for o in later if o.selectable), None)
            if following is None or following.key == "none":
                if self.yes("Nothing else to try. Keep pushes %s? [Y/n]" % self.current_label(), True):
                    self.io.say("Pushes stay %s." % self.current_label())
                    return True
                return False
            if not self.yes("Try %s instead? [Y/n]" % following.label, True):
                return False
            option = following

    def set_adapter(self, name: str) -> None:
        if self.cfg.adapter == name:
            self.io.say("Pushes stay %s." % self.current_label())
            return
        self.write(self.edit("notify", "adapter", toml_string(name)))
        self.io.say("Pushes are now %s." % self.current_label())

    def prepare_ntfy(self) -> None:
        """A topic in config.toml before the test (kept when it is already valid: the phone may be
        subscribed to it), and how to subscribe without printing it."""
        topic = self.cfg.ntfy_topic
        if not TOPIC_RE.match(topic or ""):
            topic = self.fx.new_topic()
            self.write(self.edit("notify.ntfy", "topic", toml_string(topic)))
        where = "%s under [notify.ntfy]" % self.tilde(self.path)
        if self.fx.copy(topic):
            found = "It is on your clipboard now (%s), and in %s." % (mask_topic(topic), where)
        else:
            found = "Copy it from %s (it starts %s)." % (where, mask_topic(topic))
        self.io.say("Install the ntfy app on your phone, tap +, and subscribe to your topic. The topic "
                    "works like a password, so it is not shown here. " + found)
        self.io.ask("Press Enter once you have subscribed.", "")

    def prepare_claude_app(self) -> None:
        """claude_path in config.toml (launchd's PATH is short), the notifier's folder, and the
        one-time trust step Claude Code needs before it runs a background session there."""
        from core import notifier

        if self.claude_path and self.cfg.claude_app_path != self.claude_path:
            self.write(self.edit("notify.claude-app", "claude_path", toml_string(self.claude_path)))
        folder = notifier.ensure_folder(self.cfg.home)
        self.io.say("The Claude app gets its pushes from a small background Claude Code session named "
                    "Ghosted. Claude Code runs it only in a folder it trusts, so once, in another "
                    "terminal, run:")
        self.io.say("    cd %s && claude" % self.tilde(folder))
        self.io.say("answer yes to trusting the folder, then type /exit. The first test push takes "
                    "about 20 seconds, and the phone also shows \"Remote control enabled: Ghosted\".")
        self.io.ask("Press Enter once Claude Code trusts the folder.", "")

    def trial(self, option: Channel) -> bool:
        """Test push through ``option``; config.toml switches to it only once it arrived."""
        if option.key == "ntfy":
            self.prepare_ntfy()
        elif option.key == "claude-app":
            self.prepare_claude_app()
        candidate = self.edit("notify", "adapter", toml_string(option.key))
        cfg = checked_config(candidate, self.home, self.env, self.minitoml_ok)
        self.io.say("Sending a test push through %s." % option.label)
        try:
            env = config_mod.load_env(cfg)
        except config_mod.ConfigError:
            env = {}
        try:
            adapter = self.fx.adapter_for(cfg, env)
            result = adapter.send([], title=TEST_TITLE, body=TEST_BODY, kind="test")
            ok, error = bool(getattr(result, "ok", False)), getattr(result, "error", None)
        except Exception as exc:  # noqa: BLE001 - one short line; adapters keep secrets out of errors
            ok, error = False, type(exc).__name__
        if not ok:
            self.io.say("The test push did not go out: %s." % (error or "unknown error"))
            return False
        if self.yes("Did it arrive? [y/n]", None):
            self.write(candidate)
            self.io.say("Pushes are now %s." % self.current_label())
            if option.key == "claude-app" and self.cfg.claude_app_fallback == "ntfy" \
                    and TOPIC_RE.match(self.cfg.ntfy_topic or ""):
                self.io.say("Your ntfy topic is still set, so ntfy stays the backup: a push the Claude "
                            "app can't confirm goes through ntfy.")
            return True
        if option.key == "claude-app":
            self.io.say("On the phone, check that Claude may send notifications, and allow Claude in "
                        "your Focus modes: under a Focus, pushes land silently in Notification Center.")
        elif option.key == "macos":
            self.io.say("macOS files these banners under Script Editor: check that it may send "
                        "notifications in System Settings > Notifications.")
        elif option.key == "ntfy":
            self.io.say("Check the topic in the ntfy app, and that it may send notifications.")
        return False

    # -- step 4 ------------------------------------------------------------------------------------

    def hourly_job(self) -> None:
        if self.cfg.adapter == "none":
            return
        self.io.say("")
        if self.fx.launchd_installed():
            self.io.say("The hourly job is already installed.")
        else:
            self.io.say("Pushes need the hourly job: launchd runs ghosted run at 2 minutes past each hour, "
                        "and it works from 07:00 to 23:59 your time.")
            if not self.yes("Install it now? [Y/n]", True):
                self.io.say("Skipped. Add it later with ghosted install --launchd.")
                return
            if not self.show_steps(self.fx.install(["launchd"])):
                return
            self.installed.append("the hourly job")
        self.watchdog()

    def watchdog(self) -> None:
        current = self.cfg.watchdog_ping_url
        if current:
            self.io.say("Watchdog: on (pings %s)." % origin_of(current))
            return
        self.io.say("Optional watchdog: a ping URL from a service such as healthchecks.io. The hourly "
                    "job calls it after each run, and the service tells you if the calls stop. Give "
                    "the check the schedule %s in your timezone, so quiet nights are not an outage."
                    % CRON_HINT)
        for _ in range(MAX_TRIES):
            url = self.io.ask("Paste the ping URL, or press Enter to skip:", "")
            if not url:
                self.io.say("No watchdog. Add one later under [watchdog] ping_url in config.toml.")
                return
            problem = config_mod.https_url_problem(url, "The ping URL")
            if problem:
                self.io.say(problem + ".")
                continue
            self.write(self.edit("watchdog", "ping_url", toml_string(url)))
            self.io.say("Watchdog on: the hourly job pings %s." % origin_of(url))
            return
        self.io.say("No watchdog for now.")

    # -- the end -----------------------------------------------------------------------------------

    def summary(self) -> None:
        self.io.say("")
        self.io.say("Done. Installed this time: %s." % (" and ".join(self.installed) if self.installed
                                                         else "nothing new"))
        self.io.say("Pushes are %s." % self.current_label())
        self.io.say("See what is waiting on you: ghosted report")
        self.io.say("To remove everything Ghosted installed: ghosted uninstall --all")


def run(ctx: Any, io: Any, effects: Optional[Effects] = None) -> int:
    """The guided setup. 0 when it ran to the end; 1 when it stopped (EOF, Ctrl-C, a config.toml
    it cannot read or edit safely). What was written before a stop is complete and valid."""
    channel = _IO(io)
    env = getattr(ctx, "env", None) or os.environ
    if effects is None:
        try:
            now = getattr(ctx, "now", None)
        except Exception:  # noqa: BLE001 - a bad GHOSTED_NOW: install stamps use the clock
            now = None
        effects = default_effects(env, now)
    session = _Session(ctx, channel, effects)
    try:
        channel.say("Ghosted setup. ghosted report already works without any of this; each step "
                    "adds something and can be skipped.")
        if not session.load_or_create():
            return 1
        session.live_record()
        session.session_rules()
        session.nudges()
        session.hourly_job()
        session.summary()
        return 0
    except (EOFError, KeyboardInterrupt):
        try:
            channel.say("")
            channel.say("Stopped. What was already saved stays; the rest is unchanged.")
        except Exception:  # noqa: BLE001 - the terminal may be gone
            pass
        return 1
    except OnboardError as exc:
        channel.say("Could not update %s safely: %s. Nothing more was changed." % (session.tilde(session.path), exc))
        return 1
