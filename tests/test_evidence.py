"""Tests for core.evidence: git facts from real temporary repos, the evidence phrases, sanitizing."""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from core import evidence, sanitize
from core.evidence import counts_text, evidence_text, git_facts, git_text

GIT = shutil.which("git")
# Hermetic git: no global or system config (so status.showUntrackedFiles etc. can't change counts).
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd: Path, *args: str) -> None:
    env = dict(os.environ, **HERMETIC, **IDENTITY)
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(path: Path, branch: str = "main", commits: int = 1) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    for i in range(commits):
        (path / ("f%d.txt" % i)).write_text("v%d\n" % i, encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "c%d" % i)
    return path


def facts(**kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"dirty": 0, "branch": "main", "upstream": None, "ahead": None, "timed_out": False}
    base.update(kw)
    return base


class GitTextTests(unittest.TestCase):
    def test_spec_examples(self) -> None:
        self.assertEqual(git_text(facts(dirty=91, upstream="origin/main", ahead=2)), "91 uncommitted, 2 unpushed")
        self.assertEqual(git_text(facts(dirty=18, branch="round-2")), "18 uncommitted on round-2, no upstream")
        self.assertEqual(git_text(facts(dirty=0, upstream="origin/main", ahead=0)), "clean tree")
        self.assertEqual(git_text(None), "not a git repo")

    def test_other_combinations(self) -> None:
        self.assertEqual(git_text(facts(dirty=3, upstream="origin/main", ahead=0)), "3 uncommitted")
        self.assertEqual(git_text(facts(dirty=0, upstream="origin/main", ahead=4)), "4 unpushed")
        self.assertEqual(git_text(facts(dirty=3, upstream="origin/main", ahead=None)), "3 uncommitted")
        self.assertEqual(git_text(facts(dirty=0, branch="round-2")), "clean tree on round-2, no upstream")
        self.assertEqual(git_text(facts(dirty=5, branch=None)), "5 uncommitted, no upstream")  # detached HEAD
        self.assertEqual(git_text(facts(dirty=5, branch="round-2"), branch=False), "5 uncommitted, no upstream")
        self.assertEqual(git_text(facts(dirty=None, timed_out=True)), "git status unavailable")
        self.assertEqual(git_text({}), "not a git repo")

    def test_odd_values(self) -> None:
        self.assertEqual(git_text(facts(dirty=True)), "git status unavailable")
        self.assertEqual(git_text(facts(dirty=-1)), "git status unavailable")
        self.assertEqual(git_text(facts(dirty=2, branch="   ")), "2 uncommitted, no upstream")
        self.assertEqual(git_text(facts(dirty=2, upstream="origin/x", ahead="7")), "2 uncommitted")

    def test_counts_text(self) -> None:
        self.assertEqual(counts_text(facts(dirty=18, upstream="origin/x", ahead=2)), "18 uncommitted, 2 unpushed")
        self.assertEqual(counts_text(facts(dirty=18, branch="round-2")), "18 uncommitted")
        self.assertEqual(counts_text(facts(dirty=0, upstream="origin/x", ahead=0)), "")
        self.assertEqual(counts_text(None), "")


