"""Configuration: where GHOSTED_HOME is, what config.toml says, and the .env secrets file.

`load_config(home=None, environ=None)` never writes anything. GHOSTED_HOME = `home` if given, else env
GHOSTED_HOME, else ~/.ghosted. `~` expands with environ["HOME"] when present. A missing
config.toml means every default below; unknown keys are kept in `cfg.raw` and ignored; a value of
the wrong type is a `ConfigError` naming the key (never a silent fallback).

timezone "" resolves to the system zone: the target of the /etc/localtime symlink (the part after
"zoneinfo/"), then the TZ environment variable, else "UTC". So `cfg.timezone` is always a concrete
IANA name.

v2 keys (contract 16): `[detect] direct_questions` ("waiting" | "soft": whether a direct question
at the end of a session waits on you or counts as soft) and `[watchdog] ping_url` (https only: the
hourly job GETs it after an ok scheduled run, so a dead-man service hears when the Mac stops).

Env GHOSTED_PROJECTS_DIR, when set and not blank, overrides projects_dir (config.toml's value and the
default alike; `~` expands as above). It lets a test, a benchmark or a one-off scan point the
detector at another transcript folder without editing config.toml. A bare `Config()` honours it
too.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

from core import classify as _classify
from core import minitoml, timeutil

try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:  # 3.9 / 3.10
    _tomllib = None

_toml_loads: Callable[[str], Dict[str, Any]] = (
    _tomllib.loads if _tomllib is not None else minitoml.loads
)

ADAPTERS = ("none", "ntfy", "pushover", "remote", "macos", "claude-app")
CLAUDE_APP_FALLBACKS = ("ntfy", "none")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DIRECT_QUESTIONS = ("waiting", "soft")  # [detect] direct_questions: the tier of a direct question
DEFAULT_HOME = "~/.ghosted"
DEFAULT_PROJECTS_DIR = "~/.claude/projects"
PROJECTS_DIR_ENV = "GHOSTED_PROJECTS_DIR"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_IGNORE_TAIL_PATTERNS = [
    r"(?i)anything else\?\s*$",
    r"(?i)does (that|this) (help|make sense)\?\s*$",
]
LOCALTIME_PATH = "/etc/localtime"
ENV_KEYS = ("GHOSTED_SERVICE_TOKEN_ID", "GHOSTED_SERVICE_TOKEN_SECRET", "PUSHOVER_TOKEN", "PUSHOVER_USER",
            "NTFY_TOKEN")


class ConfigError(ValueError):
    """config.toml (or .env) can't be used as written. The message names the key, not secrets."""


def _default_home() -> Path:
    # GHOSTED_HOME as the contract defines it: env GHOSTED_HOME, else ~/.ghosted. So a bare
    # Config() in a test that sets GHOSTED_HOME never lands on the real ledger.
    return resolve_home()


def _default_projects_dir() -> Path:
    return default_projects_dir()


