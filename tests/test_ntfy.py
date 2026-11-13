from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from core import schedule
from core.config import Config
from notify import base, ntfy
from notify.ntfy import NtfyAdapter
from tests.fakeserver import FakeServer, Response, closed_port

TOPIC = "ghosted-test-Zq8vN1kd0cY7aR2xLmW5tB9e"  # invented
TOKEN = "tk_testtoken0000000000000000000"  # invented
LOCAL_MARKER = "LOCAL-ONLY-MARKER-91be2d"
OK = {"id": "bAXOJRYg6q8N", "time": 1791258943, "expires": 1791302143, "event": "message"}
ITEMS = [
    {"id": "GS-0001", "kind": "promise", "title": "metronome: readout script cap/end date",
     "local_title": LOCAL_MARKER + " check the readout cap", "status": "open"},
    {"id": "GS-0002", "kind": "interrupted", "title": "boathouse: session 10-01 ended on usage limit",
     "local_title": "boathouse: session 10-01 ended on usage limit", "status": "open"},
]


def _never(*_a: Any, **_kw: Any) -> base.HttpResponse:
    raise AssertionError("no request may be made")


class NtfyCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"GHOSTED_HOME": str(self.root / "ghosted-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.srv = FakeServer().start()
        self.addCleanup(self.srv.stop)
        self.sleeps: List[float] = []
        self.adapter = self.make()

    def cfg(self, **kw: Any) -> Config:
        kw.setdefault("ntfy_server", self.srv.url)
        kw.setdefault("ntfy_topic", TOPIC)
        return Config(home=self.root / "ghosted-home", projects_dir=self.root / "projects",
                      timezone="America/Chicago", adapter="ntfy", **kw)

    def make(self, env: Dict[str, str] = None, **kw: Any) -> NtfyAdapter:
        options = {"retry_delay": 0.01, "sleep": self.sleeps.append}
        options.update({k: kw.pop(k) for k in list(kw) if k in ("transport", "timeout")})
        return NtfyAdapter(self.cfg(**kw), env or {}, **options)

    def send(self, adapter: NtfyAdapter = None, **kw: Any):
        kw.setdefault("title", "Ghosted")
        kw.setdefault("body", "2 open: a · b")
        kw.setdefault("kind", "digest")
        return (adapter or self.adapter).send(kw.pop("items", ITEMS), **kw)

    def last_json(self) -> Dict[str, Any]:
        return self.srv.requests[-1].json()


class PublishShapeTest(NtfyCase):
    def test_json_publish_to_the_root(self) -> None:
        self.srv.queue(Response(200, json=OK))
        result = self.send()
        self.assertEqual((result.ok, result.channel, result.error, result.status), (True, "ntfy", None, 200))
        self.assertEqual(len(self.srv.requests), 1)
        req = self.srv.requests[0]
        self.assertEqual((req.method, req.path), ("POST", "/"))
        self.assertEqual(req.header("content-type"), "application/json")
        self.assertIsNone(req.header("authorization"))
        payload = req.json()
        self.assertEqual(set(payload), {"topic", "message", "title", "priority", "tags"})
        self.assertEqual(payload["topic"], TOPIC)
        self.assertEqual(payload["title"], "Ghosted")
        self.assertEqual(payload["message"], "2 open: a · b")
        self.assertEqual(payload["priority"], 3)
        self.assertEqual(payload["tags"], ["digest"])

    def test_click_url(self) -> None:
        self.srv.queue(Response(200, json=OK), Response(200, json=OK))
        self.send(click_url="https://ghosted.example.workers.dev/?n=7")
        self.assertEqual(self.last_json()["click"], "https://ghosted.example.workers.dev/?n=7")
        self.send(click_url="javascript:alert(1)")
        self.assertNotIn("click", self.last_json())

    def test_deadman_and_high_priority(self) -> None:
        for kind, priority, expected, tags in (("deadman", "high", 4, ["warning", "deadman"]),
                                               ("deadman", "normal", 4, ["warning", "deadman"]),
                                               ("advance", "normal", 3, ["advance"]),
                                               ("digest", "high", 4, ["digest"])):
            with self.subTest(kind=kind, priority=priority):
                self.srv.queue(Response(200, json=OK))
                self.assertTrue(self.send(kind=kind, priority=priority, items=[]).ok)
                payload = self.last_json()
                self.assertEqual(payload["priority"], expected)
                self.assertEqual(payload["tags"], tags)

    def test_always_a_title(self) -> None:
        for title in (None, "", "   "):
            with self.subTest(title=title):
                self.srv.queue(Response(200, json=OK))
                self.send(title=title)
                self.assertEqual(self.last_json()["title"], "Ghosted")

    def test_unicode_survives_as_json(self) -> None:
        self.srv.queue(Response(200, json=OK))
        self.send(title="Due in 24 h ✓", body="café ☕ · …")
        payload = self.last_json()
        self.assertEqual((payload["title"], payload["message"]), ("Due in 24 h ✓", "café ☕ · …"))
        self.srv.requests[-1].body.decode("ascii")  # escaped JSON: pure ASCII on the wire

    def test_token_goes_in_a_bearer_header(self) -> None:
        adapter = self.make(env={"NTFY_TOKEN": TOKEN})
        self.srv.queue(Response(200, json=OK))
        self.assertTrue(self.send(adapter).ok)
        self.assertEqual(self.srv.requests[0].header("authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN.encode(), self.srv.requests[0].body)

    def test_body_is_capped_at_1_kib(self) -> None:
        self.srv.queue(Response(200, json=OK))
        self.send(body="é☕" * 600)  # 3000 bytes
        message = self.last_json()["message"]
        self.assertLessEqual(len(message.encode("utf-8")), ntfy.MESSAGE_MAX_BYTES)
        self.assertTrue(message.endswith("…"))

    def test_timeout_is_10_seconds(self) -> None:
        seen = []

        def transport(url: str, **kw: Any) -> base.HttpResponse:
            seen.append((url, kw["timeout"], kw["method"]))
            return base.HttpResponse(200, {}, json.dumps(OK).encode())

        self.assertTrue(self.send(self.make(transport=transport)).ok)
        self.assertEqual(seen, [(self.srv.url + "/", 10.0, "POST")])


class PrivacyTest(NtfyCase):
    def test_local_titles_never_reach_the_wire(self) -> None:
        self.srv.queue(Response(200, json=OK), Response(200, json=OK))
        titles = [it["title"] for it in ITEMS]
        self.assertTrue(self.send(body=schedule.digest_body(titles)).ok)
        self.assertTrue(self.send(body=None).ok)  # built here, from public titles only
        self.assertEqual(self.last_json()["message"], schedule.digest_body(titles))
        for raw in self.srv.bodies():
            self.assertNotIn(LOCAL_MARKER.encode(), raw)

    def test_a_body_carrying_a_local_title_is_refused_unsent(self) -> None:
        result = self.send(body="1 open: " + ITEMS[0]["local_title"])
        self.assertFalse(result.ok)
        self.assertIn("local", result.error)
        self.assertNotIn(LOCAL_MARKER, result.error)
        self.assertEqual(self.srv.requests, [])

    def test_errors_never_show_the_topic_or_token(self) -> None:
        adapter = self.make(env={"NTFY_TOKEN": TOKEN})
        self.srv.queue(Response(400, json={"code": 40009, "http": 400,
                                           "error": "invalid request: topic %s invalid %s" % (TOPIC, TOKEN)}))
        result = self.send(adapter)
        self.assertFalse(result.ok)
        self.assertIn("40009", result.error)
        self.assertNotIn(TOPIC, result.error)
        self.assertNotIn(TOKEN, result.error)


class RetryTest(NtfyCase):
    def test_4xx_is_never_retried(self) -> None:
        for status, body in ((429, {"code": 42908, "http": 429, "error": "limit reached: daily message quota reached"}),
                             (429, {"code": 42901, "http": 429, "error": "limit reached: too many requests"}),
                             (400, {"code": 40007, "http": 400, "error": "invalid priority parameter"}),
                             (401, {"code": 40101, "http": 401, "error": "unauthorized"}),
                             (404, None)):
            with self.subTest(status=status):
                self.srv.clear()
                self.srv.queue(Response(status, json=body), Response(200, json=OK))
                result = self.send()
                self.assertFalse(result.ok)
                self.assertEqual(result.status, status)
                self.assertEqual(len(self.srv.requests), 1)
                self.assertEqual(self.sleeps, [])

    def test_daily_quota_error_says_when_it_resets(self) -> None:
        self.srv.queue(Response(429, json={"code": 42908, "http": 429,
                                           "error": "limit reached: daily message quota reached"}))
        error = self.send().error
        self.assertIn("HTTP 429", error)
        self.assertIn("42908", error)
        self.assertIn("00:00 UTC", error)

    def test_one_retry_after_a_5xx(self) -> None:
        self.srv.queue(Response(503, body=b"busy"), Response(200, json=OK))
        result = self.send()
        self.assertTrue(result.ok)
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.sleeps, [0.01])
        self.assertEqual(self.srv.requests[0].body, self.srv.requests[1].body)

    def test_two_5xx_give_up(self) -> None:
        self.srv.queue(Response(500), Response(502), Response(200, json=OK))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 502)
        self.assertIn("HTTP 502", result.error)
        self.assertEqual(len(self.srv.requests), 2)

    def test_one_retry_after_a_network_error(self) -> None:
        self.srv.queue(Response(drop=True), Response(200, json=OK))
        self.assertTrue(self.send().ok)
        self.assertEqual(len(self.srv.requests), 2)

    def test_network_errors_twice(self) -> None:
        self.srv.queue(Response(drop=True), Response(drop=True), Response(200, json=OK))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertIsNone(result.status)
        self.assertIn("unreachable", result.error)
        self.assertEqual(len(self.srv.requests), 2)

    def test_connection_refused(self) -> None:
        adapter = self.make(ntfy_server="http://127.0.0.1:%d" % closed_port())
        result = self.send(adapter)
        self.assertFalse(result.ok)
        self.assertIn("connection refused", result.error)
        self.assertEqual(self.sleeps, [0.01])  # one retry, then give up

    def test_a_200_that_is_not_ntfy_json_is_a_captive_portal(self) -> None:
        self.srv.queue(Response(200, body=b"<html>Sign in to the cafe wifi</html>", content_type="text/html"),
                       Response(200, json=OK))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertIn("captive portal", result.error)
        self.assertEqual(len(self.srv.requests), 1)

    def test_a_200_without_a_message_id_is_not_a_delivery(self) -> None:
        self.srv.queue(Response(200, json={"ok": True}))
        self.assertFalse(self.send().ok)

    def test_redirects_are_not_followed(self) -> None:
        self.srv.queue(Response(301, headers={"Location": self.srv.url + "/elsewhere"}))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 301)
        self.assertIn("redirected", result.error)
        self.assertEqual([r.path for r in self.srv.requests], ["/"])


