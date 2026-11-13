"""Install and uninstall what makes Ghosted run on this Mac (contract sections 10 and 11).

Four components, each idempotent:

- ``bin``: symlink ``~/.local/bin/ghosted`` -> ``<repo>/bin/ghosted``. A different file or link
  already there is refused unless ``force``; a warning says when ``~/.local/bin`` is not on PATH,
  and another when a different program named ``ghosted`` comes first on PATH.
- ``skill``: symlink ``~/.claude/skills/ghosted`` -> ``<repo>/core/skills/ghosted`` (the
  ``/ghosted`` report). A real directory or file there is always refused; a link elsewhere is
  replaced only with ``force``.
- ``hook``: one NEW group appended to ``hooks.Stop`` and to ``hooks.StopFailure`` in
  ``~/.claude/settings.json``, exec form, async. Every other key and group is kept exactly; a
  symlinked settings file is written through to its target and keeps its mode; the file is
  backed up to ``GHOSTED_HOME/backups/settings.json.<timestamp>`` before any change; our group is
  recognised by ``args`` ending in ``[.../bin/ghosted, "hook"]``. The file's contents are never
  printed or logged (hook commands can hold secrets): errors name a line and column only.
- ``launchd``: ``~/Library/LaunchAgents/com.ghosted.run.plist`` rendered from the template
  in ``core/launchd/``, validated with plistlib, then ``launchctl bootout`` (may fail) and
  ``launchctl bootstrap gui/<uid> <plist>``. The job fires at :02 every hour of the system clock
  with ``GHOSTED_SCHEDULED=1``; `ghosted run` itself skips the hours outside 07:00-23:59 in the
  configured timezone, so a Mac on another zone still reports during that zone's day. Refused
  while config.toml uses TOML the job's interpreter cannot read (core.minitoml before Python
  3.11), and, on the machine that holds the real-session fixtures, until the hand-labelled
  negatives are in the manifest (spec rule 7; ``force`` installs anyway).

Every path is rooted at an injectable home directory, and every system call (``launchctl``,
``claude --version``) goes through an injectable runner, so tests never touch the real system.
``plan`` computes the steps and changes nothing (it runs no launchctl at all); ``apply`` and
``uninstall`` perform them and return the same kind of steps with what happened.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

COMPONENTS = ("bin", "skill", "hook", "launchd")
SKILL_NAMES = ("ghosted",)  # the report skill
BIN_NAMES = ("ghosted",)  # the command
LABEL = "com.ghosted.run"
PLIST_NAME = LABEL + ".plist"
HOOK_EVENTS = ("Stop", "StopFailure")
MIN_CLAUDE = (2, 1, 139)  # exec-form hooks (command + args)
SYSTEM_PYTHON = "/usr/bin/python3"
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
RUN_HOURS = tuple(range(0, 24))  # every hour of the system clock; `ghosted run` keeps 07-23 local
RUN_MINUTE = 2
SCHEDULED_ENV = "GHOSTED_SCHEDULED"

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "core" / "launchd" / (PLIST_NAME + ".template")

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]


def has_launchctl(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the hourly job can be installed here: launchd's ``launchctl`` is on PATH (macOS).
    Asked of PATH, not of the platform name, so a test's stand-in launchctl counts anywhere."""
    import shutil

    env = os.environ if environ is None else environ
    return shutil.which("launchctl", path=env.get("PATH") or os.defpath) is not None

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class InstallError(RuntimeError):
    """A component can't be planned or applied (the message never quotes file contents)."""


@dataclass
class Step:
    """One thing an install or uninstall does (or would do, for a plan)."""

    component: str
    action: str           # create replace update exists refuse remove absent keep load warn error
    path: str
    detail: str = ""
    ok: bool = True       # False: refused or failed
    changed: bool = False  # True: this step writes or removes something

    def line(self, home: Optional[Path] = None, dry_run: bool = False) -> str:
        verb = ("would " + self.action) if (dry_run and self.changed) else self.action
        where = tilde(self.path, home) if self.path else ""
        text = "%-8s %-9s %s" % (self.component, verb, where)
        if self.detail:
            text += (": " if where else "") + self.detail
        return text.rstrip()