class EvidenceTextTests(unittest.TestCase):
    def test_spec_shapes(self) -> None:
        self.assertEqual(evidence_text(evidence.ENDING_LIMIT, facts(dirty=91, upstream="origin/main", ahead=2)),
                         "ended on usage limit · 91 uncommitted, 2 unpushed")
        self.assertEqual(evidence_text(evidence.ENDING_QUESTION, facts(dirty=18, branch="round-2")),
                         "ended on an open question · 18 uncommitted on round-2, no upstream")
        self.assertEqual(evidence_text(evidence.ENDING_QUESTION, facts(upstream="origin/main", ahead=0), resumed="10-06"),
                         "ended on an open question · resumed 10-06, ended clean · clean tree")
        self.assertEqual(evidence_text(evidence.ENDING_LIMIT, None), "ended on usage limit · not a git repo")

    def test_branch_that_fails_the_sanitizer_is_dropped(self) -> None:
        for branch in ("fix/bob@example.com", "feat/ghp_" + "x" * 36,
                       "wip/sk-ant-api03-abc", "x/" + "ab12CD34" * 5, "refund-$480", "call-555-867-5309"):
            with self.subTest(branch=branch):
                text = evidence_text(evidence.ENDING_QUESTION, facts(dirty=3, branch=branch))
                self.assertEqual(text, "ended on an open question · 3 uncommitted, no upstream")
                self.assertEqual(sanitize.problems(text), [])

    def test_long_branch_is_dropped_to_stay_within_100_characters(self) -> None:
        branch = "feature/" + "very-long-branch-name-" * 3
        text = evidence_text(evidence.ENDING_QUESTION, facts(dirty=3, branch=branch), resumed="10-06")
        self.assertLessEqual(len(text), 100)
        self.assertNotIn("feature/", text)
        self.assertTrue(text.endswith("3 uncommitted, no upstream"))

    def test_counts_only_then_the_ending_alone(self) -> None:
        def strict(text: str) -> List[str]:
            return ["nope"] if "no upstream" in text else []
        with mock.patch.object(evidence.sanitize, "problems", strict):
            self.assertEqual(evidence_text(evidence.ENDING_QUESTION, facts(dirty=3, branch="b")),
                             "ended on an open question · 3 uncommitted")
            self.assertEqual(evidence_text(evidence.ENDING_QUESTION, facts(dirty=0, branch="b")),
                             "ended on an open question")
        with mock.patch.object(evidence.sanitize, "problems", lambda text: ["nope"] if "·" in text else []):
            self.assertEqual(evidence_text(evidence.ENDING_LIMIT, facts(dirty=3, upstream="o/m", ahead=1), resumed="10-06"),
                             "ended on usage limit")

    def test_random_branch_names_always_give_clean_evidence(self) -> None:
        rng = random.Random(20261005)
        alphabet = "abcXYZ019-_/.@$+#~ é中"
        for _ in range(500):
            branch = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            f = facts(dirty=rng.randint(0, 5000), branch=branch, upstream=rng.choice([None, "origin/main"]),
                      ahead=rng.randint(0, 300))
            for ending in (evidence.ENDING_LIMIT, evidence.ENDING_QUESTION):
                text = evidence_text(ending, f, resumed=rng.choice([None, "10-06"]))
                self.assertEqual(sanitize.problems(text), [], repr(text))
                self.assertTrue(text.startswith(ending))