class ConfigTest(NtfyCase):
    def test_https_is_enforced(self) -> None:
        for server in ("http://ntfy.sh", "http://ntfy.example.com:8080", "ftp://ntfy.sh"):
            with self.subTest(server=server):
                adapter = self.make(ntfy_server=server, transport=_never)
                result = self.send(adapter)
                self.assertFalse(result.ok)
                self.assertIn("https://", result.error)
                self.assertIsNone(result.status)

    def test_server_must_be_the_root(self) -> None:
        result = self.send(self.make(ntfy_server="https://ntfy.sh/my-topic", transport=_never))
        self.assertFalse(result.ok)
        self.assertIn("root", result.error)

    def test_topic_must_be_set_and_valid(self) -> None:
        for topic, needle in (("", "not set"), ("bad topic!", "1 to 64"), ("x" * 65, "1 to 64")):
            with self.subTest(topic=topic):
                adapter = self.make(ntfy_topic=topic, transport=_never)
                result = self.send(adapter)
                self.assertFalse(result.ok)
                self.assertIn(needle, result.error)
                if topic:
                    self.assertNotIn(topic, result.error)


class CheckTest(NtfyCase):
    def test_healthy(self) -> None:
        self.srv.queue(Response(200, json={"healthy": True}))
        ok, message = self.adapter.check()
        self.assertTrue(ok)
        self.assertIn("reachable", message)
        self.assertNotIn(TOPIC, message)
        req = self.srv.requests[0]
        self.assertEqual((req.method, req.path), ("GET", "/v1/health"))
        self.assertIsNone(req.header("authorization"))

    def test_token_is_reported_as_set_never_shown(self) -> None:
        self.srv.queue(Response(200, json={"healthy": True}))
        ok, message = self.make(env={"NTFY_TOKEN": TOKEN}).check()
        self.assertTrue(ok)
        self.assertIn("NTFY_TOKEN set", message)
        self.assertNotIn(TOKEN, message)

    def test_unhealthy_and_odd_answers(self) -> None:
        for response, expected in ((Response(200, json={"healthy": False}), False),
                                   (Response(404, body=b"not found"), True),  # older server
                                   (Response(200, body=b"<html>portal</html>"), False),
                                   (Response(500), False),
                                   (Response(302, headers={"Location": "/x"}), False)):
            with self.subTest(status=response.status):
                self.srv.queue(response)
                ok, message = self.adapter.check()
                self.assertIs(ok, expected)
                self.assertTrue(message.startswith("ntfy"))
                self.assertNotIn(TOPIC, message)

    def test_unreachable(self) -> None:
        ok, message = self.make(ntfy_server="http://127.0.0.1:%d" % closed_port()).check()
        self.assertFalse(ok)
        self.assertIn("unreachable", message)

    def test_config_problems_need_no_network(self) -> None:
        ok, message = self.make(ntfy_topic="", transport=_never).check()
        self.assertFalse(ok)
        self.assertIn("topic", message)


if __name__ == "__main__":
    unittest.main()