@dataclass
class Config:
    """Resolved settings. Attribute names are the config.toml keys, flattened (contract §2)."""

    home: Path = field(default_factory=_default_home)
    timezone: str = "UTC"
    projects_dir: Path = field(default_factory=_default_projects_dir)
    workspace_roots: List[Path] = field(default_factory=list)
    scan_since_days: int = 14
    maybe_active_minutes: int = 30
    review_days: int = 7
    env_file: Optional[Path] = None  # None -> home / ".env"
    skip_entrypoints: List[str] = field(default_factory=lambda: ["sdk-cli"])
    skip_first_prompt_patterns: List[str] = field(default_factory=list)
    skip_path_patterns: List[str] = field(default_factory=list)
    skip_temp_folders: bool = True  # sessions launched under /tmp and the like (contract 16.13)
    ignore_tail_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_TAIL_PATTERNS))
    adapter: str = "none"
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    remote_url: str = ""
    digest_hour: int = 9
    quiet_start: int = 22
    quiet_end: int = 8
    daily_budget: int = 3
    direct_questions: str = "waiting"
    # The hand-off cues (contract 16.10): the defaults plus the opt-in name patterns; [] = off.
    handoff_patterns: List[str] = field(default_factory=lambda: list(_classify.DEFAULT_HANDOFF_PATTERNS))
    handoff_names: List[str] = field(default_factory=list)
    watchdog_ping_url: str = ""
    unsaved_stale_days: int = 3
    unsaved_unpushed_days: int = 1
    unsaved_unmerged_days: int = 14
    unsaved_no_remote_ok: List[Path] = field(default_factory=list)
    mixed_window_seconds: int = 15  # contract 16.11: mtime <-> tool-call attribution window
    # contract 16.12: [notify.claude-app]
    claude_app_path: str = ""
    claude_app_fallback: str = "ntfy"
    claude_app_model: str = "haiku"
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.home = Path(self.home)
        self.env_file = self.home / ".env" if self.env_file is None else Path(self.env_file)
        self.projects_dir = Path(self.projects_dir)
        self.workspace_roots = [Path(p) for p in self.workspace_roots]
        self.unsaved_no_remote_ok = [Path(p) for p in self.unsaved_no_remote_ok]

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"


# ---- paths and timezone --------------------------------------------------------------------------

def expand_path(value: Union[str, Path], environ: Optional[Mapping[str, str]] = None) -> Path:
    """Expand a leading ~ (using environ["HOME"] when given) and make the path absolute."""
    env = os.environ if environ is None else environ
    text = str(value)
    if text == "~" or text.startswith("~/"):
        home = env.get("HOME") or os.path.expanduser("~")
        text = home + text[1:]
    elif text.startswith("~"):
        text = os.path.expanduser(text)
    return Path(os.path.abspath(text))


