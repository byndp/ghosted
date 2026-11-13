"""core.install: bin and skill links, the settings.json hook groups, the launchd plist.

Everything runs under a temp home with an injected runner, so no test touches the real
~/.claude, ~/.local/bin or ~/Library/LaunchAgents, and launchctl never runs. Also checks the
plugin-shaped files (SKILL.md frontmatter, hooks.json, plugin.json) with tiny parsers.
"""
from __future__ import annotations

import copy
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import install
from core.config import Config
from core.install import InstallError

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 5, 20, 45, 0, tzinfo=timezone.utc)
SECRET = "Bearer sk-live-SECRET-MARKER-0000"

# Shaped like a real ~/.claude/settings.json: several events, async groups with timeouts, a
# matcher group, an env block and non-ASCII text. The hook commands are invented.
REAL_LIKE = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "env": {"EXAMPLE_TOKEN": "placeholder-value", "GREETING": "Grüße, café ✓"},
    "permissions": {"allow": ["Bash(git status:*)", "Read(~/notes/**)"], "deny": [],
                    "defaultMode": "default"},
    "model": "opus",
    "statusLine": {"type": "command", "command": "~/.claude/statusline.sh", "padding": 0},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/bin/guard.py", "timeout": 10}]},
            {"hooks": [{"type": "command", "command": "curl -s -H 'Authorization: %s' https://hooks.example/x" % SECRET,
                        "async": True, "timeout": 5}]},
        ],
        "SessionStart": [{"hooks": [{"type": "command", "command": "~/bin/start.sh", "timeout": 5}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "~/bin/end.sh", "timeout": 5}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "~/bin/notify.sh", "async": True,
                                     "timeout": 5}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "~/bin/bridge stop", "async": True, "timeout": 5}]}],
        "StopFailure": [{"hooks": [{"type": "command", "command": "~/bin/bridge fail", "async": True,
                                    "timeout": 5}]}],
        "PermissionRequest": [{"hooks": [{"type": "command", "command": "~/bin/ask.sh", "timeout": 1800}]}],
    },
    "feedbackSurveyRate": 0.05,
    "cleanupPeriodDays": 99,
    "includeCoAuthoredBy": False,
}


class Recorder:
    """An injected runner: records argv, answers from a table (default rc 0)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        for prefix, answer in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        if argv[:2] == ["claude", "--version"]:
            return 0, "2.1.290 (Claude Code)\n", ""
        return 0, "", ""

    def launchctl(self):
        return [c for c in self.calls if c and c[0] == "launchctl"]


def tree(root: Path):
    """Every path under root with its type, mode and content (for 'nothing changed')."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = os.lstat(str(path))
            if stat.S_ISLNK(info.st_mode):
                out[str(path)] = ("link", os.readlink(str(path)))
            elif stat.S_ISDIR(info.st_mode):
                out[str(path)] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                out[str(path)] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
    return out


class InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.le_home = self.home / ".ghosted"
        self.runner = Recorder()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ctx(self, force=False, cfg=None, path_env=None, le_home=None, runner=None):
        cfg = cfg or Config(home=le_home or self.le_home, projects_dir=self.root / "projects")
        env = {"HOME": str(self.home),
               "PATH": path_env if path_env is not None else str(self.home / ".local" / "bin") + ":/usr/bin"}
        return install.make_context(cfg=cfg, home=self.home, repo=ROOT, runner=runner or self.runner,
                                    uid=501, environ=env, force=force, now=NOW)

    def write_settings(self, data, mode=0o644, path=None):
        path = path or self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(str(path), mode)
        return path

    def read_settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))