class UnsavedTests(unittest.TestCase):
    """evidence.unsaved / unsaved_from_text feed classify.tier for save_offer (contract 16.1)."""

    def test_unsaved_from_facts(self) -> None:
        cases = (
            (facts(dirty=12, upstream="origin/main", ahead=2), True),
            (facts(dirty=12, upstream="origin/main", ahead=0), True),
            (facts(dirty=0, upstream="origin/main", ahead=2), True),       # committed, never pushed
            (facts(dirty=0, upstream="origin/main", ahead=0), False),      # saved
            (facts(dirty=18, branch="round-2"), True),                     # no upstream
            (facts(dirty=0, branch="round-2"), False),                     # no upstream counts as saved
            (facts(dirty=0, branch="feat", ahead=2), True),                # no upstream, 2 commits on no remote
            (facts(dirty=0, branch="feat", ahead=0), False),               # no upstream, HEAD already on a remote
            (facts(dirty=3, timed_out=True), True),                        # what git did say is enough
            (facts(dirty=0, timed_out=True), None),                        # ahead never counted
            (facts(dirty=0, upstream="origin/main", ahead=None), None),    # the count failed
            (facts(dirty=None, timed_out=True), None),                     # git status unavailable
            (None, None),                                                  # not a git repo
            ({}, None),
        )
        for f, expected in cases:
            with self.subTest(facts=f):
                self.assertIs(evidence.unsaved(f), expected)

    def test_odd_facts(self) -> None:
        self.assertIsNone(evidence.unsaved(facts(dirty=True)))
        self.assertIsNone(evidence.unsaved(facts(dirty=-1)))
        self.assertIsNone(evidence.unsaved(facts(dirty="3")))
        self.assertIs(evidence.unsaved(facts(dirty=0, upstream="origin/x", ahead="7")), None)  # count not an int
        self.assertIs(evidence.unsaved(facts(dirty=0, upstream="origin/x", ahead=True)), None)
        self.assertIs(evidence.unsaved(facts(dirty=2, upstream="origin/x", ahead="7")), True)

    def test_every_git_text_shape_reads_back(self) -> None:
        cases = (
            ("clean tree", False),
            ("12 uncommitted, 2 unpushed", True),
            ("91 uncommitted, 2 unpushed", True),
            ("3 uncommitted", True),
            ("4 unpushed", True),
            ("10 uncommitted", True),                       # holds "0 uncommitted" as a substring
            ("18 uncommitted on round-2, no upstream", True),
            ("18 uncommitted, no upstream", True),
            ("clean tree on x, no upstream", False),
            ("clean tree on feat, 2 unpushed, no upstream", True),
            ("clean tree, 2 unpushed, no upstream", True),
            ("3 uncommitted on feat, 1 unpushed, no upstream", True),
            ("clean tree, no upstream", False),
            ("not a git repo", None),
            (evidence.UNAVAILABLE, None),
            ("git status unavailable", None),
            ("0 uncommitted", False),                        # never written by git_text; read as saved
            ("ended on usage limit · 91 uncommitted, 2 unpushed", True),
            ("ended on an open question · 18 uncommitted on round-2, no upstream", True),
            ("ended on an open question · resumed 10-06, ended clean · clean tree", False),
            ("ended on usage limit · not a git repo", None),
            ("ended on an open question", None),             # the git part was dropped
            ("", None),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertIs(evidence.unsaved_from_text(text), expected)
        for odd in (None, 42, b"3 uncommitted", ["3 uncommitted"]):
            with self.subTest(odd=odd):
                self.assertIsNone(evidence.unsaved_from_text(odd))

    def test_read_back_agrees_with_the_facts(self) -> None:
        rng = random.Random(20261108)
        for _ in range(400):
            f = facts(dirty=rng.choice([0, 0, 1, 7, 10, 120]), branch=rng.choice([None, "main", "round-2", "x"]),
                      upstream=rng.choice([None, "origin/main"]), ahead=rng.choice([None, 0, 0, 2, 10]))
            # as git_facts writes them: ahead is counted when there is an upstream, and without
            # one only when the repo has a remote (commits on no remote), else None
            if f["upstream"] and f["ahead"] is None:
                f["ahead"] = 0
            want = evidence.unsaved(f)
            for ending in (evidence.ENDING_LIMIT, evidence.ENDING_QUESTION):
                for resumed in (None, "10-06"):
                    text = evidence_text(ending, f, resumed=resumed)
                    with self.subTest(text=text):
                        self.assertIs(evidence.unsaved_from_text(text), want)
            self.assertIs(evidence.unsaved_from_text(git_text(f)), want)
            self.assertIs(evidence.unsaved_from_text(git_text(f, branch=False)), want)
        self.assertIsNone(evidence.unsaved_from_text(evidence_text(evidence.ENDING_LIMIT, None)))

    def test_feeds_the_save_offer_tier(self) -> None:
        from core import classify as C
        dirty = evidence.unsaved(facts(dirty=32, branch="round-2"))
        saved = evidence.unsaved(facts(dirty=0, upstream="origin/main", ahead=0))
        self.assertEqual(C.tier(C.OPEN_QUESTION, C.SAVE_OFFER, unsaved=dirty), C.WAITING)
        self.assertEqual(C.tier(C.OPEN_QUESTION, C.SAVE_OFFER, unsaved=saved), C.SOFT)
        self.assertEqual(C.tier(C.OPEN_QUESTION, C.SAVE_OFFER, unsaved=evidence.unsaved(None)), C.SOFT)
        text = evidence_text(evidence.ENDING_QUESTION, facts(dirty=32, branch="round-2"))
        self.assertEqual(C.tier(C.OPEN_QUESTION, C.SAVE_OFFER, unsaved=evidence.unsaved_from_text(text)), C.WAITING)


@unittest.skipIf(GIT is None, "git is not installed")
class GitFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, HERMETIC)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dirty_tree_without_upstream(self) -> None:
        repo = make_repo(self.dir / "chipmunk", branch="round-2")
        (repo / "f0.txt").write_text("changed\n", encoding="utf-8")  # modified
        (repo / "new1.txt").write_text("x", encoding="utf-8")  # untracked
        (repo / "newdir").mkdir()
        (repo / "newdir" / "a.txt").write_text("a", encoding="utf-8")
        (repo / "newdir" / "b.txt").write_text("b", encoding="utf-8")  # an untracked dir counts once
        f = git_facts(repo)
        self.assertEqual(f, {"dirty": 3, "branch": "round-2", "upstream": None, "ahead": None, "timed_out": False})
        self.assertEqual(git_text(f), "3 uncommitted on round-2, no upstream")
        self.assertIs(evidence.unsaved(f), True)

    def test_upstream_and_unpushed_commits(self) -> None:
        remote = self.dir / "remote.git"
        remote.mkdir()
        git(remote, "init", "-q", "--bare")
        repo = make_repo(self.dir / "boathouse")
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        self.assertEqual(git_text(git_facts(repo)), "clean tree")
        self.assertIs(evidence.unsaved(git_facts(repo)), False)
        for i in range(2):
            (repo / ("later%d.txt" % i)).write_text("x", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "later %d" % i)
        (repo / "scratch.txt").write_text("x", encoding="utf-8")
        f = git_facts(repo)
        self.assertEqual((f["dirty"], f["branch"], f["upstream"], f["ahead"]), (1, "main", "origin/main", 2))
        self.assertEqual(git_text(f), "1 uncommitted, 2 unpushed")
        self.assertIs(evidence.unsaved(f), True)
        (repo / "scratch.txt").unlink()
        f = git_facts(repo)
        self.assertEqual(git_text(f), "2 unpushed")
        self.assertIs(evidence.unsaved(f), True)  # committed but not pushed is not saved

    def test_local_branch_without_upstream_counts_commits_on_no_remote(self) -> None:
        remote = self.dir / "remote.git"
        remote.mkdir()
        git(remote, "init", "-q", "--bare")
        repo = make_repo(self.dir / "shop")
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        git(repo, "checkout", "-q", "-b", "feat")
        f = git_facts(repo)  # the new branch's HEAD is on the remote already: saved
        self.assertEqual((f["upstream"], f["ahead"]), (None, 0))
        self.assertIs(evidence.unsaved(f), False)
        self.assertEqual(git_text(f), "clean tree on feat, no upstream")
        (repo / "feat.txt").write_text("x", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat")
        f = git_facts(repo)
        self.assertEqual((f["dirty"], f["branch"], f["upstream"], f["ahead"]), (0, "feat", None, 1))
        self.assertIs(evidence.unsaved(f), True)
        self.assertEqual(git_text(f), "clean tree on feat, 1 unpushed, no upstream")
        text = evidence_text(evidence.ENDING_QUESTION, f)
        self.assertEqual(text, "ended on an open question · clean tree on feat, 1 unpushed, no upstream")
        self.assertIs(evidence.unsaved_from_text(text), True)
        from core import classify as C
        self.assertEqual(C.tier(C.OPEN_QUESTION, C.SAVE_OFFER, unsaved=evidence.unsaved(f)), C.WAITING)

    def test_no_remote_at_all_counts_as_saved(self) -> None:
        repo = make_repo(self.dir / "notes", commits=2)
        f = git_facts(repo)
        self.assertEqual((f["upstream"], f["ahead"]), (None, None))
        self.assertIs(evidence.unsaved(f), False)

    def test_subdirectory_reports_the_whole_repo(self) -> None:
        repo = make_repo(self.dir / "mono")
        (repo / "pkg").mkdir()
        (repo / "top.txt").write_text("x", encoding="utf-8")
        self.assertEqual(git_facts(repo / "pkg")["dirty"], 1)

    def test_detached_head_and_unborn_branch(self) -> None:
        repo = make_repo(self.dir / "detached", commits=2)
        git(repo, "checkout", "-q", "--detach", "HEAD~1")
        f = git_facts(repo)
        self.assertIsNone(f["branch"])
        self.assertEqual(git_text(f), "clean tree, no upstream")
        empty = self.dir / "empty"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "trunk")
        (empty / "a.txt").write_text("a", encoding="utf-8")
        self.assertEqual(git_text(git_facts(empty)), "1 uncommitted on trunk, no upstream")

    def test_not_a_repo_and_bad_paths(self) -> None:
        plain = self.dir / "plain"
        plain.mkdir()
        self.assertIsNone(git_facts(plain))
        self.assertIsNone(evidence.unsaved(git_facts(plain)))
        f = self.dir / "file.txt"
        f.write_text("x", encoding="utf-8")
        for bad in (f, self.dir / "missing", "", None, 42, "bad\x00path", b"/tmp"):
            with self.subTest(path=repr(bad)):
                self.assertIsNone(git_facts(bad))
        self.assertIsNone(git_facts(make_repo(self.dir / "r") / ".git"))  # the git dir is not a work tree

    def test_odd_timeouts_never_raise(self) -> None:
        repo = make_repo(self.dir / "t")
        self.assertIsNotNone(git_facts(repo, timeout=float("nan")))
        self.assertIsNone(git_facts(repo, timeout="soon"))  # type: ignore[arg-type]

    def test_timeout_keeps_what_it_has(self) -> None:
        repo = make_repo(self.dir / "slow")
        f = git_facts(repo, timeout=0)
        self.assertEqual(f, {"dirty": None, "branch": None, "upstream": None, "ahead": None, "timed_out": True})
        self.assertEqual(git_text(f), "git status unavailable")

        real = subprocess.run
        calls = []

        def slow_after_status(cmd: List[str], **kw: Any) -> Any:
            calls.append(cmd[3])
            if cmd[3] != "status":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return real(cmd, **kw)
        with mock.patch.object(evidence.subprocess, "run", slow_after_status):
            f = git_facts(repo)
        self.assertEqual((f["dirty"], f["branch"], f["timed_out"]), (0, None, True))
        self.assertEqual(calls, ["status", "symbolic-ref"])
        self.assertIsNone(evidence.unsaved(f))  # a clean tree, but unpushed commits were never counted

    def test_git_missing(self) -> None:
        repo = make_repo(self.dir / "nogit")
        with mock.patch.object(evidence, "GIT", str(self.dir / "no-such-git")):
            self.assertIsNone(git_facts(repo))

    def test_environment_is_read_only_and_points_at_the_directory(self) -> None:
        a = make_repo(self.dir / "a")
        b = make_repo(self.dir / "b")
        (b / "dirty.txt").write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(b / ".git"), "GIT_WORK_TREE": str(b)}):
            self.assertEqual(git_facts(a)["dirty"], 0)  # GIT_DIR from the caller is ignored
        seen: List[Dict[str, Any]] = []
        real = subprocess.run

        def spy(cmd: List[str], **kw: Any) -> Any:
            seen.append(kw)
            return real(cmd, **kw)
        with mock.patch.object(evidence.subprocess, "run", spy):
            git_facts(a)
        self.assertTrue(seen)
        for kw in seen:
            self.assertEqual(kw["env"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertNotIn("GIT_DIR", kw["env"])
            self.assertIs(kw["stdin"], subprocess.DEVNULL)
            self.assertLessEqual(kw["timeout"], evidence.DEFAULT_TIMEOUT)

    def test_status_takes_no_index_lock(self) -> None:
        repo = make_repo(self.dir / "locked")
        (repo / ".git" / "index.lock").write_text("", encoding="utf-8")  # a session's git is mid-command
        (repo / "f0.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(git_facts(repo)["dirty"], 1)
        self.assertTrue((repo / ".git" / "index.lock").exists())


if __name__ == "__main__":
    unittest.main()
