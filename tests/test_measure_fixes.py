"""measure.jsonl status records: a person's status change the server reports is logged once,
with its reason, and still logged after a phone snooze expired; the Mac's own change is not logged
again.

Synthetic data only (invented titles); temp GHOSTED_HOME per test.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import timeutil
from core.config import Config
from core.ledger import Ledger

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)          # Mon Oct 5, 10:00 in Chicago
DIGEST_1 = datetime(2026, 10, 6, 14, 15, tzinfo=UTC)   # Tue 09:15 local
LOCAL_MARKER = "Quokka invoice for the Halvorsen account"


def iso(dt: datetime) -> str:
    return timeutil.to_iso(dt)


class StatusRecordCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.cfg = Config(home=root / "ghosted-home", timezone="America/Chicago", projects_dir=root / "projects")
        self.led = Ledger(self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, title, kind="decision", now=T0, **kw):
        item, _ = self.led.add(kind=kind, project="shop", local_title=LOCAL_MARKER + " " + title,
                               title="shop: " + title, now=now, **kw)
        return item["id"]


class PhoneStatusTests(StatusRecordCase):
    def test_a_phone_snooze_is_logged_after_the_cron_reopened_it(self) -> None:
        a = self.add("reply about the pricing page")
        snoozed_at = iso(DIGEST_1 + timedelta(minutes=5))
        # The Mac was asleep all weekend: its first sync after the snooze sees the item open
        # again (the cron expired the snooze), with the phone's act kept in acted_at.
        self.led.merge_server_items([{"id": a, "status": "open", "rev": 3, "snooze_until": "2026-10-07T09:00",
                                      "acted_at": snoozed_at, "acted_status": "snoozed"}], 1,
                                    DIGEST_1 + timedelta(days=3))
        recs = [m for m in self.led.measures() if m.get("type") == "status"]
        self.assertEqual(recs, [{"type": "status", "id": a, "status": "snoozed", "at": snoozed_at}])
        # The same act seen again is not logged twice.
        self.led.merge_server_items([{"id": a, "status": "open", "rev": 3, "acted_at": snoozed_at,
                                      "acted_status": "snoozed"}], 1, DIGEST_1 + timedelta(days=4))
        self.assertEqual(len([m for m in self.led.measures() if m.get("type") == "status"]), 1)

    def test_a_phone_dismissal_logs_its_reason(self) -> None:
        a = self.add("decide on the logo")
        at = iso(DIGEST_1 + timedelta(hours=1))
        self.led.merge_server_items([{"id": a, "status": "dismissed", "dismiss_reason": "not_real", "rev": 2,
                                      "closed_by": "phone", "closed_at": at, "acted_at": at,
                                      "acted_status": "dismissed"}], 1, DIGEST_1 + timedelta(hours=2))
        recs = [m for m in self.led.measures() if m.get("type") == "status"]
        self.assertEqual(recs, [{"type": "status", "id": a, "status": "dismissed", "reason": "not_real", "at": at}])

    def test_the_macs_own_change_is_not_logged_again(self) -> None:
        a = self.add("decide on the logo")
        item, ev = self.led.set_status(a, "done", now=DIGEST_1)
        # The Worker times a session's change by its event, so the merge sees nothing new.
        self.led.merge_server_items([{"id": a, "status": "done", "rev": 2, "closed_by": "session",
                                      "closed_at": ev["at"], "acted_at": ev["at"], "acted_status": "done"}],
                                    1, DIGEST_1 + timedelta(hours=1))
        self.assertEqual([m for m in self.led.measures() if m.get("type") == "status"], [])


if __name__ == "__main__":
    unittest.main()
