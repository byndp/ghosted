"""Tests for core.transcript: human turns, the final text, head + expanding tail == whole file."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from core import classify, transcript
from core.transcript import human_text, human_turn, parse_lines, parse_transcript

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
KIB = 1024

# A text question the parser must surface. Classifier v2 (contract 16.1) splits a trailing "?" into a
# direct question (trailing_q), a polite offer (offer_q) and an offer to save the work (save_offer);
# these tests are about parsing, so any of them passes ("Want me to push ...?" is a save offer).
QUESTION_ENDINGS = {("open_question", "trailing_q"), ("open_question", "offer_q"), ("open_question", "save_offer")}


def fixture_paths() -> List[Path]:
    return sorted(FIXTURES.glob("*.jsonl"))


def read_lines(path: Path) -> List[bytes]:
    with open(path, "rb") as f:
        return f.readlines()


def by_lines(path: Path, **kw: Any) -> transcript.SessionSummary:
    return parse_lines(read_lines(path), path.stem, path, **kw)


# --------------------------------------------------------------------------- builders

class Builder:
    """Writes invented transcripts in the real shape (for runtime-generated cases)."""

    def __init__(self, cwd: str = "/Users/sam/workspace/big-repo", entrypoint: str = "cli") -> None:
        self.cwd = cwd
        self.entrypoint = entrypoint
        self.n = 0
        self.lines: List[str] = []

    def _ts(self) -> str:
        self.n += 1
        h, rem = divmod(self.n, 3600)
        m, s = divmod(rem, 60)
        return "2026-10-02T%02d:%02d:%02d.%03dZ" % (h % 24, m, s, self.n % 1000)

    def env(self, etype: str, **extra: Any) -> Dict[str, Any]:
        e: Dict[str, Any] = {
            "parentUuid": None, "isSidechain": False, "userType": "external", "cwd": self.cwd,
            "sessionId": "8d1f3c2a-0b7e-4c55-9a61-2f4e8b7c9d10", "version": "2.1.281", "gitBranch": "main",
            "entrypoint": self.entrypoint, "type": etype, "uuid": "u-%d" % self.n, "timestamp": self._ts(),
        }
        e.update(extra)
        return e

    def put(self, obj: Dict[str, Any]) -> None:
        self.lines.append(json.dumps(obj, separators=(",", ":")))

    def meta(self, etype: str, **extra: Any) -> None:
        d: Dict[str, Any] = {"type": etype}
        d.update(extra)
        self.put(d)

    def user(self, content: Any, **extra: Any) -> None:
        self.put(self.env("user", message={"role": "user", "content": content}, **extra))

    def text(self, text: str, **extra: Any) -> None:
        self.put(self.env("assistant", message={"role": "assistant", "content": [{"type": "text", "text": text}]}, **extra))

    def tool_use(self, name: str = "Bash", **extra: Any) -> None:
        self.put(self.env("assistant", message={"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x%d" % self.n, "name": name, "input": {}}]}, **extra))

    def tool_result(self, body: str = "ok") -> None:
        self.put(self.env("user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": body}]}, toolUseResult={"stdout": body}))

    def system(self, subtype: str, **extra: Any) -> None:
        self.put(self.env("system", subtype=subtype, **extra))

    def attachment(self, att: Dict[str, Any]) -> None:
        self.put(self.env("attachment", attachment=att))

    def size(self) -> int:
        return sum(len(x.encode("utf-8")) + 1 for x in self.lines)

    def write(self, path: Path) -> Path:
        path.write_text("".join(x + "\n" for x in self.lines), encoding="utf-8")
        return path

    def nonhuman_filler(self, target_bytes: int) -> None:
        """Non-human lines (metadata, system, attachments, meta/wrapper user entries, sidechain)."""
        pad = "x" * 1500
        start = self.size()
        i = 0
        while self.size() - start < target_bytes:
            k = i % 9
            if k == 0:
                self.system("turn_duration", durationMs=1000 + i)
            elif k == 1:
                self.attachment({"type": "total_tokens_reminder", "used": i, "note": pad})
            elif k == 2:
                self.meta("file-history-snapshot", messageId="m%d" % i, snapshot={"trackedFileBackups": {"f": pad}})
            elif k == 3:
                self.user("<task-notification>\n<task-id>t%d</task-id>\n<status>completed</status>\n</task-notification>\nRead the output file to retrieve the result: /tmp/t%d" % (i, i))
            elif k == 4:
                self.user("<system-reminder>%s</system-reminder>" % pad, isMeta=True)
            elif k == 5:
                self.meta("queue-operation", operation="enqueue", content="yes go ahead %s" % pad)
            elif k == 6:
                self.text("Sidechain asks: should I keep going? %s" % pad, isSidechain=True)
                self.tool_use(isSidechain=True)
            elif k == 7:
                self.attachment({"type": "queued_command", "commandMode": "task-notification", "prompt": "<task-notification>%s</task-notification>" % pad, "origin": {"kind": "task-notification"}})
            else:
                self.meta("artifact-autoreact-ledger", entries=[pad])
            i += 1


def big_question_session(filler: int = 700 * KIB) -> Builder:
    b = Builder()
    b.meta("mode", mode="default")
    b.meta("last-prompt", lastPrompt="ship it")
    b.user("Prepare the release branch and run the full suite.")
    for _ in range(3):
        b.tool_use()
        b.tool_result("all good " + "y" * 200)
    b.text("Release branch is ready and the suite passes. Want me to push the release branch?")
    b.system("stop_hook_summary", hookCount=1)
    b.nonhuman_filler(filler)
    return b


# --------------------------------------------------------------------------- human text

class HumanTextTests(unittest.TestCase):
    def test_plain_text(self) -> None:
        self.assertEqual(human_text("  fix the bug  "), "fix the bug")

    def test_blank_and_non_string(self) -> None:
        self.assertIsNone(human_text("   \n "))
        self.assertIsNone(human_text(None))
        self.assertIsNone(human_text(42))

    def test_system_reminder_is_stripped(self) -> None:
        self.assertIsNone(human_text("<system-reminder>\nctx\n</system-reminder>"))
        self.assertEqual(human_text("<system-reminder>ctx</system-reminder>\nRun it"), "Run it")
        self.assertEqual(human_text("Run it\n<system-reminder>a</system-reminder><system-reminder>b</system-reminder>"), "Run it")

    def test_nested_and_attributed_wrappers(self) -> None:
        self.assertIsNone(human_text("<system-reminder>a<system-reminder>b</system-reminder>c</system-reminder>"))
        self.assertIsNone(human_text('<system-reminder priority="high">x</system-reminder>'))
        self.assertIsNone(human_text("<user-prompt-submit-hook>hook said hi</user-prompt-submit-hook>"))
        self.assertEqual(human_text("Deploy it <user-prompt-submit-hook>ok</user-prompt-submit-hook>"), "Deploy it")

    def test_command_and_local_command_wrappers(self) -> None:
        self.assertIsNone(human_text("<command-name>/cost</command-name>\n<command-message>cost</command-message>\n<command-args></command-args>"))
        self.assertIsNone(human_text("<command-message>review is running</command-message>\n<command-name>/review</command-name>"))
        self.assertIsNone(human_text("<local-command-stdout>Total cost: $0.10</local-command-stdout>"))
        self.assertIsNone(human_text("<local-command-stderr>oops</local-command-stderr>"))
        self.assertIsNone(human_text("<local-command-caveat>Caveat: generated locally</local-command-caveat>"))

    def test_bash_mode_wrappers(self) -> None:
        self.assertIsNone(human_text("<bash-input>git status</bash-input>"))
        self.assertIsNone(human_text("<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>"))

    def test_leading_system_block_disqualifies_trailing_text(self) -> None:
        note = "<task-notification>\n<task-id>a1</task-id>\n</task-notification>\nRead the output file to retrieve the result: /tmp/a1"
        self.assertIsNone(human_text(note))
        self.assertIsNone(human_text("<system-reminder>x</system-reminder>\n" + note))
        self.assertIsNone(human_text("<command-name>/x</command-name> trailing words"))
        nested = "<task-notification>a<task-notification>b</task-notification>c</task-notification> trailing"
        self.assertIsNone(human_text(nested))
        self.assertEqual(human_text("<task-notification>a<task-notification>b</task-notification> unclosed outer"),
                         "<task-notification>a unclosed outer")

    def test_unclosed_tag_is_human(self) -> None:
        self.assertEqual(human_text("<task-notification> lines should be ignored"), "<task-notification> lines should be ignored")

    def test_wrapper_in_the_middle_is_removed(self) -> None:
        self.assertEqual(human_text("before <bash-stdout>out</bash-stdout> after"), "before  after")

    def test_pasted_content_is_human(self) -> None:
        t = human_text('<pasted_content name="a.log" lines="1">\nERROR x\n</pasted_content>')
        self.assertIsNotNone(t)
        self.assertIn("ERROR x", t or "")


class HumanTurnTests(unittest.TestCase):
    def user(self, content: Any, **extra: Any) -> Dict[str, Any]:
        e: Dict[str, Any] = {"type": "user", "message": {"role": "user", "content": content}}
        e.update(extra)
        return e

    def queued(self, prompt: Any = "yes please", mode: Optional[str] = "prompt", **origin: Any) -> Dict[str, Any]:
        att: Dict[str, Any] = {"type": "queued_command", "prompt": prompt}
        if mode is not None:
            att["commandMode"] = mode
        if "origin" in origin:
            att["origin"] = origin["origin"]
        return {"type": "attachment", "attachment": att}

    def test_plain_prompt(self) -> None:
        self.assertEqual(human_turn(self.user("hello")), ("prompt", "hello"))

    def test_flags_exclude(self) -> None:
        for flag in ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly"):
            self.assertIsNone(human_turn(self.user("hello", **{flag: True})), flag)
        self.assertEqual(human_turn(self.user("hello", isMeta=False)), ("prompt", "hello"))

    def test_tool_use_result_key_excludes_even_when_null(self) -> None:
        self.assertIsNone(human_turn(self.user("hello", toolUseResult=None)))
        self.assertIsNone(human_turn(self.user([{"type": "tool_result", "content": "x"}], toolUseResult={"a": 1})))

    def test_tool_result_blocks_ignored(self) -> None:
        self.assertIsNone(human_turn(self.user([{"type": "tool_result", "content": "looks human?"}])))
        self.assertEqual(human_turn(self.user([{"type": "tool_result", "content": "x"}, {"type": "text", "text": "and this"}])), ("prompt", "and this"))

    def test_images_and_documents_count(self) -> None:
        self.assertEqual(human_turn(self.user([{"type": "image", "source": {}}])), ("prompt", ""))
        self.assertEqual(human_turn(self.user([{"type": "document", "source": {}}])), ("prompt", ""))
        self.assertEqual(human_turn(self.user([{"type": "text", "text": "<system-reminder>x</system-reminder>"}, {"type": "image"}])), ("prompt", ""))

    def test_wrapper_only_lists_are_not_human(self) -> None:
        self.assertIsNone(human_turn(self.user([{"type": "text", "text": "<system-reminder>x</system-reminder>"}])))
        self.assertIsNone(human_turn(self.user([])))
        self.assertIsNone(human_turn(self.user(None)))
        self.assertIsNone(human_turn({"type": "user"}))

    def test_text_blocks_join(self) -> None:
        turn = human_turn(self.user([{"type": "text", "text": "one"}, "two", {"type": "text", "text": "<system-reminder>r</system-reminder>"}]))
        self.assertEqual(turn, ("prompt", "one\ntwo"))

    def test_interrupt_markers(self) -> None:
        self.assertEqual(human_turn(self.user("[Request interrupted by user]")), ("interrupt", "[Request interrupted by user]"))
        self.assertEqual(human_turn(self.user([{"type": "text", "text": "[Request interrupted by user for tool use]"}]))[0], "interrupt")  # type: ignore[index]
        self.assertEqual(human_turn(self.user("[Request interrupted by user] then this"))[0], "prompt")  # type: ignore[index]

    def test_queued_command_origins(self) -> None:
        self.assertEqual(human_turn(self.queued()), ("prompt", "yes please"))  # origin absent
        self.assertEqual(human_turn(self.queued(origin=None)), ("prompt", "yes please"))
        self.assertEqual(human_turn(self.queued(origin={"kind": "human"})), ("prompt", "yes please"))
        self.assertIsNone(human_turn(self.queued(origin={"kind": "peer", "from": "agent-2"})))
        self.assertIsNone(human_turn(self.queued(origin={"kind": "task-notification"})))
        self.assertIsNone(human_turn(self.queued(origin="human")))

    def test_queued_command_modes_and_text(self) -> None:
        self.assertIsNone(human_turn(self.queued(mode="task-notification")))
        self.assertIsNone(human_turn(self.queued(mode=None)))
        self.assertIsNone(human_turn(self.queued(prompt="   ")))
        self.assertIsNone(human_turn(self.queued(prompt="<task-notification>x</task-notification>")))
        self.assertEqual(human_turn(self.queued(prompt=[{"type": "image"}])), ("prompt", ""))

    def test_other_entries_are_not_human(self) -> None:
        self.assertIsNone(human_turn({"type": "attachment", "attachment": {"type": "environment"}}))
        self.assertIsNone(human_turn({"type": "attachment"}))
        self.assertIsNone(human_turn({"type": "system", "subtype": "local_command", "content": "hi"}))
        self.assertIsNone(human_turn({"type": "assistant", "message": {"content": "hi"}}))
        self.assertIsNone(human_turn({"type": "last-prompt", "lastPrompt": "hi"}))

    def test_user_entry_origins(self) -> None:
        # Real user entries carry origin too (human, peer, task-notification, auto-continuation).
        # Today the non-human ones are also isMeta or wrapped; the origin alone must be enough.
        self.assertIsNone(human_turn(self.user("ok, starting", origin={"kind": "peer", "from": "agent-2"})))
        self.assertIsNone(human_turn(self.user("Read the output file", origin={"kind": "task-notification"})))
        self.assertIsNone(human_turn(self.user("Continue where you left off.", origin={"kind": "auto-continuation"})))
        self.assertIsNone(human_turn(self.user("hi", origin="human")))
        self.assertEqual(human_turn(self.user("hi", origin={"kind": "human"})), ("prompt", "hi"))
        self.assertEqual(human_turn(self.user("hi", origin=None)), ("prompt", "hi"))
        self.assertEqual(human_turn(self.user("hi")), ("prompt", "hi"))

    def test_peer_message_after_a_question_does_not_answer_it(self) -> None:
        lines = [
            json.dumps({"type": "user", "origin": {"kind": "peer"}, "message": {"content": "peer kickoff"}}),
            json.dumps({"type": "user", "message": {"content": "Ship the fix."}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed. Push it?"}]}}),
            json.dumps({"type": "user", "origin": {"kind": "peer"}, "message": {"content": "status: done"}}),
        ]
        s = parse_lines(lines, "x")
        self.assertEqual(s.first_human_text, "Ship the fix.")
        self.assertFalse(s.human_after_final)
        self.assertIn(tuple(classify.classify(s)), QUESTION_ENDINGS)

    def test_non_dict_and_unhashable_types(self) -> None:
        self.assertIsNone(human_turn({"type": ["user"], "message": {"content": "yes"}}))
        self.assertIsNone(human_turn("not an entry"))  # type: ignore[arg-type]
        self.assertIsNone(human_turn(self.user([{"type": ["image"]}, {"type": {"t": 1}}])))
        att = {"type": "attachment", "attachment": {"type": ["queued_command"], "commandMode": "prompt", "prompt": "yes"}}
        self.assertIsNone(human_turn(att))


# --------------------------------------------------------------------------- fixtures

class FixtureParseTests(unittest.TestCase):
    def test_fixtures_exist(self) -> None:
        self.assertGreaterEqual(len(fixture_paths()), 25)

    def test_head_tail_equals_whole_file_for_every_fixture(self) -> None:
        for p in fixture_paths():
            with self.subTest(fixture=p.name):
                self.assertEqual(parse_transcript(p), by_lines(p))

    def test_equality_holds_for_any_window_and_head_size(self) -> None:
        for p in fixture_paths():
            for head in (1, 5, 400):
                with mock.patch.object(transcript, "HEAD_LINES", head):
                    expected = by_lines(p)
                    for window in (1, 2, 7, 64, 333, 4096):
                        with self.subTest(fixture=p.name, window=window, head=head):
                            with mock.patch.object(transcript, "TAIL_START_BYTES", window):
                                self.assertEqual(parse_transcript(p), expected)

    def test_window_starting_exactly_on_each_line_boundary(self) -> None:
        for name in ("pos_question_then_nonhuman.jsonl", "neg_malformed_lines.jsonl", "neg_question_then_tool_use.jsonl"):
            p = FIXTURES / name
            data = p.read_bytes()
            expected = by_lines(p)
            starts = [i + 1 for i, b in enumerate(data) if b == 0x0A and i + 1 < len(data)]
            for s in starts:
                for start in (s - 1, s, s + 1):
                    with self.subTest(fixture=name, start=start):
                        with mock.patch.object(transcript, "TAIL_START_BYTES", len(data) - start):
                            self.assertEqual(parse_transcript(p), expected)

    def test_summary_basics(self) -> None:
        for p in fixture_paths():
            with self.subTest(fixture=p.name):
                s = parse_transcript(p)
                self.assertEqual(s.session_uuid, p.stem)
                self.assertEqual(s.path, str(p))
                self.assertEqual(s.size, p.stat().st_size)
                self.assertEqual(s.mtime, p.stat().st_mtime)

    def test_limit_meta_fields(self) -> None:
        s = parse_transcript(FIXTURES / "pos_limit_meta.jsonl")
        self.assertEqual(s.launch_cwd, "/Users/sam/workspace/acme-api")
        self.assertEqual(s.entrypoints, {"cli"})
        self.assertEqual(s.version, "2.1.281")
        self.assertEqual(s.git_branch, "fix/upload-retry")
        self.assertEqual(s.first_human_text, "Run the integration suite and fix whatever fails in the upload service.")
        self.assertTrue(s.final_is_api_error)
        self.assertEqual(s.final_error, "rate_limit")
        self.assertEqual(s.final_status, 429)
        self.assertTrue(s.final_has_quota)
        self.assertTrue(s.final_text and s.final_text.startswith("You"))
        self.assertIsNotNone(s.final_ts)
        self.assertIsNotNone(s.last_ts)
        self.assertGreater(s.last_ts, s.final_ts)  # trailing system entries came later
        self.assertFalse(s.human_after_final)
        self.assertFalse(s.tool_use_after_final)

    def test_text_only_limit_has_no_error_metadata(self) -> None:
        s = parse_transcript(FIXTURES / "pos_limit_text_only.jsonl")
        self.assertFalse(s.final_is_api_error)
        self.assertIsNone(s.final_error)
        self.assertIsNone(s.final_status)
        self.assertFalse(s.final_has_quota)
        self.assertEqual(s.final_text, "Claude AI usage limit reached|1759363200")

    def test_limit_then_unanswered_continue(self) -> None:
        s = parse_transcript(FIXTURES / "pos_limit_then_continue_unanswered.jsonl")
        self.assertTrue(s.human_after_final)
        self.assertEqual(s.final_error, "rate_limit")
        self.assertFalse(s.final_has_quota)

    def test_first_prompt_reminder_stripped_and_nonhuman_tail(self) -> None:
        s = parse_transcript(FIXTURES / "pos_question_then_nonhuman.jsonl")
        self.assertEqual(s.first_human_text, "Rewrite the release note for v2.3 in two lengths.")
        self.assertFalse(s.human_after_final)
        self.assertFalse(s.tool_use_after_final)
        self.assertTrue(s.final_text and s.final_text.endswith("Should I publish the shorter one?"))

    def test_after_final_flags(self) -> None:
        self.assertTrue(parse_transcript(FIXTURES / "neg_question_then_tool_use.jsonl").tool_use_after_final)
        for name in ("neg_question_then_queued_human.jsonl", "neg_question_then_pasted_content.jsonl", "neg_interrupted_by_user.jsonl"):
            with self.subTest(fixture=name):
                s = parse_transcript(FIXTURES / name)
                self.assertTrue(s.human_after_final)
                self.assertFalse(s.tool_use_after_final)

    def test_answered_question_is_not_final(self) -> None:
        s = parse_transcript(FIXTURES / "neg_answered_then_committed.jsonl")
        self.assertEqual(s.final_text, "Committed.")
        self.assertFalse(s.human_after_final)

    def test_metadata_only(self) -> None:
        s = parse_transcript(FIXTURES / "neg_metadata_only.jsonl")
        self.assertIsNone(s.final_text)
        self.assertIsNone(s.final_ts)
        self.assertIsNone(s.launch_cwd)
        self.assertIsNone(s.version)
        self.assertIsNone(s.first_human_text)
        self.assertIsNone(s.last_ts)
        self.assertEqual(s.entrypoints, set())

    def test_sidechain_ignored(self) -> None:
        s = parse_transcript(FIXTURES / "neg_sidechain_question.jsonl")
        self.assertEqual(s.final_text, "The migration ran on dev and all checks pass.")
        self.assertFalse(s.human_after_final)

    def test_malformed_lines_skipped(self) -> None:
        s = parse_transcript(FIXTURES / "neg_malformed_lines.jsonl")
        self.assertEqual(s.launch_cwd, "/Users/sam/workspace/widgets")
        self.assertEqual(s.first_human_text, "Update the README badges and push.")
        self.assertEqual(s.final_text, "Updated the README badges and pushed the docs branch.")
        self.assertFalse(p_ends_with_newline(FIXTURES / "neg_malformed_lines.jsonl"))

    def test_sdk_cli_entrypoint(self) -> None:
        s = parse_transcript(FIXTURES / "auto_sdk_cli_question.jsonl")
        self.assertEqual(s.entrypoints, {"sdk-cli"})

    def test_session_ids_inside_fixtures_are_uuids(self) -> None:
        for p in fixture_paths():
            with self.subTest(fixture=p.name):
                ids = set()
                for raw in read_lines(p):
                    e = transcript._decode(raw)
                    if e and isinstance(e.get("sessionId"), str):
                        ids.add(e["sessionId"])
                self.assertEqual(len(ids), 1)
                self.assertTrue(transcript.is_session_file(ids.pop() + ".jsonl"))


def p_ends_with_newline(path: Path) -> bool:
    return path.read_bytes().endswith(b"\n")


# --------------------------------------------------------------------------- big files

class LargeTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tail_window_expands_past_600_kib_of_nonhuman_lines(self) -> None:
        b = big_question_session(filler=700 * KIB)
        p = b.write(self.dir / "a1b2c3d4-0000-4000-8000-000000000001.jsonl")
        data = p.read_bytes()
        question_at = data.index(b"Want me to push the release branch?")
        self.assertGreater(len(data) - question_at, 600 * KIB)
        starts: List[int] = []
        real = transcript._read_window

        def spy(f: Any, start: int, size: int) -> List[bytes]:
            starts.append(start)
            return real(f, start, size)

        with mock.patch.object(transcript, "_read_window", spy):
            s = parse_transcript(p)
        self.assertGreater(len(starts), 1, "window never expanded")
        self.assertLess(starts[-1], question_at)
        self.assertGreater(starts[0], question_at)
        self.assertEqual(s, by_lines(p))
        self.assertIn(tuple(classify.classify(s, classify.DEFAULT_IGNORE_TAIL_PATTERNS)), QUESTION_ENDINGS)
        self.assertEqual(s.first_human_text, "Prepare the release branch and run the full suite.")
        self.assertEqual(s.session_uuid, "a1b2c3d4-0000-4000-8000-000000000001")

    def test_human_turn_at_the_very_end_of_a_big_file(self) -> None:
        b = big_question_session(filler=650 * KIB)
        b.attachment({"type": "queued_command", "commandMode": "prompt", "prompt": "no, wait for the review", "origin": None})
        b.nonhuman_filler(10 * KIB)
        p = b.write(self.dir / "big-human.jsonl")
        s = parse_transcript(p)
        self.assertTrue(s.human_after_final)
        self.assertEqual(s, by_lines(p))
        self.assertEqual(classify.classify(s), ("clean", "human_after_final"))

    def test_head_without_conversation_falls_back_to_final_entry(self) -> None:
        b = Builder(cwd="/Users/sam/workspace/late-start")
        for i in range(450):
            b.meta("file-history-delta", n=i)
        b.user("Tidy the changelog.")
        b.text("Changelog tidied. Want me to tag it?")
        p = b.write(self.dir / "late.jsonl")
        s = parse_transcript(p)
        self.assertEqual(s, by_lines(p))
        self.assertEqual(s.launch_cwd, "/Users/sam/workspace/late-start")  # from the final text's entry
        self.assertEqual(s.git_branch, "main")
        self.assertEqual(s.entrypoints, set())
        self.assertIsNone(s.version)
        self.assertIsNone(s.first_human_text)

    def test_head_fields_only_come_from_the_head(self) -> None:
        b = Builder(cwd="/Users/sam/workspace/first", entrypoint="cli")
        b.user("first prompt")
        b.text("ok")
        for i in range(450):  # push everything below past the 400-line head
            b.system("turn_duration", durationMs=i)
        b.nonhuman_filler(300 * KIB)  # and past the first tail window, so head and tail do not overlap
        b.cwd = "/Users/sam/workspace/second"
        b.entrypoint = "sdk-cli"
        for _ in range(50):
            b.user("later prompt")
        b.text("All done.")
        p = b.write(self.dir / "head.jsonl")
        self.assertGreater(len(b.lines), transcript.HEAD_LINES)
        s = parse_transcript(p)
        self.assertEqual(s, by_lines(p))
        self.assertEqual(s.launch_cwd, "/Users/sam/workspace/first")
        self.assertEqual(s.entrypoints, {"cli"})
        self.assertEqual(s.first_human_text, "first prompt")
        self.assertEqual(s.final_text, "All done.")

    def test_max_tail_bytes_bounds_the_window(self) -> None:
        b = big_question_session(filler=700 * KIB)
        p = b.write(self.dir / "capped.jsonl")
        capped = parse_transcript(p, max_tail_bytes=256 * KIB)
        self.assertIsNone(capped.final_text)
        self.assertEqual(classify.classify(capped), ("empty", "no_final_text"))
        self.assertIsNotNone(parse_transcript(p, max_tail_bytes=2048 * KIB).final_text)

    def test_file_without_final_text_scans_to_the_start(self) -> None:
        b = Builder()
        b.user("hello")
        b.nonhuman_filler(600 * KIB)
        p = b.write(self.dir / "notext.jsonl")
        s = parse_transcript(p)
        self.assertIsNone(s.final_text)
        self.assertEqual(s, by_lines(p))


# --------------------------------------------------------------------------- modes and cuts

class ModeTests(unittest.TestCase):
    def test_max_lines_matches_a_line_cut(self) -> None:
        for name in ("pos_limit_meta.jsonl", "neg_answered_then_committed.jsonl", "neg_malformed_lines.jsonl"):
            p = FIXTURES / name
            lines = read_lines(p)
            for n in (0, 1, 7, len(lines) // 2, len(lines), len(lines) + 5):
                with self.subTest(fixture=name, n=n):
                    cut = parse_transcript(p, max_lines=n)
                    self.assertEqual(cut, parse_lines(lines[:n], p.stem, p))
                    self.assertEqual(cut.size, sum(len(x) for x in lines[:n]))

    def test_cut_changes_the_ending(self) -> None:
        p = FIXTURES / "neg_answered_then_committed.jsonl"
        lines = read_lines(p)
        idx = next(i for i, x in enumerate(lines) if b"Want me to commit these 18 files?" in x)
        s = parse_transcript(p, max_lines=idx + 1)
        self.assertIn(tuple(classify.classify(s)), QUESTION_ENDINGS)
        self.assertEqual(classify.classify(parse_transcript(p)), ("clean", "no_question"))

    def test_tail_only_mode(self) -> None:
        for p in fixture_paths():
            with self.subTest(fixture=p.name):
                full = parse_transcript(p)
                tail = parse_transcript(p, head=False)
                self.assertEqual(tail.entrypoints, set())
                self.assertIsNone(tail.first_human_text)
                self.assertIsNone(tail.version)
                for name in ("final_text", "final_ts", "final_is_api_error", "final_error", "final_status",
                             "final_has_quota", "human_after_final", "tool_use_after_final", "last_ts", "size",
                             "pending_tool", "final_resets_at"):
                    self.assertEqual(getattr(tail, name), getattr(full, name), name)
                self.assertEqual(tail.head_human_turns, 0)
                self.assertEqual(tail, parse_lines(read_lines(p), p.stem, p, head=False))

    def test_parse_lines_accepts_str_and_whole_buffers(self) -> None:
        p = FIXTURES / "pos_trailing_question.jsonl"
        data = p.read_bytes()
        expected = parse_lines(read_lines(p), p.stem, p)
        self.assertEqual(parse_lines(data, p.stem, p), expected)
        self.assertEqual(parse_lines(data.decode("utf-8"), p.stem, p), expected)
        self.assertEqual(parse_lines([x.decode("utf-8") for x in read_lines(p)], p.stem, p), expected)

    def test_parse_lines_without_path(self) -> None:
        s = parse_lines([b'{"type":"assistant","message":{"content":[{"type":"text","text":"Hi?"}]}}\n'], "abc")
        self.assertIsNone(s.path)
        self.assertIsNone(s.mtime)
        self.assertEqual(s.final_text, "Hi?")
        self.assertIsNone(s.final_ts)
        self.assertIsNone(s.last_ts)

    def test_string_content_and_odd_blocks(self) -> None:
        lines = [
            json.dumps({"type": "assistant", "timestamp": "t1", "message": {"content": "plain string reply?"}}),
            json.dumps({"type": "assistant", "timestamp": "t2", "message": {"content": [None, "x", {"type": "text", "text": "   "}, {"type": "text"}]}}),
            json.dumps({"type": "assistant", "timestamp": "t3", "message": None}),
        ]
        s = parse_lines(lines, "odd")
        self.assertEqual(s.final_text, "plain string reply?")
        self.assertEqual(s.final_ts, "t1")
        self.assertEqual(s.last_ts, "t3")

    def test_tool_use_in_the_same_entry_after_the_text(self) -> None:
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Doing it?"}, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}})
        self.assertTrue(parse_lines([line], "x").tool_use_after_final)
        line2 = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}, {"type": "text", "text": "Done?"}]}})
        self.assertFalse(parse_lines([line2], "x").tool_use_after_final)
        line3 = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Search?"}, {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}]}})
        self.assertTrue(parse_lines([line3], "x").tool_use_after_final)

    def test_api_status_parsing(self) -> None:
        def status(v: Any) -> Optional[int]:
            line = json.dumps({"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": v, "message": {"content": [{"type": "text", "text": "API Error"}]}})
            return parse_lines([line], "x").final_status
        self.assertEqual(status(529), 529)
        self.assertEqual(status("429"), 429)
        self.assertIsNone(status(None))
        self.assertIsNone(status(True))
        self.assertIsNone(status("n/a"))


# --------------------------------------------------------------------------- pending tool, head turns, resetsAt

def _a(*blocks: Dict[str, Any], **extra: Any) -> str:
    e: Dict[str, Any] = {"type": "assistant", "timestamp": "2026-10-02T10:00:00.000Z", "message": {"content": list(blocks)}}
    e.update(extra)
    return json.dumps(e)


def _u(content: Any, **extra: Any) -> str:
    e: Dict[str, Any] = {"type": "user", "timestamp": "2026-10-02T10:00:01.000Z", "message": {"content": content}}
    e.update(extra)
    return json.dumps(e)


def _txt(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def _tool(name: str, tid: Optional[str]) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "tool_use", "name": name, "input": {}}
    if tid is not None:
        block["id"] = tid
    return block


def _result(tid: str, body: str = "ok", **extra: Any) -> str:
    block: Dict[str, Any] = {"type": "tool_result", "tool_use_id": tid, "content": body}
    block.update(extra)
    return _u([block], toolUseResult={"stdout": body})


class PendingToolTests(unittest.TestCase):
    """SessionSummary.pending_tool (contract section 7 rule 4) on runtime-built transcripts."""

    def both(self, lines: List[str]) -> transcript.SessionSummary:
        """parse_lines, after checking parse_transcript agrees for several window sizes."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.jsonl"
            p.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
            expected = parse_lines(read_lines(p), "p", p)
            for window in (1, 50, 300, 4096):
                with mock.patch.object(transcript, "TAIL_START_BYTES", window):
                    self.assertEqual(parse_transcript(p), expected, window)
        return expected

    def test_unanswered_question_tools(self) -> None:
        for name, reason in (("AskUserQuestion", "ask_user"), ("ExitPlanMode", "plan_approval")):
            with self.subTest(tool=name):
                s = self.both([_u("Set it up."), _a(_txt("Two decisions first.")), _a(_tool(name, "toolu_q"))])
                self.assertEqual(s.pending_tool, name)
                self.assertTrue(s.tool_use_after_final)
                self.assertEqual(classify.classify(s), ("open_question", reason))

    def test_same_entry_after_the_text(self) -> None:
        s = self.both([_a(_txt("Two decisions first."), _tool("AskUserQuestion", "q1"))])
        self.assertEqual(s.pending_tool, "AskUserQuestion")
        s = self.both([_a(_tool("AskUserQuestion", "q1"), _txt("Never mind, picking Postgres."))])
        self.assertIsNone(s.pending_tool)  # text written after the tool use: the conversation moved on

    def test_answered_or_rejected(self) -> None:
        base = [_a(_txt("Two decisions first.")), _a(_tool("AskUserQuestion", "q1"))]
        s = self.both(base + [_result("q1", "User has answered your questions")])
        self.assertIsNone(s.pending_tool)
        self.assertEqual(classify.classify(s), ("clean", "tool_use_after_final"))
        s = self.both(base + [_result("q1", "The user doesn't want to proceed", is_error=True)])
        self.assertIsNone(s.pending_tool)

    def test_result_for_another_id_does_not_answer(self) -> None:
        s = self.both([_a(_txt("Checking.")), _a(_tool("AskUserQuestion", "q1")), _result("other")])
        self.assertEqual(s.pending_tool, "AskUserQuestion")

    def test_only_the_last_tool_use_counts(self) -> None:
        s = self.both([_a(_txt("Checking.")), _a(_tool("AskUserQuestion", "q1")), _a(_tool("Bash", "b1"))])
        self.assertIsNone(s.pending_tool)
        s = self.both([_a(_txt("Checking.")), _a(_tool("Bash", "b1")), _result("b1"), _a(_tool("ExitPlanMode", "p1"))])
        self.assertEqual(s.pending_tool, "ExitPlanMode")

    def test_a_later_final_text_forgets_the_tool(self) -> None:
        s = self.both([_a(_txt("Which one?")), _a(_tool("AskUserQuestion", "q1")), _a(_txt("Going with Postgres."))])
        self.assertIsNone(s.pending_tool)
        self.assertEqual(classify.classify(s), ("clean", "no_question"))

    def test_human_turn_after_an_unanswered_question_tool(self) -> None:
        s = self.both([_a(_txt("Two decisions first.")), _a(_tool("AskUserQuestion", "q1")), _u("use postgres")])
        self.assertEqual(s.pending_tool, "AskUserQuestion")
        self.assertTrue(s.human_after_final)
        self.assertEqual(classify.classify(s), ("open_question", "ask_user"))  # rule 4 comes before 4b

    def test_sidechain_and_other_tools_are_ignored(self) -> None:
        s = self.both([_a(_txt("Done.")), _a(_tool("AskUserQuestion", "q1"), isSidechain=True)])
        self.assertIsNone(s.pending_tool)
        s = self.both([_a(_txt("Done.")), _a(_tool("Bash", "b1"))])
        self.assertIsNone(s.pending_tool)
        s = self.both([_a(_txt("Done.")), _a({"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}})])
        self.assertIsNone(s.pending_tool)

    def test_tool_use_without_an_id(self) -> None:
        s = self.both([_a(_txt("Two decisions first.")), _a(_tool("AskUserQuestion", None))])
        self.assertEqual(s.pending_tool, "AskUserQuestion")
        s = self.both([_a(_txt("Two decisions first.")), _a(_tool("AskUserQuestion", None)), _result("whatever")])
        self.assertIsNone(s.pending_tool)

    def test_no_final_text(self) -> None:
        s = self.both([_u("Plan it."), _a(_tool("ExitPlanMode", "p1"))])
        self.assertEqual(s.pending_tool, "ExitPlanMode")
        self.assertEqual(classify.classify(s), ("empty", "no_final_text"))  # rule 1 first

    def test_pending_tool_survives_a_big_nonhuman_tail(self) -> None:
        b = Builder()
        b.user("Set up the database layer.")
        b.text("I need two decisions from you before I write the migrations.")
        b.put(b.env("assistant", message={"role": "assistant", "content": [_tool("AskUserQuestion", "toolu_q")]}))
        b.nonhuman_filler(650 * KIB)  # sidechain tool uses and task notifications only
        with tempfile.TemporaryDirectory() as d:
            p = b.write(Path(d) / "big.jsonl")
            s = parse_transcript(p)
            self.assertEqual(s, by_lines(p))
        self.assertEqual(s.pending_tool, "AskUserQuestion")
        self.assertEqual(classify.classify(s, classify.DEFAULT_IGNORE_TAIL_PATTERNS), ("open_question", "ask_user"))

    def test_fixtures(self) -> None:
        self.assertEqual(parse_transcript(FIXTURES / "pos_pending_ask_user.jsonl").pending_tool, "AskUserQuestion")
        self.assertEqual(parse_transcript(FIXTURES / "pos_pending_exit_plan.jsonl").pending_tool, "ExitPlanMode")
        answered = parse_transcript(FIXTURES / "neg_ask_user_answered.jsonl")
        self.assertIsNone(answered.pending_tool)
        self.assertFalse(answered.tool_use_after_final)
        for p in fixture_paths():
            if not p.name.startswith("pos_pending_"):
                with self.subTest(fixture=p.name):
                    self.assertIsNone(parse_transcript(p).pending_tool)


class HeadHumanTurnTests(unittest.TestCase):
    def test_counts_prompts_and_interrupts_in_the_head(self) -> None:
        lines = [
            json.dumps({"type": "mode", "mode": "default"}),
            _u("first prompt"),
            _a(_txt("ok")),
            _u("<system-reminder>x</system-reminder>", isMeta=True),
            _result("t1"),
            _u("<command-name>/cost</command-name>"),
            _u("[Request interrupted by user]"),
            json.dumps({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "and this"}}),
            json.dumps({"type": "attachment", "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "peer", "origin": {"kind": "peer"}}}),
            _u("sidechain prompt", isSidechain=True),
            _u("second prompt"),
        ]
        self.assertEqual(parse_lines(lines, "h").head_human_turns, 4)  # 2 prompts, 1 interrupt, 1 queued
        with mock.patch.object(transcript, "HEAD_LINES", 3):
            self.assertEqual(parse_lines(lines, "h").head_human_turns, 1)  # only the head counts
        self.assertEqual(parse_lines(lines, "h", head=False).head_human_turns, 0)

    def test_fixtures(self) -> None:
        self.assertEqual(parse_transcript(FIXTURES / "auto_sdk_cli_question.jsonl").head_human_turns, 1)
        self.assertEqual(parse_transcript(FIXTURES / "neg_metadata_only.jsonl").head_human_turns, 0)
        self.assertEqual(parse_transcript(FIXTURES / "pos_limit_continue_limit.jsonl").head_human_turns, 2)


class ResetsAtTests(unittest.TestCase):
    def resets(self, quota: Any, **extra: Any) -> Optional[int]:
        e: Dict[str, Any] = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                             "message": {"content": [_txt("You've hit your weekly limit")]}}
        if quota is not None:
            e["quotaLimits"] = quota
        e.update(extra)
        return parse_lines([json.dumps(e)], "x").final_resets_at

    def test_values(self) -> None:
        self.assertEqual(self.resets({"resetsAt": 1791320400, "status": "rejected"}), 1791320400)
        self.assertEqual(self.resets({"resetsAt": 1791320400.0}), 1791320400)
        self.assertEqual(self.resets({"resetsAt": "1791320400"}), 1791320400)
        for bad in ({"resetsAt": True}, {"resetsAt": -5}, {"resetsAt": 0}, {"resetsAt": "soon"},
                    {"resetsAt": None}, {"resetsAt": float("nan")}, {}, [1791320400], "1791320400", None):
            with self.subTest(quota=bad):
                self.assertIsNone(self.resets(bad))

    def test_only_the_final_entry_counts(self) -> None:
        limit = json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                            "quotaLimits": {"resetsAt": 1791320400}, "message": {"content": [_txt("You've hit your weekly limit")]}})
        later = _a(_txt("Back again. All done."))
        self.assertEqual(parse_lines([limit], "x").final_resets_at, 1791320400)
        self.assertIsNone(parse_lines([limit, _u("continue"), later], "x").final_resets_at)

    def test_fixture(self) -> None:
        self.assertEqual(parse_transcript(FIXTURES / "pos_limit_meta.jsonl").final_resets_at, 1759834800)
        self.assertIsNone(parse_transcript(FIXTURES / "pos_limit_text_only.jsonl").final_resets_at)

    def test_new_fields_are_in_safe_dict(self) -> None:
        safe = parse_transcript(FIXTURES / "pos_pending_ask_user.jsonl").safe_dict()
        self.assertEqual(safe["pending_tool"], "AskUserQuestion")
        self.assertEqual(safe["head_human_turns"], 1)
        self.assertIsNone(safe["final_resets_at"])


# --------------------------------------------------------------------------- small helpers

class HelperTests(unittest.TestCase):
    def test_content_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.jsonl"
            p.write_bytes(b"")
            self.assertEqual(transcript.content_version(p), "0:" + hashlib.sha1(b"").hexdigest())
            small = b'{"type":"user"}\n' * 10
            p.write_bytes(small)
            self.assertEqual(transcript.content_version(p), "%d:%s" % (len(small), hashlib.sha1(small).hexdigest()))
            big = bytes(range(256)) * 1000  # 256000 bytes
            p.write_bytes(big)
            expected = "%d:%s" % (len(big), hashlib.sha1(big[-64 * KIB:]).hexdigest())
            self.assertEqual(transcript.content_version(p), expected)
            self.assertEqual(transcript.content_version(str(p)), expected)
            self.assertEqual(transcript.content_version_of(big), expected)
            with open(p, "ab") as f:
                f.write(b"\n")
            self.assertNotEqual(transcript.content_version(p), expected)

    def test_split_lines(self) -> None:
        self.assertEqual(transcript.split_lines(b"a\nb\n"), [b"a\n", b"b\n"])
        self.assertEqual(transcript.split_lines(b"a\nb"), [b"a\n", b"b"])
        self.assertEqual(transcript.split_lines(b""), [])
        self.assertEqual(transcript.split_lines("x\u2028y\r\nz"), ["x\u2028y\r\n", "z"])

    def test_u2028_inside_a_json_string_is_not_a_line_break(self) -> None:
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "one\u2028two?"}]}}, ensure_ascii=False)
        s = parse_lines(line + "\n", "x")
        self.assertEqual(s.final_text, "one\u2028two?")

    def test_is_session_file(self) -> None:
        self.assertTrue(transcript.is_session_file("/x/0b5e6c1d-2f3a-4b7c-8d9e-0a1b2c3d4e5f.jsonl"))
        self.assertFalse(transcript.is_session_file("/x/0b5e6c1d-2f3a-4b7c-8d9e-0a1b2c3d4e5f.json"))
        self.assertFalse(transcript.is_session_file("/x/notes.jsonl"))
        self.assertFalse(transcript.is_session_file(Path("pos_limit_meta.jsonl")))

    def test_text_fields_stay_out_of_repr_and_safe_dict(self) -> None:
        s = parse_transcript(FIXTURES / "pos_trailing_question.jsonl")
        self.assertNotIn("commit these 18 files", repr(s))
        self.assertNotIn("formatter", repr(s))
        safe = json.dumps(s.safe_dict())
        self.assertNotIn("commit these 18 files", safe)
        self.assertNotIn("/Users/", safe)
        self.assertEqual(s.safe_dict()["final_text_len"], len(s.final_text or ""))

    def test_bad_lines_never_raise(self) -> None:
        junk = [b"", b"   ", b"{", b"[1,2]", b'"str"', b"null", b"\xff\xfe\x00", b"[" * 100000 + b"]" * 100000, b'{"type": 5}']
        s = parse_lines(junk, "junk")
        self.assertIsNone(s.final_text)

    def test_unhashable_type_values_are_skipped_not_fatal(self) -> None:
        # Valid JSON with a list or dict "type" used to raise TypeError ("unhashable type") and
        # abort the whole parse; the contract says bad lines are skipped.
        q = json.dumps({"type": "assistant", "timestamp": "t1", "message": {"content": [{"type": "text", "text": "Push it?"}]}})
        junk = [
            json.dumps({"type": ["user"], "message": {"content": "yes"}}),
            json.dumps({"type": {"k": 1}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": ["tool_use"]}, {"type": {"a": 1}, "text": "x?"}]}}),
            json.dumps({"type": "user", "message": {"content": [{"type": ["image"]}, {"type": {"t": 1}}]}}),
            json.dumps({"type": "attachment", "attachment": {"type": ["queued_command"], "commandMode": "prompt", "prompt": "yes"}}),
        ]
        s = parse_lines(junk + [q] + junk, "x")
        self.assertEqual(s.final_text, "Push it?")
        self.assertFalse(s.human_after_final)
        self.assertFalse(s.tool_use_after_final)
        self.assertIn(tuple(classify.classify(s)), QUESTION_ENDINGS)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "junk.jsonl"
            p.write_text("\n".join(junk + [q] + junk) + "\n", encoding="utf-8")
            self.assertEqual(parse_transcript(p), by_lines(p))  # junk in the head and in the tail window
            self.assertEqual(parse_transcript(p).final_text, "Push it?")


class FuzzTests(unittest.TestCase):
    """Seeded fuzz: random JSON shapes in every field the parser reads never raise, and the two
    readers still agree. (With the old raw ``type`` lookup this raised TypeError in 449 of 500.)"""

    def junk(self, rng: Any, depth: int = 0) -> Any:
        r = rng.random()
        if depth > 2 or r < 0.3:
            return rng.choice([None, True, False, 0, 7, -1.5, "", "x", "Q?", "rate_limit", "429", [], {},
                               "user", "text", "image", "tool_use", "human", "prompt"])
        if r < 0.6:
            return [self.junk(rng, depth + 1) for _ in range(rng.randint(0, 3))]
        return {rng.choice(["type", "text", "kind", "content", "a"]): self.junk(rng, depth + 1) for _ in range(rng.randint(0, 3))}

    def entry(self, rng: Any) -> Dict[str, Any]:
        e: Dict[str, Any] = {}
        for k in ("type", "message", "attachment", "isSidechain", "isMeta", "isCompactSummary",
                  "isVisibleInTranscriptOnly", "toolUseResult", "origin", "timestamp", "cwd", "gitBranch",
                  "version", "entrypoint", "isApiErrorMessage", "error", "apiErrorStatus", "quotaLimits"):
            if rng.random() < 0.5:
                e[k] = self.junk(rng)
        if rng.random() < 0.6:
            e["type"] = rng.choice(["user", "assistant", "attachment", "system"])
        if rng.random() < 0.5:
            e["message"] = {"content": rng.choice([self.junk(rng), [self.junk(rng) for _ in range(3)],
                                                   [{"type": self.junk(rng), "text": self.junk(rng)} for _ in range(3)]])}
        if rng.random() < 0.3:
            e["attachment"] = {"type": rng.choice(["queued_command", self.junk(rng)]),
                               "commandMode": rng.choice(["prompt", self.junk(rng)]),
                               "prompt": self.junk(rng), "origin": self.junk(rng)}
        return e

    def test_random_entries_never_raise_and_readers_agree(self) -> None:
        import random
        rng = random.Random(20261005)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fuzz.jsonl"
            for trial in range(600):
                lines = [json.dumps(self.entry(rng)) + "\n" for _ in range(rng.randint(1, 12))]
                s = parse_lines(lines, "fuzz")
                classify.classify(s, rng.choice([classify.DEFAULT_IGNORE_TAIL_PATTERNS, None, 5, ["(bad"], [None]]))
                s.safe_dict()
                repr(s)
                if trial % 5 == 0:
                    p.write_text("".join(lines), encoding="utf-8")
                    with mock.patch.object(transcript, "TAIL_START_BYTES", 64), mock.patch.object(transcript, "HEAD_LINES", 3):
                        self.assertEqual(parse_transcript(p), parse_lines(read_lines(p), p.stem, p))


class PerformanceTests(unittest.TestCase):
    """Wrapper stripping stays linear on hostile user text (the hook has a sub-second budget).

    An unbounded attribute scan (``[^>]*``) took 7.7 s on the first input below.
    """

    def test_unclosed_tag_openers_are_linear(self) -> None:
        for name, text in (("reminder openers", "<system-reminder a" * 5000),
                           ("leading block", "<task-notification>" + "<task-notification a" * 5000),
                           ("command openers", "<command-name " * 8000)):
            with self.subTest(case=name):
                t0 = time.perf_counter()
                human_text(text)
                self.assertLess(time.perf_counter() - t0, 1.5)

    def test_attributes_still_recognised(self) -> None:
        self.assertIsNone(human_text('<system-reminder priority="high" id="r1">x</system-reminder>'))
        self.assertIsNone(human_text('<command-name kind="slash">/x</command-name> trailing'))


# --------------------------------------------------------------------------- pending_question (contract 16.2)

def _ask(tid: str, input_: Any, name: str = "AskUserQuestion") -> Dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": input_}


def _q(text: Any) -> Dict[str, Any]:
    return {"question": text, "header": "Pick", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": False}


def _write(d: str, name: str, lines: List[str]) -> Path:
    p = Path(d) / name
    p.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    return p


class PendingQuestionTests(unittest.TestCase):
    """SessionSummary.pending_question: set exactly with pending_tool, same from both readers."""

    def both(self, lines: List[str]) -> transcript.SessionSummary:
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "q.jsonl", lines)
            expected = parse_lines(read_lines(p), "q", p)
            for head in (1, 400):
                with mock.patch.object(transcript, "HEAD_LINES", head):
                    same_head = parse_lines(read_lines(p), "q", p)
                    for window in (1, 50, 300, 4096):
                        with mock.patch.object(transcript, "TAIL_START_BYTES", window):
                            got = parse_transcript(p)
                        self.assertEqual(got, same_head, (window, head))
                        self.assertEqual(got.pending_question, expected.pending_question, (window, head))
            self.assertEqual(parse_transcript(p, head=False).pending_question, expected.pending_question)
        if expected.pending_tool is None:
            self.assertIsNone(expected.pending_question)
        return expected

    def ask(self, input_: Any, *after: str) -> transcript.SessionSummary:
        return self.both([_u("Set it up."), _a(_txt("Two decisions first.")), _a(_ask("toolu_q", input_))] + list(after))

    def test_ask_user_shapes(self) -> None:
        cases = [
            ({"questions": [_q("Which database should the cache use?")]}, "Which database should the cache use?"),
            ({"questions": [_q("Which database?"), _q("Run migrations on deploy?")]}, "Which database? (+1 more)"),
            ({"questions": [_q("Which database?"), _q("Migrations?"), _q("Region?"), "junk"]}, "Which database? (+3 more)"),
            ({"questions": [_q("  Which database?\n")]}, "Which database?"),
            ({"question": "Ship the hotfix tonight?"}, "Ship the hotfix tonight?"),       # legacy shape
            ({"questions": [], "question": "Legacy fallback?"}, "Legacy fallback?"),
            ({"questions": [_q(7)], "question": "Legacy fallback?"}, "Legacy fallback?"),
        ]
        for input_, expected in cases:
            with self.subTest(input=input_):
                s = self.ask(input_)
                self.assertEqual(s.pending_tool, "AskUserQuestion")
                self.assertEqual(s.pending_question, expected)

    def test_non_text_questions_give_none_but_keep_the_tool(self) -> None:
        for input_ in ({}, None, [], "Which one?", {"questions": "Which one?"}, {"questions": ["Which one?"]},
                       {"questions": [_q(None)]}, {"questions": [_q(["Which one?"])]}, {"questions": [_q({"t": "x"})]},
                       {"questions": [_q("   ")]}, {"question": 5}, {"question": "  "}, {"questions": [{}]}):
            with self.subTest(input=input_):
                s = self.ask(input_)
                self.assertEqual(s.pending_tool, "AskUserQuestion")
                self.assertIsNone(s.pending_question)

    def test_answered_rejected_or_moved_on(self) -> None:
        good = {"questions": [_q("Which database?")]}
        self.assertIsNone(self.ask(good, _result("toolu_q", "User has answered your questions")).pending_question)
        self.assertIsNone(self.ask(good, _result("toolu_q", "No", is_error=True)).pending_question)
        self.assertIsNone(self.ask(good, _a(_txt("Going with Postgres."))).pending_question)
        self.assertIsNone(self.ask(good, _a(_tool("Bash", "b1"))).pending_question)
        # A result for another id does not answer it; a human turn does not clear it (rule 4 first).
        self.assertEqual(self.ask(good, _result("other")).pending_question, "Which database?")
        self.assertEqual(self.ask(good, _u("postgres please")).pending_question, "Which database?")

    def test_the_last_question_tool_is_the_one_quoted(self) -> None:
        s = self.both([_a(_txt("Two rounds.")), _a(_ask("q1", {"questions": [_q("First round?")]})),
                       _result("q1", "answered"), _a(_ask("q2", {"questions": [_q("Second round?")]}))])
        self.assertEqual(s.pending_question, "Second round?")
        s = self.both([_a(_txt("Then the plan.")), _a(_ask("q1", {"questions": [_q("Which one?")]})),
                       _result("q1", "answered"), _a(_ask("p1", {"plan": "# Plan: move billing"}, "ExitPlanMode"))])
        self.assertEqual((s.pending_tool, s.pending_question), ("ExitPlanMode", "Plan: move billing"))

    def test_exit_plan_mode_shapes(self) -> None:
        cases = [
            ("# Plan: Split billing into its own package\n\n## Steps\n1. Move it", "Plan: Split billing into its own package"),
            ("\n\n   \n## Steps\n1. Move it", "Steps"),
            ("- **Move** src/billing  \n- Point callers", "Move** src/billing"),  # leading only (16.2)
            ("* item one\n* item two", "item one"),
            ("  -  #  * mixed markers", "mixed markers"),
            ("1. Create packages/billing.\n2. Run the suite.", "1. Create packages/billing."),
            ("---\n\n# Title after a rule", "Title after a rule"),
            ("\r\n# Windows newlines\r\nmore", "Windows newlines"),
        ]
        for plan, expected in cases:
            with self.subTest(plan=plan):
                s = self.both([_u("Plan it."), _a(_txt("Here is the plan.")), _a(_ask("p1", {"plan": plan}, "ExitPlanMode"))])
                self.assertEqual(s.pending_tool, "ExitPlanMode")
                self.assertEqual(s.pending_question, expected)

    def test_exit_plan_mode_without_a_text_plan(self) -> None:
        for input_ in ({}, None, {"plan": None}, {"plan": 42}, {"plan": ["# a"]}, {"plan": "   \n\n"}, {"plan": "#\n- \n* "},
                       {"questions": [_q("Wrong shape for this tool?")]}):
            with self.subTest(input=input_):
                s = self.both([_a(_txt("Plan below.")), _a(_ask("p1", input_, "ExitPlanMode"))])
                self.assertEqual(s.pending_tool, "ExitPlanMode")
                self.assertIsNone(s.pending_question)

    def test_ask_user_ignores_a_plan_field(self) -> None:
        self.assertIsNone(self.ask({"plan": "# A plan"}).pending_question)

    def test_sidechain_question_tool_is_ignored(self) -> None:
        s = self.both([_a(_txt("Done.")),
                       _a(_ask("q1", {"questions": [_q("Sidechain asks?")]}), isSidechain=True),
                       _a(_ask("p1", {"plan": "# Sidechain plan"}, "ExitPlanMode"), isSidechain=True)])
        self.assertIsNone(s.pending_tool)
        self.assertIsNone(s.pending_question)
        # A sidechain tool use after a real pending question does not replace it.
        s = self.both([_a(_txt("Done.")), _a(_ask("q1", {"questions": [_q("Main chain asks?")]})),
                       _a(_tool("Bash", "b1"), isSidechain=True)])
        self.assertEqual(s.pending_question, "Main chain asks?")

    def test_no_final_text(self) -> None:
        s = self.both([_u("Plan it."), _a(_ask("p1", {"plan": "# Plan: one step"}, "ExitPlanMode"))])
        self.assertEqual(s.pending_question, "Plan: one step")

    def test_survives_a_big_nonhuman_tail_and_window_growth(self) -> None:
        b = Builder()
        b.user("Set up the database layer.")
        b.text("I need two decisions from you before I write the migrations.")
        b.put(b.env("assistant", message={"role": "assistant", "content": [
            _ask("toolu_q", {"questions": [_q("Which database should the inventory service use?"), _q("Migrate on deploy?")]})]}))
        b.nonhuman_filler(650 * KIB)
        with tempfile.TemporaryDirectory() as d:
            p = b.write(Path(d) / "big.jsonl")
            s = parse_transcript(p)
            self.assertEqual(s, by_lines(p))
            self.assertEqual(parse_transcript(p, head=False).pending_question, s.pending_question)
        self.assertEqual(s.pending_question, "Which database should the inventory service use? (+1 more)")

    def test_fixtures(self) -> None:
        ask = parse_transcript(FIXTURES / "pos_pending_ask_user.jsonl")
        self.assertEqual(ask.pending_question, "Which database should the inventory service use? (+1 more)")
        plan = parse_transcript(FIXTURES / "pos_pending_exit_plan.jsonl")
        self.assertEqual(plan.pending_question, "1. Create packages/billing with its own package.json.")
        for p in fixture_paths():
            with self.subTest(fixture=p.name):
                s = parse_transcript(p)
                self.assertEqual(s.pending_question, by_lines(p).pending_question)
                if s.pending_tool is None:
                    self.assertIsNone(s.pending_question)

    def test_question_stays_out_of_repr_and_safe_dict(self) -> None:
        s = parse_transcript(FIXTURES / "pos_pending_ask_user.jsonl")
        self.assertNotIn("inventory service", repr(s))
        safe = json.dumps(s.safe_dict())
        self.assertNotIn("inventory service", safe)
        self.assertTrue(s.safe_dict()["has_pending_question"])
        self.assertFalse(parse_transcript(FIXTURES / "neg_ask_user_answered.jsonl").safe_dict()["has_pending_question"])

    def test_pending_question_text_directly(self) -> None:
        f = transcript.pending_question_text
        self.assertEqual(f("AskUserQuestion", {"questions": [_q("A?"), _q("B?")]}), "A? (+1 more)")
        self.assertEqual(f("ExitPlanMode", {"plan": "## Do it"}), "Do it")
        self.assertIsNone(f("Bash", {"question": "A?"}))
        self.assertIsNone(f(None, {"question": "A?"}))
        self.assertIsNone(f("AskUserQuestion", "A?"))


# --------------------------------------------------------------------------- drift counters (contract 16.6)

def drift(s: transcript.SessionSummary) -> Any:
    return (s.lines_read, s.conversation_entries, s.unknown_types)


CONTRACT_METADATA = (
    "last-prompt atis-latch bridge-session mode ai-title api-request api-request-blob api-request-shape "
    "frame-link permission-mode session-owner file-history-snapshot file-history-delta queue-operation "
    "artifact-autoreact-ledger artifact-comment-monitor tools-used cost-state agent-name custom-title summary"
).split()


def _meta(etype: Any, **extra: Any) -> str:
    d: Dict[str, Any] = {"type": etype}
    d.update(extra)
    return json.dumps(d)


class DriftCounterTests(unittest.TestCase):
    """lines_read / conversation_entries / unknown_types (contract 16.6)."""

    def agree(self, p: Path, **kw: Any) -> transcript.SessionSummary:
        """parse_transcript, after checking parse_lines gives the same drift counters."""
        s = parse_transcript(p, **kw)
        self.assertEqual(drift(s), drift(by_lines(p, **kw)), p.name)
        return s

    def test_constants_match_the_contract(self) -> None:
        self.assertEqual(transcript.KNOWN_METADATA_TYPES, frozenset(CONTRACT_METADATA))
        self.assertEqual(transcript.KNOWN_METADATA_PREFIXES, ("artifact-", "agent-", "file-history-", "api-request"))

    def test_counts_by_kind(self) -> None:
        lines = [
            _meta("mode", mode="default"), _meta("permission-mode"), _meta("summary", summary="x"),
            _meta("artifact-new-thing"), _meta("agent-color"), _meta("file-history-checkpoint"), _meta("api-request-v2"),
            _u("Set it up."), _a(_txt("Done.")), _meta("system", subtype="turn_duration"),
            _meta("attachment", attachment={"type": "x"}),
            _a(_txt("Sidechain."), isSidechain=True),                  # conversation, even on a sidechain
            _meta("frobnicate"), _meta("frobnicate"), _meta("pr-link"),  # unknown
            _meta("has spaces and words"), _meta("x" * 65),              # unknown, name not kept
            _meta(None), _meta(["user"]), _meta({"k": 1}), json.dumps({"no": "type"}),  # entries, no type
            "{truncated", "not json at all", "[1, 2]", '"a string"', "null",            # lines, not entries
            "", "   ", "\t",                                                            # blank: not lines
        ]
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "kinds.jsonl", lines)
            s = self.agree(p)
            self.assertEqual(s.lines_read, len(lines) - 3)
            self.assertEqual(s.conversation_entries, 5)
            self.assertEqual(s.unknown_types, {"frobnicate": 2, "pr-link": 1, transcript.UNKNOWN_TYPE_OTHER: 2})
            self.assertEqual(drift(parse_lines(lines, "kinds")), drift(s))  # str lines without newlines
            self.assertEqual(drift(parse_lines("\n".join(lines), "kinds")), drift(s))

    def test_every_known_metadata_type_is_not_unknown(self) -> None:
        lines = [_meta(t) for t in CONTRACT_METADATA] + [_meta(pre + "anything") for pre in transcript.KNOWN_METADATA_PREFIXES]
        s = parse_lines(lines, "m")
        self.assertEqual((s.lines_read, s.conversation_entries, s.unknown_types), (len(lines), 0, {}))
        s = parse_transcript(FIXTURES / "neg_metadata_only.jsonl")
        self.assertEqual(s.conversation_entries, 0)
        self.assertGreater(s.lines_read, 0)

    def test_fixtures_agree_and_have_no_unknown_types(self) -> None:
        for p in fixture_paths():
            with self.subTest(fixture=p.name):
                s = self.agree(p)
                self.agree(p, head=False)
                # neg_metadata_only also holds two types the 16.6 list does not name.
                self.assertEqual(s.unknown_types, {"worktree-state": 1, "relocated": 1}
                                 if p.name == "neg_metadata_only.jsonl" else {})
                nonblank = sum(1 for x in read_lines(p) if x.strip())
                self.assertEqual(s.lines_read, nonblank)  # small file: every line, each once
                if p.name != "neg_metadata_only.jsonl":
                    self.assertGreater(s.conversation_entries, 0)

    def test_overlap_any_window_and_head_size(self) -> None:
        """Head and tail windows overlap (or not) in every way: each line is still counted once."""
        for p in fixture_paths():
            nonblank = sum(1 for x in read_lines(p) if x.strip())
            for head in (0, 1, 5, 400):
                for window in (1, 2, 7, 64, 333, 4096):
                    with self.subTest(fixture=p.name, head=head, window=window):
                        with mock.patch.object(transcript, "HEAD_LINES", head), \
                                mock.patch.object(transcript, "TAIL_START_BYTES", window):
                            s = self.agree(p)
                            self.agree(p, head=False)
                        self.assertLessEqual(s.lines_read, nonblank)

    def test_window_starting_exactly_on_each_line_boundary(self) -> None:
        for name in ("pos_question_then_nonhuman.jsonl", "neg_malformed_lines.jsonl", "pos_pending_exit_plan.jsonl"):
            p = FIXTURES / name
            data = p.read_bytes()
            for i, b in enumerate(data):
                if b != 0x0A:
                    continue
                for back in (0, 1):  # the window starts on a line start, or one byte before it
                    window = len(data) - (i + 1) + back
                    if window <= 0:
                        continue
                    with self.subTest(fixture=name, window=window), \
                            mock.patch.object(transcript, "HEAD_LINES", 3), \
                            mock.patch.object(transcript, "TAIL_START_BYTES", window):
                        self.agree(p)

    def test_big_file_counts_the_head_and_the_tail_window_only(self) -> None:
        b = Builder()
        b.meta("early-unknown")
        b.user("Prepare the release branch.")
        for i in range(1000):  # past the 400-line head and the 256 KiB window
            b.meta("middle-unknown", n=i, pad="z" * 1500)
        # The last turn's start: the window must reach it (contract 16.10), so it sits right here.
        b.user("Push it when it's ready.")
        b.text("Release branch is ready. Want me to push it?")
        for i in range(20):
            b.meta("late-unknown", n=i)
        with tempfile.TemporaryDirectory() as d:
            p = b.write(Path(d) / "big.jsonl")
            self.assertGreater(p.stat().st_size, 2 * transcript.TAIL_START_BYTES)
            s = self.agree(p)
            self.assertEqual(s.unknown_types.get("early-unknown"), 1)
            self.assertEqual(s.unknown_types.get("late-unknown"), 20)
            # The head reads lines 1-400 (398 middle lines); the 256 KiB window ~170 more; the rest
            # of the 1000 sit in the gap and are never decoded.
            middle = s.unknown_types["middle-unknown"]
            self.assertGreater(middle, 398)
            self.assertLess(middle, 700)
            self.assertEqual(s.lines_read, 1 + 1 + middle + 1 + 1 + 20)
            self.assertEqual(s.conversation_entries, 3)
            tail = self.agree(p, head=False)
            self.assertNotIn("early-unknown", tail.unknown_types)
            self.assertEqual(tail.lines_read, 1 + 1 + 20 + middle - 398)
            # A line cut reads the whole prefix through parse_lines; both readers still agree.
            cut = parse_transcript(p, max_lines=450)
            self.assertEqual(drift(cut), drift(parse_lines(read_lines(p)[:450], p.stem, p)))
            self.assertEqual(cut.lines_read, 450)

    def test_expanding_window_counts_each_line_once(self) -> None:
        b = big_question_session(filler=700 * KIB)
        with tempfile.TemporaryDirectory() as d:
            p = b.write(Path(d) / "grow.jsonl")
            starts: List[int] = []
            real = transcript._read_window

            def spy(f: Any, start: int, size: int) -> List[bytes]:
                starts.append(start)
                return real(f, start, size)

            with mock.patch.object(transcript, "_read_window", spy):
                s = parse_transcript(p)
            self.assertGreater(len(starts), 1, "window never expanded")
            self.assertEqual(drift(s), drift(by_lines(p)))
            # Independent count: the head's lines plus the lines that start inside the last window.
            offset, expected = 0, 0
            for i, line in enumerate(read_lines(p)):
                if line.strip() and (i < transcript.HEAD_LINES or offset >= starts[-1]):
                    expected += 1
                offset += len(line)
            self.assertEqual(s.lines_read, expected)

    def test_no_final_text_reads_the_whole_file(self) -> None:
        b = Builder()
        b.user("Hello")
        b.nonhuman_filler(600 * KIB)
        b.meta("brand-new-type")
        with tempfile.TemporaryDirectory() as d:
            p = b.write(Path(d) / "nofinal.jsonl")
            # Sidechain text is not a final text, so the window grows to the file start.
            s = self.agree(p)
            self.assertEqual(s.lines_read, len(b.lines))
            self.assertEqual(s.unknown_types, {"brand-new-type": 1})

    def test_max_tail_bytes_counts_what_was_read(self) -> None:
        b = big_question_session(filler=700 * KIB)
        with tempfile.TemporaryDirectory() as d:
            p = b.write(Path(d) / "hook.jsonl")
            bounded = parse_transcript(p, head=False, max_tail_bytes=transcript.TAIL_START_BYTES)
            full = parse_transcript(p, head=False)
            self.assertGreater(bounded.lines_read, 0)
            self.assertLess(bounded.lines_read, full.lines_read)

    def test_counters_do_not_take_part_in_equality(self) -> None:
        a = transcript.SessionSummary(session_uuid="x", lines_read=3, conversation_entries=1, unknown_types={"a": 1})
        self.assertEqual(a, transcript.SessionSummary(session_uuid="x"))
        self.assertNotEqual(transcript.SessionSummary(session_uuid="x", pending_question="Q?"),
                            transcript.SessionSummary(session_uuid="x"))

    def test_safe_dict_carries_the_counts(self) -> None:
        safe = parse_transcript(FIXTURES / "pos_trailing_question.jsonl").safe_dict()
        self.assertGreater(safe["lines_read"], 0)
        self.assertGreater(safe["conversation_entries"], 0)
        self.assertEqual(safe["unknown_types"], {})

    def test_random_layouts_agree(self) -> None:
        """Seeded: random mixes of line kinds and sizes; head/window sizes that cut anywhere."""
        import random
        rng = random.Random(20261108)
        kinds = [
            lambda: _a(_txt("Reply %d." % rng.randint(0, 99))),
            lambda: _a(_txt("No timestamp.")).replace('"timestamp": "2026-10-02T10:00:00.000Z", ', ""),
            lambda: _u("prompt " + "p" * rng.randint(0, 300)),
            lambda: _a(_txt("side"), isSidechain=True),
            lambda: _meta("file-history-snapshot", pad="f" * rng.randint(0, 500)),
            lambda: _meta(rng.choice(["mystery", "new-kind", "x y"])),
            lambda: rng.choice(["{bad", "", "  ", "[]", "null"]),
            lambda: _meta("system", timestamp="2026-10-02T10:00:09.000Z"),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rand.jsonl"
            for trial in range(300):
                lines = [rng.choice(kinds)() for _ in range(rng.randint(0, 40))]
                text = "".join(x + "\n" for x in lines)
                if lines and rng.random() < 0.3:
                    text = text[:-1]  # no newline at the end
                p.write_text(text, encoding="utf-8")
                head = rng.choice([0, 1, 3, 10, 400])
                window = rng.choice([1, 17, 100, 600, 3000, 256 * KIB])
                with self.subTest(trial=trial), mock.patch.object(transcript, "HEAD_LINES", head), \
                        mock.patch.object(transcript, "TAIL_START_BYTES", window):
                    self.agree(p)
                    self.agree(p, head=False)
                    s = parse_transcript(p)
                    self.assertEqual(s, by_lines(p))


if __name__ == "__main__":
    unittest.main()
