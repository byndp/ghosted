"""Unsaved work (core.unsaved, the report's Unsaved work section, contract 16.9).

Synthetic repos in temp dirs only: file ages set with os.utime, commit ages with
GIT_COMMITTER_DATE. Hermetic git (no global/system config). Nothing reads ~/workspace.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

from core import cli, config, pull, timeutil, touches, unsaved
from core.config import Config
from core.ledger import Ledger
from tests.test_pull import Synth
from tests.test_touches import Session

TZ = "America/Chicago"
NOW = timeutil.parse_iso("2026-11-08T18:00:00Z")
GIT = shutil.which("git")
HERMETIC = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
SECRET_FILE = "client-acme-contract.md"
SECRET_BRANCH = "feat/acme-pricing"
SECRET_SUBJECT = "Acme invoice tweaks"


def run_git(cwd: Path, *args: str, when: Optional[datetime] = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", **HERMETIC)
    if when is not None:
        stamp = "%d +0000" % int(when.timestamp())
        env["GIT_COMMITTER_DATE"] = stamp
        env["GIT_AUTHOR_DATE"] = stamp
    out = subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return out.stdout.decode()


def age(path: Path, days: float) -> None:
    t = (NOW - timedelta(days=days)).timestamp()
    os.utime(str(path), (t, t))


@unittest.skipIf(GIT is None, "git not installed")
class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.home = self.root / "ghosted-home"
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.env_patch = mock.patch.dict(os.environ, HERMETIC)
        self.env_patch.start()
        # These repos live in a temp dir, which the touch index treats as scratch (contract 16.11).
        self.eph_patch = mock.patch.object(touches, "EPHEMERAL_ROOTS", ())
        self.eph_patch.start()

    def tearDown(self) -> None:
        self.eph_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def cfg(self, **kw: Any) -> Config:
        base: Dict[str, Any] = dict(home=self.home, projects_dir=self.projects, timezone=TZ,
                                    workspace_roots=[self.ws])
        base.update(kw)
        return Config(**base)

    def repo(self, name: str, remote: bool = True, branch: str = "main", committed_days: float = 30,
             parent: Optional[Path] = None) -> Path:
        path = (parent or self.ws) / name
        path.mkdir(parents=True)
        run_git(path, "init", "-q", "-b", branch)
        (path / "README.md").write_text("hi\n")
        run_git(path, "add", "-A")
        run_git(path, "commit", "-q", "-m", "init", when=NOW - timedelta(days=committed_days))
        age(path / "README.md", committed_days)
        if remote:
            bare = self.root / "remotes" / (name + ".git")
            bare.parent.mkdir(exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(path), str(bare)], check=True,
                           env=dict(os.environ, **HERMETIC), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run_git(path, "remote", "add", "origin", str(bare))
            run_git(path, "fetch", "-q", "origin")
            run_git(path, "branch", "-q", "-u", "origin/" + branch)
            run_git(path, "remote", "set-head", "origin", branch)
        return path

    def commit(self, path: Path, days: float, name: str = "f.txt", message: str = "work") -> None:
        (path / name).write_text("x %s\n" % days)
        run_git(path, "add", "-A")
        run_git(path, "commit", "-q", "-m", message, when=NOW - timedelta(days=days))
        age(path / name, days)

    def flags_of(self, path: Path, cfg: Optional[Config] = None) -> Dict[str, int]:
        cfg = cfg or self.cfg()
        return unsaved.flags(unsaved.repo_facts(path, NOW), cfg, NOW)


# ---- scan rules ---------------------------------------------------------------------------------

class RuleTests(Base):
    def test_repos_found_once_root_included_sorted(self) -> None:
        run_git(self.ws, "init", "-q", "-b", "main")
        self.repo("b")
        self.repo("a")
        (self.ws / "plain-dir").mkdir()
        got = unsaved.repos(self.cfg(workspace_roots=[self.ws, self.ws]))
        self.assertEqual(got, [self.ws, self.ws / "a", self.ws / "b"])

    def test_clean_repo_has_no_flags(self) -> None:
        self.assertEqual(self.flags_of(self.repo("shop")), {})

    def test_dirty_stale_vs_fresh(self) -> None:
        stale = self.repo("stale")
        (stale / "README.md").write_text("changed\n")
        age(stale / "README.md", 5)
        fresh = self.repo("fresh")
        (fresh / "README.md").write_text("changed\n")
        age(fresh / "README.md", 1)
        self.assertEqual(self.flags_of(stale), {"uncommitted": 1})
        self.assertEqual(self.flags_of(fresh), {})
        facts = unsaved.repo_facts(stale, NOW)
        self.assertEqual(facts["uncommitted_since"][:10], (NOW - timedelta(days=5)).strftime("%Y-%m-%d"))

    def test_oldest_changed_file_sets_the_age(self) -> None:
        path = self.repo("mixed")
        for i, days in enumerate((10, 0.1, 0.2)):
            f = path / ("n%d.txt" % i)
            f.write_text("x")
            age(f, days)
        self.assertEqual(self.flags_of(path), {"uncommitted": 3})

    def test_untracked_only_and_untracked_directory(self) -> None:
        path = self.repo("notes")
        d = path / "drafts"
        d.mkdir()
        for i in range(3):
            (d / ("d%d.md" % i)).write_text("x")
            age(d / ("d%d.md" % i), 9 - i)  # newest inside = 7 days
        age(d, 0)  # the directory's own mtime is ignored
        facts = unsaved.repo_facts(path, NOW)
        self.assertEqual(facts["uncommitted"], 1)
        self.assertEqual(facts["uncommitted_since"][:10], (NOW - timedelta(days=7)).strftime("%Y-%m-%d"))
        self.assertEqual(self.flags_of(path), {"uncommitted": 1})

    def test_deleted_path_has_no_age(self) -> None:
        path = self.repo("gone")
        (path / "README.md").unlink()
        facts = unsaved.repo_facts(path, NOW)
        self.assertEqual(facts["uncommitted"], 1)
        self.assertIsNone(facts["uncommitted_since"])
        self.assertEqual(self.flags_of(path), {})

    def test_ahead_old_vs_new(self) -> None:
        old = self.repo("old")
        self.commit(old, 3)
        new = self.repo("new")
        self.commit(new, 0.2)
        self.assertEqual(self.flags_of(old), {"unpushed": 1})
        self.assertEqual(self.flags_of(new), {})
        self.assertEqual(unsaved.repo_facts(new, NOW)["unpushed"], 1)

    def test_branch_with_commits_and_no_upstream_is_unpushed(self) -> None:
        path = self.repo("feat")
        run_git(path, "checkout", "-q", "-b", SECRET_BRANCH)
        self.commit(path, 2)
        self.commit(path, 2.5, name="g.txt")
        facts = unsaved.repo_facts(path, NOW)
        self.assertEqual(facts["unpushed"], 2)
        self.assertEqual(self.flags_of(path), {"unpushed": 2})

    def test_no_remote_vs_no_remote_ok(self) -> None:
        path = self.repo("books", remote=False)
        self.commit(path, 20)  # commits with no remote are the no-remote rule, not "unpushed"
        self.assertEqual(self.flags_of(path), {"no_remote": 1})
        ok = self.cfg(unsaved_no_remote_ok=[path])
        self.assertEqual(self.flags_of(path, ok), {})

    def test_unmerged_old_vs_new(self) -> None:
        path = self.repo("app")
        for name, days in (("old-1", 20), ("old-2", 30), ("new-1", 3)):
            run_git(path, "checkout", "-q", "-b", name, "main")
            self.commit(path, days, name=name + ".txt")
            run_git(path, "push", "-q", "origin", name)  # pushed, so not unpushed: only unmerged
        run_git(path, "checkout", "-q", "main")
        run_git(path, "branch", "-q", "merged", "main")  # no own commits: merged
        self.assertEqual(self.flags_of(path), {"unmerged": 2})

    def test_no_default_branch_skips_unmerged(self) -> None:
        path = self.repo("odd", remote=False, branch="trunk")
        run_git(path, "checkout", "-q", "-b", "side")
        self.commit(path, 40)
        facts = unsaved.repo_facts(path, NOW)
        self.assertIsNone(facts["default_branch"])
        cfg = self.cfg(unsaved_no_remote_ok=[path])
        self.assertEqual(unsaved.flags(facts, cfg, NOW), {})
        notes = unsaved.notes(facts)
        self.assertIn("no default branch: unmerged not checked", notes)

    def test_default_branch_falls_back_to_master(self) -> None:
        path = self.repo("legacy", remote=False, branch="master")
        self.assertEqual(unsaved.repo_facts(path, NOW)["default_branch"], "master")

    def test_nested_repo_not_double_counted(self) -> None:
        run_git(self.ws, "init", "-q", "-b", "main")
        (self.ws / "top.md").write_text("x")
        run_git(self.ws, "add", "-A")
        run_git(self.ws, "commit", "-q", "-m", "top", when=NOW - timedelta(days=30))
        self.repo("child")
        child_file = self.ws / "child" / "README.md"
        child_file.write_text("dirty\n")
        age(child_file, 10)
        root_facts = unsaved.repo_facts(self.ws, NOW)
        self.assertEqual(root_facts["uncommitted"], 0)  # the child repo is not the root's untracked dir
        child_facts = unsaved.repo_facts(self.ws / "child", NOW)
        self.assertEqual(child_facts["uncommitted"], 1)

    def test_linked_worktree_branch_facts_not_double_counted(self) -> None:
        main = self.repo("trip")
        run_git(main, "checkout", "-q", "-b", "local-only")
        self.commit(main, 20)
        run_git(main, "checkout", "-q", "main")
        run_git(main, "worktree", "add", "-q", "-b", "wt", str(self.ws / "trip-map"))
        wt_file = self.ws / "trip-map" / "draft.md"
        wt_file.write_text("x")
        age(wt_file, 9)
        by = {c.path.name: c for c in unsaved.check(self.cfg(), NOW)}
        self.assertEqual(by["trip"].flags, {"unpushed": 1, "unmerged": 1})
        self.assertEqual(by["trip-map"].flags, {"uncommitted": 1})  # its own tree only
        # Alone (the main repo outside the roots) a worktree keeps every rule.
        alone = unsaved.flags(unsaved.repo_facts(self.ws / "trip-map", NOW), self.cfg(), NOW)
        self.assertIn("unpushed", alone)

    def test_timeout_is_git_status_unavailable(self) -> None:
        path = self.repo("slow")
        (path / "README.md").write_text("changed\n")
        age(path / "README.md", 10)
        facts = unsaved.repo_facts(path, NOW, timeout=0.0)
        self.assertEqual(facts["error"], "timeout")
        self.assertEqual(unsaved.flags(facts, self.cfg(), NOW), {})
        self.assertIn("git status unavailable", unsaved.notes(facts))

    def test_not_a_repo_never_raises(self) -> None:
        facts = unsaved.repo_facts(self.root / "missing", NOW)
        self.assertEqual(facts["error"], "git")

    def test_never_writes_to_the_repo(self) -> None:
        path = self.repo("ro")
        (path / "README.md").write_text("changed\n")
        age(path / "README.md", 10)
        before = run_git(path, "status", "--porcelain") + run_git(path, "log", "--all", "--format=%H")
        refs = sorted(p.name for p in (path / ".git").rglob("*") if p.is_file() and "refs" in p.parts)
        unsaved.check(self.cfg(), NOW)
        self.assertEqual(before, run_git(path, "status", "--porcelain") + run_git(path, "log", "--all", "--format=%H"))
        self.assertEqual(refs, sorted(p.name for p in (path / ".git").rglob("*") if p.is_file() and "refs" in p.parts))

    def test_check_skips_active_repo(self) -> None:
        busy = self.repo("busy")
        (busy / "README.md").write_text("changed\n")
        age(busy / "README.md", 10)
        idle = self.repo("idle", remote=False)
        checks = unsaved.check(self.cfg(), NOW, active_cwds=[str(busy / "sub")])
        by = {c.path.name: c for c in checks}
        self.assertEqual(by["busy"].flags, {})
        self.assertIn("active: not checked", by["busy"].notes)
        self.assertEqual(by["idle"].flags, {"no_remote": 1})


# ---- config ---------------------------------------------------------------------------------------

class ConfigTests(Base):
    def test_defaults_and_keys(self) -> None:
        cfg = config.build_config({}, self.home, environ={"HOME": str(self.root)})
        self.assertEqual((cfg.unsaved_stale_days, cfg.unsaved_unpushed_days, cfg.unsaved_unmerged_days),
                         (3, 1, 14))
        self.assertEqual(cfg.unsaved_no_remote_ok, [])
        raw = {"unsaved": {"stale_days": 1, "unpushed_days": 0, "unmerged_days": 30,
                           "no_remote_ok": ["~/workspace/invoices"]}}
        cfg = config.build_config(raw, self.home, environ={"HOME": str(self.root)})
        self.assertEqual((cfg.unsaved_stale_days, cfg.unsaved_unpushed_days, cfg.unsaved_unmerged_days),
                         (1, 0, 30))
        self.assertEqual(cfg.unsaved_no_remote_ok, [self.root / "workspace" / "invoices"])
        with self.assertRaises(config.ConfigError):
            config.build_config({"unsaved": {"stale_days": -1}}, self.home, environ={})

    def test_init_writes_the_table(self) -> None:
        text = cli.render_config()
        self.assertIn("[unsaved]", text)
        self.assertIn("stale_days = 3", text)
        from core import minitoml

        self.assertEqual(minitoml.loads(text)["unsaved"]["unmerged_days"], 14)


# ---- report + ledger --------------------------------------------------------------------------------

class ReportTests(Base):
    def gather(self, cfg: Optional[Config] = None, **kw: Any) -> pull.Report:
        cfg = cfg or self.cfg()
        kw.setdefault("scan", False)
        return pull.gather(cfg, Ledger(cfg), NOW, window="7d", live=None, **kw)

    def dirty(self, name: str, n: int = 1, days: float = 10, remote: bool = True) -> Path:
        path = self.repo(name, remote=remote)
        for i in range(n):
            f = path / ("%s-%d" % (SECRET_FILE, i))
            f.write_text("x")
            age(f, days)
        return path

    def test_section_rows_numbers_and_layout(self) -> None:
        self.dirty("metronome", n=3, days=100)
        books = self.repo("tax-books", remote=False)
        app = self.repo("boathouse")
        run_git(app, "checkout", "-q", "-b", SECRET_BRANCH)
        self.commit(app, 20, message=SECRET_SUBJECT)
        run_git(app, "push", "-q", "origin", SECRET_BRANCH)
        run_git(app, "checkout", "-q", "main")
        self.commit(app, 2)
        rep = self.gather()
        text = pull.render_text(rep)
        self.assertIn("Unsaved work: 3 repos ─", text)
        self.assertRegex(text, r"\n  \d ! metronome · 3 uncommitted since 31 Jul \(100d\)\n")
        self.assertRegex(text, r"\n  \d ! boathouse · 1 unpushed \(2d\) · 1 branch not merged\n")
        self.assertRegex(text, r"\n  \d ! tax-books · no remote\n")
        self.assertIn("→ cd %s && git status" % (self.ws / "metronome"), text)
        self.assertIn("Nothing waiting on you in the last 7 days (0 sessions checked).", text)
        self.assertNotIn("✓", text)                                # no session was checked: no tick
        receipt = pull.render_receipt(rep).splitlines()            # totals only: the whole receipt
        self.assertEqual(receipt[1:], ["0 sessions in 0 projects checked", "0 ghosted",
                                       "3 files left uncommitted · 1 commit left unpushed",
                                       pull.RECEIPT_ADDRESS])
        self.assertRegex(receipt[0], r"^GHOSTED · 7 days of Claude Code \([A-Za-z0-9 –]+\)$")
        items = Ledger(self.cfg()).items()
        kinds = sorted(it["kind"] for it in items.values())
        self.assertEqual(kinds, ["unsaved"] * 3)
        n = int(re.search(r"\n  (\d) ! metronome", text).group(1))
        self.assertEqual(items["GS-%04d" % n]["repo"], str(self.ws / "metronome"))
        self.assertIn("Done with one? ghosted done", text)
        self.assertTrue(books.exists())

    def test_no_names_in_output(self) -> None:
        app = self.dirty("acme-site", n=2, days=9)
        run_git(app, "checkout", "-q", "-b", SECRET_BRANCH)
        self.commit(app, 20, message=SECRET_SUBJECT)
        rep = self.gather()
        for plain in (False, True):
            text = pull.render_text(rep, plain=plain) + pull.render_text(rep, plain=plain)
            for secret in (SECRET_FILE, SECRET_BRANCH, "acme-pricing", SECRET_SUBJECT):
                self.assertNotIn(secret, text)
        blob = pull.render_json(rep) + json.dumps(Ledger(self.cfg()).items())
        for secret in (SECRET_FILE, SECRET_BRANCH, SECRET_SUBJECT):
            self.assertNotIn(secret, blob)

    def test_plain_words(self) -> None:
        self.dirty("shop", n=2, days=5)
        self.repo("books", remote=False)
        text = pull.render_text(self.gather(), plain=True)
        self.assertIn("2 changes not saved in git since", text)
        self.assertIn("no copy anywhere but this computer (no remote)", text)
        self.assertIn("To check it, paste: cd ", text)
        self.assertNotIn(" ! ", text)

    def test_no_unsaved_flag_and_no_roots(self) -> None:
        self.dirty("shop")
        self.assertNotIn("Unsaved work", pull.render_text(self.gather(unsaved=False)))
        self.assertNotIn("Unsaved work", pull.render_text(self.gather(cfg=self.cfg(workspace_roots=[]))))

    def test_json_counts_and_measure(self) -> None:
        self.dirty("shop", n=4, days=6)
        rep = self.gather()
        doc = json.loads(pull.render_json(rep))
        self.assertEqual(doc["counts"]["unsaved"], 1)
        row = doc["unsaved"][0]
        self.assertEqual((row["project"], row["uncommitted"], row["no_remote"]), ("shop", 4, False))
        self.assertEqual(sorted(row), sorted(["n", "id", "project", "repo", "uncommitted", "uncommitted_since",
                                              "unpushed", "unpushed_since", "unmerged", "no_remote", "mixed"]))
        self.assertIsNone(row["mixed"])
        rec = [m for m in Ledger(self.cfg()).measures() if m.get("type") == "report"][-1]
        self.assertEqual(rec["unsaved"], [row["id"]])

    def test_project_filter(self) -> None:
        self.dirty("shop")
        self.dirty("api")
        text = pull.render_text(self.gather(project="api"))
        self.assertIn("! api", text)
        self.assertNotIn("! shop", text)

    def test_items_are_local_only(self) -> None:
        self.dirty("shop")
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        self.assertEqual(led.events(), [])
        self.assertEqual(led.items_missing_events(), [])
        led.set_status(iid, "dismissed", now=NOW, dismiss_reason="not_real")
        self.assertEqual(led.events(), [])

    def test_repo_saved_closes_item_then_new_dirt_reopens(self) -> None:
        path = self.dirty("shop", n=1)
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        run_git(path, "add", "-A")
        run_git(path, "commit", "-q", "-m", "save", when=NOW - timedelta(hours=1))
        run_git(path, "push", "-q", "origin", "main")
        rep = self.gather()
        self.assertEqual(rep.unsaved, [])
        self.assertEqual((led.get(iid)["status"], led.get(iid)["closed_by"]), ("done", "scan"))
        f = path / "again.txt"
        f.write_text("x")
        age(f, 8)
        rep = self.gather()
        self.assertEqual([r["id"] for r in rep.unsaved], [iid])
        self.assertEqual(led.get(iid)["status"], "open")

    def test_dismiss_then_count_increases_returns(self) -> None:
        path = self.dirty("shop", n=2)
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        led.set_status(iid, "dismissed", now=NOW, dismiss_reason="not_real")
        self.assertEqual(led.get(iid)["dismissed_counts"], {"uncommitted": 2})
        self.assertEqual(self.gather().unsaved, [])          # same count: stays dismissed
        f = path / "third.txt"
        f.write_text("x")
        age(f, 8)
        rep = self.gather()
        self.assertEqual([r["id"] for r in rep.unsaved], [iid])
        self.assertEqual(led.get(iid)["status"], "open")

    def test_dismiss_no_remote_stays_dismissed(self) -> None:
        self.repo("books", remote=False)
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        led.set_status(iid, "dismissed", now=NOW, dismiss_reason="not_real")
        self.assertEqual(self.gather().unsaved, [])
        self.assertEqual(led.get(iid)["status"], "dismissed")

    def test_snoozed_hidden_until_due(self) -> None:
        self.dirty("shop")
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        led.set_status(iid, "snoozed", now=NOW, snooze_until="2026-11-11T09:00")
        self.assertEqual(self.gather().unsaved, [])
        cfg = self.cfg()
        later = NOW + timedelta(days=4)
        rep = pull.gather(cfg, Ledger(cfg), later, window="7d", live=None, scan=False)
        self.assertEqual([r["id"] for r in rep.unsaved], [iid])

    def test_active_session_repo_skipped(self) -> None:
        busy = self.dirty("busy")
        s = Synth(self.projects, self.ws)
        s.write("busy", [s.text("Working on it.")], NOW - timedelta(minutes=5), cwd=busy)
        rep = self.gather(scan=True)
        self.assertEqual(rep.unsaved, [])
        self.assertIn("(busy: active: not checked)", pull.render_text(rep, debug=True))
        self.assertNotIn("not checked", pull.render_text(rep))

    def test_debug_notes_for_timeouts(self) -> None:
        self.dirty("slow")
        with mock.patch.object(unsaved, "REPO_TIMEOUT", 0.0):
            rep = self.gather()
        self.assertEqual(rep.unsaved, [])
        self.assertIn("(slow: git status unavailable)", pull.render_text(rep, debug=True))

    def test_failed_history_query_never_closes_an_item(self) -> None:
        # A failed git query is "unavailable", never "saved".
        path = self.repo("shop")
        self.commit(path, 3)
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        real = unsaved.evidence._git
        for query in ("log", "for-each-ref", "remote"):
            with self.subTest(query=query):
                def one(where, args, deadline, env, q=query):
                    return None if args[:1] == [q] else real(where, args, deadline, env)
                with mock.patch.object(unsaved.evidence, "_git", side_effect=one):
                    facts = unsaved.repo_facts(path, NOW)
                    self.assertEqual(facts["error"], "git")
                    self.assertIsNone(unsaved.remaining(facts, ["unpushed"], self.cfg(), NOW))
                    self.gather()
                self.assertEqual(led.get(iid)["status"], "open")

    def test_id_collision_renumbers_unsaved_locally_without_events(self) -> None:
        # A server item under an unsaved item's id moves the unsaved
        # item to a new id, and still queues nothing for the server.
        from tests.test_id_collision import server_item

        self.dirty("shop")
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        out = led.merge_server_items([server_item(iid, "demo: other thing", status="open")], max_seq=1, now=NOW)
        self.assertEqual(len(out["renumbered"]), 1)
        moved = led.find_by_repo(str(self.ws / "shop"))
        self.assertNotEqual(moved["id"], iid)
        self.assertEqual((moved["kind"], moved["status"], moved["rev"]), ("unsaved", "open", 1))
        self.assertEqual([e for e in led.events() if (e.get("item") or {}).get("kind") == "unsaved"], [])
        self.assertEqual(led.unacked_events(), [])

    def test_done_ignores_age_for_unmerged(self) -> None:
        # `ghosted done` counts every unmerged branch, fresh tips too.
        facts = {"path": "/x", "error": None, "uncommitted": 0, "unpushed": 0, "has_remote": True,
                 "default_branch": "main", "unmerged": [timeutil.to_iso(NOW - timedelta(hours=1))]}
        self.assertEqual(unsaved.remaining(facts, ["unmerged"], self.cfg(), NOW), {"unmerged": 1})
        self.assertEqual(unsaved.remaining(dict(facts, unmerged=[]), ["unmerged"], self.cfg(), NOW), {})

    def test_blocked_session_does_not_hide_its_repo(self) -> None:
        # A deferred session `claude agents` reports blocked is
        # waiting on you, not active: its repo is still checked.
        from core import agents

        busy = self.dirty("busy")
        s = Synth(self.projects, self.ws)
        sid = s.write("busy", [s.text("Working on it.")], NOW - timedelta(minutes=5), cwd=busy)
        cfg = self.cfg()
        live = {sid: agents.LiveState("interactive", "blocked")}
        rep = pull.gather(cfg, Ledger(cfg), NOW, window="7d", live=live, scan=True)
        self.assertEqual([r["project"] for r in rep.unsaved], ["busy"])

    def test_local_push_never_sees_unsaved(self) -> None:
        from core import runner

        self.dirty("shop")
        self.gather()
        items = list(Ledger(self.cfg()).items().values())
        self.assertEqual(runner.push_candidates(self.cfg(), items), [])


# ---- CLI ------------------------------------------------------------------------------------------

class CliMixin:
    def run_cli(self, *argv: str):
        self.home.mkdir(mode=0o700, exist_ok=True)
        cfg = self.home / "config.toml"
        if not cfg.exists():
            cfg.write_text('timezone = "%s"\nworkspace_roots = [%s]\n' % (TZ, json.dumps(str(self.ws))))
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home), "GHOSTED_PROJECTS_DIR": str(self.projects),
               "GHOSTED_NOW": timeutil.to_iso(NOW), "PATH": str(self.root / "nobin")}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), environ=env)
        return code, out.getvalue(), err.getvalue()


class CliTests(CliMixin, Base):
    def test_done_refused_while_dirty_then_accepted(self) -> None:
        path = self.repo("shop")
        for i in range(3):
            (path / ("n%d" % i)).write_text("x")
            age(path / ("n%d" % i), 9)
        code, out, err = self.run_cli("report", "--no-scan")
        self.assertEqual(code, 0, err)
        n = int(re.search(r"\n  (\d) ! shop", out).group(1))
        code, out, err = self.run_cli("done", str(n))
        self.assertEqual(code, 1)
        self.assertIn("GS-%04d still 3 uncommitted" % n, out + err)
        self.assertIn("ghosted dismiss %d" % n, out + err)
        run_git(path, "add", "-A")
        run_git(path, "commit", "-q", "-m", "save")
        code, out, err = self.run_cli("done", str(n))
        self.assertIn("GS-%04d still 1 unpushed" % n, out + err)  # committed, not pushed: still unsaved
        self.assertEqual(code, 1)
        run_git(path, "push", "-q", "origin", "main")
        code, out, err = self.run_cli("done", str(n))
        self.assertEqual(code, 0, out + err)

    def test_report_flags(self) -> None:
        self.repo("books", remote=False)
        code, out, _ = self.run_cli("report", "--no-scan", "--no-unsaved")
        self.assertEqual(code, 0)
        self.assertNotIn("Unsaved work", out)
        code, out, _ = self.run_cli("report", "--no-scan", "--debug")
        self.assertEqual(code, 0)
        self.assertIn("Unsaved work: 1 repo ─", out)


# ---- step 11 (contract 16.11): repos from sessions, the mixed rule, ghosted mixed --------------------------

START_A = NOW - timedelta(minutes=100)
START_B = NOW - timedelta(minutes=80)


class SharedBase(CliMixin, Base):
    def gather(self, cfg: Optional[Config] = None, **kw: Any) -> pull.Report:
        cfg = cfg or self.cfg()
        kw.setdefault("scan", True)
        return pull.gather(cfg, Ledger(cfg), NOW, window="7d", live=None, **kw)

    def changed(self, repo: Path, name: str, when: datetime) -> Path:
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        os.utime(str(f), (when.timestamp(), when.timestamp()))
        return f

    def work(self, repo: Path, names: Any, start: datetime, launch: Optional[Path] = None) -> Session:
        """A session that edits ``names`` in ``repo`` a minute apart; each file saved a second later."""
        s = Session(self.projects, launch or repo)
        for i, name in enumerate(names):
            when = start + timedelta(seconds=60 * i)
            s.edit(str(repo / name), when)
            self.changed(repo, name, when + timedelta(seconds=1))
        s.text("Done: the tests pass.", start + timedelta(seconds=60 * len(names)))
        return s.write()

    def mixed_repo(self, name: str = "shop") -> Any:
        repo = self.repo(name)
        a = self.work(repo, ["%s-a%d" % (SECRET_FILE, i) for i in range(3)], START_A)
        b = self.work(repo, ["%s-b%d" % (SECRET_FILE, i) for i in range(2)], START_B)
        self.changed(repo, SECRET_FILE + "-loose", NOW - timedelta(minutes=90))  # no session near it
        return repo, a, b


class FoundFromSessionsTests(SharedBase):
    def test_no_config_finds_the_repos_sessions_worked_in(self) -> None:
        elsewhere = self.root / "elsewhere"
        shop = self.repo("shop", parent=elsewhere)
        api = self.repo("api", parent=elsewhere, remote=False)
        self.work(shop, ["old.txt"], NOW - timedelta(days=4))
        Session(self.projects, api).bash(NOW - timedelta(days=2)).text("Done.", NOW - timedelta(days=2)).write()
        text = pull.render_text(self.gather(cfg=self.cfg(workspace_roots=[])))
        self.assertIn("Unsaved work: 2 repos", text)
        self.assertRegex(text, r"! shop · 1 uncommitted since 4 Nov \(4d\)\n")
        self.assertRegex(text, r"! api · no remote\n")

    def test_root_repo_and_a_session_repo_outside_it(self) -> None:
        self.repo("books", remote=False)
        api = self.repo("api", parent=self.root / "elsewhere", remote=False)
        Session(self.projects, api).bash(NOW - timedelta(days=1)).write()
        text = pull.render_text(self.gather())
        self.assertIn("! books · no remote", text)
        self.assertIn("! api · no remote", text)

    def test_deleted_session_repo_is_skipped(self) -> None:
        api = self.repo("api", parent=self.root / "elsewhere", remote=False)
        Session(self.projects, api).bash(NOW - timedelta(days=1)).write()
        shutil.rmtree(str(api))
        rep = self.gather(cfg=self.cfg(workspace_roots=[]))
        self.assertEqual(rep.unsaved, [])

    def test_repo_seen_through_a_symlink_is_listed_once(self) -> None:
        books = self.repo("books", remote=False)
        link = self.root / "books-link"
        link.symlink_to(books)
        Session(self.projects, link).bash(NOW - timedelta(days=1)).write()
        rep = self.gather()
        self.assertEqual([r["repo"] for r in rep.unsaved], [str(books)])

    def test_old_touches_do_not_discover(self) -> None:
        api = self.repo("api", parent=self.root / "elsewhere", remote=False)
        Session(self.projects, api).bash(NOW - timedelta(days=31)).write(mtime=NOW - timedelta(days=29))
        self.assertEqual(self.gather(cfg=self.cfg(workspace_roots=[])).unsaved, [])

    def test_no_scan_uses_the_stored_index(self) -> None:
        api = self.repo("api", parent=self.root / "elsewhere", remote=False)
        Session(self.projects, api).bash(NOW - timedelta(days=1)).write()
        cfg = self.cfg(workspace_roots=[])
        self.assertEqual(self.gather(cfg=cfg, scan=False).unsaved, [])   # never indexed yet
        self.gather(cfg=cfg)
        self.assertEqual(len(self.gather(cfg=cfg, scan=False).unsaved), 1)


class MixedTests(SharedBase):
    def row_lines(self, text: str, project: str) -> List[str]:
        lines = text.splitlines()
        i = next(k for k, line in enumerate(lines) if re.search(r"\d ! %s · " % project, line))
        return lines[i:i + 3]

    def test_two_sessions_fresh_dirt_gets_a_row(self) -> None:
        repo, a, b = self.mixed_repo()
        rep = self.gather()
        text = pull.render_text(rep)
        head, cmd, split = self.row_lines(text, "shop")
        n = re.search(r"(\d+) ! shop", head).group(1)
        self.assertRegex(head, r"! shop · 6 uncommitted since 8 Nov \(0d\) · from 2 sessions \(3 \+ 2, 1 other\)$")
        self.assertEqual(cmd.strip(), "→ cd %s && git status" % repo)
        self.assertEqual(split.strip(), "⚠ split before committing: ghosted mixed %s · cd %s && claude --resume %s · "
                                        "cd %s && claude --resume %s" % (n, repo, a.sid, repo, b.sid))

    def test_one_session_never_mixed(self) -> None:
        repo = self.repo("shop")
        self.work(repo, ["a", "b", "c"], START_A)
        self.assertEqual(self.gather().unsaved, [])          # fresh, one session: nothing to say
        stale = self.repo("old")
        self.work(stale, ["a", "b"], NOW - timedelta(days=5))
        text = pull.render_text(self.gather())
        self.assertRegex(text, r"! old · 2 uncommitted since 3 Nov \(5d\)\n")
        self.assertNotIn("from ", text)
        self.assertNotIn("split before", text)

    def test_other_left_out_when_zero_and_more_than_three_sessions(self) -> None:
        repo = self.repo("shop")
        sids = [self.work(repo, ["f%d" % i], START_A + timedelta(minutes=5 * i)).sid for i in range(4)]
        text = pull.render_text(self.gather())
        head, _, split = self.row_lines(text, "shop")
        self.assertTrue(head.endswith("from 4 sessions (1 + 1 + 1 + 1)"), head)
        self.assertEqual(split.count("claude --resume"), 3)
        self.assertTrue(split.endswith("· +1 more"), split)
        self.assertEqual(len(set(sids)), 4)

    def test_plain_words(self) -> None:
        self.mixed_repo()
        text = pull.render_text(self.gather(), plain=True)
        self.assertIn("changes from 2 different Claude sessions are mixed here; save each one's part separately", text)
        self.assertIn("To split them, paste: ghosted mixed ", text)
        self.assertNotIn("⚠", text)

    def test_json_and_no_file_names_anywhere(self) -> None:
        repo, a, b = self.mixed_repo()
        rep = self.gather()
        doc = json.loads(pull.render_json(rep))
        (row,) = doc["unsaved"]
        self.assertEqual(row["mixed"], {"sessions": {a.sid: 3, b.sid: 2}, "ambiguous": 0, "none": 1})
        self.assertEqual(row["uncommitted"], 6)
        blob = pull.render_text(rep) + pull.render_text(rep, plain=True) + pull.render_json(rep) \
            + json.dumps(Ledger(self.cfg()).items()) + (self.home / "touches.json").read_text()
        self.assertNotIn(SECRET_FILE, blob)
        self.assertTrue(repo.exists())

    def test_ambiguous_when_both_touch_within_the_window(self) -> None:
        repo = self.repo("shop")
        a = Session(self.projects, repo).edit(str(repo / "x"), START_A).write()
        b = Session(self.projects, repo).bash(START_A + timedelta(seconds=10)).write()
        self.changed(repo, "x", START_A + timedelta(seconds=5))
        self.work(repo, ["y"], START_B)
        rep = self.gather()
        self.assertEqual(rep.unsaved, [])   # one owned file + one ambiguous: still one session
        self.assertTrue(a.sid and b.sid)

    def test_dismiss_then_a_third_session_brings_it_back(self) -> None:
        repo, _, _ = self.mixed_repo()
        self.gather()
        led = Ledger(self.cfg())
        (iid,) = led.items()
        led.set_status(iid, "dismissed", now=NOW, dismiss_reason="not_real")
        self.assertEqual(led.get(iid)["dismissed_counts"]["mixed"], 2)
        self.assertEqual(self.gather().unsaved, [])
        self.work(repo, ["third"], NOW - timedelta(minutes=60))
        rep = self.gather()
        self.assertEqual([r["id"] for r in rep.unsaved], [iid])
        self.assertIn("from 3 sessions", pull.render_text(rep))

    def test_window_from_config(self) -> None:
        repo = self.repo("shop")
        a = Session(self.projects, repo).edit(str(repo / "x"), START_A).write()
        self.changed(repo, "x", START_A + timedelta(seconds=20))      # 20 s after A's edit
        self.work(repo, ["y"], START_B)
        self.assertEqual(self.gather().unsaved, [])                   # 15 s: x has no owner
        self.assertEqual(len(self.gather(cfg=self.cfg(mixed_window_seconds=30)).unsaved), 1)
        self.assertTrue(a.sid)

    def test_le_done_still_refused_while_mixed_dirt_is_there(self) -> None:
        self.mixed_repo()
        code, out, err = self.run_cli("report")
        self.assertEqual(code, 0, err)
        n = re.search(r"\n  (\d+) ! shop", out).group(1)
        code, out, err = self.run_cli("done", n)
        self.assertEqual(code, 1)
        self.assertIn("still 6 uncommitted", out + err)


class NewFolderTests(SharedBase):
    """Git lists a new folder as one entry; attribution splits it per file."""

    def test_two_sessions_in_one_new_folder(self) -> None:
        repo = self.repo("shop")
        (repo / "new").mkdir()
        a = self.work(repo, ["new/a.txt"], START_A)
        b = self.work(repo, ["new/b.txt"], START_B)
        code, out, err = self.run_cli("report")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"! shop · 1 uncommitted since 8 Nov \(0d\) · from 2 sessions \(1 \+ 1\)\n")
        n = re.search(r"\n  (\d+) ! shop", out).group(1)
        for s, name in ((a, "new/a.txt"), (b, "new/b.txt")):
            code, files, err = self.run_cli("mixed", n, "--session", s.sid, "--files")
            self.assertEqual((code, files.splitlines()), (0, [name]), err)

    def test_a_folder_too_big_to_split_is_ambiguous(self) -> None:
        repo = self.repo("shop")
        (repo / "big").mkdir()
        with mock.patch.object(unsaved, "SPLIT_FILE_CAP", 2):
            self.work(repo, ["a.txt", "big/1", "big/2", "big/3"], START_A)
            self.work(repo, ["other.txt"], START_B)
            code, out, err = self.run_cli("report")
            n = re.search(r"\n  (\d+) ! shop", out).group(1)
            code, files, _ = self.run_cli("mixed", n, "--session", "ambiguous", "--files")
        self.assertEqual(files.splitlines(), ["big/"])


class LeMixedTests(SharedBase):
    def setup_mixed(self) -> Any:
        repo, a, b = self.mixed_repo()
        code, out, err = self.run_cli("report")
        self.assertEqual(code, 0, err)
        self.assertNotIn(SECRET_FILE, out)
        n = re.search(r"\n  (\d+) ! shop", out).group(1)
        return repo, a, b, n

    def test_counts_without_files(self) -> None:
        _, a, b, n = self.setup_mixed()
        code, out, err = self.run_cli("mixed", n)
        self.assertEqual(code, 0, err)
        self.assertNotIn(SECRET_FILE, out + err)
        lines = out.splitlines()
        self.assertIn("2 sessions", lines[0])
        self.assertRegex(out, r"\n\s+3  %s  cd ~/ws/shop && claude --resume %s\n" % (a.sid, a.sid))
        self.assertRegex(out, r"\n\s+2  %s  cd ~/ws/shop && claude --resume %s\n" % (b.sid, b.sid))
        self.assertRegex(out, r"\n\s+0  ambiguous")
        self.assertRegex(out, r"\n\s+1  none")
        self.assertIn("ghosted mixed %s --session %s --files" % (n, a.sid[:8]), out)
        self.assertIn("timing", out)

    def test_files_for_one_session(self) -> None:
        _, a, b, n = self.setup_mixed()
        code, out, err = self.run_cli("mixed", n, "--session", a.sid[:8], "--files")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(out.splitlines()), ["%s-a%d" % (SECRET_FILE, i) for i in range(3)])
        self.assertIn("timing guess", err)
        code, out, _ = self.run_cli("mixed", n, "--session", b.sid, "--files", "-0")
        self.assertEqual(sorted(x for x in out.split("\0") if x), ["%s-b%d" % (SECRET_FILE, i) for i in range(2)])
        self.assertTrue(out.endswith("\0"))
        self.assertNotIn("\n", out)
        code, out, _ = self.run_cli("mixed", n, "--session", "none", "--files")
        self.assertEqual(out.splitlines(), [SECRET_FILE + "-loose"])
        code, out, _ = self.run_cli("mixed", n, "--session", "ambiguous", "--files")
        self.assertEqual((code, out), (0, ""))

    def test_bad_arguments(self) -> None:
        _, a, _, n = self.setup_mixed()
        self.assertEqual(self.run_cli("mixed", n, "--files")[0], 2)
        self.assertEqual(self.run_cli("mixed", n, "--session", "ffffffff", "--files")[0], 2)
        self.assertEqual(self.run_cli("mixed", "99")[0], 1)
        code, _, _ = self.run_cli("add", "Ship the thing", "--public", "Ship it")
        self.assertEqual(code, 0)
        added = max(int(k[3:]) for k in Ledger(self.cfg()).items())
        self.assertEqual(self.run_cli("mixed", str(added))[0], 1)
        self.assertTrue(a.sid)

    def test_paths_are_relative_with_subfolders(self) -> None:
        repo = self.repo("shop")
        (repo / "src").mkdir()
        (repo / "src" / "keep.py").write_text("before\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "src", when=NOW - timedelta(days=9))
        self.work(repo, ["src/keep.py"], START_A)          # a tracked file, changed
        b = self.work(repo, ["docs/b.md"], START_B)        # a new folder: git lists it as one entry
        code, out, err = self.run_cli("report")
        n = re.search(r"\n  (\d+) ! shop", out).group(1)
        code, out, err = self.run_cli("mixed", n, "--session", b.sid, "--files")
        self.assertEqual((code, out.splitlines()), (0, ["docs/b.md"]), err)   # split into its files
        code, out, err = self.run_cli("mixed", n, "--session", "none", "--files")
        self.assertEqual(out, "")


# ---- step 11 part B: two sessions in one work tree right now -------------------------------------------

LIVE = NOW - timedelta(minutes=10)
ACTIVE_MTIME = NOW - timedelta(minutes=2)


class LiveTests(SharedBase):
    def editor(self, repo: Path, name: str = "a.py", when: datetime = LIVE, launch: Optional[Path] = None) -> Session:
        s = Session(self.projects, launch or repo).edit(str(repo / name), when)
        self.changed(repo, name, when + timedelta(seconds=1))
        return s.write(mtime=ACTIVE_MTIME)

    def line(self, rep: pull.Report, plain: bool = False) -> List[str]:
        return [x for x in pull.render_text(rep, plain=plain).splitlines() if "right now" in x]

    def test_two_editing_sessions_in_one_repo(self) -> None:
        repo = self.repo("shop")
        self.editor(repo, "a.py")
        self.editor(repo, "b.py", when=LIVE + timedelta(minutes=2))
        rep = self.gather()
        self.assertEqual(self.line(rep), ["⚠ 2 sessions are working in shop right now. Give one its own folder: "
                                          "git worktree add ../shop-2"])
        text = pull.render_text(rep)
        self.assertLess(text.index("right now"), text.index("\n\n"))       # in the header block
        doc = json.loads(pull.render_json(rep))
        self.assertEqual(doc["shared_now"], [{"project": "shop", "repo": str(repo), "sessions": 2}])
        self.assertEqual(self.line(rep, plain=True), [
            "2 Claude sessions are changing files in shop right now. To give one its own folder, paste: "
            "git worktree add ../shop-2"])

    def test_bash_only_in_the_root_never_counts(self) -> None:
        run_git(self.ws, "init", "-q", "-b", "main")
        repo = self.repo("shop")
        self.editor(repo)
        # launched from the root, only looked around there (ls / grep), and in shop without changing anything
        Session(self.projects, self.ws).bash(LIVE).bash(LIVE + timedelta(minutes=3), cwd=str(repo)) \
            .write(mtime=ACTIVE_MTIME)
        Session(self.projects, self.ws).bash(LIVE).write(mtime=ACTIVE_MTIME)
        rep = self.gather()
        self.assertEqual(self.line(rep), [])
        self.assertEqual(json.loads(pull.render_json(rep))["shared_now"], [])

    def test_bash_touch_that_lines_up_with_a_changed_file_counts(self) -> None:
        repo = self.repo("shop")
        self.editor(repo)
        Session(self.projects, repo).bash(LIVE + timedelta(minutes=4)).write(mtime=ACTIVE_MTIME)
        self.changed(repo, "generated.json", LIVE + timedelta(minutes=4, seconds=3))  # what that Bash call wrote
        self.assertEqual(len(self.line(self.gather())), 1)

    def test_two_worktrees_of_one_repo_are_two_folders(self) -> None:
        main = self.repo("app")
        wt = self.ws / "app-2"
        run_git(main, "worktree", "add", "-q", str(wt))
        self.editor(main)
        self.editor(wt)
        self.assertEqual(self.line(self.gather()), [])

    def test_old_work_by_an_active_session_does_not_count(self) -> None:
        repo = self.repo("shop")
        self.editor(repo)
        self.editor(repo, "b.py", when=NOW - timedelta(minutes=45))   # the session is open, its edit was earlier
        self.assertEqual(self.line(self.gather()), [])

    def test_sessions_that_ended_are_not_active(self) -> None:
        repo = self.repo("shop")
        self.editor(repo)
        s = self.editor(repo, "b.py")
        s.write(mtime=NOW - timedelta(hours=2))
        self.assertEqual(self.line(self.gather()), [])

    def test_project_filter(self) -> None:
        repo = self.repo("shop")
        self.editor(repo)
        self.editor(repo, "b.py")
        self.assertEqual(self.line(self.gather(project="other")), [])
        self.assertEqual(len(self.line(self.gather(project="shop"))), 1)


if __name__ == "__main__":
    unittest.main()
