from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import config, minitoml
from core.config import Config, ConfigError, load_config, load_env

try:
    import tomllib
except ImportError:
    tomllib = None

FULL_CONFIG = """\
timezone = "Europe/Paris"
projects_dir = "~/alt/projects"
workspace_roots = ["~/workspace", "/srv/code"]
scan_since_days = 21
maybe_active_minutes = 45
review_days = 10
env_file = "~/secrets/ghosted.env"
mystery_key = "kept in raw"

[skip]
entrypoints = ["sdk-cli", "headless"]
first_prompt_patterns = ['(?i)^overnight research']
path_patterns = ["/scratch/"]
temp_folders = false

[detect]
ignore_tail_patterns = ['(?i)shall we\\?\\s*$']

[notify]
adapter = "ntfy"

[notify.ntfy]
server = "https://ntfy.example.org"
topic = "ghosted-abc"

[notify.remote]
url = "https://ghosted.example.workers.dev"

[push]
digest_hour = 8
quiet_start = 21
quiet_end = 7
daily_budget = 2

[later]
something = 1
"""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.fake_home = self.root / "user"
        self.fake_home.mkdir()
        self.le_home = self.root / "ghosted-home"
        self.le_home.mkdir()
        self.env = {"HOME": str(self.fake_home), "GHOSTED_HOME": str(self.le_home)}
        # Point "the system zone" at a symlink we control.
        self.localtime = self.root / "localtime"
        os.symlink("/var/db/timezone/zoneinfo/America/Chicago", str(self.localtime))
        patcher = mock.patch.object(config, "LOCALTIME_PATH", str(self.localtime))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, text: str) -> None:
        (self.le_home / "config.toml").write_text(text, encoding="utf-8")


