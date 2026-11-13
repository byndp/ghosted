"""Tests for core.detector: helpers, and end-to-end scans over a temp projects dir of fixture copies.

Every test builds its own GHOSTED_HOME and projects_dir under a temp directory and passes both to
Config explicitly; nothing reads ~/.claude/projects or ~/.ghosted. Assertion messages carry ids
and counts only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from core import classify, detector, evidence, ledger, timeutil, transcript
from core.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
TZ = "America/Chicago"
NOW = timeutil.parse_iso("2026-10-06T15:00:00Z")  # 10:00 in Chicago
OLD = 2 * 86400
GIT = shutil.which("git")
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

FRESH = "aaaaaaaa-0000-4000-8000-000000000001"     # pos_want_me_to copy, modified 5 minutes ago
SDK2 = "aaaaaaaa-0000-4000-8000-000000000002"      # sdk-cli with two human turns: not automated
GITREPO = "aaaaaaaa-0000-4000-8000-000000000003"   # pos_trailing_question launched in a real git repo
LIMIT = "aaaaaaaa-0000-4000-8000-000000000004"     # pos_limit_meta whose resetsAt is in the future


def at(delta: timedelta) -> Any:
    return NOW + delta


def git(cwd: Path, *args: str) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", **HERMETIC)
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fixture_uuid(path: Path) -> str:
    for raw in transcript.split_lines(path.read_bytes()):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
            return entry["sessionId"]
    raise AssertionError("fixture without a sessionId: " + path.name)


def rewrite(src: Path, fn: Callable[[Dict[str, Any]], List[Dict[str, Any]]]) -> bytes:
    """The fixture with each JSON object line replaced by fn(entry) (a list: drop or insert)."""
    out: List[bytes] = []
    for raw in transcript.split_lines(src.read_bytes()):
        try:
            entry = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        if not isinstance(entry, dict):
            out.append(raw)
            continue
        for e in fn(entry):
            out.append(json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    return b"".join(out)


def strings_in(path: Path) -> List[str]:
    """Every prompt, reply, tool input and tool result string in a fixture (for leak checks)."""
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if len(value.strip()) >= 20:
                found.append(value.strip())
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
    for raw in transcript.split_lines(path.read_bytes()):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict):
            walk(entry.get("message"))
            walk(entry.get("attachment"))
            walk(entry.get("toolUseResult"))
    return found


class Env:
    """A temp GHOSTED_HOME + projects_dir + workspace root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "ghosted-home"
        self.projects = root / "projects"
        self.workspace = root / "workspace"
        self.projects.mkdir()
        self.workspace.mkdir()
        self.paths: Dict[str, Path] = {}

    def cfg(self, **kw: Any) -> Config:
        # The fake projects live in a temp folder, so the temp-folder skip (contract 16.13) is off
        # except in the tests written for it.
        base: Dict[str, Any] = dict(home=self.home, projects_dir=self.projects, timezone=TZ,
                                    workspace_roots=[self.workspace, Path("/Users/sam/workspace")],
                                    skip_temp_folders=False)
        base.update(kw)
        return Config(**base)

    def write(self, uuid: str, data: bytes, age: float = OLD, folder: Optional[str] = None) -> Path:
        d = self.projects / (folder or ("-proj-" + uuid[:8]))
        d.mkdir(parents=True, exist_ok=True)
        path = d / (uuid + ".jsonl")
        path.write_bytes(data)
        self.touch(path, age)
        self.paths[uuid] = path
        return path

    def touch(self, path: Path, age: float, base: Any = None) -> None:
        when = (NOW if base is None else base).timestamp() - age
        os.utime(str(path), (when, when))

    def add_fixture(self, name: str, uuid: Optional[str] = None, age: float = OLD,
                    fn: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None) -> Tuple[str, Path]:
        src = FIXTURES / name
        uuid = uuid or fixture_uuid(src)
        data = rewrite(src, fn) if fn is not None else src.read_bytes()
        return uuid, self.write(uuid, data, age)

    def append(self, uuid: str, entries: List[Dict[str, Any]], age: float, base: Any = None) -> None:
        path = self.paths[uuid]
        with open(path, "ab") as f:
            for e in entries:
                f.write(json.dumps(e, separators=(",", ":")).encode("utf-8") + b"\n")
        self.touch(path, age, base)


def envelope(etype: str, ts: str, cwd: str = "/Users/sam/workspace/widgets", **extra: Any) -> Dict[str, Any]:
    e: Dict[str, Any] = {"parentUuid": None, "isSidechain": False, "userType": "external", "cwd": cwd,
                         "version": "2.1.281", "gitBranch": "main", "entrypoint": "cli", "type": etype,
                         "uuid": "x-" + ts, "timestamp": ts}
    e.update(extra)
    return e