class HookSettingsTests(InstallCase):
    def ours(self, ctx):
        return {"hooks": [{"type": "command", "command": ctx.hook_python,
                           "args": [str(ROOT / "bin" / "ghosted"), "hook"], "async": True}]}

    def test_appends_a_new_group_and_keeps_everything_else(self) -> None:
        path = self.write_settings(REAL_LIKE)
        before_bytes = path.read_bytes()
        ctx = self.ctx()
        steps = install.apply(ctx, ["hook"])
        self.assertTrue(all(s.ok for s in steps))
        after = self.read_settings()
        expected = copy.deepcopy(REAL_LIKE)
        for event in ("Stop", "StopFailure"):
            expected["hooks"][event].append(self.ours(ctx))
        self.assertEqual(after, expected)
        self.assertEqual(list(after), list(REAL_LIKE))  # key order kept
        self.assertEqual(list(after["hooks"]), list(REAL_LIKE["hooks"]))
        text = path.read_text(encoding="utf-8")
        self.assertIn("Grüße, café ✓", text)  # ensure_ascii=False: no \\u escapes
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text, json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o644)
        backups = sorted((self.le_home / "backups").iterdir())
        self.assertEqual([b.name for b in backups], ["settings.json.20261005T204500Z"])
        self.assertEqual(backups[0].read_bytes(), before_bytes)
        self.assertEqual(stat.S_IMODE(os.stat(str(backups[0])).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.le_home / "backups")).st_mode), 0o700)

    def test_idempotent(self) -> None:
        self.write_settings(REAL_LIKE)
        install.apply(self.ctx(), ["hook"])
        first = (self.home / ".claude" / "settings.json").read_bytes()
        for _ in range(2):
            steps = install.apply(self.ctx(), ["hook"])
            self.assertEqual(steps[0].action, "exists")
            self.assertFalse(steps[0].changed)
        self.assertEqual((self.home / ".claude" / "settings.json").read_bytes(), first)
        self.assertEqual(len(list((self.le_home / "backups").iterdir())), 1)

    def test_uninstall_round_trips_to_the_original(self) -> None:
        path = self.write_settings(REAL_LIKE)
        original = path.read_bytes()
        install.apply(self.ctx(), ["hook"])
        steps = install.uninstall(self.ctx(), ["hook"])
        self.assertEqual(steps[0].action, "update")
        self.assertEqual(self.read_settings(), REAL_LIKE)
        self.assertEqual(path.read_bytes(), original)
        again = install.uninstall(self.ctx(), ["hook"])
        self.assertEqual(again[0].action, "absent")

    def test_missing_settings_file_is_created_and_removed_cleanly(self) -> None:
        ctx = self.ctx()
        install.apply(ctx, ["hook"])
        path = self.home / ".claude" / "settings.json"
        self.assertEqual(self.read_settings(), {"hooks": {"Stop": [self.ours(ctx)],
                                                          "StopFailure": [self.ours(ctx)]}})
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)
        self.assertFalse((self.le_home / "backups").exists())  # nothing to back up
        install.uninstall(ctx, ["hook"])
        self.assertEqual(self.read_settings(), {})

    def test_writes_through_a_symlink_and_keeps_its_mode(self) -> None:
        target = self.write_settings(REAL_LIKE, mode=0o640, path=self.root / "dotfiles" / "settings.json")
        link = self.home / ".claude" / "settings.json"
        link.parent.mkdir(parents=True)
        os.symlink(str(target), str(link))
        install.apply(self.ctx(), ["hook"])
        self.assertTrue(os.path.islink(str(link)))
        self.assertEqual(os.readlink(str(link)), str(target))
        self.assertEqual(stat.S_IMODE(os.stat(str(target)).st_mode), 0o640)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["settings.json"])

    def test_refreshes_an_old_group_in_place_and_drops_duplicates(self) -> None:
        old = {"type": "command", "command": "/opt/old/python3", "args": ["/old/repo/bin/ghosted", "hook"],
               "async": True}
        data = copy.deepcopy(REAL_LIKE)
        data["hooks"]["Stop"].insert(0, {"hooks": [dict(old)]})
        data["hooks"]["Stop"].append({"hooks": [dict(old)]})
        data["hooks"]["StopFailure"][0]["hooks"].append(dict(old))  # merged into a user group
        self.write_settings(data)
        ctx = self.ctx()
        install.apply(ctx, ["hook"])
        after = self.read_settings()
        mine = self.ours(ctx)["hooks"][0]
        self.assertEqual(after["hooks"]["Stop"], [{"hooks": [mine]}] + REAL_LIKE["hooks"]["Stop"])
        self.assertEqual(after["hooks"]["StopFailure"][0]["hooks"],
                         REAL_LIKE["hooks"]["StopFailure"][0]["hooks"] + [mine])
        self.assertEqual(len(after["hooks"]["StopFailure"]), 1)  # no extra group appended
        for event in [e for e in REAL_LIKE["hooks"] if e not in ("Stop", "StopFailure")]:
            self.assertEqual(after["hooks"][event], REAL_LIKE["hooks"][event])

    def test_uninstall_leaves_a_shared_group_with_its_other_handlers(self) -> None:
        data = copy.deepcopy(REAL_LIKE)
        mine = {"type": "command", "command": "/usr/bin/python3", "args": ["/x/bin/ghosted", "hook"], "async": True}
        data["hooks"]["Stop"][0]["hooks"].append(mine)
        self.write_settings(data)
        install.uninstall(self.ctx(), ["hook"])
        self.assertEqual(self.read_settings(), REAL_LIKE)

    def test_bad_settings_are_refused_without_quoting_them(self) -> None:
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        cases = [
            '{"hooks": {"Stop": [ {"command": "%s"} ]' % SECRET,   # truncated JSON
            '["%s"]' % SECRET,                                      # not an object
            '{"hooks": "%s"}' % SECRET,                              # hooks not an object
            '{"hooks": {"Stop": {"cmd": "%s"}}}' % SECRET,           # Stop not a list
        ]
        for text in cases:
            with self.subTest(text=text[:30]):
                path.write_text(text, encoding="utf-8")
                steps = install.apply(self.ctx(), ["hook"])
                self.assertFalse(steps[0].ok)
                self.assertEqual(steps[0].action, "error")
                rendered = " ".join(s.line(self.home) for s in steps)
                self.assertNotIn("SECRET", rendered)
                self.assertNotIn("sk-live", rendered)
                self.assertEqual(path.read_text(encoding="utf-8"), text)  # untouched
        path.write_text(cases[0], encoding="utf-8")
        with self.assertRaises(InstallError) as caught:
            install._read_settings(path)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertRegex(str(caught.exception), r"line \d+, column \d+")

    def test_secret_values_never_appear_in_steps_or_status(self) -> None:
        self.write_settings(REAL_LIKE)
        lines = [s.line(self.home) for s in install.plan(self.ctx(), ["hook"])]
        lines += [s.line(self.home) for s in install.apply(self.ctx(), ["hook"])]
        lines += [s.line(self.home) for s in install.uninstall(self.ctx(), ["hook"])]
        facts = json.dumps(install.status(self.ctx()))
        for text in lines + [facts]:
            self.assertNotIn("SECRET", text)
            self.assertNotIn("placeholder-value", text)

    def test_plan_changes_nothing(self) -> None:
        self.write_settings(REAL_LIKE)
        before = tree(self.root)
        steps = install.plan(self.ctx(), ["all"])
        self.assertEqual(tree(self.root), before)
        self.assertEqual(self.runner.launchctl(), [])
        self.assertTrue(any(s.component == "hook" and s.action == "update" for s in steps))

    def test_claude_version_warnings(self) -> None:
        self.write_settings({})
        cases = [((0, "2.1.290 (Claude Code)\n", ""), False), ((0, "2.1.139 (Claude Code)", ""), False),
                 ((0, "2.1.138 (Claude Code)", ""), True), ((0, "1.9.0", ""), True),
                 ((127, "", "not found"), True), ((0, "garbage", ""), True)]
        for answer, warns in cases:
            with self.subTest(answer=answer):
                runner = Recorder({("claude", "--version"): answer})
                steps = install.plan(self.ctx(runner=runner), ["hook"])
                found = [s for s in steps if s.action == "warn" and "Claude Code" in s.detail]
                self.assertEqual(bool(found), warns)

    def test_hook_python_override_and_default(self) -> None:
        cfg = Config(home=self.le_home, projects_dir=self.root / "p",
                     raw={"install": {"hook_python": "/opt/custom/python3"}})
        ctx = self.ctx(cfg=cfg)
        self.assertEqual(ctx.hook_python, "/opt/custom/python3")
        install.apply(ctx, ["hook"])
        handler = self.read_settings()["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(handler["command"], "/opt/custom/python3")
        self.assertEqual(handler["args"], [str(ROOT / "bin" / "ghosted"), "hook"])
        self.assertIs(handler["async"], True)
        import sys
        expected = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
        self.assertEqual(self.ctx().hook_python, expected)
        self.assertEqual(self.ctx().launchd_python, expected)

    def test_is_our_handler(self) -> None:
        yes = [{"type": "command", "command": "python3", "args": ["/a/b/bin/ghosted", "hook"]},
               {"command": "/usr/bin/python3", "args": ["${CLAUDE_PLUGIN_ROOT}/bin/ghosted", "hook"]},
               {"type": "command", "command": "py", "args": ["-I", "/x/bin/ghosted", "hook"]}]
        no = [{"type": "command", "command": "/x/bin/ghosted hook"},
              {"type": "command", "command": "python3", "args": ["/x/bin/ghosted", "run"]},
              {"type": "command", "command": "python3", "args": ["/x/bin/lever", "hook"]},
              {"type": "http", "url": "https://x", "args": ["/x/bin/ghosted", "hook"]},
              "not a dict", {"args": "bin/ghosted hook"}]
        for h in yes:
            self.assertTrue(install.is_our_handler(h), h)
        for h in no:
            self.assertFalse(install.is_our_handler(h), h)


class LinkTests(InstallCase):
    def test_bin_link_create_exists_and_uninstall(self) -> None:
        ctx = self.ctx()
        steps = install.apply(ctx, ["bin"])
        link = self.home / ".local" / "bin" / "ghosted"
        self.assertEqual(steps[0].action, "create")
        self.assertTrue(os.path.islink(str(link)))
        self.assertEqual(os.path.realpath(str(link)), str((ROOT / "bin" / "ghosted").resolve()))
        self.assertEqual(install.apply(ctx, ["bin"])[0].action, "exists")
        self.assertEqual(install.uninstall(ctx, ["bin"])[0].action, "remove")
        self.assertFalse(os.path.lexists(str(link)))
        self.assertEqual(install.uninstall(ctx, ["bin"])[0].action, "absent")

    def test_bin_is_one_command_and_the_link_runs_it(self) -> None:
        # contract 16.19: one name, `ghosted`; the old `le` and `loose-ends` links are gone
        ctx = self.ctx()
        steps = install.apply(ctx, ["bin"])
        self.assertEqual([s.action for s in steps], ["create"])
        self.assertEqual(sorted(p.name for p in (self.home / ".local" / "bin").iterdir()), ["ghosted"])
        proc = subprocess.run([sys.executable, str(self.home / ".local" / "bin" / "ghosted"), "--version"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("ghosted "), proc.stdout)
        self.assertEqual(install.status(ctx)["bin"]["state"], "ours")
        self.assertEqual(sorted(p.name for p in (ROOT / "bin").iterdir() if p.name != "le"), ["ghosted"])

    def test_bin_warns_when_another_ghosted_comes_first_on_path(self) -> None:
        other = self.root / "other-bin"
        other.mkdir()
        (other / "ghosted").write_text("#!/bin/sh\necho another program\n")
        os.chmod(str(other / "ghosted"), 0o755)
        ours_dir = str(self.home / ".local" / "bin")
        shadowed = self.ctx(path_env=str(other) + ":" + ours_dir)
        steps = install.apply(shadowed, ["bin"])
        warns = [s.detail for s in steps if s.action == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertIn(str(other / "ghosted"), warns[0])
        self.assertIn("run ~/.local/bin/ghosted instead", warns[0])
        self.assertEqual(install.status(shadowed)["bin"]["shadowed_by"], str(other / "ghosted"))
        # ours first on PATH, or no other ghosted at all: no warning
        fine = self.ctx(path_env=ours_dir + ":" + str(other))
        self.assertIsNone(install.other_program(fine))
        self.assertEqual([s.action for s in install.apply(fine, ["bin"])], ["exists"])
        self.assertIsNone(install.status(self.ctx())["bin"]["shadowed_by"])

    def test_bin_refuses_a_different_file_unless_forced(self) -> None:
        link = self.home / ".local" / "bin" / "ghosted"
        link.parent.mkdir(parents=True)
        link.write_text("#!/bin/sh\necho other ghosted\n")
        steps = install.apply(self.ctx(), ["bin"])
        self.assertEqual((steps[0].action, steps[0].ok), ("refuse", False))
        self.assertIn("--force", steps[0].detail)
        self.assertFalse(os.path.islink(str(link)))
        self.assertEqual(install.uninstall(self.ctx(), ["bin"])[0].action, "keep")
        self.assertTrue(link.exists())
        steps = install.apply(self.ctx(force=True), ["bin"])
        self.assertEqual((steps[0].action, steps[0].ok), ("replace", True))
        self.assertTrue(os.path.islink(str(link)))

    def test_bin_refuses_a_foreign_link_unless_forced_and_never_a_directory(self) -> None:
        link = self.home / ".local" / "bin" / "ghosted"
        link.parent.mkdir(parents=True)
        os.symlink("/usr/bin/true", str(link))
        self.assertEqual(install.apply(self.ctx(), ["bin"])[0].action, "refuse")
        self.assertEqual(install.apply(self.ctx(force=True), ["bin"])[0].action, "replace")
        os.unlink(str(link))
        link.mkdir()
        self.assertEqual(install.apply(self.ctx(force=True), ["bin"])[0].action, "refuse")

    def test_bin_warns_when_not_on_path(self) -> None:
        steps = install.plan(self.ctx(path_env="/usr/bin:/bin"), ["bin"])
        self.assertTrue(any(s.action == "warn" and "not on PATH" in s.detail for s in steps))
        steps = install.plan(self.ctx(), ["bin"])
        self.assertFalse(any(s.action == "warn" for s in steps))

    def test_skill_link_and_refusals(self) -> None:
        ctx = self.ctx()
        skills = self.home / ".claude" / "skills"
        main = skills / "ghosted"
        steps = install.apply(ctx, ["skill"])
        self.assertEqual([(s.action, s.path) for s in steps], [("create", str(main))])
        self.assertEqual(os.path.realpath(str(main)), str((ROOT / "core" / "skills" / "ghosted").resolve()))
        self.assertTrue((main / "SKILL.md").is_file())
        self.assertEqual(sorted(p.name for p in skills.iterdir()), ["ghosted"])  # no alias skill (contract 16.19)
        self.assertEqual([s.action for s in install.apply(ctx, ["skill"])], ["exists"])  # idempotent
        self.assertEqual(install.status(ctx, check_loaded=False)["skill"]["state"], "ours")
        self.assertEqual([s.action for s in install.uninstall(ctx, ["skill"])], ["remove"])
        self.assertFalse(os.path.lexists(str(main)))
        self.assertEqual(install.status(ctx, check_loaded=False)["skill"]["state"], "missing")
        # Someone else's directory at the name: refused, even with force.
        main.mkdir(parents=True)
        (main / "SKILL.md").write_text("someone else's skill")
        for force in (False, True):
            steps = install.apply(self.ctx(force=force), ["skill"])
            self.assertEqual([(s.action, s.ok) for s in steps], [("refuse", False)])
        self.assertEqual((main / "SKILL.md").read_text(), "someone else's skill")
        facts = install.status(ctx, check_loaded=False)["skill"]
        self.assertEqual((facts["state"], facts["path"]), ("not-a-link", str(main)))
        self.assertEqual([s.action for s in install.uninstall(ctx, ["skill"])], ["keep"])
        shutil.rmtree(str(main))
        # A link elsewhere is replaced only with force.
        other = self.root / "elsewhere"
        other.mkdir()
        os.symlink(str(other), str(main))
        self.assertEqual(install.apply(ctx, ["skill"])[0].action, "refuse")
        self.assertEqual(install.apply(self.ctx(force=True), ["skill"])[0].action, "replace")
        self.assertEqual(os.path.realpath(str(main)), str((ROOT / "core" / "skills" / "ghosted").resolve()))


class LaunchdTests(InstallCase):
    def test_rendered_plist(self) -> None:
        ctx = self.ctx()
        data = plistlib.loads(install.render_plist(ctx).encode("utf-8"))
        self.assertEqual(data["Label"], "com.ghosted.run")
        self.assertEqual(data["ProgramArguments"], [ctx.launchd_python, str(ROOT / "bin" / "ghosted"), "run"])
        # Every hour of the system clock: `ghosted run` (GHOSTED_SCHEDULED=1) keeps 07-23 in the
        # configured zone, so a Mac on another timezone still reports during the Chicago day.
        self.assertEqual(data["StartCalendarInterval"], [{"Hour": h, "Minute": 2} for h in range(24)])
        log = str(self.le_home / "logs" / "run.log")
        self.assertEqual((data["StandardOutPath"], data["StandardErrorPath"]), (log, log))
        self.assertEqual(data["EnvironmentVariables"],
                         {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "HOME": str(self.home),
                          "GHOSTED_SCHEDULED": "1"})
        self.assertIs(data["RunAtLoad"], False)
        self.assertEqual(data["ProcessType"], "Background")

    def test_ghosted_home_only_when_not_the_default(self) -> None:
        custom = self.root / "elsewhere & <odd>"
        ctx = self.ctx(le_home=custom)
        data = plistlib.loads(install.render_plist(ctx).encode("utf-8"))
        self.assertEqual(data["EnvironmentVariables"]["GHOSTED_HOME"], str(custom))
        self.assertEqual(data["StandardOutPath"], str(custom / "logs" / "run.log"))

    def test_launchd_python_override(self) -> None:
        cfg = Config(home=self.le_home, projects_dir=self.root / "p",
                     raw={"install": {"launchd_python": "/opt/homebrew/bin/python3"}})
        data = plistlib.loads(install.render_plist(self.ctx(cfg=cfg)).encode("utf-8"))
        self.assertEqual(data["ProgramArguments"][0], "/opt/homebrew/bin/python3")

    def test_apply_writes_and_loads_then_is_idempotent(self) -> None:
        runner = Recorder({("launchctl", "print"): (113, "", "Could not find service")})
        ctx = self.ctx(runner=runner)
        steps = install.apply(ctx, ["launchd"])
        self.assertTrue(all(s.ok for s in steps), [s.line() for s in steps])
        plist = self.home / "Library" / "LaunchAgents" / "com.ghosted.run.plist"
        self.assertEqual(plist.read_text(encoding="utf-8"), install.render_plist(ctx))
        self.assertEqual(stat.S_IMODE(os.stat(str(plist)).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.le_home / "logs")).st_mode), 0o700)
        self.assertEqual(runner.launchctl(), [
            ["launchctl", "bootout", "gui/501/com.ghosted.run"],
            ["launchctl", "bootstrap", "gui/501", str(plist)],
        ])
        loaded = Recorder()  # launchctl print answers 0: loaded
        steps = install.apply(self.ctx(runner=loaded), ["launchd"])
        self.assertEqual(loaded.launchctl(), [["launchctl", "print", "gui/501/com.ghosted.run"]])
        self.assertFalse(any(s.changed for s in steps))

    def test_bootstrap_failure_is_reported(self) -> None:
        runner = Recorder({("launchctl", "bootstrap"): (5, "", "Bootstrap failed: 5: Input/output error\n"),
                           ("launchctl", "print"): (113, "", "")})
        steps = install.apply(self.ctx(runner=runner), ["launchd"])
        self.assertFalse(steps[-1].ok)
        self.assertIn("Bootstrap failed: 5", steps[-1].detail)

    def test_uninstall_boots_out_and_removes(self) -> None:
        install.apply(self.ctx(), ["launchd"])
        runner = Recorder()
        steps = install.uninstall(self.ctx(runner=runner), ["launchd"])
        self.assertEqual(runner.launchctl(), [["launchctl", "bootout", "gui/501/com.ghosted.run"]])
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.ghosted.run.plist").exists())
        self.assertEqual([s.action for s in steps], ["unload", "remove"])

    def test_dry_run_uninstall_runs_nothing(self) -> None:
        install.apply(self.ctx(), ["launchd"])
        runner = Recorder()
        before = tree(self.root)
        install.uninstall(self.ctx(runner=runner), ["all"], dry_run=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(tree(self.root), before)


class ContextAndStatusTests(InstallCase):
    def test_select(self) -> None:
        self.assertEqual(install.select(["all"]), list(install.COMPONENTS))
        self.assertEqual(install.select(["launchd", "bin", "bin"]), ["bin", "launchd"])
        with self.assertRaises(InstallError):
            install.select(["everything"])

    def test_status_reports_each_component(self) -> None:
        ctx = self.ctx()
        facts = install.status(ctx)
        self.assertEqual(facts["bin"]["state"], "missing")
        self.assertEqual(facts["hook"]["state"], "missing")
        self.assertFalse(facts["launchd"]["plist"])
        self.write_settings(REAL_LIKE)
        install.apply(ctx, ["all"])
        facts = install.status(ctx)
        self.assertEqual((facts["bin"]["state"], facts["skill"]["state"]), ("ours", "ours"))
        self.assertTrue(facts["bin"]["on_path"])
        self.assertEqual(facts["hook"]["state"], "registered")
        self.assertTrue(facts["hook"]["matches_repo"])
        self.assertEqual(facts["hook"]["events"], {"Stop": True, "StopFailure": True})
        self.assertTrue(facts["launchd"]["plist"] and facts["launchd"]["matches"])
        self.assertIs(facts["launchd"]["loaded"], True)

    def test_step_lines(self) -> None:
        step = install.Step("bin", "create", str(self.home / ".local/bin/ghosted"), "-> repo", changed=True)
        self.assertEqual(step.line(self.home), "bin      create    ~/.local/bin/ghosted: -> repo")
        self.assertEqual(step.line(self.home, dry_run=True), "bin      would create ~/.local/bin/ghosted: -> repo")


def frontmatter(text: str):
    """A tiny parser for SKILL.md frontmatter: flat `key: value` lines between --- markers.

    Also enforces what makes a YAML plain scalar safe, since Claude Code drops every field
    silently when the YAML doesn't parse (and validate --strict skips custom skill paths)."""
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("frontmatter must open on line 1")
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        m = re.match(r"^([a-z][a-z0-9_-]*): (.+)$", line)
        if not m:
            raise ValueError("not a flat key: value line")
        key, value = m.group(1), m.group(2)
        if key in fields:
            raise ValueError("duplicate key %s" % key)
        quoted = re.fullmatch(r'"([^"\\]*)"', value)
        if quoted:  # a double-quoted scalar with no escapes or inner quotes is safe too
            fields[key] = quoted.group(1)
            continue
        if value[0] in "[]{}>|*&!%@`'\"#,?:-" or ": " in value or " #" in value or value != value.strip():
            raise ValueError("value of %s is not a safe plain scalar" % key)
        fields[key] = value
    return fields, "\n".join(lines[end + 1:])


SKILL_LE = "${CLAUDE_SKILL_DIR}/../../../bin/ghosted"


class PluginFilesTests(unittest.TestCase):
    # contract 16.18: the skill runs its own copy by full path, never a bare `ghosted` from PATH
    LE = SKILL_LE
    ALLOWED = ", ".join(["Bash(%s %s:*)" % (SKILL_LE, verb) for verb in ("report", "receipt", "done", "dismiss", "snooze")])

    def test_the_skill_path_reaches_bin_ghosted(self) -> None:
        skill_dir = ROOT / "core" / "skills" / "ghosted"
        reached = Path(os.path.normpath(self.LE.replace("${CLAUDE_SKILL_DIR}", str(skill_dir))))
        self.assertEqual(reached, ROOT / "bin" / "ghosted")
        body = self.skill("ghosted")[1]
        self.assertIn("    " + self.LE + "\n", body)           # the path on a line of its own
        self.assertIn("another program on this computer may have that name", body)
        self.assertIn("begins with a dollar sign", body)       # an older Claude Code that fills nothing in
        for line in body.splitlines():                          # no instruction runs a bare `ghosted`
            self.assertNotRegex(line, r"run `ghosted (report|done|dismiss|snooze)", line)
            self.assertNotRegex(line, r"-> (run )?`ghosted ", line)

    def test_there_is_one_skill(self) -> None:
        # contract 16.19: /ghosted only; the /sweep and /loose-ends names are gone
        self.assertEqual(sorted(p.name for p in (ROOT / "core" / "skills").iterdir()), ["ghosted"])

    def skill(self, name: str):
        return frontmatter((ROOT / "core" / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))

    def test_skill_frontmatter(self) -> None:
        fields, body = self.skill("ghosted")
        self.assertEqual(fields["name"], "ghosted")
        self.assertEqual(fields["allowed-tools"], self.ALLOWED)
        self.assertEqual(set(fields), {"name", "description", "argument-hint", "allowed-tools"})
        for phrase in ("/ghosted", "what did I ghost?", "any loose ends?", "what's waiting on me", "ghosted report"):
            self.assertIn(phrase, fields["description"])
        self.assertLessEqual(len(fields["description"]), 1536)
        # contract 16.4: the command, output as printed, the follow-ups, the fallbacks
        self.assertIn("    " + self.LE + " report <args> --via skill --markdown\n", body)
        self.assertIn("`GHOSTED report --via skill --markdown` with **no window at all**", body)
        self.assertIn("exactly as printed", body)
        self.assertIn("not inside\na code block", body)          # contract 16.17: Markdown, relayed as is
        self.assertNotIn("in a code block", body)
        for follow in ("`GHOSTED done 7`", "`GHOSTED dismiss 7`", "`GHOSTED snooze 7 3d`"):
            self.assertIn(follow, body)
        self.assertIn("Python is not installed", body)          # audit 2026-11-11: its own row, not "reinstall"
        self.assertIn("No such file or directory", body)
        self.assertIn("/plugin install ghosted --marketplace byndp/ghosted", body)
        self.assertIn("bin/ghosted install --skill", body)
        self.assertIn("GHOSTED report --no-quotes --via skill --markdown", body)
        self.assertIn("run this in a terminal", body)           # the full path: a plugin install has no bare command
        self.assertIn("PermissionError", body)
        self.assertIn("$ARGUMENTS", body)

    def test_frontmatter_parser_catches_yaml_traps(self) -> None:
        for bad in ("---\nname: a: b\n---\n", "no frontmatter\n", "---\ndescription: 'quoted\n---\n",
                    "---\nname: x #comment\n---\n"):
            with self.assertRaises(ValueError):
                frontmatter(bad)

    def test_hooks_json_plugin_shape(self) -> None:
        data = json.loads((ROOT / "core" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("description"), str)
        self.assertEqual(set(data["hooks"]), {"Stop", "StopFailure"})
        for event in ("Stop", "StopFailure"):
            self.assertEqual(data["hooks"][event], [{"hooks": [{
                "type": "command", "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/bin/ghosted", "hook"],
                "async": True}]}])
            self.assertTrue(install.is_our_handler(data["hooks"][event][0]["hooks"][0]))

    def test_plugin_manifest(self) -> None:
        data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "ghosted")
        self.assertEqual(data["version"], "0.2.0")
        self.assertEqual(data["author"], {"name": "Studio 2133", "url": "https://github.com/byndp"})
        self.assertEqual(data["license"], "MIT")
        self.assertTrue((ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"))
        self.assertEqual(data["skills"], "./core/skills/")
        # Installing the plugin has no side effects: the hook is opt-in (ghosted init / ghosted install --hook),
        # so the manifest names no hooks and there is no auto-discovered hooks/hooks.json at the root.
        self.assertNotIn("hooks", data)
        self.assertFalse((ROOT / "hooks" / "hooks.json").exists())
        self.assertTrue(data["description"])
        self.assertTrue((ROOT / data["skills"]).is_dir())
        self.assertTrue((ROOT / data["skills"] / "ghosted" / "SKILL.md").is_file())
        # bin/ghosted at the plugin root is what puts `ghosted` on PATH when the plugin is installed
        self.assertTrue(os.access(str(ROOT / "bin" / "ghosted"), os.X_OK))

    def test_marketplace_manifest(self) -> None:
        data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "ghosted")
        self.assertEqual(data["owner"], {"name": "byndp"})
        (entry,) = data["plugins"]
        self.assertEqual((entry["name"], entry["source"]), ("ghosted", "./"))
        self.assertTrue(entry["description"])

    def test_plist_template_placeholders(self) -> None:
        text = install.TEMPLATE.read_text(encoding="utf-8")
        found = set(re.findall(r"\{\{[A-Z_]+\}\}", text))
        self.assertEqual(found, {"{{LABEL}}", "{{PYTHON}}", "{{LE}}", "{{LOG}}", "{{PATH}}", "{{HOME}}",
                                 "{{EXTRA_ENV}}"})
        for comment in re.findall(r"<!--(.*?)-->", text, re.S):
            self.assertNotIn("--", comment)  # XML forbids "--" inside a comment


if __name__ == "__main__":
    unittest.main()
