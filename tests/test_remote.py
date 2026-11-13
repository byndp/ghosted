from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from core import __version__
from core import ledger as L
from core.config import Config
from core.ledger import Ledger
from notify import base, remote
from notify.remote import RemoteAdapter
from tests.fakeserver import FakeServer, Request, Response, closed_port

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # Monday 10:00 in Chicago
TOKEN_ID = "test-client-id.access"  # invented
TOKEN_SECRET = "test-client-secret-not-real"  # invented
ENV = {"GHOSTED_SERVICE_TOKEN_ID": TOKEN_ID, "GHOSTED_SERVICE_TOKEN_SECRET": TOKEN_SECRET}
LOCAL_MARKER = "LOCAL-ONLY-MARKER-5ab1e7"
SESSION = "0a0a0a0a-1111-4222-8333-444455556666"
LIMIT_TITLE = "boathouse: session 10-01 ended on usage limit"
RUN = {"run_id": "run-0001", "started_at": "2026-10-05T14:01:00Z", "finished_at": "2026-10-05T14:01:09Z",
       "ok": True, "sessions_scanned": 3, "items_added": 1, "error": None,
       "note": LOCAL_MARKER, "digest_items": 4}
RUN_PUBLIC = {"run_id": "run-0001", "started_at": "2026-10-05T14:01:00Z",
              "finished_at": "2026-10-05T14:01:09Z", "ok": True, "sessions_scanned": 3,
              "items_added": 1, "error": None}
SERVER_TIME = "2026-10-05T15:00:05Z"
STATUS_FIELDS = {"status": "open", "rev": 1, "snooze_until": None, "dismiss_reason": None,
                 "closed_at": None, "closed_by": None}


def _never(*_a: Any, **_kw: Any) -> base.HttpResponse:
    raise AssertionError("no request may be made")


