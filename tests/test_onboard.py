"""core.onboard: the guided `ghosted init` (contract 16.5).

Every path is scripted: a fake io answers the questions in order (running out of answers is an
EOF), and fake effects stand in for install, the adapters, the platform checks and the clipboard.
Nothing touches the real HOME, launchctl, osascript or the network.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from core import config, onboard
from core.install import Step

TOPIC = "ghosted-" + "T" * 20 + "opicSecret12"  # 35 chars: the v1 shape
URL = "https://hc-ping.com/1b2c3d4e-secret-path"


class ScriptIO:
    """Answers in order; past the end, an EOF (what Ctrl-D does at a real prompt)."""

    def __init__(self, answers: List[Any]) -> None:
        self.answers = list(answers)
        self.prompts: List[str] = []
        self.said: List[str] = []

    def ask(self, prompt: str, default: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer.strip() or default

    def say(self, text: str = "") -> None:
        self.said.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.prompts + self.said)


class FakeAdapter:
    def __init__(self, name: str, ok: bool = True, error: Optional[str] = None) -> None:
        self.name, self.ok, self.error = name, ok, error
        self.sent: List[Dict[str, Any]] = []

    def send(self, items: Any, *, title: Any, body: Any, kind: str, priority: str = "normal",
             click_url: Any = None) -> Any:
        self.sent.append({"items": items, "title": title, "body": body, "kind": kind})
        return SimpleNamespace(ok=self.ok, channel=self.name, error=self.error)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.user_home = root / "home"
        self.user_home.mkdir()
        self.le_home = root / "ghosted"
        self.env = {"HOME": str(self.user_home), "GHOSTED_HOME": str(self.le_home)}
        self.installs: List[List[str]] = []
        self.install_result: Dict[str, List[Step]] = {}
        self.adapters: Dict[str, FakeAdapter] = {}
        self.built: List[Any] = []
        self.mac = True
        self.hook_on = False
        self.hook_stale = False
        self.job_on = False
        self.copied: List[str] = []
        self.clipboard = True
        self.topics = iter(["ghosted-" + c * 32 for c in "abcdefgh"])
        self.claude_path: Optional[str] = None  # `claude` not found unless a test sets it

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @property
    def path(self) -> Path:
        return self.le_home / "config.toml"

    def effects(self) -> onboard.Effects:
        def install(names: List[str]) -> List[Step]:
            self.installs.append(list(names))
            name = names[0]
            return self.install_result.get(name) or [
                Step(name, "create", str(self.user_home / ".claude" / "settings.json"), "added", changed=True)]

        def adapter_for(cfg: Any, env: Any) -> Any:
            self.built.append(cfg)
            return self.adapters.setdefault(cfg.adapter, FakeAdapter(cfg.adapter))

        def copy(text: str) -> bool:
            self.copied.append(text)
            return self.clipboard

        return onboard.Effects(install=install, adapter_for=adapter_for, mac_available=lambda: self.mac,
                               hook_installed=lambda: self.hook_on, launchd_installed=lambda: self.job_on,
                               copy=copy, new_topic=lambda: next(self.topics),
                               hook_stale=lambda: self.hook_stale,
                               find_claude=lambda: self.claude_path)

    def run_onboard(self, answers: List[Any], io: Any = None) -> "tuple[int, ScriptIO]":
        script = ScriptIO(answers)
        code = onboard.run(SimpleNamespace(env=self.env), io if io is not None else script, self.effects())
        return code, script

    def cfg(self) -> config.Config:
        return config.load_config(environ=self.env)

    def write(self, text: str) -> None:
        self.le_home.mkdir(mode=0o700, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def assertPlain(self, io: ScriptIO) -> None:
        self.assertNotIn("!", io.text)

    def assertNoSecrets(self, io: ScriptIO, *secrets: str) -> None:
        for secret in secrets:
            self.assertNotIn(secret, io.text)


EXISTING = """\
# my own notes up here
timezone = "America/Chicago"   # keep this comment
workspace_roots = ["~/code"]

[detect]
# a comment inside a table
ignore_tail_patterns = ['(?i)shall we\\?\\s*$']

[notify]
adapter = "none"   # my note about pushes

[notify.ntfy]
server = "https://ntfy.sh"
topic = "%s"

