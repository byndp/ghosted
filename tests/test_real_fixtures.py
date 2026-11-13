"""REAL-FIXTURE TESTS: real sessions on this machine, referenced by uuid + line cut + prefix sha1.

These are the only tests that read ~/.claude/projects (or $GHOSTED_PROJECTS_DIR). They skip cleanly when
a transcript is absent (any other machine), unless GHOSTED_REQUIRE_REAL=1. Entries labelled "pending"
are skipped. Privacy: nothing here prints, and every assertion message carries only ids, verdicts,
reason codes, counts and hashes, never transcript text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from core import classify as C
from core import config as CFG
from core import transcript as T

# Private, outside the repo (contract 16.8): GHOSTED_REAL_MANIFEST, else <GHOSTED_HOME>/fixtures/real-manifest.json.
MANIFEST = CFG.real_manifest_path()
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def projects_dir() -> Path:
    env = os.environ.get("GHOSTED_PROJECTS_DIR")
    return Path(env).expanduser() if env else Path("~/.claude/projects").expanduser()


def load_manifest() -> Dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def require_manifest(case: unittest.TestCase) -> None:
    """Skip when this machine has no manifest (every machine but the pilot Mac), unless GHOSTED_REQUIRE_REAL=1."""
    if MANIFEST.is_file():
        return
    if os.environ.get("GHOSTED_REQUIRE_REAL") == "1":
        case.fail("no real-session manifest at %s (GHOSTED_REQUIRE_REAL=1)" % MANIFEST)
    raise unittest.SkipTest("no real-session manifest on this machine")


def read_prefix(path: Path, cut: int) -> List[bytes]:
    """The first ``cut`` lines, each with its newline (what ``head -n cut`` prints)."""
    lines: List[bytes] = []
    with open(path, "rb") as f:
        for line in f:
            if len(lines) >= cut:
                break
            lines.append(line)
    return lines


def measure_fixture(path: Path, uuid: str, cut: int) -> Dict[str, Any]:
    """Content-free facts about one fixture prefix: line count, sha1, verdict, reason, error type.

    The raw transcript lines exist only inside this frame, and nothing is raised from it, so no
    failing frame ever holds transcript bytes (``python -m unittest --locals`` prints every local
    of every frame in a failure's traceback).
    """
    try:
        lines = read_prefix(path, cut)
        facts: Dict[str, Any] = {"lines": len(lines), "sha1": hashlib.sha1(b"".join(lines)).hexdigest(),
                                 "verdict": None, "reason": None, "error": None}
        verdict, reason = C.classify(T.parse_lines(lines, uuid, path=str(path)), C.DEFAULT_IGNORE_TAIL_PATTERNS)
        facts["verdict"], facts["reason"] = verdict, reason
        return facts
    except Exception as exc:  # the type name only: an exception message could quote the transcript
        return {"lines": None, "sha1": None, "verdict": None, "reason": None, "error": type(exc).__name__}


def check_fixture(case: unittest.TestCase, fx: Dict[str, Any]) -> None:
    uuid = fx["uuid"]
    short = uuid[:8]
    if fx.get("labelled_by") == "pending":
        case.skipTest("%s: label pending" % short)
    path = projects_dir() / fx["folder"] / (uuid + ".jsonl")
    if not path.is_file():
        if os.environ.get("GHOSTED_REQUIRE_REAL") == "1":
            case.fail("%s: transcript absent and GHOSTED_REQUIRE_REAL=1" % short)
        case.skipTest("%s: transcript not on this machine" % short)
    cut = int(fx["cut"])
    facts = measure_fixture(path, uuid, cut)
    if facts["error"] is not None:
        case.fail("%s: reading/parsing/classifying raised %s" % (short, facts["error"]))
    if facts["lines"] != cut:
        case.fail("%s: transcript has %d lines, fixture cut is %d (the transcript changed; relabel)" % (short, facts["lines"], cut))
    if facts["sha1"] != fx["prefix_sha1"]:
        case.fail("%s: PREFIX SHA1 CHANGED: got %s, manifest has %s. The transcript changed; relabel this fixture."
                  % (short, facts["sha1"], fx["prefix_sha1"]))
    expect = fx.get("expect", {})
    if facts["verdict"] != expect.get("verdict"):
        case.fail("%s: expected %s, got %s/%s" % (short, expect.get("verdict"), facts["verdict"], facts["reason"]))
    if "reason" in expect and facts["reason"] != expect["reason"]:
        case.fail("%s: expected reason %s, got %s" % (short, expect["reason"], facts["reason"]))


class ManifestTests(unittest.TestCase):
    """Shape of the manifest itself; needs no real transcript."""

    def setUp(self) -> None:
        require_manifest(self)

    def test_shape(self) -> None:
        data = load_manifest()
        self.assertEqual(data.get("version"), 1)
        fixtures = data.get("fixtures")
        self.assertIsInstance(fixtures, list)
        seen = set()
        for fx in fixtures:
            uuid = fx.get("uuid", "")
            with self.subTest(fixture=str(uuid)[:8]):
                self.assertRegex(uuid, _UUID)
                self.assertNotIn(uuid, seen)
                seen.add(uuid)
                folder = fx.get("folder")
                self.assertIsInstance(folder, str)
                self.assertTrue(folder and "/" not in folder and ".." not in folder)
                self.assertIsInstance(fx.get("cut"), int)
                self.assertGreater(fx["cut"], 0)
                self.assertRegex(fx.get("prefix_sha1", ""), _SHA1)
                self.assertIn(fx.get("expect", {}).get("verdict"), set(C.VERDICTS) - {C.AUTOMATED})
                self.assertIsInstance(fx.get("labelled_by"), str)
                self.assertTrue(fx["labelled_by"])

    def test_both_known_positive_kinds_are_listed(self) -> None:
        # By verdict, not by id: real session ids stay out of the repo.
        positives = {fx["expect"]["verdict"] for fx in load_manifest()["fixtures"]
                     if fx.get("expect", {}).get("verdict") in C.FLAGGED_VERDICTS}
        self.assertIn("usage_limit", positives)
        self.assertIn("open_question", positives)


class CheckFixtureMachineryTests(unittest.TestCase):
    """check_fixture() itself, on a synthetic transcript in a temp projects dir (no real data)."""

    UUID = "0b5e6c1d-2f3a-4b7c-8d9e-0a1b2c3d4e5f"
    FOLDER = "-tmp-workspace-demo"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "neg_answered_then_committed.jsonl"
        folder = Path(self.tmp.name) / self.FOLDER
        folder.mkdir()
        self.path = folder / (self.UUID + ".jsonl")
        shutil.copyfile(src, self.path)
        lines = self.path.read_bytes().split(b"\n")
        self.cut = next(i for i, x in enumerate(lines) if b"Want me to commit these 18 files?" in x) + 1
        self.sha = hashlib.sha1(b"".join(read_prefix(self.path, self.cut))).hexdigest()
        self.env = mock.patch.dict(os.environ, {"GHOSTED_PROJECTS_DIR": self.tmp.name}, clear=False)
        self.env.start()
        os.environ.pop("GHOSTED_REQUIRE_REAL", None)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def fx(self, **kw: Any) -> Dict[str, Any]:
        d: Dict[str, Any] = {"uuid": self.UUID, "folder": self.FOLDER, "cut": self.cut, "prefix_sha1": self.sha,
                             "expect": {"verdict": "open_question"}, "labelled_by": "test"}
        d.update(kw)
        return d

    def test_pass(self) -> None:
        check_fixture(self, self.fx())
        check_fixture(self, self.fx(expect={"verdict": "open_question", "reason": "save_offer"}))
        whole = len(self.path.read_bytes().splitlines())
        sha = hashlib.sha1(b"".join(read_prefix(self.path, whole))).hexdigest()
        check_fixture(self, self.fx(cut=whole, prefix_sha1=sha, expect={"verdict": "clean"}))

    def test_prefix_change_fails_loudly(self) -> None:
        with self.assertRaisesRegex(AssertionError, "PREFIX SHA1 CHANGED"):
            check_fixture(self, self.fx(prefix_sha1="0" * 40))

    def test_cut_beyond_the_file_fails(self) -> None:
        with self.assertRaisesRegex(AssertionError, "fixture cut is"):
            check_fixture(self, self.fx(cut=10 ** 6))

    def test_wrong_verdict_and_reason_fail_without_text(self) -> None:
        with self.assertRaises(AssertionError) as ctx:
            check_fixture(self, self.fx(expect={"verdict": "usage_limit"}))
        self.assertNotIn("commit these", str(ctx.exception))
        with self.assertRaisesRegex(AssertionError, "expected reason"):
            check_fixture(self, self.fx(expect={"verdict": "open_question", "reason": "want_me_to"}))

    def test_failure_tracebacks_hold_no_transcript_text_even_with_locals(self) -> None:
        # `python -m unittest --locals` formats failures exactly like this (capture_locals=True).
        for name, kw in (("verdict", {"expect": {"verdict": "usage_limit"}}),
                         ("sha1", {"prefix_sha1": "0" * 40}),
                         ("cut", {"cut": 10 ** 6})):
            with self.subTest(case=name):
                try:
                    check_fixture(self, self.fx(**kw))
                except AssertionError as exc:
                    te = traceback.TracebackException(type(exc), exc, exc.__traceback__, capture_locals=True)
                    text = "".join(te.format())
                    self.assertIn("facts =", text)  # frame locals were captured
                    self.assertNotIn("commit these 18 files", text)
                    self.assertNotIn("Want me to", text)
                else:
                    self.fail("check_fixture did not fail")

    def test_parse_errors_report_the_type_only(self) -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise ValueError("Want me to commit these 18 files?")

        with mock.patch.object(T, "parse_lines", boom):
            with self.assertRaisesRegex(AssertionError, "raised ValueError") as ctx:
                check_fixture(self, self.fx())
        self.assertNotIn("commit these", str(ctx.exception))

    def test_pending_and_absent_skip(self) -> None:
        with self.assertRaises(unittest.SkipTest):
            check_fixture(self, self.fx(labelled_by="pending"))
        with self.assertRaises(unittest.SkipTest):
            check_fixture(self, self.fx(folder="-missing"))
        os.environ["GHOSTED_REQUIRE_REAL"] = "1"
        with self.assertRaisesRegex(AssertionError, "GHOSTED_REQUIRE_REAL"):
            check_fixture(self, self.fx(folder="-missing"))


def negatives_status(data: Dict[str, Any], root: Path) -> Dict[str, Any]:
    """Counts only: are the positives' transcripts on this machine, and how many clean negatives
    are labelled against how many the spec wants (rule 7: ten, before any launchd install)."""
    fixtures = [fx for fx in data.get("fixtures", []) if isinstance(fx, dict)]
    positives = [fx for fx in fixtures if fx.get("expect", {}).get("verdict") in C.FLAGGED_VERDICTS]
    here = bool(positives) and all((root / fx["folder"] / (fx["uuid"] + ".jsonl")).is_file() for fx in positives)
    labelled = sum(1 for fx in fixtures if fx.get("expect", {}).get("verdict") == C.CLEAN
                   and fx.get("labelled_by") not in (None, "", "pending"))
    return {"here": here, "labelled": labelled, "wanted": int(data.get("negatives_wanted") or 0)}


def check_negatives(case: unittest.TestCase, status: Dict[str, Any]) -> None:
    """Skip on other machines; on this one, GHOSTED_REQUIRE_REAL=1 (the pre-install gate) fails until
    enough negatives are labelled, and the default run skips with the count."""
    if not status["here"]:
        case.skipTest("the positive fixtures' transcripts are not on this machine")
    if status["labelled"] >= status["wanted"]:
        return
    message = ("%d of the %d hand-labelled clean negatives are labelled in the manifest (spec rule 7: "
               "they must be, and pass, before the hourly job is installed)" % (status["labelled"], status["wanted"]))
    if os.environ.get("GHOSTED_REQUIRE_REAL") == "1":
        case.fail(message)
    case.skipTest(message + "; GHOSTED_REQUIRE_REAL=1 enforces it")


class NegativesGateTests(unittest.TestCase):
    """Spec rule 7's gate: the real fixtures, labelled, before any launchd install."""

    def test_enough_clean_negatives_are_labelled(self) -> None:
        require_manifest(self)
        check_negatives(self, negatives_status(load_manifest(), projects_dir()))

    def test_the_gate_machinery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pos = {"uuid": "0b5e6c1d-2f3a-4b7c-8d9e-0a1b2c3d4e5f", "folder": "-a", "expect": {"verdict": "usage_limit"},
                   "labelled_by": "test"}
            neg = {"uuid": "0b5e6c1d-2f3a-4b7c-8d9e-0a1b2c3d4e60", "folder": "-a", "expect": {"verdict": "clean"},
                   "labelled_by": "pending"}
            data = {"negatives_wanted": 1, "fixtures": [pos, neg]}
            with self.assertRaises(unittest.SkipTest):  # elsewhere: nothing to check
                check_negatives(self, negatives_status(data, root))
            (root / "-a").mkdir()
            (root / "-a" / (pos["uuid"] + ".jsonl")).write_text("{}\n")
            with mock.patch.dict(os.environ, {"GHOSTED_REQUIRE_REAL": "1"}):
                with self.assertRaisesRegex(AssertionError, "0 of the 1 hand-labelled"):
                    check_negatives(self, negatives_status(data, root))
            with mock.patch.dict(os.environ, {"GHOSTED_REQUIRE_REAL": ""}):
                with self.assertRaises(unittest.SkipTest):
                    check_negatives(self, negatives_status(data, root))
            neg["labelled_by"] = "me"
            check_negatives(self, negatives_status(data, root))  # passes

    def test_no_candidate_is_an_agent_scratchpad_session(self) -> None:
        require_manifest(self)
        # The Q&A candidate and one other were agent probe sessions under /private/tmp, not the user's.
        for fx in load_manifest().get("fixtures", []):
            self.assertFalse(str(fx.get("folder", "")).startswith("-private-"), fx.get("uuid", "")[:8])


class RealFixtureTests(unittest.TestCase):
    """One test per manifest fixture (generated below)."""


def _add_fixture_tests() -> None:
    try:
        fixtures = load_manifest().get("fixtures", [])
    except (OSError, ValueError):
        fixtures = []
    for fx in fixtures:
        uuid = str(fx.get("uuid", ""))
        if not _UUID.match(uuid):
            continue  # ManifestTests reports the bad entry

        def test(self: unittest.TestCase, fx: Dict[str, Any] = fx) -> None:
            check_fixture(self, fx)

        test.__doc__ = "real fixture %s (%s)" % (uuid[:8], fx.get("expect", {}).get("verdict"))
        setattr(RealFixtureTests, "test_fixture_%s" % uuid[:8], test)


_add_fixture_tests()


if __name__ == "__main__":
    unittest.main()