@dataclass
class Context:
    """Where everything goes. Build it with ``make_context``."""

    home: Path
    repo: Path
    le_home: Path
    le_home_is_default: bool
    hook_python: str
    launchd_python: str
    runner: Runner
    uid: int
    path_env: str
    force: bool = False
    now: Optional[datetime] = None
    warnings: List[str] = field(default_factory=list)
    projects_dir: Optional[Path] = None
    manifest: Optional[Path] = None

    @property
    def bin_link(self) -> Path:
        return self.home / ".local" / "bin" / "ghosted"

    @property
    def bin_target(self) -> Path:
        return self.repo / "bin" / "ghosted"

    @property
    def bin_links(self) -> List[Tuple[Path, Path]]:
        """(link, target) for every command we link."""
        return [(self.home / ".local" / "bin" / name, self.repo / "bin" / name) for name in BIN_NAMES]

    @property
    def skill_link(self) -> Path:
        return self.home / ".claude" / "skills" / SKILL_NAMES[0]

    @property
    def skill_target(self) -> Path:
        return self.repo / "core" / "skills" / SKILL_NAMES[0]

    @property
    def skill_links(self) -> List[Tuple[Path, Path]]:
        """(link, target) for every skill we link."""
        return [(self.home / ".claude" / "skills" / name, self.repo / "core" / "skills" / name)
                for name in SKILL_NAMES]

    @property
    def settings_path(self) -> Path:
        return self.home / ".claude" / "settings.json"

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / PLIST_NAME

    @property
    def log_path(self) -> Path:
        return self.le_home / "logs" / "run.log"

    @property
    def backups_dir(self) -> Path:
        return self.le_home / "backups"

    @property
    def domain(self) -> str:
        return "gui/%d" % self.uid


# ---- context and runner ------------------------------------------------------------------------

def default_runner(argv: Sequence[str], timeout: float = 30.0) -> Tuple[int, str, str]:
    """Run a command without a shell; (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "%s: command not found" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "%s: timed out" % argv[0]
    except OSError as exc:
        return 126, "", "%s: %s" % (argv[0], exc.strerror or exc)
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def _install_table(cfg: Any) -> Mapping[str, Any]:
    raw = getattr(cfg, "raw", None) or {}
    table = raw.get("install") if isinstance(raw, dict) else None
    return table if isinstance(table, dict) else {}


def _python_choice(override: Any) -> str:
    if isinstance(override, str) and override.strip():
        return override.strip()
    return SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else sys.executable


def make_context(cfg: Any = None, home: Optional[os.PathLike] = None,
                 repo: Optional[os.PathLike] = None, runner: Optional[Runner] = None,
                 uid: Optional[int] = None, environ: Optional[Mapping[str, str]] = None,
                 force: bool = False, now: Optional[datetime] = None) -> Context:
    """A Context for this user. ``home`` defaults to environ HOME; GHOSTED_HOME comes from cfg.home
    (else GHOSTED_HOME, else ~/.ghosted); ``[install] hook_python`` / ``launchd_python``
    in config.toml override the interpreter (default /usr/bin/python3 when it exists)."""
    env = os.environ if environ is None else environ
    home_path = Path(home) if home is not None else Path(env.get("HOME") or os.path.expanduser("~"))
    home_path = Path(os.path.abspath(str(home_path)))
    default_le_home = home_path / ".ghosted"
    if cfg is not None and getattr(cfg, "home", None) is not None:
        le_home = Path(cfg.home)
    elif env.get("GHOSTED_HOME"):
        le_home = Path(os.path.abspath(os.path.expanduser(env["GHOSTED_HOME"])))
    else:
        le_home = default_le_home
    table = _install_table(cfg)
    return Context(
        home=home_path,
        repo=Path(repo).resolve() if repo is not None else REPO_ROOT,
        le_home=le_home,
        le_home_is_default=os.path.normpath(str(le_home)) == os.path.normpath(str(default_le_home)),
        hook_python=_python_choice(table.get("hook_python")),
        launchd_python=_python_choice(table.get("launchd_python")),
        runner=runner or default_runner,
        uid=os.getuid() if uid is None else int(uid),
        path_env=env.get("PATH", ""),
        force=bool(force),
        now=now,
        projects_dir=Path(cfg.projects_dir) if cfg is not None and getattr(cfg, "projects_dir", None) else None,
        manifest=_manifest_path(le_home, env),
    )


def _manifest_path(le_home: Path, env: Mapping[str, str]) -> Optional[Path]:
    from core import config as config_mod

    try:
        return config_mod.real_manifest_path(le_home, env)
    except Exception:  # noqa: BLE001 - the gate is best effort
        return None


def tilde(path: Any, home: Optional[Path] = None) -> str:
    """``path`` with the home directory shown as ``~``."""
    text = str(path)
    if home is None:
        return text
    base = str(home).rstrip(os.sep)
    if text == base:
        return "~"
    if base and text.startswith(base + os.sep):
        return "~" + text[len(base):]
    return text


def select(names: Iterable[str]) -> List[str]:
    """Normalise a component list ('all' expands); raises InstallError for an unknown name."""
    out: List[str] = []
    for name in names:
        if name == "all":
            wanted: Iterable[str] = COMPONENTS
        elif name in COMPONENTS:
            wanted = (name,)
        else:
            raise InstallError("unknown component %r (use %s or all)" % (name, ", ".join(COMPONENTS)))
        for item in wanted:
            if item not in out:
                out.append(item)
    return [c for c in COMPONENTS if c in out]


# ---- public entry points -----------------------------------------------------------------------

def plan(ctx: Context, components: Iterable[str]) -> List[Step]:
    """What ``apply`` would do. Touches nothing and runs no launchctl (only the read-only
    ``claude --version`` check)."""
    steps: List[Step] = []
    for name in select(components):
        try:
            steps.extend(_PLANNERS[name](ctx))
        except (OSError, InstallError) as exc:
            steps.append(Step(name, "error", "", _safe_error(exc), ok=False))
    return steps


def apply(ctx: Context, components: Iterable[str]) -> List[Step]:
    """Install the components; returns the steps with what happened."""
    steps: List[Step] = []
    for name in select(components):
        try:
            steps.extend(_APPLIERS[name](ctx))
        except (OSError, InstallError) as exc:
            steps.append(Step(name, "error", "", _safe_error(exc), ok=False))
    return steps


def uninstall(ctx: Context, components: Iterable[str], dry_run: bool = False) -> List[Step]:
    """Remove what we installed (only ours). With dry_run, changes nothing and runs nothing."""
    steps: List[Step] = []
    for name in select(components):
        try:
            steps.extend(_REMOVERS[name](ctx, dry_run))
        except (OSError, InstallError) as exc:
            steps.append(Step(name, "error", "", _safe_error(exc), ok=False))
    return steps


def status(ctx: Context, check_loaded: bool = True) -> Dict[str, Dict[str, Any]]:
    """Facts for `ghosted doctor`: each component's state, booleans and our own paths only."""
    out: Dict[str, Dict[str, Any]] = {}
    out["bin"] = dict(_links_status(ctx.bin_links), on_path=bin_dir_on_path(ctx), shadowed_by=other_program(ctx))
    out["skill"] = _skill_status(ctx)
    hook: Dict[str, Any] = {"path": str(ctx.settings_path), "events": {}, "matches_repo": False,
                            "python": None, "state": "missing"}
    try:
        data = _read_settings(ctx.settings_path)
    except InstallError as exc:
        hook["state"] = "unreadable"
        hook["error"] = str(exc)
    else:
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        desired = _desired_handler(ctx)
        exact = True
        for event in HOOK_EVENTS:
            handlers = [h for h in _our_handlers(hooks.get(event))]
            hook["events"][event] = bool(handlers)
            if handlers:
                hook["python"] = handlers[0].get("command")
            exact = exact and any(h == desired for h in handlers)
        present = [e for e in HOOK_EVENTS if hook["events"].get(e)]
        hook["state"] = ("registered" if len(present) == len(HOOK_EVENTS)
                         else "partial" if present else "missing")
        hook["matches_repo"] = bool(present) and exact
    out["hook"] = hook
    launchd: Dict[str, Any] = {"path": str(ctx.plist_path), "plist": ctx.plist_path.is_file(),
                               "matches": False, "loaded": None}
    if launchd["plist"]:
        try:
            launchd["matches"] = ctx.plist_path.read_bytes() == render_plist(ctx).encode("utf-8")
        except (OSError, InstallError):
            launchd["matches"] = False
    if check_loaded:
        launchd["loaded"] = launchd_loaded(ctx)
    out["launchd"] = launchd
    return out


