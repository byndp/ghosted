"""The `ghosted` command line (contract section 10).

Subprocess tests run the real launcher (bin/ghosted) on the interpreter running the suite, with a temp
HOME and GHOSTED_HOME, GHOSTED_NOW pinned to Mon 2026-10-05 10:00 Chicago, and PATH set to a
directory holding only fake `launchctl`, `claude` and `xcode-select` scripts that log their
arguments: the real launchctl can't be reached. Commands that call modules other parts of the
build own (core.runner, core.detector, notify) are tested in-process with fakes.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

import core
from core import cli, install
from core.config import Config, load_config
from core.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
LE = ROOT / "bin" / "ghosted"
NOW = "2026-10-05T15:00:00Z"  # Mon Oct 5, 10:00 in Chicago
SESSION = "0a0a0a0a-1111-4222-8333-444455556666"
SECRET = "SECRET-VALUE-7f3a9"

FAKE_TOOL = """#!/bin/sh
printf '%%s\\n' "${0##*/} $*" >> "%(log)s"
case "${0##*/} $1" in
  "claude --version") printf '2.1.290 (Claude Code)\\n' ;;
  "launchctl print") exit %(print_rc)d ;;
esac
exit 0
"""

SETTINGS = {
    "env": {"API_KEY": SECRET},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard --token " + SECRET,
                                                       "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "~/bin/bridge stop", "async": True, "timeout": 5}]}],
    },
    "model": "opus",
}


def snapshot(*roots):
    """Every path under the roots with its content. Apple's /usr/bin/python3 keeps its bytecode
    under $HOME/Library/Caches/com.apple.python (sys.pycache_prefix): that is the interpreter's,
    not ours, so it is left out."""
    out = {}
    for root in roots:
        cache_dirs = {os.path.join(str(root), "Library"), os.path.join(str(root), "Library", "Caches")}
        for dirpath, dirnames, filenames in os.walk(str(root)):
            if dirpath == os.path.join(str(root), "Library", "Caches"):
                dirnames[:] = [d for d in dirnames if d != "com.apple.python"]
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                if path in cache_dirs:
                    continue
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    out[path] = ("link", os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    out[path] = ("dir",)
                else:
                    with open(path, "rb") as fh:
                        out[path] = ("file", stat.S_IMODE(info.st_mode), fh.read())
    return out


class CliCase(unittest.TestCase):
    print_rc = 0

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.le_home = self.root / "ghosted-home"
        self.ws = self.root / "ws"
        (self.ws / "shop").mkdir(parents=True)
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        self.log = self.root / "system-calls.log"
        for tool in ("launchctl", "claude", "xcode-select"):
            path = self.fakebin / tool
            path.write_text(FAKE_TOOL % {"log": self.log, "print_rc": self.print_rc})
            os.chmod(str(path), 0o755)
        self.write_config()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, extra: str = "", home: Path = None) -> None:
        home = home or self.le_home
        home.mkdir(mode=0o700, exist_ok=True)
        (home / "config.toml").write_text(
            'timezone = "America/Chicago"\nprojects_dir = %s\nworkspace_roots = [%s]\n%s'
            % (json.dumps(str(self.root / "projects")), json.dumps(str(self.ws)), extra))

    def env(self, **extra):
        env = {"HOME": str(self.home), "GHOSTED_HOME": str(self.le_home), "PATH": str(self.fakebin),
               "GHOSTED_NOW": NOW, "CLAUDE_PROJECT_DIR": str(self.ws / "shop")}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def ghosted(self, *args, env=None):
        return subprocess.run([sys.executable, str(LE)] + list(args), capture_output=True,
                              encoding="utf-8", env=env or self.env(), cwd=str(self.root), timeout=120)

    def ok(self, *args, env=None):
        proc = self.ghosted(*args, env=env)
        self.assertEqual(proc.returncode, 0, "ghosted %s failed: %s%s" % (" ".join(args), proc.stdout, proc.stderr))
        return proc.stdout

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def items(self):
        return json.loads((self.le_home / "items.json").read_text())["items"]

    def inbox(self):
        path = self.le_home / "inbox.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def ledger(self) -> Ledger:
        return Ledger(load_config(environ=self.env()))


# ---- subprocess tests --------------------------------------------------------------------------

class BasicsTests(CliCase):
    def test_the_launcher_is_executable_through_its_shebang(self) -> None:
        self.assertTrue(os.access(str(LE), os.X_OK), "bin/ghosted lost its executable bit (chmod 755 bin/ghosted)")
        self.assertEqual(LE.read_text(encoding="utf-8").split("\n", 1)[0], "#!/usr/bin/env python3")
        env = self.env(PATH="%s:/usr/bin:/bin" % os.path.dirname(sys.executable))
        proc = subprocess.run([str(LE), "version"], capture_output=True, encoding="utf-8", env=env,
                              cwd=str(self.root), timeout=60)
        self.assertEqual((proc.returncode, proc.stdout), (0, "ghosted %s\n" % core.__version__))

    def test_the_launcher_finds_its_repo_from_any_cwd_and_through_a_symlink(self) -> None:
        elsewhere = self.root / "somewhere" / "else"
        elsewhere.mkdir(parents=True)
        link = self.root / "plugin-bin" / "ghosted"
        link.parent.mkdir()
        os.symlink(str(LE), str(link))
        chain = self.root / "plugin-bin" / "le2"
        os.symlink(str(link), str(chain))  # a link to a link
        for target in (LE, link, chain):
            proc = subprocess.run([sys.executable, str(target), "version"], capture_output=True,
                                  encoding="utf-8", env=self.env(), cwd=str(elsewhere), timeout=60)
            self.assertEqual((proc.returncode, proc.stdout), (0, "ghosted %s\n" % core.__version__), str(target))

    def test_version_and_help(self) -> None:
        self.assertEqual(self.ok("version"), "ghosted %s\n" % core.__version__)
        self.assertEqual(self.ok("--version"), "ghosted %s\n" % core.__version__)
        text = self.ok("--help")
        listed = [line.split()[0] for line in text.splitlines() if line.startswith("    ") and line[4] != " "]
        self.assertEqual(listed, ["report", "receipt", "add", "done", "snooze", "dismiss", "mixed", "list",
                                  "doctor", "init", "install", "uninstall", "version"])
        # The push plumbing is not listed, and still runs (its own --help).
        for name in ("sweep", "run", "sync", "resync", "test-push", "pair"):
            self.assertIn("usage: ghosted %s" % name, self.ok(name, "--help"))
        # Commands that no longer exist: not listed, and argparse refuses them (an unknown choice).
        for name in ("mark", "miss", "candidates", "minutes", "pilot"):
            self.assertNotIn(name, listed)
            proc = self.ghosted(name)
            self.assertEqual(proc.returncode, 2, name)
            self.assertIn("invalid choice: '%s'" % name, proc.stderr)
        self.assertEqual(self.ghosted("report", "--days", "7").returncode, 2)
        for flag in ("--seed", "--consequential"):
            self.assertEqual(self.ghosted("add", "x", "--public", "shop: a thing", flag).returncode, 2, flag)
        self.assertEqual(self.ok("hook", "--help"), "")  # the hook never prints (contract 11)
        self.assertNotIn("Mac", text)
        self.assertEqual(self.ghosted().returncode, 2)
        self.assertEqual(self.ghosted("frobnicate").returncode, 2)

    def test_errors_are_one_line_and_never_echo_file_contents(self) -> None:
        self.le_home.mkdir(exist_ok=True)
        (self.le_home / "items.json").write_text('{"items": {"GS-0001": {"local_title": "%s"' % SECRET)
        proc = self.ghosted("list")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("items.json is damaged", proc.stderr)
        self.assertNotIn(SECRET, proc.stderr + proc.stdout)
        (self.le_home / "config.toml").write_text('timezone = "%s\n' % SECRET)
        proc = self.ghosted("list")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("config.toml", proc.stderr)
        self.assertNotIn(SECRET, proc.stderr)
        self.write_config()
        (self.le_home / "items.json").unlink()
        proc = self.ghosted("list", env=self.env(GHOSTED_NOW="tuesday"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("GHOSTED_NOW", proc.stderr)


class AddTests(CliCase):
    def test_add_prints_id_title_and_due(self) -> None:
        out = self.ok("add", "Check the readout script cap and end date", "--public",
                      "metronome: readout script cap/end date", "--due", "10-11", "--project", "metronome")
        self.assertEqual(out, "GS-0001 added: metronome: readout script cap/end date (due Sun Oct 11)\n")
        item = self.items()["GS-0001"]
        self.assertEqual((item["kind"], item["project"], item["owner"]), ("promise", "metronome", "me"))
        self.assertEqual(item["local_title"], "Check the readout script cap and end date")
        self.assertEqual((item["due_at"], item["review_at"]), ("2026-10-11", None))
        self.assertFalse(item["consequential"])
        (event,) = self.inbox()
        self.assertEqual(event["type"], "item.add")
        self.assertNotIn("local_title", event["item"])
        self.assertNotIn("Check the readout", json.dumps(event))

    def test_owner_me_or_claude(self) -> None:
        self.ok("add", "a", "--public", "shop: first", "--owner", "me", "--project", "shop")
        self.ok("add", "b", "--public", "shop: second", "--owner", "claude", "--project", "shop")
        self.assertEqual([it["owner"] for it in self.items().values()], ["me", "claude"])
        bad = self.ghosted("add", "d", "--public", "shop: fourth", "--owner", "sam", "--project", "shop")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("me or claude", bad.stderr)

    def test_add_defaults_kind_and_project(self) -> None:
        out = self.ok("add", "Decide on the new logo", "--public", "shop: decide on the logo")
        self.assertEqual(out, "GS-0001 added: shop: decide on the logo (review Mon Oct 12)\n")
        item = self.items()["GS-0001"]
        self.assertEqual((item["kind"], item["project"], item["review_at"]), ("decision", "shop", "2026-10-12"))
        out = self.ok("add", "Ask the bank about the wire", "--public", "home: ask about the transfer",
                      "--due", "tomorrow 14:00", "--owner", "claude", "--kind", "promise", "--project", "home",
                      "--review", "fri")
        self.assertEqual(out, "GS-0002 added: home: ask about the transfer (due Tue Oct 6, 2:00 PM; "
                              "review Fri Oct 9)\n")
        item = self.items()["GS-0002"]
        self.assertEqual((item["owner"], item["due_at"], item["review_at"]),
                         ("claude", "2026-10-06T14:00", "2026-10-09"))

    def test_project_defaults_from_the_cwd_without_claude_project_dir(self) -> None:
        cwd = self.ws / "shop"
        proc = subprocess.run([sys.executable, str(LE), "add", "x y", "--public", "shop: a thing"],
                              capture_output=True, encoding="utf-8", cwd=str(cwd),
                              env=self.env(CLAUDE_PROJECT_DIR=None), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.items()["GS-0001"]["project"], "shop")

    def test_public_is_required_with_a_hint(self) -> None:
        proc = self.ghosted("add", "Pay the invoice")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--public is required", proc.stderr)
        self.assertIn("no names, amounts, emails or customer details", proc.stderr)
        self.assertFalse((self.le_home / "items.json").exists())

    def test_unsafe_public_titles_are_refused_not_edited(self) -> None:
        cases = [("email bob@example.com the invoice", "contains an email address"),
                 ("pay $480 to the vendor", "contains a money amount"),
                 ("   ", "is blank"),
                 ("x" * 101, "characters long"),
                 ("call 415-555-0123 back", "contains a phone number"),
                 ("rotate sk-ant-abc123 now", "looks like a key or token")]
        for title, problem in cases:
            with self.subTest(title=title[:20]):
                proc = self.ghosted("add", "local", "--public", title, "--project", "shop")
                self.assertEqual(proc.returncode, 2)
                self.assertIn(problem, proc.stderr)
                self.assertIn(cli.REWRITE_HINT, proc.stderr)
                for leak in ("bob@example.com", "480", "415-555", "sk-ant"):
                    self.assertNotIn(leak, proc.stderr + proc.stdout)
        self.assertFalse((self.le_home / "items.json").exists())
        self.assertEqual(self.inbox(), [])

    def test_unsafe_project_names_are_refused(self) -> None:
        proc = self.ghosted("add", "local", "--public", "a neutral title", "--project", "acme bob@example.com")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--project contains an email address", proc.stderr)
        named = self.ws / ("deadbeef" * 5)
        named.mkdir()
        proc = self.ghosted("add", "local", "--public", "a neutral title", env=self.env(CLAUDE_PROJECT_DIR=str(named)))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("pass --project NAME", proc.stderr)
        self.assertFalse((self.le_home / "items.json").exists())

    def test_bad_dates_are_usage_errors(self) -> None:
        for args, text in ((("--due", "someday"), "can't read the due date"),
                           (("--due", "02-30"), "not a real date"),
                           (("--due", "10-11 25:00"), "not a time of day"),
                           (("--review", "10-12 14:00"), "takes a day")):
            with self.subTest(args=args):
                proc = self.ghosted("add", "local", "--public", "shop: a title", *args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(text, proc.stderr)
        self.assertEqual(self.ghosted("add", "  ", "--public", "shop: a title").returncode, 2)
        self.assertEqual(self.ghosted("add", "x", "--public", "y", "--owner", "bob").returncode, 2)


class StatusTests(CliCase):
    def setUp(self) -> None:
        super().setUp()
        for n in (1, 2, 3):
            self.ok("add", "Local title %d" % n, "--public", "shop: item %d" % n, "--project", "shop")

    def test_done(self) -> None:
        self.assertEqual(self.ok("done", "1"), "GS-0001 done: shop: item 1\n")
        self.assertEqual(self.ok("done", "GS-0001"), "GS-0001 is already done: shop: item 1\n")
        self.assertEqual(self.ok("done", "gs-1"), "GS-0001 is already done: shop: item 1\n")
        item = self.items()["GS-0001"]
        self.assertEqual((item["status"], item["rev"], item["closed_by"]), ("done", 2, "session"))
        status = [e for e in self.inbox() if e["type"] == "item.status"]
        self.assertEqual(len(status), 1)
        self.assertEqual((status[0]["item"]["status"], status[0]["item"]["rev"]), ("done", 1))
        proc = self.ghosted("done", "99")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GS-0099 not found", proc.stderr)
        self.assertEqual(self.ghosted("done", "abc").returncode, 2)

    def test_snooze_and_resnooze(self) -> None:
        self.assertEqual(self.ok("snooze", "2", "3d"), "GS-0002 snoozed until Thu Oct 8, 9:00 AM: shop: item 2\n")
        item = self.items()["GS-0002"]
        self.assertEqual((item["status"], item["snooze_until"], item["rev"]), ("snoozed", "2026-10-08T09:00", 2))
        self.assertEqual(self.ok("snooze", "2", "3d"),
                         "GS-0002 is already snoozed until Thu Oct 8, 9:00 AM: shop: item 2\n")
        self.assertEqual(self.ok("snooze", "GS-0002", "MON"),
                         "GS-0002 snoozed until Mon Oct 12, 9:00 AM: shop: item 2\n")
        item = self.items()["GS-0002"]
        self.assertEqual((item["snooze_until"], item["rev"]), ("2026-10-12T09:00", 3))
        revs = [(e["item"]["status"], e["item"]["rev"], e["item"]["snooze_until"])
                for e in self.inbox() if e["type"] == "item.status"]
        # a re-snooze is one change carrying the rev it saw, as the Worker expects
        self.assertEqual(revs, [("snoozed", 1, "2026-10-08T09:00"), ("snoozed", 2, "2026-10-12T09:00")])
        self.assertEqual(self.ok("snooze", "3", "1d"), "GS-0003 snoozed until Tue Oct 6, 9:00 AM: shop: item 3\n")
        self.assertEqual(self.ghosted("snooze", "2", "5d").returncode, 2)
        self.assertEqual(self.ghosted("snooze", "77", "1d").returncode, 1)

    def test_dismiss(self) -> None:
        self.assertEqual(self.ok("dismiss", "3", "--reason", "not-real"),
                         "GS-0003 dismissed (not real): shop: item 3\n")
        item = self.items()["GS-0003"]
        self.assertEqual((item["status"], item["dismiss_reason"]), ("dismissed", "not_real"))
        self.assertEqual(self.ok("dismiss", "3", "--reason", "not_real"),
                         "GS-0003 is already dismissed (not real): shop: item 3\n")
        # the other reason corrects it (it matters: not_real counts as a false alarm)
        self.assertEqual(self.ok("dismiss", "3", "--reason", "handled_elsewhere"),
                         "GS-0003 dismissed (handled elsewhere): shop: item 3\n")
        item = self.items()["GS-0003"]
        self.assertEqual((item["dismiss_reason"], item["rev"]), ("handled_elsewhere", 3))
        self.assertEqual(self.ghosted("dismiss", "3", "--reason", "bored").returncode, 2)
        # --reason is optional (contract 16.3): the default is not_real
        self.assertEqual(self.ok("dismiss", "2"), "GS-0002 dismissed (not real): shop: item 2\n")
        self.assertEqual(self.items()["GS-0002"]["dismiss_reason"], "not_real")


class ListTests(CliCase):
    def test_empty(self) -> None:
        self.assertEqual(self.ok("list"), "Nothing open.\n")
        self.assertEqual(self.ok("list", "--all"), "No items yet.\n")
        self.assertEqual(json.loads(self.ok("list", "--json"))["items"], [])

    def test_grouped_like_the_phone(self) -> None:
        self.ok("add", "Local soon", "--public", "shop: due soon", "--due", "10-06", "--project", "shop")
        self.ok("add", "Local waiting", "--public", "shop: waiting", "--project", "shop")
        self.ok("add", "Local claude", "--public", "shop: claude's", "--owner", "claude", "--project", "shop")
        self.ok("add", "Local snoozed", "--public", "shop: snoozed", "--project", "shop")
        self.ok("add", "Local done", "--public", "shop: done", "--project", "shop")
        self.ok("add", "Local overdue", "--public", "shop: overdue", "--due", "10-01", "--project", "shop")
        self.ok("snooze", "4", "1d")
        self.ok("done", "5")
        led = self.ledger()
        led.upsert_interrupted(SESSION, "10:abc", "boathouse", "boathouse: session 10-04 ended on usage limit",
                               "ended on usage limit · 3 uncommitted", False,
                               "cd ~/workspace/boathouse && claude --resume " + SESSION, "limit_meta",
                               "2026-10-05T14:00:00Z", now=NOW)
        text = self.ok("list")
        heads = [line for line in text.splitlines() if line and not line.startswith(" ")]
        self.assertEqual(heads, ["Due soon / overdue (2)", "Interrupted (1)", "Waiting on you (1)",
                                 "Also open (owner claude) (1)", "Snoozed (1)"])
        self.assertLess(text.index("Local overdue"), text.index("Local soon"))  # earliest due first
        self.assertIn("due Thu Oct 1, overdue", text)
        self.assertIn("cd ~/workspace/boathouse && claude --resume " + SESSION, text)
        self.assertIn("until Tue Oct 6, 9:00 AM", text)
        self.assertNotIn("Local done", text)
        text = self.ok("list", "--all")
        self.assertIn("Closed (1)", text)
        self.assertIn("Local done", text)
        rows = json.loads(self.ok("list", "--json"))["items"]
        self.assertEqual([r["group"] for r in rows], ["due", "due", "interrupted", "waiting", "later", "snoozed"])
        self.assertEqual(rows[0]["local_title"], "Local overdue")
        rows = json.loads(self.ok("list", "--json", "--all"))["items"]
        self.assertEqual(rows[-1]["group"], "closed")


class ReportTests(CliCase):
    def test_report(self) -> None:
        (self.root / "projects").mkdir()
        self.ok("add", "check the deploy", "--public", "shop: check", "--project", "shop", "--due", "today")
        text = self.ok("report", "today")
        self.assertEqual(text.splitlines()[0], "GHOSTED · Today, Mon 5 Oct · 1 reminder due")
        self.assertIn("No session is waiting on you today (0 sessions checked).", text.splitlines())
        self.assertIn('1 ◷ due TODAY · "check the deploy"   (ghosted add, Mon 5)', text)
        self.assertIn("Done with one? ghosted done 1 · ghosted snooze 1 3d · ghosted dismiss 1", text)
        data = json.loads(self.ok("report", "--json"))
        self.assertEqual((data["window"]["name"], data["first_run"]), ("7d", False))
        self.assertEqual([r["kind"] for r in data["rows"]], ["due"])
        self.assertEqual(self.ok("done", "1"), "GS-0001 done: shop: check\n")
        self.assertIn("Nothing waiting on you in the last 30 days (0 sessions checked).",
                      self.ok("report", "30d"))
        records = [m for m in self.ledger().measures() if m.get("type") == "report"]
        self.assertEqual([(r["window"], r["via"], r["waiting"]) for r in records],
                         [("today", "cli", ["GS-0001"]), ("7d", "cli", ["GS-0001"]), ("30d", "cli", [])])
        for bad in (("report", "fortnight"), ("report", "0d"), ("report", "--quotes", "--no-quotes")):
            self.assertEqual(self.ghosted(*bad).returncode, 2, bad)


class InitTests(CliCase):
    def ghosted(self, *args, env=None):
        # stdin is never a terminal here (a suite run from one would otherwise inherit it, and a
        # bare `ghosted init` on a terminal is the guided setup): these are the v1 one-shot tests.
        return subprocess.run([sys.executable, str(LE)] + list(args), capture_output=True,
                              stdin=subprocess.DEVNULL, encoding="utf-8", env=env or self.env(),
                              cwd=str(self.root), timeout=120)

    def fresh(self, name="fresh"):
        home = self.root / name
        return home, self.env(GHOSTED_HOME=str(home))

    def test_init_writes_config_once(self) -> None:
        home, env = self.fresh()
        out = self.ok("init", "--adapter", "ntfy", "--timezone", "America/Chicago",
                      "--workspace-root", "~/workspace", env=env)
        path = home / "config.toml"
        self.assertEqual(out.splitlines()[0], "wrote %s" % path)
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(str(home)).st_mode), 0o700)
        cfg = load_config(environ=env)
        self.assertEqual((cfg.adapter, cfg.timezone), ("ntfy", "America/Chicago"))
        self.assertEqual(cfg.workspace_roots, [self.home / "workspace"])
        self.assertRegex(cfg.ntfy_topic, r"^ghosted-[A-Za-z0-9_-]{32}$")
        # The topic works like a password: never printed (this path runs in captured output),
        # only where it is (config.toml, 0600) and its first characters.
        self.assertNotIn(cfg.ntfy_topic, out)
        self.assertIn("config.toml under [notify.ntfy] (it starts %s…" % cfg.ntfy_topic[:5], out)
        self.assertIn(cfg.ntfy_topic, path.read_text())
        self.assertEqual(cfg.projects_dir, self.home / ".claude" / "projects")
        self.assertEqual(cfg.env_file, home / ".env")  # beside this GHOSTED_HOME, not ~/.ghosted
        before = path.read_bytes()
        out = self.ok("init", "--adapter", "remote", env=env)
        self.assertIn("already exists; left unchanged (edit it to change settings)", out)
        self.assertEqual(path.read_bytes(), before)

    def test_init_defaults_to_no_pushes(self) -> None:
        home, env = self.fresh()
        out = self.ok("init", env=env)
        self.assertIn("pushes are off", out)
        cfg = load_config(environ=env)
        self.assertEqual((cfg.adapter, cfg.workspace_roots), ("none", []))
        self.assertTrue(cfg.ntfy_topic.startswith("ghosted-"))
        self.assertNotIn(cfg.ntfy_topic, out)  # non-TTY output lands in transcripts: masked
        self.assertIn("run ghosted init in a terminal", out)

    def test_init_points_env_file_at_this_le_home(self) -> None:
        under_home = self.home / ".ghosted"
        env = self.env(GHOSTED_HOME=str(under_home))
        out = self.ok("init", "--adapter", "pushover", env=env)
        self.assertIn('env_file = "~/.ghosted/.env"', (under_home / "config.toml").read_text())
        self.assertIn("PUSHOVER_TOKEN and PUSHOVER_USER in ~/.ghosted/.env", out)
        elsewhere = self.home / "alt-ghosted"
        env = self.env(GHOSTED_HOME=str(elsewhere))
        out = self.ok("init", "--adapter", "pushover", env=env)
        self.assertIn('env_file = "~/alt-ghosted/.env"', (elsewhere / "config.toml").read_text())
        self.assertIn("in ~/alt-ghosted/.env", out)
        self.assertEqual(load_config(environ=env).env_file, elsewhere / ".env")

    def test_init_topics_are_random(self) -> None:
        topics = set()
        for n in range(3):
            home, env = self.fresh("fresh%d" % n)
            self.ok("init", env=env)
            topics.add(load_config(environ=env).ntfy_topic)
        self.assertEqual(len(topics), 3)

    def test_init_v2_keys_and_the_macos_adapter(self) -> None:
        home, env = self.fresh()
        out = self.ok("init", "--adapter", "macos", env=env)
        cfg = load_config(environ=env)
        self.assertEqual((cfg.adapter, cfg.direct_questions, cfg.watchdog_ping_url), ("macos", "waiting", ""))
        text = (home / "config.toml").read_text()
        self.assertIn("[watchdog]", text)
        self.assertIn('direct_questions = "waiting"', text)
        from core import config as config_mod

        self.assertIsNone(config_mod.minitoml_problem(text))
        self.assertIn("then: ghosted install --all", out)

    def test_init_yes_is_the_v1_one_shot(self) -> None:
        home, env = self.fresh()
        out = self.ok("init", "--yes", env=env)
        self.assertIn("pushes are off", out)
        self.assertEqual(load_config(environ=env).adapter, "none")
        self.assertIn("already exists; left unchanged", self.ok("init", "--yes", env=env))

    def test_render_config_writes_the_v2_keys(self) -> None:
        from core import config as config_mod

        text = cli.render_config(direct_questions="soft", ping_url="https://hc-ping.com/x")
        raw = config_mod.parse_config_text(text)
        self.assertEqual((raw["detect"]["direct_questions"], raw["watchdog"]["ping_url"]),
                         ("soft", "https://hc-ping.com/x"))
        self.assertIsNone(config_mod.minitoml_problem(text))
        cfg = config_mod.build_config(raw, self.root / "x", self.env())
        self.assertEqual((cfg.direct_questions, cfg.watchdog_ping_url), ("soft", "https://hc-ping.com/x"))
        self.assertIs(raw["skip"]["temp_folders"], True)  # the temp-folder skip is visible, and on

    def test_init_on_a_terminal_runs_the_guided_setup(self) -> None:
        import pty
        import select
        import time

        home, env = self.fresh()
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([sys.executable, str(LE), "init"], stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, cwd=str(self.root))
        finally:
            os.close(slave)
        try:
            # no ~/workspace here: folder (skip), live record (no), the two CLAUDE.md rules (no, no),
            # channel (Enter keeps off)
            os.write(master, b"\nn\nn\nn\n\n")
            deadline = time.monotonic() + 60
            while proc.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        os.read(master, 4096)  # drain the echo
                    except OSError:
                        break
            out, err = proc.communicate(timeout=60)
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
        text = out.decode("utf-8")
        self.assertEqual(proc.returncode, 0, text + err.decode("utf-8"))
        self.assertIn("Ghosted setup.", text)
        self.assertIn("Wrote %s." % (home / "config.toml"), text)
        self.assertIn("ghosted uninstall --all", text)
        cfg = load_config(environ=env)
        self.assertEqual(cfg.adapter, "none")
        self.assertNotIn(cfg.ntfy_topic, text)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())  # the hook was declined
        self.assertIn("<what it is>", text)                                    # the ghosted-add rule, shown
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())     # both rules declined

    def test_init_refuses_bad_options(self) -> None:
        home, env = self.fresh()
        self.assertEqual(self.ghosted("init", "--timezone", "Mars/Olympus_Mons", env=env).returncode, 2)
        self.assertEqual(self.ghosted("init", "--remote-url", "http://plain.example", env=env).returncode, 2)
        self.assertEqual(self.ghosted("init", "--adapter", "pigeon", env=env).returncode, 2)
        self.assertFalse((home / "config.toml").exists())


class InstallCliTests(CliCase):
    def settings_path(self):
        return self.home / ".claude" / "settings.json"

    def write_settings(self):
        self.settings_path().parent.mkdir(parents=True, exist_ok=True)
        self.settings_path().write_text(json.dumps(SETTINGS, indent=2) + "\n")

    def test_dry_run_prints_the_plan_and_touches_nothing(self) -> None:
        self.write_settings()
        before = snapshot(self.home, self.le_home)
        proc = self.ghosted("install", "--all", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        for text in ("bin      would create", "skill    would create", "hook     would update",
                     "launchd  would create", "dry run: nothing was changed"):
            self.assertIn(text, out)
        self.assertEqual(snapshot(self.home, self.le_home), before)
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        self.assertNotIn(SECRET, out + proc.stderr)

    def test_install_all_doctor_then_uninstall(self) -> None:
        self.write_settings()
        original = self.settings_path().read_bytes()
        proc = self.ghosted("install", "--all")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        link = self.home / ".local" / "bin" / "ghosted"
        self.assertEqual(os.path.realpath(str(link)), str(LE.resolve()))
        via_link = subprocess.run([sys.executable, str(link), "version"], capture_output=True,
                                  encoding="utf-8", env=self.env(), cwd=str(self.root), timeout=60)
        self.assertEqual((via_link.returncode, via_link.stdout), (0, "ghosted %s\n" % core.__version__))
        main_skill = self.home / ".claude" / "skills" / "ghosted"
        self.assertTrue((main_skill / "SKILL.md").is_file())
        data = json.loads(self.settings_path().read_text())
        self.assertEqual(data["hooks"]["Stop"][0], SETTINGS["hooks"]["Stop"][0])
        self.assertEqual(data["hooks"]["Stop"][1]["hooks"][0]["args"], [str(ROOT / "bin" / "ghosted"), "hook"])
        self.assertEqual(data["hooks"]["StopFailure"][0]["hooks"][0]["args"], [str(ROOT / "bin" / "ghosted"), "hook"])
        self.assertEqual(data["hooks"]["PreToolUse"], SETTINGS["hooks"]["PreToolUse"])
        plist = self.home / "Library" / "LaunchAgents" / "com.ghosted.run.plist"
        self.assertTrue(plist.is_file())
        uid = os.getuid()
        self.assertEqual([c for c in self.calls() if c.startswith("launchctl")], [
            "launchctl bootout gui/%d/com.ghosted.run" % uid,
            "launchctl bootstrap gui/%d %s" % (uid, plist),
        ])
        backups = list((self.le_home / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

        again = self.ok("install", "--all")
        self.assertNotIn("create", again)
        self.assertNotIn("update", again)

        proc = self.ghosted("doctor")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        out = proc.stdout
        self.assertIn("Stop and StopFailure registered", out)
        self.assertIn("-> this repo", out)
        self.assertIn("current, loaded", out)
        self.assertIn("is not on PATH", out)  # PATH here is the fake bin only
        self.assertNotIn(SECRET, out + proc.stderr)

        proc = self.ghosted("uninstall", "--all")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.lexists(str(link)))
        self.assertFalse(os.path.lexists(str(main_skill)))
        self.assertFalse(plist.exists())
        self.assertEqual(self.settings_path().read_bytes(), original)
        self.assertEqual(self.calls()[-1], "launchctl bootout gui/%d/com.ghosted.run" % uid)

    def test_install_needs_a_component(self) -> None:
        proc = self.ghosted("install")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--all", proc.stderr)
        self.assertEqual(self.ghosted("uninstall").returncode, 2)

    def test_refusals_exit_1(self) -> None:
        link = self.home / ".local" / "bin" / "ghosted"
        link.parent.mkdir(parents=True)
        link.write_text("someone else's ghosted")
        proc = self.ghosted("install", "--bin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refuse", proc.stdout)
        self.assertEqual(link.read_text(), "someone else's ghosted")
        self.assertEqual(self.ghosted("install", "--bin", "--force").returncode, 0)
        self.assertTrue(os.path.islink(str(link)))


class DoctorTests(CliCase):
    def test_doctor_shows_secrets_as_set_or_missing_only(self) -> None:
        env_file = self.le_home / ".env"
        env_file.write_text("GHOSTED_SERVICE_TOKEN_ID=id-%s\nGHOSTED_SERVICE_TOKEN_SECRET=%s\n" % (SECRET, SECRET))
        os.chmod(str(env_file), 0o600)
        self.write_config('[notify.ntfy]\ntopic = "ghosted-%s"\n' % SECRET)
        proc = self.ghosted("doctor")
        out = proc.stdout + proc.stderr
        self.assertNotIn(SECRET, out)
        self.assertIn("GHOSTED_SERVICE_TOKEN_ID set", out)
        self.assertIn("GHOSTED_SERVICE_TOKEN_SECRET set", out)
        self.assertIn("PUSHOVER_TOKEN missing", out)
        self.assertIn("topic set", out)
        self.assertIn("adapter none", out)
        self.assertIn("last run", out)

    def test_doctor_flags_a_loose_env_file_and_a_broken_config(self) -> None:
        env_file = self.le_home / ".env"
        env_file.write_text("PUSHOVER_TOKEN=%s\n" % SECRET)
        os.chmod(str(env_file), 0o644)
        out = self.ghosted("doctor").stdout
        self.assertIn("chmod 600", out)
        self.assertNotIn(SECRET, out)
        (self.le_home / "config.toml").write_text("adapter = [\n")
        proc = self.ghosted("doctor")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL  config", proc.stdout)

    def test_doctor_drift_and_watchdog_rows(self) -> None:
        out = self.ghosted("doctor").stdout
        self.assertRegex(out, r"(?m)^ok\s+drift\s+no scan has read a session file yet$")
        self.assertRegex(out, r"(?m)^ok\s+watchdog\s+off$")
        self.write_config('[watchdog]\nping_url = "https://hc-ping.example/%s"\n' % SECRET)
        (self.le_home / "state.json").write_text(json.dumps({
            "version": 1, "since": None, "sessions": {}, "deferred": [], "last_scan": None,
            "drift": {"at": NOW, "files": 6, "lines": 60, "zero_conversation": 5, "empty_final": 5,
                      "unknown_lines": 0, "unknown_types": {}}}))
        proc = self.ghosted("doctor")
        out = proc.stdout + proc.stderr
        self.assertNotIn(SECRET, out)
        self.assertRegex(out, r"(?m)^ok\s+watchdog\s+set \(hc-ping\.example\)$")
        self.assertRegex(out, r"(?m)^warn\s+drift\s+Claude Code's session format may have changed: "
                              r"5 of 6 session files had no messages Ghosted could read$")
        state = json.loads((self.le_home / "state.json").read_text())
        state["drift"].update(zero_conversation=0, empty_final=0)
        (self.le_home / "state.json").write_text(json.dumps(state))
        self.assertRegex(self.ghosted("doctor").stdout, r"(?m)^ok\s+drift\s+the last scan read 6 session files as expected$")

    def test_remote_only_commands_need_the_remote_adapter(self) -> None:
        for args in (("sync",), ("pair",), ("pair", "--revoke-all")):
            proc = self.ghosted(*args)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("needs the remote adapter", proc.stderr)
        proc = self.ghosted("test-push")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no notify adapter is set", proc.stderr)

    def test_doctor_finds_and_fixes_an_item_without_its_event(self) -> None:
        self.ok("add", "Local one", "--public", "shop: one", "--project", "shop")
        led = self.ledger()
        with mock.patch.object(Ledger, "_append_event", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):  # a crash between saving the item and its event
                led.add("decision", "shop", "Local two", "shop: two", now=led.items()["GS-0001"]["created_at"])
        proc = self.ghosted("doctor")
        line = [l for l in proc.stdout.splitlines() if " inbox " in l]
        self.assertEqual(len(line), 1, proc.stdout)
        self.assertTrue(line[0].startswith("warn"), line[0])  # adapter none: nothing is lost yet
        self.assertIn("1 item has no create event", line[0])
        self.assertIn("GS-0002", line[0])
        self.assertIn("ghosted doctor --fix", line[0])
        proc = self.ghosted("doctor", "--fix")
        self.assertIn("queued 1 event for GS-0002", proc.stdout)
        self.assertEqual([e["item"]["id"] for e in self.inbox() if e["type"] == "item.add"],
                         ["GS-0001", "GS-0002"])
        self.assertIn("every item has its create event", self.ghosted("doctor").stdout)
        self.assertNotIn("Local two", self.ghosted("doctor").stdout)


# ---- in-process tests with fakes for modules other parts of the build own ---------------------

RunOutcome = namedtuple("RunOutcome", "run scan notify_result skipped")


class FakeAdapter:
    def __init__(self, sync_result=None, send_result=None, check_result=(True, "ntfy reachable; topic set")):
        self.sync_result = sync_result
        self.send_result = send_result
        self.check_result = check_result
        self.sync_calls = []
        self.send_calls = []

    def sync(self, ledger, now=None, *, test_push=None, pair=None, revoke_devices=False):
        self.sync_calls.append({"test_push": test_push, "pair": pair, "revoke_devices": revoke_devices,
                                "now": now})
        return self.sync_result

    def send(self, items, *, title, body, kind, priority="normal", click_url=None):
        self.send_calls.append({"items": items, "title": title, "body": body, "kind": kind})
        return self.send_result

    def check(self):
        return self.check_result


class InProcessCase(CliCase):
    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), environ=env or self.env())
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def fake_module(self, dotted, **attrs):
        module = types.ModuleType(dotted)
        for key, value in attrs.items():
            setattr(module, key, value)
        patches = [mock.patch.dict(sys.modules, {dotted: module})]
        if dotted.startswith("core."):
            patches.append(mock.patch.object(core, dotted.split(".", 1)[1], module, create=True))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield module

    def fake_notify(self, adapter):
        return self.fake_module("notify", get_adapter=lambda cfg, env=None, **kw: adapter)


class RunAndSweepTests(InProcessCase):
    def test_run_summary_line(self) -> None:
        sync = types.SimpleNamespace(ok=True, error=None, acks={"e%d" % n: {"result": "applied"} for n in range(4)},
                                     stale=[], merged=0)
        outcome = RunOutcome({"run_id": "r1", "ok": True, "sessions_scanned": 3, "items_added": 1},
                             types.SimpleNamespace(sessions_scanned=3, items_added=1, items_updated=0, deferred=[]),
                             sync, False)
        calls = []

        def run(cfg, now=None, notify=True, scan=True):
            calls.append((now, notify, scan))
            return outcome

        with self.fake_module("core.runner", run=run):
            code, out, err = self.run_cli("run")
        self.assertEqual((code, out, err), (0, "run ok · 3 scanned · 1 new · synced 4 events\n", ""))
        self.assertEqual(calls[0][1:], (True, True))
        self.assertEqual(cli.run_summary(outcome)[0], "run ok · 3 scanned · 1 new · synced 4 events")

    def test_run_summary_variants(self) -> None:
        failed = RunOutcome({"ok": False, "error": "projects_dir is missing", "sessions_scanned": 0,
                             "items_added": 0}, None, None, False)
        self.assertEqual(cli.run_summary(failed), ("run failed: projects_dir is missing · 0 scanned · 0 new", 1))
        self.assertEqual(cli.run_summary(RunOutcome({}, None, None, True)),
                         ("run skipped: another scan is running", 0))
        scan = types.SimpleNamespace(sessions_scanned=2, items_added=0, items_updated=1, deferred=2)
        local = RunOutcome({"ok": True}, scan, {"reopened": ["GS-0004"], "error": None, "sent": [
            {"dedup_key": "digest:2026-10-05", "ok": True}, {"dedup_key": "advance:GS-0002", "ok": False}]},
            False)
        self.assertEqual(cli.run_summary(local)[0],
                         "run ok · 2 scanned · 0 new · 1 updated · 2 deferred · sent 1 push · 1 push failed · 1 reopened")
        quiet = RunOutcome({"ok": True}, None, {"reopened": [], "sent": [], "error": None}, False)
        self.assertEqual(cli.run_summary(quiet)[0], "run ok · 0 scanned · 0 new · nothing to push")
        broken = RunOutcome({"ok": True}, None, {"reopened": [], "sent": [], "error": "OSError: disk full"}, False)
        self.assertEqual(cli.run_summary(broken)[0], "run ok · 0 scanned · 0 new · push failed: OSError: disk full")
        sync_failed = RunOutcome({"ok": True, "sessions_scanned": 1, "items_added": 0}, None,
                                 types.SimpleNamespace(ok=False, error="network unreachable", acks={}), False)
        self.assertEqual(cli.run_summary(sync_failed),
                         ("run ok · 1 scanned · 0 new · sync failed: network unreachable", 0))
        raised = RunOutcome({"ok": True, "sessions_scanned": 1, "items_added": 0}, None,
                            {"ok": False, "error": "TransportError: timed out"}, False)
        self.assertEqual(cli.run_summary(raised)[0], "run ok · 1 scanned · 0 new · notify failed: TransportError: timed out")

    def test_run_summary_with_the_real_sync_result(self) -> None:
        try:
            from notify.base import SyncResult
        except ImportError:  # pragma: no cover - notify is built in parallel
            self.skipTest("notify not available")
        result = SyncResult(ok=True, acks={"e1": {"result": "applied"}}, stale=[("GS-0002", "done")])
        outcome = RunOutcome({"ok": True, "sessions_scanned": 5, "items_added": 2}, None, result, False)
        self.assertEqual(cli.run_summary(outcome)[0], "run ok · 5 scanned · 2 new · synced 1 event (1 stale)")

    def test_sweep(self) -> None:
        calls = []

        def sweep(cfg, now=None, scan=True):
            calls.append(scan)
            return "# Ghosted\n\nWaiting on you\n  GS-0001 something\n"

        with self.fake_module("core.runner", sweep=sweep):
            code, out, _ = self.run_cli("sweep")
            self.assertEqual((code, out), (0, "# Ghosted\n\nWaiting on you\n  GS-0001 something\n"))
            self.run_cli("sweep", "--no-scan")
        self.assertEqual(calls, [True, False])


class AdapterCommandTests(InProcessCase):
    def remote(self):
        self.write_config('[notify]\nadapter = "remote"\n[notify.remote]\nurl = "https://ghosted.example.workers.dev"\n')

    def test_sync(self) -> None:
        self.remote()
        result = types.SimpleNamespace(
            ok=True, error=None, merged=1, invalid=[],
            acks={"e1": {"result": "applied", "item_id": "GS-0001"}, "e2": {"result": "stale", "item_id": "GS-0002"}},
            stale=[("GS-0002", "done")])
        adapter = FakeAdapter(sync_result=result)
        with self.fake_notify(adapter):
            code, out, err = self.run_cli("sync")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "synced: 2 events acked (1 applied, 1 stale)",
            "  GS-0002 stale: changed elsewhere (now done); this Mac now has the server's version",
            "  1 item updated from the server",
        ])
        failing = FakeAdapter(sync_result=types.SimpleNamespace(ok=False, error="HTTP 403 from Access", acks={}))
        with self.fake_notify(failing):
            code, out, err = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("sync failed: HTTP 403 from Access", err)

    def test_test_push_remote(self) -> None:
        self.remote()
        adapter = FakeAdapter(sync_result=types.SimpleNamespace(ok=True, error=None, test_push={"sent": 1, "failed": 0}))
        with self.fake_notify(adapter):
            code, out, _ = self.run_cli("test-push", "--message", "hello phone")
        self.assertEqual((code, out), (0, "test push: sent to 1 subscription, 0 failed\n"))
        self.assertEqual(adapter.sync_calls[0]["test_push"], {"title": "Ghosted", "body": "hello phone"})
        none = FakeAdapter(sync_result=types.SimpleNamespace(ok=True, error=None, test_push={"sent": 0, "failed": 0}))
        with self.fake_notify(none):
            code, out, _ = self.run_cli("test-push")
        self.assertEqual(code, 1)
        self.assertIn("Enable notifications", out)

    def test_test_push_remote_counts_the_push_even_when_the_sync_has_a_problem(self) -> None:
        self.remote()
        partial = FakeAdapter(sync_result=types.SimpleNamespace(
            ok=False, error="the server did not acknowledge 1 event; it is sent again next time",
            test_push={"sent": 1, "failed": 0}))
        with self.fake_notify(partial):
            code, out, err = self.run_cli("test-push")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "test push: sent to 1 subscription, 0 failed",
            "  note: the sync also reported: the server did not acknowledge 1 event; it is sent again next time"])
        down = FakeAdapter(sync_result=types.SimpleNamespace(ok=False, error="cannot reach https://x: timed out",
                                                             test_push=None))
        with self.fake_notify(down):
            code, _, err = self.run_cli("test-push")
        self.assertEqual(code, 1)
        self.assertIn("test push failed: cannot reach https://x: timed out", err)

    def test_doctor_fails_an_item_without_its_event_for_the_remote_adapter(self) -> None:
        self.remote()
        led = self.ledger()
        with mock.patch.object(Ledger, "_append_event", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                led.add("decision", "shop", "Local one", "shop: one")
        runner = lambda argv: (0, "3.9.6\n" if "-c" in argv else "", "")  # noqa: E731
        with self.fake_notify(FakeAdapter(check_result=(True, "remote reachable"))), \
                mock.patch.object(install, "default_runner", runner):
            code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  inbox", out)

    def test_test_push_local_adapter(self) -> None:
        self.write_config('[notify]\nadapter = "ntfy"\n[notify.ntfy]\ntopic = "ghosted-test-topic"\n')
        adapter = FakeAdapter(send_result=types.SimpleNamespace(ok=True, channel="ntfy", error=None))
        with self.fake_notify(adapter):
            code, out, _ = self.run_cli("test-push")
        self.assertEqual((code, out), (0, "test push sent via ntfy\n"))
        self.assertEqual(adapter.send_calls, [{"items": [], "title": "Ghosted", "body": cli.TEST_BODY,
                                               "kind": "test"}])
        failing = FakeAdapter(send_result=types.SimpleNamespace(ok=False, channel="ntfy", error="HTTP 429"))
        with self.fake_notify(failing):
            code, _, err = self.run_cli("test-push")
        self.assertEqual(code, 1)
        self.assertIn("test push failed: HTTP 429", err)

    def test_pair(self) -> None:
        self.remote()
        adapter = FakeAdapter(sync_result=types.SimpleNamespace(
            ok=True, error=None, pair={"code": "K7QF-2M9X", "expires_at": "2026-10-05T15:10:00Z"}))
        with self.fake_notify(adapter):
            code, out, _ = self.run_cli("pair")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "pairing code: K7QF-2M9X (single use, expires 10:10 AM)")
        self.assertEqual(adapter.sync_calls[0]["pair"], {"ttl_minutes": 10})
        revoke = FakeAdapter(sync_result=types.SimpleNamespace(ok=True, error=None, pair=None))
        with self.fake_notify(revoke):
            self.assertEqual(self.run_cli("pair", "--revoke-all")[0], 0)
            self.assertTrue(revoke.sync_calls[0]["revoke_devices"])
            code, _, err = self.run_cli("pair")
        self.assertEqual(code, 1)
        self.assertIn("AUTH_MODE device", err)

    def test_doctor_runs_the_adapter_check(self) -> None:
        self.write_config('[notify]\nadapter = "ntfy"\n[notify.ntfy]\ntopic = "ghosted-%s"\n' % SECRET)
        runner_calls = []

        def runner(argv):
            runner_calls.append(list(argv))
            return 0, "3.9.6\n" if "-c" in argv else "", ""

        for check, status in (((True, "ntfy https://ntfy.sh reachable; topic set"), "ok"),
                              ((False, "ntfy https://ntfy.sh unreachable: timed out"), "FAIL")):
            with self.fake_notify(FakeAdapter(check_result=check)), \
                    mock.patch.object(install, "default_runner", runner):
                code, out, _ = self.run_cli("doctor")
            line = [l for l in out.splitlines() if "adapter" in l and "ntfy" in l and "reachable" in l]
            self.assertTrue(line and line[0].startswith(status), out)
            self.assertNotIn(SECRET, out)
            self.assertEqual(code, 0 if status == "ok" else 1)
        self.assertTrue(all(c[0] != "launchctl" or c[1] == "print" for c in runner_calls))


class ProjectDefaultTests(InProcessCase):
    def test_add_uses_the_detector_when_it_has_one(self) -> None:
        with self.fake_module("core.detector", project_for=lambda cwd, cfg: "from-detector"):
            code, out, err = self.run_cli("add", "x y", "--public", "a neutral title")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.items()["GS-0001"]["project"], "from-detector")

    def test_add_falls_back_when_the_detector_fails(self) -> None:
        def broken(cwd, cfg):
            raise RuntimeError("git exploded")

        with self.fake_module("core.detector", project_for=broken):
            code, _, err = self.run_cli("add", "x y", "--public", "a neutral title")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.items()["GS-0001"]["project"], "shop")

    def test_a_refusal_from_the_ledger_is_a_usage_error(self) -> None:
        from core.sanitize import Refused

        refused = Refused(["the project name contains an email address"])
        with mock.patch.object(Ledger, "add", side_effect=refused):
            code, out, err = self.run_cli("add", "x y", "--public", "a neutral title")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("ghosted add: refused: the project name contains an email address", err)
        self.assertIn(cli.REWRITE_HINT, err)

    def test_hook_dispatch_in_process(self) -> None:
        data = json.dumps({"session_id": SESSION, "hook_event_name": "Stop", "cwd": str(self.ws / "shop"),
                           "last_assistant_message": "Want me to ship it?"}).encode()
        from core import hook

        with mock.patch.object(hook, "read_stdin", return_value=data), \
                mock.patch.dict(os.environ, {"GHOSTED_HOME": str(self.le_home), "HOME": str(self.home)}):
            code, out, err = self.run_cli("hook")
        self.assertEqual((code, out, err), (0, "", ""))
        rows = Ledger(Config(home=self.le_home, projects_dir=self.root / "p")).stops()
        self.assertEqual([(r["verdict"], r["cwd_project"]) for r in rows], [("open_question", "shop")])


class InitDispatchTests(InProcessCase):
    """`ghosted init` picks the guided setup only with no options on a terminal (contract 16.5)."""

    def dispatch(self, *argv, tty=True):
        calls = []

        def run(ctx, io):
            calls.append((ctx, io))
            return 7

        with mock.patch.object(cli, "_stdin_is_tty", return_value=tty), \
                self.fake_module("core.onboard", run=run, ConsoleIO=lambda: "console"):
            code, out, err = self.run_cli(*argv, env=self.env(GHOSTED_HOME=str(self.root / "ghosted-fresh")))
        return code, calls

    def test_no_options_on_a_terminal(self) -> None:
        code, calls = self.dispatch("init")
        self.assertEqual(code, 7)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "console")
        self.assertEqual(calls[0][0].env["GHOSTED_HOME"], str(self.root / "ghosted-fresh"))

    def test_options_yes_or_no_terminal_keep_v1(self) -> None:
        for argv, tty in ((("init", "--yes"), True), (("init", "--adapter", "ntfy"), True),
                          (("init", "--timezone", "America/Chicago"), True), (("init",), False)):
            with self.subTest(argv=argv, tty=tty):
                code, calls = self.dispatch(*argv, tty=tty)
                self.assertEqual((code, calls), (0, []))
                (self.root / "ghosted-fresh" / "config.toml").unlink()

    def test_stdin_is_tty_survives_a_closed_stdin(self) -> None:
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(cli._stdin_is_tty())
        closed = io.StringIO()
        closed.close()
        with mock.patch.object(sys, "stdin", closed):
            self.assertFalse(cli._stdin_is_tty())


if __name__ == "__main__":
    unittest.main()
