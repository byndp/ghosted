"""Tests for core.agents (contract 16.3 "Live state"). Synthetic listings and an injected runner
only: nothing here runs the real ``claude`` binary."""
from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from typing import Any, List, Optional, Sequence

from core import agents
from core.agents import LiveState

S1 = "11111111-0000-4000-8000-000000000001"
S2 = "11111111-0000-4000-8000-000000000002"
S3 = "11111111-0000-4000-8000-000000000003"
S4 = "11111111-0000-4000-8000-000000000004"
SECRET_NAME = "fix the billing export for acme corp"


def interactive(sid: str, status: str, pid: Any = 4242) -> dict:
    return {"sessionId": sid, "cwd": "/tmp/x", "kind": "interactive", "startedAt": 1762600000000,
            "name": SECRET_NAME, "pid": pid, "status": status}


def background(sid: str, state: str) -> dict:
    return {"id": "bg-" + sid[:8], "sessionId": sid, "cwd": "/tmp/y", "kind": "background",
            "startedAt": "2026-11-08T10:00:00Z", "name": SECRET_NAME, "state": state}


class Runner:
    def __init__(self, stdout: Any = b"[]", returncode: int = 0, exc: Optional[BaseException] = None) -> None:
        self.stdout, self.returncode, self.exc = stdout, returncode, exc
        self.calls: List[Any] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> Any:
        self.calls.append((list(argv), timeout))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout)


def which(name: str) -> Optional[str]:
    return "/opt/bin/" + name


class ClassifyLiveTests(unittest.TestCase):
    def test_states(self) -> None:
        self.assertEqual(agents.classify_live(interactive(S1, "busy")), "active")
        self.assertEqual(agents.classify_live(interactive(S1, "idle")), "open")
        self.assertIsNone(agents.classify_live(interactive(S1, "idle", pid=None)))
        self.assertIsNone(agents.classify_live(interactive(S1, "idle", pid=True)))
        self.assertIsNone(agents.classify_live(interactive(S1, "idle", pid=0)))
        for state in ("blocked", "waiting", "needs_input", "needs-input", "awaiting_input", "BLOCKED "):
            self.assertEqual(agents.classify_live(background(S2, state)), "waiting", state)
        for state in ("working", "running", "starting", "busy"):
            self.assertEqual(agents.classify_live(background(S2, state)), "active", state)
        for state in ("done", "failed", "idle", ""):
            self.assertIsNone(agents.classify_live(background(S2, state)), state)
        self.assertEqual(agents.classify_live(LiveState("interactive", "idle", 7)), "open")
        self.assertEqual(agents.classify_live(LiveState("background", "blocked")), "waiting")
        for junk in (None, 3, "busy", [], {}):
            self.assertIsNone(agents.classify_live(junk))

    def test_interactive_reads_status_background_reads_state(self) -> None:
        mixed = dict(interactive(S1, "idle"), state="blocked")
        self.assertEqual(agents.state_of(mixed).state, "idle")
        mixed = dict(background(S2, "done"), status="busy")
        self.assertEqual(agents.state_of(mixed).state, "done")
        self.assertEqual(agents.state_of({"sessionId": S3, "kind": "new-kind", "status": "Busy"}).state, "busy")

    def test_sets_are_the_extension_point(self) -> None:
        self.assertIn("blocked", agents.WAITING_STATES)
        self.assertIn("busy", agents.ACTIVE_STATES)
        self.assertFalse(agents.WAITING_STATES & agents.ACTIVE_STATES)


