"""Two items, one id (review findings python-correctness-2, spec-compliance-9) and a server that
lost items (worker-correctness-11).

A fresh or reset ledger (or a second GHOSTED_HOME on the same Worker) assigns GS-0001 again.
The fixes: a new ledger starts after every id its inbox and acks know, pulls the server's ids
before its first one, and when the server still answers "conflict" the Mac renumbers its item
(never taking the other item's status), drops the old id's queued events and sends the item again.
`ghosted resync` re-sends what a restored database lost.

Synthetic data only; the Worker is the in-memory FakeWorker of tests/test_remote.py (extended
with the conflict rule of notify/remote-worker/src/sync.js).
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from typing import Any, Dict, List
from unittest import mock

from core import cli, ledger as L, runner
from core.ledger import Ledger
from notify.remote import RemoteAdapter
from tests.fakeserver import FakeServer, Request, Response
from tests.test_remote import ENV, NOW, STATUS_FIELDS, FakeWorker, RemoteCase


class ConflictWorker(FakeWorker):
    """FakeWorker plus the Worker's conflict rule: an item.add or item.upsert for an id the server
    holds for another item (another kind or creation time) is answered conflict and never stored,
    and so is every later event of that id in the same request. want.resync is recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.refused: set = set()
        self.resync_requests = 0

    def __call__(self, req: Request) -> Response:
        self.refused = set()
        try:
            if req.json().get("want", {}).get("resync"):
                self.resync_requests += 1
        except ValueError:
            pass
        return super().__call__(req)

    def apply(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        it = ev["item"]
        item_id = it.get("id")
        if ev["event_id"] not in self.seen:
            cur = self.items.get(item_id)
            other = cur is not None and (cur.get("kind") != it.get("kind") or cur.get("created_at") != it.get("created_at")
                                         or (it.get("session_uuid") and cur.get("session_uuid") != it.get("session_uuid")))
            if item_id in self.refused or (ev["type"] in ("item.add", "item.upsert") and other):
                self.refused.add(item_id)
                return {"result": "conflict", "item_id": item_id, "rev": None,
                        "error": "id already belongs to another item"}
        return super().apply(ev)


def server_item(item_id: str, title: str, status: str = "done", created: str = "2026-09-20T15:00:00Z") -> Dict[str, Any]:
    row = dict(STATUS_FIELDS, id=item_id, kind="promise", project="demo", title=title, owner="me",
               due_at=None, review_at=None, created_at=created, updated_at=created)
    if status != "open":
        row.update(status=status, rev=2, closed_by="phone", closed_at="2026-09-21T15:00:00Z")
    return row


class FreshLedgerIdsTests(RemoteCase):
    def test_a_fresh_items_json_starts_after_every_id_the_inbox_and_acks_know(self) -> None:
        for _ in range(3):
            self.add()
        self.sync_ok(self.sync())
        os.replace(str(self.led.items_path), str(self.led.items_path) + ".damaged")  # "move it aside"
        item, _ = self.add()
        self.assertEqual(item["id"], "GS-0004")


class ConflictTests(RemoteCase):
    def setUp(self) -> None:
        super().setUp()
        self.worker = ConflictWorker()
        self.srv.stop()
        self.srv = FakeServer(self.worker).start()
        self.addCleanup(self.srv.stop)
        self.cfg = self.make_cfg(self.srv.url)
        self.led = Ledger(self.cfg)
        self.adapter = RemoteAdapter(self.cfg, ENV, ledger=self.led)
        # Another home already used GS-0001 and GS-0002 on this Worker (a demo item, done).
        self.worker.items["GS-0001"] = server_item("GS-0001", "demo: a stale upload test")
        self.worker.items["GS-0002"] = server_item("GS-0002", "demo: a second test item", status="open")
        self.worker.max_seq = 2

    def test_a_conflict_renumbers_the_item_and_never_copies_the_other_items_status(self) -> None:
        mine, _ = self.add()  # GS-0001 here too: the ledger never synced, the pull was skipped
        self.assertEqual(mine["id"], "GS-0001")
        self.led.set_status("GS-0001", "snoozed", snooze_until="2026-10-07T09:00", now=NOW)
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.renumbered, [("GS-0001", "GS-0003")])
        moved = self.led.get("GS-0003")
        self.assertEqual((moved["title"], moved["status"], moved["synced"]),
                         (mine["title"], "snoozed", True))
        # Sent again under the new id in the same sync: the server has it, snoozed.
        self.assertEqual(self.worker.items["GS-0003"]["title"], mine["title"])
        self.assertEqual(self.worker.items["GS-0003"]["status"], "snoozed")
        # The other home's item is untouched, and now mirrored here under its own id.
        self.assertEqual(self.worker.items["GS-0001"]["status"], "done")
        self.assertEqual(self.worker.items["GS-0001"]["title"], "demo: a stale upload test")
        self.assertEqual(self.led.get("GS-0001")["title"], "demo: a stale upload test")
        self.assertEqual(self.led.unacked_events(), [])
        self.assertEqual(self.led.renumbered(), {"GS-0001": "GS-0003"})

    def test_old_id_status_events_never_reach_the_other_item(self) -> None:
        # 100 events first, so the add (event 100) and the done (event 101) are in different requests.
        filler = []
        for _ in range(98):
            filler.append(self.add())
        self.worker.items["GS-0099"] = server_item("GS-0099", "demo: the colliding one", status="open",
                                                   created="2026-09-01T10:00:00Z")
        self.worker.max_seq = 99
        mine, _ = self.add()
        self.assertEqual(mine["id"], "GS-0099")
        self.add()  # GS-0100, the 100th event
        self.led.set_status("GS-0099", "done", now=NOW)  # event 101: rev 1 -> 2, as the other item's rev
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(self.worker.items["GS-0099"]["status"], "open")  # never undone or closed by us
        new_id = dict(result.renumbered)["GS-0099"]
        self.assertEqual(self.worker.items[new_id]["status"], "done")
        self.assertEqual(self.worker.items[new_id]["title"], mine["title"])

    def test_le_done_with_the_old_id_finds_the_renumbered_item(self) -> None:
        self.add()
        self.sync_ok(self.sync())
        env = dict(os.environ, GHOSTED_HOME=str(self.cfg.home), GHOSTED_NOW="2026-10-05T16:00:00Z")
        (self.cfg.home / "config.toml").write_text('timezone = "America/Chicago"\n')
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["done", "GS-0001"], environ=env)
        self.assertEqual(code, 0)
        self.assertIn("GS-0001 is now GS-0003", out.getvalue())
        self.assertEqual(Ledger(self.cfg).get("GS-0003")["status"], "done")
        self.assertEqual(Ledger(self.cfg).get("GS-0001")["status"], "done")  # the other item, unchanged
        self.assertEqual(Ledger(self.cfg).get("GS-0001")["closed_by"], "phone")

    def test_an_old_worker_duplicate_ack_is_caught_by_the_merge(self) -> None:
        # A Worker without the conflict rule acks the colliding add as a duplicate; the merge sees
        # another creation time under that id and renumbers instead of copying "done".
        self.worker.refused = set()
        with mock.patch.object(ConflictWorker, "apply", FakeWorker.apply):
            mine, _ = self.add()
            result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.renumbered, [("GS-0001", "GS-0003")])
        self.assertEqual(self.led.get("GS-0003")["status"], "open")
        self.sync_ok(self.sync())  # the re-sent creation lands
        self.assertEqual(self.worker.items["GS-0003"]["title"], mine["title"])

    def test_a_new_ledger_pulls_the_servers_ids_before_its_first_one(self) -> None:
        self.assertFalse(self.led.has_server_view())
        self.assertTrue(runner.ensure_server_view(self.cfg, self.led, self.adapter, NOW, timeout=5))
        self.assertTrue(self.led.has_server_view())
        bodies = self.bodies()
        self.assertEqual((len(bodies), bodies[0]["events"], bodies[0]["runs"]), (1, [], []))
        item, _ = self.add()
        self.assertEqual(item["id"], "GS-0003")
        # Once it has the view it never pulls again.
        self.assertTrue(runner.ensure_server_view(self.cfg, self.led, self.adapter, NOW))
        self.assertEqual(len(self.bodies()), 1)

    def test_le_add_on_a_never_synced_ledger_pulls_first(self) -> None:
        home = self.cfg.home
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('timezone = "America/Chicago"\n[notify]\nadapter = "remote"\n'
                                          '[notify.remote]\nurl = "%s"\n' % self.srv.url)
        (home / ".env").write_text("".join("%s=%s\n" % kv for kv in ENV.items()))
        env = dict(os.environ, GHOSTED_HOME=str(home), GHOSTED_NOW="2026-10-05T16:00:00Z")
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["add", "local: renew it", "--public", "shop: renew the certificate",
                             "--project", "shop", "--due", "10-09"], environ=env)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("GS-0003 added"))

    def test_an_unreachable_server_does_not_stop_le_add(self) -> None:
        self.srv.stop()
        self.assertFalse(runner.ensure_server_view(self.cfg, self.led, self.adapter, NOW, timeout=1))
        item, _ = self.add()
        self.assertEqual(item["id"], "GS-0001")  # local ids; a conflict would renumber it later


