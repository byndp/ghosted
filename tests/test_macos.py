"""notify.macos: the Mac notification adapter (contract 16.5).

The runner is injected, so no banner ever appears. One test hands the escaped literal to the real
osascript inside `return ...` (no notification) to prove the escaping holds in AppleScript itself.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Sequence, Tuple

import notify
from core.config import Config
from notify import macos
from notify.macos import MacAdapter, applescript_string

EVIL = 'a "quoted" \\ back\nslash" & (do shell script "touch /tmp/ghosted-pwned") & "'


class FakeRunner:
    def __init__(self, result: Tuple[int, str, str] = (0, "", "")) -> None:
        self.result = result
        self.calls: List[Tuple[List[str], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        self.calls.append((list(argv), timeout))
        return self.result


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), adapter="macos", timezone="UTC")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def adapter(self, runner: Any = None, osascript: str = "/usr/bin/osascript") -> MacAdapter:
        return MacAdapter(self.cfg, {}, runner=runner or FakeRunner(), osascript=osascript)


class EscapingTests(unittest.TestCase):
    def test_backslash_and_quote_are_escaped_and_control_characters_flattened(self) -> None:
        self.assertEqual(applescript_string('say "hi" \\o/'), '"say \\"hi\\" \\\\o/"')
        self.assertEqual(applescript_string("two\nlines\r\tand\x00more  spaces"), '"two lines and more spaces"')
        self.assertEqual(applescript_string("line\u2028sep"), '"line sep"')
        self.assertEqual(macos.script("T", "B"), 'display notification "B" with title "T"')

    @unittest.skipUnless(os.path.exists("/usr/bin/osascript"), "macOS only")
    def test_the_literal_survives_real_applescript(self) -> None:
        proc = subprocess.run(["/usr/bin/osascript", "-e", "return " + applescript_string(EVIL)],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        if proc.returncode != 0:
            self.skipTest("osascript is not usable here")
        self.assertEqual(proc.stdout.decode("utf-8").rstrip("\n"), " ".join(EVIL.split()))


class SendTests(Base):
    def test_send_runs_osascript_with_an_argument_list(self) -> None:
        runner = FakeRunner()
        res = self.adapter(runner).send([{"id": "GS-0001", "title": "shop: decide the cart"}],
                                        title="Ghosted", body=None, kind="digest")
        self.assertEqual((res.ok, res.channel, res.error), (True, "macos", None))
        (argv, timeout), = runner.calls
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertEqual(len(argv), 3)
        self.assertEqual(timeout, 10.0)
        self.assertTrue(argv[2].startswith('display notification "1 open: shop: decide the cart'))
        self.assertTrue(argv[2].endswith('with title "Ghosted"'))

    def test_hostile_text_stays_inside_the_literals(self) -> None:
        runner = FakeRunner()
        self.adapter(runner).send([], title=EVIL, body=EVIL, kind="test")
        script = runner.calls[0][0][2]
        literal = applescript_string(" ".join(EVIL.split()))
        self.assertEqual(script, "display notification %s with title %s" % (literal, literal))

    def test_a_local_title_is_refused_before_anything_runs(self) -> None:
        runner = FakeRunner()
        items = [{"id": "GS-0002", "title": "client: send the invoice",
                  "local_title": "Acme Corp: send invoice 4411 to Jane"}]
        res = self.adapter(runner).send(items, title="Ghosted", body="Acme Corp: send invoice 4411 to Jane",
                                        kind="due")
        self.assertFalse(res.ok)
        self.assertIn("local (Mac-only) title", res.error)
        self.assertEqual(runner.calls, [])

    def test_failures_are_short_results(self) -> None:
        cases = (((1, "", "execution error: Not authorized (-1743)\n"), "osascript exited 1: execution error: "
                  "Not authorized (-1743)"),
                 ((124, "", "timed out"), "osascript timed out after 10 s"),
                 ((2, "", ""), "osascript exited 2"))
        for result, error in cases:
            with self.subTest(result=result):
                res = self.adapter(FakeRunner(result)).send([], title=None, body="x", kind="test")
                self.assertEqual((res.ok, res.error), (False, error))

    def test_a_runner_that_raises_is_still_a_result(self) -> None:
        def boom(argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
            raise RuntimeError("secret detail")

        res = self.adapter(boom).send([], title=None, body="x", kind="test")
        self.assertEqual((res.ok, res.error), (False, "osascript failed (RuntimeError)"))

    def test_long_text_is_capped(self) -> None:
        runner = FakeRunner()
        self.adapter(runner).send([], title="t" * 500, body="b" * 5000, kind="test")
        script = runner.calls[0][0][2]
        self.assertLess(len(script), macos.TITLE_MAX_CHARS + macos.BODY_MAX_CHARS + 60)

    def test_without_osascript(self) -> None:
        runner = FakeRunner()
        adapter = self.adapter(runner, osascript="")
        res = adapter.send([], title=None, body="x", kind="test")
        self.assertFalse(res.ok)
        self.assertIn("osascript not found", res.error)
        self.assertEqual(runner.calls, [])
        self.assertEqual(adapter.reachable()[0], False)
        ok, message = adapter.check()
        self.assertFalse(ok)
        self.assertTrue(message.startswith("macos"))


class WiringTests(Base):
    def test_reachable_and_check_with_osascript(self) -> None:
        adapter = self.adapter()
        self.assertEqual(adapter.reachable(), (True, None))
        ok, message = adapter.check()
        self.assertTrue(ok)
        self.assertTrue(message.startswith("macos: notifications via /usr/bin/osascript"))
        self.assertIn("Script Editor", message)

    def test_get_adapter_builds_it(self) -> None:
        adapter = notify.get_adapter(self.cfg, {})
        self.assertIsInstance(adapter, MacAdapter)
        self.assertEqual((adapter.name, adapter.owns_schedule), ("macos", False))
        self.assertIn("macos", notify.ADAPTERS)
        self.assertEqual(repr(adapter), "<macos adapter>")

    def test_find_osascript_falls_back_to_path(self) -> None:
        if os.path.exists(macos.OSASCRIPT):
            self.assertEqual(macos.find_osascript(lambda name: None), macos.OSASCRIPT)
        else:
            self.assertEqual(macos.find_osascript(lambda name: "/opt/bin/" + name), "/opt/bin/osascript")
            self.assertIsNone(macos.find_osascript(lambda name: None))

    def test_default_runner_never_raises(self) -> None:
        self.assertEqual(macos.default_runner(["/nonexistent/osascript-ghosted-test"], 1.0)[0], 127)


if __name__ == "__main__":
    unittest.main()
