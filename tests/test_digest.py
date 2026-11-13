"""Tests for core.digest: grouping, rendering, and the "waiting right now" list."""
from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from core import digest, timeutil
from core.config import Config

TZ = "America/Chicago"
NOW = timeutil.parse_iso("2026-10-06T15:00:00Z")  # Tue Oct 6, 10:00 AM in Chicago
NOW_ISO = timeutil.to_iso(NOW)
SID = "1a2b3c4d-0000-4000-8000-000000000001"


def item(n: int, **kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "GS-%04d" % n, "kind": "promise", "project": "shop", "local_title": "local %d" % n,
        "title": "shop: public %d" % n, "owner": "me", "due_at": None, "review_at": None,
        "status": "open", "snooze_until": None, "dismiss_reason": None, "evidence": None,
        "looks_resolved": False, "resume": None, "session_uuid": None, "rev": 1,
        "created_at": "2026-10-01T15:00:00Z",
    }
    base.update(kw)
    return base


def ids(rows: List[Dict[str, Any]]) -> List[str]:
    return [r["id"] for r in rows]


class GroupTests(unittest.TestCase):
    def test_due_window(self) -> None:
        rows = [
            item(1, due_at="2026-10-08T09:00"),  # 47 h away
            item(2, due_at="2026-10-08T11:00"),  # 49 h away
            item(3, due_at="2026-10-05T09:00"),  # past
            item(4, due_at="2026-10-08"),        # date-only, today + 2
            item(5, due_at="2026-10-09"),        # date-only, today + 3
            item(6, due_at="2026-10-01"),        # date-only, past
            item(7, due_at="2026-10-06"),        # today
            item(8, due_at="not a date"),
        ]
        g = digest.group(rows, NOW, TZ)
        self.assertEqual(ids(g["due"]), ["GS-0006", "GS-0003", "GS-0007", "GS-0004", "GS-0001"])
        self.assertEqual(ids(g["waiting"]), ["GS-0002", "GS-0005", "GS-0008"])

    def test_groups_by_kind_owner_and_status(self) -> None:
        rows = {
            "a": item(1, kind="interrupted", title="x: session 10-01 ended on usage limit"),
            "b": item(2, kind="interrupted", due_at="2026-10-06"),
            "c": item(3, owner="claude", review_at="2026-10-09"),
            "d": item(4, review_at="2026-10-10"),
            "e": item(5, owner=None),
            "f": item(6, status="snoozed", snooze_until="2026-10-09T09:00"),
            "g": item(7, status="snoozed", snooze_until="2026-10-07T09:00"),
            "h": item(8, status="done"),
            "i": item(9, status="dismissed", dismiss_reason="not_real"),
        }
        g = digest.group(rows, NOW, TZ)
        self.assertEqual(ids(g["due"]), ["GS-0002"])
        self.assertEqual(ids(g["interrupted"]), ["GS-0001"])
        self.assertEqual(ids(g["later"]), ["GS-0003"])
        self.assertEqual(ids(g["waiting"]), ["GS-0004", "GS-0005"])
        self.assertEqual(ids(g["snoozed"]), ["GS-0007", "GS-0006"])
        self.assertEqual(set(g), set(digest.GROUPS))

    def test_waiting_order_is_due_then_review(self) -> None:
        rows = [item(1, review_at="2026-10-20"), item(2, due_at="2026-10-30"), item(3, review_at="2026-10-10"),
                item(4), item(5, due_at="2026-10-20T09:00")]
        self.assertEqual(ids(digest.group(rows, NOW, TZ)["waiting"]), ["GS-0005", "GS-0002", "GS-0003", "GS-0001", "GS-0004"])

    def test_odd_input(self) -> None:
        g = digest.group([None, "x", item(1, status=None)], NOW_ISO, TZ)
        self.assertEqual(sum(len(v) for v in g.values()), 0)
        self.assertEqual(digest.group(None, NOW, TZ)["due"], [])


