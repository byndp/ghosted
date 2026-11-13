"""The `ghosted` command line (contract section 10).

Exit codes: 0 ok, 1 not found / failed, 2 usage error or refusal. Output is short plain text on
stdout; errors go to stderr as "ghosted <command>: ...".

`ghosted hook` is dispatched before argparse or any other module loads (it runs on every Claude Code
turn end), and every other import is lazy, inside the command that needs it. Modules written by
other parts of the build are reached the same way: core.runner (run, sweep), core.detector
(project_for) and the notify package (get_adapter).

Test affordance: GHOSTED_NOW (an ISO instant) stands in for the clock, so subprocess tests can
pin dates. Nothing else reads it.
"""
from __future__ import annotations

import os
import sys

PUBLIC_HINT = ('--public is required: a neutral title for the phone, with no names, amounts, '
               'emails or customer details, e.g. --public "home: pay the overdue water bill"')
REWRITE_HINT = "rewrite it without names, amounts, emails or customer details"
TEST_TITLE = "Ghosted"
TEST_BODY = "Test push: Ghosted can reach this device."


def main(argv=None, environ=None) -> int:
    """Run one `ghosted` command; returns the exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["hook"]:
        return _hook()
    try:
        code = _main(args, os.environ if environ is None else environ)
        sys.stdout.flush()  # a closed pipe (`ghosted list | head -1`) surfaces here, not at exit
        return code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 1


def _hook() -> int:
    """`ghosted hook`: never raises, never prints (core.hook), always 0."""
    try:
        from core import hook

        return hook.main(hook.read_stdin())
    except BaseException:  # noqa: BLE001 - a hook must never fail a Claude session
        return 0


# ---- plumbing -------------------------------------------------------------------------------------

class CliError(Exception):
    """A failure to report in one line: code 1 (failed / not found) or 2 (usage or refusal)."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Ctx:
    """Lazily loaded config, ledger and clock for one invocation."""

    def __init__(self, environ) -> None:
        self.env = environ
        self._cfg = None
        self._ledger = None
        self._now = None

    @property
    def cfg(self):
        if self._cfg is None:
            from core import config

            self._cfg = config.load_config(environ=self.env)
        return self._cfg

    @property
    def ledger(self):
        if self._ledger is None:
            from core.ledger import Ledger

            self._ledger = Ledger(self.cfg)
        return self._ledger

    @property
    def now(self):
        if self._now is None:
            from core import timeutil

            pinned = self.env.get("GHOSTED_NOW")
            if pinned:
                try:
                    self._now = timeutil.parse_iso(pinned)
                except ValueError:
                    raise CliError("GHOSTED_NOW is not an ISO time", 2) from None
            else:
                self._now = timeutil.now_utc()
        return self._now

    @property
    def tz(self) -> str:
        return self.cfg.timezone


