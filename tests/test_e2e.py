"""End to end: the `ghosted` commands a day uses, through a symlinked bin/ghosted, on synthetic data.

One temp root holds HOME, GHOSTED_HOME, the projects dir (copies of the synthetic fixtures)
and a PATH whose first entry has fake `launchctl`, `claude` and `xcode-select`; pushes go to a
fake ntfy server on 127.0.0.1 (tests.fakeserver). The real ~/.claude, ~/.ghosted, launchctl
and the network are never reached. Each command runs the interpreter running the suite, so the
two documented runs (/usr/bin/python3 and python3) cover both.

The flow: init -> add (good and refused) -> list -> sweep (the detector on every fixture) ->
hook (Stop, StopFailure, junk) -> the 09:15 run (exactly one digest) -> a second run (nothing)
-> done / snooze / re-snooze / dismiss -> the snooze expiring at a later 09:15 (kept
snooze_until, back in that digest) -> doctor -> install --all --dry-run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tests.fakeserver import FakeServer, Response

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"
SETUP = "2026-10-05T15:00:00Z"    # Mon 10:00 in Chicago
DIGEST = "2026-10-06T14:15:00Z"   # Tue 09:15: the digest tick
LATER = "2026-10-06T14:45:00Z"    # Tue 09:45
FRIDAY = "2026-10-09T14:15:00Z"   # Fri 09:15
FAKE_TOOL = """#!/bin/sh
printf '%%s\\n' "${0##*/} $*" >> "%(log)s"
case "${0##*/} $1" in
  "claude --version") printf '2.1.290 (Claude Code)\\n' ;;
  "launchctl print") exit 113 ;;