# ---- bin and skill (symlinks) -------------------------------------------------------------------

def _link_state(link: Path, target: Path) -> str:
    """ours | missing | other-link | not-a-link."""
    if os.path.islink(str(link)):
        try:
            same = os.path.realpath(str(link)) == os.path.realpath(str(target))
        except OSError:
            same = False
        return "ours" if same else "other-link"
    if os.path.lexists(str(link)):
        return "not-a-link"
    return "missing"


def bin_dir_on_path(ctx: Context) -> bool:
    wanted = os.path.normpath(str(ctx.bin_link.parent))
    for entry in ctx.path_env.split(os.pathsep):
        if not entry:
            continue
        entry = os.path.expanduser(entry) if entry.startswith("~") else entry
        if os.path.normpath(entry) == wanted:
            return True
    return False


def _plan_link(ctx: Context, component: str, link: Path, target: Path,
               replace_file_ok: bool) -> Step:
    state = _link_state(link, target)
    dest = "-> " + tilde(target, ctx.home)
    if state == "ours":
        return Step(component, "exists", str(link), "already links " + dest)
    if state == "missing":
        return Step(component, "create", str(link), dest, changed=True)
    if state == "other-link":
        if ctx.force:
            return Step(component, "replace", str(link), "a link elsewhere, now " + dest, changed=True)
        return Step(component, "refuse", str(link),
                    "links somewhere else; rerun with --force to replace it", ok=False)
    # a real file or directory
    if os.path.isdir(str(link)) and not os.path.islink(str(link)):
        return Step(component, "refuse", str(link),
                    "a directory is in the way; move it aside first", ok=False)
    if replace_file_ok and ctx.force:
        return Step(component, "replace", str(link), "a file, now " + dest, changed=True)
    hint = "rerun with --force to replace it" if replace_file_ok else "move it aside first"
    return Step(component, "refuse", str(link), "a different file is in the way; " + hint, ok=False)


