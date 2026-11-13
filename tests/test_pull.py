"""The pull report (core.pull, `ghosted report`, contract 16.3).

Every test builds its own GHOSTED_HOME and projects folder under a temp directory with
invented transcripts (``Synth``): nothing reads ~/.claude/projects or ~/.ghosted. Live state
is injected (``live=``) so `claude agents` is never run. Quotes in these tests are synthetic.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from core import agents, classify, cli, pull, timeutil
from core.config import Config
from core.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
LE = ROOT / "bin" / "ghosted"
TZ = "America/Chicago"
NOW = timeutil.parse_iso("2026-11-08T18:00:00Z")  # Sun Nov 8, 12:00 in Chicago
GIT = shutil.which("git")
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

Q_STICKY = "Keep the sticky cart, or move it to the header?"
Q_BOX = "Which database should the inventory service use?"
PLAN = "# Move checkout to the new payments API\n\n1. Swap the client\n2. Migrate the webhooks"
OFFER = "I updated the three tests and the fixtures. Want me to run the whole suite?"
SAVE = "The migration is done and the tests pass. Want me to commit and push this?"


def git(cwd: Path, *args: str) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", **HERMETIC)
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Synth:
    """Writes invented session transcripts into a projects folder."""

    def __init__(self, projects: Path, workspace: Path) -> None:
        self.projects = projects
        self.workspace = workspace
        self.n = 0

    def next_uuid(self) -> str:
        self.n += 1
        return str(uuid_mod.UUID(int=(0xABCD << 112) | self.n))

    def write(self, project: str, entries: List[Dict[str, Any]], ended: datetime,
              uuid: Optional[str] = None, cwd: Optional[Path] = None, mtime: Optional[datetime] = None) -> str:
        sid = uuid or self.next_uuid()
        launch = str(cwd or (self.workspace / project))
        folder = self.projects / ("-" + launch.strip("/").replace("/", "-").replace(".", "-"))
        folder.mkdir(parents=True, exist_ok=True)
        lines = []
        t = ended - timedelta(minutes=10)
        head = {"isSidechain": False, "userType": "external", "cwd": launch, "sessionId": sid,
                "version": "2.1.319", "entrypoint": "cli"}
        lines.append(dict(head, type="user", uuid=self.next_uuid(), timestamp=iso(t),
                          message={"role": "user", "content": "Please work on the %s task." % project}))
        for i, entry in enumerate(entries):
            stamp = ended if i == len(entries) - 1 else t + timedelta(minutes=1 + i)
            full = dict(head, uuid=self.next_uuid(), timestamp=iso(stamp))
            full.update(entry)
            lines.append(full)
        path = folder / (sid + ".jsonl")
        path.write_text("".join(json.dumps(e) + "\n" for e in lines))
        when = (mtime or ended).timestamp()
        os.utime(str(path), (when, when))
        return sid

    @staticmethod
    def text(text: str) -> Dict[str, Any]:
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    @staticmethod
    def tool(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_" + name, "name": name, "input": payload}]}}

    @staticmethod
    def limit(resets: Optional[datetime]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                                 "apiErrorStatus": 429,
                                 "message": {"role": "assistant", "content": [
                                     {"type": "text", "text": "You've hit your session limit · resets 6am"}]}}
        if resets is not None:
            entry["quotaLimits"] = {"rateLimitType": "five_hour", "resetsAt": int(resets.timestamp()),
                                    "status": "rejected"}
        return entry

    # one-call session kinds -------------------------------------------------------------------------

    def question(self, project: str, ended: datetime, text: str = Q_STICKY, **kw: Any) -> str:
        return self.write(project, [self.text("I looked at both layouts. " + text)], ended, **kw)

    def clean(self, project: str, ended: datetime, **kw: Any) -> str:
        return self.write(project, [self.text("Done: the tests pass and the branch is pushed.")], ended, **kw)

    def ask_user(self, project: str, ended: datetime, **kw: Any) -> str:
        return self.write(project, [self.text("Two decisions block the migrations."),
                                    self.tool("AskUserQuestion", {"questions": [
                                        {"question": Q_BOX, "header": "Database",
                                         "options": [{"label": "Postgres"}, {"label": "SQLite"}]}]})], ended, **kw)

    def plan(self, project: str, ended: datetime, **kw: Any) -> str:
        return self.write(project, [self.text("Here is the plan."), self.tool("ExitPlanMode", {"plan": PLAN})],
                          ended, **kw)

    def limit_session(self, project: str, ended: datetime, resets: Optional[datetime], **kw: Any) -> str:
        return self.write(project, [self.text("Working through the upload retries now."), self.limit(resets)],
                          ended, **kw)


class PullCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "ghosted-home"            # not created: cold start
        self.projects = self.root / "projects"
        self.ws = self.root / "ws"
        self.projects.mkdir()
        self.ws.mkdir()
        self.s = Synth(self.projects, self.ws)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cfg(self, **kw: Any) -> Config:
        # The fake projects live in a temp folder, so the temp-folder skip (contract 16.13) is off.
        base: Dict[str, Any] = dict(home=self.home, projects_dir=self.projects, timezone=TZ,
                                    workspace_roots=[self.ws], skip_temp_folders=False)
        base.update(kw)
        return Config(**base)

    def keep_temp_sessions(self) -> None:
        """A config.toml that only turns the temp-folder skip off, for runs that load it from disk
        (first-run detection reads the ledger, not this file)."""
        self.home.mkdir(mode=0o700, exist_ok=True)
        (self.home / "config.toml").write_text("[skip]\ntemp_folders = false\n", encoding="utf-8")

    def ledger(self, **kw: Any) -> Ledger:
        return Ledger(self.cfg(**kw))

    def ago(self, **kw: Any) -> datetime:
        return NOW - timedelta(**kw)

    def gather(self, window: Optional[str] = "7d", live: Any = None, cfg: Any = None, **kw: Any) -> pull.Report:
        cfg = cfg or self.cfg()
        return pull.gather(cfg, Ledger(cfg), NOW, window=window, live=live, **kw)

    def text(self, window: Optional[str] = "7d", live: Any = None, render: Optional[Dict[str, Any]] = None,
             **kw: Any) -> str:
        return pull.render_text(self.gather(window, live, **kw), **(render or {}))

    def files_under_home(self) -> Dict[str, str]:
        out = {}
        for dirpath, _, names in os.walk(str(self.home)):
            for name in names:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as fh:
                    out[path] = fh.read().decode("utf-8", "replace")
        return out

    def stop(self, sid: str, event: str, verdict: str, reason: str, at: datetime) -> None:
        self.home.mkdir(mode=0o700, exist_ok=True)
        with open(str(self.home / "stops.jsonl"), "a") as fh:
            fh.write(json.dumps({"at": timeutil.to_iso(at), "session_id": sid, "event": event,
                                 "verdict": verdict, "reason": reason, "cwd_project": "x"}) + "\n")


# ---- window and labels ------------------------------------------------------------------------------

class WindowTests(unittest.TestCase):
    def test_parse_window(self) -> None:
        self.assertEqual(pull.parse_window("today"), ("today", 1))
        self.assertEqual(pull.parse_window("7d"), ("7d", 7))
        self.assertEqual(pull.parse_window(" 30D "), ("30d", 30))
        self.assertEqual(pull.parse_window("90d"), ("90d", 90))
        # contract 16.15: any window up to ten years, and "all" = everything on disk
        self.assertEqual(pull.parse_window("365d"), ("365d", 365))
        self.assertEqual(pull.parse_window("3650d"), ("3650d", 3650))
        self.assertEqual(pull.parse_window(" ALL "), ("all", pull.MAX_DAYS))
        for bad in ("0d", "3651d", "7", "week", "", "-3d", "7dd", "alls", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pull.parse_window(bad)

    def test_window_start_is_local_midnight(self) -> None:
        start = pull.window_start(7, NOW, TZ)
        self.assertEqual(timeutil.to_iso(start), "2026-11-02T06:00:00Z")  # Mon Nov 2, 00:00 CST
        self.assertEqual(timeutil.to_iso(pull.window_start(1, NOW, TZ)), "2026-11-08T06:00:00Z")
        # 30 days back crosses the DST change (Nov 1): Oct 10 00:00 CDT
        self.assertEqual(timeutil.to_iso(pull.window_start(30, NOW, TZ)), "2026-10-10T05:00:00Z")

    def test_labels(self) -> None:
        from datetime import date
        self.assertEqual(pull.range_label(date(2026, 11, 2), date(2026, 11, 8)), "Mon 2 – Sun 8 Nov")
        self.assertEqual(pull.range_label(date(2026, 10, 10), date(2026, 11, 8)), "Sat 10 Oct – Sun 8 Nov")
        self.assertEqual(pull.range_label(date(2026, 11, 8), date(2026, 11, 8)), "Today, Sun 8 Nov")
        self.assertEqual(pull.range_label(date(2025, 12, 30), date(2026, 1, 2)), "Tue 30 Dec 2025 – Fri 2 Jan")
        self.assertEqual(pull.day_label(date(2026, 11, 5), date(2026, 11, 8)), "Thu 5")
        self.assertEqual(pull.day_label(date(2026, 10, 30), date(2026, 11, 8)), "Fri 30 Oct")

    def test_one_line(self) -> None:
        self.assertEqual(pull.one_line("a\nb\r\nc\u2028d\u2029e\x85f\tg  h\x1b"), "a b c d e f g h")
        self.assertEqual(pull.flat("  x\ny   z"), "  x y   z")

    def test_save_word(self) -> None:
        self.assertEqual(pull.save_word("Want me to commit and push this?"), "commit")
        self.assertEqual(pull.save_word("Should I open a PR?"), "open a PR")
        self.assertEqual(pull.save_word("Shall I deploy it?"), "deploy")
        self.assertIsNone(pull.save_word("Want me to run the suite?"))


# ---- the whole history (contract 16.15) -------------------------------------------------------------

class AllWindowTests(PullCase):
    def test_all_starts_at_the_oldest_session(self) -> None:
        old = self.s.question("shop", self.ago(days=200))
        new = self.s.question("api", self.ago(days=2))
        report = self.gather(window="all")
        self.assertEqual(report.window_name, "all")
        self.assertEqual(report.start, pull.window_start(201, NOW, TZ))  # local midnight of that day
        self.assertEqual(report.days, 201)
        self.assertEqual({r["session_uuid"] for r in report.waiting}, {old, new})

    def test_all_with_no_sessions_is_one_day(self) -> None:
        report = self.gather(window="all")
        self.assertEqual((report.window_name, report.days, report.waiting), ("all", 1, []))
        self.assertIn("Nothing waiting on you", pull.render_text(report))

    def test_a_long_window_reaches_old_sessions(self) -> None:
        old = self.s.question("shop", self.ago(days=200))
        self.assertEqual([r["session_uuid"] for r in self.gather(window="365d").waiting], [old])
        self.assertEqual(self.gather(window="90d").waiting, [])

    def test_the_first_run_offers_the_older_history(self) -> None:
        self.s.question("shop", self.ago(days=3))
        self.s.clean("shop", self.ago(days=45))
        self.s.question("api", self.ago(days=200))
        report = self.gather(window=None, record=False)  # unrecorded: the next one is a first run too
        self.assertEqual((report.window_name, report.first_run), ("30d", True))
        self.assertEqual(report.older["sessions"], 2)
        self.assertEqual(report.older["since"], timeutil.to_iso(self.ago(days=200)))
        text = pull.render_text(report)
        self.assertIn("2 older sessions go back to 22 Apr 2026", text)
        self.assertIn("ghosted report all", text)
        self.assertEqual(pull.as_dict(report)["older"], report.older)
        skill = pull.render_text(self.gather(window=None, via="skill"))
        self.assertIn("/ghosted all", skill)

    def test_no_offer_without_older_history_or_after_the_first_run(self) -> None:
        self.s.question("shop", self.ago(days=3))
        first = self.gather(window=None)
        self.assertIsNone(first.older)
        self.assertNotIn("ghosted report all", pull.render_text(first))
        self.s.question("api", self.ago(days=200))
        second = self.gather(window=None)  # 7 days: the offer belongs to the first run only
        self.assertEqual((second.window_name, second.older), ("7d", None))
        self.assertIsNone(pull.as_dict(second)["older"])

    def test_old_rows_sit_under_their_own_heading(self) -> None:
        self.s.question("shop", self.ago(days=3))
        self.s.question("api", self.ago(days=100))
        self.s.question("docs", self.ago(days=31))
        text = pull.render_text(self.gather(window="all"), quotes=False)
        head, _, tail = text.partition("Older than 30 days: 2")
        self.assertIn("shop ─", head)
        self.assertNotIn("api ─", head)
        self.assertIn("api ─", tail)
        self.assertIn("docs ─", tail)
        self.assertIn("3 waiting on you", head)
        recent_only = pull.render_text(self.gather(window="30d"), quotes=False)
        self.assertNotIn("Older than 30 days", recent_only)


# ---- colour (contract 16.16) ------------------------------------------------------------------------

class ColorTests(PullCase):
    ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def report_text(self) -> str:
        self.s.question("shop", self.ago(days=3))
        self.s.limit_session("api", self.ago(days=1), self.ago(hours=20))
        self.s.plan("docs", self.ago(days=2))
        self.s.question("web", self.ago(days=2), text=OFFER)
        return pull.render_text(self.gather(window="7d"), soft=True)

    def test_colour_only_adds_codes(self) -> None:
        plain = self.report_text()
        coloured = pull.colorize(plain)
        self.assertNotEqual(coloured, plain)
        self.assertEqual(self.ANSI.sub("", coloured), plain)   # nothing else changes
        self.assertEqual(len(coloured.splitlines()), len(plain.splitlines()))
        for line in coloured.splitlines():
            if self.ANSI.search(line):
                self.assertTrue(line.endswith("\x1b[0m"), repr(line[-12:]))  # every styled line resets

    def test_what_gets_which_colour(self) -> None:
        lines = pull.colorize(self.report_text()).splitlines()
        by_plain = {self.ANSI.sub("", ln): ln for ln in lines}
        header = next(v for k, v in by_plain.items() if k.startswith("GHOSTED"))
        self.assertIn("\x1b[1m", header)                                   # bold title
        self.assertIn("\x1b[1;33m3 waiting on you", header)                # the count stands out
        rule = next(v for k, v in by_plain.items() if k.startswith("shop ─"))
        self.assertTrue(rule.startswith("\x1b[1;36mshop\x1b[0m"))          # project name
        resume = next(v for k, v in by_plain.items() if "claude --resume" in k)
        self.assertIn("\x1b[32m", resume)                                  # the command to paste
        quote = next(v for k, v in by_plain.items() if k.strip().startswith('"'))
        self.assertIn("\x1b[1m", quote)
        blank = [ln for ln in lines if not self.ANSI.sub("", ln).strip()]
        self.assertTrue(all(ln == "" for ln in blank))                     # empty lines stay empty

    def test_use_color(self) -> None:
        self.assertFalse(pull.use_color({}, tty=False))
        self.assertTrue(pull.use_color({}, tty=True))
        self.assertFalse(pull.use_color({"NO_COLOR": "1"}, tty=True))
        self.assertFalse(pull.use_color({"TERM": "dumb"}, tty=True))
        self.assertTrue(pull.use_color({"FORCE_COLOR": "1"}, tty=False))
        self.assertFalse(pull.use_color({"FORCE_COLOR": "0"}, tty=False))
        self.assertFalse(pull.use_color({"FORCE_COLOR": "1", "NO_COLOR": "1"}, tty=False))  # NO_COLOR wins
        self.assertTrue(pull.use_color({"NO_COLOR": ""}, tty=True))        # empty = not set (no-color.org)


# ---- Markdown for Claude Code (contract 16.17) -------------------------------------------------------

NASTY = 'Use **bold**, a [link](https://evil.example/x), <b>html</b> or `code` in the my_app title?'


class MarkdownTests(PullCase):
    def md(self, **render: Any) -> str:
        return pull.markdown(pull.render_text(self.gather(window="7d", via="skill"), **render))

    def test_structure(self) -> None:
        sid = self.s.question("shop", self.ago(days=3))
        self.s.limit_session("api", self.ago(days=1), self.ago(hours=20))
        self.s.question("web", self.ago(days=2), text=OFFER)
        out = self.md()
        lines = out.split("\n")
        self.assertTrue(lines[0].startswith("**Ghosted** · "))
        self.assertIn("**2 waiting on you**", lines[0])
        self.assertIn("### shop", lines)
        self.assertIn("### api", lines)
        head = next(ln for ln in lines if "ended on a question" in ln)
        self.assertRegex(head, r"^\*\*\d+\*\* · ")                 # the number to say back, in bold
        self.assertNotIn("?", head.split("·")[0])                   # no glyph column in Markdown
        quote = next(ln for ln in lines if ln.startswith("> "))
        self.assertEqual(quote, '> "%s"' % Q_STICKY)
        # the resume command sits alone in a fenced block, untouched, so it can be copied
        at = lines.index(pull.detector.resume_command(str(self.ws / "shop"), sid))
        self.assertEqual((lines[at - 1], lines[at + 1]), ("```", "```"))
        self.assertEqual((lines[at - 2], lines[at + 2]), ("", ""))  # blocks stand apart
        # footers are a list, one per line whatever the renderer does with single line breaks
        self.assertTrue(any(ln.startswith("- Soft (ended on a polite offer") for ln in lines))
        self.assertTrue(any(ln.startswith("- Done with one? say: done ") for ln in lines))
        self.assertNotIn("─", out)
        self.assertNotIn("\x1b", out)
        self.assertTrue(out.endswith("\n"))

    def test_facts_join_the_head_line(self) -> None:
        self.s.limit_session("api", self.ago(days=1), self.ago(hours=20))
        head = next(ln for ln in self.md().split("\n") if "usage limit cut it off" in ln)
        self.assertIn(" · limit has reset", head)                   # one line: no soft line breaks to lose

    def test_a_fact_that_starts_with_a_number_is_not_a_row(self) -> None:
        repo = self.ws / "shop"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("a")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        for name in ("x.txt", "y.txt"):
            (repo / name).write_text(name)
        self.s.question("shop", self.ago(days=1))
        for render in ({}, {"plain": True}):
            report = self.gather(window="7d", via="skill", unsaved=False)  # the session row only
            out = pull.markdown(pull.render_text(report, **render))
            heads = [ln for ln in out.split("\n") if ln.startswith("**") and not ln.startswith("**Ghosted")]
            self.assertEqual(len(heads), 1, out)
            self.assertIn("2 ", heads[0].split(" · ", 3)[-1])        # "2 uncommitted" / "2 changes …" joined on

    def test_session_text_cannot_add_markdown(self) -> None:
        self.s.question("my_app", self.ago(days=1), text=NASTY)
        out = self.md()
        quote = next(ln for ln in out.split("\n") if ln.startswith("> "))
        for raw in ("**bold**", "[link](", "<b>", "`code`", "https://"):
            self.assertNotIn(raw, quote)
        for safe in (r"\*\*bold\*\*", r"\[link\]", r"\<b\>", r"\`code\`", r"https\://", r"my\_app"):
            self.assertIn(safe, quote)
        self.assertIn(r"### my\_app", out.split("\n"))              # folder names are escaped too

    def test_escape(self) -> None:
        self.assertEqual(pull.md_escape("a*b_c`d[e]f<g>h#i|j~k\\l"), r"a\*b\_c\`d\[e\]f\<g\>h\#i\|j\~k\\l")
        self.assertEqual(pull.md_escape("see www.example.com or http://x.y"), r"see www\.example.com or http\://x.y")
        self.assertEqual(pull.md_escape("plain words, 3 · done?"), "plain words, 3 · done?")

    def test_a_command_with_backticks_gets_a_longer_fence(self) -> None:
        self.assertEqual(pull.fenced("cd 'a```b' && claude --resume x"),
                         ["````", "cd 'a```b' && claude --resume x", "````"])
        self.assertEqual(pull.fenced("cd ~/shop")[0], "```")

    def test_nothing_waiting(self) -> None:
        self.s.clean("shop", self.ago(days=1))
        out = self.md()
        self.assertIn("✓ You didn't ghost anything in the last 7 days.", out.split("\n"))
        self.assertIn("- 1 session in 1 project checked", out.split("\n"))

    def test_unsaved_rows_and_older_heading(self) -> None:
        self.s.question("api", self.ago(days=100))
        self.s.question("shop", self.ago(days=3))
        out = pull.markdown(pull.render_text(self.gather(window="all", via="skill")))
        self.assertIn("**Older than 30 days: 1**", out.split("\n"))


# ---- cold start, windows, measure ------------------------------------------------------------------

class ColdStartTests(PullCase):
    def test_cold_start_creates_home_first_run_30d_then_7d(self) -> None:
        self.s.question("shop", self.ago(days=12))   # only the 30-day first run sees this
        self.assertFalse(self.home.exists())
        first = self.gather(window=None)
        self.assertTrue(self.home.is_dir())
        self.assertEqual(os.stat(str(self.home)).st_mode & 0o777, 0o700)
        self.assertEqual((first.window_name, first.first_run), ("30d", True))
        self.assertEqual(len(first.waiting), 1)
        text = pull.render_text(first)
        self.assertIn("GHOSTED · Sat 10 Oct – Sun 8 Nov · 1 waiting on you", text)
        self.assertIn("First run: this looked back 30 days.", text)
        second = self.gather(window=None)
        self.assertEqual((second.window_name, second.first_run), ("7d", False))
        self.assertEqual(second.waiting, [])
        self.assertIn("Nothing waiting on you in the last 7 days (0 sessions checked).", pull.render_text(second))
        self.assertNotIn("First run", pull.render_text(second))

    def test_cold_start_via_the_cli(self) -> None:
        self.s.question("shop", self.ago(days=3))
        self.keep_temp_sessions()
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home), "GHOSTED_PROJECTS_DIR": str(self.projects),
               "GHOSTED_NOW": timeutil.to_iso(NOW), "PATH": str(self.root / "nobin")}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["report", "--json"], environ=env)
        self.assertEqual(code, 0, err.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual((data["window"]["name"], data["first_run"]), ("30d", True))
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertFalse(data["sources"]["agents"])  # no claude on PATH: no live signal
        with contextlib.redirect_stdout(io.StringIO()) as out2:
            cli.main(["report", "--json"], environ=env)
        self.assertEqual(json.loads(out2.getvalue())["window"]["name"], "7d")

    def test_measure_record_has_ids_only(self) -> None:
        sid = self.s.question("shop", self.ago(days=1))
        soft = self.s.question("api", self.ago(days=2), text=OFFER)
        report = self.gather(window="7d", via="skill")
        (rec,) = [r for r in Ledger(self.cfg()).measures() if r.get("type") == "report"]
        self.assertEqual(set(rec), {"type", "at", "window", "via", "waiting", "soft", "checked", "unsaved"})
        self.assertEqual((rec["window"], rec["via"], rec["checked"]), ("7d", "skill", 2))
        self.assertEqual(rec["waiting"], [report.waiting[0]["id"]])
        self.assertEqual(rec["soft"], [report.soft[0]["id"]])
        self.assertTrue(all(i.startswith("GS-") for i in rec["waiting"] + rec["soft"]))
        self.assertTrue(sid and soft)

    def test_no_scan_reads_stored_state(self) -> None:
        self.gather()
        self.s.question("shop", self.ago(days=1))
        self.assertEqual(self.gather(scan=False).scan, "skipped")
        self.assertEqual(self.gather(scan=False).waiting, [])
        self.assertEqual(len(self.gather().waiting), 1)

    def test_a_held_run_lock_skips_the_scan(self) -> None:
        from core import runner

        self.s.question("shop", self.ago(days=1))
        cfg = self.cfg()
        with runner.run_lock(cfg) as held:
            self.assertTrue(held)
            report = pull.gather(cfg, Ledger(cfg), NOW, window="7d", live=None)
        self.assertEqual(report.scan, "busy")
        self.assertIn("Another scan is running", pull.render_text(report))

    def test_no_tick_from_another_scans_saved_state(self) -> None:
        from core import runner

        self.s.clean("docs", self.ago(days=1))
        cfg = self.cfg()
        self.assertIn("✓", pull.render_text(self.gather()))        # scanned: one clean session
        self.s.question("shop", self.ago(hours=2))                 # not scanned yet
        with runner.run_lock(cfg):
            stale = pull.gather(cfg, Ledger(cfg), NOW, window="7d", live=None, record=False)
        unscanned = self.gather(scan=False)
        for report in (stale, unscanned):
            self.assertNotIn("✓", pull.render_text(report), report.scan)
            self.assertNotIn("✓", pull.render_receipt(report), report.scan)
            self.assertIn("0 ghosted", pull.render_receipt(report).splitlines())


# ---- row kinds, glyphs, header ---------------------------------------------------------------------

class RowTests(PullCase):
    def test_each_kind_and_glyph(self) -> None:
        q = self.s.question("shop", self.ago(days=4))
        a = self.s.ask_user("inventory", self.ago(days=3))
        p = self.s.plan("checkout", self.ago(days=2))
        lim = self.s.limit_session("billing", self.ago(days=1), resets=self.ago(days=1, hours=-2))
        self.s.clean("docs", self.ago(days=1))
        report = self.gather()
        kinds = {r["session_uuid"]: r["kind"] for r in report.rows}
        self.assertEqual(kinds, {q: "question", a: "ask_user", p: "plan_approval", lim: "usage_limit"})
        text = pull.render_text(report)
        lines = text.splitlines()
        self.assertEqual(lines[0], "GHOSTED · Mon 2 – Sun 8 Nov · 4 waiting on you   (5 sessions checked, 1 clean)")
        self.assertIn("? Wed 4 · 4d ago · ended on a question", text)
        self.assertIn('"%s"' % Q_STICKY, text)
        self.assertIn("? Thu 5 · 3d ago · asked you in a question box", text)
        self.assertIn('"%s"' % Q_BOX, text)
        self.assertIn("▤ Fri 6 · 2d ago · waiting for you to approve a plan", text)
        self.assertIn('"Move checkout to the new payments API"', text)
        self.assertIn("⏸ Sat 7 · yesterday · usage limit cut it off mid-task", text)
        self.assertIn("limit has reset", text)
        self.assertIn("→ cd %s && claude --resume %s" % (self.ws / "shop", q), text)  # the full id
        self.assertIn("Quiet: 1 project, nothing hanging.", text)
        self.assertIn("Done with one? ghosted done ", text)
        self.assertIn("not a git repo", text)  # the synthetic launch dirs are not repos

    def test_ordering_projects_by_newest_row_rows_newest_first(self) -> None:
        old = self.s.question("alpha", self.ago(days=5))
        new = self.s.question("alpha", self.ago(days=1), text="Ship the banner on Monday or Tuesday?")
        mid = self.s.question("beta", self.ago(days=3))
        report = self.gather()
        self.assertEqual([r["session_uuid"] for r in report.rows], [new, old, mid])
        text = pull.render_text(report)
        self.assertLess(text.index("alpha ─"), text.index("beta ─"))

    def test_rows_without_quotes_and_plain_mode(self) -> None:
        self.s.question("shop", self.ago(days=1))
        self.s.limit_session("billing", self.ago(days=2), resets=None)
        report = self.gather()
        bare = pull.render_text(report, quotes=False)
        self.assertNotIn(Q_STICKY, bare)
        plain = pull.render_text(report, plain=True)
        self.assertIn("To pick it back up, paste: cd ", plain)
        self.assertIn("not tracked by git", plain)
        self.assertIn("Claude's usage limit cut it off mid-task", plain)
        for glyph in ("?", "⏸", "→"):
            self.assertFalse(any(line.lstrip().startswith(glyph) or (" %s " % glyph) in line
                                 for line in plain.splitlines() if not line.lstrip().startswith('"')), glyph)

    @unittest.skipUnless(GIT, "git not installed")
    def test_plain_git_wording(self) -> None:
        repo = self.ws / "shop"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("x")
        self.s.question("shop", self.ago(days=1), cwd=repo)
        report = self.gather()
        self.assertIn("1 uncommitted · no upstream", pull.render_text(report))
        self.assertIn("1 change saved on your computer but not saved to GitHub yet · this branch isn't on GitHub yet",
                      pull.render_text(report, plain=True))
        row = report.rows[0]
        self.assertEqual((row["uncommitted"], row["not_git"], row["branch_no_upstream"]), (1, False, True))

    def test_one_line_rule_with_newlines_in_quotes_and_titles(self) -> None:
        self.s.write("shop", [Synth.text("Two options. Keep the sticky   cart,\tor move it\tto the header?")],
                     self.ago(days=1))
        led = self.ledger()
        led.add(kind="promise", project="shop", local_title="check the\ndeploy\u2028now\r\x1b[31m",
                title="shop: check", due_at="2026-11-08", now=NOW)
        text = self.text()
        for line in text.splitlines():
            for bad in ("\r", "\t", "\u2028", "\x1b"):
                self.assertNotIn(bad, line)
        self.assertEqual(len(text.split("\n")), len(text.splitlines()) + 1)  # no stray separators
        self.assertIn('"Keep the sticky cart, or move it to the header?"', text)
        self.assertIn('"check the deploy now [31m"', text)

    def test_a_missing_history_folder_is_said_not_passed_off_as_empty(self) -> None:
        gone = self.projects.parent / "moved-away" / "projects"
        text = pull.render_text(self.gather(cfg=self.cfg(projects_dir=gone)))
        self.assertIn("⚠ No Claude Code history at %s. If yours is somewhere else, set projects_dir" % gone, text)
        self.assertIn("Nothing waiting on you in the last 7 days (0 sessions checked).", text)
        self.assertNotIn("✓", text)
        self.assertNotIn("No Claude Code history", pull.render_text(self.gather()))

    def test_empty_states(self) -> None:
        self.s.clean("docs", self.ago(hours=2))
        self.s.clean("docs", self.ago(days=3))
        today = self.text("today")
        self.assertEqual(today.splitlines()[0], "GHOSTED · Today, Sun 8 Nov")
        self.assertEqual(today.splitlines()[2:5], [
            "✓ You didn't ghost anything today.",
            "1 session in 1 project checked",
            "Looked for: unanswered questions, usage-limit cut-offs, plans waiting for approval, "
            "blocked background sessions."])
        week = self.text("7d")
        self.assertIn("✓ You didn't ghost anything in the last 7 days.", week)
        self.assertIn("2 sessions in 1 project checked", week.splitlines())
        self.assertNotIn("Quiet:", week)                 # the all-clear already names the projects
        self.assertNotIn("Done with one?", week)

    def test_no_all_clear_when_nothing_could_be_checked(self) -> None:
        # Zero sessions checked, or a session file that could not be read: never a tick.
        empty = self.text("7d")
        self.assertIn("Nothing waiting on you in the last 7 days (0 sessions checked).", empty)
        self.assertNotIn("✓", empty)
        self.s.clean("docs", self.ago(hours=2))
        report = self.gather(window="7d")
        report.scan_errors = 1
        unread = pull.render_text(report)
        self.assertIn("Nothing waiting on you in the last 7 days (1 session checked).", unread)
        self.assertNotIn("✓", unread)

    def test_soft_hidden_by_default_and_listed_with_soft(self) -> None:
        self.s.question("shop", self.ago(days=1))
        soft = self.s.question("api", self.ago(days=2), text=OFFER)
        report = self.gather()
        self.assertEqual([r["session_uuid"] for r in report.soft], [soft])
        self.assertEqual(report.counts["soft"], 1)
        text = pull.render_text(report)
        self.assertNotIn("Want me to run the whole suite", text)
        self.assertIn("Soft (ended on a polite offer, probably fine): 1  → ghosted report --soft", text)
        listed = pull.render_text(report, soft=True)
        self.assertIn("Soft (ended on a polite offer, probably fine): 1\napi ─", listed)
        self.assertIn("· Fri 6 · 2d ago · ended on a polite offer", listed)
        self.assertIn('"Want me to run the whole suite?"', listed)
        self.assertLess(listed.index("shop ─"), listed.index("Soft ("))
        self.assertIn("(2 sessions checked, 0 clean)", text)
        skill = pull.render_text(self.gather("30d", via="skill"))
        self.assertIn("→ /ghosted 30d --soft", skill)
        self.assertIn("Done with one? say: done ", skill)

    def test_direct_questions_soft_config(self) -> None:
        self.s.question("shop", self.ago(days=1))
        report = self.gather(cfg=self.cfg(direct_questions="soft"))
        self.assertEqual((len(report.waiting), len(report.soft)), (0, 1))

    def test_project_filter(self) -> None:
        self.s.question("shop", self.ago(days=1))
        self.s.question("Billing", self.ago(days=2))
        self.s.clean("docs", self.ago(days=2))
        report = self.gather(project="billing")
        self.assertEqual({r["project"] for r in report.rows}, {"Billing"})
        self.assertEqual(report.counts["checked"], 1)
        self.assertIn("GHOSTED · Mon 2 – Sun 8 Nov · billing · 1 waiting on you", pull.render_text(report))

    def test_drift_warning_line(self) -> None:
        for i in range(4):
            path = self.projects / "-x" / (self.s.next_uuid() + ".jsonl")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"type": "brand-new-entry", "sessionId": "x"}) + "\n")
            os.utime(str(path), (self.ago(days=1).timestamp(),) * 2)
        report = self.gather()
        self.assertIsNotNone(report.drift)
        lines = pull.render_text(report).splitlines()
        self.assertTrue(lines[1].startswith("⚠ Claude Code's session format may have changed: 4 of 4 session files"), lines[1])
        self.assertTrue(lines[1].endswith("Results may be incomplete (ghosted doctor)."))
        self.assertIn("problem", json.loads(pull.render_json(report))["drift"])


# ---- save offers --------------------------------------------------------------------------------

class SaveOfferTests(PullCase):
    @unittest.skipUnless(GIT, "git not installed")
    def test_save_offer_waits_only_with_unsaved_work(self) -> None:
        repo = self.ws / "shop"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("x")
        sid = self.s.question("shop", self.ago(days=1), text=SAVE, cwd=repo)
        report = self.gather()
        (row,) = report.rows
        self.assertEqual((row["reason"], row["tier"], row["kind"]), (classify.SAVE_OFFER, "waiting", "question"))
        text = pull.render_text(report)
        self.assertIn("offered to commit; the work still isn't saved", text)
        self.assertIn("1 uncommitted", text)
        self.assertIn("offered to commit; your work still isn't saved to GitHub", pull.render_text(report, plain=True))
        # Saved since: the same session is soft now.
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "save")
        report = self.gather()
        self.assertEqual([r["session_uuid"] for r in report.soft], [sid])
        self.assertEqual(report.waiting, [])

    def test_save_offer_outside_a_repo_is_soft(self) -> None:
        self.s.question("shop", self.ago(days=1), text=SAVE)
        report = self.gather()
        self.assertEqual((len(report.waiting), len(report.soft)), (0, 1))


# ---- usage limits ---------------------------------------------------------------------------------

class LimitTests(PullCase):
    def test_future_reset_is_a_deferral_row_without_a_number(self) -> None:
        resets = NOW + timedelta(hours=6)  # 6:00 PM Chicago
        sid = self.s.limit_session("billing", self.ago(hours=2), resets=resets)
        report = self.gather()
        (row,) = report.rows
        self.assertEqual((row["session_uuid"], row["n"], row["id"], row["kind"]), (sid, None, None, "usage_limit"))
        text = pull.render_text(report)
        self.assertIn("⏸ Sun 8 · 2h ago · usage limit cut it off mid-task", text)
        self.assertIn("can't resume until 6:00 PM", text)
        self.assertNotIn("Done with one?", text)  # nothing numbered
        head = [l for l in text.splitlines() if "⏸" in l][0]
        self.assertTrue(head.startswith("    ⏸ "), head)  # number column left blank

    def test_past_reset_row_with_a_number(self) -> None:
        self.s.limit_session("billing", self.ago(days=2), resets=self.ago(days=2, hours=-1))
        report = self.gather()
        (row,) = report.rows
        self.assertIsInstance(row["n"], int)
        self.assertIsNotNone(row["resets_at"])
        self.assertIn("limit has reset", pull.render_text(report))
        self.assertIn("Claude's usage limit has reset", pull.render_text(report, plain=True))

    def test_future_reset_on_a_processed_row(self) -> None:
        # A processed limit whose stored reset is still ahead (a hand-edited or skewed clock case).
        self.s.limit_session("billing", self.ago(days=2), resets=None)
        cfg = self.cfg()
        self.gather(cfg=cfg)
        led = Ledger(cfg)
        state = led.load_state()
        (sid,) = list(state["sessions"])
        state["sessions"][sid]["resets_at"] = timeutil.to_iso(NOW + timedelta(days=1, hours=-6))
        led.save_state(state)
        self.assertIn("limit reset Mon 9, 6:00 AM", self.text(scan=False))


# ---- items: handled, due rows -----------------------------------------------------------------------

class ItemTests(PullCase):
    def test_snoozed_done_dismissed_are_handled(self) -> None:
        a = self.s.question("shop", self.ago(days=1))
        b = self.s.question("shop", self.ago(days=2))
        c = self.s.question("shop", self.ago(days=3))
        d = self.s.question("shop", self.ago(days=4))
        e = self.s.question("shop", self.ago(days=5))
        report = self.gather()
        ids = {r["session_uuid"]: r["id"] for r in report.rows}
        led = self.ledger()
        led.set_status(ids[a], "done", now=NOW)
        led.set_status(ids[b], "dismissed", now=NOW, dismiss_reason="not_real")
        led.set_status(ids[c], "snoozed", now=NOW, snooze_until="2026-11-10T09:00")
        led.set_status(ids[d], "snoozed", now=NOW - timedelta(days=3), snooze_until="2026-11-07T09:00")  # expired
        report = self.gather()
        self.assertEqual({r["session_uuid"] for r in report.rows}, {d, e})
        self.assertEqual(report.counts["handled"], 3)
        self.assertEqual(report.counts["clean"], 0)
        self.assertIn("(5 sessions checked, 0 clean · 3 handled)", pull.render_text(report))

    def test_due_rows_and_also_open(self) -> None:
        led = self.ledger()
        led.add(kind="promise", project="api", local_title="check the deploy", title="api: check", due_at="2026-11-08", now=NOW)
        led.add(kind="promise", project="api", local_title="send the invoice", title="api: invoice", due_at="2026-11-03", now=NOW - timedelta(days=6))
        led.add(kind="decision", project="web", local_title="pick a font", title="web: font", due_at="2026-11-09", now=NOW)
        led.add(kind="promise", project="web", local_title="renew the cert", title="web: cert", due_at="2026-11-10T14:00", now=NOW)
        led.add(kind="promise", project="web", local_title="far away", title="web: far", due_at="2026-11-20", now=NOW)
        led.add(kind="decision", project="web", local_title="no date", title="web: no date", now=NOW)
        done, _ = led.add(kind="promise", project="web", local_title="already done", title="web: done", due_at="2026-11-08", now=NOW)
        led.set_status(done["id"], "done", now=NOW)
        self.s.question("web", self.ago(days=1))
        report = self.gather()
        due = [r for r in report.rows if r["kind"] == "due"]
        self.assertEqual([r["title"] for r in due], ["pick a font", "renew the cert", "send the invoice", "check the deploy"])
        self.assertEqual(report.counts["also_open"], 2)
        self.assertEqual(report.counts["waiting"], 5)
        text = pull.render_text(report)
        self.assertIn('◷ due tomorrow · "pick a font"   (ghosted add, Sun 8)', text)
        self.assertIn('◷ due Tue 10, 2:00 PM · "renew the cert"', text)
        self.assertIn('◷ overdue since Tue 3 · "send the invoice"   (ghosted add, Mon 2)', text)
        self.assertIn('◷ due TODAY · "check the deploy"', text)
        self.assertIn("Also open: 2 items with no date soon (ghosted list).", text)
        # web's session row first, then its due rows; api (due only) after
        self.assertLess(text.index("web ─"), text.index("api ─"))
        web = text[text.index("web ─"):text.index("api ─")]
        self.assertLess(web.index("ended on a question"), web.index("pick a font"))
        # the header counts the session and the reminders apart
        self.assertEqual(text.splitlines()[0], "GHOSTED · Mon 2 – Sun 8 Nov · 1 waiting on you · 4 reminders due   "
                                               "(1 session checked, 0 clean)")
        self.assertIn("\x1b[1;33m1 waiting on you · 4 reminders due\x1b[0m", pull.colorize(text))

    def test_reminders_alone_keep_the_all_clear(self) -> None:
        led = self.ledger()
        led.add(kind="promise", project="api", local_title="check the deploy", title="api: check", due_at="2026-11-08", now=NOW)
        self.s.clean("web", self.ago(days=1))
        lines = pull.render_text(self.gather()).splitlines()
        self.assertEqual(lines[0], "GHOSTED · Mon 2 – Sun 8 Nov · 1 reminder due")
        self.assertEqual(lines[2:4], ["✓ You didn't ghost anything in the last 7 days.",
                                      "1 session in 1 project checked"])
        self.assertEqual(lines[5], "")                   # the tick block, a blank line, then the reminders
        self.assertTrue(lines[6].startswith("api ─"))
        self.assertIn('◷ due TODAY · "check the deploy"', "\n".join(lines))
        md = pull.markdown("\n".join(lines))
        self.assertIn("**Ghosted** · Mon 2 – Sun 8 Nov · **1 reminder due**", md)

    def test_reminders_with_no_session_checked(self) -> None:
        led = self.ledger()
        led.add(kind="promise", project="api", local_title="check the deploy", title="api: check", due_at="2026-11-08", now=NOW)
        text = pull.render_text(self.gather())
        self.assertIn("No session is waiting on you in the last 7 days (0 sessions checked).", text.splitlines())
        self.assertNotIn("✓", text)

    def test_due_today_timed_and_passed(self) -> None:
        led = self.ledger()
        led.add(kind="promise", project="api", local_title="call back", title="api: call", due_at="2026-11-08T09:00", now=NOW)
        self.assertIn('◷ overdue since 9:00 AM today · "call back"', self.text())


# ---- hook records ---------------------------------------------------------------------------------

class HookTests(PullCase):
    def test_stopfailure_limit_newer_than_the_transcript(self) -> None:
        sid = self.s.clean("billing", self.ago(days=1))
        self.stop(sid, "StopFailure", "usage_limit", "rate_limit", self.ago(days=1) + timedelta(minutes=1))
        report = self.gather()
        (row,) = report.rows
        self.assertEqual((row["session_uuid"], row["kind"], row["source"], row["n"]), (sid, "usage_limit", "hook", None))
        self.assertTrue(report.sources["hook"])

    def test_older_stopfailure_does_nothing(self) -> None:
        sid = self.s.clean("billing", self.ago(days=1))
        self.stop(sid, "StopFailure", "usage_limit", "rate_limit", self.ago(days=1, minutes=5))
        self.assertEqual(self.gather().rows, [])

    def test_clean_stop_more_than_60s_later_drops_the_row(self) -> None:
        late = self.s.question("shop", self.ago(days=2))
        near = self.s.question("shop", self.ago(days=1))
        self.stop(late, "Stop", "clean", "no_question", self.ago(days=2) + timedelta(seconds=61))
        self.stop(near, "Stop", "clean", "no_question", self.ago(days=1) + timedelta(seconds=30))
        report = self.gather()
        self.assertEqual([(r["session_uuid"], r["source"]) for r in report.rows], [(near, "hook")])
        self.assertEqual(report.counts["clean"], 1)

    def test_source_is_transcript_without_a_record(self) -> None:
        self.s.question("shop", self.ago(days=1))
        report = self.gather()
        self.assertEqual(report.rows[0]["source"], "transcript")
        self.assertFalse(report.sources["hook"])
        self.assertEqual(json.loads(pull.render_json(report))["sources"]["transcripts"], "best-effort backfill")


# ---- live agents ----------------------------------------------------------------------------------

class LiveTests(PullCase):
    def test_busy_skipped_blocked_waiting_idle_open(self) -> None:
        busy = self.s.question("shop", self.ago(days=1))
        blocked = self.s.clean("api", self.ago(days=2))
        idle = self.s.question("web", self.ago(days=3))
        live = {busy: agents.LiveState("interactive", "busy", 11),
                blocked: agents.LiveState("background", "blocked"),
                idle: agents.LiveState("interactive", "idle", 12)}
        report = self.gather(live=live)
        rows = {r["session_uuid"]: r for r in report.rows}
        self.assertNotIn(busy, rows)
        self.assertEqual(report.counts["active"], 1)
        self.assertEqual((rows[blocked]["kind"], rows[blocked]["tier"], rows[blocked]["reason"]),
                         ("agent_blocked", "waiting", "agent_blocked"))
        self.assertTrue(rows[idle]["open_in_terminal"])
        self.assertTrue(report.sources["agents"])
        text = pull.render_text(report)
        self.assertIn("● Fri 6 · 2d ago · background session is blocked on you", text)
        self.assertIn("still open in a terminal", text)
        self.assertIn("(3 sessions checked, 0 clean · 1 active now)", text)

    def test_blocked_session_still_deferred_as_active(self) -> None:
        sid = self.s.question("api", self.ago(minutes=5))   # maybe active: the detector defers it
        self.assertEqual(self.gather().rows, [])
        self.assertEqual(self.gather().counts["active"], 1)
        report = self.gather(live={sid: agents.LiveState("background", "blocked")})
        (row,) = report.rows
        self.assertEqual((row["session_uuid"], row["kind"], row["project"]), (sid, "agent_blocked", "api"))
        self.assertEqual(report.counts["active"], 0)


# ---- JSON and privacy -----------------------------------------------------------------------------

class JsonPrivacyTests(PullCase):
    def test_json_has_no_quotes_unless_asked_and_nothing_is_stored(self) -> None:
        secret_q = "Should the banner use the teal or the coral variant for zebra-launch-8841?"
        self.s.question("shop", self.ago(days=1), text=secret_q)
        self.s.ask_user("inventory", self.ago(days=2))
        report = self.gather()
        plain_json = pull.render_json(report)
        self.assertNotIn("zebra-launch-8841", plain_json)
        self.assertNotIn(Q_BOX, plain_json)
        data = json.loads(plain_json)
        self.assertEqual(set(data), {"version", "generated_at", "window", "first_run", "older", "project", "counts", "sources",
                                     "scan", "drift", "rows", "quiet_projects", "unsaved", "shared_now"})
        self.assertEqual(set(data["counts"]), {"checked", "clean", "waiting", "soft", "active", "handled",
                                               "automated", "also_open", "unsaved"})
        self.assertTrue(all("quote" not in r for r in data["rows"]))
        self.assertTrue(all(not k.startswith("_") for r in data["rows"] for k in r))
        quoted = json.loads(pull.render_json(report, quotes=True))
        self.assertIn("zebra-launch-8841", json.dumps(quoted))
        self.assertIn(Q_BOX, [r["quote"] for r in quoted["rows"]])
        self.assertIn("zebra-launch-8841", pull.render_text(report))
        for path, content in self.files_under_home().items():
            self.assertNotIn("zebra-launch-8841", content, os.path.basename(path))
            self.assertNotIn(Q_BOX, content, os.path.basename(path))

    def test_quotes_are_masked(self) -> None:
        self.s.question("shop", self.ago(days=1), text="Is the $4,200 quote for sam@example.com final, or a draft?")
        text = self.text()
        self.assertNotIn("sam@example.com", text)
        self.assertIn('"Is the [amount] quote for [email] final, or a draft?"', text)


# ---- the receipt (contract 16.20) ---------------------------------------------------------------------

class ReceiptTests(PullCase):
    def test_numbers_only(self) -> None:
        uuid = self.s.question("shop", self.ago(days=3), text="Teal or coral for zebra-launch-8841?")
        self.s.clean("docs", self.ago(days=1))
        self.ledger().add(kind="promise", project="api", local_title="check the deploy", title="api: check",
                          due_at="2026-11-08", now=NOW)            # a reminder: not counted
        out = pull.render_receipt(self.gather())
        self.assertEqual(out.splitlines(), ["GHOSTED · 7 days of Claude Code (Mon 2 – Sun 8 Nov)",
                                            "2 sessions in 2 projects checked",
                                            "1 ghosted · longest wait 3 days",
                                            "github.com/byndp/ghosted"])
        for private in ("zebra", "shop", "docs", "api", "deploy", uuid):
            self.assertNotIn(private, out)

    def test_nothing_ghosted(self) -> None:
        self.s.clean("docs", self.ago(days=1))
        self.assertIn("0 ghosted ✓", pull.render_receipt(self.gather()).splitlines())

    def test_no_tick_when_nothing_was_checked(self) -> None:
        lines = pull.render_receipt(self.gather("today")).splitlines()
        self.assertEqual(lines[:3], ["GHOSTED · today in Claude Code", "0 sessions in 0 projects checked",
                                     "0 ghosted"])

    def test_span_words(self) -> None:
        report = self.gather()
        for days, words in ((1, "1 day"), (13, "13 days"), (14, "2 weeks"), (17, "2 weeks"), (18, "3 weeks"),
                            (77, "11 weeks")):
            report.days = days
            self.assertEqual(pull._span(report), words)


# ---- the CLI ----------------------------------------------------------------------------------------

class CliTests(PullCase):
    def test_colour_only_when_asked_or_on_a_terminal(self) -> None:
        self.s.question("shop", self.ago(days=3))
        code, out, _ = self.run_cli("report", "7d")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", out)                       # a pipe (and the skill): no codes
        code, out, _ = self.run_cli("report", "7d", env=self.env(FORCE_COLOR="1"))
        self.assertIn("\x1b[", out)
        code, out, _ = self.run_cli("report", "7d", "--json", env=self.env(FORCE_COLOR="1"))
        self.assertNotIn("\x1b[", out)                       # never in JSON
        json.loads(out)

    def test_markdown_flag(self) -> None:
        self.s.question("shop", self.ago(days=3))
        code, out, err = self.run_cli("report", "7d", "--via", "skill", "--markdown", env=self.env(FORCE_COLOR="1"))
        self.assertEqual(code, 0, err)
        self.assertIn("### shop", out.split("\n"))
        self.assertNotIn("\x1b[", out)                       # Markdown is never coloured
        code, _, err = self.run_cli("report", "--markdown", "--json")
        self.assertEqual(code, 2)

    def test_receipt_is_not_a_report(self) -> None:
        self.s.question("shop", self.ago(days=40))
        code, out, err = self.run_cli("receipt")                   # default window: all
        self.assertEqual(code, 0, err)
        self.assertIn("1 ghosted · longest wait 40 days", out.splitlines())
        self.assertNotIn("shop", out)
        code, out, err = self.run_cli("report")                    # still the first report: 30 days
        self.assertEqual(code, 0, err)
        self.assertIn("First run: this looked back 30 days.", out)
        self.assertIn("Your numbers only, safe to post: ghosted receipt", out.splitlines())
        code, _, _ = self.run_cli("receipt", "bogus")
        self.assertEqual(code, 2)

    def test_all_via_the_cli(self) -> None:
        self.s.question("api", self.ago(days=200))
        code, out, err = self.run_cli("report", "all", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["window"]["name"], data["counts"]["waiting"]), ("all", 1))
        code, _, err = self.run_cli("report", "4000d")
        self.assertEqual(code, 2)
        self.assertIn("3650d", err)

    def env(self, **extra: str) -> Dict[str, str]:
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home), "GHOSTED_PROJECTS_DIR": str(self.projects),
               "GHOSTED_NOW": timeutil.to_iso(NOW), "PATH": str(self.root / "nobin")}
        env.update(extra)
        return env

    def run_cli(self, *argv: str, env: Optional[Dict[str, str]] = None):
        self.home.mkdir(mode=0o700, exist_ok=True)
        cfg = self.home / "config.toml"
        if not cfg.exists():
            cfg.write_text('timezone = "%s"\nworkspace_roots = [%s]\n[skip]\ntemp_folders = false\n'
                           % (TZ, json.dumps(str(self.ws))))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), environ=env or self.env())
        return code, out.getvalue(), err.getvalue()

    def test_flags(self) -> None:
        self.s.question("shop", self.ago(days=1))
        self.s.question("api", self.ago(days=2), text=OFFER)
        code, out, err = self.run_cli("report", "7d", "--soft", "--plain", "--project", "api", "--via", "skill")
        self.assertEqual(code, 0, err)
        self.assertIn("api ─", out)
        self.assertNotIn("shop ─", out)
        self.assertIn("To pick it back up, paste:", out)
        code, out, _ = self.run_cli("report", "--no-quotes")
        self.assertNotIn(Q_STICKY, out)
        code, out, _ = self.run_cli("report", "--json", "--quotes")
        self.assertIn(Q_STICKY, out)
        vias = [r["via"] for r in Ledger(self.cfg()).measures() if r.get("type") == "report"]
        self.assertEqual(vias, ["skill", "cli", "cli"])

    def test_usage_errors(self) -> None:
        for argv in (("report", "week"), ("report", "0d"), ("report", "3651d"), ("report", "--quotes", "--no-quotes"),
                     ("report", "7d", "--days", "7"), ("report", "--via", "phone"), ("report", "--project", " ")):
            code, _, _ = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)

    def test_dismiss_by_the_number_shown(self) -> None:
        self.s.question("shop", self.ago(days=1))
        code, out, _ = self.run_cli("report", "--json")
        n = json.loads(out)["rows"][0]["n"]
        code, out, err = self.run_cli("dismiss", str(n))
        self.assertEqual(code, 0, err)
        self.assertIn("dismissed (not real)", out)
        item = Ledger(self.cfg()).get(n)
        self.assertEqual((item["status"], item["dismiss_reason"]), ("dismissed", "not_real"))
        code, out, _ = self.run_cli("report", "--json")
        self.assertEqual(json.loads(out)["counts"]["handled"], 1)


# ---- review fixes (2026-11-08) ------------------------------------------------------------------

class ReviewFixTests(PullCase):
    def test_hook_limit_record_keeps_a_stored_limit_reset_and_reason(self) -> None:
        ended = self.ago(days=2)
        sid = self.s.limit_session("billing", ended, resets=self.ago(days=2, hours=-1))
        self.stop(sid, "StopFailure", "usage_limit", "rate_limit", ended + timedelta(seconds=2))
        report = self.gather()
        (row,) = report.rows
        self.assertEqual((row["session_uuid"], row["kind"], row["reason"]), (sid, "usage_limit", "limit_meta"))
        self.assertIsNotNone(row["resets_at"])
        self.assertEqual(row["ended_at"], timeutil.to_iso(ended))
        self.assertIn("limit has reset", pull.render_text(report))

    def test_project_filter_counts_never_processed_active_sessions(self) -> None:
        self.s.question("api", self.ago(minutes=5))
        self.s.question("web", self.ago(minutes=4))
        self.assertEqual(self.gather().counts["active"], 2)
        report = self.gather(project="api")
        self.assertEqual(report.counts["active"], 1)
        self.assertIn("1 active now", pull.render_text(report))
        self.assertEqual(self.gather(project="other").counts["active"], 0)

    def test_v1_limit_deferral_without_the_extras_is_still_listed(self) -> None:
        sid = self.s.limit_session("billing", self.ago(hours=2), resets=NOW + timedelta(hours=3))
        cfg = self.cfg()
        self.gather(cfg=cfg)
        led = Ledger(cfg)
        state = led.load_state()
        entry = state["deferred_info"][sid]
        state["deferred_info"][sid] = {k: entry[k] for k in ("why", "size", "mtime", "version", "until")}
        led.save_state(state)
        report = self.gather(cfg=cfg)
        (row,) = report.rows
        self.assertEqual((row["session_uuid"], row["kind"], row["project"], row["n"]),
                         (sid, "usage_limit", "billing", None))
        self.assertEqual(report.counts["checked"], 1)
        self.assertIn("can't resume until", pull.render_text(report))
        self.assertEqual(len(self.gather(cfg=cfg, project="billing").rows), 1)

    def test_blocked_rows_respect_item_status(self) -> None:
        sid = self.s.question("shop", self.ago(days=1))
        (row,) = self.gather().rows
        led = self.ledger()
        led.set_status(row["id"], "dismissed", now=NOW, dismiss_reason="not_real")
        blocked = {sid: agents.LiveState("background", "blocked")}
        report = self.gather(live=blocked)
        self.assertEqual(report.rows, [])
        self.assertEqual(report.counts["handled"], 1)
        led.set_status(row["id"], "snoozed", now=NOW, snooze_until="2026-11-11T09:00")
        report = self.gather(live=blocked)
        self.assertEqual((report.rows, report.counts["handled"]), ([], 1))
        # The session went on after the snooze and is blocked again: it shows.
        later = (NOW + timedelta(seconds=30)).timestamp()
        path = next(self.projects.rglob(sid + ".jsonl"))
        os.utime(str(path), (later, later))
        (again,) = self.gather(live=blocked).rows
        self.assertEqual((again["session_uuid"], again["kind"]), (sid, "agent_blocked"))

    def test_deferred_blocked_rows_respect_item_status(self) -> None:
        sid = self.s.question("shop", self.ago(days=1))
        (row,) = self.gather().rows
        self.ledger().set_status(row["id"], "done", now=NOW)
        recent = self.ago(minutes=5).timestamp()  # touched before the act: maybe active, nothing new
        path = next(self.projects.rglob(sid + ".jsonl"))
        os.utime(str(path), (recent, recent))
        report = self.gather(live={sid: agents.LiveState("background", "blocked")})
        self.assertEqual(report.rows, [])
        self.assertEqual(report.counts["handled"], 1)


# ---- speed --------------------------------------------------------------------------------------

class SpeedTests(PullCase):
    def test_150_sessions_warm_30d_under_2s(self) -> None:
        for i in range(150):
            ended = self.ago(days=1 + (i % 28), minutes=i)
            project = "proj%02d" % (i % 12)
            if i % 5 == 0:
                self.s.question(project, ended)
            elif i % 7 == 0:
                self.s.ask_user(project, ended)
            else:
                self.s.clean(project, ended)
        self.keep_temp_sessions()
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home), "GHOSTED_PROJECTS_DIR": str(self.projects),
               "GHOSTED_NOW": timeutil.to_iso(NOW), "PATH": str(self.root / "nobin")}
        cold_start = time.monotonic()
        first = subprocess.run([sys.executable, str(LE), "report", "30d"], env=env, capture_output=True,
                               encoding="utf-8", timeout=120)
        cold = time.monotonic() - cold_start
        self.assertEqual(first.returncode, 0, first.stderr[-300:])
        warm_start = time.monotonic()
        second = subprocess.run([sys.executable, str(LE), "report", "30d"], env=env, capture_output=True,
                                encoding="utf-8", timeout=120)
        warm = time.monotonic() - warm_start
        self.assertEqual(second.returncode, 0, second.stderr[-300:])
        self.assertIn("150 sessions checked", second.stdout)
        self.assertLess(warm, 2.0, "warm %.2fs (cold %.2fs)" % (warm, cold))


if __name__ == "__main__":
    unittest.main()
