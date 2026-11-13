"""Regression tests for the smaller review findings (Mac side):

- python-correctness-6: `ghosted add` from an automated run, and the same open item twice
- privacy-and-security-6 / python-correctness-7 / worker-correctness-7: one 60-character project
  limit on both sides, and refused events visible in the hourly line and the digest
- python-correctness-3: a config.toml the launchd interpreter cannot read
- python-correctness-9: the hourly job follows the configured zone, not the system clock
- spec-compliance-4 (install half): no launchd job before the real fixtures are labelled
- spec-compliance-11: lowercase street addresses
- worker-correctness-4 (Mac half): status events carry the state they change from
- privacy-and-security-9: secrets files are ignored repo-wide

Synthetic data only; temp homes; launchctl is never run (injected runners).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import cli, config, detector, digest, install, ledger, sanitize, timeutil
from core.config import Config
from core.ledger import Ledger
from notify.base import SyncResult

ROOT = Path(__file__).resolve().parent.parent
NOW = timeutil.parse_iso("2026-10-06T15:00:00Z")  # Tue 10:00 in Chicago
INLINE_TABLE_CONFIG = 'timezone = "America/Chicago"\n[notify]\nntfy = { server = "https://ntfy.sh", topic = "ghosted-x" }\n'


class Home(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.le_home = self.root / "ghosted-home"
        self.le_home.mkdir(mode=0o700)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.write_config('timezone = "America/Chicago"\nprojects_dir = %s\n' % json.dumps(str(self.projects)))

    def write_config(self, text: str) -> None:
        (self.le_home / "config.toml").write_text(text)

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.le_home), "PATH": "/usr/bin:/bin",
               "GHOSTED_NOW": timeutil.to_iso(NOW), "CLAUDE_PROJECT_DIR": str(self.root / "shop")}
        env.update(extra)
        return env

    def run_cli(self, *argv: str, env: dict = None):
        out, err = io.StringIO(), io.StringIO()
        # No real system calls: `ghosted doctor` would otherwise run launchctl print and xcode-select.
        fake_runner = lambda argv: (0, "3.9.6\n" if "-c" in argv else "", "")  # noqa: E731
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(install, "default_runner", fake_runner):
            code = cli.main(list(argv), environ=env or self.env())
        return code, out.getvalue(), err.getvalue()

    def ledger(self) -> Ledger:
        return Ledger(config.load_config(environ=self.env()))


class AddGuardTests(Home):
    ARGS = ("add", "Re-run the readout and compare the totals", "--public", "shop: re-check the readout",
            "--project", "shop", "--due", "tomorrow")

    def test_the_same_open_item_is_not_added_twice(self) -> None:
        code, out, _ = self.run_cli(*self.ARGS)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("GS-0001 added"))
        later = self.env(GHOSTED_NOW=timeutil.to_iso(NOW + timedelta(days=1)))
        code, out, _ = self.run_cli(*self.ARGS, env=later)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("GS-0001 already open: shop: re-check the readout"), out)
        self.assertEqual(sorted(self.ledger().items()), ["GS-0001"])
        self.assertEqual(len(self.ledger().events()), 1)
        # Closed, it can be registered again.
        self.run_cli("done", "GS-0001")
        code, out, _ = self.run_cli(*self.ARGS, env=later)
        self.assertTrue(out.startswith("GS-0002 added"), out)

    def test_an_automated_run_is_refused_unless_allowed(self) -> None:
        code, _, err = self.run_cli(*self.ARGS, env=self.env(CLAUDE_CODE_ENTRYPOINT="sdk-cli"))
        self.assertEqual(code, 2)
        self.assertIn("automated run", err)
        self.assertIn("--allow-automated", err)
        self.assertEqual(self.ledger().items(), {})
        code, out, _ = self.run_cli(*self.ARGS, "--allow-automated", env=self.env(CLAUDE_CODE_ENTRYPOINT="sdk-cli"))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("GS-0001 added"))
        # An interactive session (entrypoint cli) is not refused.
        code, _, _ = self.run_cli("add", "x", "--public", "shop: another check", "--project", "shop",
                                  env=self.env(CLAUDE_CODE_ENTRYPOINT="cli"))
        self.assertEqual(code, 0)


class ProjectLimitTests(Home):
    def test_the_mac_refuses_a_project_the_worker_would_refuse(self) -> None:
        long61 = "p" * 61
        self.assertEqual(sanitize.project_problems("p" * 60), [])
        self.assertEqual(sanitize.project_problems(long61), ["is 61 characters long (the limit is 60)"])
        code, _, err = self.run_cli("add", "x", "--public", "shop: send the quote", "--project", long61)
        self.assertEqual(code, 2)
        self.assertIn("the limit is 60", err)
        led = self.ledger()
        with self.assertRaises(sanitize.Refused):
            led.add("decision", long61, "x", "shop: send the quote", now=NOW)

    def test_the_detector_aliases_a_long_project(self) -> None:
        name = "client-website-redesign-phase-two-marketing-site-and-blog-2026"  # 62, invented
        project, title = detector.safe_title(name, NOW.date(), "usage_limit")
        self.assertTrue(project.startswith("project-"))
        self.assertEqual(title, "%s: session 10-06 ended on usage limit" % project)
        ledger.check_interrupted_title(title, project)  # accepted


class RefusedEventsVisibleTests(unittest.TestCase):
    def result(self) -> SyncResult:
        return SyncResult(ok=True, acks={"e1": {"result": "applied"}, "e2": {"result": "invalid", "item_id": "GS-0002"}},
                          push={"subscriptions": 0, "last_ok_at": None, "last_failure": None,
                                "undeliverable": "no phone is subscribed (open the app and tap Enable notifications)"})

    def test_the_hourly_line_counts_invalid_acks_and_a_dead_push_channel(self) -> None:
        line = cli._notify_summary(self.result())
        self.assertIn("synced 2 events", line)
        self.assertIn("1 invalid (ghosted sync shows why)", line)
        self.assertIn("pushes cannot be delivered: no phone is subscribed", line)

    def test_the_digest_header_says_so_too(self) -> None:
        text = digest.sync_text(self.result())
        self.assertTrue(text.startswith("synced, 1 rejected (ghosted sync shows why)"), text)
        failing = SyncResult(ok=True, push={"subscriptions": 0, "last_failure": {
            "sent_at": "2026-10-06T14:15:00Z", "kind": "digest", "error": "webpush: 1 failed (410)"}})
        self.assertEqual(digest.sync_text(failing),
                         "synced · pushes failing since 2026-10-06T14:15:00Z: webpush: 1 failed (410)")


class ConfigReaderTests(Home):
    def test_minitoml_problem(self) -> None:
        self.assertIsNone(config.minitoml_problem('timezone = "America/Chicago"\n'))
        self.assertIn("inline tables", config.minitoml_problem(INLINE_TABLE_CONFIG))

    def test_le_run_records_a_config_it_cannot_read(self) -> None:
        self.write_config("timezone = \n")  # invalid TOML for every reader
        code, _, err = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertIn("config.toml", err)
        runs = ledger.read_jsonl(self.le_home / "runs.jsonl")
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0]["ok"])
        self.assertTrue(runs[0]["error"].startswith("ConfigError: config.toml"))

    def test_doctor_fails_when_the_launchd_python_cannot_read_the_config(self) -> None:
        self.write_config(INLINE_TABLE_CONFIG + 'projects_dir = %s\n' % json.dumps(str(self.projects)))
        code, out, _ = self.run_cli("doctor")  # the fake runner reports python 3.9.6
        self.assertEqual(code, 1)
        # On python 3.11+ `ghosted` itself reads it (tomllib), and the launchd row says the job cannot;
        # on 3.9 `ghosted` cannot either, and the config row says so. Both name the TOML feature.
        self.assertRegex(out, r"FAIL\s+config\s+.*inline tables")

    def test_install_refuses_the_launchd_job_for_that_config(self) -> None:
        self.write_config(INLINE_TABLE_CONFIG)
        cfg = Config(home=self.le_home, projects_dir=self.projects)
        ctx = install.make_context(cfg=cfg, home=self.root, repo=ROOT, runner=lambda argv: (0, "", ""),
                                   uid=501, environ={"HOME": str(self.root), "PATH": "/usr/bin"}, now=NOW)
        steps = install.plan(ctx, ["launchd"])
        self.assertEqual((steps[0].action, steps[0].ok), ("refuse", False))
        self.assertIn("cannot read config.toml", steps[0].detail)
        self.assertFalse((self.root / "Library").exists())


class ScheduledHoursTests(Home):
    def test_a_scheduled_run_outside_the_configured_hours_does_nothing(self) -> None:
        night = self.env(GHOSTED_SCHEDULED="1", GHOSTED_NOW="2026-10-06T08:02:00Z")  # 03:02 Chicago
        code, out, _ = self.run_cli("run", env=night)
        self.assertEqual(code, 0)
        self.assertEqual(out, "run skipped: 03:xx in America/Chicago is outside 07:00-23:59\n")
        self.assertFalse((self.le_home / "runs.jsonl").exists())

    def test_a_scheduled_run_inside_them_runs(self) -> None:
        day = self.env(GHOSTED_SCHEDULED="1", GHOSTED_NOW="2026-10-06T13:02:00Z")  # 08:02 Chicago
        code, out, _ = self.run_cli("run", env=day)
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("run ok"))
        # By hand (no GHOSTED_SCHEDULED) ghosted run works at any hour.
        code, out, _ = self.run_cli("run", env=self.env(GHOSTED_NOW="2026-10-06T08:02:00Z"))
        self.assertTrue(out.startswith("run ok"))

    def test_doctor_warns_when_the_mac_is_on_another_zone(self) -> None:
        with mock.patch.object(config, "system_timezone", lambda env=None, localtime_path=None: "Asia/Tokyo"):
            code, out, _ = self.run_cli("doctor")
        self.assertIn("this computer is on Asia/Tokyo but config.toml says America/Chicago", out)


class FixtureGateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "tests" / "fixtures" / "real").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "ghosted").write_text("")
        (self.repo / "core" / "launchd").mkdir(parents=True)
        shutil.copy(str(install.TEMPLATE), str(self.repo / "core" / "launchd" / install.TEMPLATE.name))
        self.projects = self.root / "projects"
        self.fixtures = [
            {"uuid": "11111111-0000-4000-8000-000000000001", "folder": "-a", "cut": 3, "prefix_sha1": "0" * 40,
             "expect": {"verdict": "usage_limit"}, "labelled_by": "test"},
            {"uuid": "11111111-0000-4000-8000-000000000002", "folder": "-a", "cut": 3, "prefix_sha1": "0" * 40,
             "expect": {"verdict": "open_question"}, "labelled_by": "test"},
            {"uuid": "11111111-0000-4000-8000-000000000003", "folder": "-b", "cut": 3, "prefix_sha1": "0" * 40,
             "expect": {"verdict": "clean"}, "labelled_by": "pending"},
        ]
        self.write_manifest(wanted=1)

    def write_manifest(self, wanted: int) -> None:
        manifest = self.root / "ghosted-home" / "fixtures" / "real-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"version": 1, "negatives_wanted": wanted, "fixtures": self.fixtures}))

    def ctx(self, force: bool = False):
        cfg = Config(home=self.root / "ghosted-home", projects_dir=self.projects)
        return install.make_context(cfg=cfg, home=self.root / "home", repo=self.repo,
                                    runner=lambda argv: (0, "", ""), uid=501,
                                    environ={"HOME": str(self.root / "home"), "PATH": "/usr/bin"}, force=force, now=NOW)

    def test_the_gate_reads_le_real_manifest_when_set(self) -> None:
        for fx in self.fixtures[:2]:
            (self.projects / fx["folder"]).mkdir(parents=True, exist_ok=True)
            (self.projects / fx["folder"] / (fx["uuid"] + ".jsonl")).write_text("{}\n")
        other = self.root / "elsewhere.json"
        labelled = [dict(fx, labelled_by="me") for fx in self.fixtures]
        other.write_text(json.dumps({"version": 1, "negatives_wanted": 1, "fixtures": labelled}))
        cfg = Config(home=self.root / "ghosted-home", projects_dir=self.projects)
        ctx = install.make_context(cfg=cfg, home=self.root / "home", repo=self.repo,
                                   runner=lambda argv: (0, "", ""), uid=501, now=NOW,
                                   environ={"HOME": str(self.root / "home"), "PATH": "/usr/bin",
                                            "GHOSTED_REAL_MANIFEST": str(other)})
        self.assertEqual(ctx.manifest, other)
        self.assertIsNone(install.fixture_gate(ctx))
        self.assertIn("0 of the 1", install.fixture_gate(self.ctx()))

    def test_elsewhere_there_is_nothing_to_gate(self) -> None:
        self.assertIsNone(install.fixture_gate(self.ctx()))

    def test_on_the_fixture_machine_unlabelled_negatives_block_the_job(self) -> None:
        for fx in self.fixtures[:2]:
            (self.projects / fx["folder"]).mkdir(parents=True, exist_ok=True)
            (self.projects / fx["folder"] / (fx["uuid"] + ".jsonl")).write_text("{}\n")
        gate = install.fixture_gate(self.ctx())
        self.assertIn("0 of the 1 hand-labelled clean sessions are labelled", gate)
        self.assertIn("GHOSTED_REQUIRE_REAL=1", gate)
        steps = install.plan(self.ctx(), ["launchd"])
        self.assertEqual((steps[0].action, steps[0].ok), ("refuse", False))
        forced = install.plan(self.ctx(force=True), ["launchd"])
        self.assertTrue(all(s.ok for s in forced))
        self.fixtures[2]["labelled_by"] = "me"
        self.write_manifest(wanted=1)
        self.assertIsNone(install.fixture_gate(self.ctx()))


class RealManifestPathTests(unittest.TestCase):
    """The real-session manifest is private and lives outside the repo (contract 16.8)."""

    def test_default_is_under_le_home(self) -> None:
        env = {"HOME": "/h"}
        self.assertEqual(config.real_manifest_path(environ=env), Path("/h/.ghosted/fixtures/real-manifest.json"))
        env = {"HOME": "/h", "GHOSTED_HOME": "/x"}
        self.assertEqual(config.real_manifest_path(environ=env), Path("/x/fixtures/real-manifest.json"))

    def test_env_override_wins(self) -> None:
        env = {"HOME": "/h", "GHOSTED_HOME": "/x", "GHOSTED_REAL_MANIFEST": "~/m.json"}
        self.assertEqual(config.real_manifest_path("/y", env), Path("/h/m.json"))

    def test_the_repo_holds_no_manifest(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.assertFalse((root / "tests" / "fixtures" / "real" / "manifest.json").exists())
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("tests/fixtures/real/", ignored)


class LowercaseAddressTests(unittest.TestCase):
    def test_lowercase_street_addresses_are_refused(self) -> None:
        for title in ("home: ship to 123 main street", "home: 45 oak avenue", "home: drop it at 9 elm st"):
            self.assertIn(sanitize.MSG_ADDRESS, sanitize.problems(title), title)
        for title in ("shop: send 3 samples on the way", "home: fix 2 bugs in the road map",
                      "shop: order 12 boxes", "x123 main street is a code name"):
            self.assertEqual(sanitize.problems(title), [], title)


class StatusPriorTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.led = Ledger(Config(home=Path(tmp.name) / "ghosted-home", timezone="America/Chicago"))
        self.led.add("decision", "shop", "x", "shop: pick the logo", now=NOW)

    def test_events_carry_the_state_they_change_from(self) -> None:
        _, ev = self.led.set_status("GS-0001", "snoozed", snooze_until="2026-10-07T09:00", now=NOW)
        self.assertEqual({k: ev["item"][k] for k in ("prior_status", "prior_snooze_until", "prior_dismiss_reason", "rev")},
                         {"prior_status": "open", "prior_snooze_until": None, "prior_dismiss_reason": None, "rev": 1})
        _, ev = self.led.set_status("GS-0001", "done", now=NOW + timedelta(minutes=1))
        self.assertEqual((ev["item"]["prior_status"], ev["item"]["prior_snooze_until"], ev["item"]["rev"]),
                         ("snoozed", "2026-10-07T09:00", 2))
        item = self.led.get("GS-0001")
        self.assertEqual((item["acted_status"], item["acted_at"]), ("done", ev["at"]))

    def test_a_snooze_expiry_is_not_an_act(self) -> None:
        self.led.set_status("GS-0001", "snoozed", snooze_until="2026-10-06T09:00", now=NOW - timedelta(days=1))
        [(item, ev)] = self.led.reopen_expired(None, NOW)
        self.assertEqual((ev["item"]["prior_status"], ev["item"]["prior_snooze_until"]), ("snoozed", "2026-10-06T09:00"))
        self.assertEqual(item["acted_status"], "snoozed")  # still the person's last act


class InstallGuardTests(Home):
    def test_install_from_a_package_is_refused(self) -> None:
        # uvx / pip: no bin/ghosted beside the package, so there is nothing to link.
        with mock.patch.object(install, "REPO_ROOT", self.root / "site-packages"):
            code, out, err = self.run_cli("install", "--bin")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("ghosted install: this needs a clone of the repo", err)
        self.assertFalse((self.root / ".local").exists())

    def test_all_skips_the_hourly_job_where_there_is_no_launchctl(self) -> None:
        env = self.env(PATH=str(self.root / "no-such-bin"))    # Linux: no launchctl on PATH
        code, out, err = self.run_cli("install", "--all", "--dry-run", env=env)
        self.assertEqual(code, 0, err)
        self.assertIn("bin      would create", out)
        self.assertIn("launchd  skipped: no launchctl here", out)
        self.assertNotIn("launchd  would", out)
        code, out, err = self.run_cli("install", "--launchd", env=env)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("macOS only", err)
        self.assertFalse((self.root / "Library").exists())


class SecretsIgnoredTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs git and a git checkout")
    def test_secrets_files_are_ignored_anywhere_in_the_repo(self) -> None:
        for path in (".secrets.env", "notify/.secrets.env", "notify/remote-worker/.secrets.env", "x.secrets.env"):
            proc = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(proc.returncode, 0, path)


if __name__ == "__main__":
    unittest.main()


class PluginManifestVersionTests(__import__("unittest").TestCase):
    """The plugin users install and the CLI report one version (0.2.0 = v2 pull)."""

    def test_plugin_json_matches_core_version(self) -> None:
        import json
        from pathlib import Path

        import core

        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], core.__version__)
        self.assertNotIn("hooks", manifest)  # installing the plugin must have no side effects