class RenderTests(unittest.TestCase):
    def test_header(self) -> None:
        run = {"ok": True, "started_at": NOW_ISO, "finished_at": NOW_ISO}
        text = digest.render([], NOW, TZ, run=run)
        self.assertEqual(text.splitlines()[0], "# Ghosted · generated Tue Oct 6, 10:00 AM · last run ok · local only")
        earlier = {"ok": 0, "started_at": "2026-10-06T14:02:00Z", "finished_at": "2026-10-06T14:03:00Z"}
        self.assertIn("last run failed (9:03 AM)", digest.render([], NOW, TZ, run=earlier))
        yesterday = {"ok": True, "finished_at": "2026-10-05T23:30:00Z"}
        self.assertIn("last run ok (Mon Oct 5, 6:30 PM)", digest.render([], NOW, TZ, run=yesterday))
        self.assertIn("no run yet", digest.render([], NOW, TZ))

    def test_sync_status(self) -> None:
        self.assertEqual(digest.sync_text(None), "local only")
        self.assertEqual(digest.sync_text("push via ntfy"), "push via ntfy")
        self.assertEqual(digest.sync_text(SimpleNamespace(ok=True, error=None)), "synced")
        self.assertEqual(digest.sync_text({"ok": False, "error": "HTTP 401"}), "sync failed: HTTP 401")
        self.assertEqual(digest.sync_text({"ok": False}), "sync failed")
        self.assertIn("· synced", digest.render([], NOW, TZ, sync={"ok": True}))

    def test_items(self) -> None:
        rows = [
            item(1, due_at="2026-10-01", local_title="pay the overdue invoice\nfor the domain"),
            item(2, due_at="2026-10-06"),
            item(3, due_at="2026-10-08T09:00", local_title=""),
            item(4, kind="interrupted", project="boathouse", title="boathouse: session 10-01 ended on usage limit",
                 local_title="boathouse: session 10-01 ended on usage limit",
                 evidence="ended on usage limit · resumed 10-02, ended clean · 91 uncommitted, 2 unpushed",
                 looks_resolved=True, resume="cd ~/workspace/boathouse && claude --resume " + SID, review_at="2026-10-08"),
            item(5, review_at="2026-10-13"),
            item(6, owner="claude", review_at="2026-10-09"),
            item(7, status="snoozed", snooze_until="2026-10-07T09:00", review_at="2026-10-11"),
        ]
        text = digest.render(rows, NOW, TZ)
        expected = """## Due soon / overdue (3)
- GS-0001 · pay the overdue invoice for the domain · due Thu Oct 1 (overdue)
- GS-0002 · local 2 · due Tue Oct 6 (today)
- GS-0003 · shop: public 3 · due Thu Oct 8, 9:00 AM

## Interrupted (1)
- GS-0004 · boathouse: session 10-01 ended on usage limit
  ended on usage limit · resumed 10-02, ended clean · 91 uncommitted, 2 unpushed
  looks resolved
  `cd ~/workspace/boathouse && claude --resume %s`

## Waiting on you (1)
- GS-0005 · local 5 · review Tue Oct 13

## Also open (owner claude) (1)
- GS-0006 · local 6 · review Fri Oct 9

## Snoozed (1)
- GS-0007 · local 7 · until Wed Oct 7, 9:00 AM · review Sun Oct 11
""" % SID
        self.assertEqual(text.split("\n", 2)[2], expected)

    def test_overdue_timed_and_empty(self) -> None:
        text = digest.render([item(1, due_at="2026-10-06T08:00")], NOW, TZ)
        self.assertIn("due Tue Oct 6, 8:00 AM (overdue)", text)
        self.assertTrue(digest.render([item(2, status="done")], NOW, TZ).rstrip().endswith("Nothing open."))

    def test_notes_and_live(self) -> None:
        live = [{"session_uuid": SID, "project": "boathouse", "verdict": "open_question", "reason": "trailing_q",
                 "at": "2026-10-06T14:41:00Z", "until": None},
                {"session_uuid": "2b3c4d5e-0000-4000-8000-000000000002", "project": "chipmunk", "verdict": "usage_limit",
                 "reason": "rate_limit", "at": "2026-10-05T22:00:00Z", "until": "2026-10-07T10:30:00Z"}]
        text = digest.render([], NOW, TZ, live=live, notes=["2 session file(s) could not be scanned (ValueError)", ""])
        lines = text.splitlines()
        self.assertEqual(lines[2], "_2 session file(s) could not be scanned (ValueError)_")
        self.assertIn("## Waiting right now (2)", lines)
        self.assertIn("- boathouse · session 1a2b3c4d · ended on an open question · 9:41 AM", lines)
        self.assertIn("- chipmunk · session 2b3c4d5e · hit a usage limit · Mon Oct 5, 5:00 PM"
                      " · deferred until Wed Oct 7, 5:30 AM", lines)
        self.assertNotIn("Nothing open.", text)  # live sessions count as something to look at
        self.assertIn("No open items.", text)

    def test_title_fallbacks(self) -> None:
        text = digest.render([item(1, local_title=None, title=None)], NOW, TZ)
        self.assertIn("- GS-0001 · GS-0001", text)