class ResyncTests(RemoteCase):
    def test_items_the_server_lost_are_listed_and_resync_sends_them_again(self) -> None:
        a, _ = self.add()
        b, _ = self.add()
        self.led.set_status(b["id"], "done", now=NOW)
        self.sync_ok(self.sync())
        self.worker.items.clear()  # a D1 time-travel restore to before they existed
        lost = self.sync()
        self.sync_ok(lost)
        self.assertEqual(lost.missing, [a["id"], b["id"]])
        events = self.led.reemit_events(lost.missing, NOW, force=True)
        self.assertEqual([(e["type"], e["item"]["id"]) for e in events],
                         [("item.add", a["id"]), ("item.add", b["id"]), ("item.status", b["id"])])
        again = self.adapter.sync(self.led, NOW + timedelta(minutes=5), resync=True)
        self.sync_ok(again)
        self.assertTrue(all(body["want"].get("resync") for body in self.bodies()[-1:]))
        self.assertEqual(again.missing, [])
        self.assertEqual(self.worker.items[b["id"]]["status"], "done")

    def test_le_resync_command(self) -> None:
        a, _ = self.add()
        self.sync_ok(self.sync())
        self.worker.items.clear()
        home = self.cfg.home
        (home / "config.toml").write_text('timezone = "America/Chicago"\n[notify]\nadapter = "remote"\n'
                                          '[notify.remote]\nurl = "%s"\n' % self.srv.url)
        (home / ".env").write_text("".join("%s=%s\n" % kv for kv in ENV.items()))
        env = dict(os.environ, GHOSTED_HOME=str(home), GHOSTED_NOW="2026-10-05T16:00:00Z")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["resync"], environ=env)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("the server has them now", out.getvalue())
        self.assertIn(a["id"], self.worker.items)


if __name__ == "__main__":
    import unittest

    unittest.main()
