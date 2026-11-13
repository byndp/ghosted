"""Tests for core.runner: run.lock, run records, digest rewrites, local pushes and the remote sync.

Adapters are fakes; nothing touches the network. Every test has its own temp GHOSTED_HOME and
projects_dir.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest import mock

from core import detector, ledger, runner, timeutil, transcript
from core.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
TZ = "America/Chicago"
NOW = timeutil.parse_iso("2026-10-06T15:00:00Z")  # Tue 10:00 in Chicago: inside the digest window
OLD = 2 * 86400
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SESSIONS = ("pos_limit_meta.jsonl", "pos_trailing_question.jsonl", "neg_safe_to_clear.jsonl", "neg_qa_session.jsonl")


def fixture_uuid(path: Path) -> str:
    for raw in transcript.split_lines(path.read_bytes()):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
            return entry["sessionId"]
    raise AssertionError(path.name)


class FakeLocal:
    name = "ntfy"
    owns_schedule = False

    def __init__(self, ok: bool = True, raises: Optional[BaseException] = None) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def send(self, items: List[Dict[str, Any]], *, title: str, body: str, kind: str,
             priority: str = "normal", click_url: Optional[str] = None) -> Any:
        self.calls.append({"items": items, "title": title, "body": body, "kind": kind, "priority": priority})
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(ok=self.ok, channel="ntfy", error=None if self.ok else "HTTP 500", status=200 if self.ok else 500)


class FakeRemote:
    name = "remote"
    owns_schedule = True

    def __init__(self, close: Optional[str] = None, raises: Optional[BaseException] = None) -> None:
        self.close = close
        self.raises = raises
        self.calls: List[Any] = []
        self.unacked_runs_at_sync: List[str] = []

    def sync(self, ledger_obj: Any, now: Any, *, test_push: Any = None, pair: Any = None, revoke_devices: bool = False) -> Any:
        self.calls.append(now)
        self.unacked_runs_at_sync = [r["run_id"] for r in ledger_obj.unacked_runs()]
        if self.raises is not None:
            raise self.raises
        if self.close:
            ledger_obj.merge_server_items([{"id": self.close, "status": "done", "rev": 2, "closed_by": "phone",
                                            "closed_at": timeutil.to_iso(now)}], None, now)
        return SimpleNamespace(ok=True, error=None, acks={}, stale=[], merged={}, test_push=None, pair=None)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.home = root / "ghosted-home"
        self.paths: Dict[str, Path] = {}
        for name in SESSIONS:
            uuid = fixture_uuid(FIXTURES / name)
            d = self.projects / ("-p-" + uuid[:8])
            d.mkdir(parents=True)
            p = d / (uuid + ".jsonl")
            shutil.copy(str(FIXTURES / name), str(p))
            t = NOW.timestamp() - OLD
            os.utime(str(p), (t, t))
            self.paths[name] = p
        self.cfg = self.make_cfg()
        self.ledger = ledger.Ledger(self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_cfg(self, **kw: Any) -> Config:
        base: Dict[str, Any] = dict(home=self.home, projects_dir=self.projects, timezone=TZ,
                                    workspace_roots=[Path("/Users/sam/workspace")])
        base.update(kw)
        return Config(**base)


class RunTests(Base):
    def test_run_records_and_writes_the_digest(self) -> None:
        out = runner.run(self.cfg, NOW)
        self.assertFalse(out.skipped)
        self.assertIsNotNone(out.scan)
        self.assertIsNone(out.notify_result)
        rec = out.run
        assert rec is not None
        self.assertRegex(rec["run_id"], UUID_RE)
        self.assertEqual(set(rec), {"run_id", "started_at", "finished_at", "ok", "sessions_scanned", "items_added", "error"})
        self.assertEqual((rec["started_at"], rec["ok"], rec["error"]), (timeutil.to_iso(NOW), True, None))
        self.assertGreaterEqual(timeutil.parse_iso(rec["finished_at"]), NOW)
        self.assertEqual((rec["sessions_scanned"], rec["items_added"]), (4, 2))
        self.assertEqual(self.ledger.runs(), [rec])
        text = self.ledger.digest_path.read_text(encoding="utf-8")
        self.assertEqual(text, out.digest)
        self.assertTrue(text.startswith("# Ghosted · generated Tue Oct 6, 10:00 AM · last run ok · local only"))
        self.assertIn("## Interrupted (2)", text)

    def test_busy_lock_skips_and_records_nothing(self) -> None:
        with runner.run_lock(self.cfg) as held:
            self.assertTrue(held)
            out = runner.run(self.cfg, NOW)
        self.assertTrue(out.skipped)
        self.assertIsNone(out.run)
        self.assertEqual(self.ledger.runs(), [])
        self.assertFalse(self.ledger.digest_path.exists())
        self.assertFalse(self.ledger.state_path.exists())
        with runner.run_lock(self.cfg) as held:  # released again
            self.assertTrue(held)

    def test_scan_failure(self) -> None:
        with mock.patch.object(detector, "scan", side_effect=ledger.LedgerError("state.json is damaged (invalid JSON at line 1, column 2); move it aside")):
            out = runner.run(self.cfg, NOW)
        assert out.run is not None
        self.assertFalse(out.run["ok"])
        self.assertEqual(out.run["error"], "LedgerError: state.json is damaged (invalid JSON at line 1, column 2); move it aside")
        self.assertIn("_scan failed: LedgerError", out.digest or "")
        self.assertIn("last run failed", out.digest or "")
        with mock.patch.object(detector, "scan", side_effect=ValueError("words from a transcript")):
            out = runner.run(self.cfg, NOW + timedelta(minutes=1))
        self.assertEqual(out.run["error"], "ValueError")  # unknown exception types: the name only
        with mock.patch.object(detector, "scan", side_effect=PermissionError(13, "Permission denied", "/x/-Users-me-clientname/a.jsonl")):
            out = runner.run(self.cfg, NOW + timedelta(minutes=2))
        self.assertEqual(out.run["error"], "PermissionError: Permission denied")
        self.assertNotIn("clientname", json.dumps(self.ledger.runs()))

    def test_per_file_errors_do_not_fail_the_run(self) -> None:
        bad = self.paths["neg_qa_session.jsonl"]
        real = transcript.parse_transcript

        def flaky(path: Any, *a: Any, **kw: Any) -> Any:
            if Path(path) == bad:
                raise UnicodeDecodeError("utf-8", b"x", 0, 1, "transcript bytes")
            return real(path, *a, **kw)
        with mock.patch.object(transcript, "parse_transcript", flaky):
            out = runner.run(self.cfg, NOW)
        assert out.run is not None and out.scan is not None
        self.assertEqual((out.run["ok"], out.run["error"]), (True, None))
        self.assertEqual(out.scan.errors, ["UnicodeDecodeError"])
        self.assertIn("_1 session file(s) could not be scanned (UnicodeDecodeError)_", out.digest or "")

    def test_scan_false(self) -> None:
        with mock.patch.object(detector, "scan", side_effect=AssertionError("must not scan")):
            out = runner.run(self.cfg, NOW, scan=False)
        assert out.run is not None
        self.assertEqual((out.run["ok"], out.run["sessions_scanned"], out.scan), (True, 0, None))

    def test_notify_false_never_asks_for_an_adapter(self) -> None:
        cfg = self.make_cfg(adapter="ntfy")
        with mock.patch.object(runner, "_get_adapter", side_effect=AssertionError("no adapter")):
            out = runner.run(cfg, NOW, notify=False)
        self.assertIsNone(out.notify_result)
        self.assertIn("notifications off", out.digest or "")

    def test_snoozes_expire_with_pushes_off(self) -> None:
        item, _ = self.ledger.add("decision", "shop", "pick the logo", "shop: pick the logo",
                                  now=NOW - timedelta(days=3))
        self.ledger.set_status(item["id"], "snoozed", now=NOW - timedelta(days=2), snooze_until="2026-10-06T09:00")
        out = runner.run(self.cfg, NOW)  # adapter none
        self.assertEqual(out.notify_result, {"reopened": [item["id"]], "sent": [], "error": None})
        self.assertEqual(self.ledger.get(item["id"])["status"], "open")
        self.assertNotIn("## Snoozed", out.digest or "")  # re-rendered after the reopen
        self.assertEqual(self.ledger.notification_rows(), [])
        again = runner.run(self.cfg, NOW + timedelta(hours=1))
        self.assertIsNone(again.notify_result)  # nothing to report when nothing expired

    def test_the_remote_adapter_never_reopens_locally(self) -> None:
        cfg = self.make_cfg(adapter="remote")
        led = ledger.Ledger(cfg)
        item, _ = led.add("decision", "shop", "pick the logo", "shop: pick the logo", now=NOW - timedelta(days=3))
        led.set_status(item["id"], "snoozed", now=NOW - timedelta(days=2), snooze_until="2026-10-06T09:00")
        runner.run(cfg, NOW, adapter=None)
        runner.run(cfg, NOW + timedelta(hours=1), adapter=FakeRemote())
        self.assertEqual(led.get(item["id"])["status"], "snoozed")  # the Worker's cron reopens it

    def test_adapter_lookup_failure_is_reported(self) -> None:
        cfg = self.make_cfg(adapter="ntfy")
        with mock.patch.object(runner, "_get_adapter", side_effect=ImportError("no module named notify")):
            out = runner.run(cfg, NOW)
        self.assertEqual(out.notify_result, {"ok": False, "error": "ImportError"})
        assert out.run is not None
        self.assertTrue(out.run["ok"])


class AdapterWiringTests(Base):
    def test_get_adapter_passes_cfg_and_env(self) -> None:
        cfg = self.make_cfg(adapter="pushover")
        ledger.ensure_dir(self.home)
        (self.home / ".env").write_text("PUSHOVER_TOKEN=tok\nPUSHOVER_USER=usr\n", encoding="utf-8")
        seen: List[Any] = []
        fake = types.ModuleType("notify")
        fake.get_adapter = lambda c, env: seen.append((c, env)) or "ADAPTER"  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"notify": fake}):
            self.assertEqual(runner._get_adapter(cfg), "ADAPTER")
        self.assertEqual(seen, [(cfg, {"PUSHOVER_TOKEN": "tok", "PUSHOVER_USER": "usr"})])

    def test_adapter_none_imports_nothing(self) -> None:
        with mock.patch.dict(sys.modules, {"notify": None}):  # importing would raise
            self.assertIsNone(runner._get_adapter(self.make_cfg(adapter="none")))


class LocalPushTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.make_cfg(adapter="ntfy")
        self.ledger = ledger.Ledger(self.cfg)

    def test_digest_push(self) -> None:
        fake = FakeLocal()
        out = runner.run(self.cfg, NOW, adapter=fake)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual((call["title"], call["kind"], call["priority"]), ("Ghosted", "digest", "normal"))
        # Two items, but pos_trailing_question ("Want me to commit these 18 files?") is a save offer
        # outside a git repo: soft, so local pushes leave it out (contract 16.5). The limit is pushed.
        self.assertTrue(call["body"].startswith("1 open: "))
        self.assertEqual([i["title"] for i in call["items"]], ["acme-api: session 10-01 ended on usage limit"])
        for item in call["items"]:
            for key in ("local_title", "reason", "consequential", "synced"):
                self.assertNotIn(key, item)  # a third-party push service gets public fields only
        rows = self.ledger.notifications()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["dedup_key"], row["kind"], row["local_date"], row["ok"], row["channel"], row["error"]),
                         ("digest:2026-10-06", "digest", "2026-10-06", True, "ntfy", None))
        self.assertEqual(row["sent_at"], timeutil.to_iso(NOW))
        self.assertEqual(sorted(row["item_ids"]), sorted(i["id"] for i in call["items"]))
        self.assertEqual(out.notify_result["sent"], rows)
        self.assertIn("push via ntfy", out.digest or "")
        # an hour later the digest has gone out today: nothing more
        runner.run(self.cfg, NOW + timedelta(hours=1), adapter=fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(self.ledger.notifications()), 1)

    def test_failed_send_is_recorded_and_not_retried(self) -> None:
        fake = FakeLocal(raises=RuntimeError("connection reset by a transcript"))
        runner.run(self.cfg, NOW, adapter=fake)
        rows = self.ledger.notifications()
        self.assertEqual([(r["ok"], r["error"]) for r in rows], [(False, "RuntimeError")])
        runner.run(self.cfg, NOW + timedelta(minutes=30), adapter=FakeLocal())
        self.assertEqual(len(self.ledger.notifications()), 1)

    def test_send_that_reports_failure(self) -> None:
        runner.run(self.cfg, NOW, adapter=FakeLocal(ok=False))
        self.assertEqual([(r["ok"], r["error"]) for r in self.ledger.notifications()], [(False, "HTTP 500")])

    def test_quiet_hours_send_nothing(self) -> None:
        fake = FakeLocal()
        quiet = timeutil.parse_iso("2026-10-07T04:00:00Z")  # 23:00 in Chicago
        out = runner.run(self.cfg, quiet, adapter=fake)
        self.assertEqual((fake.calls, self.ledger.notifications()), ([], []))
        self.assertEqual(out.notify_result, {"reopened": [], "sent": [], "error": None})

    def test_due_snooze_reopens_and_goes_in_the_digest(self) -> None:
        item, _ = self.ledger.add("promise", "shop", "renew the domain", "shop: renew the domain", due_at="2026-10-20",
                                  now=NOW - timedelta(days=3))
        self.ledger.set_status(item["id"], "snoozed", now=NOW - timedelta(days=2), snooze_until="2026-10-06T09:00")
        fake = FakeLocal()
        out = runner.run(self.cfg, NOW, adapter=fake)
        self.assertEqual(out.notify_result["reopened"], [item["id"]])
        reopened = self.ledger.get(item["id"])
        self.assertEqual((reopened["status"], reopened["rev"], reopened["snooze_until"]),
                         ("open", 3, "2026-10-06T09:00"))  # expiry keeps snooze_until
        self.assertIn(item["id"], [i["id"] for i in fake.calls[0]["items"]])
        self.assertIn("## Waiting on you (1)", out.digest or "")  # re-rendered after the reopen

    def test_a_snooze_reopened_at_a_quiet_tick_is_in_the_morning_digest(self) -> None:
        # T5's second branch: reopened at 07:30 (quiet hours, nothing sent), still "reopened
        # today" at the 09:15 digest because the expiry kept snooze_until.
        item, _ = self.ledger.add("decision", "shop", "pick the logo", "shop: pick the logo",
                                  review_at="2026-10-30", now=NOW - timedelta(days=3))
        self.ledger.set_status(item["id"], "snoozed", now=NOW - timedelta(days=2), snooze_until="2026-10-06T07:00")
        fake = FakeLocal()
        quiet = runner.run(self.cfg, timeutil.parse_iso("2026-10-06T12:30:00Z"), adapter=fake)  # 07:30
        self.assertEqual((quiet.notify_result["reopened"], fake.calls), ([item["id"]], []))
        runner.run(self.cfg, timeutil.parse_iso("2026-10-06T14:15:00Z"), adapter=fake)  # 09:15
        self.assertEqual(len(fake.calls), 1)
        self.assertIn(item["id"], [i["id"] for i in fake.calls[0]["items"]])

    def test_the_claim_row_is_written_before_the_send(self) -> None:
        seen: List[Any] = []

        class Watching(FakeLocal):
            def send(inner, items: Any, **kw: Any) -> Any:  # noqa: N805
                seen.extend(self.ledger.notification_rows())
                return FakeLocal.send(inner, items, **kw)

        runner.run(self.cfg, NOW, adapter=Watching())
        self.assertEqual([(r["dedup_key"], r["ok"]) for r in seen], [("digest:2026-10-06", None)])
        raw = self.ledger.notification_rows()
        self.assertEqual([r["ok"] for r in raw], [None, True])  # claim, then outcome
        self.assertEqual(raw[1], dict(raw[0], ok=True))
        self.assertEqual([r["ok"] for r in self.ledger.notifications()], [True])

    def test_a_crash_after_the_claim_never_sends_twice(self) -> None:
        class Crash(BaseException):
            pass

        with self.assertRaises(Crash):
            runner.run(self.cfg, NOW, adapter=FakeLocal(raises=Crash()))
        self.assertEqual([(r["dedup_key"], r["ok"]) for r in self.ledger.notifications()],
                         [("digest:2026-10-06", None)])
        fake = FakeLocal()
        runner.run(self.cfg, NOW + timedelta(minutes=30), adapter=fake)
        self.assertEqual(fake.calls, [])  # the claim dedups the day's digest
        with runner.run_lock(self.cfg) as held:
            self.assertTrue(held)  # the crash released run.lock

    def test_decide_error_is_reported(self) -> None:
        with mock.patch.object(runner.schedule, "decide", side_effect=ValueError("bad tz")):
            out = runner.run(self.cfg, NOW, adapter=FakeLocal())
        self.assertEqual(out.notify_result["error"], "ValueError")
        assert out.run is not None
        self.assertTrue(out.run["ok"])


class LocalPushTierTests(unittest.TestCase):
    """runner.local_push only lets waiting interrupted items reach the send rules (contract 16.5)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = Config(home=root / "ghosted-home", projects_dir=root / "projects", timezone=TZ, adapter="ntfy")
        self.led = ledger.Ledger(self.cfg)
        self.n = 0
        self.created = NOW - timedelta(hours=3)

    def interrupted(self, reason: str, git: str = "clean tree", limit: bool = False) -> str:
        self.n += 1
        sid = "cccccccc-0000-4000-8000-%012d" % self.n
        ending = "usage limit" if limit else "an open question"
        title = "widgets: session 10-05 ended on %s" % ending
        ev = "ended on %s · %s" % (ending, git)
        item, _ = self.led.upsert_interrupted(sid, "1:v", "widgets", title, ev, False, None, reason,
                                              observed_at=self.created, now=self.created)
        return item["id"]

    def pushed_ids(self, cfg: Any = None, now: Any = NOW) -> List[str]:
        fake = FakeLocal()
        out = runner.local_push(cfg or self.cfg, self.led, fake, now)
        self.assertIsNone(out["error"])
        return sorted(i for call in fake.calls for i in (it["id"] for it in call["items"]))

    def test_only_waiting_interrupted_items_are_pushed(self) -> None:
        waiting = [
            self.interrupted("limit_meta", limit=True),
            self.interrupted("rate_limit", limit=True),
            self.interrupted("ask_user"),
            self.interrupted("plan_approval"),
            self.interrupted("trailing_q"),
            self.interrupted("save_offer", git="3 uncommitted, 1 unpushed"),
            self.interrupted("save_offer", git="2 unpushed"),
            self.interrupted("save_offer", git="4 uncommitted on round-2, no upstream"),
            self.interrupted("something_new"),  # never hide an unknown
            self.interrupted("offer_q", limit=True),  # a usage limit is waiting whatever the reason
        ]
        for reason, git in (("offer_q", "clean tree"), ("want_me_to", "9 uncommitted"), ("say_the_word", "clean tree"),
                            ("your_call", "clean tree"), ("save_offer", "clean tree"),
                            ("save_offer", "not a git repo"), ("save_offer", "git unavailable")):
            self.interrupted(reason, git=git)
        promise, _ = self.led.add("promise", "widgets", "send the deck", "widgets: promise due 10-06",
                                  due_at="2026-10-06", now=self.created)
        self.assertEqual(self.pushed_ids(), sorted(waiting + [promise["id"]]))

    def test_direct_questions_soft(self) -> None:
        q = self.interrupted("trailing_q")
        lim = self.interrupted("limit_meta", limit=True)
        # The default config: a direct question is waiting.
        self.assertEqual(sorted(runner.push_candidates(self.cfg, self.led.items().values()), key=lambda i: i["id"]),
                         sorted(self.led.items().values(), key=lambda i: i["id"]))
        soft = Config(home=self.cfg.home, projects_dir=self.cfg.projects_dir, timezone=TZ, adapter="ntfy",
                      direct_questions="soft")
        self.assertEqual(self.pushed_ids(soft), [lim])
        self.assertNotIn(q, [i["id"] for i in runner.push_candidates(soft, self.led.items().values())])

    def test_nothing_waiting_sends_nothing(self) -> None:
        self.interrupted("offer_q")
        self.interrupted("save_offer", git="clean tree")
        fake = FakeLocal()
        out = runner.local_push(self.cfg, self.led, fake, NOW)
        self.assertEqual((fake.calls, out["sent"], out["reopened"]), ([], [], []))
        self.assertEqual(self.led.notifications(), [])

    def test_snooze_expiry_still_reopens_soft_items(self) -> None:
        soft = self.interrupted("want_me_to")
        hard = self.interrupted("ask_user")
        for item_id in (soft, hard):
            self.led.set_status(item_id, "snoozed", now=self.created, snooze_until="2026-10-06T09:00")
        fake = FakeLocal()
        out = runner.local_push(self.cfg, self.led, fake, NOW)  # 10:00 Chicago: both snoozes are over
        self.assertEqual(sorted(out["reopened"]), sorted([soft, hard]))
        self.assertEqual(self.led.items()[soft]["status"], "open")
        self.assertEqual(sorted(i["id"] for call in fake.calls for i in call["items"]), [hard])

    def test_snooze_expiry_without_an_adapter(self) -> None:
        soft = self.interrupted("offer_q")
        self.led.set_status(soft, "snoozed", now=self.created, snooze_until="2026-10-06T09:00")
        out = runner.local_push(self.cfg, self.led, None, NOW)
        self.assertEqual(out["reopened"], [soft])

    def test_interrupted_tier(self) -> None:
        limit = {"kind": "interrupted", "title": "a: session 10-05 ended on usage limit", "reason": None}
        question = {"kind": "interrupted", "title": "a: session 10-05 ended on an open question"}
        self.assertEqual(runner.interrupted_tier(self.cfg, limit), "waiting")
        self.assertEqual(runner.interrupted_tier(self.cfg, dict(question, reason="offer_q")), "soft")
        self.assertEqual(runner.interrupted_tier(self.cfg, dict(question, reason=None)), "waiting")
        self.assertEqual(runner.interrupted_tier(self.cfg, dict(question, reason="save_offer",
                                                                evidence="ended on an open question · 1 uncommitted")), "waiting")
        self.assertEqual(runner.interrupted_tier(self.cfg, dict(question, reason="save_offer")), "soft")
        self.assertIsNone(runner.interrupted_tier(self.cfg, {"kind": "promise", "reason": "offer_q"}))
        self.assertEqual(runner.push_candidates(self.cfg, [dict(question, reason="offer_q"), "junk"]), ["junk"])


class RemoteTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.make_cfg(adapter="remote")
        self.ledger = ledger.Ledger(self.cfg)

    def test_sync_after_the_run_record_then_rerender(self) -> None:
        runner.run(self.cfg, NOW - timedelta(hours=1), notify=False)  # creates the items
        target = sorted(self.ledger.items())[0]
        fake = FakeRemote(close=target)
        out = runner.run(self.cfg, NOW, adapter=fake)
        assert out.run is not None
        self.assertEqual(fake.calls, [NOW])
        self.assertIn(out.run["run_id"], fake.unacked_runs_at_sync)  # this run is the heartbeat
        self.assertEqual(out.notify_result.ok, True)
        text = self.ledger.digest_path.read_text(encoding="utf-8")
        self.assertEqual(text, out.digest)
        self.assertIn("· synced", text.splitlines()[0])
        self.assertNotIn(target, text)  # the phone closed it during the sync
        self.assertEqual(self.ledger.notifications(), [])  # the Worker owns the schedule

    def test_sync_failure(self) -> None:
        out = runner.run(self.cfg, NOW, adapter=FakeRemote(raises=OSError(5, "Input/output error")))
        self.assertEqual(out.notify_result, {"ok": False, "error": "OSError: Input/output error"})
        self.assertIn("sync failed: OSError: Input/output error", out.digest or "")
        assert out.run is not None
        self.assertTrue(out.run["ok"])