class ParseTests(unittest.TestCase):
    def test_real_shapes(self) -> None:
        got = agents.parse([interactive(S1, "busy"), interactive(S2, "idle", pid=99),
                            background(S3, "blocked"), background(S4, "done")])
        self.assertEqual(got, {S1: LiveState("interactive", "busy", 4242), S2: LiveState("interactive", "idle", 99),
                               S3: LiveState("background", "blocked", None), S4: LiveState("background", "done", None)})

    def test_name_is_never_kept(self) -> None:
        got = agents.parse([interactive(S1, "busy"), background(S3, "blocked")])
        self.assertNotIn(SECRET_NAME, repr(got))
        self.assertNotIn("/tmp", repr(got))

    def test_junk_entries_are_skipped(self) -> None:
        data = [None, 3, "x", [], {"kind": "interactive", "status": "busy"}, {"sessionId": ""},
                {"sessionId": "has space", "status": "busy"}, {"sessionId": 5}, {"sessionId": "x" * 200},
                interactive(S1, "busy")]
        self.assertEqual(list(agents.parse(data) or {}), [S1])

    def test_not_a_list(self) -> None:
        for data in ({}, {"agents": []}, "[]", None, 3):
            self.assertIsNone(agents.parse(data))
        self.assertEqual(agents.parse([]), {})

    def test_strongest_signal_wins_per_session(self) -> None:
        got = agents.parse([interactive(S1, "idle"), background(S1, "blocked"), interactive(S1, "busy")])
        self.assertEqual(agents.classify_live(got[S1]), "waiting")
        got = agents.parse([background(S2, "done"), interactive(S2, "idle"), interactive(S2, "busy")])
        self.assertEqual(agents.classify_live(got[S2]), "active")
        got = agents.parse([interactive(S3, "idle"), background(S3, "done")])
        self.assertEqual(agents.classify_live(got[S3]), "open")


class LiveTests(unittest.TestCase):
    def test_runs_claude_agents(self) -> None:
        runner = Runner(json.dumps([interactive(S1, "busy"), background(S3, "blocked")]).encode("utf-8"))
        got = agents.live(timeout=2.5, runner=runner, which=which)
        self.assertEqual(runner.calls, [(["/opt/bin/claude", "agents", "--json", "--all"], 2.5)])
        self.assertEqual(set(got or {}), {S1, S3})
        self.assertEqual(agents.classify_live(got[S3]), "waiting")

    def test_str_output_and_empty_list(self) -> None:
        self.assertEqual(agents.live(runner=Runner("[]"), which=which), {})
        self.assertEqual(agents.live(runner=Runner(b"  [ ]\n"), which=which), {})

    def test_missing_claude(self) -> None:
        runner = Runner()
        self.assertIsNone(agents.live(runner=runner, which=lambda name: None))
        self.assertEqual(runner.calls, [])

    def test_failures_give_none(self) -> None:
        cases = [
            Runner(b"[]", returncode=1),
            Runner(b"not json"),
            Runner(b'{"sessions": []}'),
            Runner(b""),
            Runner(None),
            Runner(b"[" + b" " * (agents.MAX_OUTPUT_BYTES + 1) + b"]"),
            Runner(exc=subprocess.TimeoutExpired(["claude"], 3.0)),
            Runner(exc=FileNotFoundError(2, "No such file")),
            Runner(exc=PermissionError(13, "denied")),
            Runner(exc=RuntimeError("anything")),
        ]
        for runner in cases:
            with self.subTest(i=cases.index(runner)):
                self.assertIsNone(agents.live(runner=runner, which=which))

    def test_which_raising_gives_none(self) -> None:
        def bad(name: str) -> Optional[str]:
            raise OSError("PATH broken")
        self.assertIsNone(agents.live(runner=Runner(), which=bad))

    def test_default_runner_honours_the_timeout(self) -> None:
        # The default runner, on a stand-in binary that never answers in time: no real claude.
        import shutil
        sleep = shutil.which("sleep")
        if sleep is None:
            self.skipTest("no sleep binary")
        import time

        def slow(argv: Sequence[str], timeout: float) -> Any:
            return agents._default_runner([sleep, "5"], timeout)
        t0 = time.monotonic()
        self.assertIsNone(agents.live(timeout=0.2, runner=slow, which=which))
        self.assertLess(time.monotonic() - t0, 3.0)


if __name__ == "__main__":
    unittest.main()
