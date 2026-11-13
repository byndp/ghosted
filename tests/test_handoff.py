"""Hand-off endings (step 10 part B2, contract 16.10). Synthetic text only.

B1 found two of the three real misses ended on a summary whose hand-off ("waiting on you", "you
decide") sat in the body, far from the 200-char tail the question test reads. A hand-off is soft,
read over the whole final message, and skipped when the last turn registered it with `ghosted add`.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core import classify as C
from core import config, quote, transcript
from core.transcript import SessionSummary

FILLER = ("I rebuilt the three screens, moved the tokens into the shared file and reran the "
          "snapshot suite; everything is green and the build is on TestFlight. ") * 6
SUMMARY_END = "\n\nFiles: specs/demo.md, app/Views/Home.swift. Tests: 212 passed."


def classify(text: str, handoff=None, **summary) -> C.Ending:
    s = SessionSummary(session_uuid="x", final_text=text, **summary)
    if handoff is None:
        return C.classify(s, ())
    return C.classify(s, (), handoff_patterns=handoff)


class HandoffCueTests(unittest.TestCase):
    def test_hand_off_in_the_body_of_a_summary(self) -> None:
        text = "Done.\n\n**Waiting on you:** keep the coached run or cut it.\n\n" + FILLER + SUMMARY_END
        self.assertGreater(len(text) - text.find("Waiting on you"), 600)
        self.assertEqual(classify(text), (C.OPEN_QUESTION, C.HANDOFF))
        self.assertEqual(C.tier(C.OPEN_QUESTION, C.HANDOFF), C.SOFT)

    def test_label_line_forms(self) -> None:
        for line in ("Waiting on you: pick a name.", "**Waiting on you** \u2014 the hero image.",
                     "- Still waiting on you: the copy.", "## Waiting on you", "Waiting on you - the deck"):
            with self.subTest(line=line):
                self.assertEqual(classify("Done.\n\n" + line + "\n\n" + FILLER + SUMMARY_END),
                                 (C.OPEN_QUESTION, C.HANDOFF))

    def test_looser_phrases_are_not_shipped(self) -> None:
        # 22 of 107 real plain summaries held one of these (contract 16.10): too noisy by default.
        for phrase in ("It's up to you which deck ships.", "You decide between the two layouts.",
                       "When you pick a name I'll wire it in.", "This needs your sign-off before Friday.",
                       "Your call on the hero image.", "The rest is waiting on you.",
                       "\n\n**Next:** you pick A or B in the spec."):
            with self.subTest(phrase=phrase):
                self.assertEqual(classify("Done. " + phrase + " " + FILLER + SUMMARY_END), (C.CLEAN, C.NO_QUESTION))

    def test_plain_summary_stays_clean(self) -> None:
        self.assertEqual(classify(FILLER + SUMMARY_END), (C.CLEAN, C.NO_QUESTION))
        # "you" alone, or a past choice, is not a hand-off.
        self.assertEqual(classify(FILLER + "As you asked, I kept the old header. You chose blue last week."),
                         (C.CLEAN, C.NO_QUESTION))

    def test_negated_hand_offs_are_not_hand_offs(self) -> None:
        named = C.handoff_patterns_for(C.DEFAULT_HANDOFF_PATTERNS, ["Alex"])
        for phrase in ("The tree is clean and nothing is waiting on Alex.", "Nobody is waiting on Alex.",
                       "Nothing waits on Alex now.", "It isn't Alex's call any more."):
            with self.subTest(phrase=phrase):
                self.assertEqual(classify("Done. " + phrase + " " + FILLER + SUMMARY_END, handoff=named),
                                 (C.CLEAN, C.NO_QUESTION))
        # A negation in an earlier clause does not cancel a later hand-off.
        self.assertEqual(classify("Done. Nothing broke, and the copy is waiting on Alex. " + FILLER,
                                  handoff=named)[1], C.HANDOFF)

    def test_code_is_masked(self) -> None:
        text = FILLER + "\n```\nWaiting on you: x\n```\nAll set." + SUMMARY_END
        self.assertEqual(classify(text), (C.CLEAN, C.NO_QUESTION))

    def test_question_cues_win(self) -> None:
        self.assertEqual(classify("Waiting on you: the hero.\nKeep A, or switch to B?")[1], C.TRAILING_Q)

    def test_turn_continued_stays_clean(self) -> None:
        self.assertEqual(classify(FILLER + "\nWaiting on you: the copy.", human_after_final=True),
                         (C.CLEAN, C.HUMAN_AFTER_FINAL))

    def test_registered_with_le_add_is_handled(self) -> None:
        text = FILLER + "\nWaiting on you: the demo ruling." + SUMMARY_END
        self.assertEqual(classify(text, le_add_last_turn=True), (C.CLEAN, C.NO_QUESTION))

    def test_patterns_off_and_names_opt_in(self) -> None:
        text = FILLER + "Alex has not ruled on the demo yet." + SUMMARY_END
        self.assertEqual(classify(text), (C.CLEAN, C.NO_QUESTION))
        named = C.handoff_patterns_for(C.DEFAULT_HANDOFF_PATTERNS, ["Alex"])
        self.assertEqual(classify(text, handoff=named), (C.OPEN_QUESTION, C.HANDOFF))
        self.assertEqual(classify(FILLER + "Next = Alex rules on the demo." + SUMMARY_END, handoff=named)[1],
                         C.HANDOFF)
        self.assertEqual(classify("Alex's call on the hero. " + FILLER + SUMMARY_END, handoff=named)[1], C.HANDOFF)
        self.assertEqual(classify(FILLER + "Nickel plating is done." + SUMMARY_END, handoff=named),
                         (C.CLEAN, C.NO_QUESTION))
        self.assertEqual(classify(FILLER + "\nWaiting on you: it." + SUMMARY_END, handoff=()),
                         (C.CLEAN, C.NO_QUESTION))

    def test_quote_is_the_hand_off_sentence(self) -> None:
        text = "Done. " + FILLER + "\nWaiting on you: the demo choice, see SPEC 12 Q4.\n" + FILLER
        s = SessionSummary(session_uuid="x", final_text=text)
        self.assertEqual(quote.extract(s, C.OPEN_QUESTION, C.HANDOFF), "Waiting on you: the demo choice, see SPEC 12 Q4.")
        named = C.handoff_patterns_for(C.DEFAULT_HANDOFF_PATTERNS, ["Alex"])
        text = "Done. " + FILLER + "The demo is waiting on Alex since Monday. " + FILLER
        s = SessionSummary(session_uuid="x", final_text=text)
        self.assertEqual(quote.extract(s, C.OPEN_QUESTION, C.HANDOFF, handoff_patterns=named),
                         "The demo is waiting on Alex since Monday.")

    def test_long_text_is_fast(self) -> None:
        import time

        text = ("waiting on Alex " * 6000) + "\nWaiting on you"
        t0 = time.perf_counter()
        C.handoff_cue(text, C.handoff_patterns_for(C.DEFAULT_HANDOFF_PATTERNS, ["Alex"]))
        self.assertLess(time.perf_counter() - t0, 0.5)


class TranscriptLeAddTests(unittest.TestCase):
    def lines(self, *entries):
        head = {"isSidechain": False, "cwd": "/tmp/x", "sessionId": "s", "entrypoint": "cli"}
        out = []
        for i, e in enumerate(entries):
            out.append(json.dumps(dict(head, uuid="u%d" % i, timestamp="2026-11-08T10:%02d:00Z" % i, **e)))
        return out

    @staticmethod
    def human(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    @staticmethod
    def bash(cmd, tid):
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}}

    @staticmethod
    def result(tid):
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "GS-0012 added"}]}}

    @staticmethod
    def say(text):
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    def parse_both(self, lines):
        a = transcript.parse_lines(lines, "s")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "11111111-1111-1111-1111-111111111111.jsonl"
            p.write_text("\n".join(lines) + "\n")
            b = transcript.parse_transcript(p)
        return a, b

    def test_le_add_in_the_last_turn(self) -> None:
        lines = self.lines(self.human("finish it"), self.bash('ghosted add "Demo ruling" --public "Demo ruling"', "t1"),
                           self.result("t1"), self.say("Logged it.\n\nWaiting on you: the demo."))
        a, b = self.parse_both(lines)
        self.assertTrue(a.le_add_last_turn)
        self.assertEqual(a.le_add_last_turn, b.le_add_last_turn)
        self.assertEqual(C.classify(a, ()), (C.CLEAN, C.NO_QUESTION))

    def test_le_add_in_an_earlier_turn_does_not_count(self) -> None:
        lines = self.lines(self.human("one"), self.bash("ghosted add x --public y", "t1"), self.result("t1"),
                           self.say("ok"), self.human("now the demo"),
                           self.say("Done.\n\nWaiting on you: the demo."))
        a, b = self.parse_both(lines)
        self.assertFalse(a.le_add_last_turn)
        self.assertFalse(b.le_add_last_turn)
        self.assertEqual(C.classify(a, ()), (C.OPEN_QUESTION, C.HANDOFF))

    def test_other_commands_do_not_count(self) -> None:
        lines = self.lines(self.human("go"), self.bash("grep -r 'ghosted added' docs", "t1"), self.result("t1"),
                           self.say("The demo is waiting on you."))
        self.assertFalse(transcript.parse_lines(lines, "s").le_add_last_turn)

    def test_le_add_only_in_command_position(self) -> None:
        # Mentioning "ghosted add" is not running it.
        for cmd in ("echo ghosted add demo", "grep -n 'ghosted add' docs/contract.md", "cat <<'EOF'\nrun ghosted add later\nEOF",
                    "git commit -m 'document ghosted add'"):
            with self.subTest(cmd=cmd):
                self.assertFalse(transcript.ran_le_add(cmd))
        for cmd in ('ghosted add "Demo" --public "Demo"', 'cd ~/x && ghosted add a --public b', "./bin/ghosted add a --public b",
                    "GHOSTED_HOME=/tmp/h ghosted add a --public b", "true; /opt/ghosted/bin/ghosted add a --public b",
                    "ghosted add 'unbalanced --public b"):
            with self.subTest(cmd=cmd):
                self.assertTrue(transcript.ran_le_add(cmd))

    def test_le_add_early_in_a_long_last_turn(self) -> None:
        # The file reader must reach back to the turn's start.
        big = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "x" * 300000}]}}
        lines = self.lines(self.human("finish it"), self.bash("ghosted add demo --public demo", "t1"), self.result("t1"),
                           self.bash("cat big.log", "t2"), big, self.say("Done.\n\nWaiting on you: pick a demo."))
        a, b = self.parse_both(lines)
        self.assertTrue(a.le_add_last_turn)
        self.assertTrue(b.le_add_last_turn)
        self.assertEqual(C.classify(a, ()), C.classify(b, ()))
        self.assertEqual(C.classify(b, ()), (C.CLEAN, C.NO_QUESTION))

    def test_fenced_code_cut_by_the_window_stays_masked(self) -> None:
        # Mask the whole message before cropping to HANDOFF_CHARS.
        text = "Here is the log:\n```\n" + ("line of output\n" * 700) + "Waiting on you: x\n```\nAll done."
        self.assertGreater(len(text), C.HANDOFF_CHARS)
        self.assertEqual(classify(text), (C.CLEAN, C.NO_QUESTION))


class ConfigTests(unittest.TestCase):
    def test_keys(self) -> None:
        home = Path(tempfile.mkdtemp())
        cfg = config.build_config({}, home, environ={})
        self.assertEqual(cfg.handoff_names, [])
        self.assertEqual(cfg.handoff_patterns, list(C.DEFAULT_HANDOFF_PATTERNS))
        cfg = config.build_config({"detect": {"handoff_names": ["Alex"], "handoff_patterns": []}}, home, environ={})
        self.assertEqual(cfg.handoff_names, ["Alex"])
        self.assertTrue(cfg.handoff_patterns)  # the name patterns still apply
        for bad in ({"handoff_names": ["Ni(ck"]}, {"handoff_names": [""]}, {"handoff_patterns": ["("]}):
            with self.assertRaises(config.ConfigError):
                config.build_config({"detect": bad}, home, environ={})

    def test_init_writes_names_key(self) -> None:
        from core import cli

        self.assertIn("handoff_names = []", cli.render_config())


if __name__ == "__main__":
    unittest.main()