esac
exit 0
"""


def session_id(path: Path) -> str:
    for raw in path.read_bytes().split(b"\n"):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
            return entry["sessionId"]
    raise AssertionError("no sessionId in " + path.name)


class PilotDayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.le_home = self.root / "ghosted-home"
        self.ws = self.root / "ws"
        self.bin = self.root / "bin"
        self.fakebin = self.root / "fakebin"
        self.log = self.root / "system-calls.log"
        projects = self.home / ".claude" / "projects"
        for d in (projects, self.ws / "shop", self.bin, self.fakebin):
            d.mkdir(parents=True)
        os.symlink(str(ROOT / "bin" / "ghosted"), str(self.bin / "ghosted"))
        for tool in ("launchctl", "claude", "xcode-select"):
            (self.fakebin / tool).write_text(FAKE_TOOL % {"log": self.log})
            os.chmod(str(self.fakebin / tool), 0o755)
        self.uuids = {}
        stamp = datetime.fromisoformat(DIGEST.replace("Z", "+00:00")).timestamp() - 2 * 86400
        for src in sorted(FIXTURES.glob("*.jsonl")):
            uuid = session_id(src)
            folder = projects / ("-Users-sam-workspace-" + uuid[:8])
            folder.mkdir()
            dst = folder / (uuid + ".jsonl")
            shutil.copy(str(src), str(dst))
            os.utime(str(dst), (stamp, stamp))
            self.uuids[src.name] = uuid
        self.server = FakeServer(handler=self.ntfy).start()
        self.addCleanup(self.server.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ntfy(self, req):
        if req.method == "GET" and req.path == "/v1/health":
            return Response(200, json={"healthy": True})
        if req.method == "POST" and req.path == "/":
            return Response(200, json={"id": "fake%d" % len(self.server.requests), "event": "message"})
        return Response(404, json={"error": "not found"})

    def ghosted(self, *args, now=SETUP, stdin=None, **extra):
        env = {"HOME": str(self.home), "GHOSTED_HOME": str(self.le_home), "GHOSTED_NOW": now,
               "PATH": "%s:/usr/bin:/bin" % self.fakebin, "CLAUDE_PROJECT_DIR": str(self.ws / "shop")}
        env.update(extra)
        proc = subprocess.run([sys.executable, str(self.bin / "ghosted")] + list(args), input=stdin,
                              capture_output=True, env=env, cwd=str(self.root), timeout=120)
        return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")

    def ok(self, *args, **kw) -> str:
        code, out, err = self.ghosted(*args, **kw)
        self.assertEqual(code, 0, "ghosted %s: %s%s" % (" ".join(args), out, err))
        return out

    def items(self):
        return json.loads((self.le_home / "items.json").read_text())["items"]

    def jsonl(self, name):
        path = self.le_home / name
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []

    def posts(self):
        return [r for r in self.server.requests if r.method == "POST"]

    def test_a_day(self) -> None:
        out = self.ok("init", "--adapter", "ntfy", "--timezone", "America/Chicago",
                      "--workspace-root", str(self.ws))
        self.assertIn("wrote", out)
        cfg = self.le_home / "config.toml"
        cfg.write_text(cfg.read_text().replace('server = "https://ntfy.sh"', 'server = "%s"' % self.server.url))

        # add: one good item, refusals write nothing
        out = self.ok("add", "Local: pay Bob Example $480 for the domain", "--public",
                      "home: pay the overdue water bill", "--due", "10-07", "--project", "home")
        self.assertTrue(out.startswith("GS-0001 added: home: pay the overdue water bill"), out)
        self.assertTrue(self.ok("add", "Local: ask about the logo", "--public", "shop: decide on the logo")
                        .startswith("GS-0002 added"))
        for args in (("--public", "home: email bob@example.com"), ("--public", "neutral", "--project", "ops-$500k")):
            code, _, err = self.ghosted("add", "Local: x", *args)
            self.assertEqual(code, 2, err)
            self.assertIn("refused", err)
        self.assertEqual(sorted(self.items()), ["GS-0001", "GS-0002"])
        self.assertNotIn("Bob Example", (self.le_home / "inbox.jsonl").read_text())
        self.assertIn("Local: pay Bob Example", self.ok("list"))

        # sweep: the detector on every synthetic fixture; no run record, nothing pushed
        out = self.ok("sweep")
        self.assertTrue(out.startswith("# Ghosted"), out[:80])
        flagged = {it["session_uuid"] for it in self.items().values() if it["kind"] == "interrupted"}
        positives = {u for name, u in self.uuids.items() if name.startswith("pos_")}
        negatives = {u for name, u in self.uuids.items() if not name.startswith("pos_")}
        self.assertEqual((flagged & positives, flagged & negatives), (positives, set()))
        self.assertEqual((self.jsonl("runs.jsonl"), self.posts()), ([], []))

        # the hook: silent, exit 0, verdicts only
        sid = self.uuids["pos_want_me_to.jsonl"]
        for payload in ({"session_id": sid, "hook_event_name": "Stop", "cwd": str(self.ws / "shop"),
                         "last_assistant_message": "Done. Want me to open the PR?"},
                        {"session_id": sid, "hook_event_name": "StopFailure", "cwd": str(self.ws / "shop"),
                         "error": "rate_limit"}):
            self.assertEqual(self.ghosted("hook", stdin=json.dumps(payload).encode()), (0, "", ""))
        self.assertEqual(self.ghosted("hook", stdin=b"\x00 not json"), (0, "", ""))
        self.assertEqual([(s["verdict"], s["reason"], s["cwd_project"]) for s in self.jsonl("stops.jsonl")],
                         [("open_question", "save_offer", "shop"), ("usage_limit", "rate_limit", "shop")])
        self.assertNotIn("open the PR", (self.le_home / "stops.jsonl").read_text())

        # 09:15: exactly one push, the digest, with public titles only
        out = self.ok("run", now=DIGEST)
        self.assertIn("sent 1 push", out)
        self.assertEqual(len(self.posts()), 1)
        body = self.posts()[0].json()
        self.assertEqual((body["title"], body["tags"]), ("Ghosted", ["digest"]))
        self.assertIn("home: pay the overdue water bill", body["message"])
        self.assertNotIn("Bob", json.dumps(body))
        self.assertEqual([(r["dedup_key"], r["ok"]) for r in self.jsonl("notifications.jsonl")],
                         [("digest:2026-10-06", None), ("digest:2026-10-06", True)])  # claim, outcome
        self.ok("run", now=LATER)
        self.assertEqual(len(self.posts()), 1)

        # status changes
        self.assertIn("GS-0001 done", self.ok("done", "GS-0001", now=LATER))
        self.assertIn("snoozed until Wed Oct 7", self.ok("snooze", "2", "1d", now=LATER))
        self.assertIn("snoozed until Fri Oct 9", self.ok("snooze", "2", "3d", now=LATER))
        target = sorted(i for i, it in self.items().items() if it["kind"] == "interrupted")[0]
        self.assertIn("dismissed (not real)", self.ok("dismiss", target, "--reason", "not_real", now=LATER))
        status = [(e["item"]["id"], e["item"]["status"], e["item"]["rev"])
                  for e in self.jsonl("inbox.jsonl") if e["type"] == "item.status"]
        self.assertEqual(status, [("GS-0001", "done", 1), ("GS-0002", "snoozed", 1), ("GS-0002", "snoozed", 2),
                                  (target, "dismissed", 1)])

        # Friday 09:15: the snooze expires (snooze_until kept) and the item is in that digest
        out = self.ok("run", now=FRIDAY)
        self.assertIn("1 reopened", out)
        item = self.items()["GS-0002"]
        self.assertEqual((item["status"], item["snooze_until"], item["rev"]), ("open", "2026-10-09T09:00", 4))
        self.assertEqual(len(self.posts()), 2)
        self.assertIn("shop: decide on the logo", self.posts()[-1].json()["message"])

        # doctor, install --dry-run
        code, out, err = self.ghosted("doctor", now=FRIDAY)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("FAIL", out)
        self.assertIn("every item has its create event", out)
        before = sorted(str(p) for p in self.home.rglob("*") if "com.apple.python" not in str(p))
        out = self.ok("install", "--all", "--dry-run")
        self.assertIn("dry run: nothing was changed", out)
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*") if "com.apple.python" not in str(p)), before)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertEqual([c for c in calls if c.startswith("launchctl") and not c.startswith("launchctl print")], [])


if __name__ == "__main__":
    unittest.main()
