from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from core import schedule
from core.config import Config
from notify import base, pushover
from notify.pushover import PushoverAdapter
from tests.fakeserver import FakeServer, Response, closed_port

TOKEN = "azGDORePK8gMaC0QOYAMyEEuzJnyUi"  # invented, Pushover-shaped (30 chars)
USER = "uQiRzpo4DXghDmr9QzzfQu27cmVRsG"  # invented
LOCAL_MARKER = "LOCAL-ONLY-MARKER-c0ffee"
OK = {"status": 1, "request": "647d2300-702c-4b38-8b2f-d56326ae460b"}
BAD_USER = {"user": "invalid", "errors": ["user identifier is invalid"], "status": 0,
            "request": "5042853c-402d-4a18-abcb-168734a801de"}
ITEMS = [{"id": "GS-0003", "kind": "promise", "title": "home: pay the overdue water bill",
          "local_title": LOCAL_MARKER + " pay the registrar", "status": "open"}]


def _never(*_a: Any, **_kw: Any) -> base.HttpResponse:
    raise AssertionError("no request may be made")


class PushoverCase(unittest.TestCase):
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

    def make(self, env: Dict[str, str] = None, **kw: Any) -> PushoverAdapter:
        cfg = Config(home=self.root / "ghosted-home", projects_dir=self.root / "projects",
                     timezone="America/Chicago", adapter="pushover")
        kw.setdefault("endpoint", self.srv.url + "/1/messages.json")
        kw.setdefault("sleep", self.sleeps.append)
        return PushoverAdapter(cfg, {"PUSHOVER_TOKEN": TOKEN, "PUSHOVER_USER": USER} if env is None else env, **kw)

    def send(self, adapter: PushoverAdapter = None, **kw: Any):
        kw.setdefault("title", "Ghosted")
        kw.setdefault("body", "1 open: home: pay the overdue water bill")
        kw.setdefault("kind", "digest")
        return (adapter or self.adapter).send(kw.pop("items", ITEMS), **kw)

    def last_form(self) -> Dict[str, str]:
        return self.srv.requests[-1].form()


class MessageShapeTest(PushoverCase):
    def test_form_post(self) -> None:
        self.srv.queue(Response(200, json=OK))
        result = self.send()
        self.assertEqual((result.ok, result.channel, result.error, result.status), (True, "pushover", None, 200))
        req = self.srv.requests[0]
        self.assertEqual((req.method, req.path), ("POST", "/1/messages.json"))
        self.assertTrue(req.header("content-type").startswith("application/x-www-form-urlencoded"))
        form = req.form()
        self.assertEqual(set(form), {"token", "user", "title", "message", "priority"})
        self.assertEqual((form["token"], form["user"]), (TOKEN, USER))
        self.assertEqual(form["title"], "Ghosted")
        self.assertEqual(form["message"], "1 open: home: pay the overdue water bill")
        self.assertEqual(form["priority"], "0")

    def test_deadman_is_priority_1(self) -> None:
        for kind, priority, expected in (("deadman", "high", "1"), ("deadman", "normal", "1"),
                                         ("advance", "normal", "0"), ("digest", "high", "1")):
            with self.subTest(kind=kind, priority=priority):
                self.srv.queue(Response(200, json=OK))
                self.send(kind=kind, priority=priority, items=[])
                self.assertEqual(self.last_form()["priority"], expected)

    def test_url_when_given_and_valid(self) -> None:
        self.srv.queue(Response(200, json=OK), Response(200, json=OK))
        self.send(click_url="https://ghosted.example.workers.dev/")
        self.assertEqual(self.last_form()["url"], "https://ghosted.example.workers.dev/")
        self.send(click_url="https://example.com/" + "a" * 600)  # over Pushover's 512
        self.assertNotIn("url", self.last_form())

    def test_lengths_are_capped(self) -> None:
        self.srv.queue(Response(200, json=OK))
        self.send(title="T" * 300, body="☕" * 1500)
        form = self.last_form()
        self.assertEqual(len(form["title"]), pushover.TITLE_MAX)
        self.assertEqual(len(form["message"]), pushover.MESSAGE_MAX)
        self.assertTrue(form["message"].endswith("…"))

    def test_blank_title_and_body(self) -> None:
        self.srv.queue(Response(200, json=OK))
        self.send(title=None, body=None)
        form = self.last_form()
        self.assertEqual(form["title"], "Ghosted")
        self.assertEqual(form["message"], schedule.digest_body([ITEMS[0]["title"]]))

    def test_timeout_is_10_seconds(self) -> None:
        seen = []

        def transport(url: str, **kw: Any) -> base.HttpResponse:
            seen.append(kw["timeout"])
            return base.HttpResponse(200, {}, b'{"status":1}')

        self.assertTrue(self.send(self.make(transport=transport)).ok)
        self.assertEqual(seen, [10.0])


class PrivacyTest(PushoverCase):
    def test_local_titles_never_reach_the_wire(self) -> None:
        self.srv.queue(Response(200, json=OK))
        self.assertTrue(self.send(body=None).ok)
        for raw in self.srv.bodies():
            self.assertNotIn(LOCAL_MARKER.encode(), raw)

    def test_a_local_title_in_the_text_is_refused_unsent(self) -> None:
        result = self.send(body=ITEMS[0]["local_title"])
        self.assertFalse(result.ok)
        self.assertIn("local", result.error)
        self.assertEqual(self.srv.requests, [])

    def test_errors_never_show_the_keys(self) -> None:
        self.srv.queue(Response(400, json={"token": "invalid", "status": 0,
                                           "errors": ["application token %s is invalid for %s" % (TOKEN, USER)]}))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertIn("is invalid", result.error)
        self.assertNotIn(TOKEN, result.error)
        self.assertNotIn(USER, result.error)