def _make_link(link: Path, target: Path) -> None:
    parent = link.parent
    if not parent.is_dir():
        os.makedirs(str(parent), mode=0o755, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + link.name + ".", suffix=".tmp", dir=str(parent))
    os.close(fd)
    os.unlink(tmp_name)
    os.symlink(str(target), tmp_name)
    try:
        os.replace(tmp_name, str(link))  # atomic: never a moment without a working `ghosted`
    except BaseException:
        if os.path.lexists(tmp_name):
            os.unlink(tmp_name)
        raise


def _remove_link(ctx: Context, component: str, link: Path, target: Path, dry_run: bool) -> Step:
    state = _link_state(link, target)
    if state == "missing":
        return Step(component, "absent", str(link), "nothing to remove")
    if state != "ours":
        return Step(component, "keep", str(link), "not ours, left alone")
    if not dry_run:
        os.unlink(str(link))
    return Step(component, "remove", str(link), "", changed=True)


def other_program(ctx: Context) -> Optional[str]:
    """The path of a different program named ``ghosted`` that PATH finds before ours, else None.

    The name is not ours alone (npm and PyPI each hold an unrelated ``ghosted`` package, contract
    16.19). Where another one comes first, the bare name starts it, so the user is given the
    full path of our link."""
    ours = os.path.realpath(str(ctx.bin_target))
    for entry in ctx.path_env.split(os.pathsep):
        if not entry:
            continue
        entry = os.path.expanduser(entry) if entry.startswith("~") else entry
        found = os.path.join(entry, "ghosted")
        if os.path.isfile(found) and os.access(found, os.X_OK):
            return None if os.path.realpath(found) == ours else found
    return None


def _plan_bin(ctx: Context) -> List[Step]:
    steps = [_plan_link(ctx, "bin", link, target, replace_file_ok=True) for link, target in ctx.bin_links]
    if not bin_dir_on_path(ctx):
        steps.append(Step("bin", "warn", "", "%s is not on PATH; add it to your shell profile"
                          % tilde(ctx.bin_link.parent, ctx.home)))
    other = other_program(ctx)
    if other:
        steps.append(Step("bin", "warn", "", "another program named ghosted (%s) comes first on PATH; "
                          "run %s instead" % (tilde(other, ctx.home), tilde(ctx.bin_link, ctx.home))))
    return steps


def _apply_bin(ctx: Context) -> List[Step]:
    steps = _plan_bin(ctx)
    for step, (link, target) in zip(steps, ctx.bin_links):
        if step.changed and step.ok:
            _make_link(link, target)
    return steps


_LINK_RANK = {"ours": 0, "missing": 1, "other-link": 2, "not-a-link": 3}


def _links_status(pairs: Sequence[Tuple[Path, Path]]) -> Dict[str, Any]:
    """One component's state for `ghosted doctor`: ``ours`` only when every link is ours; else the
    worst link's state and path. ``links`` holds each link's own state."""
    links = [{"path": str(link), "state": _link_state(link, target)} for link, target in pairs]
    worst = max(links, key=lambda entry: _LINK_RANK.get(entry["state"], 0))
    shown = links[0] if worst["state"] == "ours" else worst
    return {"path": shown["path"], "state": worst["state"], "links": links}


def _skill_status(ctx: Context) -> Dict[str, Any]:
    return _links_status(ctx.skill_links)


def _plan_skill(ctx: Context) -> List[Step]:
    return [_plan_link(ctx, "skill", link, target, replace_file_ok=False) for link, target in ctx.skill_links]


def _apply_skill(ctx: Context) -> List[Step]:
    steps = _plan_skill(ctx)
    for step, (link, target) in zip(steps, ctx.skill_links):
        if step.changed and step.ok:
            _make_link(link, target)
    return steps


def _uninstall_bin(ctx: Context, dry_run: bool) -> List[Step]:
    return [_remove_link(ctx, "bin", link, target, dry_run) for link, target in ctx.bin_links]


def _uninstall_skill(ctx: Context, dry_run: bool) -> List[Step]:
    return [_remove_link(ctx, "skill", link, target, dry_run) for link, target in ctx.skill_links]


# ---- hook (settings.json) ------------------------------------------------------------------------

