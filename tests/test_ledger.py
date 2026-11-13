from __future__ import annotations

import collections
import fcntl
import json
import multiprocessing
import os
import stat
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from core import ledger as L
from core import timeutil
from core.config import Config
from core.ledger import Ledger, LedgerBusy, LedgerError, UnknownItem, public_fields
from core.sanitize import MSG_EMAIL, Refused

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # Monday 10:00 in Chicago
SESSION = "0a0a0a0a-1111-4222-8333-444455556666"
SESSION_2 = "0b0b0b0b-1111-4222-8333-444455556666"
LIMIT_TITLE = "boathouse: session 10-01 ended on usage limit"
QUESTION_TITLE = "chipmunk-tinta: session 10-02 ended on an open question"
RESUME = "cd ~/workspace/boathouse && claude --resume " + SESSION
PER_WORKER = 10


def make_cfg(home: str, tz: str = "America/Chicago", review_days: int = 7) -> Config:
    root = Path(home)
    return Config(home=root, timezone=tz, review_days=review_days, projects_dir=root / "projects")


# ---- worker processes (module level so the spawn start method can import them) ----------------

def _add_worker(home: str, worker_no: int, count: int, go) -> None:
    go.wait(60)
    led = Ledger(make_cfg(home))
    for n in range(count):
        led.add("promise", "proj%d" % worker_no, "local w%d n%d" % (worker_no, n),
                "proj%d: public item %d" % (worker_no, n), now=NOW)


def _claim_worker(home: str, keys, go, results) -> None:
    go.wait(60)
    led = Ledger(make_cfg(home))
    results.put([key for key in keys if led.claim_dedup(key, now=NOW)])


def _hold_lock_worker(home: str, acquired, release) -> None:
    led = Ledger(make_cfg(home))
    with led.lock():
        acquired.set()
        release.wait(60)


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "ghosted-home"  # created by the ledger itself
        self.cfg = make_cfg(str(self.home))
        self.led = Ledger(self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def inbox_lines(self):
        if not self.led.inbox_path.exists():
            return []
        return [line for line in self.led.inbox_path.read_bytes().split(b"\n") if line]

    def add_promise(self, local="check the readout script cap and end date",
                    title="metronome: readout script cap/end date", **kw):
        kw.setdefault("now", NOW)
        return self.led.add("promise", "metronome", local, title, **kw)

    def upsert(self, evidence="ended on usage limit · 91 uncommitted, 2 unpushed",
               observed=NOW, session=SESSION, title=LIMIT_TITLE, looks_resolved=False,
               version="1000:abc", project="boathouse"):
        return self.led.upsert_interrupted(session, version, project, title, evidence,
                                           looks_resolved, RESUME, "limit_meta",
                                           observed_at=observed, now=observed)


class TestAdd(LedgerCase):
    def test_sequential_ids_and_exactly_one_event_each(self):
        item, ev = self.add_promise(due_at="2026-10-11")
        item2, ev2 = self.led.add("decision", "metronome", "tooling audit: read the answers",
                                  "shop: tooling audit waits on you", now=NOW)
        self.assertEqual((item["id"], item2["id"]), ("GS-0001", "GS-0002"))
        self.assertEqual([e["event_id"] for e in self.led.events()], [ev["event_id"], ev2["event_id"]])
        self.assertEqual(len(self.inbox_lines()), 2)
        self.assertEqual(uuid.UUID(ev["event_id"]).version, 4)
        self.assertEqual(ev["type"], "item.add")
        self.assertEqual(ev["at"], "2026-10-05T15:00:00Z")
        self.assertEqual(set(ev["item"]), set(L.ADD_EVENT_KEYS))
        self.assertEqual(ev["item"]["due_at"], "2026-10-11")
        self.assertIsNone(ev["item"]["review_at"])
        doc = json.loads(self.led.items_path.read_text())
        self.assertEqual((doc["version"], doc["next_id"]), (1, 3))

    def test_owner_is_me_or_claude(self):
        # contract 16.14: the user is "me".
        self.assertEqual(L.OWNERS, ("me", "claude"))
        mine, _ = self.led.add("decision", "home", "pick the plan", "home: pick the plan", owner="me")
        self.assertEqual(mine["owner"], "me")
        theirs, _ = self.led.add("decision", "home", "run the check", "home: run the check", owner="claude")
        self.assertEqual(theirs["owner"], "claude")
        with self.assertRaises(L.LedgerError) as ctx:
            self.led.add("decision", "home", "x", "home: x", owner="sam")
        self.assertIn("me or claude", str(ctx.exception))
        self.assertEqual({it["owner"] for it in self.led.items().values()}, {"me", "claude"})

    def test_new_item_fields(self):
        item, _ = self.add_promise(due_at="2026-10-11", consequential=True)
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["rev"], 1)
        self.assertEqual(item["owner"], "me")
        self.assertEqual(item["kind"], "promise")
        self.assertEqual(item["local_title"], "check the readout script cap and end date")
        self.assertTrue(item["consequential"])
        self.assertFalse(item["synced"])
        self.assertEqual(item["created_at"], "2026-10-05T15:00:00Z")
        self.assertIsNone(item["closed_at"])
        self.assertEqual(self.led.get("GS-0001"), item)
        self.assertEqual(self.led.get("1"), item)

    def test_review_at_is_local_today_plus_review_days(self):
        late = datetime(2026, 10, 6, 3, 0, tzinfo=UTC)  # still Oct 5 (22:00) in Chicago
        item, ev = self.led.add("decision", "home", "money talk follow-up",
                                "home: follow up on the 09-12 budget call", now=late)
        self.assertEqual(item["review_at"], "2026-10-12")
        self.assertEqual(ev["item"]["review_at"], "2026-10-12")
        utc_led = Ledger(make_cfg(str(self.home), tz="UTC"))
        item_utc, _ = utc_led.add("decision", "home", "x", "home: another decision", now=late)
        self.assertEqual(item_utc["review_at"], "2026-10-13")

    def test_review_at_rules(self):
        dated, _ = self.add_promise(due_at="2026-10-11")
        self.assertIsNone(dated["review_at"])
        explicit, _ = self.add_promise(review_at="2026-10-20", title="metronome: a second promise")
        self.assertEqual(explicit["review_at"], "2026-10-20")
        short = Ledger(make_cfg(str(self.home), review_days=3))
        item, _ = short.add("decision", "home", "x", "home: three-day review", now=NOW)
        self.assertEqual(item["review_at"], "2026-10-08")

    def test_refusals_write_nothing(self):
        with self.assertRaises(Refused):
            self.add_promise(title="home: pay the $480 hosting bill")
        with self.assertRaises(LedgerError):
            self.led.add("interrupted", "boathouse", "x", LIMIT_TITLE, now=NOW)
        with self.assertRaises(LedgerError):
            self.led.add("promise", "home", "x", "home: ok title", owner="bob", now=NOW)
        with self.assertRaises(LedgerError):
            self.led.add("promise", "home", "  ", "home: ok title", now=NOW)
        with self.assertRaises(LedgerError):
            self.led.add("promise", "", "x", "home: ok title", now=NOW)
        with self.assertRaises(ValueError):
            self.add_promise(due_at="10-11")  # stored dues are already normalized
        with self.assertRaises(ValueError):
            self.add_promise(review_at="2026-10-20T09:00")
        self.assertFalse(self.home.exists())