def _say(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _get(obj, name, default=None):
    """Field access for results that may be dicts, dataclasses or named tuples."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _count(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return len(value)
    except TypeError:
        return 0


def _tilde(path, env) -> str:
    text = str(path)
    home = (env.get("HOME") or "").rstrip("/")
    if home and text == home:
        return "~"
    if home and text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    _err(str(code))
    return 1


# ---- the parser -----------------------------------------------------------------------------------

def _parser():
    import argparse

    from core import __version__

    parser = argparse.ArgumentParser(
        prog="ghosted",
        description="Ghosted: make sure you didn't ghost a Claude Code session. It lists the sessions "
                    "still waiting on you, with the command to resume each one.",
        epilog="Start with: ghosted report. The commands for the optional phone pushes are in "
               "docs/guide.md.")
    parser.add_argument("-V", "--version", action="version", version="ghosted " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name, func, help_text, hidden=False, **kw):
        # hidden: it still runs and has its own --help, but `ghosted --help` does not list it
        # (the push plumbing: docs/guide.md lists every command).
        if not hidden:
            kw["help"] = help_text
        p = sub.add_parser(name, description=help_text, **kw)
        p.set_defaults(func=func, parser=p)
        return p

    p = command("report", _cmd_report, "what your sessions left waiting on you, with the command to "
                                       "pick each one back up (Ghosted sends nothing anywhere)")
    p.add_argument("window", nargs="?", metavar="WINDOW",
                   help="today, Nd (1d..3650d) or all (everything on disk); default 7d (the first "
                        "report ever: 30d)")
    shape = p.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true", help="machine-readable; no quotes unless --quotes")
    shape.add_argument("--markdown", action="store_true",
                       help="the report as Markdown, for a chat that renders it (the skill uses this)")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--quotes", action="store_true", help="put the quoted questions in --json too")
    which.add_argument("--no-quotes", action="store_true", help="leave the quoted questions out")
    p.add_argument("--soft", action="store_true", help="also list the soft ones (polite offers)")
    p.add_argument("--plain", action="store_true", help="plain words instead of git jargon")
    p.add_argument("--project", metavar="NAME", help="only this project")
    p.add_argument("--no-scan", action="store_true", help="use what the last scan saved")
    p.add_argument("--no-unsaved", action="store_true", help="leave out the Unsaved work section")
    p.add_argument("--debug", action="store_true", help="say which repos were skipped and why")
    p.add_argument("--via", choices=("cli", "skill"), default="cli", help=argparse.SUPPRESS)
    p = command("receipt", _cmd_receipt, "your numbers and nothing else, safe to post: sessions checked, "
                                         "how many you ghosted, work never saved")
    p.add_argument("window", nargs="?", metavar="WINDOW",
                   help="today, Nd (1d..3650d) or all; default all (everything on disk)")

    p = command("add", _cmd_add, "register a dated promise or a decision waiting on someone")
    p.add_argument("local_title", help="the full title; it stays on your computer")
    p.add_argument("--public", help="the neutral title the phone shows (required)")
    p.add_argument("--due", metavar="WHEN", help="MM-DD, YYYY-MM-DD, today, tomorrow, +Nd or mon..sun, "
                                                 "optionally with HH:MM")
    p.add_argument("--review", metavar="WHEN", help="when to bring it up again if nothing is due")
    p.add_argument("--owner", metavar="me|claude", default="me",
                   help="whose move it is: yours (the default) or Claude's")
    p.add_argument("--kind", choices=("promise", "decision"),
                   help="default: promise with --due, else decision")
    p.add_argument("--project", metavar="NAME", help="default: from CLAUDE_PROJECT_DIR or the cwd")
    p.add_argument("--allow-automated", action="store_true",
                   help="register it even from an automated (claude -p) run")

    p = command("done", _cmd_done, "mark an item done")
    p.add_argument("id", help="GS-0012, ghosted-12 or 12")
    p = command("snooze", _cmd_snooze, "snooze an item until 9:00 on a later day")
    p.add_argument("id")
    p.add_argument("until", type=lambda s: s.strip().lower(), choices=("1d", "3d", "mon"))
    p = command("dismiss", _cmd_dismiss, "dismiss an item (the number ghosted report shows works too)")
    p.add_argument("id")
    p.add_argument("--reason", default="not_real", type=lambda s: s.strip().lower().replace("-", "_"),
                   choices=("not_real", "handled_elsewhere"), help="default: not_real")

    p = command("mixed", _cmd_mixed, "split a repo's uncommitted files by the Claude session that "
                                     "changed them (an Unsaved work row marked 'from N sessions')")
    p.add_argument("id", help="the Unsaved work row's number")
    p.add_argument("--session", metavar="ID", help="a session id (or its first characters), ambiguous or none")
    p.add_argument("--files", action="store_true",
                   help="print that owner's files, relative to the repo, one per line")
    p.add_argument("-0", dest="null", action="store_true", help="with --files: end each path with NUL")

    p = command("list", _cmd_list, "list the open reminders (local titles)")
    p.add_argument("--all", action="store_true", help="include done and dismissed items")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = command("sweep", _cmd_sweep, "scan now (no pushes), rewrite and print digest.md "
                                     "(v1 view; ghosted report is the v2 one)", hidden=True)
    p.add_argument("--no-scan", action="store_true", help="print the digest without scanning")
    command("run", _cmd_run, "the hourly job: scan, digest, then notify or sync", hidden=True)
    command("sync", _cmd_sync, "remote adapter: send queued events and runs now", hidden=True)
    command("resync", _cmd_resync, "remote adapter: send again the items the server lost "
                                   "(after a D1 restore or a new database)", hidden=True)
    p = command("test-push", _cmd_test_push, "send a test push now", hidden=True)
    p.add_argument("--message", metavar="TEXT")

    p = command("doctor", _cmd_doctor, "check config, paths, hook, skill, the hourly job and the push channel")
    p.add_argument("--fix", action="store_true",
                   help="queue the create event again for any item that has none (crash recovery)")

    p = command("init", _cmd_init, "set up Ghosted: on a terminal with no options, a short "
                                   "guided setup; with options, write config.toml if it is missing")
    p.add_argument("--adapter", choices=("none", "ntfy", "pushover", "remote", "macos", "claude-app"))
    p.add_argument("--timezone", metavar="TZ", help="IANA name, e.g. America/Chicago")
    p.add_argument("--workspace-root", metavar="DIR", action="append",
                   help="a folder whose subfolders are projects (repeatable)")
    p.add_argument("--remote-url", metavar="URL")
    p.add_argument("--yes", action="store_true",
                   help="no questions: write config.toml with defaults if it is missing (v1 behaviour)")

    for name, func, text in (("install", _cmd_install, "install the bin link, skill, hook or launchd job"),
                             ("uninstall", _cmd_uninstall, "remove what install added (only ours)")):
        p = command(name, func, text)
        for comp in ("bin", "skill", "hook", "launchd", "all"):
            p.add_argument("--" + comp, action="store_true")
        if name == "uninstall":
            p.add_argument("--claude-md", dest="claude_md", action="store_true",
                           help="remove the session rules ghosted init added to ~/.claude/CLAUDE.md")
        p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
        p.add_argument("--force", action="store_true", help="replace a different file or link")

    p = command("pair", _cmd_pair, "remote, device mode: a one-time pairing code for the phone", hidden=True)
    p.add_argument("--revoke-all", action="store_true", help="revoke every paired device")
    command("hook", _cmd_hook_help, "internal: the Stop / StopFailure hook (JSON on stdin)", hidden=True)
    command("version", _cmd_version, "print the version")
    return parser


def _main(args, environ) -> int:
    from core.config import ConfigError
    from core.ledger import LedgerBusy, LedgerError, UnknownItem
    from core.sanitize import Refused

    parser = _parser()
    try:
        ns = parser.parse_args(args)
    except SystemExit as exc:
        return _exit_code(exc)
    if not getattr(ns, "func", None):
        parser.print_help(sys.stderr)
        return 2
    ctx = Ctx(environ)
    where = "ghosted " + ns.command
    try:
        return ns.func(ns, ctx)
    except SystemExit as exc:  # parser.error() inside a command
        return _exit_code(exc)
    except CliError as exc:
        _err("%s: %s" % (where, exc))
        return exc.code
    except ConfigError as exc:
        _err("%s: %s" % (where, exc))
        return 1
    except LedgerBusy as exc:
        _err("%s: %s; try again in a moment" % (where, exc))
        return 1
    except Refused as exc:  # the ledger's own sanitizer gate (the CLI checks first)
        _err("%s: refused: %s\n  %s" % (where, exc, REWRITE_HINT))
        return 2
    except (LedgerError, UnknownItem) as exc:
        _err("%s: %s" % (where, exc))
        return 1
    except ImportError as exc:
        _err("%s: a module is missing (%s)" % (where, getattr(exc, "name", None) or exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - one short line, unless GHOSTED_DEBUG asks for more
        if environ.get("GHOSTED_DEBUG"):
            raise
        _err("%s: %s: %s (GHOSTED_DEBUG=1 shows a traceback)" % (where, type(exc).__name__, exc))
        return 1


# ---- add ------------------------------------------------------------------------------------------

def _refusal(where: str, problems) -> str:
    return "refused: %s %s\n  %s" % (where, "; ".join(problems), REWRITE_HINT)


def _default_project(ctx) -> str:
    """The project of CLAUDE_PROJECT_DIR, else of the cwd (core.detector's rule)."""
    base = ctx.env.get("CLAUDE_PROJECT_DIR")
    if not base:
        try:
            base = os.getcwd()
        except OSError:
            return "unknown"
    from core import hook

    return hook.cwd_project(os.path.abspath(base), ctx.cfg) or "unknown"


def _parse_when(text, flag, ctx):
    if text is None:
        return None
    from core import timeutil

    try:
        return timeutil.parse_due(text, ctx.now, ctx.tz)
    except ValueError as exc:
        raise CliError("%s: %s" % (flag, exc), 2) from None


def _when_suffix(item) -> str:
    from core import timeutil

    parts = []
    if item.get("due_at"):
        parts.append("due " + timeutil.human_due(item["due_at"]))
    if item.get("review_at"):
        parts.append("review " + timeutil.human_date(timeutil.parse_date(item["review_at"])))
    return " (%s)" % "; ".join(parts) if parts else ""


AUTOMATED_HINT = ("this looks like an automated run (CLAUDE_CODE_ENTRYPOINT=%s): automated runs do "
                  "not register loose ends, since a daily job would add the same item every day. "
                  "If a person is in this conversation, run it again with --allow-automated")


def _automated_entrypoint(ctx):
    """The CLAUDE_CODE_ENTRYPOINT value when it is one the detector skips (config skip.entrypoints,
    default sdk-cli: claude -p and SDK runs), else None."""
    value = (ctx.env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip()
    if not value:
        return None
    try:
        skip = set(getattr(ctx.cfg, "skip_entrypoints", None) or ())
    except Exception:  # noqa: BLE001 - a broken config is reported by the command itself
        skip = {"sdk-cli"}
    return value if value in skip else None


def _first_pull(ctx) -> None:
    """Remote adapter, a ledger that has never synced: learn the server's ids before assigning
    one (core.runner.ensure_server_view; at most a few seconds, never fatal)."""
    if getattr(ctx.cfg, "adapter", "none") != "remote":
        return
    try:
        if ctx.ledger.has_server_view():
            return
        import notify

        from core import config, runner

        adapter = notify.get_adapter(ctx.cfg, config.load_env(ctx.cfg))
        from notify import remote

        runner.ensure_server_view(ctx.cfg, ctx.ledger, adapter, ctx.now, timeout=remote.PULL_TIMEOUT)
    except Exception:  # noqa: BLE001 - offline or misconfigured: local ids, and the server's
        return  # "conflict" answer renumbers a reused one later


def _add_item(ns, ctx, kind, verb) -> int:
    from core import sanitize, timeutil

    if ns.public is None:
        ns.parser.error(PUBLIC_HINT)
    if not ns.local_title or not ns.local_title.strip():
        raise CliError("the local title is blank", 2)
    automated = _automated_entrypoint(ctx)
    if automated and not getattr(ns, "allow_automated", False):
        raise CliError(AUTOMATED_HINT % automated, 2)
    found = sanitize.problems(ns.public)
    if found:
        raise CliError(_refusal("--public", found), 2)
    project = ns.project if ns.project is not None else _default_project(ctx)
    found = sanitize.project_problems(project)
    if found:
        where = "--project" if ns.project is not None else \
            "the project name (from the current directory; pass --project NAME)"
        raise CliError(_refusal(where, found), 2)
    due = _parse_when(getattr(ns, "due", None), "--due", ctx)
    review = _parse_when(getattr(ns, "review", None), "--review", ctx)
    if review is not None and timeutil.is_timed(review):
        raise CliError("--review takes a day, not a time (e.g. --review mon)", 2)
    if kind is None:
        kind = "promise" if due else "decision"
    from core import ledger as ledger_mod

    owner = getattr(ns, "owner", None) or "me"
    if owner not in ledger_mod.OWNERS:
        raise CliError("--owner is me or claude", 2)
    _first_pull(ctx)
    item, event = ctx.ledger.add(
        kind=kind, project=project, local_title=ns.local_title, title=ns.public,
        owner=owner, due_at=due, review_at=review, now=ctx.now)
    if event is None:
        _say("%s already open: %s%s" % (item["id"], item["title"], _when_suffix(item)))
        return 0
    _say("%s %s: %s%s" % (item["id"], verb, item["title"], _when_suffix(item)))
    return 0


def _cmd_add(ns, ctx) -> int:
    return _add_item(ns, ctx, ns.kind, "added")


# ---- status changes -------------------------------------------------------------------------------

def _item_id(text) -> str:
    from core import ledger

    try:
        return ledger.normalize_id(text)
    except ledger.LedgerError:
        raise CliError("%r is not an item id (use GS-0012 or 12)" % (text,), 2) from None


def _set_status(ctx, item_id, status, **kw):
    from core.ledger import UnknownItem

    target, asked = ctx.ledger.resolve_id(item_id)
    if asked:
        _say("%s is now %s (the server already held its id for another item)" % (asked, target))
    try:
        return ctx.ledger.set_status(target, status, now=ctx.now, closed_by="session", **kw)
    except UnknownItem:
        raise CliError("%s not found" % item_id, 1) from None


_UNSAVED_WORDS = {"uncommitted": "uncommitted", "unpushed": "unpushed", "unmerged": "branches not merged"}


def _unsaved_still(ctx, item) -> None:
    """`ghosted done` on an unsaved-work item (contract 16.9): refused while the repo still holds
    unsaved work. Counts only, never names."""
    from core import unsaved

    if item.get("kind") != "unsaved" or item.get("status") == "done":
        return
    n = item["id"].split("-")[-1].lstrip("0") or "0"
    facts = unsaved.repo_facts(item.get("repo") or "", ctx.now)
    shown = list(((item.get("unsaved") or {}).get("flags") or {}).keys())
    left = unsaved.remaining(facts, shown, ctx.cfg, ctx.now)
    if left is None:
        raise CliError("%s: git could not check that repo just now; try again (or ghosted dismiss %s)"
                       % (item["id"], n), 1)
    if left:
        parts = []
        for rule, value in left.items():
            parts.append("no remote" if rule == "no_remote" else "%d %s" % (value, _UNSAVED_WORDS[rule]))
        raise CliError("%s still %s (save it first, or ghosted dismiss %s)" % (item["id"], " · ".join(parts), n), 1)


def _cmd_done(ns, ctx) -> int:
    item_id = _item_id(ns.id)
    target, _ = ctx.ledger.resolve_id(item_id)
    current = ctx.ledger.get(target)
    if current is not None:
        _unsaved_still(ctx, current)
    item, event = _set_status(ctx, item_id, "done")
    item_id = item.get("id") or item_id
    _say("%s %s: %s" % (item_id, "done" if event else "is already done", item.get("title", "")))
    return 0


def _pick_owner(text, candidates):
    from core import touches

    want = text.strip()
    if want.lower() in (touches.AMBIGUOUS, touches.NONE):
        return want.lower()
    hits = sorted(u for u in candidates if u.lower().startswith(want.lower())) if want else []
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise CliError("%r matches %d sessions here; give more of the id" % (want, len(hits)), 2)
    raise CliError("no session that worked in this repo has an id starting with %r" % want, 2)


def _cmd_mixed(ns, ctx) -> int:
    """`ghosted mixed N` (contract 16.11): who changed a repo's uncommitted files, by timing. File names
    are printed only with --files, the one place they reach a terminal."""
    from core import detector, pull, touches, unsaved

    item_id = _item_id(ns.id)
    target, _ = ctx.ledger.resolve_id(item_id)
    item = ctx.ledger.get(target)
    if item is None or item.get("kind") != "unsaved" or not isinstance(item.get("repo"), str):
        raise CliError("%s is not an Unsaved work row (ghosted report marks those with !)" % item_id, 1)
    if ns.files and not ns.session:
        raise CliError("--files needs --session ID (or ambiguous / none): whose part?", 2)
    repo = item["repo"]
    n = ledger_id_number(target)
    entries = unsaved.dirty_entries(repo)
    if entries is None:
        raise CliError("%s: git could not read that repo just now; try again" % target, 1)
    window = int(getattr(ctx.cfg, "mixed_window_seconds", touches.DEFAULT_WINDOW))
    by = touches.update(ctx.cfg, ctx.now).touches(repo)
    owners = touches.owner_list([t for _, t in entries], by, window)
    counts = {}
    for owner in owners:
        if owner is not None:
            counts[owner] = counts.get(owner, 0) + 1
    sessions = {u: c for u, c in counts.items() if u not in (touches.AMBIGUOUS, touches.NONE)}
    guess = "a timing guess (within %d s of a session's work): check ambiguous and none yourself" % window

    if ns.session:
        want = _pick_owner(ns.session, set(by) | set(sessions))
        if ns.files:
            picked = [rel for (rel, _), owner in zip(entries, owners) if owner == want]
            for rel in picked:
                if ns.null:
                    sys.stdout.write(rel + "\0")
                else:
                    _say(rel)
            _err("%d %s from %s: %s" % (len(picked), "entry" if len(picked) == 1 else "entries",
                                         want[:8] if want not in (touches.AMBIGUOUS, touches.NONE) else want, guess))
            return 0
        sessions = {want: sessions.get(want, 0)} if want in sessions or want in by else {}

    report = pull.Report()
    report.cfg = ctx.cfg
    state_sessions = (ctx.ledger.load_state() or {}).get("sessions") or {}
    home = ctx.env.get("HOME")
    ranked = touches.ranked(sessions)
    width = max([len(str(c)) for c in counts.values()] + [1])
    _say("%s %s: %d uncommitted, from %d %s (split by timing, a guess)"
         % (target, item.get("project") or "", len(entries), len(ranked), "session" if len(ranked) == 1 else "sessions"))
    for uuid, c in ranked:
        cmd = detector.resume_command(pull.launch_cwd(ctx.ledger, report, uuid, state_sessions), uuid, home=home)
        _say("  %*d  %s  %s" % (width, c, uuid, cmd))
    if not ns.session:
        _say("  %*d  %s  (more than one session worked here within %d s)" % (width, counts.get(touches.AMBIGUOUS, 0),
                                                                            touches.AMBIGUOUS, window))
        _say("  %*d  %s  (no session worked here within %d s)" % (width, counts.get(touches.NONE, 0), touches.NONE, window))
    if ranked:
        _say("Files for one: ghosted mixed %s --session %s --files   (to stage them: ... --files -0 | xargs -0 git add --)"
             % (n, ranked[0][0][:8]))
    _say("It is %s." % guess)
    return 0


def ledger_id_number(item_id) -> str:
    from core import ledger

    num = ledger.id_number(item_id)
    return str(num) if num is not None else str(item_id)


def _cmd_snooze(ns, ctx) -> int:
    from core import timeutil

    item_id = _item_id(ns.id)
    until = timeutil.snooze_until(ns.until, ctx.now, ctx.tz)
    # A re-snooze to another time is one status change (rev + 1), as on the server; the same
    # time again is a no-op.
    item, event = _set_status(ctx, item_id, "snoozed", snooze_until=until)
    item_id = item.get("id") or item_id
    label = timeutil.human_due(until)
    _say("%s %s %s: %s" % (item_id, "snoozed until" if event else "is already snoozed until",
                            label, item.get("title", "")))
    return 0


def _cmd_dismiss(ns, ctx) -> int:
    item_id = _item_id(ns.id)
    # Dismissing again with the other reason corrects it (one status change, as on the server).
    item, event = _set_status(ctx, item_id, "dismissed", dismiss_reason=ns.reason)
    item_id = item.get("id") or item_id
    why = ns.reason.replace("_", " ")
    if event is None:
        _say("%s is already dismissed (%s): %s" % (item_id, why, item.get("title", "")))
    else:
        _say("%s dismissed (%s): %s" % (item_id, why, item.get("title", "")))
    return 0


# ---- list -----------------------------------------------------------------------------------------

def list_groups(items, now, tz, include_closed=False):
    """[(key, title, [items])] in phone order (core.digest.group), empty groups left out; with
    include_closed a final "closed" group holds done and dismissed items, latest first."""
    from core import digest

    groups = digest.group(items, now, tz)
    out = [(key, digest.GROUP_TITLES[key], groups[key]) for key in digest.GROUPS if groups.get(key)]
    if include_closed:
        closed = [it for it in items if it.get("status") in ("done", "dismissed")]
        closed.sort(key=lambda it: (it.get("closed_at") or "", it.get("id") or ""), reverse=True)
        if closed:
            out.append(("closed", "Closed", closed))
    return out


def _detail_line(item, group, now, tz) -> str:
    from core import timeutil

    bits = [item.get("project") or "?"]
    due = item.get("due_at")
    if due:
        label = "due " + timeutil.human_due(due)
        try:
            instant = timeutil.due_instant(due, tz)
        except (ValueError, TypeError):
            instant = None
        if instant is not None and instant < now and group != "closed":
            label += ", overdue"
        bits.append(label)
    if group == "snoozed" and item.get("snooze_until"):
        bits.append("until " + timeutil.human_due(item["snooze_until"]))
    elif item.get("review_at") and not due and group != "closed":
        bits.append("review " + timeutil.human_date(timeutil.parse_date(item["review_at"])))
    if group == "closed":
        closed = item.get("closed_at")
        when = ""
        if closed:
            when = " " + timeutil.human_date(timeutil.local_date(timeutil.parse_iso(closed), tz))
        reason = item.get("dismiss_reason")
        bits.append("%s%s%s" % (item.get("status"), " (%s)" % reason.replace("_", " ") if reason else "",
                                when))
    if item.get("owner") == "claude" and group != "later":
        bits.append("owner claude")
    if item.get("evidence"):
        bits.append(item["evidence"])
    if item.get("looks_resolved"):
        bits.append("looks resolved")
    if item.get("consequential"):
        bits.append("consequential")
    return " · ".join(bits)


def _cmd_list(ns, ctx) -> int:
    import json

    from core import timeutil

    items = list(ctx.ledger.items().values())
    groups = list_groups(items, ctx.now, ctx.tz, include_closed=ns.all)
    if ns.json:
        rows = [dict(item, group=key) for key, _, members in groups for item in members]
        _say(json.dumps({"now": timeutil.to_iso(ctx.now), "items": rows}, indent=2,
                        ensure_ascii=False))
        return 0
    if not groups:
        _say("No items yet." if ns.all else "Nothing open.")
        return 0
    for index, (key, title, members) in enumerate(groups):
        if index:
            _say()
        _say("%s (%d)" % (title, len(members)))
        for item in members:
            _say("  %s  %s" % (item.get("id"), item.get("local_title") or item.get("title") or ""))
            _say("           %s" % _detail_line(item, key, ctx.now, ctx.tz))
            if item.get("kind") == "interrupted" and item.get("resume") and key != "closed":
                _say("           %s" % item["resume"])
    return 0


# ---- sweep / run / sync / test-push / pair -----------------------------------------------------

def _cmd_sweep(ns, ctx) -> int:
    from core import runner

    text = runner.sweep(ctx.cfg, now=ctx.now, scan=not ns.no_scan)
    _say((text or "").rstrip("\n"))
    return 0


def _pushes(n: int) -> str:
    return "%d push%s" % (n, "" if n == 1 else "es")


def push_problem(push):
    """core.digest.push_problem (the Worker's push block as one line, or '')."""
    from core import digest

    return digest.push_problem(push)


def _sync_notes(result) -> list:
    """Extra parts of the run line for a SyncResult: refused events, renumbered and lost items,
    and a push channel that is not delivering."""
    from collections import Counter

    acks = _get(result, "acks") or {}
    counts = Counter(a.get("result") for a in acks.values() if isinstance(a, dict))
    parts = []
    if counts.get("invalid"):
        parts.append("%d invalid (ghosted sync shows why)" % counts["invalid"])
    renumbered = list(_get(result, "renumbered") or [])
    if renumbered:
        parts.append("%d renumbered (%s)" % (len(renumbered), ", ".join("%s -> %s" % pair for pair in renumbered[:3])))
    missing = list(_get(result, "missing") or [])
    if missing:
        parts.append("%d missing on the server (ghosted resync)" % len(missing))
    problem = push_problem(_get(result, "push"))
    if problem:
        parts.append(problem)
    return parts


def _notify_summary(result) -> str:
    """The notify part of the `ghosted run` line, for RunOutcome.notify_result: a SyncResult (remote),
    {"reopened", "sent", "error"} (a local adapter), or {"ok": False, "error"} (it raised)."""
    if result is None:
        return ""
    error = _get(result, "error")
    if isinstance(result, dict) and "sent" in result:  # local adapter: one row per push decided
        if error:
            return "push failed: %s" % error
        if result.get("unreachable"):
            text = "push skipped: %s; the next run tries again" % result["unreachable"]
            reopened = _count(result.get("reopened"))
            return text + (" · %d reopened" % reopened if reopened else "")
        rows = [r for r in (result.get("sent") or []) if isinstance(r, dict)]
        good = sum(1 for r in rows if r.get("ok") is True or r.get("ok") == 1)
        parts = []
        if good:
            parts.append("sent " + _pushes(good))
        if len(rows) - good:
            parts.append(_pushes(len(rows) - good) + " failed")
        reopened = _count(result.get("reopened"))
        if reopened:
            parts.append("%d reopened" % reopened)
        return " · ".join(parts) or "nothing to push"
    ok = _get(result, "ok", True)
    if hasattr(result, "acks") or _get(result, "acks") is not None:
        if not ok:
            return "sync failed: %s" % (error or "unknown error")
        acked = _count(_get(result, "acks"))
        text = "synced %d event%s" % (acked, "" if acked == 1 else "s")
        stale = _count(_get(result, "stale"))
        if stale:
            text += " (%d stale)" % stale
        notes = _sync_notes(result)
        return " · ".join([text] + notes)
    if not ok:
        return "notify failed: %s" % (error or "unknown error")
    return ""


def run_summary(outcome):
    """('run ok · 3 scanned · 1 new · synced 4 events', exit code) for a RunOutcome."""
    if _get(outcome, "skipped"):
        return "run skipped: another scan is running", 0
    run = _get(outcome, "run") or {}
    scan = _get(outcome, "scan")
    ok = bool(_get(run, "ok", scan is not None))
    head = "run ok" if ok else "run failed"
    if not ok and _get(run, "error"):
        head += ": %s" % str(_get(run, "error"))[:200]
    parts = [head]
    scanned = _get(scan, "sessions_scanned", _get(run, "sessions_scanned"))
    added = _get(scan, "items_added", _get(run, "items_added"))
    parts.append("%d scanned" % _count(scanned))
    parts.append("%d new" % _count(added))
    updated = _count(_get(scan, "items_updated"))
    if updated:
        parts.append("%d updated" % updated)
    again = _count(_get(scan, "items_reopened"))
    if again:
        parts.append("%d re-flagged" % again)
    deferred = _count(_get(scan, "deferred"))
    if deferred:
        parts.append("%d deferred" % deferred)
    note = _notify_summary(_get(outcome, "notify_result"))
    if note:
        parts.append(note)
    watchdog = _get(outcome, "watchdog")
    if watchdog:  # origin and status only: the ping URL itself is a credential
        parts.append("watchdog pinged" if _get(watchdog, "ok") else
                     "watchdog ping failed: %s" % (_get(watchdog, "error") or "error"))
    return " · ".join(parts), (0 if ok else 1)


SCHEDULED_ENV = "GHOSTED_SCHEDULED"  # set by the launchd job, which fires every hour
RUN_HOURS = range(7, 24)  # local hours (cfg.timezone) in which the scheduled run works


def _record_config_failure(ctx, exc) -> None:
    """A config.toml the launchd interpreter cannot read: a failed run record in GHOSTED_HOME, so
    `ghosted doctor`'s last-run row shows why the hourly job stopped (resolve_home needs no config)."""
    try:
        import uuid as uuid_mod

        from core import config, ledger, timeutil

        stamp = timeutil.to_iso(ctx.now)
        home = config.resolve_home(None, ctx.env)
        if not home.is_dir():
            return
        message = " ".join(str(exc).split())[:160]
        ledger.append_jsonl(home / "runs.jsonl", {
            "run_id": str(uuid_mod.uuid4()), "started_at": stamp, "finished_at": stamp, "ok": False,
            "sessions_scanned": 0, "items_added": 0, "error": "ConfigError: " + message})
    except Exception:  # noqa: BLE001 - the error is printed anyway
        pass


def _cmd_run(ns, ctx) -> int:
    from core import runner
    from core.config import ConfigError

    try:
        cfg = ctx.cfg
    except ConfigError as exc:
        _record_config_failure(ctx, exc)
        raise
    if ctx.env.get(SCHEDULED_ENV) == "1":
        # launchd fires every hour on the system clock; the work hours are those of the
        # configured zone (a trip abroad must not move them or wake the dead-man at night).
        from core import timeutil

        hour = timeutil.local_dt(ctx.now, cfg.timezone).hour
        if hour not in RUN_HOURS:
            _say("run skipped: %02d:xx in %s is outside 07:00-23:59" % (hour, cfg.timezone))
            return 0
    line, code = run_summary(runner.run(cfg, now=ctx.now, notify=True, scan=True))
    _say(line)
    return code


def _adapter(ctx, need=None, what=""):
    cfg = ctx.cfg
    if need is not None and cfg.adapter != need:
        raise CliError("%s needs the %s adapter; config.toml has adapter = %s"
                       % (what, need, cfg.adapter), 1)
    import notify

    from core import config

    adapter = notify.get_adapter(cfg, config.load_env(cfg))
    if adapter is None:
        raise CliError("no notify adapter is set: set adapter under [notify] in config.toml "
                       "(ntfy, pushover, macos or remote), or run ghosted init", 1)
    return adapter


def _require_ok(result, what) -> None:
    if not _get(result, "ok", False):
        raise CliError("%s failed: %s" % (what, _get(result, "error") or "unknown error"), 1)


def _cmd_sync(ns, ctx) -> int:
    """Prints the acks, then each stale rejection ((item_id, server status) in SyncResult.stale)
    and each invalid event ((event_id, item_id, reason) in SyncResult.invalid)."""
    from collections import Counter

    result = _adapter(ctx, need="remote", what="ghosted sync").sync(ctx.ledger, ctx.now)
    _require_ok(result, "sync")
    acks = _get(result, "acks") or {}
    counts = Counter(a.get("result") for a in acks.values() if isinstance(a, dict))
    detail = ", ".join("%d %s" % (counts[k], k) for k in ("applied", "duplicate", "stale", "invalid")
                       if counts.get(k))
    _say("synced: %d event%s acked%s" % (len(acks), "" if len(acks) == 1 else "s",
                                         " (%s)" % detail if detail else ""))
    for item_id, status in _get(result, "stale") or ():
        _say("  %s stale: changed elsewhere%s; this Mac now has the server's version"
             % (item_id, " (now %s)" % status if status else ""))
    for event_id, item_id, reason in _get(result, "invalid") or ():
        _say("  event %s%s rejected: %s" % (str(event_id)[:8], " (%s)" % item_id if item_id else "",
                                            reason or "invalid"))
    for old, new in _get(result, "renumbered") or ():
        _say("  %s renumbered to %s: the server already held %s for another item" % (old, new, old))
    missing = list(_get(result, "missing") or [])
    if missing:
        _say("  %d item%s the server no longer has (%s): ghosted resync sends %s again"
             % (len(missing), "" if len(missing) == 1 else "s", _ids_text(missing),
                "it" if len(missing) == 1 else "them"))
    problem = push_problem(_get(result, "push"))
    if problem:
        _say("  " + problem)
    merged = _count(_get(result, "merged"))
    if merged:
        _say("  %d item%s updated from the server" % (merged, "" if merged == 1 else "s"))
    return 0


def _cmd_resync(ns, ctx) -> int:
    """After a D1 restore (or a new database): items this Mac synced that the server lost are
    queued again (creation + status) and sent with want.resync, so the Worker stores them even
    though it has seen their ids before."""
    adapter = _adapter(ctx, need="remote", what="ghosted resync")
    first = adapter.sync(ctx.ledger, ctx.now)
    _require_ok(first, "sync")
    missing = list(_get(first, "missing") or [])
    if not missing:
        _say("resync: the server has every item this Mac synced")
        return 0
    events = ctx.ledger.reemit_events(missing, ctx.now, force=True)
    result = adapter.sync(ctx.ledger, ctx.now, resync=True)
    _require_ok(result, "resync")
    still = list(_get(result, "missing") or [])
    _say("resync: queued %d event%s for %s; %s" % (
        len(events), "" if len(events) == 1 else "s", _ids_text(missing),
        "the server has them now" if not still else "%d still missing: %s" % (len(still), _ids_text(still))))
    return 0 if not still else 1


def _cmd_test_push(ns, ctx) -> int:
    """Not recorded, not budgeted (contract section 12)."""
    adapter = _adapter(ctx)
    body = ns.message or TEST_BODY
    if ctx.cfg.adapter == "remote":
        result = adapter.sync(ctx.ledger, ctx.now, test_push={"title": TEST_TITLE, "body": body})
        report = _get(result, "test_push")
        if report is None:
            _require_ok(result, "test push")
            raise CliError("the Worker did not report the test push", 1)
        # The Worker answered the test push; a sync problem with other events (e.g. some left
        # unacknowledged) does not make the push itself a failure.
        sent, failed = _count(_get(report, "sent")), _count(_get(report, "failed"))
        _say("test push: sent to %d subscription%s, %d failed" % (sent, "" if sent == 1 else "s", failed))
        if not sent and not failed:
            _say("  no phone has notifications on yet: open the app and tap Enable notifications")
        if not _get(result, "ok", True) and _get(result, "error"):
            _say("  note: the sync also reported: %s" % _get(result, "error"))
        return 0 if sent and not failed else 1
    sent = adapter.send([], title=TEST_TITLE, body=body, kind="test")  # a SendResult
    if not _get(sent, "ok", False):
        detail = _get(sent, "error")
        raise CliError("test push failed%s" % (": %s" % detail if detail else ""), 1)
    _say("test push sent via %s" % ctx.cfg.adapter)
    return 0


def _cmd_pair(ns, ctx) -> int:
    from core import timeutil

    adapter = _adapter(ctx, need="remote", what="ghosted pair")
    if ns.revoke_all:
        result = adapter.sync(ctx.ledger, ctx.now, revoke_devices=True)
        _require_ok(result, "revoke")
        _say("revoked every paired device; pair again with ghosted pair")
        return 0
    result = adapter.sync(ctx.ledger, ctx.now, pair={"ttl_minutes": 10})
    _require_ok(result, "pair")
    pair = _get(result, "pair") or {}
    code = _get(pair, "code")
    if not code:
        raise CliError("the Worker returned no pairing code (pairing needs AUTH_MODE device)", 1)
    expires = _get(pair, "expires_at")
    when = ""
    if expires:
        try:
            when = ", expires %s" % timeutil.human_time(timeutil.parse_iso(expires), ctx.tz)
        except ValueError:
            when = ""
    _say("pairing code: %s (single use%s)" % (code, when))
    _say("enter it in the Ghosted app on the phone")
    return 0


# ---- report ---------------------------------------------------------------------------------------

def _live_agents(ctx):
    """`claude agents --json --all` (core.agents.live), with `claude` looked up on this
    invocation's PATH. None (no live signal) on any failure."""
    import shutil

    from core import agents

    path = ctx.env.get("PATH")
    return agents.live(which=lambda name: shutil.which(name, path=path) if path else None)


def _cmd_report(ns, ctx) -> int:
    """`ghosted report [WINDOW]` (contract 16.3)."""
    from core import pull

    if ns.window is not None:
        try:
            pull.parse_window(ns.window)
        except ValueError as exc:
            raise CliError(str(exc), 2) from None
    if ns.project is not None and not ns.project.strip():
        raise CliError("--project needs a name", 2)
    rep = pull.gather(ctx.cfg, ctx.ledger, ctx.now, window=ns.window, scan=not ns.no_scan,
                      project=ns.project, via=ns.via, live=_live_agents(ctx), unsaved=not ns.no_unsaved)
    if ns.json:
        _say(pull.render_json(rep, quotes=ns.quotes).rstrip("\n"))
    else:
        text = pull.render_text(rep, quotes=not ns.no_quotes, soft=ns.soft, plain=ns.plain,
                                debug=ns.debug).rstrip("\n")
        tty = bool(getattr(sys.stdout, "isatty", None) and sys.stdout.isatty())
        if ns.markdown:
            _say(pull.markdown(text).rstrip("\n"))
        else:
            _say(pull.colorize(text) if pull.use_color(ctx.env, tty) else text)
    return 0


def _cmd_receipt(ns, ctx) -> int:
    """`ghosted receipt [WINDOW]` (contract 16.20): the counts of one report, made to be posted.
    Never counts as a report: the first real report still looks back 30 days."""
    from core import pull

    window = ns.window if ns.window is not None else pull.ALL_WINDOW
    try:
        pull.parse_window(window)
    except ValueError as exc:
        raise CliError(str(exc), 2) from None
    rep = pull.gather(ctx.cfg, ctx.ledger, ctx.now, window=window, live=_live_agents(ctx), record=False)
    _say(pull.render_receipt(rep).rstrip("\n"))
    return 0


# ---- init -----------------------------------------------------------------------------------------

def _toml_str(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_config(timezone="", adapter="none", workspace_roots=(), remote_url="", topic="",
                  env_file="~/.ghosted/.env", direct_questions="waiting", ping_url="") -> str:
    """config.toml text with every key and its default spelled out. ``env_file`` is GHOSTED_HOME's
    .env (ghosted init passes it), so a non-default GHOSTED_HOME keeps its secrets beside it."""
    roots = ", ".join(_toml_str(r) for r in workspace_roots)
    return "\n".join([
        "# Ghosted settings (written by ghosted init). Every key is optional; deleting one restores",
        "# its default. See docs/contract.md section 2.",
        "",
        'timezone = %s                       # IANA name; "" = the system zone' % _toml_str(timezone),
        '# projects_dir = "~/.claude/projects"   # default: Claude Code\'s folder (CLAUDE_CONFIG_DIR/projects if set)',
        "workspace_roots = [%s]              # project = first folder under a root" % roots,
        "scan_since_days = 14",
        "maybe_active_minutes = 30",
        "review_days = 7",
        "env_file = %s     # secrets only, chmod 600" % _toml_str(env_file),
        "",
        "[skip]",
        'entrypoints = ["sdk-cli"]',
        "first_prompt_patterns = []",
        "path_patterns = []",
        "temp_folders = true                 # skip sessions launched from a temp folder (/tmp, $TMPDIR)",
        "",
        "[detect]",
        "ignore_tail_patterns = ['(?i)anything else\\?\\s*$', "
        "'(?i)does (that|this) (help|make sense)\\?\\s*$']",
        "direct_questions = %s           # waiting | soft: where a direct question to you is listed" % _toml_str(direct_questions),
        "handoff_names = []                     # e.g. [\"Alex\"]: also catch \"Alex has not ruled\" endings (soft)",
        "",
        "[notify]",
        "adapter = %s                          # none | claude-app | ntfy | pushover | macos | remote" % _toml_str(adapter),
        "",
        "[notify.claude-app]",
        'claude_path = ""                       # "" = find claude on PATH and the usual places',
        'fallback = "ntfy"                      # ntfy | none: where a push the Claude app could not confirm goes',
        'model = "haiku"',
        "",
        "[notify.ntfy]",
        'server = "https://ntfy.sh"',
        "topic = %s" % _toml_str(topic),
        "",
        "[notify.remote]",
        "url = %s" % _toml_str(remote_url),
        "",
        "[push]",
        "digest_hour = 9",
        "quiet_start = 22",
        "quiet_end = 8",
        "daily_budget = 3",
        "",
        "[unsaved]                              # ghosted report's Unsaved work section",
        "stale_days = 3                         # uncommitted changes older than this",
        "unpushed_days = 1                      # commits on no remote older than this",
        "unmerged_days = 14                     # branches not merged into the default branch",
        "no_remote_ok = []                      # repos that have no remote on purpose",
        "",
        "[mixed]                                # two sessions' changes in one repo",
        "window_seconds = 15                    # a file belongs to a session that worked there this close to its save",
        "",
        "[watchdog]",
        "ping_url = %s                         # https:// only; the hourly job GETs it after each ok run" % _toml_str(ping_url),
        "",
    ])


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):  # closed or replaced stdin
        return False


def _cmd_init(ns, ctx) -> int:
    """No options on a terminal: the guided setup (core.onboard, contract 16.5). Any option,
    --yes, or stdin that is not a terminal (scripts, Claude Code's Bash tool): the v1 one-shot."""
    given = any(v is not None for v in (ns.adapter, ns.timezone, ns.workspace_root, ns.remote_url))
    if not given and not ns.yes and _stdin_is_tty():
        from core import onboard

        return onboard.run(ctx, onboard.ConsoleIO())
    return _init_v1(ns, ctx)


def _init_v1(ns, ctx) -> int:
    import secrets

    from core import config, ledger, timeutil

    home = config.resolve_home(None, ctx.env)
    path = home / "config.toml"
    given = any(v is not None for v in (ns.adapter, ns.timezone, ns.workspace_root, ns.remote_url))
    if path.exists():
        _say("%s already exists; left unchanged%s" % (_tilde(path, ctx.env),
                                                      " (edit it to change settings)" if given else ""))
        problem = config.config_minitoml_problem(home)
        if problem:
            _say("warning: /usr/bin/python3 (the hourly job and the hook) cannot read it: %s" % problem)
        return 0
    tz = (ns.timezone or "").strip()
    if tz:
        canonical = timeutil.canonical_timezone(tz)
        if canonical is None:
            raise CliError("--timezone %r is not an IANA zone (e.g. America/Chicago)" % tz, 2)
        tz = canonical
    remote_url = (ns.remote_url or "").strip()
    if remote_url and not remote_url.startswith("https://"):
        raise CliError("--remote-url must start with https://", 2)
    roots = [r.strip() for r in (ns.workspace_root or []) if r.strip()]
    adapter = ns.adapter or ("remote" if remote_url else "none")
    topic = "ghosted-" + secrets.token_urlsafe(24)
    # env_file names GHOSTED_HOME's own .env: the default config value (~/.ghosted/.env) would point
    # a non-default GHOSTED_HOME at another folder's secrets.
    text = render_config(tz, adapter, roots, remote_url, topic, env_file=_tilde(home / ".env", ctx.env))
    try:
        config.build_config(config.parse_config_text(text), home, ctx.env)
    except config.ConfigError as exc:
        raise CliError("those settings don't work: %s" % exc, 2) from None
    ledger.ensure_dir(home)
    ledger.atomic_write_text(path, text)
    _say("wrote %s" % _tilde(path, ctx.env))
    # The topic works like a password (anyone who has it can read the pushes), and this path runs
    # where output is captured (Claude Code's Bash tool, scripts, logs): it is never printed, only
    # where to find it in config.toml (0600) and its first characters (onboard.mask_topic's form).
    where = "%s under [notify.ntfy] (it starts %s…; it works like a password, so it is not printed)" % (
        _tilde(path, ctx.env), topic[:5])
    if adapter == "ntfy":
        _say("pushes go to ntfy on https://ntfy.sh. Your topic is in %s: subscribe to it in the "
             "ntfy app, then run ghosted test-push" % where)
    elif adapter == "none":
        _say("pushes are off (adapter none). For phone pushes, run ghosted init in a terminal (guided), "
             'or subscribe in the ntfy app to the topic in %s and set adapter = "ntfy" under [notify]' % where)
    elif adapter == "pushover":
        _say("next: put PUSHOVER_TOKEN and PUSHOVER_USER in %s (chmod 600)" % _tilde(home / ".env", ctx.env))
    else:
        _say("next: put GHOSTED_SERVICE_TOKEN_ID and GHOSTED_SERVICE_TOKEN_SECRET in %s (chmod 600), "
             "then run ghosted sync" % _tilde(home / ".env", ctx.env))
    _say("then: ghosted install --all")
    return 0


# ---- install / uninstall --------------------------------------------------------------------------

def _components(ns):
    names = [c for c in ("bin", "skill", "hook", "launchd") if getattr(ns, c)]
    if ns.all:
        names.append("all")
    if not names and not getattr(ns, "claude_md", False):
        extra = ", --claude-md" if hasattr(ns, "claude_md") else ""
        raise CliError("pick what to change: --bin, --skill, --hook, --launchd%s or --all" % extra, 2)
    return names


def _install(ns, ctx, remove) -> int:
    from core import claudemd, install

    names = _components(ns)
    note = None
    if not remove:
        if not (install.REPO_ROOT / "bin" / "ghosted").is_file():
            # A packaged run (uvx, pip) has no launcher to link, and its path can vanish with a cache.
            raise CliError("this needs a clone of the repo: this copy runs from a package "
                           "(uvx or pip), made for trying the report. See Install in the README.", 1)
        if not install.has_launchctl(ctx.env):
            if "launchd" in names:
                raise CliError("--launchd needs launchctl: the hourly job is macOS only for now", 1)
            if "all" in names:
                names = [c for c in install.COMPONENTS if c != "launchd"]
                note = "launchd  skipped: no launchctl here (the hourly job is macOS only for now)"
    ictx = install.make_context(cfg=ctx.cfg, environ=ctx.env, force=ns.force, now=ctx.now)
    if remove:
        steps = install.uninstall(ictx, names, dry_run=ns.dry_run) if names else []
        if getattr(ns, "claude_md", False) or ns.all:
            try:
                steps.append(claudemd.remove(ictx, dry_run=ns.dry_run))
            except OSError as exc:
                steps.append(install.Step(claudemd.COMPONENT, "error", "", type(exc).__name__, ok=False))
    elif ns.dry_run:
        steps = install.plan(ictx, names)
    else:
        steps = install.apply(ictx, names)
    for step in steps:
        _say(step.line(ictx.home, dry_run=ns.dry_run))
    if note:
        _say(note)
    if ns.dry_run:
        _say("dry run: nothing was changed")
    return 0 if all(step.ok for step in steps) else 1


def _cmd_install(ns, ctx) -> int:
    return _install(ns, ctx, remove=False)


def _cmd_uninstall(ns, ctx) -> int:
    return _install(ns, ctx, remove=True)


# ---- doctor ---------------------------------------------------------------------------------------

def _mode(path) -> str:
    try:
        return "%04o" % (os.stat(str(path)).st_mode & 0o7777)
    except OSError:
        return "?"


def _python_version(ictx, python) -> str:
    if sys.platform == "darwin" and python == "/usr/bin/python3":
        # /usr/bin/python3 is a shim: without the Command Line Tools it opens an install dialog.
        code, _, _ = ictx.runner(["xcode-select", "-p"])
        if code != 0:
            return "needs the Xcode Command Line Tools (xcode-select --install)"
    if not os.path.exists(python):
        return "missing"
    code, out, _ = ictx.runner([python, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
    return out.strip() if code == 0 and out.strip() else "can't run"


def _before_311(version: str) -> bool:
    """True for a version string such as '3.9.6' older than 3.11 (False when unreadable)."""
    parts = version.split(".")
    try:
        return (int(parts[0]), int(parts[1])) < (3, 11)
    except (IndexError, ValueError):
        return False


def _cmd_doctor(ns, ctx) -> int:
    from core import config, install, timeutil
    from core.ledger import Ledger, LedgerError

    rows = []

    def row(status, label, detail):
        rows.append((status, label, detail))

    env = ctx.env
    home = config.resolve_home(None, env)
    try:
        cfg = config.load_config(environ=env)
    except config.ConfigError as exc:
        cfg = None
        row("FAIL", "config", str(exc))
    if cfg is not None:
        exists = cfg.config_path.exists()
        row("ok" if exists else "warn", "config",
            _tilde(cfg.config_path, env) + ("" if exists else " missing: defaults apply (ghosted init writes it)"))
        roots = ", ".join(_tilde(r, env) for r in cfg.workspace_roots) or "none"
        row("", "", "timezone %s · adapter %s · review %d d · since %d d · maybe-active %d min"
            % (cfg.timezone, cfg.adapter, cfg.review_days, cfg.scan_since_days, cfg.maybe_active_minutes))
        row("", "", "digest %d:00 · quiet %d-%d · budget %d/day · workspace roots %s"
            % (cfg.digest_hour, cfg.quiet_start, cfg.quiet_end, cfg.daily_budget, roots))
        row("", "", "ntfy %s, topic %s · remote url %s"
            % (cfg.ntfy_server, "set" if cfg.ntfy_topic else "missing", cfg.remote_url or "none"))
        system_tz = config.system_timezone(env)
        if system_tz != cfg.timezone and not _same_offsets(system_tz, cfg.timezone, ctx.now):
            row("warn", "timezone", "this computer is on %s but config.toml says %s: the hourly job "
                "only works 07:00-23:59 %s, and every push follows %s" % (system_tz, cfg.timezone,
                                                                          cfg.timezone, cfg.timezone))
        projects = cfg.projects_dir
        via = " (from GHOSTED_PROJECTS_DIR)" if config.projects_dir_override(env) else ""
        if projects.is_dir():
            try:
                count = sum(1 for p in projects.iterdir() if p.is_dir())
            except OSError:
                count = -1
            row("ok", "projects", "%s (%s project folders)%s" % (_tilde(projects, env),
                                                                 count if count >= 0 else "?", via))
        else:
            row("warn", "projects", "%s does not exist%s" % (_tilde(projects, env), via))
        # secrets: set / missing only
        need = {"remote": ("GHOSTED_SERVICE_TOKEN_ID", "GHOSTED_SERVICE_TOKEN_SECRET"),
                "pushover": ("PUSHOVER_TOKEN", "PUSHOVER_USER")}.get(cfg.adapter, ())
        try:
            secrets_found = config.load_env(cfg)
        except config.ConfigError as exc:
            secrets_found = {}
            row("FAIL", "secrets", str(exc))
        env_path = cfg.env_file
        states = ", ".join("%s %s" % (k, "set" if k in secrets_found else "missing")
                           for k in config.ENV_KEYS)
        missing = [k for k in need if k not in secrets_found]
        if env_path.exists():
            loose = (os.stat(str(env_path)).st_mode & 0o077) != 0
            status = "FAIL" if missing else ("warn" if loose else "ok")
            row(status, "secrets", "%s (%s%s): %s" % (_tilde(env_path, env), _mode(env_path),
                                                     ", chmod 600 it" if loose else "", states))
        else:
            row("FAIL" if missing else "ok", "secrets",
                "%s missing%s" % (_tilde(env_path, env),
                                  " (needed: %s)" % ", ".join(missing) if missing else " (not needed)"))
    if home.is_dir():
        mode = _mode(home)
        row("ok" if mode == "0700" else "warn", "GHOSTED_HOME",
            "%s (%s%s)" % (_tilde(home, env), mode, "" if mode == "0700" else ", should be 0700"))
    else:
        row("warn", "GHOSTED_HOME", "%s not created yet" % _tilde(home, env))

    ictx = install.make_context(cfg=cfg, environ=env, now=ctx.now)
    try:
        facts = install.status(ictx)
    except Exception as exc:  # noqa: BLE001 - doctor reports, it doesn't crash
        facts = None
        row("FAIL", "install", "%s: %s" % (type(exc).__name__, exc))
    if facts is not None:
        bin_dir = _tilde(ictx.bin_link.parent, env)
        row("ok" if facts["bin"]["on_path"] else "warn", "PATH",
            "%s is %son PATH" % (bin_dir, "" if facts["bin"]["on_path"] else "not "))
        if facts["bin"].get("shadowed_by"):
            row("warn", "ghosted", "another program named ghosted (%s) comes first on PATH: run %s "
                "instead" % (_tilde(facts["bin"]["shadowed_by"], env), _tilde(ictx.bin_link, env)))
        link_text = {"ours": "-> this repo", "missing": "not installed (ghosted install --bin)",
                     "other-link": "links somewhere else", "not-a-link": "a different file is there"}
        for comp in ("bin", "skill"):
            state = facts[comp]["state"]
            row("ok" if state == "ours" else "warn", comp,
                "%s %s" % (_tilde(facts[comp]["path"], env),
                           link_text[state].replace("--bin", "--" + comp)))
        hook = facts["hook"]
        if hook["state"] == "unreadable":
            row("FAIL", "hook", hook.get("error", "settings.json can't be read"))
        elif hook["state"] == "missing":
            row("warn", "hook", "not registered (ghosted install --hook)")
        else:
            events = " and ".join(e for e, on in hook["events"].items() if on)
            detail = "%s registered, python %s" % (events, hook.get("python"))
            if not hook["matches_repo"]:
                detail += "; points elsewhere (ghosted install --hook refreshes it)"
            row("ok" if hook["state"] == "registered" and hook["matches_repo"] else "warn", "hook", detail)
        ld = facts["launchd"]
        if not ld["plist"]:
            row("warn", "launchd", "not installed (ghosted install --launchd)")
        else:
            loaded = {True: "loaded", False: "NOT loaded", None: "load state unknown"}[ld["loaded"]]
            good = ld["matches"] and ld["loaded"] is True
            row("ok" if good else "warn", "launchd", "%s %s, %s" % (
                _tilde(ld["path"], env), "current" if ld["matches"] else "differs from this repo's",
                loaded))
        pythons = []
        old_readers = []
        for label, python in (("hook", hook.get("python") or ictx.hook_python),
                              ("launchd", ictx.launchd_python)):
            version = _python_version(ictx, python)
            pythons.append("%s %s %s" % (label, python, version))
            if _before_311(version):
                old_readers.append("%s %s" % (label, version))
        pythons.append("this ghosted %s" % sys.version.split()[0])
        row("ok", "python", " · ".join(pythons))
        if old_readers and cfg is not None:
            # Those interpreters read config.toml with core.minitoml (no tomllib before 3.11).
            problem = config.config_minitoml_problem(cfg.home)
            if problem:
                row("FAIL", "config", "the %s python cannot read config.toml (so every hourly run "
                    "stops): %s" % (" and ".join(old_readers), problem))

    if cfg is not None:
        if cfg.adapter == "none":
            row("ok", "adapter", "none (pushes off)")
        else:
            try:
                import notify

                adapter = notify.get_adapter(cfg, config.load_env(cfg))
                ok, detail = adapter.check()  # (ok, message); messages carry no secrets
                row("ok" if ok else "FAIL", "adapter", detail or cfg.adapter)
            except Exception as exc:  # noqa: BLE001
                row("FAIL", "adapter", "%s: %s: %s" % (cfg.adapter, type(exc).__name__, exc))
        try:
            led = Ledger(cfg)
            runs = led.runs()
            if not runs:
                row("warn", "last run", "none yet (ghosted run, or ghosted install --launchd)")
            else:
                last = runs[-1]
                when = last.get("finished_at") or last.get("started_at")
                label = "?"
                if when:
                    instant = timeutil.parse_iso(when)
                    label = "%s, %s" % (timeutil.human_date(timeutil.local_date(instant, cfg.timezone)),
                                        timeutil.human_time(instant, cfg.timezone))
                if last.get("ok"):
                    row("ok", "last run", "%s: ok, %d scanned" % (label, _count(last.get("sessions_scanned"))))
                else:
                    row("warn", "last run", "%s: failed: %s" % (label, str(last.get("error") or "?")[:200]))
            if cfg.adapter == "remote":
                unsynced = sum(1 for it in led.items().values() if not it.get("synced"))
                events, unacked_runs = len(led.unacked_events()), len(led.unacked_runs())
                row("ok" if not (unsynced or events) else "warn", "unsynced",
                    "%d item%s, %d event%s, %d run%s waiting for ghosted sync" % (
                        unsynced, "" if unsynced == 1 else "s", events, "" if events == 1 else "s",
                        unacked_runs, "" if unacked_runs == 1 else "s"))
            _doctor_inbox(row, led, cfg, ctx, ns.fix)
            _doctor_drift(row, led)
        except (LedgerError, ValueError) as exc:
            row("FAIL", "ledger", str(exc))
        row("ok", "watchdog", _watchdog_text(cfg))

    width = max(len(label) for _, label, _ in rows) if rows else 8
    for status, label, detail in rows:
        _say("%-4s  %-*s  %s" % (status, width, label, detail))
    return 1 if any(status == "FAIL" for status, _, _ in rows) else 0


def _doctor_drift(row, led) -> None:
    """The schema-drift row (contract 16.6): warn with the problem, else ok."""
    from core import detector

    drift = led.load_state().get("drift")
    problem = detector.drift_problem(drift)
    if problem:
        row("warn", "drift", "Claude Code's session format may have changed: %s" % problem)
    elif isinstance(drift, dict) and drift.get("files"):
        row("ok", "drift", "the last scan read %d session file%s as expected"
            % (_count(drift.get("files")), "" if _count(drift.get("files")) == 1 else "s"))
    else:
        row("ok", "drift", "no scan has read a session file yet")


def _watchdog_text(cfg) -> str:
    """`set (<host>)` or `off`: the ping URL itself is a credential and is never printed."""
    url = (getattr(cfg, "watchdog_ping_url", "") or "").strip()
    if not url:
        return "off"
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname or "?"
    except ValueError:
        host = "?"
    return "set (%s)" % host


def _same_offsets(a, b, now) -> bool:
    try:
        from notify import remote

        return remote.same_zone(a, b, now)
    except Exception:  # noqa: BLE001
        return a == b


def _ids_text(ids, limit=8) -> str:
    ids = list(ids)
    return ", ".join(ids[:limit]) + (", ..." if len(ids) > limit else "")


def _doctor_inbox(row, led, cfg, ctx, fix) -> None:
    """Items whose create event never reached inbox.jsonl (a crash between saving the item and
    writing its event): they would never reach the server. --fix queues the events again."""
    missing = led.items_missing_events()
    if not missing:
        if led.items():
            row("ok", "inbox", "every item has its create event")
        return
    many = len(missing) != 1
    if not fix:
        row("FAIL" if cfg.adapter == "remote" else "warn", "inbox",
            "%d item%s ha%s no create event (a crash before it was written): %s; "
            "ghosted doctor --fix queues %s" % (len(missing), "s" if many else "", "ve" if many else "s",
                                           _ids_text(missing), "them" if many else "it"))
        return
    events = led.reemit_events(missing, ctx.now)
    row("ok", "inbox", "queued %d event%s for %s%s" % (
        len(events), "" if len(events) == 1 else "s", _ids_text(missing),
        "; ghosted sync sends them" if cfg.adapter == "remote" else ""))


# ---- small commands -------------------------------------------------------------------------------

def _cmd_hook_help(ns, ctx) -> int:
    return 0  # unreachable from the command line: main() dispatches "hook" first


def _cmd_version(ns, ctx) -> int:
    from core import __version__

    _say("ghosted " + __version__)
    return 0