class LiveWaitingTests(unittest.TestCase):
    cfg = Config(home=Path("/nonexistent-ghosted-home"), projects_dir=Path("/nonexistent-projects"))

    def stop(self, sid: str, minutes_ago: float, verdict: str = "open_question", **kw: Any) -> Dict[str, Any]:
        row = {"at": timeutil.to_iso(NOW - timedelta(minutes=minutes_ago)), "session_id": sid, "event": "Stop",
               "verdict": verdict, "reason": "trailing_q", "cwd_project": "proj-" + sid[:1]}
        row.update(kw)
        return row

    def test_rules(self) -> None:
        a, b, c, d, e, f, h = ("a" + "0" * 7 + "-0000-4000-8000-000000000001", "b" * 8 + "-0000-4000-8000-000000000002",
                               "c" * 8 + "-0000-4000-8000-000000000003", "d" * 8 + "-0000-4000-8000-000000000004",
                               "e" * 8 + "-0000-4000-8000-000000000005", "f" * 8 + "-0000-4000-8000-000000000006",
                               "9" * 8 + "-0000-4000-8000-000000000008")
        stops = [
            self.stop(a, 5),                                              # deferred: waiting
            self.stop(b, 50), self.stop(b, 20, verdict="clean"),          # latest is clean
            self.stop(c, 120),                                            # processed after the stop
            self.stop(d, 10, verdict="usage_limit", reason="rate_limit"), # no scan since: waiting
            self.stop(e, 40),                                             # a scan ran past it, not deferred
            self.stop(f, 300, verdict="usage_limit", reason="rate_limit", cwd_project=""),  # limit deferral
            self.stop(h, 20 * 24 * 60),                                   # never seen, too old
            {"at": "garbage", "session_id": a}, {"session_id": None, "at": NOW_ISO}, "junk", None,
        ]
        state = {
            "sessions": {c: {"processed_at": timeutil.to_iso(NOW - timedelta(minutes=60))},
                         f: {"project": "orchard", "processed_at": timeutil.to_iso(NOW - timedelta(days=3))}},
            "deferred": [a, f],
            "deferred_info": {f: {"why": "limit", "until": "2026-10-07T10:30:00Z"}, a: {"why": "active"}},
            "last_scan": timeutil.to_iso(NOW - timedelta(minutes=30)),
        }
        out = digest.live_waiting(stops, state, NOW, self.cfg)
        self.assertEqual([x["session_uuid"] for x in out], [a, d, f])  # newest first
        self.assertEqual(out[0], {"session_uuid": a, "project": "proj-a", "verdict": "open_question", "reason": "trailing_q",
                                  "at": timeutil.to_iso(NOW - timedelta(minutes=5)), "until": None})
        self.assertEqual(out[2]["project"], "orchard")
        self.assertEqual(out[2]["until"], "2026-10-07T10:30:00Z")

    def test_latest_stop_wins_by_time_then_order(self) -> None:
        sid = "1" * 8 + "-0000-4000-8000-000000000001"
        stops = [self.stop(sid, 1, verdict="clean"), self.stop(sid, 10)]  # out of order in the file
        self.assertEqual(digest.live_waiting(stops, {}, NOW, self.cfg), [])
        same = self.stop(sid, 3)
        stops = [same, dict(same, verdict="clean")]
        self.assertEqual(digest.live_waiting(stops, {}, NOW, self.cfg), [])

    def test_empty_inputs(self) -> None:
        self.assertEqual(digest.live_waiting(None, None, NOW, self.cfg), [])
        self.assertEqual(digest.live_waiting([], {"deferred": "x", "sessions": []}, NOW, self.cfg), [])


if __name__ == "__main__":
    unittest.main()