def projects_dir_override(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The GHOSTED_PROJECTS_DIR value when it is set and not blank, else None."""
    env = os.environ if environ is None else environ
    value = env.get(PROJECTS_DIR_ENV)
    return value.strip() if isinstance(value, str) and value.strip() else None


def claude_projects_default(environ: Optional[Mapping[str, str]] = None) -> str:
    """Where Claude Code keeps its sessions when nothing says otherwise: ``projects`` under
    CLAUDE_CONFIG_DIR when that is set (Claude Code's own setting), else ~/.claude/projects."""
    env = os.environ if environ is None else environ
    base = env.get(CLAUDE_CONFIG_DIR_ENV)
    if isinstance(base, str) and base.strip():
        return os.path.join(base.strip(), "projects")
    return DEFAULT_PROJECTS_DIR


def default_projects_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """projects_dir when config.toml says nothing: GHOSTED_PROJECTS_DIR, else Claude Code's folder
    (``claude_projects_default``)."""
    env = os.environ if environ is None else environ
    return expand_path(projects_dir_override(env) or claude_projects_default(env), env)


def resolve_home(home: Union[str, Path, None] = None,
                 environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if home is not None and str(home) != "":
        return expand_path(home, env)
    override = env.get("GHOSTED_HOME")
    if override:
        return expand_path(override, env)
    return expand_path(DEFAULT_HOME, env)


REAL_MANIFEST_ENV = "GHOSTED_REAL_MANIFEST"


def real_manifest_path(home: Union[str, Path, None] = None,
                       environ: Optional[Mapping[str, str]] = None) -> Path:
    """The hand-labelled real-session manifest (uuids, folder names, notes: private, never in the
    repo): env GHOSTED_REAL_MANIFEST, else <GHOSTED_HOME>/fixtures/real-manifest.json."""
    env = os.environ if environ is None else environ
    override = env.get(REAL_MANIFEST_ENV)
    if override:
        return expand_path(override, env)
    return resolve_home(home, env) / "fixtures" / "real-manifest.json"


def _zone_after_marker(text: str) -> Optional[str]:
    marker = "zoneinfo/"
    idx = text.rfind(marker)
    name = text[idx + len(marker):] if idx >= 0 else text
    name = name.strip().strip("/")
    for prefix in ("posix/", "right/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name or None


def _zone_from_localtime(path: str) -> Optional[str]:
    try:
        target = os.readlink(path)
    except (OSError, ValueError):
        return None  # missing, or a copied file rather than a symlink
    if "zoneinfo/" not in target:
        return None
    return timeutil.canonical_timezone(_zone_after_marker(target))


def _zone_from_tz_env(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if text.startswith(":"):
        text = text[1:]
    name = _zone_after_marker(text) if "zoneinfo/" in text else text.strip()
    return timeutil.canonical_timezone(name)


def system_timezone(environ: Optional[Mapping[str, str]] = None,
                    localtime_path: Optional[str] = None) -> str:
    """The machine's IANA zone: /etc/localtime symlink target, then TZ, else 'UTC'."""
    env = os.environ if environ is None else environ
    path = LOCALTIME_PATH if localtime_path is None else localtime_path
    return _zone_from_localtime(path) or _zone_from_tz_env(env.get("TZ")) or "UTC"


# ---- typed getters ---------------------------------------------------------------------------------

def _table(raw: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    table: Any = raw
    for depth in range(len(path)):
        table = table.get(path[depth], {})
        if not isinstance(table, dict):
            raise ConfigError("[%s] must be a table" % ".".join(path[: depth + 1]))
    return table


def _str(table: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError("%s must be a string" % where)
    return value


def _int(table: Mapping[str, Any], key: str, default: int, where: str, lo: int, hi: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s must be a whole number" % where)
    if not lo <= value <= hi:
        raise ConfigError("%s must be between %d and %d" % (where, lo, hi))
    return value


def _bool(table: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError("%s must be true or false" % where)
    return value


def _str_list(table: Mapping[str, Any], key: str, default: List[str], where: str) -> List[str]:
    value = table.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError("%s must be a list of strings" % where)
    return list(value)


def _regex_list(table: Mapping[str, Any], key: str, default: List[str], where: str) -> List[str]:
    patterns = _str_list(table, key, default, where)
    for index, pattern in enumerate(patterns):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError("%s[%d] is not a valid regular expression: %s" % (where, index, exc)) from None
    return patterns


def _enum(table: Mapping[str, Any], key: str, default: str, where: str, choices: Sequence[str]) -> str:
    value = _str(table, key, default, where).strip().lower()
    if value not in choices:
        raise ConfigError("%s must be one of %s" % (where, ", ".join(choices)))
    return value


def https_url_problem(value: str, where: str) -> Optional[str]:
    """Why ``value`` is not a usable https:// URL, or None (also None for ""). The message never
    repeats the URL: a ping URL is a credential (anyone holding it can fake a heartbeat)."""
    if not value:
        return None
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        return "%s must not contain spaces or control characters" % where
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "%s is not a valid URL" % where
    if parts.scheme.lower() != "https" or not parts.hostname:
        return "%s must be an https:// URL" % where
    if parts.username is not None or parts.password is not None:
        return "%s must not contain a user name or password" % where
    return None


# ---- loading -----------------------------------------------------------------------------------------

def parse_config_text(text: str) -> Dict[str, Any]:
    """Parse config.toml text with tomllib (3.11+) or the minitoml fallback."""
    if text.startswith("﻿"):  # a UTF-8 byte-order mark (Windows editors)
        text = text[1:]
    try:
        return _toml_loads(text)
    except ValueError as exc:  # tomllib.TOMLDecodeError and minitoml.TOMLDecodeError
        raise ConfigError("config.toml: %s" % exc) from None


def minitoml_problem(text: str) -> Optional[str]:
    """Why ``core.minitoml`` (the TOML reader of Pythons before 3.11, such as /usr/bin/python3
    3.9 that launchd and the hook run) cannot read this config.toml text, or None when it can.
    tomllib accepts more (inline tables, multi-line strings, dates), so a config that `ghosted` on a
    newer python reads fine can still stop every hourly run."""
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        minitoml.loads(text)
    except ValueError as exc:
        return str(exc)
    return None


def config_minitoml_problem(home: Path) -> Optional[str]:
    """minitoml_problem for GHOSTED_HOME/config.toml (None when the file is missing or readable)."""
    try:
        text = (Path(home) / "config.toml").read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return minitoml_problem(text)


def build_config(raw: Dict[str, Any], home: Path,
                 environ: Optional[Mapping[str, str]] = None) -> Config:
    """Turn parsed config.toml data into a validated Config rooted at `home`."""
    env = os.environ if environ is None else environ
    if not isinstance(raw, dict):
        raise ConfigError("config.toml must be a table of settings")

    tz = _str(raw, "timezone", "", "timezone").strip()
    if tz:
        canonical = timeutil.canonical_timezone(tz)
        if canonical is None:
            raise ConfigError("timezone %r is not a known IANA zone (e.g. America/Chicago)" % tz)
        tz = canonical
    else:
        tz = system_timezone(env)

    roots = _str_list(raw, "workspace_roots", [], "workspace_roots")
    if any(not r.strip() for r in roots):
        raise ConfigError("workspace_roots must not contain empty paths")

    env_file_text = _str(raw, "env_file", "", "env_file").strip()
    skip = _table(raw, "skip")
    detect = _table(raw, "detect")
    notify = _table(raw, "notify")
    ntfy = _table(raw, "notify", "ntfy")
    remote = _table(raw, "notify", "remote")
    push = _table(raw, "push")
    watchdog = _table(raw, "watchdog")
    handoff_names = _str_list(detect, "handoff_names", [], "detect.handoff_names")
    for name in handoff_names:
        if not _classify.valid_handoff_name(name):
            raise ConfigError("detect.handoff_names: %r must be a plain name (letters, spaces, . ' -)" % name)
    handoff_base = _regex_list(detect, "handoff_patterns", list(_classify.DEFAULT_HANDOFF_PATTERNS),
                               "detect.handoff_patterns")
    unsaved = _table(raw, "unsaved")
    no_remote_ok = _str_list(unsaved, "no_remote_ok", [], "unsaved.no_remote_ok")
    if any(not r.strip() for r in no_remote_ok):
        raise ConfigError("unsaved.no_remote_ok must not contain empty paths")

    adapter = _str(notify, "adapter", "none", "notify.adapter").strip().lower()
    if adapter not in ADAPTERS:
        raise ConfigError("notify.adapter must be one of %s" % ", ".join(ADAPTERS))

    claude_app = _table(raw, "notify", "claude-app")
    claude_app_path = _str(claude_app, "claude_path", "", "notify.claude-app.claude_path").strip()
    claude_app_model = _str(claude_app, "model", "haiku", "notify.claude-app.model").strip()
    if not _MODEL_RE.match(claude_app_model):
        raise ConfigError("notify.claude-app.model must be a model name such as \"haiku\"")

    ping_url = _str(watchdog, "ping_url", "", "watchdog.ping_url").strip()
    problem = https_url_problem(ping_url, "watchdog.ping_url")
    if problem:
        raise ConfigError(problem)

    projects_text = _str(raw, "projects_dir", claude_projects_default(env), "projects_dir").strip()
    if not projects_text:
        raise ConfigError("projects_dir must not be empty")
    projects_text = projects_dir_override(env) or projects_text

    return Config(
        home=home,
        timezone=tz,
        projects_dir=expand_path(projects_text, env),
        workspace_roots=[expand_path(r.strip(), env) for r in roots],
        scan_since_days=_int(raw, "scan_since_days", 14, "scan_since_days", 0, 36500),
        maybe_active_minutes=_int(raw, "maybe_active_minutes", 30, "maybe_active_minutes", 0, 100000),
        review_days=_int(raw, "review_days", 7, "review_days", 0, 36500),
        env_file=expand_path(env_file_text, env) if env_file_text else home / ".env",
        skip_entrypoints=_str_list(skip, "entrypoints", ["sdk-cli"], "skip.entrypoints"),
        skip_first_prompt_patterns=_regex_list(skip, "first_prompt_patterns", [],
                                               "skip.first_prompt_patterns"),
        skip_path_patterns=_str_list(skip, "path_patterns", [], "skip.path_patterns"),
        skip_temp_folders=_bool(skip, "temp_folders", True, "skip.temp_folders"),
        ignore_tail_patterns=_regex_list(detect, "ignore_tail_patterns",
                                         list(DEFAULT_IGNORE_TAIL_PATTERNS),
                                         "detect.ignore_tail_patterns"),
        adapter=adapter,
        ntfy_server=_str(ntfy, "server", "https://ntfy.sh", "notify.ntfy.server").strip(),
        ntfy_topic=_str(ntfy, "topic", "", "notify.ntfy.topic").strip(),
        remote_url=_str(remote, "url", "", "notify.remote.url").strip(),
        digest_hour=_int(push, "digest_hour", 9, "push.digest_hour", 0, 23),
        quiet_start=_int(push, "quiet_start", 22, "push.quiet_start", 0, 24),
        quiet_end=_int(push, "quiet_end", 8, "push.quiet_end", 0, 24),
        daily_budget=_int(push, "daily_budget", 3, "push.daily_budget", 0, 1000),
        direct_questions=_enum(detect, "direct_questions", "waiting", "detect.direct_questions",
                               DIRECT_QUESTIONS),
        watchdog_ping_url=ping_url,
        handoff_patterns=_classify.handoff_patterns_for(handoff_base, handoff_names),
        handoff_names=handoff_names,
        unsaved_stale_days=_int(unsaved, "stale_days", 3, "unsaved.stale_days", 0, 36500),
        unsaved_unpushed_days=_int(unsaved, "unpushed_days", 1, "unsaved.unpushed_days", 0, 36500),
        unsaved_unmerged_days=_int(unsaved, "unmerged_days", 14, "unsaved.unmerged_days", 0, 36500),
        unsaved_no_remote_ok=[expand_path(r.strip(), env) for r in no_remote_ok],
        mixed_window_seconds=_int(_table(raw, "mixed"), "window_seconds", 15, "mixed.window_seconds", 1, 600),
        claude_app_path=claude_app_path,
        claude_app_fallback=_enum(claude_app, "fallback", "ntfy", "notify.claude-app.fallback",
                                  CLAUDE_APP_FALLBACKS),
        claude_app_model=claude_app_model,
        raw=raw,
    )


def load_config(home: Union[str, Path, None] = None,
                environ: Optional[Mapping[str, str]] = None) -> Config:
    """Read GHOSTED_HOME/config.toml (if any) and return the resolved Config. Writes nothing."""
    env = os.environ if environ is None else environ
    home_path = resolve_home(home, env)
    path = home_path / "config.toml"
    raw: Dict[str, Any] = {}
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = None
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc.strerror or exc)) from None
    if data is not None:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigError("%s is not UTF-8 text" % path) from None
        raw = parse_config_text(text)
    return build_config(raw, home_path, env)


# ---- secrets -----------------------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_INLINE_COMMENT = re.compile(r"\s#")


def _env_value(text: str) -> Optional[str]:
    """The value part of KEY=value: optional matching quotes, or an unquoted value whose
    whitespace-prefixed '#' starts a comment. None for an unterminated quote."""
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        return text[1:end] if end > 0 else None
    m = _INLINE_COMMENT.search(text)
    return text[: m.start()].rstrip() if m else text


def load_env(cfg: Config) -> Dict[str, str]:
    """Read cfg.env_file: KEY=value lines; blanks, # comments and malformed lines are skipped,
    keys with empty values are left out (so `key in env` means it is set). Values are never
    printed or logged. A missing file gives {}."""
    path = Path(cfg.env_file) if cfg.env_file is not None else Path(cfg.home) / ".env"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc.strerror or exc)) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("%s is not UTF-8 text" % path) from None
    out: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        value = _env_value(m.group(2))
        if value:
            out[m.group(1)] = value
    return out