class TestPrivacy(LedgerCase):
    def test_inbox_never_contains_a_local_title(self):
        marker = "LOCAL-MARKER-" + uuid.uuid4().hex
        local = "pay Bob Example $480 for the hosting, ref %s" % marker
        item, _ = self.led.add("promise", "home", local, "home: pay the overdue water bill",
                               due_at="2026-10-09", now=NOW)
        self.led.set_status(item["id"], "snoozed", now=NOW, snooze_until="2026-10-06T09:00")
        self.led.set_status(item["id"], "open", now=NOW)
        self.led.set_status(item["id"], "done", now=NOW)
        self.upsert()
        self.led.merge_server_items([dict(public_fields(item), status="open", rev=5)], max_seq=3, now=NOW)
        data = self.led.inbox_path.read_bytes()
        self.assertNotIn(marker.encode(), data)
        self.assertNotIn(b"Bob Example", data)
        for key in (b'"local_title"', b'"reason"', b'"consequential"', b'"synced"'):
            self.assertNotIn(key, data)
        self.assertIn(marker.encode(), self.led.items_path.read_bytes())  # it is kept locally

    def test_observed_at_only_on_interrupted_events(self):
        _, add_ev = self.add_promise()
        _, status_ev = self.led.set_status("GS-0001", "done", now=NOW)
        _, upsert_ev = self.upsert()
        self.assertNotIn("observed_at", add_ev["item"])
        self.assertNotIn("observed_at", status_ev["item"])
        self.assertEqual(upsert_ev["item"]["observed_at"], "2026-10-05T15:00:00Z")

    def test_public_fields_is_a_whitelist(self):
        item = {k: "v" for k in L.PUBLIC_KEYS}
        item.update(kind="promise", local_title="secret", reason="trailing_q", consequential=True,
                    synced=True, observed_at="2026-10-05T15:00:00Z", notes="a future local field")
        pub = public_fields(item)
        for key in L.MAC_ONLY_FIELDS + ("observed_at", "notes"):
            self.assertNotIn(key, pub)
        self.assertEqual(set(pub), set(L.PUBLIC_KEYS))
        item["kind"] = "interrupted"
        self.assertEqual(public_fields(item)["observed_at"], "2026-10-05T15:00:00Z")
        pub["title"] = "changed"
        self.assertEqual(item["title"], "v")  # a copy, not a view