class RetryTest(PushoverCase):
    def test_4xx_is_never_retried(self) -> None:
        for status, body in ((400, BAD_USER), (429, {"status": 0, "errors": ["over quota"]}),
                             (401, None), (404, None)):
            with self.subTest(status=status):
                self.srv.clear()
                self.srv.queue(Response(status, json=body), Response(200, json=OK))
                result = self.send()
                self.assertFalse(result.ok)
                self.assertEqual(result.status, status)
                self.assertEqual(len(self.srv.requests), 1)
                self.assertEqual(self.sleeps, [])

    def test_invalid_input_explains_itself(self) -> None:
        self.srv.queue(Response(400, json=BAD_USER))
        self.assertIn("user identifier is invalid", self.send().error)
        self.srv.queue(Response(429, json={"status": 0}))
        self.assertIn("monthly message limit", self.send().error)

    def test_one_retry_no_sooner_than_5_seconds(self) -> None:
        self.srv.queue(Response(500, body=b"oops"), Response(200, json=OK))
        result = self.send()
        self.assertTrue(result.ok)
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.sleeps, [5.0])

    def test_two_failures_give_up(self) -> None:
        self.srv.queue(Response(503), Response(drop=True), Response(200, json=OK))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertIsNone(result.status)
        self.assertIn("unreachable", result.error)
        self.assertEqual(len(self.srv.requests), 2)

    def test_network_error_then_success(self) -> None:
        self.srv.queue(Response(drop=True), Response(200, json=OK))
        self.assertTrue(self.send().ok)

    def test_a_200_must_say_status_1(self) -> None:
        self.srv.queue(Response(200, json={"status": 0, "errors": ["message cannot be blank"]}))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertIn("message cannot be blank", result.error)
        self.assertEqual(len(self.srv.requests), 1)

    def test_captive_portal(self) -> None:
        self.srv.queue(Response(200, body=b"<html>hotel wifi</html>", content_type="text/html"))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertIn("captive portal", result.error)
        self.assertEqual(len(self.srv.requests), 1)

    def test_redirect_is_not_followed(self) -> None:
        self.srv.queue(Response(302, headers={"Location": self.srv.url + "/elsewhere"}))
        result = self.send()
        self.assertFalse(result.ok)
        self.assertEqual([r.path for r in self.srv.requests], ["/1/messages.json"])


class ConfigTest(PushoverCase):
    def test_missing_keys(self) -> None:
        for env, missing in (({}, "PUSHOVER_TOKEN and PUSHOVER_USER"),
                             ({"PUSHOVER_TOKEN": TOKEN}, "PUSHOVER_USER"),
                             ({"PUSHOVER_USER": USER}, "PUSHOVER_TOKEN")):
            with self.subTest(missing=missing):
                adapter = self.make(env=env, transport=_never)
                result = adapter.send(ITEMS, title="t", body="b", kind="digest")
                self.assertFalse(result.ok)
                self.assertIn(missing + " missing", result.error)
                ok, message = adapter.check()
                self.assertFalse(ok)
                self.assertIn(missing + " missing", message)

    def test_https_is_enforced(self) -> None:
        adapter = self.make(endpoint="http://api.pushover.example/1/messages.json", transport=_never)
        result = self.send(adapter)
        self.assertFalse(result.ok)
        self.assertIn("https://", result.error)

    def test_default_endpoints(self) -> None:
        cfg = Config(home=self.root / "ghosted-home", projects_dir=self.root / "projects", timezone="UTC")
        adapter = PushoverAdapter(cfg, {})
        self.assertEqual(adapter.endpoint, "https://api.pushover.net/1/messages.json")
        self.assertEqual(adapter.validate_endpoint, "https://api.pushover.net/1/users/validate.json")
        self.assertEqual(adapter.retry_delay, 5.0)


class CheckTest(PushoverCase):
    def test_valid_keys(self) -> None:
        self.srv.queue(Response(200, json={"status": 1, "group": 0, "devices": ["iphone"],
                                           "licenses": ["iOS"], "request": "x"}))
        ok, message = self.adapter.check()
        self.assertTrue(ok)
        self.assertIn("1 active device", message)
        self.assertNotIn("iphone", message)  # device names are personal
        req = self.srv.requests[0]
        self.assertEqual((req.method, req.path), ("POST", "/1/users/validate.json"))
        self.assertEqual(req.form(), {"token": TOKEN, "user": USER})

    def test_invalid_keys(self) -> None:
        self.srv.queue(Response(400, json=BAD_USER))
        ok, message = self.adapter.check()
        self.assertFalse(ok)
        self.assertIn("user identifier is invalid", message)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn(USER, message)

    def test_unreachable(self) -> None:
        port = closed_port()
        ok, message = self.make(endpoint="http://127.0.0.1:%d/1/messages.json" % port).check()
        self.assertFalse(ok)
        self.assertIn("unreachable", message)
        self.assertEqual(self.sleeps, [])  # ghosted doctor never waits on a retry


if __name__ == "__main__":
    unittest.main()