class SweepTests(Base):
    def test_sweep_scans_and_writes_but_records_no_run(self) -> None:
        text = runner.sweep(self.cfg, NOW)
        self.assertEqual(text, self.ledger.digest_path.read_text(encoding="utf-8"))
        self.assertIn("## Interrupted (2)", text)
        self.assertIn("no run yet", text)
        self.assertEqual(self.ledger.runs(), [])
        runner.run(self.cfg, NOW + timedelta(minutes=5), notify=False)
        text = runner.sweep(self.cfg, NOW + timedelta(hours=1))
        self.assertIn("last run ok (10:05 AM)", text)

    def test_no_scan(self) -> None:
        with mock.patch.object(detector, "scan", side_effect=AssertionError("must not scan")):
            text = runner.sweep(self.cfg, NOW, scan=False)
        self.assertIn("Nothing open.", text)

    def test_busy_returns_the_last_digest_with_a_note(self) -> None:
        with runner.run_lock(self.cfg):
            fresh = runner.sweep(self.cfg, NOW)
        self.assertTrue(fresh.startswith("# Ghosted"))
        self.assertIn("_%s_" % runner.BUSY_NOTE_FRESH, fresh)
        self.assertFalse(self.ledger.digest_path.exists())  # nothing written while busy
        written = runner.sweep(self.cfg, NOW)
        with runner.run_lock(self.cfg):
            again = runner.sweep(self.cfg, NOW + timedelta(minutes=1))
        self.assertEqual(again, "_%s_\n\n%s" % (runner.BUSY_NOTE, written))

    def test_scan_failure_still_renders(self) -> None:
        with mock.patch.object(detector, "scan", side_effect=ledger.LedgerBusy("another ghosted process is holding the ledger lock (waited 10.0 s)")):
            text = runner.sweep(self.cfg, NOW)
        self.assertIn("_scan failed: LedgerBusy: another ghosted process", text)