class TestDefaults(Base):
    def test_defaults_without_a_config_file(self):
        cfg = load_config(environ=self.env)
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.home, self.le_home)
        self.assertEqual(cfg.timezone, "America/Chicago")
        self.assertEqual(cfg.projects_dir, self.fake_home / ".claude" / "projects")
        self.assertEqual(cfg.workspace_roots, [])
        self.assertEqual(cfg.scan_since_days, 14)
        self.assertEqual(cfg.maybe_active_minutes, 30)
        self.assertEqual(cfg.review_days, 7)
        self.assertEqual(cfg.env_file, self.le_home / ".env")
        self.assertEqual(cfg.skip_entrypoints, ["sdk-cli"])
        self.assertEqual(cfg.skip_first_prompt_patterns, [])
        self.assertEqual(cfg.skip_path_patterns, [])
        self.assertTrue(cfg.skip_temp_folders)
        self.assertEqual(cfg.ignore_tail_patterns, config.DEFAULT_IGNORE_TAIL_PATTERNS)
        self.assertEqual(cfg.adapter, "none")
        self.assertEqual(cfg.ntfy_server, "https://ntfy.sh")
        self.assertEqual(cfg.ntfy_topic, "")
        self.assertEqual(cfg.remote_url, "")
        self.assertEqual((cfg.digest_hour, cfg.quiet_start, cfg.quiet_end, cfg.daily_budget),
                         (9, 22, 8, 3))
        self.assertEqual(cfg.raw, {})

    def test_load_config_writes_nothing(self):
        fresh = self.root / "never-created"
        cfg = load_config(home=fresh, environ=self.env)
        self.assertEqual(cfg.home, fresh)
        self.assertFalse(fresh.exists())

    def test_default_patterns_compile_and_match(self):
        import re
        cfg = load_config(environ=self.env)
        tails = [re.compile(p) for p in cfg.ignore_tail_patterns]
        self.assertTrue(tails[0].search("Anything else?  "))
        self.assertTrue(tails[1].search("Does that make sense?"))

    def test_bare_dataclass_defaults(self):
        cfg = Config(home=self.le_home)
        self.assertEqual(cfg.env_file, self.le_home / ".env")
        self.assertEqual(cfg.timezone, "UTC")
        self.assertIsInstance(cfg.projects_dir, Path)

    def test_bare_dataclass_home_follows_ghosted_home(self):
        # A Config() built without home= must not point at the real ~/.ghosted when the
        # environment names another GHOSTED_HOME (contract section 2). Nothing is written here.
        with mock.patch.dict(os.environ, {"GHOSTED_HOME": str(self.le_home)}):
            self.assertEqual(Config().home, self.le_home)
            self.assertEqual(Config().env_file, self.le_home / ".env")
        env = {k: v for k, v in os.environ.items() if k != "GHOSTED_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config().home, Path(os.path.expanduser("~/.ghosted")))


class TestProjectsDirOverride(Base):
    def test_env_overrides_the_default_and_the_config_value(self):
        alt = self.root / "alt-projects"
        env = dict(self.env, GHOSTED_PROJECTS_DIR=str(alt))
        self.assertEqual(load_config(environ=env).projects_dir, alt)
        self.write_config('projects_dir = "~/from/config"\n')
        self.assertEqual(load_config(environ=self.env).projects_dir, self.fake_home / "from" / "config")
        self.assertEqual(load_config(environ=env).projects_dir, alt)

    def test_claude_config_dir_moves_the_default(self):
        # Claude Code's own setting: its sessions are under $CLAUDE_CONFIG_DIR/projects.
        moved = dict(self.env, CLAUDE_CONFIG_DIR=str(self.root / "claude-work"))
        self.assertEqual(load_config(environ=moved).projects_dir, self.root / "claude-work" / "projects")
        self.assertEqual(load_config(environ=dict(self.env, CLAUDE_CONFIG_DIR="  ")).projects_dir,
                         load_config(environ=self.env).projects_dir)
        both = dict(moved, GHOSTED_PROJECTS_DIR=str(self.root / "alt-projects"))
        self.assertEqual(load_config(environ=both).projects_dir, self.root / "alt-projects")
        self.write_config('projects_dir = "~/from/config"\n')     # a written value wins over Claude's
        self.assertEqual(load_config(environ=moved).projects_dir, self.fake_home / "from" / "config")

    def test_tilde_and_blank_values(self):
        env = dict(self.env, GHOSTED_PROJECTS_DIR="~/elsewhere")
        self.assertEqual(load_config(environ=env).projects_dir, self.fake_home / "elsewhere")
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                env = dict(self.env, GHOSTED_PROJECTS_DIR=blank)
                self.assertEqual(load_config(environ=env).projects_dir,
                                 self.fake_home / ".claude" / "projects")

    def test_a_bad_config_value_is_still_an_error(self):
        self.write_config("projects_dir = 7\n")
        with self.assertRaises(ConfigError):
            load_config(environ=dict(self.env, GHOSTED_PROJECTS_DIR=str(self.root)))

    def test_bare_dataclass_and_helper(self):
        alt = self.root / "via-env"
        with mock.patch.dict(os.environ, {"GHOSTED_PROJECTS_DIR": str(alt)}):
            self.assertEqual(Config(home=self.le_home).projects_dir, alt)
        self.assertEqual(config.default_projects_dir(dict(self.env, GHOSTED_PROJECTS_DIR=str(alt))), alt)
        self.assertEqual(config.default_projects_dir(self.env), self.fake_home / ".claude" / "projects")


class TestHome(Base):
    def test_env_override(self):
        self.assertEqual(load_config(environ=self.env).home, self.le_home)

    def test_explicit_home_wins(self):
        other = self.root / "explicit"
        self.assertEqual(load_config(home=str(other), environ=self.env).home, other)

    def test_tilde_default_uses_environ_home(self):
        env = {"HOME": str(self.fake_home)}
        self.assertEqual(load_config(environ=env).home, self.fake_home / ".ghosted")

    def test_tilde_in_override(self):
        env = {"HOME": str(self.fake_home), "GHOSTED_HOME": "~/ghosted"}
        self.assertEqual(load_config(environ=env).home, self.fake_home / "ghosted")


class TestTimezone(Base):
    def test_config_value_wins(self):
        self.write_config('timezone = "Asia/Tokyo"\n')
        self.assertEqual(load_config(environ=dict(self.env, TZ="Europe/Paris")).timezone, "Asia/Tokyo")

    def test_localtime_symlink_before_tz_env(self):
        env = dict(self.env, TZ="Europe/Paris")
        self.assertEqual(load_config(environ=env).timezone, "America/Chicago")

    def test_tz_env_when_localtime_is_not_a_symlink(self):
        plain = self.root / "plain-localtime"
        plain.write_bytes(b"TZif")
        with mock.patch.object(config, "LOCALTIME_PATH", str(plain)):
            self.assertEqual(load_config(environ=dict(self.env, TZ=":Europe/Paris")).timezone,
                             "Europe/Paris")
            self.assertEqual(
                load_config(environ=dict(self.env, TZ="/usr/share/zoneinfo/Asia/Tokyo")).timezone,
                "Asia/Tokyo")
            self.assertEqual(load_config(environ=dict(self.env, TZ="Nowhere/Land")).timezone, "UTC")
            self.assertEqual(load_config(environ=self.env).timezone, "UTC")

    def test_missing_localtime_falls_back(self):
        with mock.patch.object(config, "LOCALTIME_PATH", str(self.root / "absent")):
            self.assertEqual(load_config(environ=self.env).timezone, "UTC")

    def test_symlink_forms(self):
        cases = {
            "/usr/share/zoneinfo/posix/Europe/Paris": "Europe/Paris",
            "../usr/share/zoneinfo/Asia/Tokyo": "Asia/Tokyo",
            "/var/db/timezone/tz/2025b.1.0/zoneinfo/America/New_York": "America/New_York",
            "/var/db/timezone/zoneinfo/Not/AZone": None,
            "/etc/somewhere-else": None,
        }
        for index, (target, expected) in enumerate(cases.items()):
            with self.subTest(target=target):
                link = self.root / ("lt%d" % index)
                os.symlink(target, str(link))
                got = config.system_timezone(environ={}, localtime_path=str(link))
                self.assertEqual(got, expected or "UTC")

    def test_invalid_config_timezone_is_an_error(self):
        self.write_config('timezone = "America/Chicgo"\n')
        with self.assertRaises(ConfigError) as ctx:
            load_config(environ=self.env)
        self.assertIn("timezone", str(ctx.exception))

    def test_blank_config_timezone_means_system(self):
        self.write_config('timezone = "  "\n')
        self.assertEqual(load_config(environ=self.env).timezone, "America/Chicago")

    def test_resolved_zone_is_always_loadable_by_zoneinfo(self):
        # "Z" is a UTC alias timeutil reads leniently, but ZoneInfo (core.schedule) and the
        # Worker's Intl reject it, so it is stored as "UTC".
        from zoneinfo import ZoneInfo
        self.write_config('timezone = "Z"\n')
        cfg = load_config(environ=self.env)
        self.assertEqual(cfg.timezone, "UTC")
        ZoneInfo(cfg.timezone)
        plain = self.root / "plain-localtime"
        plain.write_bytes(b"TZif")
        (self.le_home / "config.toml").unlink()
        with mock.patch.object(config, "LOCALTIME_PATH", str(plain)):
            self.assertEqual(load_config(environ=dict(self.env, TZ="Z")).timezone, "UTC")
            self.assertEqual(load_config(environ=dict(self.env, TZ=":America/Chicago")).timezone,
                             "America/Chicago")


class TestOverrides(Base):
    def test_every_key(self):
        self.write_config(FULL_CONFIG)
        cfg = load_config(environ=self.env)
        self.assertEqual(cfg.timezone, "Europe/Paris")
        self.assertEqual(cfg.projects_dir, self.fake_home / "alt" / "projects")
        self.assertEqual(cfg.workspace_roots, [self.fake_home / "workspace", Path("/srv/code")])
        self.assertEqual((cfg.scan_since_days, cfg.maybe_active_minutes, cfg.review_days), (21, 45, 10))
        self.assertEqual(cfg.env_file, self.fake_home / "secrets" / "ghosted.env")
        self.assertEqual(cfg.skip_entrypoints, ["sdk-cli", "headless"])
        self.assertEqual(cfg.skip_first_prompt_patterns, ["(?i)^overnight research"])
        self.assertEqual(cfg.skip_path_patterns, ["/scratch/"])
        self.assertFalse(cfg.skip_temp_folders)
        self.assertEqual(cfg.ignore_tail_patterns, ["(?i)shall we\\?\\s*$"])
        self.assertEqual(cfg.adapter, "ntfy")
        self.assertEqual((cfg.ntfy_server, cfg.ntfy_topic), ("https://ntfy.example.org", "ghosted-abc"))
        self.assertEqual(cfg.remote_url, "https://ghosted.example.workers.dev")
        self.assertEqual((cfg.digest_hour, cfg.quiet_start, cfg.quiet_end, cfg.daily_budget),
                         (8, 21, 7, 2))

    def test_unknown_keys_are_kept_in_raw(self):
        self.write_config(FULL_CONFIG)
        cfg = load_config(environ=self.env)
        self.assertEqual(cfg.raw["mystery_key"], "kept in raw")
        self.assertEqual(cfg.raw["later"], {"something": 1})
        self.assertFalse(hasattr(cfg, "mystery_key"))

    def test_empty_lists_override_defaults(self):
        self.write_config("[skip]\nentrypoints = []\n[detect]\nignore_tail_patterns = []\n")
        cfg = load_config(environ=self.env)
        self.assertEqual(cfg.skip_entrypoints, [])
        self.assertEqual(cfg.ignore_tail_patterns, [])

    def test_bom_is_tolerated(self):
        (self.le_home / "config.toml").write_bytes(b"\xef\xbb\xbfreview_days = 3\n")
        self.assertEqual(load_config(environ=self.env).review_days, 3)

    def test_minitoml_fallback_gives_the_same_config(self):
        self.write_config(FULL_CONFIG)
        with mock.patch.object(config, "_toml_loads", minitoml.loads):
            via_minitoml = load_config(environ=self.env)
        self.assertEqual(via_minitoml.raw, minitoml.loads(FULL_CONFIG))
        if tomllib is not None:
            with mock.patch.object(config, "_toml_loads", tomllib.loads):
                via_tomllib = load_config(environ=self.env)
            self.assertEqual(via_minitoml, via_tomllib)

    def test_tomllib_used_when_available(self):
        if tomllib is None:
            self.assertIs(config._toml_loads, minitoml.loads)
        else:
            self.assertIs(config._toml_loads, tomllib.loads)


class TestErrors(Base):
    def assertConfigError(self, text: str, needle: str) -> None:
        self.write_config(text)
        with self.assertRaises(ConfigError) as ctx:
            load_config(environ=self.env)
        self.assertIn(needle, str(ctx.exception))

    def test_wrong_types_name_the_key(self):
        self.assertConfigError('review_days = "seven"\n', "review_days")
        self.assertConfigError("review_days = true\n", "review_days")
        self.assertConfigError("workspace_roots = '~/workspace'\n", "workspace_roots")
        self.assertConfigError("[skip]\nentrypoints = [1, 2]\n", "skip.entrypoints")
        self.assertConfigError("skip = 3\n", "[skip]")
        self.assertConfigError("[notify]\nntfy = 'x'\n", "[notify.ntfy]")
        self.assertConfigError("timezone = 5\n", "timezone")

    def test_ranges(self):
        self.assertConfigError("[push]\ndigest_hour = 24\n", "push.digest_hour")
        self.assertConfigError("[push]\ndaily_budget = -1\n", "push.daily_budget")
        self.assertConfigError("review_days = -2\n", "review_days")

    def test_adapter_must_be_known(self):
        self.assertConfigError('[notify]\nadapter = "pager"\n', "notify.adapter")

    def test_bad_regex(self):
        self.assertConfigError("[skip]\nfirst_prompt_patterns = ['(unclosed']\n",
                               "skip.first_prompt_patterns[0]")
        self.assertConfigError("[detect]\nignore_tail_patterns = ['ok', '[bad']\n",
                               "detect.ignore_tail_patterns[1]")

    def test_empty_paths(self):
        self.assertConfigError('workspace_roots = [""]\n', "workspace_roots")
        self.assertConfigError('projects_dir = ""\n', "projects_dir")

    def test_malformed_toml_names_the_line(self):
        self.assertConfigError("review_days = 7\nadapter = \n", "line 2")

    def test_not_utf8(self):
        (self.le_home / "config.toml").write_bytes(b"timezone = '\xff'\n")
        with self.assertRaises(ConfigError):
            load_config(environ=self.env)

    def test_config_error_is_a_value_error(self):
        self.assertTrue(issubclass(ConfigError, ValueError))


class TestV2Keys(Base):
    """Contract 16: [detect] direct_questions, [watchdog] ping_url, the macos adapter."""

    def test_defaults(self):
        cfg = load_config(environ=self.env)
        self.assertEqual((cfg.direct_questions, cfg.watchdog_ping_url), ("waiting", ""))
        self.assertEqual((Config().direct_questions, Config().watchdog_ping_url), ("waiting", ""))

    def test_values(self):
        self.write_config('[detect]\ndirect_questions = " Soft "\n\n[watchdog]\n'
                          'ping_url = "https://hc-ping.com/abc-123"\n\n[notify]\nadapter = "macos"\n')
        cfg = load_config(environ=self.env)
        self.assertEqual((cfg.direct_questions, cfg.watchdog_ping_url, cfg.adapter),
                         ("soft", "https://hc-ping.com/abc-123", "macos"))
        self.assertIn("macos", config.ADAPTERS)
        self.assertEqual(config.DIRECT_QUESTIONS, ("waiting", "soft"))

    def test_direct_questions_is_an_enum(self):
        for bad in ('"sometimes"', "1", '""'):
            with self.subTest(value=bad):
                self.write_config("[detect]\ndirect_questions = %s\n" % bad)
                with self.assertRaises(ConfigError) as ctx:
                    load_config(environ=self.env)
                self.assertIn("detect.direct_questions", str(ctx.exception))

    def test_skip_temp_folders_is_true_or_false(self):
        for bad in ('"yes"', "1", "[]"):
            with self.subTest(value=bad):
                self.write_config("[skip]\ntemp_folders = %s\n" % bad)
                with self.assertRaises(ConfigError) as ctx:
                    load_config(environ=self.env)
                self.assertIn("skip.temp_folders", str(ctx.exception))
        self.write_config("[skip]\ntemp_folders = true\n")
        self.assertTrue(load_config(environ=self.env).skip_temp_folders)

    def test_ping_url_must_be_https_and_is_never_echoed(self):
        for bad in ("http://hc-ping.com/secret-id", "ftp://x/secret-id", "hc-ping.com/secret-id",
                    "https:///secret-id", "https://user:pw@hc-ping.com/secret-id",
                    "https://hc-ping.com/secret id", "https://hc-ping.com:99999/secret-id"):
            with self.subTest(url=bad):
                self.write_config('[watchdog]\nping_url = "%s"\n' % bad)
                with self.assertRaises(ConfigError) as ctx:
                    load_config(environ=self.env)
                self.assertIn("watchdog.ping_url", str(ctx.exception))
                self.assertNotIn("secret", str(ctx.exception))
        self.write_config("[watchdog]\nping_url = 5\n")
        with self.assertRaises(ConfigError):
            load_config(environ=self.env)
        self.write_config('[watchdog]\nping_url = "  "\n')
        self.assertEqual(load_config(environ=self.env).watchdog_ping_url, "")

    def test_https_url_problem(self):
        self.assertIsNone(config.https_url_problem("", "x"))
        self.assertIsNone(config.https_url_problem("https://hc-ping.com/a?b=c", "x"))
        self.assertEqual(config.https_url_problem("http://a/", "The URL"), "The URL must be an https:// URL")

    def test_minitoml_reads_the_new_tables(self):
        text = '[detect]\ndirect_questions = "soft"\n[watchdog]\nping_url = "https://a.example/p"\n'
        self.assertEqual(minitoml.loads(text), {"detect": {"direct_questions": "soft"},
                                                "watchdog": {"ping_url": "https://a.example/p"}})


class TestLoadEnv(Base):
    def env_file(self, text: str) -> Config:
        (self.le_home / ".env").write_text(text, encoding="utf-8")
        return load_config(environ=self.env)

    def test_parses_key_value_lines(self):
        cfg = self.env_file(
            "# Ghosted secrets\n"
            "\n"
            "GHOSTED_SERVICE_TOKEN_ID=abc123.access\n"
            "GHOSTED_SERVICE_TOKEN_SECRET = 'quoted # not a comment'\n"
            'PUSHOVER_TOKEN="double quoted"  # trailing comment\n'
            "export PUSHOVER_USER=u-42\n"
            "NTFY_TOKEN=tk_live # inline comment\n"
            "HASH_INSIDE=a#b\n"
            "EMPTY=\n"
            "QUOTED_EMPTY=''\n"
            "not a key value line\n"
            "BROKEN='unterminated\n"
            "1BAD=starts with a digit\n"
        )
        self.assertEqual(load_env(cfg), {
            "GHOSTED_SERVICE_TOKEN_ID": "abc123.access",
            "GHOSTED_SERVICE_TOKEN_SECRET": "quoted # not a comment",
            "PUSHOVER_TOKEN": "double quoted",
            "PUSHOVER_USER": "u-42",
            "NTFY_TOKEN": "tk_live",
            "HASH_INSIDE": "a#b",
        })

    def test_last_value_wins(self):
        cfg = self.env_file("A=1\nA=2\n")
        self.assertEqual(load_env(cfg), {"A": "2"})

    def test_missing_file_is_empty(self):
        self.assertEqual(load_env(load_config(environ=self.env)), {})

    def test_configured_env_file(self):
        secrets = self.fake_home / "s.env"
        secrets.write_text("PUSHOVER_USER=x\n", encoding="utf-8")
        self.write_config('env_file = "~/s.env"\n')
        self.assertEqual(load_env(load_config(environ=self.env)), {"PUSHOVER_USER": "x"})

    def test_never_prints(self):
        cfg = self.env_file("GHOSTED_SERVICE_TOKEN_SECRET=do-not-print-me\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            load_env(cfg)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_does_not_read_the_real_home(self):
        cfg = load_config(environ=self.env)
        self.assertTrue(str(cfg.env_file).startswith(str(self.root)))


if __name__ == "__main__":
    unittest.main()
