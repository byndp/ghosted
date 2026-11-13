"""Re-flagging (review finding spec-compliance-1): an interrupted item is one per session, so a
session resumed after its item was closed that stops on a question or a usage limit again must
reopen that item (contract 6). Ledger rule, detector wiring, and the local push rules' T4.

Synthetic data only: invented sessions, projects and titles; temp GHOSTED_HOME per test.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid as uuid_mod
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from core import detector, ledger, runner, schedule, timeutil, transcript
from core.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
TZ = "America/Chicago"
SESSION = "0b0b0b0b-1111-4222-8333-444455556666"
T0 = timeutil.parse_iso("2026-10-06T14:00:00Z")  # the first flagged ending
TITLE = "widgets: session 10-06 ended on an open question"
TITLE2 = "widgets: session 10-07 ended on an open question"
LIMIT2 = "widgets: session 10-07 ended on usage limit"


def iso(dt: Any) -> str:
    return timeutil.to_iso(dt)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = Config(home=self.root / "ghosted-home", projects_dir=self.root / "projects", timezone=TZ,
                          workspace_roots=[self.root / "workspace"])
        self.led = ledger.Ledger(self.cfg)

    def upsert(self, title: str = TITLE, *, observed: Any, ended: Any = None, looks_resolved: bool = False,
               evidence: str = "ended on an open question · clean tree"):
        return self.led.upsert_interrupted(SESSION, "100:abc", "widgets", title, evidence, looks_resolved,
                                           "cd ~/workspace/widgets && claude --resume " + SESSION,
                                           "trailing_q", observed_at=observed, now=observed,
                                           interrupted_at=ended)


class LedgerReflagTests(Case):
    def test_a_newer_flagged_ending_reopens_a_done_item(self) -> None:
        item, ev = self.upsert(observed=T0 + timedelta(minutes=40), ended=T0)
        self.assertEqual(ev["item"]["interrupted_at"], iso(T0))
        self.led.set_status(item["id"], "done", now=T0 + timedelta(hours=2), closed_by="phone")
        later = T0 + timedelta(days=1)
        again, ev2 = self.upsert(TITLE2, observed=later + timedelta(minutes=40), ended=later)
        self.assertEqual((again["status"], again["rev"], again["closed_at"], again["closed_by"]),
                         ("open", 3, None, None))
        self.assertEqual(again["reopened_at"], iso(later + timedelta(minutes=40)))
        self.assertEqual(again["title"], TITLE2)
        self.assertIsNotNone(ev2)
        self.assertEqual(ev2["type"], "item.upsert")
        self.assertEqual(ev2["item"]["interrupted_at"], iso(later))  # the server applies the same rule
        self.assertNotIn("reopened_at", ev2["item"])  # Mac only

    def test_a_dismissed_item_reopens_on_a_new_usage_limit(self) -> None:
        item, _ = self.upsert(observed=T0 + timedelta(minutes=40), ended=T0)
        self.led.set_status(item["id"], "dismissed", dismiss_reason="not_real", now=T0 + timedelta(hours=1))
        later = T0 + timedelta(days=1)
        again, _ = self.upsert(LIMIT2, observed=later + timedelta(hours=1), ended=later)
        self.assertEqual((again["status"], again["dismiss_reason"]), ("open", None))

    def test_the_same_ending_seen_again_after_the_close_stays_done(self) -> None:
        # Metadata appended to the transcript after the close: same final text, same timestamp.
        item, _ = self.upsert(observed=T0 + timedelta(minutes=40), ended=T0)
        self.led.set_status(item["id"], "done", now=T0 + timedelta(hours=2))
        same, _ = self.upsert(observed=T0 + timedelta(hours=3), ended=T0, evidence="ended on an open question · 2 uncommitted")
        self.assertEqual(same["status"], "done")
        self.assertNotIn("reopened_at", same)

    def test_a_done_tapped_after_the_new_ending_stays_done(self) -> None:
        item, _ = self.upsert(observed=T0 + timedelta(minutes=40), ended=T0)
        later = T0 + timedelta(days=1)
        # The phone's Done (merged) came after the new ending happened, before the scan saw it.
        self.led.merge_server_items([{"id": item["id"], "status": "done", "rev": 2, "closed_by": "phone",
                                      "closed_at": iso(later + timedelta(minutes=10))}], 1, later)
        same, _ = self.upsert(TITLE2, observed=later + timedelta(minutes=40), ended=later)
        self.assertEqual(same["status"], "done")

    def test_no_ending_timestamp_never_reopens(self) -> None:
        item, _ = self.upsert(observed=T0 + timedelta(minutes=40), ended=T0)
        self.led.set_status(item["id"], "done", now=T0 + timedelta(hours=2))
        again, _ = self.upsert(TITLE2, observed=T0 + timedelta(days=1), ended=None)
        self.assertEqual(again["status"], "done")

    def test_an_open_item_that_looked_resolved_is_flagged_again(self) -> None:
        self.upsert(observed=T0 + timedelta(minutes=40), ended=T0)
        resolved, _ = self.upsert(observed=T0 + timedelta(hours=5), ended=None, looks_resolved=True,
                                  evidence="ended on an open question · resumed 10-06, ended clean · clean tree")
        self.assertTrue(resolved["looks_resolved"])
        later = T0 + timedelta(days=1)
        again, ev = self.upsert(TITLE2, observed=later + timedelta(minutes=40), ended=later)
        self.assertEqual((again["status"], again["looks_resolved"]), ("open", False))
        self.assertEqual(again["reopened_at"], iso(later + timedelta(minutes=40)))
        self.assertEqual(ev["item"]["interrupted_at"], iso(later))


class LocalT4Tests(Case):
    def test_t4_items_counts_a_reopened_item_as_new_from_its_reopen(self) -> None:
        rows = [{"id": "GS-0001", "kind": "interrupted", "created_at": "2026-10-01T10:00:00Z",
                 "reopened_at": "2026-10-07T15:00:00Z"},
                {"id": "GS-0002", "kind": "promise", "created_at": "2026-10-01T10:00:00Z",
                 "reopened_at": "2026-10-07T15:00:00Z"},
                {"id": "GS-0003", "kind": "interrupted", "created_at": "2026-10-08T10:00:00Z",
                 "reopened_at": "2026-10-07T15:00:00Z"}]
        out = runner.t4_items(rows)
        self.assertEqual([r["created_at"] for r in out],
                         ["2026-10-07T15:00:00Z", "2026-10-01T10:00:00Z", "2026-10-08T10:00:00Z"])
        self.assertEqual(rows[0]["created_at"], "2026-10-01T10:00:00Z")  # never mutated


class FakePush:
    name = "ntfy"
    owns_schedule = False

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def send(self, items: Any, *, title: str, body: str, kind: str, priority: str = "normal",
             click_url: Optional[str] = None) -> Any:
        self.calls.append({"ids": [i["id"] for i in items], "kind": kind})
        return SimpleNamespace(ok=True, channel="ntfy", error=None, status=200)


class DetectorReflagTests(Case):
    """The whole loop: flagged, Done, resumed, a new question, reopened, in the next digest."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = Config(home=self.root / "ghosted-home", projects_dir=self.root / "projects", timezone=TZ,
                          workspace_roots=[self.root / "workspace"], adapter="ntfy")
        self.led = ledger.Ledger(self.cfg)
        src = FIXTURES / "pos_trailing_question.jsonl"
        self.lines = [json.loads(raw) for raw in transcript.split_lines(src.read_bytes())]
        self.sid = next(e["sessionId"] for e in self.lines if isinstance(e.get("sessionId"), str))
        folder = self.cfg.projects_dir / ("-p-" + self.sid[:8])
        folder.mkdir(parents=True)
        self.path = folder / (self.sid + ".jsonl")
        self.write(self.lines, timeutil.parse_iso("2026-10-01T15:00:00Z"))

    def write(self, entries: List[Dict[str, Any]], mtime: Any) -> None:
        self.path.write_bytes(b"".join(json.dumps(e).encode("utf-8") + b"\n" for e in entries))
        t = mtime.timestamp()
        os.utime(str(self.path), (t, t))

    def resumed(self, when: str, question: str) -> List[Dict[str, Any]]:
        """The fixture plus a human turn and a new final question, written at `when`."""
        user = next(e for e in self.lines if e.get("type") == "user" and isinstance((e.get("message") or {}).get("content"), str))
        asst = [e for e in self.lines if e.get("type") == "assistant"][-1]
        human = dict(user, uuid=str(uuid_mod.uuid4()), timestamp=when,
                     message={"role": "user", "content": "go ahead with the first part"})
        reply = json.loads(json.dumps(asst))
        reply.update(uuid=str(uuid_mod.uuid4()), parentUuid=human["uuid"],
                     timestamp=when.replace(":00.000Z", ":30.000Z"))
        reply["message"]["content"] = [{"type": "text", "text": question}]
        return self.lines + [human, reply]

    def test_done_then_resumed_then_a_new_question_reopens_and_is_pushed(self) -> None:
        first = detector.scan(self.cfg, self.led, "2026-10-01T16:00:00Z")
        self.assertEqual(first.items_added, 1)
        item = self.led.find_by_session(self.sid)
        self.led.merge_server_items([{"id": item["id"], "status": "done", "rev": 2, "closed_by": "phone",
                                      "closed_at": "2026-10-02T15:00:00Z"}], 1, "2026-10-02T15:00:00Z")
        # Resumed on 10-07, it stops on a question again (an invented one). A direct choice, so it
        # is waiting (contract 16.1): a save offer outside a git repo would be soft and not pushed.
        self.write(self.resumed("2026-10-07T03:00:00.000Z", "Should I keep the old export, or switch to the new one?"),
                   timeutil.parse_iso("2026-10-07T03:01:00Z"))
        second = detector.scan(self.cfg, self.led, "2026-10-07T04:02:00Z")
        self.assertEqual([f[1] for f in second.flagged], ["open_question"])
        self.assertEqual(second.items_reopened, 1)
        item = self.led.find_by_session(self.sid)
        self.assertEqual((item["status"], item["rev"]), ("open", 3))
        self.assertTrue(item["title"].endswith("ended on an open question"))
        # 09:15 Chicago next morning: the reopened interruption is new since the last digest (T4).
        self.led.append_notification({"dedup_key": "digest:2026-10-06", "kind": "digest", "ok": True,
                                      "sent_at": "2026-10-06T14:15:00Z", "local_date": "2026-10-06",
                                      "item_ids": ["GS-0099"]})
        fake = FakePush()
        out = runner.local_push(self.cfg, self.led, fake, timeutil.parse_iso("2026-10-07T14:15:00Z"))
        self.assertEqual(out["error"], None)
        self.assertEqual(fake.calls, [{"ids": [item["id"]], "kind": "digest"}])

    def test_a_clean_resume_after_done_leaves_it_done(self) -> None:
        detector.scan(self.cfg, self.led, "2026-10-01T16:00:00Z")
        item = self.led.find_by_session(self.sid)
        self.led.set_status(item["id"], "done", now=timeutil.parse_iso("2026-10-02T15:00:00Z"))
        self.write(self.resumed("2026-10-07T03:00:00.000Z", "Pushed to origin. Safe to clear."),
                   timeutil.parse_iso("2026-10-07T03:01:00Z"))
        result = detector.scan(self.cfg, self.led, "2026-10-07T04:02:00Z")
        self.assertEqual(result.items_reopened, 0)
        self.assertEqual(self.led.find_by_session(self.sid)["status"], "done")


if __name__ == "__main__":
    unittest.main()