class TestSetStatus(LedgerCase):
    def test_transitions_bump_rev_and_carry_the_pre_change_rev(self):
        self.add_promise()
        t1 = NOW + timedelta(hours=1)
        done, ev = self.led.set_status("GS-0001", "done", now=t1)
        self.assertEqual((done["rev"], ev["item"]["rev"]), (2, 1))
        self.assertEqual(ev["type"], "item.status")
        self.assertEqual(set(ev["item"]), set(L.STATUS_EVENT_KEYS))
        self.assertEqual(done["closed_at"], timeutil.to_iso(t1))
        self.assertEqual((done["closed_by"], ev["item"]["closed_by"]), ("session", "session"))
        self.assertEqual(done["updated_at"], timeutil.to_iso(t1))

        reopened, ev2 = self.led.set_status("gs-1", "open", now=t1)
        self.assertEqual((reopened["rev"], ev2["item"]["rev"]), (3, 2))
        self.assertIsNone(reopened["closed_at"])
        self.assertIsNone(reopened["closed_by"])

        snoozed, ev3 = self.led.set_status(1, "snoozed", now=t1, snooze_until="2026-10-06T09:00")
        self.assertEqual((snoozed["rev"], ev3["item"]["rev"]), (4, 3))
        self.assertEqual(ev3["item"]["snooze_until"], "2026-10-06T09:00")

        dismissed, ev4 = self.led.set_status("GS-0001", "dismissed", now=t1,
                                             dismiss_reason="not_real", closed_by="phone")
        self.assertIsNone(dismissed["snooze_until"])
        self.assertEqual((dismissed["dismiss_reason"], dismissed["closed_by"]), ("not_real", "phone"))
        self.assertEqual(ev4["item"]["dismiss_reason"], "not_real")

        back, _ = self.led.set_status("GS-0001", "open", now=t1)
        self.assertEqual(back["rev"], 6)
        for key in ("snooze_until", "dismiss_reason", "closed_at", "closed_by"):
            self.assertIsNone(back[key])
        self.assertEqual(len(self.inbox_lines()), 6)  # one add + five status events

    def test_same_status_is_a_no_op(self):
        self.add_promise()
        self.led.set_status("GS-0001", "done", now=NOW)
        again, ev = self.led.set_status("GS-0001", "done", now=NOW + timedelta(minutes=5))
        self.assertIsNone(ev)
        self.assertEqual(again["rev"], 2)
        self.assertEqual(len(self.inbox_lines()), 2)

    def test_a_resnooze_to_another_time_is_a_change(self):
        # The Worker's rule: snoozed -> snoozed with another snooze_until needs the current rev
        # and bumps it; the same snooze_until is a duplicate.
        self.add_promise()
        first, ev1 = self.led.set_status("GS-0001", "snoozed", now=NOW, snooze_until="2026-10-06T09:00")
        same, none = self.led.set_status("GS-0001", "snoozed", now=NOW, snooze_until="2026-10-06T09:00")
        self.assertIsNone(none)
        self.assertEqual(same["rev"], 2)
        later, ev2 = self.led.set_status("GS-0001", "snoozed", now=NOW + timedelta(minutes=1),
                                         snooze_until="2026-10-12T09:00")
        self.assertEqual((later["status"], later["snooze_until"], later["rev"]),
                         ("snoozed", "2026-10-12T09:00", 3))
        self.assertEqual((ev1["item"]["rev"], ev2["item"]["rev"]), (1, 2))
        self.assertEqual((ev2["item"]["status"], ev2["item"]["snooze_until"]),
                         ("snoozed", "2026-10-12T09:00"))
        self.assertEqual(len(self.inbox_lines()), 3)  # add + two snoozes, no reopen in between

    def test_a_corrected_dismiss_reason_is_a_change(self):
        self.add_promise()
        _, ev1 = self.led.set_status("GS-0001", "dismissed", now=NOW, dismiss_reason="not_real")
        _, none = self.led.set_status("GS-0001", "dismissed", now=NOW, dismiss_reason="not_real")
        self.assertIsNone(none)
        later = NOW + timedelta(hours=2)
        item, ev2 = self.led.set_status("GS-0001", "dismissed", now=later,
                                        dismiss_reason="handled_elsewhere")
        self.assertEqual((item["dismiss_reason"], item["rev"], item["closed_at"]),
                         ("handled_elsewhere", 3, timeutil.to_iso(later)))
        self.assertEqual((ev2["item"]["rev"], ev2["item"]["dismiss_reason"]), (2, "handled_elsewhere"))

    def test_a_status_event_is_queued_before_the_item_is_saved(self):
        # A save lost after the event still reaches the server, and the merge brings the Mac in
        # line; the other order would let the next merge undo the change.
        self.add_promise()
        with mock.patch.object(Ledger, "_save_doc", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.led.set_status("GS-0001", "done", now=NOW)
        self.assertEqual(self.led.get("GS-0001")["status"], "open")
        status = [e for e in self.led.events() if e["type"] == "item.status"]
        self.assertEqual([(e["item"]["status"], e["item"]["rev"]) for e in status], [("done", 1)])

    def test_project_names_are_sanitized_too(self):
        with self.assertRaises(Refused) as ctx:
            self.led.add("promise", "bob@example.com", "x", "home: ok title", now=NOW)
        self.assertEqual(ctx.exception.problems, ["the project name " + MSG_EMAIL])
        with self.assertRaises(Refused):
            self.led.add("promise", "p" * 101, "x", "home: ok title", now=NOW)
        self.assertFalse(self.home.exists())

    def test_validation(self):
        self.add_promise()
        bad_calls = [
            dict(status="snoozed"),
            dict(status="dismissed"),
            dict(status="dismissed", dismiss_reason="meh"),
            dict(status="closed"),
            dict(status="done", snooze_until="2026-10-06T09:00"),
            dict(status="open", dismiss_reason="not_real"),
            dict(status="done", closed_by="robot"),
        ]
        for kwargs in bad_calls:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(LedgerError):
                    self.led.set_status("GS-0001", now=NOW, **kwargs)
        with self.assertRaises(ValueError):
            self.led.set_status("GS-0001", "snoozed", now=NOW, snooze_until="tomorrow")
        with self.assertRaises(UnknownItem):
            self.led.set_status("GS-0099", "done", now=NOW)
        with self.assertRaises(LedgerError):
            self.led.set_status("not-an-id", "done", now=NOW)
        self.assertEqual(self.led.get("GS-0001")["rev"], 1)
        self.assertEqual(len(self.inbox_lines()), 1)

    def test_open_items(self):
        for n in range(4):
            self.add_promise(title="metronome: item %d" % n)
        self.led.set_status("GS-0002", "done", now=NOW)
        self.led.set_status("GS-0003", "snoozed", now=NOW, snooze_until="2026-10-06T09:00")
        self.led.set_status("GS-0004", "dismissed", now=NOW, dismiss_reason="handled_elsewhere")
        self.assertEqual([it["id"] for it in self.led.open_items()], ["GS-0001", "GS-0003"])


class TestReopenExpired(LedgerCase):
    def snooze_three(self):
        for n in range(1, 4):
            self.add_promise(title="metronome: item %d" % n)
        self.led.set_status("GS-0001", "snoozed", now=NOW, snooze_until="2026-10-05T09:00")  # passed
        self.led.set_status("GS-0002", "snoozed", now=NOW, snooze_until="2026-10-06T09:00")  # future
        self.led.set_status("GS-0003", "snoozed", now=NOW, snooze_until="2026-10-05T10:00")  # == now

    def test_expiry_keeps_snooze_until_and_bumps_rev(self):
        self.snooze_three()
        later = NOW + timedelta(minutes=1)
        out = self.led.reopen_expired(None, now=later)
        self.assertEqual([item["id"] for item, _ in out], ["GS-0001", "GS-0003"])
        item = self.led.get("GS-0001")
        self.assertEqual((item["status"], item["rev"], item["snooze_until"], item["updated_at"]),
                         ("open", 3, "2026-10-05T09:00", timeutil.to_iso(later)))
        _, event = out[0]
        self.assertEqual(event["type"], "item.status")
        self.assertEqual({k: event["item"][k] for k in ("id", "status", "rev", "snooze_until")},
                         {"id": "GS-0001", "status": "open", "rev": 2, "snooze_until": "2026-10-05T09:00"})
        self.assertEqual(self.led.get("GS-0002")["status"], "snoozed")
        self.assertEqual(self.led.reopen_expired(None, now=later), [])  # nothing left to reopen
        self.assertEqual(len([e for e in self.led.events() if e["type"] == "item.status"]), 5)

    def test_ids_limit_it_and_each_item_is_checked_again(self):
        self.snooze_three()
        out = self.led.reopen_expired(["GS-0003", "2", "junk", None], now=NOW)
        self.assertEqual([item["id"] for item, _ in out], ["GS-0003"])  # GS-0002 is not due yet
        self.assertEqual(self.led.get("GS-0001")["status"], "snoozed")

    def test_a_manual_reopen_clears_snooze_until(self):
        self.snooze_three()
        self.led.reopen_expired(["GS-0001"], now=NOW)
        manual, ev = self.led.set_status("GS-0002", "open", now=NOW)
        self.assertIsNone(manual["snooze_until"])
        self.assertIsNone(ev["item"]["snooze_until"])
        self.assertEqual(self.led.get("GS-0001")["snooze_until"], "2026-10-05T09:00")

    def test_wall_times_follow_the_ledger_timezone(self):
        self.add_promise()
        self.led.set_status("GS-0001", "snoozed", now=NOW, snooze_until="2026-10-05T14:30")
        # 14:30 is still ahead in Chicago (it is 10:00 there) but already past in UTC (15:00)
        self.assertEqual(self.led.reopen_expired(None, now=NOW), [])
        utc_led = Ledger(make_cfg(str(self.home), tz="UTC"))
        self.assertEqual([i["id"] for i, _ in utc_led.reopen_expired(None, now=NOW)], ["GS-0001"])


class TestMissingEvents(LedgerCase):
    def crash_before_the_event(self, call):
        with mock.patch.object(Ledger, "_append_event", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                call()

    def test_a_crash_between_the_save_and_the_event_is_found_and_fixed(self):
        self.add_promise()
        self.crash_before_the_event(lambda: self.add_promise(title="metronome: lost"))
        self.crash_before_the_event(lambda: self.upsert())
        self.assertEqual(sorted(self.led.items()), ["GS-0001", "GS-0002", "GS-0003"])
        self.assertEqual(self.led.items_missing_events(), ["GS-0002", "GS-0003"])
        self.led.set_status("GS-0002", "done", now=NOW)  # its status event is queued meanwhile
        later = NOW + timedelta(hours=1)
        events = self.led.reemit_events(["GS-0003", "gs-2", "GS-0001", "junk"], now=later)
        self.assertEqual([(e["type"], e["item"]["id"]) for e in events],
                         [("item.add", "GS-0002"), ("item.status", "GS-0002"), ("item.upsert", "GS-0003")])
        self.assertEqual(set(events[0]["item"]), set(L.ADD_EVENT_KEYS))
        self.assertEqual(set(events[2]["item"]), set(L.UPSERT_EVENT_KEYS))
        self.assertEqual((events[1]["item"]["status"], events[1]["item"]["rev"]), ("done", 1))
        self.assertTrue(all(e["at"] == timeutil.to_iso(later) for e in events))
        self.assertEqual(self.led.items_missing_events(), [])
        self.assertEqual(self.led.reemit_events(["GS-0002"], now=later), [])  # nothing twice
        data = self.led.inbox_path.read_bytes()
        self.assertNotIn(b'"local_title"', data)

    def test_synced_items_are_never_listed(self):
        self.led.merge_server_items([{"id": "GS-0005", "kind": "promise", "project": "home",
                                      "title": "home: from the phone", "status": "open", "rev": 1}],
                                    max_seq=5, now=NOW)
        self.assertEqual(self.led.items_missing_events(), [])


class TestUpsertInterrupted(LedgerCase):
    def test_insert_then_refresh(self):
        item, ev = self.upsert()
        self.assertEqual(item["id"], "GS-0001")
        self.assertEqual((item["kind"], item["owner"], item["status"], item["rev"]),
                         ("interrupted", "me", "open", 1))
        self.assertEqual(item["review_at"], "2026-10-12")
        self.assertEqual(item["local_title"], LIMIT_TITLE)
        self.assertEqual(item["reason"], "limit_meta")
        self.assertEqual(ev["type"], "item.upsert")
        self.assertEqual(set(ev["item"]), set(L.UPSERT_EVENT_KEYS))
        self.assertEqual(self.led.find_by_session(SESSION)["id"], "GS-0001")

        later = NOW + timedelta(hours=1)
        same, none_ev = self.upsert(observed=later)
        self.assertIsNone(none_ev)
        self.assertEqual(same["observed_at"], timeutil.to_iso(later))
        self.assertEqual(len(self.inbox_lines()), 1)

        changed, ev2 = self.upsert(evidence="ended on usage limit · 92 uncommitted, 2 unpushed",
                                   observed=later + timedelta(hours=1))
        self.assertIsNotNone(ev2)
        self.assertEqual(ev2["item"]["evidence"], "ended on usage limit · 92 uncommitted, 2 unpushed")
        self.assertEqual(changed["rev"], 1)
        self.assertEqual(len(self.inbox_lines()), 2)

        other, _ = self.upsert(session=SESSION_2, title=QUESTION_TITLE, project="chipmunk-tinta")
        self.assertEqual(other["id"], "GS-0002")
        self.assertEqual(other["project"], "chipmunk-tinta")

    def test_each_mac_owned_field_counts_as_a_change(self):
        self.upsert()
        t = NOW
        variants = [
            dict(evidence="ended on usage limit · clean tree"),
            dict(looks_resolved=True),
            dict(version="2000:def"),
            dict(title="boathouse: session 10-02 ended on usage limit"),
        ]
        for n, kw in enumerate(variants, start=1):
            with self.subTest(change=kw):
                t = t + timedelta(minutes=10)
                _, ev = self.upsert(observed=t, **kw)
                self.assertIsNotNone(ev)
                self.assertEqual(len(self.inbox_lines()), 1 + n)

    def test_older_observation_changes_nothing(self):
        later = NOW + timedelta(hours=2)
        self.upsert(observed=later)
        item, ev = self.upsert(evidence="stale evidence", observed=NOW)
        self.assertIsNone(ev)
        self.assertEqual(item["evidence"], "ended on usage limit · 91 uncommitted, 2 unpushed")
        self.assertEqual(item["observed_at"], timeutil.to_iso(later))
        _, ev_same_time = self.upsert(evidence="also stale", observed=later)
        self.assertIsNone(ev_same_time)

    def test_never_touches_status_or_rev(self):
        self.upsert()
        self.led.set_status("GS-0001", "done", now=NOW, closed_by="phone")
        item, ev = self.upsert(evidence="ended on usage limit · resumed 10-06, ended clean",
                               looks_resolved=True, observed=NOW + timedelta(hours=1))
        self.assertEqual((item["status"], item["rev"], item["closed_by"]), ("done", 2, "phone"))
        self.assertTrue(item["looks_resolved"])
        self.assertNotIn("status", ev["item"])
        self.assertNotIn("rev", ev["item"])

    def test_title_template_and_sanitizer_are_enforced(self):
        for title in ("boathouse: the session asked about the invoice",
                      "boathouse: session 10-01 ended on a usage limit",
                      "boathouse: session 10-01 ended on usage limit, see transcript"):
            with self.subTest(title=title):
                with self.assertRaises(Refused):
                    self.upsert(title=title)
        with self.assertRaises(Refused) as ctx:
            self.upsert(title="alex@example.com: session 10-01 ended on usage limit")
        self.assertIn(MSG_EMAIL, ctx.exception.problems)
        self.assertFalse(self.home.exists())

    def test_title_must_name_the_items_own_project_and_a_real_date(self):
        refused = [
            # sanitizer-clean, template-shaped, but the prefix is not this item's project
            ("boathouse", "some other words: session 10-01 ended on usage limit"),
            ("boathouse", QUESTION_TITLE),
            ("boathouse", "boathouse extra: session 10-01 ended on usage limit"),
            ("boathouse", "boathouse: session 13-01 ended on usage limit"),
            ("boathouse", "boathouse: session 02-30 ended on an open question"),
            ("boathouse", "boathouse: session 00-10 ended on usage limit"),
            # MM-DD in other digits: int() reads them, the Worker's template check does not
            ("boathouse", "boathouse: session ١٠-٠١ ended on usage limit"),
        ]
        for project, title in refused:
            with self.subTest(title=title):
                with self.assertRaises(Refused) as ctx:
                    self.upsert(project=project, title=title)
                self.assertIn(L.MSG_TEMPLATE, ctx.exception.problems)
        self.assertFalse(self.home.exists())
        item, _ = self.upsert(title="boathouse: session 02-29 ended on usage limit")
        self.assertEqual(item["title"], "boathouse: session 02-29 ended on usage limit")
        L.check_interrupted_title(LIMIT_TITLE)  # without a project: the shape alone
        with self.assertRaises(Refused):
            L.check_interrupted_title(LIMIT_TITLE, "chipmunk-tinta")

    def test_unreadable_stored_observed_at_does_not_wedge_the_session(self):
        # A server row with a garbage observed_at is not stored as one ...
        self.led.merge_server_items([{"id": "GS-0001", "kind": "interrupted", "session_uuid": SESSION,
                                      "project": "boathouse", "title": LIMIT_TITLE, "status": "open",
                                      "rev": 1, "observed_at": "garbage"}], max_seq=1, now=NOW)
        self.assertIsNone(self.led.get("GS-0001")["observed_at"])
        item, ev = self.upsert(evidence="ended on usage limit · clean tree")
        self.assertIsNotNone(ev)
        self.assertEqual(item["observed_at"], timeutil.to_iso(NOW))
        # ... and one damaged on disk counts as absent instead of raising forever.
        doc = json.loads(self.led.items_path.read_text())
        doc["items"]["GS-0001"]["observed_at"] = "not a time"
        self.led.items_path.write_text(json.dumps(doc))
        _, ev2 = self.upsert(evidence="ended on usage limit · 3 uncommitted",
                             observed=NOW + timedelta(minutes=1))
        self.assertIsNotNone(ev2)
        self.assertEqual(self.led.get("GS-0001")["observed_at"],
                         timeutil.to_iso(NOW + timedelta(minutes=1)))

    def test_server_observed_at_is_normalized(self):
        self.led.merge_server_items([{"id": "GS-0004", "kind": "interrupted", "session_uuid": SESSION,
                                      "project": "boathouse", "title": LIMIT_TITLE, "status": "open",
                                      "rev": 1, "observed_at": "2026-10-05T10:00:00.250-05:00"}],
                                    max_seq=4, now=NOW)
        self.assertEqual(self.led.get("GS-0004")["observed_at"], "2026-10-05T15:00:00Z")
        # The server's own observation is "as old": the same observation again changes nothing.
        _, ev = self.upsert(evidence="ended on usage limit · clean tree", observed=NOW)
        self.assertIsNone(ev)


class TestAcks(LedgerCase):
    def test_record_acks_and_stale_bookkeeping(self):
        _, e_add = self.add_promise()
        _, e_done = self.led.set_status("GS-0001", "done", now=NOW)
        _, e_add2 = self.led.add("decision", "home", "x", "home: second item", now=NOW)
        ids = lambda evs: [e["event_id"] for e in evs]  # noqa: E731
        self.assertEqual(ids(self.led.unacked_events()),
                         [e_add["event_id"], e_done["event_id"], e_add2["event_id"]])
        self.assertEqual(ids(self.led.unacked_events(limit=1)), [e_add["event_id"]])

        t = NOW + timedelta(minutes=1)
        self.led.record_acks({
            e_add["event_id"]: {"result": "applied", "item_id": "GS-0001", "rev": 1, "error": None},
            e_done["event_id"]: {"result": "stale", "item_id": "GS-0001", "rev": 2, "error": None},
        }, now=t)
        self.assertEqual(ids(self.led.unacked_events()), [e_add2["event_id"]])
        acks = self.led.acks()["events"]
        self.assertEqual(acks[e_done["event_id"]],
                         {"result": "stale", "item_id": "GS-0001", "rev": 2, "at": timeutil.to_iso(t)})
        self.assertEqual(acks[e_add["event_id"]]["result"], "applied")
        self.assertTrue(self.led.get("GS-0001")["synced"])
        self.assertFalse(self.led.get("GS-0002")["synced"])

        self.led.record_acks({e_add2["event_id"]: {"result": "invalid", "item_id": "GS-0002",
                                                   "rev": None, "error": "title: contains a money amount"}},
                             now=t)
        self.assertEqual(self.led.unacked_events(), [])
        self.assertEqual(self.led.acks()["events"][e_add2["event_id"]]["error"],
                         "title: contains a money amount")
        self.assertFalse(self.led.get("GS-0002")["synced"])

    def test_duplicate_ack_of_an_upsert_marks_synced(self):
        _, ev = self.upsert()
        self.led.record_acks({ev["event_id"]: {"result": "duplicate", "item_id": "GS-0001", "rev": 1}},
                             now=NOW)
        self.assertTrue(self.led.get("GS-0001")["synced"])

    def test_runs_bookkeeping(self):
        for run_id in ("r1", "r2", "r3"):
            self.led.append_run({"run_id": run_id, "started_at": "2026-10-05T14:00:00Z",
                                 "finished_at": "2026-10-05T14:00:05Z", "ok": True,
                                 "sessions_scanned": 1, "items_added": 0, "error": None})
        self.assertEqual([r["run_id"] for r in self.led.unacked_runs()], ["r1", "r2", "r3"])
        self.led.record_runs_acked(["r1"])
        self.assertEqual([r["run_id"] for r in self.led.unacked_runs()], ["r2", "r3"])
        self.led.record_runs_acked(["r1", "r2", "r3"])
        self.assertEqual(self.led.unacked_runs(), [])
        self.assertEqual(self.led.acks()["runs"], ["r1", "r2", "r3"])


class TestMerge(LedgerCase):
    def test_server_wins_status_fields_mac_keeps_the_rest(self):
        marker = "LOCAL-" + uuid.uuid4().hex
        self.add_promise(local=marker, due_at="2026-10-11")
        self.upsert()
        before_events = len(self.inbox_lines())
        server = [
            {"id": "GS-0001", "status": "done", "rev": 3, "closed_at": "2026-10-05T16:00:00Z",
             "closed_by": "phone", "snooze_until": None, "dismiss_reason": None,
             "title": "SERVER TITLE", "project": "elsewhere", "due_at": "2026-12-01",
             "evidence": "server evidence"},
            {"id": "GS-0002", "status": "snoozed", "rev": 2, "snooze_until": "2026-10-06T09:00",
             "evidence": "server says otherwise", "looks_resolved": True},
            {"id": "GS-0009", "kind": "decision", "project": "home",
             "title": "home: decide on the domain renewal", "owner": "me", "status": "open",
             "rev": 1, "review_at": "2026-10-12", "created_at": "2026-10-04T12:00:00Z"},
            {"id": "nope"},
            "not a dict",
        ]
        result = self.led.merge_server_items(server, max_seq=12, now=NOW)
        self.assertEqual(result["added"], ["GS-0009"])
        self.assertEqual(result["changed"], ["GS-0001", "GS-0002"])

        one = self.led.get("GS-0001")
        self.assertEqual((one["status"], one["rev"], one["closed_by"], one["closed_at"]),
                         ("done", 3, "phone", "2026-10-05T16:00:00Z"))
        self.assertEqual((one["title"], one["project"], one["due_at"], one["local_title"]),
                         ("metronome: readout script cap/end date", "metronome", "2026-10-11", marker))
        self.assertIsNone(one["evidence"])
        self.assertTrue(one["synced"])

        two = self.led.get("GS-0002")
        self.assertEqual((two["status"], two["snooze_until"], two["rev"]),
                         ("snoozed", "2026-10-06T09:00", 2))
        self.assertEqual(two["evidence"], "ended on usage limit · 91 uncommitted, 2 unpushed")
        self.assertFalse(two["looks_resolved"])

        nine = self.led.get("GS-0009")
        self.assertEqual(nine["local_title"], "home: decide on the domain renewal")
        self.assertEqual((nine["kind"], nine["status"], nine["review_at"]), ("decision", "open", "2026-10-12"))
        self.assertTrue(nine["synced"])
        self.assertIsNone(nine["reason"])
        self.assertFalse(nine["consequential"])

        self.assertEqual(len(self.inbox_lines()), before_events)  # merging emits nothing
        self.assertEqual(result["next_id"], 13)
        item, _ = self.add_promise(title="metronome: after the merge")
        self.assertEqual(item["id"], "GS-0013")

    def test_next_id_never_goes_backwards(self):
        for n in range(3):
            self.add_promise(title="metronome: item %d" % n)
        self.assertEqual(self.led.merge_server_items([], max_seq=1, now=NOW)["next_id"], 4)
        self.assertEqual(self.led.merge_server_items([], max_seq=None, now=NOW)["next_id"], 4)

    def test_invalid_server_values_are_ignored(self):
        self.add_promise()
        self.led.merge_server_items([{"id": "GS-0001", "status": "exploded", "rev": "two",
                                      "closed_by": "robot"}], max_seq=1, now=NOW)
        item = self.led.get("GS-0001")
        self.assertEqual((item["status"], item["rev"], item["closed_by"]), ("open", 1, None))

    def test_a_stale_mac_write_cannot_undo_a_phone_done(self):
        _, created = self.upsert()
        # The phone marks it done (server rev 2). Meanwhile the Mac, still at rev 1, reopens
        # it locally and refreshes evidence; then it syncs.
        _, mac_status = self.led.set_status("GS-0001", "dismissed", now=NOW,
                                            dismiss_reason="handled_elsewhere")
        self.assertEqual(mac_status["item"]["rev"], 1)
        _, mac_upsert = self.upsert(evidence="ended on usage limit · clean tree",
                                    observed=NOW + timedelta(minutes=5))
        self.led.record_acks({
            created["event_id"]: {"result": "applied", "item_id": "GS-0001", "rev": 1},
            mac_status["event_id"]: {"result": "stale", "item_id": "GS-0001", "rev": 2},
            mac_upsert["event_id"]: {"result": "applied", "item_id": "GS-0001", "rev": 2},
        }, now=NOW)
        self.led.merge_server_items([{"id": "GS-0001", "status": "done", "rev": 2,
                                      "closed_at": "2026-10-05T15:01:00Z", "closed_by": "phone",
                                      "snooze_until": None, "dismiss_reason": None}],
                                    max_seq=1, now=NOW)
        item = self.led.get("GS-0001")
        self.assertEqual((item["status"], item["rev"], item["closed_by"], item["dismiss_reason"]),
                         ("done", 2, "phone", None))
        self.assertEqual(item["evidence"], "ended on usage limit · clean tree")
        self.assertEqual(self.led.unacked_events(), [])


class TestDedup(LedgerCase):
    DIGEST_ROW = {"kind": "digest", "local_date": "2026-10-05", "item_ids": ["GS-0001"],
                  "channel": "ntfy", "error": None}

    def test_claim_once_writes_a_claim_row(self):
        self.assertTrue(self.led.claim_dedup("digest:2026-10-05", now=NOW))
        self.assertFalse(self.led.claim_dedup("digest:2026-10-05", now=NOW))
        self.assertTrue(self.led.claim_dedup("advance:GS-0001", now=NOW))
        self.assertFalse(Ledger(self.cfg).claim_dedup("digest:2026-10-05"))
        self.assertEqual(self.led.notification_rows(), [
            {"dedup_key": "digest:2026-10-05", "ok": None, "sent_at": "2026-10-05T15:00:00Z"},
            {"dedup_key": "advance:GS-0001", "ok": None, "sent_at": "2026-10-05T15:00:00Z"},
        ])
        self.assertFalse(self.led.dedup_path.exists())  # claims live in notifications.jsonl now
        with self.assertRaises(LedgerError):
            self.led.claim_dedup("")

    def test_the_claim_row_carries_the_callers_fields(self):
        row = dict(self.DIGEST_ROW, dedup_key="overridden", ok=True, sent_at="2026-10-05T14:15:00Z")
        self.assertTrue(self.led.claim_dedup("digest:2026-10-05", now=NOW, row=row))
        claim = self.led.notification_rows()[0]
        self.assertEqual(claim, dict(self.DIGEST_ROW, dedup_key="digest:2026-10-05", ok=None,
                                     sent_at="2026-10-05T14:15:00Z"))
        self.assertEqual((row["dedup_key"], row["ok"]), ("overridden", True))  # not mutated

    def test_the_last_row_per_key_wins(self):
        self.led.claim_dedup("digest:2026-10-05", now=NOW, row=self.DIGEST_ROW)
        self.led.append_notification({"kind": "digest", "sent_at": "not a time", "ok": 1})  # no key
        self.led.claim_dedup("advance:GS-0002", now=NOW, row={"kind": "advance", "item_ids": ["GS-0002"]})
        self.led.append_notification(dict(self.DIGEST_ROW, dedup_key="digest:2026-10-05", ok=True,
                                          sent_at="2026-10-05T15:00:00Z"))
        self.led.append_notification({"dedup_key": "advance:GS-0002", "ok": False, "error": "HTTP 500"})
        self.assertEqual(len(self.led.notification_rows()), 5)
        rows = self.led.notifications()
        self.assertEqual([(r.get("dedup_key"), r["ok"]) for r in rows],
                         [("digest:2026-10-05", True), (None, 1), ("advance:GS-0002", False)])
        self.assertEqual(rows[2], {"dedup_key": "advance:GS-0002", "ok": False, "error": "HTTP 500",
                                   "kind": "advance", "item_ids": ["GS-0002"],
                                   "sent_at": "2026-10-05T15:00:00Z"})  # the claim fills in the rest

    def test_a_claim_without_an_outcome_reads_as_ok_null(self):
        # A crash after the claim and before the outcome row: the push rules must neither resend
        # it nor take it as delivered, and it counts against the day's budget.
        from core import schedule

        self.led.upsert_interrupted(SESSION, "1000:abc", "boathouse", LIMIT_TITLE,
                                    "ended on usage limit · clean tree", False, RESUME, "limit_meta",
                                    observed_at=NOW - timedelta(days=1), now=NOW - timedelta(days=1))
        self.led.claim_dedup("digest:2026-10-05", now=NOW, row=self.DIGEST_ROW)
        self.assertEqual([r["ok"] for r in self.led.notifications()], [None])
        self.assertFalse(self.led.claim_dedup("digest:2026-10-05", now=NOW))
        cfg = {"digest_hour": 9, "quiet_start": 22, "quiet_end": 8, "daily_budget": 1}
        items = list(self.led.items().values())
        same_day = schedule.decide(NOW + timedelta(hours=1), items, self.led.notifications(), [],
                                   "America/Chicago", cfg)
        self.assertEqual(same_day["send"], [])  # deduped (and the budget of 1 is used)
        next_day = schedule.decide(NOW + timedelta(days=1), items, self.led.notifications(), [],
                                   "America/Chicago", cfg)
        self.assertEqual([p["item_ids"] for p in next_day["send"]], [["GS-0001"]])  # still new

    def test_notification_rows_count_as_claimed(self):
        self.led.append_notification({"dedup_key": "digest:2026-10-06", "kind": "digest",
                                      "item_ids": ["GS-0001"], "ok": 1, "local_date": "2026-10-06",
                                      "sent_at": "2026-10-06T14:00:00Z"})
        self.assertFalse(self.led.claim_dedup("digest:2026-10-06", now=NOW))
        self.assertEqual(len(self.led.notifications()), 1)

    def write_dedup_json(self, keys):
        self.home.mkdir(mode=0o700)
        self.led.dedup_path.write_text(json.dumps({"version": 1, "keys": keys}))

    def test_dedup_json_from_an_older_version_is_migrated(self):
        self.write_dedup_json({"digest:2026-10-04": "2026-10-04T14:15:00Z",
                               "advance:GS-0002": "2026-10-04T15:00:00Z",
                               "digest:2026-10-03": "2026-10-03T14:15:00Z",
                               "deadman:none": "garbled"})
        self.led.append_notification({"dedup_key": "digest:2026-10-03", "kind": "digest", "ok": 1,
                                      "sent_at": "2026-10-03T14:15:02Z", "item_ids": ["GS-0001"]})
        listed = self.led.notifications()  # read-only: the legacy claims show up as ok-null rows
        self.assertEqual([(r["dedup_key"], r["ok"]) for r in listed], [
            ("digest:2026-10-03", 1), ("digest:2026-10-04", None), ("advance:GS-0002", None),
            ("deadman:none", None)])
        self.assertTrue(self.led.dedup_path.exists())
        self.assertEqual(len(self.led.notification_rows()), 1)

        self.assertFalse(self.led.claim_dedup("digest:2026-10-04", now=NOW))  # still taken
        self.assertFalse(self.led.dedup_path.exists())
        rows = self.led.notification_rows()
        self.assertEqual(rows[1:], [
            {"dedup_key": "digest:2026-10-04", "ok": None, "migrated_from": "dedup.json",
             "sent_at": "2026-10-04T14:15:00Z", "kind": "digest", "local_date": "2026-10-04"},
            {"dedup_key": "advance:GS-0002", "ok": None, "migrated_from": "dedup.json",
             "sent_at": "2026-10-04T15:00:00Z", "kind": "advance", "item_ids": ["GS-0002"]},
            {"dedup_key": "deadman:none", "ok": None, "migrated_from": "dedup.json", "kind": "deadman"},
        ])
        self.assertEqual(self.led.notifications(), listed)  # the same view as before migrating
        self.assertFalse(self.led.claim_dedup("advance:GS-0002", now=NOW))
        self.assertTrue(self.led.claim_dedup("digest:2026-10-05", now=NOW))
        self.assertEqual(len(self.led.notification_rows()), 5)

    def test_a_damaged_dedup_json_blocks_claims_but_not_reads(self):
        self.home.mkdir(mode=0o700)
        self.led.dedup_path.write_text('{"keys": ["SECRET-CONTENT"]}')
        with self.assertRaises(LedgerError) as ctx:
            self.led.claim_dedup("digest:2026-10-05", now=NOW)
        self.assertIn("dedup.json", str(ctx.exception))
        self.assertNotIn("SECRET-CONTENT", str(ctx.exception))
        self.assertEqual(self.led.notifications(), [])
        self.assertEqual(self.led.notification_rows(), [])  # nothing claimed


class TestConcurrency(LedgerCase):
    def run_workers(self, target, args_for):
        ctx = multiprocessing.get_context("spawn")
        go = ctx.Event()
        extra = {}
        if target is _claim_worker:
            extra["queue"] = ctx.Queue()
        procs = []
        for w in range(4):
            args = args_for(w, go, extra)
            procs.append(ctx.Process(target=target, args=args))
        for p in procs:
            p.start()
        go.set()
        results = []
        if "queue" in extra:
            for _ in procs:
                results.append(extra["queue"].get(timeout=120))
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)
        return results

    def test_concurrent_adds_from_four_processes(self):
        self.run_workers(_add_worker, lambda w, go, extra: (str(self.home), w, PER_WORKER, go))
        total = 4 * PER_WORKER
        items = self.led.items()
        self.assertEqual(sorted(items), ["GS-%04d" % n for n in range(1, total + 1)])
        self.assertEqual(len({it["local_title"] for it in items.values()}), total)
        lines = self.inbox_lines()
        self.assertEqual(len(lines), total)
        events = [json.loads(line) for line in lines]  # every line is whole (no interleaving)
        self.assertEqual(len({e["event_id"] for e in events}), total)
        per_item = collections.Counter(e["item"]["id"] for e in events)
        self.assertEqual(set(per_item), set(items))
        self.assertEqual(set(per_item.values()), {1})
        self.assertEqual(json.loads(self.led.items_path.read_text())["next_id"], total + 1)

    def test_concurrent_claims_from_four_processes(self):
        keys = ["digest:2026-10-%02d" % d for d in range(1, 11)]
        results = self.run_workers(
            _claim_worker, lambda w, go, extra: (str(self.home), keys, go, extra["queue"]))
        won = collections.Counter(k for batch in results for k in batch)
        self.assertEqual(set(won), set(keys))
        self.assertEqual(set(won.values()), {1})


class TestLock(LedgerCase):
    def test_reentrant_in_one_thread(self):
        with self.led.lock():
            with self.led.lock():
                self.add_promise()
            Ledger(self.cfg).add("decision", "home", "x", "home: nested instance", now=NOW)
        self.assertEqual(len(self.led.items()), 2)

    def test_busy_from_another_thread(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.led.lock():
                held.set()
                release.wait(30)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(10))
            start = time.monotonic()
            with self.assertRaises(LedgerBusy):
                with self.led.lock(timeout=0.2):
                    pass
            self.assertLess(time.monotonic() - start, 5)
            with self.assertRaises(LedgerBusy):
                Ledger(self.cfg, lock_timeout=0.2).add("decision", "home", "x", "home: blocked", now=NOW)
        finally:
            release.set()
            thread.join(30)
        self.add_promise()  # free again

    def test_busy_from_another_process(self):
        ctx = multiprocessing.get_context("spawn")
        acquired, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold_lock_worker, args=(str(self.home), acquired, release))
        proc.start()
        try:
            self.assertTrue(acquired.wait(60))
            with self.assertRaises(LedgerBusy):
                Ledger(self.cfg, lock_timeout=0.3).add("decision", "home", "x", "home: blocked", now=NOW)
        finally:
            release.set()
            proc.join(60)
        self.assertEqual(proc.exitcode, 0)
        self.add_promise()
        self.assertEqual(len(self.led.items()), 1)

    @unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
    def test_forked_child_does_not_inherit_the_lock(self):
        self.add_promise()
        with self.led.lock():
            pid = os.fork()
            if pid == 0:  # child: must not think it holds the parent's lock
                code = 1
                try:
                    Ledger(self.cfg, lock_timeout=0.2).add("decision", "home", "x",
                                                           "home: from the child", now=NOW)
                except LedgerBusy:
                    code = 0
                except BaseException:
                    code = 2
                os._exit(code)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(len(self.led.items()), 1)

    def test_default_wait_is_ten_seconds(self):
        self.assertEqual(L.LOCK_TIMEOUT, 10.0)
        self.assertEqual(Ledger(self.cfg).lock_timeout, 10.0)