class FakeWorker:
    """In-memory stand-in for POST /api/sync, following the event rules of contract section 14."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.seen: Dict[str, str] = {}
        self.max_seq = 0
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.notifications: List[Dict[str, Any]] = []
        self.subscriptions = 1
        self.devices = 2
        self.dev_mode = False
        self.pairing = True  # AUTH_MODE device
        self.ignore: Callable[[Dict[str, Any]], bool] = lambda ev: False  # neither applied nor acked
        self.drop_after_apply = 0  # apply the next N requests, then lose their responses
        self.overrides: Dict[int, Response] = {}  # request number (1-based) -> canned answer
        self.count = 0
        self.stale_now: set = set()  # items with a stale status event earlier in this request
        self.tz: Optional[str] = None  # the Worker's TZ_NAME, sent in every response when set

    def __call__(self, req: Request) -> Response:
        if (req.method, req.path) != ("POST", "/api/sync"):
            return Response(404, json={"error": "not_found"})
        self.count += 1
        if self.count in self.overrides:
            return self.overrides[self.count]
        if self.dev_mode:
            if req.header("x-dev-identity") != "sync":
                return Response(401, json={"error": "auth_required"})
        elif (req.header("cf-access-client-id"), req.header("cf-access-client-secret")) != (TOKEN_ID, TOKEN_SECRET):
            return Response(302, headers={"Location": "https://team.cloudflareaccess.com/cdn-cgi/access/login/x"})
        body = req.json()
        acks = {}
        self.stale_now = set()
        for ev in body["events"]:
            if not self.ignore(ev):
                acks[ev["event_id"]] = self.apply(ev)
        for run in body["runs"]:
            self.runs[run["run_id"]] = run
        want = body["want"]
        since = want.get("notifications_since")
        out: Dict[str, Any] = {
            "server_time": SERVER_TIME, "acks": acks, "runs_acked": [r["run_id"] for r in body["runs"]],
            "max_seq": self.max_seq,
            "notifications": [copy.deepcopy(n) for n in self.notifications if since is None or n["sent_at"] >= since],
        }
        if self.tz is not None:
            out["tz"] = self.tz
        if want.get("items"):
            out["items"] = [copy.deepcopy(self.items[k]) for k in sorted(self.items)]
        if want.get("test_push"):
            out["test_push"] = {"sent": self.subscriptions, "failed": 0}
        if want.get("pair") and self.pairing:
            out["pair"] = {"code": "K7QF-2M9X", "expires_at": "2026-10-05T15:10:00Z"}
        if want.get("revoke_devices"):
            out["revoked"], self.devices = self.devices, 0
        if self.drop_after_apply:
            self.drop_after_apply -= 1
            return Response(drop=True)
        return Response(200, json=out)

    def apply(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        event_id, kind, it = ev["event_id"], ev["type"], ev["item"]
        item_id = it.get("id")
        if event_id in self.seen:
            result = "duplicate"
        elif kind == "item.add":
            result = "duplicate" if item_id in self.items else self._insert(it)
        elif kind == "item.upsert":
            cur = next((x for x in self.items.values() if x.get("session_uuid") == it.get("session_uuid")), None)
            if cur is None:
                result = self._insert(it)
            elif (it.get("observed_at") or "") > (cur.get("observed_at") or ""):
                for key in ("title", "evidence", "looks_resolved", "content_version", "resume", "observed_at"):
                    cur[key] = it.get(key)
                result = "applied"
            else:
                result = "duplicate"
        elif kind == "item.status":
            # The Worker's order (notify/remote-worker/src/sync.js): the state it already has ->
            # duplicate; an earlier stale status event for the item in this request, or another
            # rev -> stale; else apply.
            cur = self.items.get(item_id)
            if cur is None:
                result = "invalid"
            elif self._same_state(cur, it):
                result = "duplicate"
            elif item_id in self.stale_now or it.get("rev") != cur["rev"]:
                self.stale_now.add(item_id)
                result = "stale"
            else:
                closing = it["status"] in ("done", "dismissed")
                cur.update(status=it["status"], snooze_until=it.get("snooze_until"),
                           dismiss_reason=it.get("dismiss_reason"), rev=cur["rev"] + 1,
                           closed_by=it.get("closed_by") if closing else None,
                           closed_at=ev["at"] if closing else None)
                result = "applied"
        else:
            result = "invalid"
        self.seen.setdefault(event_id, result)
        cur = self.items.get(item_id)
        return {"result": result, "item_id": item_id, "rev": cur["rev"] if cur else None,
                "error": "unknown item or type" if result == "invalid" else None}

    @staticmethod
    def _same_state(cur: Dict[str, Any], it: Dict[str, Any]) -> bool:
        if it.get("status") != cur["status"]:
            return False
        if cur["status"] == "snoozed":
            return it.get("snooze_until") == cur.get("snooze_until")
        if cur["status"] == "dismissed":
            return it.get("dismiss_reason") == cur.get("dismiss_reason")
        return True

    def _insert(self, it: Dict[str, Any]) -> str:
        row = dict(it)
        row.update(STATUS_FIELDS, updated_at=row.get("created_at"))
        self.items[it["id"]] = row
        self.max_seq = max(self.max_seq, L.id_number(it["id"]) or 0)
        return "applied"

    def phone(self, item_id: str, status: str, **fields: Any) -> None:
        cur = self.items[item_id]
        cur.update(status=status, rev=cur["rev"] + 1, **fields)


class RemoteCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"GHOSTED_HOME": str(self.root / "ghosted-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GHOSTED_DEV_INSECURE", None)
        self.worker = FakeWorker()
        self.srv = FakeServer(self.worker).start()
        self.addCleanup(self.srv.stop)
        self.cfg = self.make_cfg(self.srv.url)
        self.led = Ledger(self.cfg)
        self.adapter = RemoteAdapter(self.cfg, ENV, ledger=self.led)
        self.serial = 0

    def make_cfg(self, url: str) -> Config:
        return Config(home=self.root / "ghosted-home", projects_dir=self.root / "projects",
                      timezone="America/Chicago", adapter="remote", remote_url=url)

    def add(self, local: Optional[str] = None, **kw: Any):
        self.serial += 1
        kw.setdefault("now", NOW)
        local = local or "%s promise %d for the readout script" % (LOCAL_MARKER, self.serial)
        return self.led.add("promise", "metronome", local, "metronome: public item %d" % self.serial, **kw)

    def add_many(self, n: int) -> List[Dict[str, Any]]:
        return [self.add()[1] for _ in range(n)]

    def sync(self, adapter: Optional[RemoteAdapter] = None, **kw: Any):
        return (adapter or self.adapter).sync(self.led, NOW, **kw)

    def bodies(self) -> List[Dict[str, Any]]:
        return [r.json() for r in self.srv.requests if r.path == "/api/sync"]

    @staticmethod
    def sync_ok(result) -> None:
        if not result.ok:
            raise AssertionError("sync failed: %s" % result.error)


class RequestShapeTest(RemoteCase):
    def test_request_shape_headers_and_acks(self) -> None:
        item, event = self.add(due_at="2026-10-11")
        self.led.append_run(RUN)
        result = self.sync()
        self.sync_ok(result)
        [req] = self.srv.requests
        self.assertEqual((req.method, req.path), ("POST", "/api/sync"))
        self.assertEqual(req.header("cf-access-client-id"), TOKEN_ID)
        self.assertEqual(req.header("cf-access-client-secret"), TOKEN_SECRET)
        self.assertEqual(req.header("content-type"), "application/json")
        self.assertIsNone(req.header("x-dev-identity"))
        body = req.json()
        self.assertEqual(set(body), {"client", "runs", "events", "want"})
        self.assertEqual(body["client"], {"version": __version__, "tz": "America/Chicago"})
        self.assertEqual(body["want"], {"items": True, "notifications_since": None, "test_push": None})
        self.assertEqual(body["events"], [event])  # the ledger's public payload, as queued
        self.assertEqual(body["runs"], [RUN_PUBLIC])
        self.assertEqual(result.acks, {event["event_id"]: {"result": "applied", "item_id": item["id"],
                                                             "rev": 1, "error": None}})
        self.assertEqual(result.runs_acked, ["run-0001"])
        self.assertEqual((result.stale, result.invalid, result.requests, result.status),
                         ([], [], 1, 200))
        self.assertEqual(result.server_time, SERVER_TIME)
        self.assertEqual(self.led.unacked_events(), [])
        self.assertEqual(self.led.unacked_runs(), [])
        self.assertTrue(self.led.get(item["id"])["synced"])

    def test_nothing_to_send_is_still_one_request(self) -> None:
        self.sync_ok(self.sync())
        [body] = self.bodies()
        self.assertEqual((body["events"], body["runs"]), ([], []))

    def test_url_forms(self) -> None:
        for suffix in ("", "/", "/api/sync", "/api/sync/"):
            with self.subTest(suffix=suffix):
                self.srv.clear()
                adapter = RemoteAdapter(self.make_cfg(self.srv.url + suffix), ENV, ledger=self.led)
                self.sync_ok(self.sync(adapter))
                self.assertEqual([r.path for r in self.srv.requests], ["/api/sync"])

    def test_timeout_is_20_seconds(self) -> None:
        seen = []

        def transport(url: str, **kw: Any) -> base.HttpResponse:
            seen.append((url, kw["method"], kw["timeout"]))
            return base.HttpResponse(200, {}, b'{"acks":{},"runs_acked":[],"items":[],"max_seq":0}')

        self.sync_ok(self.sync(RemoteAdapter(self.cfg, ENV, ledger=self.led, transport=transport)))
        self.assertEqual(seen, [(self.srv.url + "/api/sync", "POST", 20.0)])

    def test_runs_are_projected_onto_the_contract_fields(self) -> None:
        self.assertEqual(remote.public_run(RUN), RUN_PUBLIC)
        odd = {"run_id": "r", "ok": 1, "sessions_scanned": -2, "items_added": "3", "started_at": 5,
               "error": "ValueError: bad thing\nat line 3"}
        self.assertEqual(remote.public_run(odd), {
            "run_id": "r", "started_at": None, "finished_at": None, "ok": True, "sessions_scanned": 0,
            "items_added": 0, "error": "ValueError: bad thing at line 3"})
        self.assertIs(remote.public_run({"run_id": "r", "ok": "yes"})["ok"], False)
        self.assertEqual(remote.public_run({"run_id": "r", "error": "mail bob@example.com failed"})["error"],
                         remote.RUN_ERROR_GENERIC)
        self.assertEqual(len(remote.public_run({"run_id": "r", "error": "x " * 300})["error"]), remote.RUN_ERROR_MAX)


class AuthTest(RemoteCase):
    def test_a_redirect_means_the_token_was_not_accepted(self) -> None:
        self.add_many(2)
        adapter = RemoteAdapter(self.cfg, {"GHOSTED_SERVICE_TOKEN_ID": TOKEN_ID,
                                           "GHOSTED_SERVICE_TOKEN_SECRET": "wrong-secret-value"}, ledger=self.led)
        result = self.sync(adapter)
        self.assertFalse(result.ok)
        self.assertIn("service token not accepted", result.error)
        self.assertIn("302", result.error)
        self.assertNotIn("wrong-secret-value", result.error)
        self.assertEqual(len(self.srv.requests), 1)  # the sign-in page was never fetched
        self.assertEqual(result.status, 302)
        self.assertEqual(len(self.led.unacked_events()), 2)

    def test_401_and_403(self) -> None:
        self.add()
        for status in (401, 403):
            with self.subTest(status=status):
                self.srv.queue(Response(status, json={"error": "forbidden"}))
                result = self.sync()
                self.assertFalse(result.ok)
                self.assertIn("service token not accepted (HTTP %d: forbidden)" % status, result.error)
                self.assertEqual(len(self.led.unacked_events()), 1)

    def test_missing_credentials_send_nothing(self) -> None:
        adapter = RemoteAdapter(self.cfg, {"GHOSTED_SERVICE_TOKEN_ID": TOKEN_ID}, ledger=self.led, transport=_never)
        result = self.sync(adapter)
        self.assertFalse(result.ok)
        self.assertIn("GHOSTED_SERVICE_TOKEN_SECRET missing from .env", result.error)

    def test_https_is_required(self) -> None:
        for url in ("http://ghosted.example.workers.dev", "", "ftp://example.com"):
            with self.subTest(url=url):
                adapter = RemoteAdapter(self.make_cfg(url), ENV, ledger=self.led, transport=_never)
                result = self.sync(adapter)
                self.assertFalse(result.ok)
                self.assertIn("notify.remote.url", result.error)

    def test_dev_mode_needs_the_flag_and_a_local_host(self) -> None:
        self.worker.dev_mode = True
        self.add()
        adapter = RemoteAdapter(self.cfg, {"GHOSTED_DEV_INSECURE": "1"}, ledger=self.led)
        self.sync_ok(self.sync(adapter))
        req = self.srv.requests[-1]
        self.assertEqual(req.header("x-dev-identity"), "sync")
        self.assertIsNone(req.header("cf-access-client-id"))
        self.assertIsNone(req.header("cf-access-client-secret"))
        with mock.patch.dict(os.environ, {"GHOSTED_DEV_INSECURE": "1"}):  # the local end-to-end test's way
            self.sync_ok(self.sync(RemoteAdapter(self.cfg, {}, ledger=self.led)))
        # Never for a real host, whatever the flag says.
        real = RemoteAdapter(self.make_cfg("https://ghosted.example.workers.dev"),
                             dict(ENV, GHOSTED_DEV_INSECURE="1"))
        _url, headers, _where, problem = real._target()
        self.assertIsNone(problem)
        self.assertNotIn("x-dev-identity", headers)
        self.assertEqual(headers["CF-Access-Client-Id"], TOKEN_ID)
        # And never without the flag: a local URL then needs the service token like any other.
        for flag in (None, "0", "true"):
            with self.subTest(flag=flag):
                env = {} if flag is None else {"GHOSTED_DEV_INSECURE": flag}
                result = self.sync(RemoteAdapter(self.cfg, env, ledger=self.led, transport=_never))
                self.assertFalse(result.ok)
                self.assertIn("GHOSTED_SERVICE_TOKEN_ID", result.error)


class ChunkingTest(RemoteCase):
    def test_250_events_take_three_requests(self) -> None:
        events = self.add_many(250)
        result = self.sync(test_push={"title": "Ghosted", "body": "Test push"})
        self.sync_ok(result)
        bodies = self.bodies()
        self.assertEqual([len(b["events"]) for b in bodies], [100, 100, 50])
        sent = [ev["event_id"] for b in bodies for ev in b["events"]]
        self.assertEqual(sent, [ev["event_id"] for ev in events])  # oldest first, each once
        for req in self.srv.requests:
            self.assertEqual(req.header("cf-access-client-id"), TOKEN_ID)
            self.assertEqual(req.header("cf-access-client-secret"), TOKEN_SECRET)
        self.assertEqual([b["want"]["test_push"] for b in bodies],
                         [{"title": "Ghosted", "body": "Test push"}, None, None])
        self.assertEqual(len(result.acks), 250)
        self.assertEqual(result.requests, 3)
        self.assertEqual(self.led.unacked_events(), [])
        self.assertEqual(len(self.worker.items), 250)

    def test_runs_are_chunked_too(self) -> None:
        for n in range(150):
            self.led.append_run(dict(RUN, run_id="run-%04d" % n))
        self.led.append_run(dict(RUN, run_id="run-0003"))  # a duplicate line is sent once
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual([len(b["runs"]) for b in self.bodies()], [100, 50])
        self.assertEqual(len(result.runs_acked), 150)
        self.assertEqual(self.led.unacked_runs(), [])

    def test_a_failure_midway_keeps_the_rest_queued(self) -> None:
        self.add_many(150)
        self.worker.overrides[2] = Response(500, json={"error": "d1_error"})
        result = self.sync()
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.error)
        self.assertEqual((result.requests, len(result.acks)), (2, 100))
        self.assertEqual(len(self.led.unacked_events()), 50)
        result = self.sync()  # the next run sends the rest
        self.sync_ok(result)
        self.assertEqual(len(self.bodies()[-1]["events"]), 50)
        self.assertEqual(self.led.unacked_events(), [])


class FailureTest(RemoteCase):
    def test_network_failure_keeps_events_then_a_later_success_sends_them(self) -> None:
        events = self.add_many(3)
        self.led.append_run(RUN)
        dead = RemoteAdapter(self.make_cfg("http://127.0.0.1:%d" % closed_port()), ENV, ledger=self.led)
        result = self.sync(dead)
        self.assertFalse(result.ok)
        self.assertIn("cannot reach http://127.0.0.1", result.error)
        self.assertIn("connection refused", result.error)
        self.assertEqual((result.status, result.acks, result.merged), (None, {}, 0))
        self.assertEqual(len(self.led.unacked_events()), 3)
        self.assertEqual(len(self.led.unacked_runs()), 1)
        self.sync_ok(self.sync())
        self.assertEqual([ev["event_id"] for ev in self.bodies()[0]["events"]],
                         [ev["event_id"] for ev in events])
        self.assertEqual((self.led.unacked_events(), self.led.unacked_runs()), ([], []))

    def test_server_errors_keep_events(self) -> None:
        self.add_many(2)
        self.srv.queue(Response(503, body=b"<html>busy</html>"))
        result = self.sync()
        self.assertFalse(result.ok)
        self.assertIn("server error", result.error)
        self.assertNotIn("busy", result.error)
        self.assertEqual(len(self.led.unacked_events()), 2)

    def test_a_lost_response_is_replayed_as_duplicates(self) -> None:
        self.add_many(2)
        self.worker.drop_after_apply = 1
        result = self.sync()
        self.assertFalse(result.ok)
        self.assertEqual(len(self.led.unacked_events()), 2)
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual({ack["result"] for ack in result.acks.values()}, {"duplicate"})
        self.assertEqual(len(self.worker.items), 2)  # replaying created nothing new
        self.assertEqual(self.led.unacked_events(), [])
        self.assertTrue(all(it["synced"] for it in self.led.items().values()))

    def test_answers_that_are_not_the_sync_json(self) -> None:
        self.add()
        for response, needle in ((Response(200, body=b"<html>Sign in</html>", content_type="text/html"), "not JSON"),
                                 (Response(200, json=[1, 2]), "not JSON"),
                                 (Response(200, json={"acks": []}), "acks is not an object"),
                                 (Response(200, json={"acks": {}, "items": {}}), "items is not a list"),
                                 (Response(404, json={"error": "not_found"}), "rejected the sync (HTTP 404: not_found)")):
            with self.subTest(needle=needle):
                self.srv.queue(response)
                result = self.sync()
                self.assertFalse(result.ok)
                self.assertIn(needle, result.error)
                self.assertEqual(len(self.led.unacked_events()), 1)

    def test_unacknowledged_events_are_reported_and_resent(self) -> None:
        self.add_many(2)
        self.worker.ignore = lambda ev: True
        result = self.sync()
        self.assertFalse(result.ok)
        self.assertIn("did not acknowledge 2 events", result.error)
        self.assertEqual(len(self.led.unacked_events()), 2)
        self.worker.ignore = lambda ev: False
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual({ack["result"] for ack in result.acks.values()}, {"applied"})

    def test_a_busy_ledger_is_a_failed_sync_not_a_crash(self) -> None:
        self.add_many(2)
        busy = L.LedgerBusy("another ghosted process is holding the ledger lock (waited 10.0 s)")
        with mock.patch.object(self.led, "record_acks", side_effect=busy):
            result = self.sync()
        self.assertFalse(result.ok)
        self.assertIn("could not update the local ledger", result.error)
        self.assertIn("holding the ledger lock", result.error)
        self.assertEqual(len(self.led.unacked_events()), 2)
        result = self.sync()  # the server already applied them: duplicates, nothing new
        self.sync_ok(result)
        self.assertEqual({ack["result"] for ack in result.acks.values()}, {"duplicate"})
        self.assertEqual(self.led.unacked_events(), [])

    def test_malformed_values_in_an_answer_do_not_crash(self) -> None:
        _item, event = self.add()
        self.srv.queue(Response(200, json={
            "acks": {event["event_id"]: {"result": "applied", "item_id": ["x"], "rev": "2"}},
            "runs_acked": [{"run_id": "x"}, None, 5],
            "items": [{"id": ["GS-0001"]}, "junk", {"id": None}],
            "max_seq": "12",
            "notifications": [{"id": [1], "dedup_key": {"k": 1}, "ok": 1}, "junk"],
            "test_push": "yes", "pair": ["code"]}))
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.acks[event["event_id"]],
                         {"result": "applied", "item_id": "GS-0001", "rev": None, "error": None})
        self.assertEqual((result.runs_acked, result.merged, result.mirrored, result.test_push, result.pair),
                         ([], 0, 0, None, None))
        self.assertEqual(self.led.unacked_events(), [])

    def test_only_valid_acks_for_events_sent_are_recorded(self) -> None:
        _item, event = self.add()
        stranger = str(uuid.uuid4())
        self.srv.queue(Response(200, json={
            "acks": {event["event_id"]: {"result": "maybe"}, stranger: {"result": "applied"}},
            "runs_acked": ["run-never-sent"], "items": [], "max_seq": 0, "notifications": []}))
        result = self.sync()
        self.assertFalse(result.ok)
        self.assertEqual(result.acks, {})
        self.assertEqual(result.runs_acked, [])
        self.assertEqual(len(self.led.unacked_events()), 1)
        self.assertNotIn(stranger, self.led.acks()["events"])


class AckTest(RemoteCase):
    def test_a_stale_status_reports_the_phones_status_and_the_server_wins(self) -> None:
        item, _ = self.add()
        self.sync_ok(self.sync())
        self.worker.phone(item["id"], "snoozed", snooze_until="2026-10-08T09:00")
        _local, status_event = self.led.set_status(item["id"], "done", now=NOW)  # carries rev 1
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.acks[status_event["event_id"]]["result"], "stale")
        self.assertEqual(result.acks[status_event["event_id"]]["rev"], 2)
        self.assertEqual(result.stale, [(item["id"], "snoozed")])
        local = self.led.get(item["id"])
        self.assertEqual((local["status"], local["rev"], local["snooze_until"]),
                         ("snoozed", 2, "2026-10-08T09:00"))
        self.assertEqual(self.led.unacked_events(), [])  # a stale ack is final

    def test_two_local_changes_after_a_phone_dismiss_keep_the_dismiss(self) -> None:
        # `ghosted snooze` (event rev 1) then `ghosted done` (event rev 2, the Mac's own count) before a
        # sync, while the phone dismissed the item (rev 2 on the server): the Worker answers both
        # stale (a later status event for an item resting on a stale one), the Dismiss stands and
        # the Mac takes the server's version; one stale line per item.
        item, _ = self.add()
        self.sync_ok(self.sync())
        self.worker.phone(item["id"], "dismissed", dismiss_reason="not_real", closed_by="phone",
                          closed_at="2026-10-05T15:30:00Z")
        _l1, snooze = self.led.set_status(item["id"], "snoozed", now=NOW, snooze_until="2026-10-06T09:00")
        _l2, done = self.led.set_status(item["id"], "done", now=NOW)
        self.assertEqual((snooze["item"]["rev"], done["item"]["rev"]), (1, 2))
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual([result.acks[e["event_id"]]["result"] for e in (snooze, done)], ["stale", "stale"])
        self.assertEqual(result.stale, [(item["id"], "dismissed")])
        self.assertEqual(self.worker.items[item["id"]]["status"], "dismissed")
        local = self.led.get(item["id"])
        self.assertEqual((local["status"], local["rev"], local["dismiss_reason"]), ("dismissed", 2, "not_real"))
        self.assertEqual(self.led.unacked_events(), [])

    def test_a_resnooze_after_a_phone_done_is_stale(self) -> None:
        item, _ = self.add()
        self.sync_ok(self.sync())
        self.led.set_status(item["id"], "snoozed", now=NOW, snooze_until="2026-10-06T09:00")
        self.sync_ok(self.sync())
        self.worker.phone(item["id"], "done", closed_by="phone", closed_at="2026-10-05T15:30:00Z")
        _local, resnooze = self.led.set_status(item["id"], "snoozed", now=NOW, snooze_until="2026-10-09T09:00")
        self.assertEqual(resnooze["item"]["rev"], 2)  # one event: a re-snooze is one change
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.acks[resnooze["event_id"]]["result"], "stale")
        self.assertEqual((self.led.get(item["id"])["status"], self.led.get(item["id"])["rev"]), ("done", 3))

    def test_a_local_done_the_server_applies(self) -> None:
        item, _ = self.add()
        self.sync_ok(self.sync())
        self.led.set_status(item["id"], "done", now=NOW)
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.stale, [])
        self.assertEqual(self.worker.items[item["id"]]["status"], "done")
        self.assertEqual((self.led.get(item["id"])["status"], self.led.get(item["id"])["rev"]), ("done", 2))

    def test_invalid_acks_are_final_and_listed(self) -> None:
        item, event = self.add()
        self.srv.queue(Response(200, json={
            "acks": {event["event_id"]: {"result": "invalid", "item_id": item["id"], "rev": None,
                                         "error": "title fails the sanitizer"}},
            "runs_acked": [], "items": [], "max_seq": 0, "notifications": []}))
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.invalid, [(event["event_id"], item["id"], "title fails the sanitizer")])
        self.assertEqual(self.led.unacked_events(), [])
        self.assertFalse(self.led.get(item["id"])["synced"])


class MergeTest(RemoteCase):
    def test_phone_changes_are_merged(self) -> None:
        first, _ = self.add()
        second, _ = self.add()
        self.sync_ok(self.sync())
        self.worker.phone(first["id"], "done", closed_by="phone", closed_at="2026-10-05T15:30:00Z")
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.merged, 1)
        local = self.led.get(first["id"])
        self.assertEqual((local["status"], local["rev"], local["closed_by"], local["closed_at"]),
                         ("done", 2, "phone", "2026-10-05T15:30:00Z"))
        self.assertEqual(local["local_title"], first["local_title"])  # Mac-only fields stay
        self.assertEqual(self.led.get(second["id"])["status"], "open")
        self.assertEqual(self.sync().merged, 0)  # nothing new the second time

    def test_unknown_server_items_are_added_and_next_id_moves_past_max_seq(self) -> None:
        self.worker.items["GS-0040"] = dict(
            STATUS_FIELDS, id="GS-0040", kind="decision", project="home",
            title="home: follow up on the budget call", owner="me", due_at=None,
            review_at="2026-10-12", created_at="2026-10-01T15:00:00Z")
        self.worker.max_seq = 41
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.merged, 1)
        local = self.led.get("GS-0040")
        self.assertEqual(local["local_title"], "home: follow up on the budget call")
        self.assertTrue(local["synced"])
        self.assertEqual(self.add()[0]["id"], "GS-0042")

    def test_a_status_change_the_server_has_not_answered_keeps_its_local_value(self) -> None:
        item, _ = self.add()
        self.sync_ok(self.sync())
        self.led.set_status(item["id"], "done", now=NOW)
        self.worker.ignore = lambda ev: ev["type"] == "item.status"
        result = self.sync()
        self.assertFalse(result.ok)  # one event unanswered
        self.assertEqual(self.worker.items[item["id"]]["status"], "open")
        self.assertEqual(self.led.get(item["id"])["status"], "done")  # not flipped back to open
        self.worker.ignore = lambda ev: False
        self.sync_ok(self.sync())
        self.assertEqual(self.worker.items[item["id"]]["status"], "done")
        self.assertEqual((self.led.get(item["id"])["status"], self.led.get(item["id"])["rev"]), ("done", 2))


class NotificationMirrorTest(RemoteCase):
    ROWS = [
        {"id": 1, "dedup_key": "digest:2026-10-05", "sent_at": "2026-10-05T14:15:30Z", "kind": "digest",
         "item_ids": ["GS-0001"], "ok": 1, "local_date": "2026-10-05", "channel": "webpush", "error": None},
        {"id": 2, "dedup_key": "advance:GS-0002", "sent_at": "2026-10-05T14:16:00Z", "kind": "advance",
         "item_ids": '["GS-0002"]', "ok": 0, "local_date": "2026-10-05", "channel": "webpush",
         "error": "BadDeviceToken"},
        {"id": 3, "dedup_key": "deadman:none", "sent_at": "2026-10-05T14:17:00Z", "kind": "deadman",
         "item_ids": [], "ok": None, "local_date": "2026-10-05", "channel": None, "error": None},
    ]

    def test_server_rows_are_mirrored_once(self) -> None:
        self.worker.notifications = copy.deepcopy(self.ROWS)
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.mirrored, 2)  # the third is still being delivered
        rows = self.led.notifications()
        self.assertEqual([r["dedup_key"] for r in rows], ["digest:2026-10-05", "advance:GS-0002"])
        self.assertEqual(rows[0], {"id": 1, "dedup_key": "digest:2026-10-05", "kind": "digest",
                                   "item_ids": ["GS-0001"], "sent_at": "2026-10-05T14:15:30Z",
                                   "local_date": "2026-10-05", "ok": 1, "channel": "webpush", "error": None,
                                   "source": "remote", "mirrored_at": "2026-10-05T15:00:00Z"})
        self.assertEqual((rows[1]["item_ids"], rows[1]["ok"], rows[1]["error"]),
                         (["GS-0002"], 0, "BadDeviceToken"))
        result = self.sync()
        self.assertEqual(result.mirrored, 0)
        self.assertEqual(len(self.led.notifications()), 2)
        self.assertEqual(self.bodies()[-1]["want"]["notifications_since"], "2026-10-04T14:16:00Z")
        self.worker.notifications[2]["ok"] = 1  # delivery recorded on the server
        self.assertEqual(self.sync().mirrored, 1)
        self.assertEqual([r["dedup_key"] for r in self.led.notifications()],
                         ["digest:2026-10-05", "advance:GS-0002", "deadman:none"])

    def test_a_row_the_ledger_already_has_is_not_duplicated(self) -> None:
        self.led.append_notification({"dedup_key": "digest:2026-10-05", "kind": "digest", "ok": True,
                                      "sent_at": "2026-10-05T14:15:31Z", "item_ids": ["GS-0001"],
                                      "local_date": "2026-10-05", "channel": "ntfy"})
        self.worker.notifications = copy.deepcopy(self.ROWS[:1]) + [dict(self.ROWS[0], dedup_key=None)]
        result = self.sync()
        self.sync_ok(result)
        self.assertEqual(result.mirrored, 1)  # only the key-less row, matched later by its server id
        self.assertEqual(self.sync().mirrored, 0)
        self.assertEqual(len(self.led.notifications()), 2)


class WantTest(RemoteCase):
    def test_test_push_plumbing(self) -> None:
        result = self.sync(test_push={"title": "Ghosted", "body": "Test push"})
        self.sync_ok(result)
        self.assertEqual(self.bodies()[0]["want"]["test_push"], {"title": "Ghosted", "body": "Test push"})
        self.assertEqual(result.test_push, {"sent": 1, "failed": 0})

    def test_pair_and_revoke_plumbing(self) -> None:
        result = self.sync(pair={"ttl_minutes": 10})
        self.sync_ok(result)
        self.assertEqual(self.bodies()[-1]["want"]["pair"], {"ttl_minutes": 10})
        self.assertEqual(result.pair, {"code": "K7QF-2M9X", "expires_at": "2026-10-05T15:10:00Z"})
        self.sync(pair=True)
        self.assertEqual(self.bodies()[-1]["want"]["pair"], {"ttl_minutes": 10})
        result = self.sync(revoke_devices=True)
        self.sync_ok(result)
        self.assertIs(self.bodies()[-1]["want"]["revoke_devices"], True)
        self.assertEqual((result.revoked, self.worker.devices), (2, 0))
        result = self.sync()
        want = self.bodies()[-1]["want"]
        self.assertNotIn("pair", want)
        self.assertNotIn("revoke_devices", want)
        self.assertEqual((result.pair, result.test_push, result.revoked), (None, None, None))

    def test_extras_ride_on_the_first_request_only(self) -> None:
        self.add_many(101)
        self.sync_ok(self.sync(pair=True, revoke_devices=True, test_push={"title": "t", "body": "b"}))
        first, second = self.bodies()
        self.assertEqual(set(first["want"]), {"items", "notifications_since", "test_push", "pair", "revoke_devices"})
        self.assertEqual(second["want"], {"items": True, "notifications_since": None, "test_push": None})

    def test_no_pairing_code_outside_device_mode(self) -> None:
        self.worker.pairing = False
        result = self.sync(pair=True)
        self.sync_ok(result)
        self.assertIsNone(result.pair)

    def test_bad_arguments_raise(self) -> None:
        for pair in (0, -5, "10", {"ttl_minutes": "x"}):
            with self.subTest(pair=pair):
                with self.assertRaises(ValueError):
                    self.sync(pair=pair)
        with self.assertRaises(TypeError):
            self.sync(test_push="hello")
        self.assertEqual(self.srv.requests, [])

    def test_test_push_text_is_bounded(self) -> None:
        self.sync_ok(self.sync(test_push={"title": "T" * 300, "body": "☕" * 2000}))
        pushed = self.bodies()[0]["want"]["test_push"]
        self.assertEqual(len(pushed["title"]), remote.TEST_PUSH_TITLE_MAX)
        self.assertLessEqual(len(pushed["body"].encode("utf-8")), remote.TEST_PUSH_BODY_MAX_BYTES)
        self.sync_ok(self.sync(test_push={}))
        self.assertEqual(self.bodies()[-1]["want"]["test_push"],
                         {"title": "Ghosted", "body": remote.DEFAULT_TEST_BODY})


class SendTest(RemoteCase):
    def test_send_is_one_sync_with_a_test_push(self) -> None:
        _item, event = self.add()
        result = self.adapter.send([], title="Ghosted", body="Test push from ghosted", kind="test")
        self.assertEqual((result.ok, result.channel, result.error, result.status), (True, "remote", None, 200))
        [body] = self.bodies()
        self.assertEqual(body["want"]["test_push"], {"title": "Ghosted", "body": "Test push from ghosted"})
        self.assertEqual([e["event_id"] for e in body["events"]], [event["event_id"]])
        self.assertEqual(self.led.unacked_events(), [])

    def test_no_phone_subscribed(self) -> None:
        self.worker.subscriptions = 0
        result = self.adapter.send([], title="Ghosted", body="hi", kind="test")
        self.assertFalse(result.ok)
        self.assertIn("Enable notifications", result.error)

    def test_send_reports_sync_failures(self) -> None:
        adapter = RemoteAdapter(self.cfg, dict(ENV, GHOSTED_SERVICE_TOKEN_SECRET="nope"), ledger=self.led)
        result = adapter.send([], title="Ghosted", body="hi", kind="test")
        self.assertFalse(result.ok)
        self.assertIn("service token not accepted", result.error)
        self.assertEqual(result.status, 302)

    def test_send_refuses_a_local_title(self) -> None:
        item, _ = self.add()
        result = self.adapter.send([item], title="Ghosted", body=item["local_title"], kind="test")
        self.assertFalse(result.ok)
        self.assertIn("local", result.error)
        self.assertEqual(self.srv.requests, [])


class PrivacyTest(RemoteCase):
    def test_no_local_title_or_mac_only_field_is_ever_uploaded(self) -> None:
        item, _ = self.add(consequential=True)
        self.led.upsert_interrupted(SESSION, "123:abc", "boathouse", LIMIT_TITLE,
                                    "ended on usage limit · clean tree", False,
                                    "cd ~/workspace/boathouse && claude --resume " + SESSION,
                                    "limit_meta", NOW, now=NOW)
        self.led.set_status(item["id"], "snoozed", now=NOW, snooze_until="2026-10-08T09:00")
        self.led.append_run(dict(RUN, error="failed for bob@example.com " + LOCAL_MARKER))
        self.sync_ok(self.sync())
        self.assertTrue(self.adapter.send([item], title="Ghosted", body=None, kind="test").ok)
        self.assertEqual(len(self.srv.requests), 2)
        for raw in self.srv.bodies():
            self.assertNotIn(LOCAL_MARKER.encode(), raw)
            for key in L.MAC_ONLY_FIELDS:
                self.assertNotIn(('"%s"' % key).encode(), raw)
        self.assertEqual(self.bodies()[0]["runs"][0]["error"], remote.RUN_ERROR_GENERIC)

    def test_a_tampered_inbox_line_is_stripped_before_upload(self) -> None:
        event = {"event_id": str(uuid.uuid4()), "type": "item.add", "at": "2026-10-05T15:00:00Z",
                 "item": {"id": "GS-0099", "kind": "promise", "project": "home", "title": "home: renew",
                          "owner": "me", "due_at": None, "review_at": "2026-10-12",
                          "created_at": "2026-10-05T15:00:00Z", "local_title": LOCAL_MARKER,
                          "reason": "x", "consequential": True, "synced": False},
                 "debug": LOCAL_MARKER}
        L.append_jsonl(self.led.inbox_path, event)
        self.sync_ok(self.sync())
        [body] = self.bodies()
        self.assertEqual(set(body["events"][0]), {"event_id", "type", "at", "item"})
        self.assertNotIn(LOCAL_MARKER.encode(), self.srv.requests[0].body)


class CheckTest(RemoteCase):
    def test_check_proves_url_and_token_without_touching_the_ledger(self) -> None:
        self.add()
        ok, message = self.adapter.check()
        self.assertTrue(ok, message)
        self.assertIn("reachable", message)
        self.assertIn("service token accepted", message)
        self.assertNotIn(TOKEN_ID, message)
        self.assertNotIn(TOKEN_SECRET, message)
        [body] = self.bodies()
        self.assertEqual((body["events"], body["runs"], body["want"]["items"]), ([], [], False))
        self.assertEqual(len(self.led.unacked_events()), 1)  # nothing recorded
        self.assertFalse(self.led.acks_path.exists())

    def test_check_failures(self) -> None:
        bad = RemoteAdapter(self.cfg, dict(ENV, GHOSTED_SERVICE_TOKEN_SECRET="nope"))
        ok, message = bad.check()
        self.assertFalse(ok)
        self.assertIn("service token not accepted", message)
        ok, message = RemoteAdapter(self.make_cfg(""), ENV, transport=_never).check()
        self.assertEqual((ok, message), (False, "remote: notify.remote.url is not set"))
        ok, message = RemoteAdapter(self.make_cfg(self.srv.url), {}, transport=_never).check()
        self.assertFalse(ok)
        self.assertIn("GHOSTED_SERVICE_TOKEN_ID and GHOSTED_SERVICE_TOKEN_SECRET missing", message)
        dead = RemoteAdapter(self.make_cfg("http://127.0.0.1:%d" % closed_port()), ENV)
        ok, message = dead.check()
        self.assertFalse(ok)
        self.assertIn("cannot reach", message)

    def test_check_flags_a_worker_in_another_timezone(self) -> None:
        # Due and snooze values are local wall times, so both sides must read them in one zone.
        # America/Menominee keeps Chicago's clock under another name. (The old alias US/Central is
        # no longer in the base tzdata on current Linux, so it cannot be the example.)
        for worker_tz, expect_ok in (("America/Chicago", True), ("America/Menominee", True),
                                     ("Europe/Berlin", False), ("Not/AZone", False)):
            self.worker.tz = worker_tz
            ok, message = self.adapter.check()
            self.assertEqual(ok, expect_ok, (worker_tz, message))
            if not expect_ok:
                self.assertIn("the Worker runs in %s and this Mac in America/Chicago" % worker_tz, message)
                self.assertIn("TZ_NAME", message)

    def test_same_zone(self) -> None:
        self.assertTrue(remote.same_zone("UTC", "UTC", NOW))
        self.assertTrue(remote.same_zone("America/Chicago", "America/Menominee", NOW))
        self.assertFalse(remote.same_zone("America/Chicago", "America/Mexico_City", NOW))  # no DST there
        self.assertFalse(remote.same_zone("America/Chicago", "", NOW))


if __name__ == "__main__":
    unittest.main()