[push]
daily_budget = 2
""" % TOPIC


class FreshHomeTests(Base):
    def test_fresh_home_with_a_workspace_writes_defaults_and_the_hook(self) -> None:
        (self.user_home / "workspace").mkdir()
        code, io = self.run_onboard(["y", "y", ""])
        self.assertEqual(code, 0)
        cfg = self.cfg()
        self.assertEqual(cfg.workspace_roots, [self.user_home / "workspace"])
        self.assertEqual(cfg.adapter, "none")
        self.assertRegex(cfg.ntfy_topic, r"^ghosted-[A-Za-z0-9_-]{32}$")
        self.assertEqual((cfg.direct_questions, cfg.watchdog_ping_url), ("waiting", ""))
        self.assertEqual(cfg.env_file, self.le_home / ".env")
        self.assertEqual(stat.S_IMODE(os.stat(str(self.path)).st_mode), 0o600)
        self.assertEqual(self.installs, [["hook"]])  # no push channel: no hourly job offered
        self.assertIn("a small script Claude Code runs when a session stops", io.text)
        self.assertIn("Your projects live in ~/workspace? [Y/n]", io.prompts)
        self.assertIn("To remove everything Ghosted installed: ghosted uninstall --all", io.said)
        self.assertIn("Done. Installed this time: the hook.", io.said)
        self.assertIn("Pushes are off.", io.said)
        self.assertNoSecrets(io, cfg.ntfy_topic)
        self.assertPlain(io)
        self.assertIsNone(config.minitoml_problem(self.path.read_text()))

    def test_fresh_home_without_a_workspace_asks_for_the_folder_once(self) -> None:
        code, io = self.run_onboard(["", "n", ""])
        self.assertEqual(code, 0)
        self.assertEqual([p for p in io.prompts if "folder" in p.lower()],
                         ["Which folder holds your projects? (e.g. ~/code; Enter to skip)"])
        self.assertEqual(self.cfg().workspace_roots, [])
        self.assertEqual(self.installs, [])
        self.assertIn("Skipped. Add it later with ghosted install --hook.", io.said)

    def test_a_named_folder_that_exists_becomes_the_root(self) -> None:
        (self.user_home / "code").mkdir()
        code, _ = self.run_onboard(["~/code", "n", ""])
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg().workspace_roots, [self.user_home / "code"])

    def test_eof_at_the_first_question_writes_nothing(self) -> None:
        (self.user_home / "workspace").mkdir()
        code, io = self.run_onboard([])
        self.assertEqual(code, 1)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.le_home.exists())
        self.assertIn("Stopped. What was already saved stays; the rest is unchanged.", io.said)

    def test_ctrl_c_at_the_hook_question_keeps_a_complete_config(self) -> None:
        code, io = self.run_onboard(["", KeyboardInterrupt()])
        self.assertEqual(code, 1)
        self.assertEqual(self.cfg().adapter, "none")  # the defaults file is whole and valid
        self.assertEqual(self.installs, [])
        self.assertEqual([p.name for p in self.le_home.iterdir()], ["config.toml"])  # no temp files

    def test_a_stale_hook_is_offered_a_refresh(self) -> None:
        (self.user_home / "workspace").mkdir()
        self.hook_stale = True
        code, io = self.run_onboard(["y", "y", ""])
        self.assertEqual(code, 0, io.text)
        self.assertNotIn("Live record: on (the hook is already installed).", io.said)
        self.assertIn("Point it at this copy? [Y/n]", io.prompts)
        self.assertEqual(self.installs, [["hook"]])
        self.assertIn("Done. Installed this time: the hook (refreshed).", io.said)
        self.assertPlain(io)

    def test_a_stale_hook_can_be_left(self) -> None:
        (self.user_home / "workspace").mkdir()
        self.hook_stale = True
        code, io = self.run_onboard(["y", "n", ""])
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.installs, [])
        self.assertIn("Left as it is. Refresh it later with ghosted install --hook.", io.said)

    def test_default_effects_tell_a_stale_hook_from_an_installed_one(self) -> None:
        from core import install
        settings = self.user_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        stale = {"type": "command", "command": "/usr/bin/python3",
                 "args": ["/gone/plugins/cache/ghosted/ghosted/0.2.0/bin/ghosted", "hook"], "async": True}
        groups = {e: [{"hooks": [stale]}] for e in install.HOOK_EVENTS}
        settings.write_text(json.dumps({"hooks": groups}), encoding="utf-8")
        fx = onboard.default_effects(self.env)
        self.assertFalse(fx.hook_installed())
        self.assertTrue(fx.hook_stale())
        install.apply(install.make_context(cfg=self.cfg(), environ=self.env), ["hook"])
        fx = onboard.default_effects(self.env)
        self.assertTrue(fx.hook_installed())
        self.assertFalse(fx.hook_stale())

    def test_a_dict_io_works_too(self) -> None:
        script = ScriptIO(["", "n", ""])
        code, _ = self.run_onboard([], io={"ask": script.ask, "say": script.say})
        self.assertEqual(code, 0)
        self.assertTrue(self.path.exists())


class ExistingConfigTests(Base):
    def test_ntfy_end_to_end_keeps_every_other_line(self) -> None:
        self.write(EXISTING)
        self.hook_on = True
        # pick ntfy, Enter once subscribed, it arrived, install the job, a watchdog URL
        code, io = self.run_onboard(["2", "", "y", "y", URL])
        self.assertEqual(code, 0, io.text)
        text = self.path.read_text()
        old, new = EXISTING.split("\n"), text.split("\n")
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(changed, [('adapter = "none"   # my note about pushes',
                                    'adapter = "ntfy"   # my note about pushes')])
        self.assertEqual(new[len(old) - 1:], ["", "[watchdog]", 'ping_url = "%s"' % URL, ""])
        cfg = self.cfg()
        self.assertEqual((cfg.adapter, cfg.ntfy_topic, cfg.watchdog_ping_url), ("ntfy", TOPIC, URL))
        self.assertEqual(self.installs, [["launchd"]])  # the hook was already on
        self.assertEqual(self.copied, [TOPIC])  # the existing topic is kept: the phone may follow it
        self.assertEqual(self.adapters["ntfy"].sent[0]["kind"], "test")
        self.assertIn("Live record: on (the hook is already installed).", io.said)
        self.assertIn("Did it arrive? [y/n]", io.prompts)
        self.assertIn("Watchdog on: the hourly job pings hc-ping.com.", io.said)
        self.assertIn("ghosted-TT…", io.text)
        self.assertNoSecrets(io, TOPIC, "1b2c3d4e-secret-path")
        self.assertPlain(io)

    def test_a_blank_topic_gets_a_new_one_written_before_the_test(self) -> None:
        self.write(EXISTING.replace(TOPIC, ""))
        self.clipboard = False
        code, io = self.run_onboard(["n", "ntfy", "", "y", "n"])
        self.assertEqual(code, 0, io.text)
        cfg = self.cfg()
        self.assertEqual(cfg.ntfy_topic, "ghosted-" + "a" * 32)
        self.assertEqual(cfg.adapter, "ntfy")
        self.assertIn("under [notify.ntfy] (it starts ghosted-aa…)", io.text)
        self.assertNoSecrets(io, cfg.ntfy_topic)

    def test_existing_config_is_used_without_the_workspace_question(self) -> None:
        self.write(EXISTING)
        (self.user_home / "workspace").mkdir()
        code, io = self.run_onboard(["n", ""])
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_text(), EXISTING)
        self.assertFalse(any("workspace" in p for p in io.prompts))
        self.assertIn("Pushes stay off.", io.said)

    def test_a_broken_config_stops_and_is_left_alone(self) -> None:
        self.write('[notify]\nadapter = "carrier-pigeon"\n')
        code, io = self.run_onboard(["y"])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_text(), '[notify]\nadapter = "carrier-pigeon"\n')
        self.assertTrue(any("has a problem" in line for line in io.said))
        self.assertEqual(self.installs, [])

    def test_choosing_none_turns_pushes_off(self) -> None:
        self.write(EXISTING.replace('adapter = "none"', 'adapter = "ntfy"'))
        self.hook_on = self.job_on = True
        code, io = self.run_onboard(["5"])
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg().adapter, "none")
        self.assertIn("Pushes are now off.", io.said)
        self.assertEqual(self.adapters, {})  # no test push for None

    def test_enter_keeps_a_push_channel_and_still_offers_the_job(self) -> None:
        self.write(EXISTING.replace('adapter = "none"', 'adapter = "ntfy"'))
        self.hook_on = True
        code, io = self.run_onboard(["", "n"])
        self.assertEqual(code, 0)
        self.assertIn("Pick a number (Enter keeps pushes on ntfy):", io.prompts)
        self.assertIn("Install it now? [Y/n]", io.prompts)
        self.assertEqual(self.installs, [])
        self.assertEqual(self.cfg().adapter, "ntfy")


class ChannelTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.write(EXISTING)
        self.hook_on = True

    def test_the_picker_lists_the_five_channels_in_order(self) -> None:
        code, io = self.run_onboard([""])
        self.assertEqual(code, 0)
        listed = [line.strip() for line in io.said if line.strip()[:2] in ("1.", "2.", "3.", "4.", "5.")]
        self.assertEqual([line.split(":")[0] for line in listed],
                         ["1. Claude app", "2. ntfy", "3. Pushover", "4. Mac notification only", "5. None"])
        self.assertIn("1. Claude app: needs Claude Code's claude command", listed)
        self.assertEqual(listed[2], "3. Pushover: $5 phone app; add PUSHOVER_TOKEN and PUSHOVER_USER to %s first"
                         % (self.le_home / ".env"))

    def test_claude_app_needs_the_claude_command(self) -> None:
        code, io = self.run_onboard(["1", ""])
        self.assertEqual(code, 0)
        self.assertIn("Claude app is not available yet: needs Claude Code's claude command.", io.said)
        self.assertEqual(self.adapters, {})
        self.assertEqual(self.cfg().adapter, "none")

    def test_claude_app_and_ntfy_show_their_pros_and_cons(self) -> None:
        self.claude_path = "/opt/bin/claude"
        code, io = self.run_onboard([""])
        self.assertEqual(code, 0)
        said = [line.strip() for line in io.said]
        claude_at = said.index("1. Claude app: pushes to the Claude app already on your phone")
        ntfy_at = next(i for i, line in enumerate(said) if line.startswith("2. ntfy"))
        claude_lines = " ".join(said[claude_at + 1:ntfy_at])
        for words in ("+ no new app or account", "- needs a claude.ai sign-in", "Haiku turn",
                      "undocumented", "Remote control enabled"):
            self.assertIn(words, claude_lines)
        pushover_at = next(i for i, line in enumerate(said) if line.startswith("3. Pushover"))
        ntfy_lines = " ".join(said[ntfy_at + 1:pushover_at])
        for words in ("+ free, no account", "- install the ntfy app", "anyone with your topic"):
            self.assertIn(words, ntfy_lines)
        self.assertPlain(io)

    def test_claude_app_chosen_after_the_trust_step(self) -> None:
        self.claude_path = "/opt/bin/claude"
        code, io = self.run_onboard(["claude", "", "y", "n"])
        self.assertEqual(code, 0, io.text)
        cfg = self.cfg()
        self.assertEqual(cfg.adapter, "claude-app")
        self.assertEqual(cfg.claude_app_path, "/opt/bin/claude")
        notifier_dir = self.le_home / "notifier"
        self.assertTrue(notifier_dir.is_dir())
        self.assertEqual(stat.S_IMODE(notifier_dir.stat().st_mode), 0o700)
        self.assertIn("cd %s && claude" % notifier_dir, io.text)
        self.assertIn("Press Enter once Claude Code trusts the folder.", io.prompts)
        sent = self.adapters["claude-app"].sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "test")
        self.assertIn("Pushes are now on the Claude app.", io.said)
        self.assertIn("ntfy stays the backup", io.text)  # EXISTING has a topic

    def test_claude_app_did_not_arrive_explains_focus_and_offers_ntfy(self) -> None:
        self.claude_path = "/opt/bin/claude"
        code, io = self.run_onboard(["1", "", "n", "y", "", "y", "n"])
        self.assertEqual(code, 0, io.text)
        self.assertIn("Focus", io.text)
        self.assertIn("Try ntfy instead? [Y/n]", io.prompts)
        self.assertEqual(self.cfg().adapter, "ntfy")

    def test_pushover_without_keys_is_not_selectable(self) -> None:
        code, io = self.run_onboard(["3", ""])
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("Pushover is not available yet:") for line in io.said))
        self.assertEqual(self.adapters, {})

    def test_pushover_with_keys_is_tested_and_chosen(self) -> None:
        (self.le_home / ".env").write_text("PUSHOVER_TOKEN=tok-secret\nPUSHOVER_USER=usr-secret\n")
        code, io = self.run_onboard(["3", "y", "n"])
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.cfg().adapter, "pushover")
        self.assertEqual(len(self.adapters["pushover"].sent), 1)
        self.assertNoSecrets(io, "tok-secret", "usr-secret")

    def test_mac_only_when_available(self) -> None:
        self.mac = False
        code, io = self.run_onboard(["4", ""])
        self.assertIn("Mac notification only is not available yet: needs macOS.", io.said)
        self.assertEqual(self.cfg().adapter, "none")

    def test_mac_chosen(self) -> None:
        code, io = self.run_onboard(["mac", "y", "n"])
        self.assertEqual(code, 0, io.text)
        self.assertEqual(self.cfg().adapter, "macos")
        self.assertIn("Pushes are now on Mac notifications.", io.said)

    def test_a_no_offers_the_next_selectable_option(self) -> None:
        # ntfy did not arrive -> Pushover has no keys, so Mac is next -> it arrived
        code, io = self.run_onboard(["2", "", "n", "y", "y", "n"])
        self.assertEqual(code, 0, io.text)
        self.assertIn("Try Mac notification only instead? [Y/n]", io.prompts)
        self.assertEqual(self.cfg().adapter, "macos")
        self.assertEqual(len(self.adapters["ntfy"].sent), 1)
        self.assertEqual(len(self.adapters["macos"].sent), 1)

    def test_a_failed_send_offers_the_next_option_without_asking_if_it_arrived(self) -> None:
        self.adapters["ntfy"] = FakeAdapter("ntfy", ok=False, error="ntfy https://ntfy.sh unreachable: timed out")
        code, io = self.run_onboard(["2", "", "y", "y", "n"])
        self.assertEqual(code, 0, io.text)
        self.assertIn("The test push did not go out: ntfy https://ntfy.sh unreachable: timed out.", io.said)
        self.assertEqual(io.prompts.count("Did it arrive? [y/n]"), 1)  # only for the Mac test
        self.assertEqual(self.cfg().adapter, "macos")

    def test_no_and_nothing_left_keeps_pushes_as_they_were(self) -> None:
        self.mac = False
        code, io = self.run_onboard(["2", "", "n", "y"])
        self.assertEqual(code, 0, io.text)
        self.assertIn("Nothing else to try. Keep pushes off? [Y/n]", io.prompts)
        self.assertEqual(self.cfg().adapter, "none")

    def test_declining_the_next_option_goes_back_to_the_picker(self) -> None:
        code, io = self.run_onboard(["2", "", "n", "n", "5"])
        self.assertEqual(code, 0, io.text)
        self.assertEqual(sum(1 for p in io.prompts if p.startswith("Pick a number")), 2)
        self.assertEqual(self.cfg().adapter, "none")

    def test_ctrl_c_at_did_it_arrive_leaves_the_adapter_unchanged(self) -> None:
        before = self.path.read_text()
        code, io = self.run_onboard(["4", KeyboardInterrupt()])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_text(), before)

    def test_a_blank_answer_to_did_it_arrive_is_asked_again(self) -> None:
        code, io = self.run_onboard(["4", "", "y", "n"])
        self.assertEqual(code, 0, io.text)
        self.assertEqual(io.prompts.count("Did it arrive? [y/n]"), 2)
        self.assertEqual(self.cfg().adapter, "macos")

    def test_nonsense_is_asked_again_and_gives_up_after_a_few_rounds(self) -> None:
        code, io = self.run_onboard(["9"] * onboard.MAX_ROUNDS)
        self.assertEqual(code, 0)
        self.assertIn("Type a number from 1 to 5.", io.said)
        self.assertEqual(self.cfg().adapter, "none")


class HourlyJobTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.write(EXISTING)
        self.hook_on = True

    def test_a_launchd_refusal_is_shown_as_it_is_and_skips_the_watchdog(self) -> None:
        refusal = ("3 of the 10 hand-labelled clean sessions are labelled in ~/.ghosted/fixtures/real-manifest.json; "
                   "label them first")
        self.install_result["launchd"] = [Step("launchd", "refuse", str(self.user_home / "Library" / "x.plist"),
                                               refusal, ok=False)]
        code, io = self.run_onboard(["4", "y", "y"])
        self.assertEqual(code, 0, io.text)
        self.assertTrue(any(refusal in line and "refuse" in line for line in io.said))
        self.assertFalse(any("ping URL" in p for p in io.prompts))
        self.assertIn("Done. Installed this time: nothing new.", io.said)

    def test_a_bad_watchdog_url_is_explained_and_asked_again(self) -> None:
        code, io = self.run_onboard(["4", "y", "y", "http://hc-ping.com/plain", ""])
        self.assertEqual(code, 0, io.text)
        self.assertIn("The ping URL must be an https:// URL.", io.said)
        self.assertEqual(self.cfg().watchdog_ping_url, "")
        self.assertIn("2 7-23 * * *", io.text)
        self.assertIn("Done. Installed this time: the hourly job.", io.said)

    def test_an_existing_watchdog_is_reported_by_host_only(self) -> None:
        self.write(EXISTING + '\n[watchdog]\nping_url = "%s"\n' % URL)
        self.job_on = True
        code, io = self.run_onboard(["4", "y"])
        self.assertEqual(code, 0, io.text)
        self.assertIn("Watchdog: on (pings hc-ping.com).", io.said)
        self.assertNoSecrets(io, "secret-path")


class SetValueTests(unittest.TestCase):
    def test_replaces_the_value_and_keeps_the_comment(self) -> None:
        text = '[notify]\nadapter = "none"   # a # note\n'
        self.assertEqual(onboard.set_value(text, "notify", "adapter", '"ntfy"'),
                         '[notify]\nadapter = "ntfy"   # a # note\n')

    def test_a_hash_inside_the_old_string_is_not_a_comment(self) -> None:
        text = '[watchdog]\nping_url = "https://x.example/a#b"\n'
        self.assertEqual(onboard.set_value(text, "watchdog", "ping_url", '"https://y.example/"'),
                         '[watchdog]\nping_url = "https://y.example/"\n')

    def test_only_the_named_table_is_edited(self) -> None:
        text = '[notify.ntfy]\ntopic = "a"\n\n[notify]\nadapter = "none"\n'
        out = onboard.set_value(text, "notify.ntfy", "topic", '"b"')
        self.assertEqual(out, '[notify.ntfy]\ntopic = "b"\n\n[notify]\nadapter = "none"\n')
        self.assertEqual(onboard.set_value(text, "notify", "topic", '"c"'),
                         '[notify.ntfy]\ntopic = "a"\n\n[notify]\nadapter = "none"\ntopic = "c"\n')

    def test_a_missing_key_goes_after_the_table_settings(self) -> None:
        text = '[notify]\n# pushes\nserver_x = 1\n\n# next table\n[push]\ndaily_budget = 2\n'
        self.assertEqual(onboard.set_value(text, "notify", "adapter", '"macos"'),
                         '[notify]\n# pushes\nserver_x = 1\nadapter = "macos"\n\n# next table\n[push]\n'
                         'daily_budget = 2\n')

    def test_a_missing_table_is_appended(self) -> None:
        self.assertEqual(onboard.set_value('timezone = "UTC"', "watchdog", "ping_url", '"https://a/"'),
                         'timezone = "UTC"\n\n[watchdog]\nping_url = "https://a/"\n')
        self.assertEqual(onboard.set_value("", "notify", "adapter", '"ntfy"'), '[notify]\nadapter = "ntfy"\n')

    def test_spaced_headers_and_array_tables(self) -> None:
        text = '[[notify]]\nadapter = "x"\n[ notify ]\nadapter = "none"\n'
        self.assertEqual(onboard.set_value(text, "notify", "adapter", '"ntfy"'),
                         '[[notify]]\nadapter = "x"\n[ notify ]\nadapter = "ntfy"\n')

    def test_a_multi_line_value_is_refused(self) -> None:
        with self.assertRaises(onboard.OnboardError):
            onboard.set_value('[notify]\nadapter = """none"""\n', "notify", "adapter", '"ntfy"')

    def test_helpers(self) -> None:
        self.assertEqual(onboard.toml_string('a"b\\c\n'), '"a\\"b\\\\c\\u000a"')
        self.assertEqual(onboard.mask_topic(TOPIC), "ghosted-TT…")
        self.assertRegex(onboard.new_topic(), r"^ghosted-[A-Za-z0-9_-]{32}$")
        self.assertEqual(onboard.origin_of(URL), "hc-ping.com")


if __name__ == "__main__":
    unittest.main()