def clean_ending(day: str = "2026-10-02", cwd: str = "/Users/sam/workspace/widgets") -> List[Dict[str, Any]]:
    """A human answer, a tool call and a clean final text on ``day`` (16:00Z = 11:00 Chicago)."""
    return [
        envelope("user", day + "T16:00:00.000Z", cwd, message={"role": "user", "content": "yes, commit them"}),
        envelope("assistant", day + "T16:00:05.000Z", cwd, message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_c1", "name": "Bash", "input": {"command": "git commit -am format"}}]}),
        envelope("user", day + "T16:00:09.000Z", cwd, toolUseResult={"stdout": "ok"}, message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_c1", "content": "1 commit"}]}),
        envelope("assistant", day + "T16:00:12.000Z", cwd, message={"role": "assistant", "content": [
            {"type": "text", "text": "Committed the formatting changes."}]}),
        envelope("system", day + "T16:00:13.000Z", cwd, subtype="turn_duration", durationMs=12000),
    ]


def question_ending(day: str, cwd: str = "/Users/sam/workspace/widgets") -> List[Dict[str, Any]]:
    return [
        envelope("user", day + "T17:00:00.000Z", cwd, message={"role": "user", "content": "now the changelog"}),
        envelope("assistant", day + "T17:00:05.000Z", cwd, message={"role": "assistant", "content": [
            {"type": "text", "text": "The changelog is drafted. Should I add it to the release notes too?"}]}),
    ]


def sdk_two_turns(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """auto_sdk_cli_question with a second human prompt in the head (a Remote Control conversation)."""
    if entry.get("type") == "user" and isinstance((entry.get("message") or {}).get("content"), str):
        second = dict(entry, uuid="second-prompt", message={"role": "user", "content": "and check the warnings too"})
        return [entry, second]
    return [entry]


class FixtureSetMixin:
    """setUp: every synthetic fixture, plus FRESH, SDK2 and GITREPO."""

    env: Env
    repo: Optional[Path]

    def build(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(Path(self.tmp.name))
        self.uuids: Dict[str, str] = {}
        for p in sorted(FIXTURES.glob("*.jsonl")):
            uuid, _ = self.env.add_fixture(p.name)
            self.uuids[p.name] = uuid
        self.env.add_fixture("pos_want_me_to.jsonl", FRESH, age=5 * 60)
        self.env.add_fixture("auto_sdk_cli_question.jsonl", SDK2, fn=sdk_two_turns)
        self.repo = None
        repo_cwd = "/Users/sam/workspace/widgets"
        if GIT is not None:
            self.repo = self.env.workspace / "chipmunk"
            self.repo.mkdir()
            git(self.repo, "init", "-q", "-b", "round-2")
            (self.repo / "a.txt").write_text("a", encoding="utf-8")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", "first")
            for name in ("x.txt", "y.txt", "z.txt"):
                (self.repo / name).write_text(name, encoding="utf-8")
            repo_cwd = str(self.repo)

        def in_repo(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            if "cwd" in entry:
                entry = dict(entry, cwd=repo_cwd)
            return [entry]
        self.env.add_fixture("pos_trailing_question.jsonl", GITREPO, fn=in_repo)
        self.cfg = self.env.cfg()
        self.ledger = ledger.Ledger(self.cfg)
        patcher = mock.patch.dict(os.environ, HERMETIC)
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]
        self.addCleanup(self.tmp.cleanup)  # type: ignore[attr-defined]

    def expected_flagged(self) -> Dict[str, Tuple[str, str]]:
        out = {}
        for name, uuid in self.uuids.items():
            if name.startswith("auto_"):
                continue
            ending = classify.classify(transcript.parse_transcript(FIXTURES / name), classify.DEFAULT_IGNORE_TAIL_PATTERNS)
            if ending.flagged:
                out[uuid] = tuple(ending)
        # "Should I open tickets for them?" is a polite offer; GITREPO is pos_trailing_question
        # ("Want me to commit these 18 files?"), an offer to save the work (contract 16.1).
        out[SDK2] = ("open_question", "offer_q")
        out[GITREPO] = ("open_question", "save_offer")
        return out

    def inbox_lines(self) -> List[Dict[str, Any]]:
        return ledger.read_jsonl(self.ledger.inbox_path)

    def item_for(self, uuid: str) -> Dict[str, Any]:
        item = self.ledger.find_by_session(uuid)
        assert item is not None, uuid[:8]
        return item


# ---- helpers -----------------------------------------------------------------------------------------

class ProjectForTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace"
        (self.ws / "boathouse" / "app" / "src").mkdir(parents=True)
        self.cfg = Config(home=self.root / "h", projects_dir=self.root / "p", workspace_roots=[self.ws])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_workspace_roots(self) -> None:
        self.assertEqual(detector.project_for(str(self.ws / "boathouse"), self.cfg), "boathouse")
        self.assertEqual(detector.project_for(str(self.ws / "boathouse" / "app" / "src"), self.cfg), "boathouse")
        self.assertEqual(detector.project_for(str(self.ws), self.cfg), "workspace")
        self.assertEqual(detector.project_for(str(self.ws) + "/boathouse/", self.cfg), "boathouse")
        self.assertEqual(detector.project_for(str(self.ws) + "-other/x", self.cfg), "x")  # not under the root

    def test_git_toplevel_then_basename(self) -> None:
        repo = self.root / "elsewhere" / "tinta"
        (repo / "deep" / "er").mkdir(parents=True)
        (repo / ".git").mkdir()
        self.assertEqual(detector.project_for(str(repo / "deep" / "er"), self.cfg), "tinta")
        worktree = self.root / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /x", encoding="utf-8")  # a worktree's .git is a file
        self.assertEqual(detector.project_for(str(worktree), self.cfg), "wt")
        self.assertEqual(detector.project_for(str(self.root / "plain" / "notes"), self.cfg), "notes")
        self.assertEqual(detector.project_for("/", self.cfg), "/")

    def test_missing_cwd(self) -> None:
        for cwd in (None, "", "   ", 5, "a\x00b"):
            self.assertEqual(detector.project_for(cwd, self.cfg), "unknown")  # type: ignore[arg-type]

    def test_symlinked_root(self) -> None:
        link = self.root / "ws-link"
        link.symlink_to(self.ws)
        cfg = Config(home=self.root / "h", projects_dir=self.root / "p", workspace_roots=[link])
        self.assertEqual(detector.project_for(str(self.ws / "boathouse" / "app"), cfg), "boathouse")
        self.assertEqual(detector.project_for(str(link / "boathouse"), cfg), "boathouse")


class ResumeCommandTests(unittest.TestCase):
    U = "0b5e6c1d-2f3a-4b7c-8d9e-0a1b2c3d4e5f"

    def test_under_home(self) -> None:
        self.assertEqual(detector.resume_command("/Users/sam/workspace/boathouse", self.U, home="/Users/sam"),
                         "cd ~/workspace/boathouse && claude --resume " + self.U)
        self.assertEqual(detector.resume_command("/Users/sam", self.U, home="/Users/sam/"), "cd ~ && claude --resume " + self.U)

    def test_quoting(self) -> None:
        self.assertEqual(detector.resume_command("/Users/sam/My Projects/a'b", self.U, home="/Users/sam"),
                         "cd ~/'My Projects/a'\"'\"'b' && claude --resume " + self.U)
        self.assertEqual(detector.resume_command("/tmp/with space", self.U, home="/Users/sam"),
                         "cd '/tmp/with space' && claude --resume " + self.U)

    def test_outside_home_and_prefix_trap(self) -> None:
        self.assertEqual(detector.resume_command("/Users/samuel/x", self.U, home="/Users/sam"),
                         "cd /Users/samuel/x && claude --resume " + self.U)
        self.assertEqual(detector.resume_command("/srv/app", self.U, home="/"), "cd /srv/app && claude --resume " + self.U)

    def test_no_cwd(self) -> None:
        self.assertEqual(detector.resume_command(None, self.U), "claude --resume " + self.U)

    def test_default_home_is_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "/Users/zed"}):
            self.assertEqual(detector.resume_command("/Users/zed/w/p", self.U), "cd ~/w/p && claude --resume " + self.U)


class TitleTests(unittest.TestCase):
    def test_template(self) -> None:
        d = timeutil.parse_date("2026-10-01")
        self.assertEqual(detector.interrupted_title("boathouse", d, "usage_limit"), "boathouse: session 10-01 ended on usage limit")
        self.assertEqual(detector.interrupted_title("chipmunk", d, "open_question"), "chipmunk: session 10-01 ended on an open question")

    def test_safe_title_aliases_a_refused_project(self) -> None:
        d = timeutil.parse_date("2026-10-01")
        self.assertEqual(detector.safe_title("boathouse", d, "usage_limit"),
                         ("boathouse", "boathouse: session 10-01 ended on usage limit"))
        for bad in ("bob@example.com", "x" * 70, "ghp_" + "a1B2" * 10):
            with self.subTest(project=bad[:12]):
                project, title = detector.safe_title(bad, d, "open_question")
                self.assertRegex(project, r"^project-[0-9a-f]{6}$")
                self.assertEqual(title, project + ": session 10-01 ended on an open question")
                ledger.check_interrupted_title(title, project)
                self.assertEqual(detector.safe_title(bad, d, "open_question")[0], project)  # stable


class AutomatedTests(unittest.TestCase):
    def cfg(self, **kw: Any) -> Config:
        return Config(home=Path("/nonexistent-ghosted-home"), projects_dir=Path("/nonexistent-projects"), **kw)

    def s(self, **kw: Any) -> transcript.SessionSummary:
        return transcript.SessionSummary(session_uuid="u", **kw)

    def test_entrypoint_needs_at_most_one_human_turn(self) -> None:
        cfg = self.cfg()
        self.assertEqual(detector.is_automated(self.s(entrypoints={"sdk-cli"}, head_human_turns=1), cfg), "entrypoint")
        self.assertEqual(detector.is_automated(self.s(entrypoints={"sdk-cli"}, head_human_turns=0), cfg), "entrypoint")
        self.assertEqual(detector.is_automated(self.s(entrypoints={"cli", "sdk-cli"}, head_human_turns=1), cfg), "entrypoint")
        self.assertIsNone(detector.is_automated(self.s(entrypoints={"sdk-cli"}, head_human_turns=2), cfg))
        self.assertIsNone(detector.is_automated(self.s(entrypoints={"cli"}, head_human_turns=1), cfg))
        self.assertIsNone(detector.is_automated(self.s(entrypoints={"sdk-cli"}, head_human_turns=1), self.cfg(skip_entrypoints=[])))

    def test_first_prompt_patterns(self) -> None:
        cfg = self.cfg(skip_first_prompt_patterns=[r"^Nightly research:", r"(?i)weekly finance audit"])
        self.assertEqual(detector.is_automated(self.s(first_human_text="Nightly research: AI tooling", head_human_turns=5), cfg), "first_prompt")
        self.assertEqual(detector.is_automated(self.s(first_human_text="Run the WEEKLY finance audit"), cfg), "first_prompt")
        self.assertIsNone(detector.is_automated(self.s(first_human_text="Please do nightly research:"), cfg))
        self.assertIsNone(detector.is_automated(self.s(first_human_text=None), cfg))


class ResetDeadlineTests(unittest.TestCase):
    def test_deadlines(self) -> None:
        now = NOW.timestamp()
        self.assertEqual(detector.reset_deadline(int(now) + 7200, now, 1800), int(now) + 9000)
        self.assertEqual(detector.reset_deadline((int(now) + 7200) * 1000, now, 1800), int(now) + 9000)  # ms
        self.assertEqual(detector.reset_deadline(int(now) - 600, now, 1800), int(now) + 1200)  # reset passed, grace left
        self.assertIsNone(detector.reset_deadline(int(now) - 3600, now, 1800))
        self.assertIsNone(detector.reset_deadline(int(now) + 30 * 86400, now, 1800))  # implausible
        for bad in (None, True, "1791320400", float("nan"), -5, 0):
            self.assertIsNone(detector.reset_deadline(bad, now, 1800))


# ---- end to end --------------------------------------------------------------------------------------

class EndToEndTests(FixtureSetMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.build()

    def test_first_scan(self) -> None:
        r = detector.scan(self.cfg, self.ledger, NOW)
        expected = self.expected_flagged()
        n_files = len(self.uuids) + 3
        self.assertEqual(r.errors, [])
        self.assertEqual(r.sessions_seen, n_files)
        self.assertEqual(r.sessions_scanned, n_files - 1)  # all but the fresh one
        self.assertEqual(r.deferred, 1)
        self.assertEqual(r.skipped_automated, 1)
        self.assertEqual(r.items_added, len(expected))
        self.assertEqual(r.items_updated, 0)
        self.assertEqual({u: (v, why) for u, v, why, _ in r.flagged}, expected)
        self.assertEqual(len(expected), 14)  # 12 flagged fixtures + SDK2 + GITREPO
        items = {it["session_uuid"]: it for it in self.ledger.items().values()}
        self.assertEqual(set(items), set(expected))
        for uuid, (verdict, reason) in expected.items():
            item = items[uuid]
            with self.subTest(session=uuid[:8]):
                summary = transcript.parse_transcript(self.env.paths[uuid])
                ended = timeutil.local_date(timeutil.parse_iso(summary.final_ts), TZ)
                project = detector.project_for(summary.launch_cwd, self.cfg)
                what = "usage limit" if verdict == "usage_limit" else "an open question"
                self.assertEqual(item["title"], "%s: session %s ended on %s" % (project, timeutil.mmdd(ended), what))
                self.assertEqual(item["local_title"], item["title"])
                self.assertEqual((item["kind"], item["owner"], item["status"], item["rev"]), ("interrupted", "me", "open", 1))
                self.assertEqual(item["review_at"], "2026-10-13")
                self.assertEqual(item["reason"], reason)
                self.assertFalse(item["looks_resolved"])
                self.assertTrue(item["evidence"].startswith(evidence.ENDINGS[verdict] + " · "))
                self.assertEqual(item["resume"], detector.resume_command(summary.launch_cwd, uuid))
                self.assertEqual(item["content_version"], transcript.content_version(self.env.paths[uuid]))
                self.assertEqual(item["observed_at"], timeutil.to_iso(NOW))
        # local dates: the sdk-cli fixture ran at 02:00Z on Oct 1, which is Sep 30 in Chicago
        self.assertEqual(items[SDK2]["title"], "acme-api: session 09-30 ended on an open question")
        self.assertEqual(items[self.uuids["pos_limit_meta.jsonl"]]["title"], "acme-api: session 10-01 ended on usage limit")
        if self.repo is not None:
            self.assertEqual(items[GITREPO]["title"], "chipmunk: session 10-01 ended on an open question")
            self.assertEqual(items[GITREPO]["evidence"], "ended on an open question · 3 uncommitted on round-2, no upstream")
        self.assertEqual(items[self.uuids["pos_trailing_question.jsonl"]]["evidence"], "ended on an open question · not a git repo")
        # The same save offer: waiting in a repo with uncommitted work, soft outside a repo (16.1).
        plain = items[self.uuids["pos_trailing_question.jsonl"]]
        self.assertEqual(classify.tier("open_question", plain["reason"],
                                       unsaved=evidence.unsaved_from_text(plain["evidence"])), "soft")
        if self.repo is not None:
            repo_item = items[GITREPO]
            self.assertEqual(classify.tier("open_question", repo_item["reason"],
                                           unsaved=evidence.unsaved_from_text(repo_item["evidence"])), "waiting")

    def test_inbox_holds_public_fields_and_no_transcript_text(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        events = self.inbox_lines()
        self.assertEqual(len(events), len(self.expected_flagged()))
        raw = self.ledger.inbox_path.read_text(encoding="utf-8")
        for ev in events:
            self.assertEqual(ev["type"], "item.upsert")
            for key in ("local_title", "reason", "consequential", "synced"):
                self.assertNotIn(key, ev["item"])
            ledger.check_interrupted_title(ev["item"]["title"], ev["item"]["project"])
        leaked = [p.name for p in FIXTURES.glob("*.jsonl") if any(s in raw for s in strings_in(p))]
        self.assertEqual(leaked, [])

    def test_scan_prints_nothing(self) -> None:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            detector.scan(self.cfg, self.ledger, NOW)
            detector.scan(self.cfg, self.ledger, at(timedelta(hours=3)))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_state_file(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        state = json.loads(self.ledger.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["since"], timeutil.to_iso(NOW - timedelta(days=14)))
        self.assertEqual(state["last_scan"], timeutil.to_iso(NOW))
        self.assertEqual(state["deferred"], [FRESH])
        self.assertEqual(state["deferred_info"][FRESH]["why"], "active")
        rec = state["sessions"][GITREPO]
        for key in ("size", "mtime", "version", "verdict", "reason", "item_id", "ended_at"):
            self.assertIn(key, rec)
        self.assertEqual(rec["verdict"], "open_question")
        self.assertEqual(rec["item_id"], self.item_for(GITREPO)["id"])
        auto = state["sessions"][self.uuids["auto_sdk_cli_question.jsonl"]]
        self.assertEqual((auto["verdict"], auto["reason"], auto["item_id"]), ("automated", "entrypoint", None))
        self.assertEqual(state["sessions"][self.uuids["neg_metadata_only.jsonl"]]["verdict"], "empty")
        self.assertEqual(os.stat(self.ledger.state_path).st_mode & 0o777, 0o600)

    def test_second_scan_is_quiet_and_reads_nothing(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        before = len(self.inbox_lines())
        with mock.patch.object(transcript, "content_version", wraps=transcript.content_version) as cv, \
                mock.patch.object(transcript, "parse_transcript", wraps=transcript.parse_transcript) as pt:
            t0 = time.perf_counter()
            r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=1)))
            elapsed = time.perf_counter() - t0
        self.assertEqual((r.items_added, r.items_updated, r.sessions_scanned, r.deferred), (0, 0, 0, 1))
        self.assertEqual(len(self.inbox_lines()), before)
        self.assertEqual(cv.call_count, 0)  # unchanged files are never hashed (the deferred one neither)
        self.assertEqual(pt.call_count, 0)
        self.assertLess(elapsed, 1.0)

    def test_touched_file_is_hashed_but_not_parsed(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        path = self.env.paths[self.uuids["neg_safe_to_clear.jsonl"]]
        self.env.touch(path, 3600)
        with mock.patch.object(transcript, "content_version", wraps=transcript.content_version) as cv, \
                mock.patch.object(transcript, "parse_transcript", wraps=transcript.parse_transcript) as pt:
            r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=1)))
        self.assertEqual(cv.call_count, 1)
        self.assertEqual(pt.call_count, 0)
        self.assertEqual(r.sessions_scanned, 0)
        state = self.ledger.load_state()
        self.assertEqual(state["sessions"][self.uuids["neg_safe_to_clear.jsonl"]]["mtime"], path.stat().st_mtime)

    def test_resumed_session_that_ends_clean(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        uuid = self.uuids["pos_trailing_question.jsonl"]
        item = self.item_for(uuid)
        later = at(timedelta(days=1))
        self.env.append(uuid, clean_ending("2026-10-02"), age=3600, base=later)
        before = len(self.inbox_lines())
        r = detector.scan(self.cfg, self.ledger, later)
        after = self.item_for(uuid)
        self.assertEqual(after["id"], item["id"])
        self.assertTrue(after["looks_resolved"])
        self.assertEqual(after["evidence"], "ended on an open question · resumed 10-02, ended clean · not a git repo")
        self.assertEqual((after["status"], after["rev"], after["title"]), ("open", 1, item["title"]))  # status untouched
        self.assertEqual(after["content_version"], transcript.content_version(self.env.paths[uuid]))
        new = [e for e in self.inbox_lines()[before:] if e["item"]["id"] == item["id"]]
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0]["item"]["looks_resolved"])
        self.assertEqual(new[0]["item"]["observed_at"], timeutil.to_iso(later))
        self.assertEqual((r.items_updated, r.items_added), (1, 1))  # the FRESH session went quiet too
        self.assertNotIn(uuid, [u for u, _, _, _ in r.flagged])

        # It is resumed again and ends on a new question: no longer looks resolved, new title date.
        latest = at(timedelta(days=2))
        self.env.append(uuid, question_ending("2026-10-03"), age=3600, base=latest)
        detector.scan(self.cfg, self.ledger, latest)
        again = self.item_for(uuid)
        self.assertFalse(again["looks_resolved"])
        self.assertEqual(again["title"], "widgets: session 10-03 ended on an open question")
        self.assertEqual(again["evidence"], "ended on an open question · not a git repo")
        self.assertEqual((again["status"], again["rev"]), ("open", 1))

    def test_detector_never_changes_status(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        done_uuid = self.uuids["pos_trailing_question.jsonl"]
        gone_uuid = self.uuids["pos_your_call.jsonl"]
        done_item, _ = self.ledger.set_status(self.item_for(done_uuid)["id"], "done", now=NOW)
        gone_item, _ = self.ledger.set_status(self.item_for(gone_uuid)["id"], "dismissed", now=NOW, dismiss_reason="not_real")
        later = at(timedelta(days=2))
        self.env.append(done_uuid, question_ending("2026-10-03"), age=3600, base=later)
        self.env.append(gone_uuid, clean_ending("2026-10-03", cwd="/Users/sam/workspace/orchard"), age=3600, base=later)
        detector.scan(self.cfg, self.ledger, later)
        a, b = self.item_for(done_uuid), self.item_for(gone_uuid)
        self.assertEqual((a["status"], a["rev"]), ("done", 2))
        self.assertEqual(a["title"], "widgets: session 10-03 ended on an open question")  # Mac-owned fields still refresh
        self.assertEqual((b["status"], b["rev"], b["dismiss_reason"]), ("dismissed", 2, "not_real"))
        self.assertTrue(b["looks_resolved"])
        statuses = [e for e in self.inbox_lines() if e["type"] == "item.status"]
        self.assertEqual(len(statuses), 2)  # only the two explicit set_status calls

    def test_deferred_session_is_retried_until_processed(self) -> None:
        r1 = detector.scan(self.cfg, self.ledger, NOW)
        self.assertIsNone(self.ledger.find_by_session(FRESH))
        # 10 minutes later it is 15 minutes old: still deferred, while other work goes on.
        self.env.append(self.uuids["neg_qa_session.jsonl"], clean_ending(), age=3600, base=at(timedelta(minutes=10)))
        r2 = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=10)))
        self.assertEqual((r1.deferred, r2.deferred, r2.sessions_scanned), (1, 1, 1))
        self.assertEqual(self.ledger.load_state()["deferred"], [FRESH])
        self.assertIsNone(self.ledger.find_by_session(FRESH))
        # A change while deferred keeps it deferred (it is active again).
        self.env.append(FRESH, [envelope("system", "2026-10-06T15:12:00.000Z", subtype="turn_duration")], age=60,
                        base=at(timedelta(minutes=12)))
        r3 = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=12)))
        self.assertEqual(r3.deferred, 1)
        # An hour later it has gone quiet: processed, item created, queue empty.
        r4 = detector.scan(self.cfg, self.ledger, at(timedelta(hours=2)))
        self.assertEqual(r4.deferred, 0)
        # pos_want_me_to: "Let me know if you want me to open the pull request." A phrase cue whose
        # sentence offers to save the work (a pull request) is save_offer (contract 16.1).
        self.assertEqual(self.item_for(FRESH)["reason"], "save_offer")
        self.assertIn(FRESH, [u for u, _, _, _ in r4.flagged])
        self.assertEqual(self.ledger.load_state()["deferred"], [])

    def test_deleted_deferred_file_leaves_the_queue(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        self.env.paths[FRESH].unlink()
        r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=5)))
        self.assertEqual(r.deferred, 0)
        state = self.ledger.load_state()
        self.assertEqual(state["deferred"], [])
        self.assertNotIn(FRESH, state["deferred_info"])
        self.assertNotIn(FRESH, state["sessions"])

    def test_automated_sessions(self) -> None:
        cfg = self.env.cfg(skip_first_prompt_patterns=[r"^Run the formatter"])
        r = detector.scan(cfg, ledger.Ledger(cfg), NOW)
        self.assertEqual(r.skipped_automated, 3)  # the sdk-cli fixture and both formatter sessions
        self.assertIsNone(self.ledger.find_by_session(self.uuids["auto_sdk_cli_question.jsonl"]))
        self.assertIsNone(self.ledger.find_by_session(self.uuids["pos_trailing_question.jsonl"]))
        self.assertIsNotNone(self.ledger.find_by_session(SDK2))  # sdk-cli with two human turns is a person
        state = self.ledger.load_state()
        self.assertEqual(state["sessions"][self.uuids["pos_trailing_question.jsonl"]]["reason"], "first_prompt")

    @unittest.skipIf(GIT is None, "git is not installed")
    def test_git_evidence_refresh(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        assert self.repo is not None
        (self.repo / "w.txt").write_text("w", encoding="utf-8")
        before = len(self.inbox_lines())
        r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=1)))
        new = self.inbox_lines()[before:]
        self.assertEqual([e["item"]["session_uuid"] for e in new], [GITREPO])
        self.assertEqual(new[0]["item"]["evidence"], "ended on an open question · 4 uncommitted on round-2, no upstream")
        self.assertEqual((r.evidence_refreshed, r.items_updated, r.sessions_scanned), (1, 1, 0))
        r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=2)))
        self.assertEqual((r.evidence_refreshed, len(self.inbox_lines())), (0, before + 1))
        # A closed item is not refreshed.
        self.ledger.set_status(self.item_for(GITREPO)["id"], "done", now=at(timedelta(minutes=3)))
        (self.repo / "v.txt").write_text("v", encoding="utf-8")
        before = len(self.inbox_lines())
        r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=4)))
        self.assertEqual((r.evidence_refreshed, len(self.inbox_lines())), (0, before))

    @unittest.skipIf(GIT is None, "git is not installed")
    def test_at_most_one_upsert_per_session_per_run(self) -> None:
        detector.scan(self.cfg, self.ledger, NOW)
        assert self.repo is not None
        (self.repo / "w.txt").write_text("w", encoding="utf-8")
        later = at(timedelta(days=1))
        self.env.append(GITREPO, clean_ending("2026-10-02", cwd=str(self.repo)), age=3600, base=later)
        before = len(self.inbox_lines())
        detector.scan(self.cfg, self.ledger, later)
        new = [e for e in self.inbox_lines()[before:] if e["item"]["session_uuid"] == GITREPO]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["item"]["evidence"],
                         "ended on an open question · resumed 10-02, ended clean · 4 uncommitted on round-2, no upstream")
        # The refresh keeps the "resumed" part on later runs.
        (self.repo / "u.txt").write_text("u", encoding="utf-8")
        detector.scan(self.cfg, self.ledger, later + timedelta(minutes=1))
        self.assertEqual(self.item_for(GITREPO)["evidence"],
                         "ended on an open question · resumed 10-02, ended clean · 5 uncommitted on round-2, no upstream")

    def test_one_bad_file_never_aborts_the_scan(self) -> None:
        bad = self.env.paths[self.uuids["pos_your_call.jsonl"]]
        real = transcript.parse_transcript

        def flaky(path: Any, *a: Any, **kw: Any) -> Any:
            if Path(path) == bad:
                raise ValueError("Your call. secret-ish transcript words")
            return real(path, *a, **kw)
        with mock.patch.object(transcript, "parse_transcript", flaky):
            r = detector.scan(self.cfg, self.ledger, NOW)
        self.assertEqual(r.errors, ["ValueError"])
        self.assertEqual(r.items_added, len(self.expected_flagged()) - 1)
        state_text = self.ledger.state_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-ish", state_text)
        self.assertNotIn(self.uuids["pos_your_call.jsonl"], json.loads(state_text)["sessions"])
        r = detector.scan(self.cfg, self.ledger, at(timedelta(minutes=1)))  # retried next run
        self.assertEqual((r.errors, r.items_added), ([], 1))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_unreadable_file(self) -> None:
        path = self.env.paths[self.uuids["neg_qa_session.jsonl"]]
        os.chmod(str(path), 0)
        try:
            r = detector.scan(self.cfg, self.ledger, NOW)
        finally:
            os.chmod(str(path), 0o600)
        self.assertEqual(r.errors, ["PermissionError"])
        self.assertEqual(r.items_added, len(self.expected_flagged()))

    def test_usage_limit_waits_for_the_reset(self) -> None:
        resets = int(NOW.timestamp()) + 2 * 3600

        def future_reset(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            if isinstance(entry.get("quotaLimits"), dict):
                entry = dict(entry, quotaLimits=dict(entry["quotaLimits"], resetsAt=resets))
            return [entry]
        self.env.add_fixture("pos_limit_meta.jsonl", LIMIT, fn=future_reset)
        r = detector.scan(self.cfg, self.ledger, NOW)
        self.assertIsNone(self.ledger.find_by_session(LIMIT))
        self.assertEqual((r.deferred, r.deferred_limit), (2, 1))
        info = self.ledger.load_state()["deferred_info"][LIMIT]
        self.assertEqual((info["why"], info["until"]), ("limit", timeutil.to_iso(NOW + timedelta(hours=2, minutes=30))))
        # What the report needs to list a limit still waiting for its reset (contract 16.3).
        summary = transcript.parse_transcript(self.env.paths[LIMIT])
        self.assertEqual(info["cwd"], summary.launch_cwd)
        self.assertEqual(info["project"], "acme-api")
        self.assertEqual(info["ended_at"], timeutil.to_iso(timeutil.parse_iso(summary.final_ts)))
        self.assertEqual(info["reason"], "limit_meta")
        self.assertEqual(info["resets_at"], timeutil.to_iso(NOW + timedelta(hours=2)))
        with mock.patch.object(transcript, "content_version", wraps=transcript.content_version) as cv, \
                mock.patch.object(transcript, "parse_transcript", wraps=transcript.parse_transcript) as pt:
            r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=1)))
        self.assertIn(LIMIT, self.ledger.load_state()["deferred"])
        self.assertEqual(cv.call_count, 0)  # waiting costs no reads
        self.assertNotIn(self.env.paths[LIMIT], [Path(c.args[0]) for c in pt.call_args_list])
        r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=2, minutes=31)))
        item = self.item_for(LIMIT)
        self.assertEqual(item["title"], "acme-api: session 10-01 ended on usage limit")
        state = self.ledger.load_state()
        self.assertNotIn(LIMIT, state["deferred"])
        self.assertEqual(state["sessions"][LIMIT]["resets_at"], timeutil.to_iso(NOW + timedelta(hours=2)))

    def test_usage_limit_that_continued_after_the_reset_is_not_an_item(self) -> None:
        resets = int(NOW.timestamp()) + 3600

        def future_reset(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            if isinstance(entry.get("quotaLimits"), dict):
                entry = dict(entry, quotaLimits=dict(entry["quotaLimits"], resetsAt=resets))
            return [entry]
        self.env.add_fixture("pos_limit_meta.jsonl", LIMIT, fn=future_reset)
        detector.scan(self.cfg, self.ledger, NOW)
        # The subscription session continued on its own after the reset and finished.
        self.env.append(LIMIT, clean_ending("2026-10-06", cwd="/Users/sam/workspace/acme-api"), age=3600,
                        base=at(timedelta(hours=3)))
        r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=3)))
        self.assertIsNone(self.ledger.find_by_session(LIMIT))
        self.assertEqual(self.ledger.load_state()["sessions"][LIMIT]["verdict"], "clean")
        self.assertNotIn(LIMIT, [u for u, _, _, _ in r.flagged])

    def test_unapplied_upsert_is_retried(self) -> None:
        # The ledger holds an observation from later than this run's clock (a run with a skewed
        # clock, or a replay): the update is ignored, so the session must stay queued.
        uuid = self.uuids["pos_trailing_question.jsonl"]
        detector.scan(self.cfg, self.ledger, at(timedelta(days=3)))
        self.env.append(uuid, clean_ending("2026-10-02"), age=3600, base=at(timedelta(days=1)))
        detector.scan(self.cfg, self.ledger, at(timedelta(days=1)))
        self.assertFalse(self.item_for(uuid)["looks_resolved"])
        rec = self.ledger.load_state()["sessions"][uuid]
        self.assertEqual(rec["verdict"], "open_question")  # the old record: not committed
        detector.scan(self.cfg, self.ledger, at(timedelta(days=4)))
        self.assertTrue(self.item_for(uuid)["looks_resolved"])


class EnumerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Env(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_top_level_uuid_files(self) -> None:
        uuid, path = self.env.add_fixture("pos_say_the_word.jsonl")
        sub = path.parent / uuid / "subagents"
        sub.mkdir(parents=True)
        shutil.copy(str(path), str(sub / "agent-1.jsonl"))
        shutil.copy(str(path), str(sub / ("bbbbbbbb-0000-4000-8000-000000000001.jsonl")))
        shutil.copy(str(path), str(path.parent / "notes.jsonl"))
        shutil.copy(str(path), str(self.env.projects / "cccccccc-0000-4000-8000-000000000001.jsonl"))  # not in a folder
        (path.parent / "dddddddd-0000-4000-8000-000000000001.jsonl").mkdir()  # a directory with a session name
        found = detector.enumerate_sessions(self.env.cfg())
        self.assertEqual([u for u, _, _ in found], [uuid])

    def test_path_patterns_and_duplicates(self) -> None:
        a, pa = self.env.add_fixture("pos_say_the_word.jsonl")
        b, pb = self.env.add_fixture("pos_your_call.jsonl")
        dup = self.env.projects / "-other-folder"
        dup.mkdir()
        newer = dup / (a + ".jsonl")
        shutil.copy(str(pa), str(newer))
        os.utime(str(newer), (NOW.timestamp(), NOW.timestamp()))
        found = {u: p for u, p, _ in detector.enumerate_sessions(self.env.cfg())}
        self.assertEqual(found[a], newer)  # the most recently modified copy wins
        found = {u for u, _, _ in detector.enumerate_sessions(self.env.cfg(skip_path_patterns=[pb.parent.name, ""]))}
        self.assertEqual(found, {a})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_unreadable_project_folder(self) -> None:
        a, pa = self.env.add_fixture("pos_say_the_word.jsonl")
        b, pb = self.env.add_fixture("pos_your_call.jsonl")
        os.chmod(str(pb.parent), 0)
        try:
            errors: List[str] = []
            found = [u for u, _, _ in detector.enumerate_sessions(self.env.cfg(), errors)]
        finally:
            os.chmod(str(pb.parent), 0o700)
        self.assertEqual((found, errors), ([a], ["PermissionError"]))

    def test_missing_projects_dir(self) -> None:
        cfg = self.env.cfg(projects_dir=self.env.root / "nope")
        r = detector.scan(cfg, ledger.Ledger(cfg), NOW)
        self.assertEqual((r.sessions_seen, r.errors), (0, []))

    def test_old_files_wait_until_they_change(self) -> None:
        uuid, path = self.env.add_fixture("pos_say_the_word.jsonl", age=20 * 86400)
        cfg = self.env.cfg()
        led = ledger.Ledger(cfg)
        r = detector.scan(cfg, led, NOW)
        self.assertEqual((r.ignored_old, r.items_added), (1, 0))
        self.assertNotIn(uuid, led.load_state()["sessions"])
        # since is the first run's horizon and stays put
        detector.scan(self.env.cfg(scan_since_days=1), ledger.Ledger(self.env.cfg(scan_since_days=1)), at(timedelta(days=5)))
        self.assertEqual(led.load_state()["since"], timeutil.to_iso(NOW - timedelta(days=14)))
        self.env.append(uuid, question_ending("2026-10-07"), age=3600, base=at(timedelta(days=2)))
        r = detector.scan(cfg, led, at(timedelta(days=2)))
        self.assertEqual(r.items_added, 1)

    def test_refused_project_names_get_an_alias(self) -> None:
        cwd = str(self.env.workspace / "bob@example.com" / "src")

        def move(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            return [dict(entry, cwd=cwd) if "cwd" in entry else entry]
        uuid, _ = self.env.add_fixture("pos_say_the_word.jsonl", fn=move)
        cfg = self.env.cfg()
        led = ledger.Ledger(cfg)
        r = detector.scan(cfg, led, NOW)
        self.assertEqual(r.errors, [])
        item = led.find_by_session(uuid)
        self.assertRegex(item["project"], r"^project-[0-9a-f]{6}$")
        self.assertNotIn("@", item["title"])

    def test_performance(self) -> None:
        names = sorted(p.name for p in FIXTURES.glob("*.jsonl"))
        for i in range(150):
            self.env.add_fixture(names[i % len(names)], "eeeeeeee-0000-4000-8000-%012d" % i)
        cfg = self.env.cfg()
        led = ledger.Ledger(cfg)
        t0 = time.perf_counter()
        r = detector.scan(cfg, led, NOW)
        cold = time.perf_counter() - t0
        t0 = time.perf_counter()
        r2 = detector.scan(cfg, led, at(timedelta(minutes=1)))
        warm = time.perf_counter() - t0
        self.assertEqual((r.sessions_seen, r.errors, r2.sessions_scanned), (150, [], 0))
        self.assertLess(cold, 20.0)
        self.assertLess(warm, 1.0)


# ---- v2 (contract 16) --------------------------------------------------------------------------------

def stale_classifier(led: ledger.Ledger, value: Any = 1) -> None:
    """Make state.json look as if a v1 classifier wrote it (value None drops the key)."""
    state = led.load_state()
    if value is None:
        state.pop("classifier", None)
    else:
        state["classifier"] = value
    led.save_state(state)


def v1_ending(real: Callable[..., Any]) -> Callable[..., Any]:
    """classify() as v1 saw it: every question ending was a plain trailing question."""
    def fake(summary: Any, patterns: Any = (), handoff: Any = None) -> Any:
        ending = real(summary, patterns, ())  # v1/v2 had no hand-off rule (contract 16.10)
        if ending.verdict == classify.OPEN_QUESTION and ending.reason in (
                classify.OFFER_Q, classify.SAVE_OFFER, classify.TRAILING_Q):
            return classify.Ending(classify.OPEN_QUESTION, classify.TRAILING_Q)
        return ending
    return fake


class SinceFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Env(Path(self.tmp.name))
        self.cfg = self.env.cfg()
        self.led = ledger.Ledger(self.cfg)

    def test_floor_makes_old_unseen_files_eligible(self) -> None:
        old_uuid, _ = self.env.add_fixture("pos_say_the_word.jsonl", age=20 * 86400)
        r = detector.scan(self.cfg, self.led, NOW)
        self.assertEqual((r.ignored_old, r.items_added), (1, 0))
        floor = NOW - timedelta(days=30)
        r = detector.scan(self.cfg, self.led, at(timedelta(minutes=5)), since_floor=floor)
        self.assertEqual((r.ignored_old, r.items_added), (0, 1))
        self.assertIsNotNone(self.led.find_by_session(old_uuid))
        self.assertEqual(self.led.load_state()["since"], timeutil.to_iso(floor))

    def test_floor_on_the_first_run_and_as_iso(self) -> None:
        old_uuid, _ = self.env.add_fixture("pos_say_the_word.jsonl", age=20 * 86400)
        r = detector.scan(self.cfg, self.led, NOW, since_floor="2026-09-06T05:00:00Z")
        self.assertEqual(r.items_added, 1)
        self.assertEqual(self.led.load_state()["since"], "2026-09-06T05:00:00Z")

    def test_a_later_floor_changes_nothing(self) -> None:
        self.env.add_fixture("pos_say_the_word.jsonl", age=20 * 86400)
        detector.scan(self.cfg, self.led, NOW)
        since = self.led.load_state()["since"]
        r = detector.scan(self.cfg, self.led, at(timedelta(minutes=5)), since_floor=NOW - timedelta(days=7))
        self.assertEqual((r.ignored_old, r.items_added), (1, 0))
        self.assertEqual(self.led.load_state()["since"], since)
        # a floor older than a file that is still too old for it leaves that file out
        r = detector.scan(self.cfg, self.led, at(timedelta(minutes=6)), since_floor=NOW - timedelta(days=19))
        self.assertEqual((r.ignored_old, r.items_added), (1, 0))
        self.assertEqual(self.led.load_state()["since"], timeutil.to_iso(NOW - timedelta(days=19)))

    def test_bad_floor_raises_before_touching_state(self) -> None:
        with self.assertRaises(ValueError):
            detector.scan(self.cfg, self.led, NOW, since_floor="last tuesday")
        self.assertFalse(self.led.state_path.exists())


class ReclassifyTests(FixtureSetMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.build()
        with mock.patch.object(classify, "classify", side_effect=v1_ending(classify.classify)):
            detector.scan(self.cfg, self.ledger, NOW)
            detector.scan(self.cfg, self.ledger, at(timedelta(minutes=40)))  # FRESH has gone quiet
        self.assertEqual(self.ledger.load_state()["deferred"], [])
        self.q = self.uuids["pos_trailing_question.jsonl"]
        self.assertEqual(self.item_for(self.q)["reason"], "trailing_q")

    def open_question_uuids(self) -> List[str]:
        """The sessions a stale classifier re-reads: open questions, and (v3, contract 16.10)
        plain clean endings that may now be hand-offs."""
        return sorted(u for u, rec in self.ledger.load_state()["sessions"].items()
                      if rec.get("verdict") == "open_question"
                      or (rec.get("verdict") == "clean" and rec.get("reason") == "no_question"))

    def test_state_records_the_classifier_version(self) -> None:
        self.assertEqual(self.ledger.load_state()["classifier"], classify.CLASSIFIER_VERSION)

    def test_stale_classifier_reprocesses_open_questions_once(self) -> None:
        for stale in (1, None):
            with self.subTest(stale=stale):
                stale_classifier(self.ledger, stale)
                inbox_before = len(self.inbox_lines())
                expected = self.open_question_uuids()
                with mock.patch.object(transcript, "parse_transcript", wraps=transcript.parse_transcript) as pt:
                    r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=1 if stale else 2)))
                parsed = sorted(Path(c.args[0]).name[:-len(".jsonl")] for c in pt.call_args_list)
                self.assertEqual(parsed, expected)  # unchanged files, but only open questions
                self.assertEqual(r.reclassified, len(expected))
                self.assertEqual(r.errors, [])
                state = self.ledger.load_state()
                self.assertEqual(state["classifier"], classify.CLASSIFIER_VERSION)
                # the reason change reaches items.json and state.json, with no status change
                item = self.item_for(self.q)
                self.assertEqual(item["reason"], "save_offer")
                self.assertEqual((item["status"], item["rev"]), ("open", 1))
                self.assertEqual(state["sessions"][self.q]["reason"], "save_offer")
                self.assertEqual(self.item_for(GITREPO)["reason"], "save_offer")
                self.assertEqual(self.item_for(SDK2)["reason"], "offer_q")
                on_disk = json.loads(self.ledger.items_path.read_text(encoding="utf-8"))
                stored = [it for it in on_disk["items"].values() if it.get("session_uuid") == self.q]
                self.assertEqual(stored[0]["reason"], "save_offer")
                # a reason is Mac-only: nothing new goes to the inbox
                self.assertEqual(len(self.inbox_lines()), inbox_before)
                with mock.patch.object(transcript, "parse_transcript", wraps=transcript.parse_transcript) as pt:
                    r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=1 if stale else 2, minutes=5)))
                self.assertEqual((pt.call_count, r.reclassified), (0, 0))  # once only

    def test_closed_items_get_the_reason_without_reopening(self) -> None:
        item, _ = self.ledger.set_status(self.item_for(self.q)["id"], "done", now=NOW)
        stale_classifier(self.ledger)
        detector.scan(self.cfg, self.ledger, at(timedelta(hours=1)))
        item = self.item_for(self.q)
        self.assertEqual((item["status"], item["rev"], item["reason"]), ("done", 2, "save_offer"))

    def test_maybe_active_still_defers_and_keeps_the_debt(self) -> None:
        stale_classifier(self.ledger)
        later = at(timedelta(hours=1))
        self.env.touch(self.env.paths[self.q], 60, base=later)  # same content, touched a minute ago
        r = detector.scan(self.cfg, self.ledger, later)
        state = self.ledger.load_state()
        self.assertIn(self.q, state["deferred"])
        self.assertTrue(state["deferred_info"][self.q]["reclassify"])
        self.assertEqual(self.item_for(self.q)["reason"], "trailing_q")
        self.assertEqual(state["classifier"], classify.CLASSIFIER_VERSION)
        self.assertEqual(r.reclassified, len(self.open_question_uuids()) - 1)
        # quiet again: processed under the new classifier even though nothing changed
        r = detector.scan(self.cfg, self.ledger, later + timedelta(hours=1))
        self.assertEqual(r.reclassified, 1)
        self.assertEqual(self.item_for(self.q)["reason"], "save_offer")
        self.assertNotIn(self.q, self.ledger.load_state()["deferred"])

    def test_a_failed_reprocess_keeps_the_old_version(self) -> None:
        stale_classifier(self.ledger)
        real = transcript.parse_transcript
        bad = self.env.paths[self.q]

        def flaky(path: Any, *a: Any, **kw: Any) -> Any:
            if Path(path) == bad:
                raise OSError("boom")
            return real(path, *a, **kw)
        with mock.patch.object(transcript, "parse_transcript", side_effect=flaky):
            r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=1)))
        self.assertEqual(r.errors, ["OSError"])
        self.assertEqual(self.ledger.load_state()["classifier"], 1)
        r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=2)))
        self.assertEqual(r.errors, [])
        self.assertEqual(self.ledger.load_state()["classifier"], classify.CLASSIFIER_VERSION)
        self.assertEqual(self.item_for(self.q)["reason"], "save_offer")

    def test_an_unlisted_folder_keeps_the_old_version(self) -> None:
        stale_classifier(self.ledger)
        folder = os.path.dirname(str(self.env.paths[self.q]))
        real = os.scandir

        def flaky(path: Any = ".") -> Any:
            if os.path.normpath(str(path)) == os.path.normpath(folder):
                raise PermissionError("denied")
            return real(path)
        with mock.patch.object(os, "scandir", side_effect=flaky):
            r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=1)))
        self.assertIn("PermissionError", r.errors)
        self.assertEqual(self.ledger.load_state()["classifier"], 1)
        self.assertEqual(self.item_for(self.q)["reason"], "trailing_q")
        r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=2)))
        self.assertEqual(r.errors, [])
        self.assertEqual(self.ledger.load_state()["classifier"], classify.CLASSIFIER_VERSION)
        self.assertEqual(self.item_for(self.q)["reason"], "save_offer")

    def test_a_vanished_file_does_not_hold_the_version(self) -> None:
        stale_classifier(self.ledger)
        os.unlink(str(self.env.paths[self.q]))
        r = detector.scan(self.cfg, self.ledger, at(timedelta(hours=1)))
        self.assertEqual(r.errors, [])
        self.assertEqual(self.ledger.load_state()["classifier"], classify.CLASSIFIER_VERSION)

    def test_clean_and_limit_sessions_are_not_reprocessed(self) -> None:
        stale_classifier(self.ledger)
        with mock.patch.object(transcript, "parse_transcript", wraps=transcript.parse_transcript) as pt:
            detector.scan(self.cfg, self.ledger, at(timedelta(hours=1)))
        parsed = {Path(c.args[0]).name[:-len(".jsonl")] for c in pt.call_args_list}
        sessions = self.ledger.load_state()["sessions"]
        for name in ("pos_limit_meta.jsonl", "auto_sdk_cli_question.jsonl"):
            self.assertNotIn(self.uuids[name], parsed)
        for uuid, rec in sessions.items():  # clean for another reason (answered, continued): left alone
            if rec.get("verdict") == "clean" and rec.get("reason") != "no_question":
                self.assertNotIn(uuid, parsed)
        # v3 (contract 16.10): a plain clean ending is re-read once, it may be a hand-off now
        self.assertIn(self.uuids["neg_qa_session.jsonl"], parsed)


