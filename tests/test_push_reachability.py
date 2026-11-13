"""A Mac woken offline keeps its digest (review finding python-correctness-4).

With a local adapter (ntfy, Pushover) a claimed push is final. A wake run before Wi-Fi is back
used to claim the day's digest, fail on DNS, and block it for the rest of the window. Now the
adapter's reachable() must open a connection first; when nothing can be reached nothing is
claimed, and the next run in the window sends the digest. No network: probes are injected.
"""
from __future__ import annotations

import socket
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest import mock

from core import cli, ledger, runner, timeutil
from core.config import Config
from notify import base
from notify.ntfy import NtfyAdapter
from notify.pushover import PushoverAdapter

TZ = "America/Chicago"
WAKE = timeutil.parse_iso("2026-10-06T14:35:00Z")  # Tue 09:35 in Chicago


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: List[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def now(self) -> float:
        return self.t


class Direct(unittest.TestCase):
    """No proxy from this machine's settings leaks into the probes under test."""

    def setUp(self) -> None:
        patcher = mock.patch.object(base, "_proxy_for", lambda scheme, host: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class ProbeTests(Direct):
    def test_a_configured_proxy_is_what_gets_probed(self) -> None:
        seen: List[Any] = []
        with mock.patch.object(base, "_proxy_for", lambda scheme, host: ("proxy.example", 3128)):
            ok, _ = base.probe("https://ntfy.sh", connect=lambda h, p, t: seen.append((h, p)), wait=0)
        self.assertEqual((ok, seen), (True, [("proxy.example", 3128)]))

    def test_retries_then_reports_the_reason(self) -> None:
        clock = Clock()
        calls: List[Any] = []

        def dial(host: str, port: int, timeout: float) -> None:
            calls.append((host, port))
            raise socket.gaierror(8, "nodename nor servname provided")

        ok, why = base.probe("https://ntfy.sh", wait=30, step=5, connect=dial, sleep=clock.sleep, clock=clock.now)
        self.assertEqual((ok, why), (False, "DNS lookup failed"))
        self.assertEqual(calls[0], ("ntfy.sh", 443))
        self.assertGreater(len(calls), 1)  # it kept trying while Wi-Fi came back
        self.assertLessEqual(sum(clock.sleeps), 30)

    def test_a_connection_that_opens_later_succeeds(self) -> None:
        clock = Clock()
        tries = {"n": 0}

        def dial(host: str, port: int, timeout: float) -> None:
            tries["n"] += 1
            if tries["n"] < 3:
                raise ConnectionRefusedError(61, "Connection refused")

        self.assertEqual(base.probe("https://ntfy.sh", connect=dial, sleep=clock.sleep, clock=clock.now), (True, None))
        self.assertEqual(tries["n"], 3)


class Fake:
    name = "ntfy"
    owns_schedule = False

    def __init__(self, reach: bool) -> None:
        self.reach = reach
        self.calls: List[Dict[str, Any]] = []

    def reachable(self):
        return (True, None) if self.reach else (False, "ntfy https://ntfy.sh unreachable: DNS lookup failed")

    def send(self, items: Any, *, title: str, body: str, kind: str, priority: str = "normal",
             click_url: Optional[str] = None) -> Any:
        self.calls.append({"kind": kind})
        return SimpleNamespace(ok=True, channel="ntfy", error=None, status=200)


class WakeOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cfg = Config(home=root / "ghosted-home", projects_dir=root / "projects", timezone=TZ, adapter="ntfy")
        self.led = ledger.Ledger(self.cfg)
        self.led.add("promise", "shop", "renew it", "shop: renew the certificate", due_at="2026-10-06",
                     now=WAKE - timedelta(days=2))

    def test_an_offline_wake_claims_nothing_and_the_next_run_sends_the_digest(self) -> None:
        offline = Fake(reach=False)
        out = runner.local_push(self.cfg, self.led, offline, WAKE)
        self.assertEqual(out["unreachable"], "ntfy https://ntfy.sh unreachable: DNS lookup failed")
        self.assertEqual((offline.calls, self.led.notification_rows()), ([], []))
        self.assertIn("push skipped", cli._notify_summary(out))
        online = Fake(reach=True)
        runner.local_push(self.cfg, self.led, online, WAKE + timedelta(minutes=27))  # the 10:02 run
        self.assertEqual(online.calls, [{"kind": "digest"}])
        self.assertEqual([(r["dedup_key"], r["ok"]) for r in self.led.notifications()],
                         [("digest:2026-10-06", True)])

    def test_the_run_digest_says_the_push_was_skipped(self) -> None:
        outcome = runner.run(self.cfg, WAKE, scan=False, adapter=Fake(reach=False))
        self.assertIn("skipped (ntfy https://ntfy.sh unreachable: DNS lookup failed)", outcome.digest or "")


class AdapterReachableTests(Direct):
    def cfg(self, **kw: Any) -> Any:
        base_kw = dict(ntfy_server="https://ntfy.sh", ntfy_topic="ghosted-test-topic-123")
        base_kw.update(kw)
        return SimpleNamespace(**base_kw)

    def test_ntfy_probes_its_server(self) -> None:
        seen: List[Any] = []

        def dial(host: str, port: int, timeout: float) -> None:
            seen.append((host, port))
            raise socket.gaierror(8, "no")

        adapter = NtfyAdapter(self.cfg(), {}, probe=dial, sleep=lambda s: None, reach_wait=0)
        ok, why = adapter.reachable()
        self.assertFalse(ok)
        self.assertEqual(why, "ntfy https://ntfy.sh unreachable: DNS lookup failed")
        self.assertNotIn("ghosted-test-topic-123", why)  # the topic is a password
        self.assertEqual(seen[0], ("ntfy.sh", 443))
        good = NtfyAdapter(self.cfg(), {}, probe=lambda h, p, t: None, reach_wait=0)
        self.assertEqual(good.reachable(), (True, None))

    def test_a_config_problem_is_left_to_send(self) -> None:
        adapter = NtfyAdapter(self.cfg(ntfy_topic=""), {}, probe=lambda h, p, t: None)
        self.assertEqual(adapter.reachable(), (True, None))

    def test_pushover_probes_its_endpoint(self) -> None:
        seen: List[Any] = []
        adapter = PushoverAdapter(SimpleNamespace(), {"PUSHOVER_TOKEN": "t", "PUSHOVER_USER": "u"},
                                  probe=lambda h, p, t: seen.append((h, p)), reach_wait=0)
        self.assertEqual(adapter.reachable(), (True, None))
        self.assertEqual(seen, [("api.pushover.net", 443)])


if __name__ == "__main__":
    unittest.main()