def _desired_handler(ctx: Context) -> Dict[str, Any]:
    return {"type": "command", "command": ctx.hook_python,
            "args": [str(ctx.repo / "bin" / "ghosted"), "hook"], "async": True}


def is_our_handler(handler: Any) -> bool:
    """Our handler: a command whose args end in [<...>/bin/ghosted, "hook"]."""
    if not isinstance(handler, dict) or handler.get("type", "command") != "command":
        return False
    args = handler.get("args")
    if not isinstance(args, list) or len(args) < 2 or args[-1] != "hook":
        return False
    script = args[-2]
    return isinstance(script, str) and (script == "bin/ghosted" or script.endswith("/bin/ghosted"))


def _our_handlers(groups: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(groups, list):
        return out
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            out.extend(h for h in group["hooks"] if is_our_handler(h))
    return out


def _read_settings(path: Path) -> Dict[str, Any]:
    """settings.json as a dict ({} when missing). Errors never quote the file."""
    real = Path(os.path.realpath(str(path)))
    try:
        data = real.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise InstallError("can't read %s: %s" % (path.name, exc.strerror or "error")) from None
    if not data.strip():
        return {}
    try:
        obj = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise InstallError("%s is not UTF-8 text; fix it first" % path.name) from None
    except json.JSONDecodeError as exc:
        raise InstallError("%s is not valid JSON (line %d, column %d); fix it first"
                           % (path.name, exc.lineno, exc.colno)) from None
    except (ValueError, RecursionError):
        raise InstallError("%s is not valid JSON; fix it first" % path.name) from None
    if not isinstance(obj, dict):
        raise InstallError("%s is not a JSON object; fix it first" % path.name)
    return obj


def _upsert_group(groups: List[Any], desired: Dict[str, Any]) -> Tuple[List[Any], bool]:
    """Keep every group as it is; refresh our handler in place (first one), drop duplicates of
    it, or append a new group with it. Returns (groups, changed)."""
    found = False
    changed = False
    out: List[Any] = []
    for group in groups:
        if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)
                and any(is_our_handler(h) for h in group["hooks"])):
            out.append(group)
            continue
        handlers: List[Any] = []
        for handler in group["hooks"]:
            if not is_our_handler(handler):
                handlers.append(handler)
            elif not found:
                found = True
                if handler != desired:
                    changed = True
                handlers.append(dict(desired))
            else:
                changed = True  # a duplicate of ours
        if handlers:
            new_group = dict(group)
            new_group["hooks"] = handlers
            out.append(new_group)
        else:
            changed = True
    if not found:
        out.append({"hooks": [dict(desired)]})
        changed = True
    return out, changed


def _remove_ours(groups: List[Any]) -> Tuple[List[Any], bool]:
    changed = False
    out: List[Any] = []
    for group in groups:
        if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)
                and any(is_our_handler(h) for h in group["hooks"])):
            out.append(group)
            continue
        changed = True
        handlers = [h for h in group["hooks"] if not is_our_handler(h)]
        if handlers:
            new_group = dict(group)
            new_group["hooks"] = handlers
            out.append(new_group)
    return out, changed


def _hooks_table(data: Dict[str, Any], path: Path) -> Dict[str, Any]:
    hooks = data.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise InstallError('%s: "hooks" is not an object; fix it first' % path.name)
    for event in HOOK_EVENTS:
        if event in hooks and not isinstance(hooks[event], list):
            raise InstallError('%s: "hooks.%s" is not a list; fix it first' % (path.name, event))
    return hooks


def edit_settings_install(data: Dict[str, Any], desired: Dict[str, Any],
                          path: Path = Path("settings.json")) -> Tuple[Dict[str, Any], List[str]]:
    """A copy of ``data`` with our group in Stop and StopFailure; the events it changed."""
    hooks = dict(_hooks_table(data, path))
    changed: List[str] = []
    for event in HOOK_EVENTS:
        groups, did = _upsert_group(list(hooks.get(event) or []), desired)
        if did:
            hooks[event] = groups
            changed.append(event)
    if not changed:
        return data, []
    out = dict(data)
    out["hooks"] = hooks
    return out, changed


def edit_settings_uninstall(data: Dict[str, Any],
                            path: Path = Path("settings.json")) -> Tuple[Dict[str, Any], List[str]]:
    """A copy of ``data`` without our handlers (and without arrays they leave empty)."""
    if "hooks" not in data:
        return data, []
    hooks = dict(_hooks_table(data, path))
    changed: List[str] = []
    for event in HOOK_EVENTS:
        if event not in hooks:
            continue
        groups, did = _remove_ours(list(hooks[event]))
        if did:
            changed.append(event)
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
    if not changed:
        return data, []
    out = dict(data)
    if hooks:
        out["hooks"] = hooks
    else:
        del out["hooks"]
    return out, changed