class TestFiles(LedgerCase):
    def mode(self, path: Path) -> int:
        return stat.S_IMODE(os.stat(str(path)).st_mode)

    def touch_everything(self) -> None:
        self.add_promise()
        self.led.set_status("GS-0001", "done", now=NOW)
        _, ev = self.upsert()
        self.led.record_acks({ev["event_id"]: {"result": "applied", "item_id": "GS-0002", "rev": 1}}, now=NOW)
        self.led.append_run({"run_id": "r1", "ok": True})
        self.led.append_notification({"dedup_key": "digest:2026-10-05", "ok": 1})
        self.led.claim_dedup("advance:GS-0001", now=NOW)
        self.led.append_measure({"kind": "minutes", "minutes": 5})
        self.led.save_state(L.default_state())
        self.led.write_digest("# Ghosted\n")

    def test_modes_dir_0700_files_0600(self):
        old = os.umask(0)  # even with a permissive umask
        try:
            self.touch_everything()
        finally:
            os.umask(old)
        self.assertEqual(self.mode(self.home), 0o700)
        names = ["items.json", "inbox.jsonl", "acks.json", "state.json", "runs.jsonl",
                 "notifications.jsonl", "measure.jsonl", "digest.md", "lock"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.mode(self.home / name), 0o600)
        leftovers = [p.name for p in self.home.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_loose_existing_files_are_tightened(self):
        self.home.mkdir(mode=0o755)
        for name in ("inbox.jsonl", "items.json"):
            (self.home / name).write_bytes(b"")
            os.chmod(str(self.home / name), 0o644)
        os.remove(str(self.home / "items.json"))
        (self.home / "items.json").write_text('{"version": 1, "next_id": 1, "items": {}}')
        os.chmod(str(self.home / "items.json"), 0o644)
        self.add_promise()
        self.assertEqual(self.mode(self.home / "inbox.jsonl"), 0o600)
        self.assertEqual(self.mode(self.home / "items.json"), 0o600)
        self.assertEqual(self.mode(self.home), 0o755)  # an existing directory keeps its mode

    def test_jsonl_append_is_one_write_on_an_o_append_fd(self):
        calls = []
        real_write = os.write

        def spy(fd, data):
            appending = bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND)
            calls.append((appending, bytes(data)))
            return real_write(fd, data)

        target = self.home / "x.jsonl"
        with mock.patch.object(os, "write", spy):
            L.append_jsonl(target, {"a": 1, "b": "two"})
            self.add_promise()
        appends = [data for appending, data in calls if appending]
        self.assertEqual(appends[0], b'{"a":1,"b":"two"}\n')
        self.assertEqual(len(appends), 2)  # the helper call, then exactly one for the add's event
        for data in appends:
            self.assertEqual(data.count(b"\n"), 1)
            self.assertTrue(data.endswith(b"\n"))

    def test_read_jsonl_skips_torn_lines(self):
        target = self.home / "t.jsonl"
        self.home.mkdir()
        target.write_bytes(b'{"n":1}\n{"n":2, "tor\n[1,2]\n\n{"n":3}\n{"n":4')
        self.assertEqual([r["n"] for r in L.read_jsonl(target)], [1, 3])
        self.assertEqual(L.read_jsonl(self.home / "missing.jsonl"), [])

    def test_append_after_a_torn_last_line_keeps_the_new_record(self):
        target = self.home / "t.jsonl"
        self.home.mkdir()
        target.write_bytes(b'{"n":1}\n{"n":2,"to')  # a crash cut the last line short
        L.append_jsonl(target, {"n": 3})
        L.append_jsonl(target, {"n": 4})
        self.assertEqual([r["n"] for r in L.read_jsonl(target)], [1, 3, 4])
        self.assertEqual(target.read_bytes(), b'{"n":1}\n{"n":2,"to\n{"n":3}\n{"n":4}\n')

    def test_a_torn_inbox_does_not_lose_the_next_event(self):
        self.home.mkdir()
        self.led.inbox_path.write_bytes(b'{"event_id":"x","type":"item.add","at":"2026-10-05T1')
        _, ev = self.add_promise()
        self.assertEqual([e["event_id"] for e in self.led.unacked_events()], [ev["event_id"]])

    def test_inbox_appends_are_fsynced(self):
        with mock.patch.object(L, "append_jsonl", wraps=L.append_jsonl) as spy:
            self.add_promise()
            self.led.append_run({"run_id": "r1", "ok": True})
        calls = {str(c.args[0]): c.kwargs.get("fsync", False) for c in spy.call_args_list}
        self.assertTrue(calls[str(self.led.inbox_path)])
        self.assertFalse(calls[str(self.led.runs_path)])
        synced = []
        real_fsync = os.fsync
        with mock.patch.object(os, "fsync", side_effect=lambda fd: (synced.append(fd), real_fsync(fd))):
            L.append_jsonl(self.home / "f.jsonl", {"a": 1}, fsync=True)
        self.assertEqual(len(synced), 1)

    def test_state_and_digest(self):
        self.assertEqual(self.led.load_state(), L.default_state())
        state = L.default_state()
        state.update(since="2026-09-21T15:00:00Z", deferred=[SESSION_2], last_scan="2026-10-05T15:00:00Z")
        state["sessions"][SESSION] = {"size": 10, "mtime": 1.5, "version": "10:abc", "verdict": "usage_limit",
                                      "reason": "limit_meta", "item_id": "GS-0001",
                                      "ended_at": "2026-10-01T20:00:00Z"}
        self.led.save_state(state)
        self.assertEqual(self.led.load_state(), state)
        self.led.write_digest("# Ghosted\n\n- GS-0001 metronome: readout\n")
        self.assertEqual(self.led.digest_path.read_text(), "# Ghosted\n\n- GS-0001 metronome: readout\n")

    def test_other_logs(self):
        self.led.append_measure({"kind": "minutes", "minutes": 5, "at": "2026-10-05T15:00:00Z"})
        self.assertEqual(self.led.measures()[0]["minutes"], 5)
        L.append_jsonl(self.led.stops_path, {"at": "2026-10-05T15:00:00Z", "session_id": SESSION,
                                             "event": "Stop", "verdict": "open_question",
                                             "reason": "trailing_q", "size": 123})
        self.assertEqual(self.led.stops()[0]["verdict"], "open_question")

    def test_fsync_flag_writes_the_same_bytes(self):
        L.append_jsonl(self.home / "a.jsonl", {"a": 1}, fsync=True)
        L.append_jsonl(self.home / "a.jsonl", {"b": 2})
        self.assertEqual((self.home / "a.jsonl").read_bytes(), b'{"a":1}\n{"b":2}\n')

    def test_damaged_items_json_raises_without_quoting_it(self):
        self.home.mkdir()
        self.led.items_path.write_bytes(b'{"items": {"GS-0001": {"local_title": "SECRET-CONTENT"')
        with self.assertRaises(LedgerError) as ctx:
            self.led.items()
        self.assertIn("items.json", str(ctx.exception))
        self.assertNotIn("SECRET-CONTENT", str(ctx.exception))
        with self.assertRaises(LedgerError):
            self.add_promise()

    def test_reads_do_not_create_anything(self):
        self.assertEqual(self.led.items(), {})
        self.assertIsNone(self.led.get("GS-0001"))
        self.assertIsNone(self.led.find_by_session(SESSION))
        self.assertEqual(self.led.events(), [])
        self.assertEqual(self.led.unacked_events(), [])
        self.assertEqual(self.led.notifications(), [])
        self.assertFalse(self.home.exists())


class TestIds(unittest.TestCase):
    def test_normalize_id(self):
        for text in ("7", "07", "GS-7", "gs-0007", "GS0007", "LE-0007", "le-7", " GS-0007 "):
            with self.subTest(text=text):
                self.assertEqual(L.normalize_id(text), "GS-0007")
        self.assertEqual(L.normalize_id("GS-12345"), "GS-12345")
        for bad in ("", "GS-", "GS-0000", "0", "x7", None):
            with self.subTest(bad=bad):
                with self.assertRaises(LedgerError):
                    L.normalize_id(bad)

    def test_format_and_number(self):
        self.assertEqual(L.format_id(12), "GS-0012")
        self.assertEqual(L.id_number("GS-0012"), 12)
        self.assertIsNone(L.id_number("GS-x"))


if __name__ == "__main__":
    unittest.main()
