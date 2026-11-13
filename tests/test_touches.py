"""The touch index and attribution (core.touches, contract 16.11).

Synthetic transcripts and temp repos only. Each test session writes entries with explicit timestamps
and working folders, the way Claude Code does: tool_use blocks on assistant entries, tool_result
blocks on user entries that carry the session's cwd.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from core import detector, touches, timeutil
from core.config import Config

NOW = timeutil.parse_iso("2026-11-08T18:00:00Z")
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
PLANTED_FILE = "client-acme-contract.md"
PLANTED_COMMAND = "rm -rf build && echo acme-secret-token"


def iso(when: datetime) -> str:
    return timeutil.to_iso(when)


def git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=dict(os.environ, **HERMETIC),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


class Session:
    """One invented session transcript in a projects folder."""

    def __init__(self, projects: Path, launch: Path, sid: Optional[str] = None, entrypoint: str = "cli") -> None:
        self.sid = sid or str(uuid_mod.uuid4())
        self.launch = str(launch)
        self.entrypoint = entrypoint
        folder = projects / ("-" + self.launch.strip("/").replace("/", "-").replace(".", "-"))
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (self.sid + ".jsonl")
        self.lines: List[Dict[str, Any]] = []
        self.n = 0
        self.prompt("Please work on it.", NOW - timedelta(hours=2))

    def _env(self, etype: str, when: datetime, cwd: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        self.n += 1
        e: Dict[str, Any] = {"parentUuid": None, "isSidechain": False, "userType": "external",
                             "cwd": cwd or self.launch, "sessionId": self.sid, "version": "2.1.319",
                             "entrypoint": self.entrypoint, "type": etype, "uuid": "u-%d" % self.n,
                             "timestamp": iso(when)}
        e.update(extra)
        return e

    def prompt(self, text: str, when: datetime) -> "Session":
        self.lines.append(self._env("user", when, message={"role": "user", "content": text}))
        return self

    def edit(self, file_path: str, when: datetime, tool: str = "Edit", cwd: Optional[str] = None,
             sidechain: bool = False) -> "Session":
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        self.lines.append(self._env("assistant", when, cwd=cwd, isSidechain=sidechain, message={
            "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_%d" % self.n, "name": tool,
                                              "input": {key: file_path, "old_string": "a", "new_string": "b"}}]}))
        return self

    def bash(self, when: datetime, cwd: Optional[str] = None, command: str = "ls",
             sidechain: bool = False) -> "Session":
        """A Bash call and its result; the result entry carries the cwd."""
        self.lines.append(self._env("assistant", when, cwd=cwd, isSidechain=sidechain, message={
            "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_%d" % self.n, "name": "Bash",
                                              "input": {"command": command}}]}))
        self.lines.append(self._env("user", when, cwd=cwd, isSidechain=sidechain, toolUseResult={"stdout": "ok"},
                                    message={"role": "user", "content": [
                                        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}]}))
        return self

    def text(self, text: str, when: datetime) -> "Session":
        self.lines.append(self._env("assistant", when, message={
            "role": "assistant", "content": [{"type": "text", "text": text}]}))
        return self

    def write(self, mtime: Optional[datetime] = None, partial: str = "") -> "Session":
        self.path.write_text("".join(json.dumps(e) + "\n" for e in self.lines) + partial)
        when = (mtime or NOW - timedelta(minutes=45)).timestamp()
        os.utime(str(self.path), (when, when))
        return self


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.home = self.root / "ghosted-home"
        # Tests live in a temp dir, so the temp-root filter would drop every repo.
        self.eph = mock.patch.object(touches, "EPHEMERAL_ROOTS", ())
        self.eph.start()

    def tearDown(self) -> None:
        self.eph.stop()
        self.tmp.cleanup()

    def cfg(self, **kw: Any) -> Config:
        base: Dict[str, Any] = dict(home=self.home, projects_dir=self.projects, timezone="UTC", workspace_roots=[])
        base.update(kw)
        return Config(**base)

    def session(self, launch: Path, **kw: Any) -> Session:
        return Session(self.projects, launch, **kw)

    def update(self, cfg: Optional[Config] = None, now: datetime = NOW) -> touches.Index:
        return touches.update(cfg or self.cfg(), now)


T0 = NOW - timedelta(hours=1)


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class IndexTests(Base):
    def test_edit_path_and_result_cwd_map_to_the_toplevel(self) -> None:
        shop = git_init(self.root / "shop")
        (shop / "src" / "deep").mkdir(parents=True)
        api = git_init(self.root / "api")
        (api / "sub").mkdir()
        s = self.session(self.root)
        s.edit(str(shop / "src" / "deep" / "new-file.py"), T0)               # file does not exist yet
        s.bash(T0 + timedelta(seconds=5), cwd=str(api / "sub"))
        s.edit(str(shop / "nb.ipynb"), T0 + timedelta(seconds=9), tool="NotebookEdit")
        s.write()
        idx = self.update()
        self.assertEqual(idx.touches(str(shop)), {s.sid: [int(T0.timestamp()), int(T0.timestamp()) + 9]})
        self.assertEqual(idx.edits(str(shop)), {s.sid: [int(T0.timestamp()), int(T0.timestamp()) + 9]})
        self.assertEqual(idx.touches(str(api)), {s.sid: [int(T0.timestamp()) + 5]})
        self.assertEqual(idx.edits(str(api)), {})

    def test_relative_edit_path_joins_the_cwd(self) -> None:
        shop = git_init(self.root / "shop")
        s = self.session(shop).edit("lib/x.py", T0).write()
        self.assertEqual(self.update().edits(str(shop)), {s.sid: [int(T0.timestamp())]})

    def test_outside_any_repo_and_temp_roots_dropped(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        scratch = git_init(self.root / "scratch" / "r")
        s = self.session(plain).bash(T0).edit(str(scratch / "a.txt"), T0).write()
        with mock.patch.object(touches, "EPHEMERAL_ROOTS", (str(self.root / "scratch"),)):
            idx = self.update()
        self.assertEqual(idx.repos_since(0), set())
        self.assertTrue(s.sid)

    def test_worktree_is_its_own_repo(self) -> None:
        main = git_init(self.root / "app")
        (main / "f").write_text("x")
        env = dict(os.environ, **HERMETIC, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x.invalid")
        subprocess.run(["git", "-C", str(main), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(main), "commit", "-qm", "i"], check=True, env=env)
        wt = self.root / "app-2"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        a = self.session(main).edit(str(main / "f"), T0).write()
        b = self.session(wt).edit(str(wt / "f"), T0).write()
        idx = self.update()
        self.assertEqual(set(idx.edits(str(main))), {a.sid})
        self.assertEqual(set(idx.edits(str(wt))), {b.sid})

    def test_symlinked_repo_is_one_key(self) -> None:
        shop = git_init(self.root / "shop")
        link = self.root / "shop-link"
        link.symlink_to(shop)
        s = self.session(link).bash(T0).edit(str(link / "a.py"), T0 + timedelta(seconds=1)).write()
        self.assertEqual(self.update().repos_since(0), {str(shop)})
        self.assertTrue(s.sid)

    def test_incremental_read_from_offset(self) -> None:
        shop = git_init(self.root / "shop")
        s = self.session(shop).edit(str(shop / "a"), T0).write()
        first = self.update()
        self.assertGreater(first.stats["bytes_read"], 0)
        size = s.path.stat().st_size
        s.edit(str(shop / "b"), T0 + timedelta(seconds=30)).write(mtime=NOW - timedelta(minutes=40))
        grown = s.path.stat().st_size - size
        second = self.update()
        self.assertEqual(second.stats["bytes_read"], grown)
        self.assertEqual(second.stats["rebuilt"], 0)
        self.assertEqual(second.touches(str(shop))[s.sid], [int(T0.timestamp()), int(T0.timestamp()) + 30])
        third = self.update()
        self.assertEqual(third.stats["bytes_read"], 0)

    def test_partial_last_line_waits(self) -> None:
        shop = git_init(self.root / "shop")
        s = self.session(shop).edit(str(shop / "a"), T0)
        s.write(partial='{"type": "assistant", "timestamp": "2026-11-08T17:1')
        idx = self.update()
        self.assertEqual(idx.touches(str(shop)), {s.sid: [int(T0.timestamp())]})
        rec = json.loads((self.home / "touches.json").read_text())["sessions"][s.sid]
        self.assertEqual(rec["offset"], s.path.stat().st_size - len('{"type": "assistant", "timestamp": "2026-11-08T17:1'))

    def test_rewritten_or_truncated_file_rebuilds(self) -> None:
        shop = git_init(self.root / "shop")
        api = git_init(self.root / "api")
        s = self.session(shop).edit(str(shop / "a"), T0).edit(str(shop / "b"), T0 + timedelta(seconds=3)).write()
        self.update()
        # same session id, different content before the old offset (a rewrite), then shorter (truncation)
        s.lines = s.lines[:1]
        s.edit(str(api / "a"), T0 + timedelta(seconds=7))
        s.edit(str(api / "b"), T0 + timedelta(seconds=8))
        s.edit(str(api / "c"), T0 + timedelta(seconds=9))
        s.write(mtime=NOW - timedelta(minutes=41))
        idx = self.update()
        self.assertEqual(idx.stats["rebuilt"], 1)
        self.assertEqual(idx.touches(str(shop)), {})
        self.assertEqual(len(idx.touches(str(api))[s.sid]), 3)
        s.lines = s.lines[:2]
        s.write(mtime=NOW - timedelta(minutes=39))
        idx = self.update()
        self.assertEqual(idx.stats["rebuilt"], 1)
        self.assertEqual(idx.touches(str(api))[s.sid], [int(T0.timestamp()) + 7])

    def test_same_size_rewrite_within_one_second_is_seen(self) -> None:
        # Mtime is compared in nanoseconds, not whole seconds.
        shop = git_init(self.root / "shop")
        shoq = git_init(self.root / "shoq")                 # same name length: the rewrite keeps the size
        s = self.session(shop).edit(str(shop / "a"), T0).write()
        ns = int((NOW - timedelta(minutes=40)).timestamp()) * 10 ** 9
        os.utime(str(s.path), ns=(ns + 100_000_000, ns + 100_000_000))
        self.assertEqual(set(self.update().edits(str(shop))), {s.sid})
        size = s.path.stat().st_size
        s.path.write_text(s.path.read_text().replace(str(shop), str(shoq)))
        os.utime(str(s.path), ns=(ns + 900_000_000, ns + 900_000_000))   # same second, same size
        self.assertEqual(s.path.stat().st_size, size)
        idx = self.update()
        self.assertEqual(idx.edits(str(shop)), {})
        self.assertEqual(set(idx.edits(str(shoq))), {s.sid})

    def test_one_malformed_record_does_not_lose_the_rest(self) -> None:
        # A bad entry is skipped on its own.
        shop = git_init(self.root / "shop")
        s = self.session(shop).edit(str(shop / "a"), T0)
        s.lines.append(s._env("assistant", T0 + timedelta(seconds=5), message={
            "role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": ["Edit"], "input": []}]}))
        s.lines.append(s._env("assistant", T0 + timedelta(seconds=6), message={"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": 7}}, "not a block"]}))
        s.lines.append(s._env("user", T0 + timedelta(seconds=7), cwd=["x"], message={"role": "user", "content": [
            {"type": "tool_result"}]}))
        s.edit(str(shop / "b"), T0 + timedelta(seconds=9)).write()
        idx = self.update()
        self.assertEqual(idx.edits(str(shop)), {s.sid: [int(T0.timestamp()), int(T0.timestamp()) + 9]})

    def test_sidechain_and_automated_skipped(self) -> None:
        shop = git_init(self.root / "shop")
        side = self.session(shop).edit(str(shop / "a"), T0, sidechain=True).bash(T0, sidechain=True).write()
        auto = self.session(shop, entrypoint="sdk-cli").edit(str(shop / "b"), T0).write()
        cfg = self.cfg(skip_entrypoints=["sdk-cli"])
        idx = touches.update(cfg, NOW)
        self.assertEqual(idx.touches(str(shop)), {})
        self.assertEqual(idx.repos_since(0), set())
        # a second human turn: an sdk-cli session with 2 turns is a Remote Control conversation, not automated
        auto.prompt("and another thing", T0 + timedelta(minutes=1)).write(mtime=NOW - timedelta(minutes=35))
        idx = touches.update(cfg, NOW)
        self.assertEqual(set(idx.touches(str(shop))), {auto.sid})
        self.assertTrue(side.sid)

    def test_first_prompt_pattern_marks_automated(self) -> None:
        shop = git_init(self.root / "shop")
        s = self.session(shop)
        s.lines[0]["message"]["content"] = "NIGHTLY JOB: tidy"
        s.edit(str(shop / "a"), T0).write()
        idx = touches.update(self.cfg(skip_first_prompt_patterns=["^NIGHTLY JOB"]), NOW)
        self.assertEqual(idx.touches(str(shop)), {})

    def test_only_repo_paths_ints_and_bools_are_stored(self) -> None:
        shop = git_init(self.root / "shop")
        self.session(shop).edit(str(shop / PLANTED_FILE), T0).bash(T0, command=PLANTED_COMMAND) \
            .text("Here is %s" % PLANTED_FILE, T0).write()
        self.update()
        raw = (self.home / "touches.json").read_text()
        for planted in (PLANTED_FILE, "acme-secret-token", "rm -rf", "Please work"):
            self.assertNotIn(planted, raw)
        self.assertEqual(os.stat(str(self.home / "touches.json")).st_mode & 0o777, 0o600)

        def walk(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, k)
                    if key in ("repos", "edits"):
                        self.assertTrue(os.path.isdir(k), k)
            elif isinstance(value, list):
                for v in value:
                    walk(v, key)
            elif isinstance(value, str):
                self.assertTrue(key in ("version",) or os.path.isdir(value), (key, value))
            else:
                self.assertIsInstance(value, (int, bool, type(None)))
        walk(json.loads(raw))

    def test_retention_and_read_window(self) -> None:
        shop = git_init(self.root / "shop")
        old = self.session(shop).edit(str(shop / "a"), NOW - timedelta(days=95)) \
            .edit(str(shop / "b"), NOW - timedelta(days=10)).write(mtime=NOW - timedelta(days=10))
        never = self.session(shop).edit(str(shop / "c"), NOW - timedelta(days=40)).write(mtime=NOW - timedelta(days=40))
        idx = self.update()
        self.assertEqual(idx.touches(str(shop)), {old.sid: [int((NOW - timedelta(days=10)).timestamp())]})
        self.assertNotIn(never.sid, idx.sessions)
        later = NOW + timedelta(days=85)
        idx = self.update(now=later)
        self.assertEqual(idx.touches(str(shop)), {})
        # the file is still there, so its record (the offset) stays; a deleted file's record goes
        self.assertIn(old.sid, idx.sessions)
        old.path.unlink()
        idx = self.update(now=later)
        self.assertNotIn(old.sid, idx.sessions)

    def test_broken_index_file_rebuilds(self) -> None:
        shop = git_init(self.root / "shop")
        s = self.session(shop).edit(str(shop / "a"), T0).write()
        self.home.mkdir(mode=0o700)
        (self.home / "touches.json").write_text("{not json")
        self.assertEqual(set(self.update().touches(str(shop))), {s.sid})

    def test_repos_since(self) -> None:
        a = git_init(self.root / "a")
        b = git_init(self.root / "b")
        self.session(a).bash(NOW - timedelta(days=3)).bash(NOW - timedelta(days=20), cwd=str(b)).write()
        idx = self.update()
        self.assertEqual(idx.repos_since((NOW - timedelta(days=30)).timestamp()), {str(a), str(b)})
        self.assertEqual(idx.repos_since((NOW - timedelta(days=7)).timestamp()), {str(a)})


class OwnerTests(unittest.TestCase):
    BY = {"s1": [1000, 2000], "s2": [2010, 5000]}

    def test_owner_of(self) -> None:
        self.assertEqual(touches.owner_of(1005.0, self.BY, 15), "s1")
        self.assertEqual(touches.owner_of(2005.0, self.BY, 15), touches.AMBIGUOUS)
        self.assertEqual(touches.owner_of(4990.0, self.BY, 15), "s2")
        self.assertEqual(touches.owner_of(3000.0, self.BY, 15), touches.NONE)
        self.assertEqual(touches.owner_of(1015.0, self.BY, 15), "s1")   # the edge is inside
        self.assertEqual(touches.owner_of(1016.0, self.BY, 15), touches.NONE)

    def test_owners_counts_and_deleted_ignored(self) -> None:
        idx = touches.Index({"s1": {"repos": {"/r": [1000, 2000]}, "edits": {}},
                             "s2": {"repos": {"/r": [2010, 5000]}, "edits": {}},
                             "s3": {"repos": {"/other": [1000]}, "edits": {}}})
        facts = {"path": "/r", "mtimes": [1001.0, 1002.0, 2005.0, 4999.0, 9000.0, None]}
        self.assertEqual(touches.owners("/r", facts, idx, 15),
                         {"sessions": {"s1": 2, "s2": 1}, "ambiguous": 1, "none": 1})

    def test_automated_sessions_never_own(self) -> None:
        idx = touches.Index({"s1": {"repos": {"/r": [1000]}, "edits": {}, "auto": True}})
        self.assertEqual(touches.owners("/r", {"mtimes": [1000.0]}, idx, 15),
                         {"sessions": {}, "ambiguous": 0, "none": 1})


class DriftTests(unittest.TestCase):
    def test_no_cwd_is_drift(self) -> None:
        counter = detector.DriftCounter()
        for _ in range(3):
            counter.add(mock.Mock(lines_read=10, conversation_entries=4, final_text="done", unknown_types={},
                                  launch_cwd=None))
        problem = detector.drift_problem(counter.value("2026-11-08T18:00:00Z"))
        self.assertEqual(problem, "3 of 3 session files had no working folder Ghosted could read")

    def test_cwd_present_is_fine(self) -> None:
        counter = detector.DriftCounter()
        for _ in range(3):
            counter.add(mock.Mock(lines_read=10, conversation_entries=4, final_text="done", unknown_types={},
                                  launch_cwd="/x"))
        self.assertIsNone(detector.drift_problem(counter.value("2026-11-08T18:00:00Z")))


if __name__ == "__main__":
    unittest.main()