def _stamp(ctx: Context) -> str:
    now = ctx.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup(ctx: Context, real: Path, name: str = "settings.json") -> Optional[Path]:
    """Copy the file's bytes to GHOSTED_HOME/backups/<name>.<timestamp> (0600)."""
    try:
        data = real.read_bytes()
    except FileNotFoundError:
        return None
    _ensure_private_dir(ctx.le_home)
    _ensure_private_dir(ctx.backups_dir)
    base = ctx.backups_dir / (name + "." + _stamp(ctx))
    target = base
    counter = 1
    while os.path.lexists(str(target)):
        counter += 1
        target = Path("%s.%d" % (base, counter))
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


def _ensure_private_dir(path: Path) -> None:
    if path.is_dir():
        return
    os.makedirs(str(path), mode=0o700, exist_ok=True)
    os.chmod(str(path), 0o700)


def _write_settings(real: Path, data: Dict[str, Any]) -> None:
    """Atomic write to the real file (never replacing a symlink), keeping its mode."""
    try:
        mode = os.stat(str(real)).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o600
    parent = real.parent
    if not parent.is_dir():
        os.makedirs(str(parent), mode=0o700, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="." + real.name + ".", suffix=".tmp", dir=str(parent))
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


def claude_version(ctx: Context) -> Optional[Tuple[int, int, int]]:
    """`claude --version` as a tuple, or None when it can't be run or read."""
    try:
        code, out, _ = ctx.runner(["claude", "--version"])
    except Exception:  # noqa: BLE001 - an injected runner may raise; treat as unknown
        return None
    if code != 0:
        return None
    m = _VERSION_RE.search(out or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _version_steps(ctx: Context) -> List[Step]:
    version = claude_version(ctx)
    need = "%d.%d.%d" % MIN_CLAUDE
    if version is None:
        return [Step("hook", "warn", "", "could not read `claude --version`; the hook needs "
                     "Claude Code %s or later" % need)]
    if version < MIN_CLAUDE:
        return [Step("hook", "warn", "", "Claude Code %d.%d.%d is older than %s, which exec-form "
                     "hooks need; update Claude Code" % (version + (need,)))]
    return []


def _plan_hook(ctx: Context) -> List[Step]:
    data = _read_settings(ctx.settings_path)
    new, changed = edit_settings_install(data, _desired_handler(ctx), ctx.settings_path)
    steps: List[Step] = []
    if changed:
        what = "add the Ghosted group to " + " and ".join(changed)
        if ctx.settings_path.exists():
            what += " (backup in %s)" % tilde(ctx.backups_dir, ctx.home)
        steps.append(Step("hook", "update", str(ctx.settings_path), what, changed=True))
    else:
        steps.append(Step("hook", "exists", str(ctx.settings_path),
                          "Stop and StopFailure already registered"))
    if not os.path.exists(ctx.hook_python):
        steps.append(Step("hook", "warn", "", "hook python %s does not exist" % ctx.hook_python))
    return steps + _version_steps(ctx)


def _apply_hook(ctx: Context) -> List[Step]:
    real = Path(os.path.realpath(str(ctx.settings_path)))
    data = _read_settings(ctx.settings_path)
    new, changed = edit_settings_install(data, _desired_handler(ctx), ctx.settings_path)
    steps: List[Step] = []
    if changed:
        backup = _backup(ctx, real)
        _write_settings(real, new)
        detail = "added the Ghosted group to " + " and ".join(changed)
        if backup is not None:
            detail += " (backup: %s)" % tilde(backup, ctx.home)
        steps.append(Step("hook", "update", str(ctx.settings_path), detail, changed=True))
    else:
        steps.append(Step("hook", "exists", str(ctx.settings_path),
                          "Stop and StopFailure already registered"))
    if not os.path.exists(ctx.hook_python):
        steps.append(Step("hook", "warn", "", "hook python %s does not exist" % ctx.hook_python))
    return steps + _version_steps(ctx)


def _uninstall_hook(ctx: Context, dry_run: bool) -> List[Step]:
    real = Path(os.path.realpath(str(ctx.settings_path)))
    data = _read_settings(ctx.settings_path)
    new, changed = edit_settings_uninstall(data, ctx.settings_path)
    if not changed:
        return [Step("hook", "absent", str(ctx.settings_path), "no Ghosted group registered")]
    detail = "remove the Ghosted group from " + " and ".join(changed)
    if not dry_run:
        backup = _backup(ctx, real)
        _write_settings(real, new)
        detail = "removed the Ghosted group from " + " and ".join(changed)
        if backup is not None:
            detail += " (backup: %s)" % tilde(backup, ctx.home)
    return [Step("hook", "update", str(ctx.settings_path), detail, changed=True)]


# ---- launchd -------------------------------------------------------------------------------------

def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_plist(ctx: Context, template: Optional[str] = None) -> str:
    """The LaunchAgent plist for this context, validated with plistlib."""
    if template is None:
        try:
            template = TEMPLATE.read_text(encoding="utf-8")
        except OSError as exc:
            raise InstallError("can't read the plist template: %s" % (exc.strerror or exc)) from None
    extra = ""
    if not ctx.le_home_is_default:
        extra = ("\n\t\t<key>GHOSTED_HOME</key>\n\t\t<string>%s</string>"
                 % _xml(str(ctx.le_home)))
    values = {
        "{{LABEL}}": LABEL,
        "{{PYTHON}}": _xml(ctx.launchd_python),
        "{{LE}}": _xml(str(ctx.repo / "bin" / "ghosted")),
        "{{LOG}}": _xml(str(ctx.log_path)),
        "{{PATH}}": _xml(LAUNCHD_PATH),
        "{{HOME}}": _xml(str(ctx.home)),
        "{{EXTRA_ENV}}": extra,
    }
    text = template
    for key, value in values.items():
        text = text.replace(key, value)
    if "{{" in text:
        raise InstallError("the plist template has an unknown placeholder")
    check_plist(text, ctx)
    return text


def expected_plist(ctx: Context) -> Dict[str, Any]:
    env = {"PATH": LAUNCHD_PATH, "HOME": str(ctx.home), SCHEDULED_ENV: "1"}
    if not ctx.le_home_is_default:
        env["GHOSTED_HOME"] = str(ctx.le_home)
    return {
        "Label": LABEL,
        "ProgramArguments": [ctx.launchd_python, str(ctx.repo / "bin" / "ghosted"), "run"],
        "StartCalendarInterval": [{"Hour": h, "Minute": RUN_MINUTE} for h in RUN_HOURS],
        "StandardOutPath": str(ctx.log_path),
        "StandardErrorPath": str(ctx.log_path),
        "EnvironmentVariables": env,
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def check_plist(text: str, ctx: Context) -> Dict[str, Any]:
    """Parse with plistlib and compare with what the job must be; raises InstallError."""
    try:
        data = plistlib.loads(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 - plistlib raises several types for bad XML
        raise InstallError("the rendered plist does not parse") from None
    if data != expected_plist(ctx):
        raise InstallError("the rendered plist does not match the expected job")
    return data


def launchd_loaded(ctx: Context) -> Optional[bool]:
    """True/False from `launchctl print`, None when launchctl can't be run."""
    try:
        code, _, _ = ctx.runner(["launchctl", "print", "%s/%s" % (ctx.domain, LABEL)])
    except Exception:  # noqa: BLE001
        return None
    if code in (126, 127):
        return None
    return code == 0


def fixture_gate(ctx: Context) -> Optional[str]:
    """Spec rule 7: the hand-labelled real-session negatives must be labelled (and the suite run
    on them) before the hourly job goes on. Applies only on the machine that holds the two
    positive fixtures' transcripts; anywhere else there is nothing to check. Counts only."""
    if ctx.projects_dir is None or ctx.manifest is None:
        return None
    try:
        data = json.loads(ctx.manifest.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        fixtures = [fx for fx in data.get("fixtures", []) if isinstance(fx, dict)]
        wanted = int(data.get("negatives_wanted") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None

    def verdict(fx: Mapping[str, Any]) -> Any:
        expect = fx.get("expect")
        return expect.get("verdict") if isinstance(expect, dict) else None

    positives = [fx for fx in fixtures if verdict(fx) in ("usage_limit", "open_question")]
    try:
        here = bool(positives) and all(
            (ctx.projects_dir / str(fx.get("folder")) / (str(fx.get("uuid")) + ".jsonl")).is_file()
            for fx in positives)
    except (OSError, ValueError):
        here = False
    if not here:
        return None
    labelled = sum(1 for fx in fixtures if verdict(fx) == "clean" and fx.get("labelled_by") not in (None, "", "pending"))
    if labelled >= wanted:
        return None
    return ("%d of the %d hand-labelled clean sessions are labelled in %s; "
            "label them, then run GHOSTED_REQUIRE_REAL=1 python3 -m unittest tests.test_real_fixtures "
            "before the hourly job goes on (--force installs it anyway)" % (labelled, wanted, ctx.manifest))


def _launchd_refusal(ctx: Context) -> Optional[Step]:
    """A Step refusing the launchd install, or None when it may go ahead."""
    try:
        from core import config as config_mod

        problem = config_mod.config_minitoml_problem(ctx.le_home)
    except Exception:  # noqa: BLE001 - never block on the check itself
        problem = None
    if problem:
        return Step("launchd", "refuse", str(ctx.plist_path),
                    "the job's python (%s) cannot read config.toml, so every hourly run would stop: %s"
                    % (ctx.launchd_python, problem), ok=False)
    gate = fixture_gate(ctx)
    if gate and not ctx.force:
        return Step("launchd", "refuse", str(ctx.plist_path), gate, ok=False)
    return None


def _plan_launchd(ctx: Context) -> List[Step]:
    refusal = _launchd_refusal(ctx)
    if refusal is not None:
        return [refusal]
    text = render_plist(ctx)
    path = ctx.plist_path
    try:
        same = path.read_bytes() == text.encode("utf-8")
    except OSError:
        same = False
    steps: List[Step] = []
    if same:
        steps.append(Step("launchd", "exists", str(path), "plist up to date"))
    else:
        verb = "replace" if path.exists() else "create"
        steps.append(Step("launchd", verb, str(path), "runs `ghosted run` at :02 every hour (it works "
                          "07:00-23:59 in the configured timezone)", changed=True))
    steps.append(Step("launchd", "load", "", "%s/%s via launchctl bootout + bootstrap (skipped "
                      "when the plist is unchanged and the job is loaded)" % (ctx.domain, LABEL),
                      changed=True))
    return steps


def _apply_launchd(ctx: Context) -> List[Step]:
    refusal = _launchd_refusal(ctx)
    if refusal is not None:
        return [refusal]
    text = render_plist(ctx)
    path = ctx.plist_path
    try:
        same = path.read_bytes() == text.encode("utf-8")
    except OSError:
        same = False
    steps: List[Step] = []
    _ensure_private_dir(ctx.le_home)
    _ensure_private_dir(ctx.log_path.parent)
    if not same:
        verb = "replace" if path.exists() else "create"
        if not path.parent.is_dir():
            os.makedirs(str(path.parent), mode=0o755, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            try:
                os.fchmod(fd, 0o644)
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, str(path))
        except BaseException:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            raise
        steps.append(Step("launchd", verb, str(path), "runs `ghosted run` at :02 every hour (it works "
                          "07:00-23:59 in the configured timezone)", changed=True))
    else:
        steps.append(Step("launchd", "exists", str(path), "plist up to date"))
    if same and launchd_loaded(ctx):
        steps.append(Step("launchd", "exists", "", "%s/%s is loaded" % (ctx.domain, LABEL)))
        return steps
    ctx.runner(["launchctl", "bootout", "%s/%s" % (ctx.domain, LABEL)])  # may fail: not loaded
    code, _, err = ctx.runner(["launchctl", "bootstrap", ctx.domain, str(path)])
    if code != 0:
        steps.append(Step("launchd", "error", "", "launchctl bootstrap failed (%d): %s"
                          % (code, _first_line(err)), ok=False))
    else:
        steps.append(Step("launchd", "load", "", "bootstrapped %s/%s" % (ctx.domain, LABEL),
                          changed=True))
    return steps


def _uninstall_launchd(ctx: Context, dry_run: bool) -> List[Step]:
    path = ctx.plist_path
    steps: List[Step] = []
    if dry_run:
        steps.append(Step("launchd", "unload", "", "launchctl bootout %s/%s" % (ctx.domain, LABEL),
                          changed=True))
        if path.exists():
            steps.append(Step("launchd", "remove", str(path), "", changed=True))
        return steps
    code, _, _ = ctx.runner(["launchctl", "bootout", "%s/%s" % (ctx.domain, LABEL)])
    if code == 0:
        steps.append(Step("launchd", "unload", "", "booted out %s/%s" % (ctx.domain, LABEL),
                          changed=True))
    else:
        steps.append(Step("launchd", "unload", "", "not loaded"))
    if os.path.lexists(str(path)):
        os.unlink(str(path))
        steps.append(Step("launchd", "remove", str(path), "", changed=True))
    else:
        steps.append(Step("launchd", "absent", str(path), "nothing to remove"))
    return steps


# ---- helpers -----------------------------------------------------------------------------------

def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()[:1]
    return line[0][:200] if line else "no message"


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        where = os.path.basename(str(exc.filename)) if exc.filename else ""
        return "%s%s" % (exc.strerror or "error", (" (%s)" % where) if where else "")
    return str(exc)


_PLANNERS = {"bin": _plan_bin, "skill": _plan_skill, "hook": _plan_hook, "launchd": _plan_launchd}
_APPLIERS = {"bin": _apply_bin, "skill": _apply_skill, "hook": _apply_hook,
             "launchd": _apply_launchd}
_REMOVERS = {"bin": _uninstall_bin, "skill": _uninstall_skill, "hook": _uninstall_hook,
             "launchd": _uninstall_launchd}
