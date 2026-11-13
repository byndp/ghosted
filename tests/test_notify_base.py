from __future__ import annotations

import dataclasses
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import notify
from core import __version__
from core.config import Config
from notify import base
from notify.base import Adapter, SendResult, SyncResult, TransportError, http_request
from notify.ntfy import NtfyAdapter
from notify.pushover import PushoverAdapter
from notify.remote import RemoteAdapter
from tests.fakeserver import FakeServer, Response, closed_port

LOCAL_MARKER = "LOCAL-ONLY-MARKER-7f3a9c"


def make_cfg(tmp: str, **kw) -> Config:
    root = Path(tmp)
    kw.setdefault("timezone", "America/Chicago")
    return Config(home=root / "ghosted-home", projects_dir=root / "projects", **kw)


class TempHomeCase(unittest.TestCase):
    """A temp GHOSTED_HOME for every test; never the real ~/.ghosted."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        patcher = mock.patch.dict(os.environ, {"GHOSTED_HOME": str(Path(tmp.name) / "ghosted-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GHOSTED_DEV_INSECURE", None)


class GetAdapterTest(TempHomeCase):
    def test_none_gives_no_adapter(self) -> None:
        self.assertIsNone(notify.get_adapter(make_cfg(self.tmp, adapter="none"), {}))
        self.assertIsNone(notify.get_adapter(make_cfg(self.tmp), {}))  # the default

    def test_each_name_selects_its_adapter(self) -> None:
        cases = (("ntfy", NtfyAdapter, False), ("pushover", PushoverAdapter, False),
                 ("remote", RemoteAdapter, True))
        for name, cls, owns in cases:
            with self.subTest(adapter=name):
                adapter = notify.get_adapter(make_cfg(self.tmp, adapter=name), {})
                self.assertIsInstance(adapter, cls)
                self.assertIsInstance(adapter, Adapter)
                self.assertEqual(adapter.name, name)
                self.assertIs(adapter.owns_schedule, owns)

    def test_name_is_case_insensitive(self) -> None:
        self.assertIsInstance(notify.get_adapter(make_cfg(self.tmp, adapter=" NTFY "), {}), NtfyAdapter)

    def test_unknown_name_is_a_clear_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            notify.get_adapter(make_cfg(self.tmp, adapter="carrier-pigeon"), {})
        self.assertIn("carrier-pigeon", str(ctx.exception))
        self.assertIn("ntfy", str(ctx.exception))

    def test_incomplete_settings_do_not_raise(self) -> None:
        # ghosted doctor needs an adapter object to call check() on, even when nothing is set up yet.
        for name in ("ntfy", "pushover", "remote"):
            with self.subTest(adapter=name):
                adapter = notify.get_adapter(make_cfg(self.tmp, adapter=name), {})
                ok, message = adapter.check()
                self.assertFalse(ok)
                self.assertTrue(message.startswith(name))

    def test_env_none_reads_the_env_file(self) -> None:
        cfg = make_cfg(self.tmp, adapter="pushover")
        cfg.home.mkdir(parents=True)
        cfg.env_file.write_text("PUSHOVER_TOKEN=tok-from-file\nPUSHOVER_USER=user-from-file\n")
        adapter = notify.get_adapter(cfg, None)
        self.assertEqual((adapter.token, adapter.user), ("tok-from-file", "user-from-file"))

    def test_options_reach_the_constructor(self) -> None:
        adapter = notify.get_adapter(make_cfg(self.tmp, adapter="ntfy"), {}, retry_delay=0.25)
        self.assertEqual(adapter.retry_delay, 0.25)

    def test_base_adapter_is_abstract_and_repr_hides_settings(self) -> None:
        adapter = Adapter()
        with self.assertRaises(NotImplementedError):
            adapter.send([], title="t", body="b", kind="digest")
        with self.assertRaises(NotImplementedError):
            adapter.check()
        ntfy = NtfyAdapter(make_cfg(self.tmp, ntfy_topic="ghosted-secret-topic-abc"), {})
        self.assertNotIn("ghosted-secret-topic-abc", repr(ntfy))


class ResultTypesTest(unittest.TestCase):
    def test_send_result_fields(self) -> None:
        result = SendResult(True, "ntfy")
        self.assertEqual((result.ok, result.channel, result.error, result.status), (True, "ntfy", None, None))
        self.assertEqual(SendResult(False, "pushover", "boom", 500).status, 500)
        self.assertEqual([f.name for f in dataclasses.fields(SendResult)], ["ok", "channel", "error", "status"])

    def test_sync_result_interface_order_and_defaults(self) -> None:
        names = [f.name for f in dataclasses.fields(SyncResult)]
        self.assertEqual(names[:7], ["ok", "error", "acks", "stale", "merged", "test_push", "pair"])
        one, two = SyncResult(False), SyncResult(True)
        self.assertEqual((one.error, one.acks, one.stale, one.merged, one.test_push, one.pair),
                         (None, {}, [], 0, None, None))
        one.acks["e"] = {"result": "applied"}
        one.stale.append(("GS-0001", "open"))
        self.assertEqual((two.acks, two.stale), ({}, []))  # no shared mutable defaults


class HttpRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = FakeServer().start()
        self.addCleanup(self.srv.stop)

    def test_redirects_are_returned_not_followed(self) -> None:
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.srv.clear()
                self.srv.queue(Response(code, headers={"Location": self.srv.url + "/followed"}))
                resp = http_request(self.srv.url + "/api/sync", method="POST", body=b"{}")
                self.assertEqual(resp.status, code)
                self.assertEqual(resp.headers.get("location"), self.srv.url + "/followed")
                self.assertEqual([r.path for r in self.srv.requests], ["/api/sync"])

    def test_error_statuses_come_back_with_their_body(self) -> None:
        self.srv.queue(Response(429, json={"code": 42908}), Response(503, body=b"busy"))
        first = http_request(self.srv.url + "/")
        self.assertEqual((first.status, first.json()), (429, {"code": 42908}))
        second = http_request(self.srv.url + "/")
        self.assertEqual((second.status, second.body, second.json()), (503, b"busy", None))

    def test_headers_and_default_user_agent(self) -> None:
        http_request(self.srv.url + "/h", method="POST", body=b"x", headers={"X-Thing": "1"})
        req = self.srv.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.header("x-thing"), "1")
        self.assertEqual(req.header("user-agent"), "ghosted/%s" % __version__)
        self.assertEqual(req.body, b"x")

    def test_connection_refused(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            http_request("http://127.0.0.1:%d/" % closed_port(), timeout=5)
        self.assertEqual(str(ctx.exception), "connection refused")
        self.assertTrue(ctx.exception.retryable)

    def test_dropped_connection(self) -> None:
        self.srv.queue(Response(drop=True))
        with self.assertRaises(TransportError) as ctx:
            http_request(self.srv.url + "/", timeout=5)
        self.assertTrue(ctx.exception.retryable)
        self.assertIn(str(ctx.exception), ("connection closed without a response", "connection reset"))

    def test_timeout(self) -> None:
        self.srv.queue(Response(200, json={}, delay=1.5))
        with self.assertRaises(TransportError) as ctx:
            http_request(self.srv.url + "/", timeout=0.2)
        self.assertEqual(str(ctx.exception), "timed out")

    def test_body_cap_marks_truncation(self) -> None:
        self.srv.queue(Response(200, body=b'{"a": "' + b"x" * 5000 + b'"}'))
        resp = http_request(self.srv.url + "/", max_bytes=100)
        self.assertTrue(resp.truncated)
        self.assertEqual(len(resp.body), 100)
        self.assertIsNone(resp.json())

    def test_unbuildable_header_never_quotes_its_value(self) -> None:
        with self.assertRaises(TransportError) as ctx:
            http_request(self.srv.url + "/", headers={"Authorization": "Bearer abc\r\nX-Evil: 1"})
        self.assertFalse(ctx.exception.retryable)
        self.assertNotIn("abc", str(ctx.exception))
        self.assertEqual(self.srv.requests, [])

    def test_bad_urls_are_not_retryable_and_send_nothing(self) -> None:
        for url in ("http://[bad", "notascheme://x", "file:///etc/hosts", "ftp://127.0.0.1/x",
                    "https://", self.srv.url + "/a b"):
            with self.subTest(url=url):
                with self.assertRaises(TransportError) as ctx:
                    http_request(url, timeout=5)
                self.assertFalse(ctx.exception.retryable)
                self.assertIn("could not be built", str(ctx.exception))
        self.assertEqual(self.srv.requests, [])

    def test_loopback_ignores_proxy_settings(self) -> None:
        dead_proxy = "http://127.0.0.1:%d" % closed_port()
        with mock.patch.dict(os.environ, {"http_proxy": dead_proxy, "HTTP_PROXY": dead_proxy,
                                          "no_proxy": "", "NO_PROXY": ""}):
            resp = http_request(self.srv.url + "/direct", timeout=5)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.srv.requests[0].path, "/direct")


class DescribeErrorTest(unittest.TestCase):
    def test_short_reasons(self) -> None:
        cases = [
            (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), "connection refused"),
            (urllib.error.URLError(socket.timeout("timed out")), "timed out"),
            (socket.timeout("timed out"), "timed out"),
            (urllib.error.URLError(socket.gaierror(8, "nodename nor servname")), "DNS lookup failed"),
            (ConnectionResetError(54, "reset"), "connection reset"),
            (urllib.error.URLError("no host given"), "no host given"),
        ]
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(base.describe_network_error(exc), expected)


class ParseUrlTest(unittest.TestCase):
    def test_accepted(self) -> None:
        for url in ("https://ntfy.sh", "https://ntfy.sh/", "https://ghosted.x.workers.dev",
                    "http://localhost:8787", "http://127.0.0.1:9/", "HTTPS://Example.COM"):
            with self.subTest(url=url):
                parts, problem = base.parse_url(url, "the url")
                self.assertIsNone(problem)
                self.assertIsNotNone(parts)

    def test_refused(self) -> None:
        cases = {
            "": "is not set",
            "   ": "is not set",
            "http://ntfy.sh": "https://",
            "http://192.168.1.5": "https://",
            "ftp://ntfy.sh": "https://",
            "ntfy.sh": "https://",
            "https://ntfy.sh/?topic=x": "query",
            "https://ntfy.sh:notaport": "valid URL",
            "https://ghosted.example.workers.dev/my path": "spaces",
            "https://ntfy.sh/\x7f": "control characters",
        }
        for url, needle in cases.items():
            with self.subTest(url=url):
                parts, problem = base.parse_url(url, "the url")
                self.assertIsNone(parts)
                self.assertIn(needle, problem)

    def test_credentials_in_the_url_are_refused_and_never_echoed(self) -> None:
        parts, problem = base.parse_url("https://bob:hunter2-pass@ntfy.example.com", "notify.ntfy.server")
        self.assertIsNone(parts)
        self.assertIn("user name or password", problem)
        self.assertNotIn("hunter2", problem)

    def test_origin_drops_path(self) -> None:
        parts, _ = base.parse_url("https://Example.com:8443/some/path", "u")
        self.assertEqual(base.origin(parts), "https://example.com:8443")
        parts, _ = base.parse_url("http://127.0.0.1:1234", "u")
        self.assertEqual(base.origin(parts), "http://127.0.0.1:1234")

    def test_is_web_url(self) -> None:
        self.assertTrue(base.is_web_url("https://ghosted.x.workers.dev/?n=3"))
        for bad in (None, "", "javascript:alert(1)", "https://x y", "mailto:a@b.c", "https://" + "a" * 3000):
            with self.subTest(url=bad):
                self.assertFalse(base.is_web_url(bad))


class TextHelpersTest(unittest.TestCase):
    def test_cap_utf8_cuts_on_a_character_boundary(self) -> None:
        text = "é" * 1000  # 2000 bytes
        out = base.cap_utf8(text, 1024)
        self.assertLessEqual(len(out.encode("utf-8")), 1024)
        self.assertTrue(out.endswith("…"))
        out.encode("utf-8").decode("utf-8")  # still valid
        self.assertEqual(base.cap_utf8("short", 1024), "short")

    def test_cap_chars(self) -> None:
        self.assertEqual(base.cap_chars("abcdef", 4), "abc…")
        self.assertEqual(base.cap_chars("abc", 4), "abc")

    def test_safe_text_filters_and_redacts(self) -> None:
        self.assertEqual(base.safe_text("invalid request: topic invalid"), "invalid request: topic invalid")
        self.assertEqual(base.safe_text("bad <script>x</script>\n"), "bad script x /script")
        self.assertNotIn("ghosted-topic-123", base.safe_text("topic ghosted-topic-123 is wrong", redact=("ghosted-topic-123",)))
        self.assertEqual(len(base.safe_text("x" * 500)), base.MAX_ERROR_TEXT)
        self.assertEqual(base.safe_text(None), "")
        self.assertNotIn("@", base.safe_text("mail bob@example.com"))


class PushTextTest(unittest.TestCase):
    ITEMS = [
        {"id": "GS-0001", "title": "home: pay the overdue water bill", "local_title": LOCAL_MARKER},
        {"id": "GS-0002", "title": "boathouse: session 10-01 ended on usage limit",
         "local_title": "boathouse: session 10-01 ended on usage limit"},
    ]

    def test_title_always_present(self) -> None:
        self.assertEqual(base.push_title(None), "Ghosted")
        self.assertEqual(base.push_title("  "), "Ghosted")
        self.assertEqual(base.push_title("Due in 24 h"), "Due in 24 h")

    def test_blank_body_is_built_from_public_titles(self) -> None:
        body = base.push_body(self.ITEMS, None, "Ghosted")
        self.assertEqual(body, "2 open: home: pay the overdue water bill · "
                               "boathouse: session 10-01 ended on usage limit")
        self.assertNotIn(LOCAL_MARKER, body)
        self.assertEqual(base.push_body(self.ITEMS, "given body", "t"), "given body")
        self.assertEqual(base.push_body([], "", "Ghosted"), "Ghosted")

    def test_items_may_be_a_dict_of_id_to_item(self) -> None:
        rows = base.iter_items({i["id"]: i for i in self.ITEMS})
        self.assertEqual([r["id"] for r in rows], ["GS-0001", "GS-0002"])
        self.assertEqual(base.iter_items(None), [])
        self.assertEqual(base.iter_items(["GS-0001", 3]), [])

    def test_leak_guard_passes_public_text(self) -> None:
        body = base.push_body(self.ITEMS, None, "Ghosted")
        self.assertIsNone(base.local_title_leak(self.ITEMS, "Ghosted", body, None))

    def test_leak_guard_refuses_a_local_title(self) -> None:
        reason = base.local_title_leak(self.ITEMS, "Ghosted", "1 open: " + LOCAL_MARKER)
        self.assertIn("local", reason)
        self.assertNotIn(LOCAL_MARKER, reason)
        self.assertIsNotNone(base.local_title_leak(self.ITEMS, LOCAL_MARKER, "body"))

    def test_leak_guard_ignores_public_or_generic_local_titles(self) -> None:
        items = [
            {"id": "GS-1", "title": "home: call the bank", "local_title": "call the bank"},  # inside its title
            {"id": "GS-2", "title": "proj: x", "local_title": "fix"},  # too short to judge
            {"id": "GS-3", "title": "proj: Ghosted readout", "local_title": "Ghosted"},
            {"id": "GS-4", "title": "proj: renew", "local_title": "Due in 24 h"},  # a product string
        ]
        body = base.push_body(items, None, "Ghosted")
        self.assertIsNone(base.local_title_leak(items, "Ghosted", body))
        self.assertIsNone(base.local_title_leak(items, "Due in 24 h", "proj: renew"))


if __name__ == "__main__":
    unittest.main()
