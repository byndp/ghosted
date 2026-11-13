"""`ghosted hook` (contract section 11): records, verdicts, and silence on every bad input.

Subprocess tests run the real launcher (bin/ghosted hook) on every available interpreter (the one
running the suite, /usr/bin/python3 and python3 on PATH, deduplicated by version), with a temp
HOME and GHOSTED_HOME. Only synthetic payloads and fixtures are used.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core import classify, hook
from core.config import Config

ROOT = Path(__file__).resolve().parent.parent
LE = ROOT / "bin" / "ghosted"
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"
SESSION = "0a0a0a0a-1111-4222-8333-444455556666"
NOW = "2026-10-06T14:15:00Z"
MARKER = "Zebra quartz lantern 7781 is the marker sentence"
RECORD_KEYS = {"at", "session_id", "event", "verdict", "reason", "cwd_project"}


def _version_of(python: str):
    try:
        out = subprocess.run([python, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.decode().strip()
    return text if out.returncode == 0 and text else None


def interpreters():
    """[(python, version)] for each distinct interpreter >= 3.9 on this machine."""
    seen = {}
    for cand in (sys.executable, "/usr/bin/python3", shutil.which("python3")):
        if not cand or not os.path.exists(cand):
            continue
        version = _version_of(cand)
        if version and tuple(int(x) for x in version.split(".")) >= (3, 9) and version not in seen:
            seen[version] = cand
    return [(py, v) for v, py in seen.items()]


PYTHONS = interpreters()


def payload(**fields) -> bytes:
    base = {"session_id": SESSION, "transcript_path": "/nonexistent/%s.jsonl" % SESSION,
            "cwd": "/tmp/ghosted-test/ws/shop", "hook_event_name": "Stop"}
    base.update(fields)
    for key in [k for k, v in base.items() if v is None]:
        del base[key]
    return json.dumps(base).encode("utf-8")


class HookUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "ghosted-home"
        self.cfg = Config(home=self.home, timezone="America/Chicago",
                          projects_dir=self.root / "projects", workspace_roots=[self.root / "ws"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def stops(self):
        path = self.home / "stops.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # -- parse_payload ---------------------------------------------------------------------------

    def test_parse_payload_rejects_everything_but_one_object(self) -> None:
        for bad in (None, b"", b"   ", b"not json", b"[1, 2]", b'"a string"', b"42", b"null",
                    b"{", b"\xff\xfe{}", b"[" * 200000, 123, object()):
            self.assertIsNone(hook.parse_payload(bad))
        self.assertEqual(hook.parse_payload(b'{"a": 1}'), {"a": 1})
        self.assertEqual(hook.parse_payload('{"a": 1}'), {"a": 1})

    def test_parse_payload_refuses_more_than_one_mebibyte(self) -> None:
        big = json.dumps({"session_id": SESSION, "last_assistant_message": "x" * (1024 * 1024)})
        self.assertGreater(len(big), hook.MAX_STDIN_BYTES)
        self.assertIsNone(hook.parse_payload(big.encode()))
        fits = json.dumps({"session_id": SESSION, "last_assistant_message": "x" * 1000}).encode()
        self.assertIsNotNone(hook.parse_payload(fits))

    # -- verdicts ----------------------------------------------------------------------------------

    def test_question_verdict_is_the_section_7_question_test(self) -> None:
        pats = classify.DEFAULT_IGNORE_TAIL_PATTERNS
        cases = [
            ("Want me to commit these changes?", ("open_question", "save_offer")),
            ("Want me to tidy these changes?", ("open_question", "offer_q")),
            ("All tests pass. Want me to open the PR", ("open_question", "save_offer")),
            ("All tests pass. Want me to tidy the README", ("open_question", "want_me_to")),
            ("Ready when you are: say the word and I'll push.", ("open_question", "save_offer")),
            ("Ready when you are: say the word and I'll rerun it.", ("open_question", "say_the_word")),
            ("Either option works, your call.", ("open_question", "your_call")),
            ("Done. All 341 tests pass.", ("clean", "no_question")),
            ("Fixed. Anything else?", ("clean", "ignored_tail")),
            ("Run `ls -la?` to see it.", ("clean", "no_question")),
            ("See https://example.com/a?b=1", ("clean", "no_question")),
            ("**Want me to commit?**  \n", ("open_question", "save_offer")),
            ("", ("clean", "no_question")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.question_verdict(text, pats), expected)

    def test_custom_ignore_patterns_come_from_config(self) -> None:
        self.assertEqual(hook.question_verdict("Shall I continue?", [r"(?i)shall i continue\?$"]),
                         ("clean", "ignored_tail"))
        self.assertEqual(hook.question_verdict("Shall I continue?", []), ("open_question", "offer_q"))

    def test_stop_records_split_offers_from_direct_questions(self) -> None:
        # Contract 16.1: the hook's reason is whatever question_cue returns (no hook change needed).
        pats = classify.DEFAULT_IGNORE_TAIL_PATTERNS
        cases = [
            ("Tests pass. Should I commit?", ("open_question", "save_offer")),
            ("Want me to commit, or keep going?", ("open_question", "save_offer")),
            ("Tests pass. Should I continue?", ("open_question", "offer_q")),
            ("Want me to tidy up, or keep going?", ("open_question", "offer_q")),
            ("Green. Let me know if you want me to push.", ("open_question", "save_offer")),
            ("Should the push notifications go out at 9am?", ("open_question", "trailing_q")),
            ("Keep the sticky cart, or move it to the header?", ("open_question", "trailing_q")),
            ("Should I keep the retry loop or switch to the queue?", ("open_question", "trailing_q")),
            ("Which one do you prefer?", ("open_question", "trailing_q")),
            ("Done. Anything else?", ("clean", "ignored_tail")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.question_verdict(text, pats), expected)
                rec = hook.make_record(json.loads(payload(last_assistant_message=text)), self.cfg, NOW)
                self.assertEqual((rec["verdict"], rec["reason"]), expected)
                self.assertEqual(rec["reason"], classify.question_cue(text) if expected[0] == "open_question"
                                 else rec["reason"])
                self.assertNotIn(text, json.dumps(rec))

    def test_failure_verdict(self) -> None:
        self.assertEqual(hook.failure_verdict("rate_limit"), ("usage_limit", "rate_limit"))
        self.assertEqual(hook.failure_verdict("server_error"), ("api_error", "server_error"))
        self.assertEqual(hook.failure_verdict("overloaded"), ("api_error", "overloaded"))
        for odd in (None, "", "Not An Identifier!", 7, {"x": 1}):
            self.assertEqual(hook.failure_verdict(odd), ("api_error", "api_error"))

    # -- records -----------------------------------------------------------------------------------

    def test_stop_question_record(self) -> None:
        rec = hook.make_record(json.loads(payload(last_assistant_message="Want me to commit these?")),
                               self.cfg, NOW)
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec, {"at": NOW, "session_id": SESSION, "event": "Stop",
                               "verdict": "open_question", "reason": "save_offer", "cwd_project": "shop"})

    def test_stop_clean_record(self) -> None:
        rec = hook.make_record(json.loads(payload(last_assistant_message="Shipped. Tests pass.")),
                               self.cfg, NOW)
        self.assertEqual((rec["event"], rec["verdict"], rec["reason"]), ("Stop", "clean", "no_question"))

    def test_stop_failure_records(self) -> None:
        for error, expected in (("rate_limit", ("usage_limit", "rate_limit")),
                                ("server_error", ("api_error", "server_error"))):
            data = json.loads(payload(hook_event_name="StopFailure", error=error,
                                      error_details="429 Too Many Requests",
                                      last_assistant_message="API Error: Rate limit reached"))
            rec = hook.make_record(data, self.cfg, NOW)
            self.assertEqual(rec["event"], "StopFailure")
            self.assertEqual((rec["verdict"], rec["reason"]), expected)

    def test_missing_event_name_is_inferred_and_other_events_are_ignored(self) -> None:
        data = json.loads(payload(error="rate_limit"))
        del data["hook_event_name"]
        self.assertEqual(hook.make_record(data, self.cfg, NOW)["event"], "StopFailure")
        data = json.loads(payload(last_assistant_message="Done."))
        del data["hook_event_name"]
        self.assertEqual(hook.make_record(data, self.cfg, NOW)["event"], "Stop")
        for other in ("SessionEnd", "Notification", "SubagentStop", 5):
            self.assertIsNone(hook.make_record(json.loads(payload(hook_event_name=other)), self.cfg, NOW))

    def test_session_id_must_look_like_an_id(self) -> None:
        for bad in (None, "", " ", "a b", "x" * 200, 12, "../../etc", "line\nbreak"):
            data = json.loads(payload(last_assistant_message="Done."))
            if bad is None:
                del data["session_id"]
            else:
                data["session_id"] = bad
            with self.subTest(bad=bad):
                self.assertIsNone(hook.make_record(data, self.cfg, NOW))

    def test_transcript_fallback_when_the_message_is_missing(self) -> None:
        cases = (("pos_trailing_question.jsonl", "open_question"),
                 ("neg_safe_to_clear.jsonl", "clean"),
                 ("pos_limit_meta.jsonl", "usage_limit"))
        for name, verdict in cases:
            with self.subTest(fixture=name):
                data = json.loads(payload(transcript_path=str(FIXTURES / name)))
                self.assertEqual(hook.make_record(data, self.cfg, NOW)["verdict"], verdict)

    def test_no_message_and_no_transcript_path_records_nothing(self) -> None:
        data = json.loads(payload(transcript_path=None))
        self.assertIsNone(hook.make_record(data, self.cfg, NOW))

    def test_cwd_project(self) -> None:
        ws = self.root / "ws"
        (ws / "shop" / "deep").mkdir(parents=True)
        repo = self.root / "elsewhere" / "tool"
        (repo / "src" / "pkg").mkdir(parents=True)
        (repo / ".git").mkdir()
        self.assertEqual(hook.cwd_project(str(ws / "shop" / "deep"), self.cfg), "shop")
        self.assertEqual(hook.cwd_project(str(ws), self.cfg), "ws")
        self.assertEqual(hook.cwd_project(str(repo / "src" / "pkg"), self.cfg), "tool")
        self.assertEqual(hook.cwd_project(str(self.root / "plain" / "dir"), self.cfg), "dir")
        self.assertEqual(hook.cwd_project("relative/name", self.cfg), "name")
        for bad in (None, "", "   ", 7, "a\x00b"):
            self.assertIsNone(hook.cwd_project(bad, self.cfg))

    def test_cwd_project_matches_a_symlinked_workspace_root(self) -> None:
        real = self.root / "real-ws"
        (real / "app").mkdir(parents=True)
        link = self.root / "ws-link"
        os.symlink(str(real), str(link))
        cfg = Config(home=self.home, projects_dir=self.root / "p", workspace_roots=[link])
        self.assertEqual(hook.cwd_project(str(real / "app"), cfg), "app")

    # -- main --------------------------------------------------------------------------------------

    def test_main_appends_one_record_without_message_text(self) -> None:
        text = MARKER + ". Want me to keep going?"
        self.assertEqual(hook.main(payload(last_assistant_message=text), cfg=self.cfg, now=NOW), 0)
        self.assertEqual(hook.main(payload(hook_event_name="StopFailure", error="rate_limit",
                                           last_assistant_message=MARKER), cfg=self.cfg, now=NOW), 0)
        rows = self.stops()
        self.assertEqual([(r["event"], r["verdict"]) for r in rows],
                         [("Stop", "open_question"), ("StopFailure", "usage_limit")])
        raw = (self.home / "stops.jsonl").read_bytes()
        self.assertNotIn(b"Zebra", raw)
        self.assertNotIn(b"lantern", raw)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.home)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.home / "stops.jsonl")).st_mode), 0o600)

    def test_main_never_raises(self) -> None:
        for weird in (None, b"", b"{}", b"[]", "text", 42, object(), b"\x00" * 10):
            self.assertEqual(hook.main(weird, cfg=self.cfg, now=NOW), 0)
        self.assertEqual(hook.main(payload(last_assistant_message="Done."), cfg=object(), now=NOW), 0)
        self.assertEqual(hook.main(payload(last_assistant_message="Done."), cfg=self.cfg,
                                   now="not a time"), 0)
        self.assertEqual(self.stops(), [])

    def test_main_with_an_unwritable_home_is_silent(self) -> None:
        blocker = self.root / "a-file"
        blocker.write_text("x")
        cfg = Config(home=blocker / "home", projects_dir=self.root / "p")
        self.assertEqual(hook.main(payload(last_assistant_message="Want me to?"), cfg=cfg, now=NOW), 0)

    def test_main_loads_config_from_the_environment(self) -> None:
        self.home.mkdir(mode=0o700)
        (self.home / "config.toml").write_text(
            '[detect]\nignore_tail_patterns = ["(?i)ok to proceed\\\\?$"]\n')
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home)}
        hook.main(payload(last_assistant_message="Ok to proceed?"), environ=env, now=NOW)
        self.assertEqual(self.stops()[-1]["reason"], "ignored_tail")

    def test_a_broken_config_still_records_with_defaults(self) -> None:
        self.home.mkdir(mode=0o700)
        (self.home / "config.toml").write_text("this is = = not toml [[[")
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home)}
        self.assertEqual(hook.main(payload(last_assistant_message="Want me to?"), environ=env, now=NOW), 0)
        self.assertEqual(self.stops()[-1]["verdict"], "open_question")

    def test_the_fallback_config_honours_le_projects_dir(self) -> None:
        self.home.mkdir(mode=0o700)
        (self.home / "config.toml").write_text("this is = = not toml [[[")
        env = {"HOME": str(self.root), "GHOSTED_HOME": str(self.home),
               "GHOSTED_PROJECTS_DIR": str(self.root / "elsewhere")}
        cfg = hook._load_cfg(env)
        self.assertEqual((cfg.home, cfg.projects_dir), (self.home, self.root / "elsewhere"))
        env.pop("GHOSTED_PROJECTS_DIR")
        self.assertEqual(hook._load_cfg(env).projects_dir, self.root / ".claude" / "projects")

    def test_read_stdin(self) -> None:
        import io

        class Pipe(io.BytesIO):
            def isatty(self):
                return False

        class Tty(io.BytesIO):
            def isatty(self):
                return True

        self.assertEqual(hook.read_stdin(Pipe(b'{"a":1}')), b'{"a":1}')
        self.assertIsNone(hook.read_stdin(Tty(b"{}")))
        big = hook.read_stdin(Pipe(b"x" * (hook.MAX_STDIN_BYTES + 50)))
        self.assertEqual(len(big), hook.MAX_STDIN_BYTES + 1)
        self.assertIsNone(hook.read_stdin(object()))


@unittest.skipUnless(PYTHONS, "no interpreter found")
class HookSubprocessTests(unittest.TestCase):
    """Through the real launcher, on every interpreter: silence and exit 0 on every path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.user_home = self.root / "home"
        self.user_home.mkdir()
        self.le_home = self.root / "ghosted-home"

    def tearDown(self) -> None:
        # Undo the chmods the tests made so cleanup can delete everything. Never follow a symlink:
        # one test links to the real bin/ghosted, and chmod through it would strip its executable bit.
        for path in self.root.rglob("*"):
            if path.is_symlink():
                continue
            try:
                os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
            except OSError:
                pass
        self.tmp.cleanup()

    def env(self, le_home=None):
        return {"HOME": str(self.user_home), "GHOSTED_HOME": str(le_home or self.le_home),
                "PATH": "/usr/bin:/bin"}

    def run_hook(self, python, data, le_home=None, script=None, extra=()):
        argv = [python, str(script or LE), "hook"] + list(extra)
        return subprocess.run(argv, input=data, capture_output=True, env=self.env(le_home), timeout=60)

    def stops(self, le_home=None):
        path = Path(le_home or self.le_home) / "stops.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")

    def test_eight_bad_inputs_exit_0_with_no_output(self) -> None:
        unreadable = self.root / ("%s.jsonl" % SESSION)
        unreadable.write_text('{"type":"assistant"}\n')
        os.chmod(str(unreadable), 0)
        blocker = self.root / "a-file"
        blocker.write_text("x")
        huge = json.dumps({"session_id": SESSION, "hook_event_name": "Stop",
                           "last_assistant_message": "Want me to? " * 200000}).encode()
        self.assertGreater(len(huge), hook.MAX_STDIN_BYTES)
        cases = [
            ("empty", b"", None),
            ("non-JSON", b"this is not json at all", None),
            ("JSON array", b'[{"session_id": "x"}]', None),
            ("JSON string", b'"Stop"', None),
            ("missing fields", b'{"hook_event_name": "Stop"}', None),
            ("huge input", huge, None),
            ("unreadable transcript", payload(transcript_path=str(unreadable)), None),
            ("unwritable home", payload(last_assistant_message="Want me to?"), blocker / "ghosted-home"),
        ]
        for python, version in PYTHONS:
            for name, data, le_home in cases:
                with self.subTest(python=version, case=name):
                    self.assert_silent(self.run_hook(python, data, le_home))
            self.assertEqual(self.stops(), [], "no bad input may leave a record")

    def test_a_read_only_home_is_silent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        self.le_home.mkdir(mode=0o500)
        for python, version in PYTHONS:
            with self.subTest(python=version):
                self.assert_silent(self.run_hook(python, payload(last_assistant_message="Want me to?")))

    def test_records_for_each_kind_of_ending(self) -> None:
        cases = [
            (payload(last_assistant_message="Tests pass. Want me to commit?"), ("Stop", "open_question", "save_offer")),
            (payload(last_assistant_message="Tests pass. Want the full log?"), ("Stop", "open_question", "offer_q")),
            (payload(last_assistant_message="Committed and pushed."), ("Stop", "clean", "no_question")),
            (payload(hook_event_name="StopFailure", error="rate_limit",
                     last_assistant_message="API Error: Rate limit reached"),
             ("StopFailure", "usage_limit", "rate_limit")),
            (payload(hook_event_name="StopFailure", error="server_error"),
             ("StopFailure", "api_error", "server_error")),
        ]
        for python, version in PYTHONS:
            with self.subTest(python=version):
                start = len(self.stops())
                for data, _ in cases:
                    self.assert_silent(self.run_hook(python, data))
                rows = self.stops()[start:]
                self.assertEqual([(r["event"], r["verdict"], r["reason"]) for r in rows],
                                 [expected for _, expected in cases])
                for row in rows:
                    self.assertEqual(set(row), RECORD_KEYS)
                    self.assertEqual(row["session_id"], SESSION)
                    self.assertEqual(row["cwd_project"], "shop")

    def test_the_marker_sentence_never_reaches_stops_jsonl(self) -> None:
        for python, version in PYTHONS:
            with self.subTest(python=version):
                self.assert_silent(self.run_hook(python, payload(
                    last_assistant_message=MARKER + ". Shall I push?")))
                self.assert_silent(self.run_hook(python, payload(
                    hook_event_name="StopFailure", error="rate_limit", error_details=MARKER,
                    last_assistant_message=MARKER)))
        raw = (self.le_home / "stops.jsonl").read_bytes()
        self.assertEqual(len(self.stops()), 2 * len(PYTHONS))
        for word in (b"Zebra", b"quartz", b"lantern", b"7781", b"marker", b"push"):
            self.assertNotIn(word, raw)

    def test_works_through_a_symlink_and_ignores_extra_args(self) -> None:
        link_dir = self.root / "elsewhere" / "bin"
        link_dir.mkdir(parents=True)
        link = link_dir / "ghosted"
        os.symlink(str(LE), str(link))
        for python, version in PYTHONS:
            with self.subTest(python=version):
                self.assert_silent(self.run_hook(python, payload(last_assistant_message="Want me to?"),
                                                 script=link, extra=("--whatever",)))
        self.assertEqual(len(self.stops()), len(PYTHONS))

    def test_an_empty_stdin_returns_at_once(self) -> None:
        # No stdin at all (closed): the hook must not hang waiting for input.
        for python, version in PYTHONS:
            with self.subTest(python=version):
                proc = subprocess.run([python, str(LE), "hook"], stdin=subprocess.DEVNULL,
                                      capture_output=True, env=self.env(), timeout=60)
                self.assert_silent(proc)

    def test_a_stdin_that_never_closes_is_cut_off_by_the_watchdog(self) -> None:
        python = PYTHONS[0][0]  # one interpreter: this test waits for the 10 s watchdog
        proc = subprocess.Popen([python, str(LE), "hook"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env())
        start = time.monotonic()
        try:
            proc.stdin.write(b'{"session_id": "%s", "hook_event_name": "Stop"' % SESSION.encode())
            proc.stdin.flush()
            code = proc.wait(timeout=60)  # stdin stays open the whole time
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdin.close()
        elapsed = time.monotonic() - start
        self.assertEqual(code, 0)
        self.assertEqual((proc.stdout.read(), proc.stderr.read()), (b"", b""))
        proc.stdout.close()
        proc.stderr.close()
        self.assertGreaterEqual(elapsed, 5)
        self.assertLess(elapsed, 40)
        self.assertEqual(self.stops(), [])

    def test_it_is_fast(self) -> None:
        data = payload(last_assistant_message="Want me to commit these?")
        for python, version in PYTHONS:
            with self.subTest(python=version):
                self.run_hook(python, data)  # warm the bytecode cache
                times = []
                for _ in range(5):
                    start = time.perf_counter()
                    self.assert_silent(self.run_hook(python, data))
                    times.append(time.perf_counter() - start)
                times.sort()
                # The target is < 100 ms; the bound here is loose so a busy machine can't flake it.
                self.assertLess(times[len(times) // 2], 1.0)


if __name__ == "__main__":
    unittest.main()