class TempFolderTests(unittest.TestCase):
    """A session launched from a temp folder is a test or a throwaway, never a loose end
    (contract 16.13)."""

    UUID = "aaaaaaaa-0000-4000-8000-000000000021"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Env(Path(self.tmp.name))
        self.scratch = Path(self.tmp.name) / "scratch"
        self.scratch.mkdir()
        self.inside = str(self.scratch / "sandbox" / "alpha")
        self.outside = str(self.env.workspace / "alpha")
        patcher = mock.patch.object(detector, "TEMP_ROOTS", (str(self.scratch),))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = self.env.cfg(skip_temp_folders=True)
        self.led = ledger.Ledger(self.cfg)

    def session(self, cwd: str, day: str = "2026-10-02", age: float = OLD) -> None:
        entries = clean_ending(day, cwd) + question_ending(day, cwd)
        data = b"".join(json.dumps(e, separators=(",", ":")).encode("utf-8") + b"\n" for e in entries)
        self.env.write(self.UUID, data, age)

    def test_in_temp_folder(self) -> None:
        self.assertTrue(detector.in_temp_folder(self.inside))
        self.assertTrue(detector.in_temp_folder(str(self.scratch)))
        self.assertFalse(detector.in_temp_folder(self.outside))
        self.assertFalse(detector.in_temp_folder(str(self.scratch) + "-kept/alpha"))  # a prefix, not a parent
        for bad in (None, "", "   ", 5, "a\x00b"):
            self.assertFalse(detector.in_temp_folder(bad))  # type: ignore[arg-type]

    def test_a_link_into_a_temp_folder_counts(self) -> None:
        link = self.env.workspace / "link"
        try:
            os.symlink(str(self.scratch), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.assertTrue(detector.in_temp_folder(str(link / "alpha")))

    def test_the_default_roots_cover_the_system_temp_folder(self) -> None:
        mock.patch.stopall()
        self.assertTrue(detector.in_temp_folder(os.path.join(tempfile.gettempdir(), "x", "alpha")))
        self.assertFalse(detector.in_temp_folder("/Users/sam/workspace/widgets"))

    def test_a_temp_folder_session_is_skipped(self) -> None:
        self.session(self.inside)
        r = detector.scan(self.cfg, self.led, NOW)
        self.assertEqual((r.items_added, r.skipped_automated, r.flagged, r.errors), (0, 1, [], []))
        self.assertIsNone(self.led.find_by_session(self.UUID))
        rec = self.led.load_state()["sessions"][self.UUID]
        self.assertEqual((rec["verdict"], rec["reason"]), ("automated", "temp_folder"))

    def test_the_same_session_elsewhere_is_flagged(self) -> None:
        self.session(self.outside)
        r = detector.scan(self.cfg, self.led, NOW)
        self.assertEqual((r.items_added, r.skipped_automated), (1, 0))

    def test_the_switch_turns_it_off(self) -> None:
        self.session(self.inside)
        cfg = self.env.cfg(skip_temp_folders=False)
        r = detector.scan(cfg, ledger.Ledger(cfg), NOW)
        self.assertEqual((r.items_added, r.skipped_automated), (1, 0))

    def test_it_is_on_by_default(self) -> None:
        self.assertTrue(Config(home=self.env.home, projects_dir=self.env.projects).skip_temp_folders)

    def test_an_item_already_open_stays_open(self) -> None:
        # The detector never closes anything (contract 6): an item flagged before the rule stays
        # until the user dismisses it; the session is just not flagged again.
        self.session(self.inside)
        off = self.env.cfg(skip_temp_folders=False)
        detector.scan(off, ledger.Ledger(off), NOW)
        before = self.led.find_by_session(self.UUID)
        assert before is not None
        self.env.append(self.UUID, question_ending("2026-10-03", self.inside), age=3600, base=at(timedelta(days=1)))
        r = detector.scan(self.cfg, self.led, at(timedelta(days=1)))
        after = self.led.find_by_session(self.UUID)
        assert after is not None
        self.assertEqual((r.items_added, r.items_updated, r.skipped_automated), (0, 0, 1))
        self.assertEqual((after["status"], after["rev"]), (before["status"], before["rev"]))


class DriftTests(unittest.TestCase):
    def d(self, **kw: Any) -> Dict[str, Any]:
        base: Dict[str, Any] = {"at": "2026-10-06T15:00:00Z", "files": 10, "lines": 1000, "zero_conversation": 0,
                                "empty_final": 0, "unknown_lines": 0, "unknown_types": {}}
        base.update(kw)
        return base

    def test_quiet_drift_is_no_problem(self) -> None:
        self.assertIsNone(detector.drift_problem(self.d()))
        for junk in (None, {}, [], "drift", {"files": "10"}, {"files": True, "zero_conversation": 9}):
            self.assertIsNone(detector.drift_problem(junk))

    def test_needs_three_files(self) -> None:
        self.assertIsNone(detector.drift_problem(self.d(files=2, zero_conversation=2, empty_final=2,
                                                        lines=10, unknown_lines=10, unknown_types={"x": 10})))
        self.assertIsNotNone(detector.drift_problem(self.d(files=3, zero_conversation=2)))

    def test_zero_conversation_threshold(self) -> None:
        self.assertEqual(detector.drift_problem(self.d(files=3, zero_conversation=2)),
                         "2 of 3 session files had no messages Ghosted could read")
        self.assertIsNone(detector.drift_problem(self.d(files=3, zero_conversation=1)))
        self.assertIsNone(detector.drift_problem(self.d(files=10, zero_conversation=4)))
        self.assertEqual(detector.drift_problem(self.d(files=10, zero_conversation=5)),
                         "5 of 10 session files had no messages Ghosted could read")
        self.assertIsNone(detector.drift_problem(self.d(files=11, zero_conversation=5)))  # 5 < 5.5

    def test_unknown_lines_threshold(self) -> None:
        types = {"frame-v2": 20, "blob": 6}
        self.assertIsNone(detector.drift_problem(self.d(lines=100, unknown_lines=25, unknown_types=types)))
        self.assertEqual(detector.drift_problem(self.d(lines=100, unknown_lines=26, unknown_types={"blob": 6, "frame-v2": 20})),
                         "unknown entry types are most of the transcript: frame-v2, blob")
        self.assertIsNone(detector.drift_problem(self.d(lines=0, unknown_lines=5)))

    def test_empty_final_threshold(self) -> None:
        self.assertEqual(detector.drift_problem(self.d(files=4, empty_final=2)),
                         "most sessions had no final reply Ghosted could read")
        self.assertIsNone(detector.drift_problem(self.d(files=4, empty_final=1)))
        self.assertIsNone(detector.drift_problem(self.d(files=5, empty_final=2)))
        self.assertIsNotNone(detector.drift_problem(self.d(files=5, empty_final=3)))

    def test_order(self) -> None:
        both = self.d(files=4, zero_conversation=4, empty_final=4, lines=10, unknown_lines=10, unknown_types={"x": 10})
        self.assertIn("had no messages", detector.drift_problem(both))
        both.update(zero_conversation=0)
        self.assertIn("unknown entry types", detector.drift_problem(both))

    def test_counter_top_five(self) -> None:
        c = detector.DriftCounter()
        self.assertIsNone(c.value("t"))
        for i in range(7):
            c.add(transcript.SessionSummary(session_uuid="u%d" % i, lines_read=10, conversation_entries=i,
                                            final_text="done" if i % 2 else None,
                                            unknown_types={"t%d" % i: i + 1, "shared": 1}))
        v = c.value("t")
        self.assertEqual((v["files"], v["lines"], v["zero_conversation"], v["empty_final"]), (7, 70, 1, 4))
        self.assertEqual(v["unknown_lines"], sum(range(1, 8)) + 7)
        self.assertEqual(v["unknown_types"], {"shared": 7, "t6": 7, "t5": 6, "t4": 5, "t3": 4})
        self.assertEqual(list(v["unknown_types"]), ["shared", "t6", "t5", "t4", "t3"])
        self.assertEqual(v["at"], "t")


class DriftScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Env(Path(self.tmp.name))
        self.cfg = self.env.cfg()
        self.led = ledger.Ledger(self.cfg)

    def test_scan_aggregates_parsed_files(self) -> None:
        names = ["pos_say_the_word.jsonl", "neg_qa_session.jsonl", "neg_metadata_only.jsonl"]
        summaries = []
        for name in names:
            self.env.add_fixture(name)
            summaries.append(transcript.parse_transcript(FIXTURES / name))
        detector.scan(self.cfg, self.led, NOW)
        drift = self.led.load_state()["drift"]
        self.assertEqual(drift["at"], timeutil.to_iso(NOW))
        self.assertEqual(drift["files"], 3)
        self.assertEqual(drift["lines"], sum(s.lines_read for s in summaries))
        self.assertEqual(drift["zero_conversation"], sum(1 for s in summaries if s.conversation_entries == 0))
        self.assertEqual(drift["empty_final"], sum(1 for s in summaries if not (s.final_text or "").strip()))
        self.assertEqual(drift["unknown_lines"], sum(sum(s.unknown_types.values()) for s in summaries))
        # a scan that parses nothing keeps the previous value
        detector.scan(self.cfg, self.led, at(timedelta(minutes=5)))
        self.assertEqual(self.led.load_state()["drift"], drift)

    def test_unknown_types_reach_the_state(self) -> None:
        def odd(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            if entry.get("type") in ("user", "assistant"):
                return [entry] + [{"type": "frame-v9", "n": i} for i in range(3)]
            return [entry]
        for i in range(3):
            self.env.add_fixture("pos_say_the_word.jsonl", "ffffffff-0000-4000-8000-%012d" % i, fn=odd)
        detector.scan(self.cfg, self.led, NOW)
        drift = self.led.load_state()["drift"]
        self.assertEqual(list(drift["unknown_types"]), ["frame-v9"])
        self.assertGreater(drift["unknown_lines"], 0.25 * drift["lines"])
        self.assertEqual(detector.drift_problem(drift), "unknown entry types are most of the transcript: frame-v9")


if __name__ == "__main__":
    unittest.main()