class WatchdogTests(Base):
    """Contract 16.5 / spec 8: one best-effort GET after an ok scheduled run."""

    SCHEDULED = {"GHOSTED_SCHEDULED": "1"}

    def serve(self) -> Any:
        from tests.fakeserver import FakeServer

        srv = FakeServer().start()
        self.addCleanup(srv.stop)
        return srv

    def test_an_ok_scheduled_run_pings_the_url(self) -> None:
        srv = self.serve()
        cfg = self.make_cfg(watchdog_ping_url=srv.url + "/ping/secret-uuid")
        out = runner.run(cfg, NOW, environ=self.SCHEDULED, adapter=None)
        self.assertEqual([(r.method, r.path) for r in srv.requests], [("GET", "/ping/secret-uuid")])
        self.assertEqual(out.watchdog, {"ok": True, "where": srv.url, "status": 200, "error": None})
        self.assertTrue(out.run["ok"])
        self.assertNotIn("secret-uuid", json.dumps(out.watchdog))
        from core import cli

        self.assertTrue(cli.run_summary(out)[0].endswith(" · watchdog pinged"))

    def test_no_ping_by_hand_without_a_url_or_after_a_failed_or_skipped_run(self) -> None:
        srv = self.serve()
        url = srv.url + "/ping/x"
        self.assertIsNone(runner.run(self.make_cfg(watchdog_ping_url=url), NOW, environ={}, adapter=None).watchdog)
        self.assertIsNone(runner.run(self.make_cfg(), NOW, environ=self.SCHEDULED, adapter=None).watchdog)
        with mock.patch.object(detector, "scan", side_effect=ledger.LedgerError("boom")):
            failed = runner.run(self.make_cfg(watchdog_ping_url=url), NOW, environ=self.SCHEDULED, adapter=None)
        self.assertFalse(failed.run["ok"])
        self.assertIsNone(failed.watchdog)
        cfg = self.make_cfg(watchdog_ping_url=url)
        with runner.run_lock(cfg):
            busy = runner.run(cfg, NOW, environ=self.SCHEDULED, adapter=None)
        self.assertTrue(busy.skipped)
        self.assertIsNone(busy.watchdog)
        self.assertEqual(srv.requests, [])

    def test_a_failed_ping_is_noted_and_never_fails_the_run(self) -> None:
        from tests.fakeserver import Response, closed_port

        srv = self.serve()
        srv.queue(Response(500))
        out = runner.run(self.make_cfg(watchdog_ping_url=srv.url + "/p/secret"), NOW,
                         environ=self.SCHEDULED, adapter=None)
        self.assertTrue(out.run["ok"])
        self.assertEqual(out.watchdog, {"ok": False, "where": srv.url, "status": 500, "error": "HTTP 500"})
        from core import cli

        line, code = cli.run_summary(out)
        self.assertEqual(code, 0)
        self.assertTrue(line.endswith(" · watchdog ping failed: HTTP 500"))
        dead = "http://127.0.0.1:%d/p/secret" % closed_port()
        out = runner.run(self.make_cfg(watchdog_ping_url=dead), NOW, environ=self.SCHEDULED, adapter=None)
        self.assertTrue(out.run["ok"])
        self.assertFalse(out.watchdog["ok"])
        self.assertEqual(out.watchdog["error"], "connection refused")
        self.assertNotIn("secret", json.dumps(out.watchdog))
        self.assertNotIn("secret", json.dumps(self.ledger.runs()))

    def test_the_injected_ping_gets_a_get_with_a_10_s_timeout(self) -> None:
        calls: List[Any] = []

        def ping(url: str, **kw: Any) -> Any:
            calls.append((url, kw))
            return SimpleNamespace(status=204)

        cfg = self.make_cfg(watchdog_ping_url="https://hc-ping.com/abc")
        out = runner.run(cfg, NOW, environ=self.SCHEDULED, adapter=None, ping=ping)
        self.assertEqual(calls, [("https://hc-ping.com/abc", {"method": "GET", "timeout": 10.0, "max_bytes": 4096})])
        self.assertEqual(out.watchdog, {"ok": True, "where": "https://hc-ping.com", "status": 204, "error": None})

    def test_odd_failures_keep_only_the_type(self) -> None:
        def ping(url: str, **kw: Any) -> Any:
            raise RuntimeError("https://hc-ping.com/abc leaked")

        cfg = self.make_cfg(watchdog_ping_url="https://hc-ping.com/abc")
        out = runner.run(cfg, NOW, environ=self.SCHEDULED, adapter=None, ping=ping)
        self.assertEqual(out.watchdog["error"], "RuntimeError")
        plain = runner.watchdog_ping(self.make_cfg(watchdog_ping_url="http://example.com/x"),
                                     runner.RunOutcome(run={"ok": True}), self.SCHEDULED, ping)
        self.assertEqual(plain, {"ok": False, "where": None, "status": None,
                                 "error": "ping_url must be an https:// URL"})

    def test_macos_has_a_sync_label(self) -> None:
        self.assertEqual(runner.default_sync_label(self.make_cfg(adapter="macos")), "push via macos")


class DescribeTests(unittest.TestCase):
    def test_messages(self) -> None:
        self.assertEqual(runner.describe(FileNotFoundError(2, "No such file or directory", "/x/secret-client/a")),
                         "FileNotFoundError: No such file or directory")
        self.assertEqual(runner.describe(ledger.LedgerBusy("held\nfor long")), "LedgerBusy: held for long")
        self.assertEqual(runner.describe(KeyError("transcript text")), "KeyError")
        self.assertEqual(runner.describe(RuntimeError()), "RuntimeError")
        self.assertLessEqual(len(runner.describe(ledger.LedgerError("x" * 500))), len("LedgerError: ") + 120)


if __name__ == "__main__":
    unittest.main()
