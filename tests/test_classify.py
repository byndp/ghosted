"""Tests for core.classify: the fixture table, rule order, the question test, the limit wording."""
from __future__ import annotations

import random
import re
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple

from core import classify as C
from core.classify import Ending, classify, is_limit_text, masked_text, matches_ignore_tail, question_cue
from core.transcript import SessionSummary, parse_lines, parse_transcript

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
DEFAULTS = C.DEFAULT_IGNORE_TAIL_PATTERNS

# The single machine-readable copy of tests/fixtures/synthetic/README.md (a test keeps them equal).
EXPECTED: Dict[str, Tuple[str, str]] = {
    "pos_limit_meta.jsonl": ("usage_limit", "limit_meta"),
    "pos_limit_text_only.jsonl": ("usage_limit", "limit_text"),
    "pos_limit_continue_limit.jsonl": ("usage_limit", "limit_meta"),
    "pos_limit_then_continue_unanswered.jsonl": ("usage_limit", "limit_meta"),
    "pos_trailing_question.jsonl": ("open_question", "save_offer"),
    "pos_want_me_to.jsonl": ("open_question", "save_offer"),
    "pos_say_the_word.jsonl": ("open_question", "say_the_word"),
    "pos_your_call.jsonl": ("open_question", "your_call"),
    "pos_question_then_nonhuman.jsonl": ("open_question", "save_offer"),
    "pos_question_url_and_code.jsonl": ("open_question", "offer_q"),
    "pos_pending_ask_user.jsonl": ("open_question", "ask_user"),
    "pos_pending_exit_plan.jsonl": ("open_question", "plan_approval"),
    "neg_ask_user_answered.jsonl": ("clean", "no_question"),
    "neg_safe_to_clear.jsonl": ("clean", "no_question"),
    "neg_qa_session.jsonl": ("clean", "no_question"),
    "neg_answered_then_committed.jsonl": ("clean", "no_question"),
    "neg_question_then_queued_human.jsonl": ("clean", "human_after_final"),
    "neg_question_then_pasted_content.jsonl": ("clean", "human_after_final"),
    "neg_qmark_only_in_url.jsonl": ("clean", "no_question"),
    "neg_qmark_only_in_code.jsonl": ("clean", "no_question"),
    "neg_question_then_tool_use.jsonl": ("clean", "tool_use_after_final"),
    "neg_anything_else.jsonl": ("clean", "ignored_tail"),
    "auto_sdk_cli_question.jsonl": ("open_question", "offer_q"),
    "neg_api_server_error.jsonl": ("api_error", "server_error"),
    "neg_metadata_only.jsonl": ("empty", "no_final_text"),
    "neg_sidechain_question.jsonl": ("clean", "no_question"),
    "neg_question_beyond_200.jsonl": ("clean", "no_question"),
    "neg_interrupted_by_user.jsonl": ("clean", "human_after_final"),
    "neg_malformed_lines.jsonl": ("clean", "no_question"),
    "neg_limit_words_in_long_prose.jsonl": ("clean", "no_question"),
}


def summary(text: Any = "Done.", **kw: Any) -> SessionSummary:
    return SessionSummary(session_uuid="t", final_text=text, **kw)


class FixtureTableTests(unittest.TestCase):
    def test_every_fixture_has_an_expectation(self) -> None:
        on_disk = {p.name for p in FIXTURES.glob("*.jsonl")}
        self.assertEqual(on_disk, set(EXPECTED))

    def test_fixture_verdicts(self) -> None:
        for name, expected in sorted(EXPECTED.items()):
            with self.subTest(fixture=name):
                ending = classify(parse_transcript(FIXTURES / name), DEFAULTS)
                self.assertEqual(tuple(ending), expected)

    def test_parse_lines_gives_the_same_verdicts(self) -> None:
        for name, expected in sorted(EXPECTED.items()):
            with self.subTest(fixture=name):
                p = FIXTURES / name
                self.assertEqual(tuple(classify(parse_lines(p.read_bytes(), p.stem, p), DEFAULTS)), expected)

    def test_readme_matches_the_table(self) -> None:
        readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
        rows = {}
        for m in re.finditer(r"^\|\s*([\w.-]+\.jsonl)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|", readme, re.M):
            rows[m.group(1)] = (m.group(2), m.group(3))
        self.assertEqual(rows, EXPECTED)

    def test_both_sides_are_covered(self) -> None:
        flagged = [n for n, (v, _) in EXPECTED.items() if v in C.FLAGGED_VERDICTS]
        self.assertGreaterEqual(len(flagged), 9)
        self.assertGreaterEqual(len(EXPECTED) - len(flagged), 15)

    def test_ignore_patterns_are_what_clears_anything_else(self) -> None:
        s = parse_transcript(FIXTURES / "neg_anything_else.jsonl")
        self.assertEqual(classify(s), ("open_question", "offer_q"))
        self.assertEqual(classify(s, DEFAULTS), ("clean", "ignored_tail"))

    def test_automation_fixture_is_marked_by_metadata(self) -> None:
        s = parse_transcript(FIXTURES / "auto_sdk_cli_question.jsonl")
        self.assertIn("sdk-cli", s.entrypoints)  # the detector turns this into verdict "automated"
        self.assertEqual(classify(s, DEFAULTS).verdict, "open_question")


class RuleOrderTests(unittest.TestCase):
    def test_empty(self) -> None:
        for text in (None, "", "   \n", 42):
            self.assertEqual(classify(summary(text)), ("empty", "no_final_text"))
        self.assertEqual(classify(object()), ("empty", "no_final_text"))

    def test_limit_meta(self) -> None:
        self.assertEqual(classify(summary("x", final_is_api_error=True, final_error="rate_limit")), ("usage_limit", "limit_meta"))
        self.assertEqual(classify(summary("x", final_error="rate_limit")), ("usage_limit", "limit_meta"))
        self.assertEqual(classify(summary("x", final_is_api_error=True, final_error="server_error", final_has_quota=True)), ("usage_limit", "limit_meta"))

    def test_quota_on_a_normal_entry_is_not_a_limit(self) -> None:
        self.assertEqual(classify(summary("Done.", final_has_quota=True)), ("clean", "no_question"))

    def test_limit_beats_a_following_human_turn(self) -> None:
        s = summary("You've hit your weekly limit \u00b7 resets 5am", final_is_api_error=True, final_error="rate_limit", human_after_final=True)
        self.assertEqual(classify(s), ("usage_limit", "limit_meta"))
        self.assertEqual(classify(summary("Claude AI usage limit reached|1759363200", human_after_final=True)), ("usage_limit", "limit_text"))

    def test_limit_text_beats_other_api_errors(self) -> None:
        s = summary("You\u2019ve reached your Opus limit", final_is_api_error=True, final_error="invalid_request")
        self.assertEqual(classify(s), ("usage_limit", "limit_text"))

    def test_api_error(self) -> None:
        self.assertEqual(classify(summary("API Error: 529", final_is_api_error=True, final_error="server_error")), ("api_error", "server_error"))
        self.assertEqual(classify(summary("API Error", final_is_api_error=True, final_error="oauth_org_not_allowed")), ("api_error", "oauth_org_not_allowed"))
        self.assertEqual(classify(summary("API Error", final_is_api_error=True)), ("api_error", "api_error"))
        self.assertEqual(classify(summary("API Error", final_is_api_error=True, final_error="Weird Code!")), ("api_error", "api_error"))

    def test_api_error_beats_a_question(self) -> None:
        self.assertEqual(classify(summary("Should I retry?", final_is_api_error=True, final_error="server_error")).verdict, "api_error")

    def test_pending_question_tool(self) -> None:
        self.assertEqual(classify(summary("I need two decisions.", pending_tool="AskUserQuestion", tool_use_after_final=True)),
                         ("open_question", "ask_user"))
        self.assertEqual(classify(summary("Here is the plan.", pending_tool="ExitPlanMode", tool_use_after_final=True)),
                         ("open_question", "plan_approval"))

    def test_pending_question_tool_rule_order(self) -> None:
        # Rule 4 comes before 4b (turn continued) and before the ignore patterns of rule 5 ...
        self.assertEqual(classify(summary("Anything else?", pending_tool="AskUserQuestion", human_after_final=True), DEFAULTS),
                         ("open_question", "ask_user"))
        # ... and after rules 1 to 3.
        self.assertEqual(classify(summary(None, pending_tool="AskUserQuestion")), ("empty", "no_final_text"))
        self.assertEqual(classify(summary("x", final_error="rate_limit", pending_tool="AskUserQuestion")), ("usage_limit", "limit_meta"))
        self.assertEqual(classify(summary("API Error", final_is_api_error=True, final_error="server_error", pending_tool="ExitPlanMode")),
                         ("api_error", "server_error"))

    def test_other_pending_values_are_ignored(self) -> None:
        self.assertEqual(classify(summary("Done.", pending_tool="Bash", tool_use_after_final=True)), ("clean", "tool_use_after_final"))
        self.assertEqual(classify(summary("Done.", pending_tool="askuserquestion")), ("clean", "no_question"))
        self.assertEqual(classify(summary("Done.", pending_tool=["AskUserQuestion"])), ("clean", "no_question"))  # unhashable: no crash
        self.assertEqual(classify(summary("Done.", pending_tool=None)), ("clean", "no_question"))

    def test_turn_continued(self) -> None:
        self.assertEqual(classify(summary("Want me to push?", human_after_final=True)), ("clean", "human_after_final"))
        self.assertEqual(classify(summary("Want me to push?", tool_use_after_final=True)), ("clean", "tool_use_after_final"))
        self.assertEqual(classify(summary("Push?", human_after_final=True, tool_use_after_final=True)), ("clean", "human_after_final"))

    def test_open_question_and_clean(self) -> None:
        self.assertEqual(classify(summary("Want me to commit these 18 files?")), ("open_question", "save_offer"))
        self.assertEqual(classify(summary("Want me to rename these 18 files?")), ("open_question", "offer_q"))
        self.assertEqual(classify(summary("Safe to clear.")), ("clean", "no_question"))

    def test_ignore_patterns(self) -> None:
        self.assertEqual(classify(summary("Fixed. Does that help?"), DEFAULTS), ("clean", "ignored_tail"))
        self.assertEqual(classify(summary("Fixed. Does this make sense?**"), DEFAULTS), ("clean", "ignored_tail"))
        self.assertEqual(classify(summary("Done. Anything else?  \n"), DEFAULTS), ("clean", "ignored_tail"))
        self.assertEqual(classify(summary("Anything else to fix? Then I will push."), DEFAULTS), ("open_question", "offer_q"))
        self.assertEqual(classify(summary("Done. Want me to push it?"), DEFAULTS), ("open_question", "save_offer"))

    def test_ignore_pattern_forms(self) -> None:
        s = summary("Merged. Ship it?")
        self.assertEqual(classify(s, [r"Ship it\?$"]), ("clean", "ignored_tail"))
        self.assertEqual(classify(s, [r"ship it\?$"]), ("open_question", "save_offer"))  # patterns are case-sensitive unless (?i)
        self.assertEqual(classify(s, r"(?i)SHIP IT\?$"), ("clean", "ignored_tail"))  # a lone string
        self.assertEqual(classify(s, [re.compile(r"Ship it\?")]), ("clean", "ignored_tail"))
        self.assertEqual(classify(s, ["(unclosed", r"\d{"]), ("open_question", "save_offer"))  # bad regex ignored
        self.assertEqual(classify(s, None), ("open_question", "save_offer"))
        self.assertEqual(classify(s, [None, 5]), ("open_question", "save_offer"))

    def test_ignore_only_applies_after_a_cue(self) -> None:
        self.assertEqual(classify(summary("All good."), [r"good\.$"]), ("clean", "no_question"))

    def test_odd_ignore_pattern_values_never_raise(self) -> None:
        s = summary("Merged. Ship it?")
        self.assertEqual(classify(s, 5), ("open_question", "save_offer"))  # not iterable: no patterns
        self.assertEqual(classify(s, [re.compile(rb"Ship")]), ("open_question", "save_offer"))  # bytes: ignored
        self.assertEqual(classify(s, (p for p in [r"Ship it\?$"])), ("clean", "ignored_tail"))  # any iterable

    def test_config_default_is_the_same_list(self) -> None:
        from core import config
        self.assertEqual(list(config.DEFAULT_IGNORE_TAIL_PATTERNS), list(C.DEFAULT_IGNORE_TAIL_PATTERNS))

    def test_ending_tuple(self) -> None:
        e = classify(summary("Push?"))
        verdict, reason = e
        self.assertEqual((verdict, reason), ("open_question", "save_offer"))
        self.assertTrue(e.flagged)
        self.assertFalse(Ending("clean", "no_question").flagged)
        self.assertTrue(Ending("usage_limit", "limit_text").flagged)


class QuestionCueTests(unittest.TestCase):
    def test_phrases(self) -> None:
        self.assertEqual(question_cue("Let me know if you want me to open the PR."), "save_offer")
        self.assertEqual(question_cue("Let me know if you want me to tidy the README."), "want_me_to")
        self.assertEqual(question_cue("If you need it in prod too, say the word."), "say_the_word")
        self.assertEqual(question_cue("Both options work. Your call."), "your_call")
        self.assertEqual(question_cue("WANT ME TO push."), "save_offer")
        self.assertEqual(question_cue("WANT ME TO rerun it."), "want_me_to")
        self.assertEqual(question_cue("Say The Word."), "say_the_word")
        self.assertEqual(question_cue("want  me\nto keep going, tell me."), "want_me_to")

    def test_word_boundaries(self) -> None:
        self.assertIsNone(question_cue("I registered your callback."))
        self.assertIsNone(question_cue("They want media tools."))
        self.assertIsNone(question_cue("Essay the wordsmith wrote."))

    def test_question_mark_beats_phrases(self) -> None:
        self.assertEqual(question_cue("Do you want me to push?"), "save_offer")
        self.assertEqual(question_cue("Do you want me to rerun it?"), "offer_q")
        self.assertEqual(question_cue("Say the word and I'll push. Want me to rename it?"), "offer_q")

    def test_window_is_the_last_200_characters(self) -> None:
        self.assertEqual(question_cue("Q?" + "x" * 199), "trailing_q")   # '?' is the 200th char from the end
        self.assertIsNone(question_cue("Q?" + "x" * 200))                # 201st
        self.assertIsNone(question_cue("want me to " + "x" * 195))      # phrase starts before the window
        self.assertEqual(question_cue("Q?" + "x" * 198 + "   \n\n**__"), "trailing_q")  # trailing junk stripped first

    def test_trailing_whitespace_and_emphasis(self) -> None:
        self.assertEqual(question_cue("**Want me to push?**"), "save_offer")
        self.assertEqual(question_cue("**Want me to rerun it?**"), "offer_q")
        self.assertEqual(question_cue("_Ship it?_ \n"), "save_offer")
        self.assertEqual(question_cue("Ship it?" + " " * 500), "save_offer")
        self.assertEqual(question_cue("Want the log?" + " " * 500), "offer_q")
        self.assertIsNone(question_cue("***"))
        self.assertIsNone(question_cue(""))
        self.assertIsNone(question_cue(None))

    def test_question_mark_in_urls(self) -> None:
        self.assertIsNone(question_cue("See https://x.dev/a?b=1 for details."))
        self.assertIsNone(question_cue("(see https://x.dev/a?b=1)"))
        self.assertIsNone(question_cue("Read [the docs](https://x.dev/?q=1)."))
        self.assertIsNone(question_cue("Open <https://x.dev/?q=1> now."))
        self.assertIsNone(question_cue("Try www.example.com/?q=1 later."))
        self.assertIsNone(question_cue("Runs on localhost:3000/?debug=1 now."))
        self.assertIsNone(question_cue("Indexed example.com/search?q=1 fine."))
        self.assertIsNone(question_cue("file:///tmp/a?b"))

    def test_question_mark_after_a_url_is_a_question(self) -> None:
        self.assertEqual(question_cue("Is it live at https://x.dev/login?"), "trailing_q")
        self.assertEqual(question_cue("Did https://x.dev/search?q=1 work?"), "trailing_q")
        self.assertEqual(question_cue("Is it https://x.dev/search?q=a?"), "trailing_q")
        self.assertEqual(question_cue("(maybe https://x.dev/a?)"), "trailing_q")

    def test_question_mark_in_code(self) -> None:
        self.assertIsNone(question_cue("Added `[ $? -ne 0 ]` to the script."))
        self.assertIsNone(question_cue("Use ``a ` b?`` here."))
        self.assertIsNone(question_cue("Patch:\n```bash\nfoo || echo $?\n```"))
        self.assertIsNone(question_cue("Patch:\n~~~\nwhy?\n~~~"))
        self.assertIsNone(question_cue("Patch:\n````\n```\nnested?\n```\n````"))
        self.assertIsNone(question_cue("Unclosed fence:\n```\nstill code?"))
        self.assertIsNone(question_cue("Phrase in code: `want me to`."))

    def test_one_line_triple_backtick_span_is_not_a_fence(self) -> None:
        # CommonMark: a backtick fence's info string has no backticks, so this is an inline span. It
        # used to open an unclosed fence that masked everything after it, hiding the real question.
        self.assertEqual(question_cue("Ran it:\n```npm test``` passed.\nWant the full log?"), "offer_q")
        self.assertIsNone(question_cue("Ran it:\n```echo $?``` passed."))  # still code
        self.assertIsNone(question_cue("Patch:\n```js\nwhy?\n```"))         # a real fence with an info string

    def test_indented_fences_inside_lists(self) -> None:
        block = "1. Run:\n\n     ```bash\n     test $? -eq 0\n\n     echo ok\n     ```\n"
        self.assertIsNone(question_cue(block + "Done."))       # 5-space fence with a blank line inside
        self.assertEqual(question_cue(block + "Ship it?"), "save_offer")
        self.assertEqual(question_cue(block + "Want the log?"), "offer_q")
        self.assertEqual(question_cue("Patch:\r\n```\r\necho $?\r\n```\r\nShip it?"), "save_offer")  # CRLF closer

    def test_unmatched_backticks_are_literal(self) -> None:
        self.assertEqual(question_cue("Press the ` key. Ready?"), "trailing_q")
        # backticks never pair across a blank line, so the '?' between them is not hidden as code
        self.assertEqual(question_cue("One ` here.\n\nIs it ready? Another ` there."), "trailing_q")
        self.assertIsNone(question_cue("One ` here. Is it ready? Another ` there."))  # same paragraph: a code span

    def test_code_span_opened_before_the_window(self) -> None:
        text = "Ran `" + "a" * 300 + " b?` and it passed."
        self.assertIsNone(question_cue(text))
        self.assertEqual(question_cue(text + " Want the log?"), "offer_q")

    def test_large_inputs_stay_correct(self) -> None:
        self.assertEqual(question_cue(("a ` b " * 5000) + "Ready?"), "trailing_q")  # thousands of lone backticks
        self.assertIsNone(question_cue(("x `y` " * 5000) + "`z?`"))                 # thousands of code spans
        self.assertIsNone(question_cue("```\n" + "code?\n" * 5000))                  # long unclosed fence

    def test_other_question_marks(self) -> None:
        self.assertEqual(question_cue("\u6e96\u5099\u3067\u304d\u307e\u3057\u305f\uff1f"), "trailing_q")
        self.assertEqual(question_cue("\u00bfLo subo a main?"), "trailing_q")

    def test_masked_text_keeps_length(self) -> None:
        for text in ("a `b?` c https://x.dev/?q=1 d", "```\nx?\n```\ntail?", "", "plain"):
            self.assertEqual(len(masked_text(text)), len(text))
        self.assertEqual(masked_text("x `y` z").replace("\x00", "_"), "x ___ z")

    def test_matches_ignore_tail(self) -> None:
        self.assertTrue(matches_ignore_tail("Done. Anything else?", DEFAULTS))
        self.assertFalse(matches_ignore_tail("Done. Anything else to do first?", DEFAULTS))
        self.assertFalse(matches_ignore_tail(None, DEFAULTS))


class LimitTextTests(unittest.TestCase):
    def test_wordings(self) -> None:
        for text in (
            "You've hit your weekly limit \u00b7 resets 5am (America/Chicago)",
            "You\u2019ve hit your session limit \u00b7 resets 11:30pm",
            "You've hit your daily limit",
            "You've hit your 5-hour limit",
            "You have hit your usage limit.",
            "Claude AI usage limit reached|1759363200",
            "Claude usage limit reached. Your limit will reset at 5pm (America/New_York).",
            "5-hour limit reached \u2219 resets 3pm",
            "Opus limit reached - resets Mon",
            "You've reached your Opus limit",
            "You\u2019ve reached your weekly usage limit for this plan",
            "YOU'VE HIT YOUR WEEKLY LIMIT",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_limit_text(text))

    def test_not_limits(self) -> None:
        for text in (
            "Rate limit handling is done.",
            "The limit is 10 requests per second.",
            "limit reached; nothing resets",
            "You hit the weekly report deadline.",
            None,
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_limit_text(text))

    def test_long_prose_is_not_the_banner(self) -> None:
        prose = "The classifier treats \"usage limit reached\" as the banner. " + "More words. " * 20
        self.assertGreater(len(prose), C.LIMIT_TEXT_MAX_CHARS)
        self.assertFalse(is_limit_text(prose))
        self.assertEqual(classify(summary(prose)), ("clean", "no_question"))
        self.assertTrue(is_limit_text("  Claude AI usage limit reached|1759363200  \n"))

    def test_limit_wording_inside_code_is_not_the_banner(self) -> None:
        # A short reply in this repo's own sessions that names the pattern is not a usage limit.
        self.assertFalse(is_limit_text("Added the `usage limit reached` pattern and a test."))
        self.assertFalse(is_limit_text("Matched ```You've hit your weekly limit``` in the fixture."))
        self.assertEqual(classify(summary("Added the `usage limit reached` pattern.")), ("clean", "no_question"))
        self.assertTrue(is_limit_text("You've hit your weekly limit \u00b7 resets 5am \u00b7 https://claude.ai/upgrade"))


def _normalize_tail_reference(text: str) -> str:
    """The original fixpoint definition, kept to prove the linear rewrite is the same function."""
    s = text
    while True:
        t = s.rstrip().rstrip("*_")
        if t == s:
            return s
        s = t


def _trim_url_reference(url: str) -> str:
    while url:
        c = url[-1]
        if c in C._URL_TRAILING:
            url = url[:-1]
            continue
        opener = C._CLOSERS.get(c)
        if opener is not None and url.count(opener) < url.count(c):
            url = url[:-1]
            continue
        break
    return url


class PerformanceTests(unittest.TestCase):
    """The question test stays linear on hostile final texts (the hook has a sub-second budget).

    Before the bounded URL pattern, 40K chars of a dotted run took about 8 s.
    """

    def test_hostile_inputs_are_fast(self) -> None:
        cases = {
            "dotted digits": ".".join(["1"] * 20000),
            "dotted words": ".".join(["ab"] * 13000),
            "long labels": ".".join(["a" * 60] * 1000),
            "url closers": "http://x.dev/" + ")" * 20000,
            "space-star tail": "Ready?" + " *" * 40000,
            "backtick line": "`" * 80000 + "x`" + "y" * 80000,
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                t0 = time.perf_counter()
                classify(summary(text), DEFAULTS)
                self.assertLess(time.perf_counter() - t0, 1.5)

    def test_long_hosts_and_schemes_still_mask(self) -> None:
        host = ".".join(["a%d" % i for i in range(20)]) + ".dev"  # more labels than the bound
        self.assertIsNone(question_cue("See https://%s/x?y=1 now." % host))
        self.assertIsNone(question_cue("See %s/x?y=1 now." % host))
        self.assertIsNone(question_cue("See git+ssh://h.dev/r?x now."))

    def test_rewritten_helpers_match_the_original_algorithms(self) -> None:
        rng = random.Random(20261005)
        tail_alphabet = ["a", "?", " ", "\t", "\n", "*", "_", "\u00a0", "\u2003", "\x1c", "`"]
        url_alphabet = ["a", "/", "?", ".", ",", ")", "(", "]", "[", "}", "{", "!", "'", "\"", "*", "_", "\uff1f"]
        for _ in range(3000):
            t = "".join(rng.choice(tail_alphabet) for _ in range(rng.randint(0, 14)))
            self.assertEqual(C.normalize_tail(t), _normalize_tail_reference(t), repr(t))
            u = "h://x" + "".join(rng.choice(url_alphabet) for _ in range(rng.randint(0, 12)))
            self.assertEqual(C._trim_url(u), _trim_url_reference(u), repr(u))

    def test_fifty_kilobyte_adversarial_strings_are_fast(self) -> None:
        # v2 adds the sentence scan, is_offer and the span helpers to the hook's path.
        n = 50 * 1024
        cases = {
            "fillers": ("so, " * n)[:n - 1] + "?",
            "sentence ends": (". " * n)[:n - 1] + "?",
            "alternatives": ("a or " * n)[:n - 1] + "?",
            "offer starts": ("should i " * n)[:n - 1] + "?",
            "closers": ("**:" * n)[:n - 1] + "?",
            "space run": "Should I" + " " * (n - 10) + "?",
            "comma run": "Should I" + ", " * (n // 2) + "or b?",
            "question marks": "?" * n,
            "newlines": "\n" * n + "x?",
            "underscores": "_" * n + "a?",
            "dotted digits": ".".join(["1"] * (n // 2)),
            "phrases": "want me to " * (n // 11),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                t0 = time.perf_counter()
                classify(summary(text), DEFAULTS)
                C.is_offer(text)
                C.question_sentence_span(text)
                C.phrase_sentence_span(text)
                C.final_sentence_span(text)
                self.assertLess(time.perf_counter() - t0, 0.5)


# --------------------------------------------------------------------------- v2 (contract 16.1)

TIER_FIXTURES = FIXTURES / "tiers"


def tier_rows() -> Dict[str, Tuple[str, str, str]]:
    """The tier table in tests/fixtures/synthetic/README.md: name -> (verdict, reason, tier)."""
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    rows = {}
    for m in re.finditer(r"^\|\s*tiers/([\w.-]+\.jsonl)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|", readme, re.M):
        rows[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return rows


class TierFixtureTests(unittest.TestCase):
    def test_every_tier_fixture_is_in_the_readme(self) -> None:
        on_disk = {p.name for p in TIER_FIXTURES.glob("*.jsonl")}
        self.assertGreaterEqual(len(on_disk), 6)
        self.assertTrue(all(n.startswith("tier_") for n in on_disk))
        self.assertEqual(on_disk, set(tier_rows()))

    def test_tier_fixture_verdicts_and_tiers(self) -> None:
        for name, (verdict, reason, tier) in sorted(tier_rows().items()):
            with self.subTest(fixture=name):
                p = TIER_FIXTURES / name
                ending = classify(parse_transcript(p), DEFAULTS)
                self.assertEqual(tuple(ending), (verdict, reason))
                self.assertEqual(tuple(classify(parse_lines(p.read_bytes(), p.stem, p), DEFAULTS)), (verdict, reason))
                # The table's tier is the one with no git facts (unsaved=None), as for a fixture.
                self.assertEqual(C.tier(ending.verdict, ending.reason), tier)
                if reason == C.SAVE_OFFER:
                    self.assertEqual(C.tier(ending.verdict, ending.reason, unsaved=True), C.WAITING)
                    self.assertEqual(C.tier(ending.verdict, ending.reason, unsaved=False), C.SOFT)

    def test_both_tiers_are_covered(self) -> None:
        tiers = [t for _, _, t in tier_rows().values()]
        self.assertGreaterEqual(tiers.count("waiting"), 3)
        self.assertGreaterEqual(tiers.count("soft"), 3)
        reasons = [r for _, r, _ in tier_rows().values()]
        for reason in ("trailing_q", "offer_q", "save_offer"):
            self.assertGreaterEqual(reasons.count(reason), 2, reason)


# Polite offers to do more work: soft.
OFFERS = (
    "Want me to commit these 18 files?",
    "Would you like me to open a PR?",
    "Do you want me to push?",
    "Should I commit?",
    "Shall I continue?",
    "Can I delete the old branch?",
    "Could I run the migration now?",
    "May I push to main?",
    "OK to push?",
    "Okay if I merge it?",
    "Want the full log?",
    "Want to see the diff?",
    "Want it?",
    "Ready for me to deploy?",
    "Ready to merge?",
    "Go ahead and tag it?",
    "Proceed with the cleanup?",
    "Sounds good?",
    "Sound good?",
    "Looks good?",
    "Good to go?",
    "Does that sound right?",
    "Does this work for you?",
    "Does that look okay?",
    "Anything else you need?",
    "Shall we ship it?",
    "Should we cut the release today?",
    "Need anything else?",
    "Commit and push?",
    "Push it?",
    "Merge it?",
    "Ship it?",
    "So, should I push?",
    "Ok, want me to commit?",
    "Also, should I update the changelog?",
    "And then shall I push?",
    "Alright, next: should I tag it?",
    "Great, cool, now should I push?",
    "- Should I commit?",
    "* Should I commit?",
    "\u2022 Should I commit?",
    "2. Should I commit?",
    "3) Should I commit?",
    "**Should I** commit?",
    "Should **I** commit?",
    "_Should I commit?_",
    "\"Should I push?\"",
    "\u201cShould I push?\u201d",
    "'Should I push?'",
    "> Should I push?",
    "## Should I push?",
    "(Should I push?)",
    "[Should I push?]",
    "\u2014 should I push?",
    "SHOULD I PUSH?",
    "Should I push\uff1f",
    "Want me to commit, or keep going?",
    "Should I commit or not?",
    "Should I commit, or no?",
    "Should I wait a day or two?",
    "Should I push it in an hour or so?",
    "Want the log, or anything else?",
    "Should I add more tests, or less?",
    "Should I also check something or something?",
    "Should I rewrite it, or should I just patch the call site?",
    "I can also add tests for it, interested?",
    "If you\u2019d like, I can open the PR now?",
    "If you want, I can split the commit?",
    "Happy to add a test too, sound good?",
    "Should I run `git fetch or pull`?",
    "Should I open https://x.dev/a?b=1 in the browser?",
    "Next, should I push?",
)

# Direct questions and choices the user has to answer: waiting.
DIRECT = (
    "Keep the sticky cart, or move it to the header?",
    "Should I keep A or switch to B?",
    "Should I keep A, or B?",
    "Should I push now, or wait?",
    "Should we use REST or GraphQL?",
    "Want it on main or on a branch?",
    "Okay to drop the table or keep it for a week?",
    "Merge or rebase?",
    "Push to main or a feature branch?",
    "Commit now or after the review?",
    "Which one do you prefer?",
    "Do you want to keep the old API?",
    "Is the deadline Friday?",
    "What should the button say?",
    "Postgres or SQLite?",
    "Which approach, A or B?",
    "Did the deploy work on your side?",
    "Can you check the staging URL?",
    "Should the cache expire after an hour?",
    "Should it live in core or in utils?",
    "Commits look right?",
    "Pushing to prod is risky, are you sure?",
    "Merger approved?",
    "So which one?",
    "Ready?",
    "OK?",
    "Now?",
    "Shipping it today?",
    "Wanted this?",
    "\u6e96\u5099\u3067\u304d\u307e\u3057\u305f\uff1f",
    "\u00bfLo subo a main?",
    "Would you like the dark theme as the default?",
    "Is `want me to` the right phrase to match?",
    "Should the push notifications go out at 9am?",
    "Is the release branch protected?",
)

# The OFFERS that are about saving the work (contract 16.1 "Save offers"): reason save_offer. Listed
# by hand, not computed with is_save_offer, so the tests check the verb list itself.
SAVE_OFFERS = frozenset((
    "Want me to commit these 18 files?",
    "Would you like me to open a PR?",
    "Do you want me to push?",
    "Should I commit?",
    "May I push to main?",
    "OK to push?",
    "Okay if I merge it?",
    "Ready for me to deploy?",
    "Ready to merge?",
    "Shall we ship it?",
    "Should we cut the release today?",
    "Commit and push?",
    "Push it?",
    "Merge it?",
    "Ship it?",
    "So, should I push?",
    "Ok, want me to commit?",
    "And then shall I push?",
    "Great, cool, now should I push?",
    "- Should I commit?",
    "* Should I commit?",
    "\u2022 Should I commit?",
    "2. Should I commit?",
    "3) Should I commit?",
    "**Should I** commit?",
    "Should **I** commit?",
    "_Should I commit?_",
    "\"Should I push?\"",
    "\u201cShould I push?\u201d",
    "'Should I push?'",
    "> Should I push?",
    "## Should I push?",
    "(Should I push?)",
    "[Should I push?]",
    "\u2014 should I push?",
    "SHOULD I PUSH?",
    "Should I push\uff1f",
    "Want me to commit, or keep going?",
    "Should I commit or not?",
    "Should I commit, or no?",
    "Should I push it in an hour or so?",
    "If you\u2019d like, I can open the PR now?",
    "If you want, I can split the commit?",
    "Next, should I push?",
))


class OfferTests(unittest.TestCase):
    def test_there_are_many_cases(self) -> None:
        self.assertGreaterEqual(len(OFFERS) + len(DIRECT), 40)

    def test_offers(self) -> None:
        # A save offer is still an offer (is_offer); only its reason differs.
        for s in OFFERS:
            with self.subTest(sentence=s):
                self.assertTrue(C.is_offer(s))
                self.assertEqual(C.is_save_offer(s), s in SAVE_OFFERS)
                expected = C.SAVE_OFFER if s in SAVE_OFFERS else C.OFFER_Q
                self.assertEqual(question_cue("All 88 tests pass.\n\n" + s), expected)

    def test_both_kinds_of_offer_are_covered(self) -> None:
        self.assertLessEqual(SAVE_OFFERS, set(OFFERS))
        self.assertGreaterEqual(len(SAVE_OFFERS), 20)
        self.assertGreaterEqual(len(set(OFFERS) - SAVE_OFFERS), 20)

    def test_direct_questions(self) -> None:
        for s in DIRECT:
            with self.subTest(sentence=s):
                self.assertFalse(C.is_offer(s))
                self.assertEqual(question_cue("All 88 tests pass.\n\n" + s), C.TRAILING_Q)

    def test_odd_values(self) -> None:
        for v in (None, 42, "", "   ", "?", "**", ["Should I push?"]):
            with self.subTest(value=v):
                self.assertFalse(C.is_offer(v))

    def test_the_contract_examples(self) -> None:
        self.assertEqual(question_cue("Should I keep A or switch to B?"), "trailing_q")
        self.assertEqual(question_cue("Should I commit?"), "save_offer")
        self.assertEqual(question_cue("Want me to commit, or keep going?"), "save_offer")
        self.assertEqual(question_cue("Should I continue?"), "offer_q")
        self.assertEqual(question_cue("Want me to tidy up, or keep going?"), "offer_q")
        self.assertEqual(question_cue("Keep the sticky cart, or move it to the header?"), "trailing_q")


class QuestionSentenceTests(unittest.TestCase):
    """question_cue judges the sentence holding the last real '?' (contract 16.1)."""

    def test_the_last_question_decides(self) -> None:
        # Documented contract behaviour: an offer after a direct question is soft (see open issues).
        self.assertEqual(question_cue("Postgres or SQLite? Want me to scaffold it?"), "offer_q")
        self.assertEqual(question_cue("Want me to scaffold it? Or do you prefer SQLite?"), "trailing_q")
        self.assertEqual(question_cue("Should I push? Which branch, main or dev?"), "trailing_q")
        # only the judged sentence counts: a save verb in an earlier or later sentence does not
        self.assertEqual(question_cue("Pushed the branch. Want me to tidy the docs?"), "offer_q")
        self.assertEqual(question_cue("Want me to tidy the docs? Then I'll push."), "offer_q")
        self.assertEqual(question_cue("Tidy the docs? Want me to push?"), "save_offer")

    def test_a_statement_after_the_question(self) -> None:
        self.assertEqual(question_cue("Should I push? I'll wait here."), "save_offer")
        self.assertEqual(question_cue("Should I continue? I'll wait here."), "offer_q")
        self.assertEqual(question_cue("Which database? I'll wait here."), "trailing_q")
        self.assertEqual(question_cue("**Should I push it?** I'll leave it on the branch."), "save_offer")

    def test_sentence_ends(self) -> None:
        self.assertEqual(question_cue("Done: should I push?"), "save_offer")
        self.assertEqual(question_cue("Tests pass! Should I push?"), "save_offer")
        self.assertEqual(question_cue("Built it.\nShould I push?"), "save_offer")
        self.assertEqual(question_cue("**Next:** should I push?"), "save_offer")
        self.assertEqual(question_cue("Pushed: should I continue?"), "offer_q")  # the verb sits before the sentence end
        self.assertEqual(question_cue("Options:\n1. Keep A\n2. Switch to B\nWhich one?"), "trailing_q")
        self.assertEqual(question_cue("Options:\n- A\n- B\nShould I go with A?"), "offer_q")
        # a line break ends a sentence, so a choice split over two lines is judged on its last line
        self.assertEqual(question_cue("Should I keep A\nor switch to B?"), "trailing_q")
        self.assertEqual(question_cue("\u6e96\u5099\u3067\u304d\u307e\u3057\u305f\u3002Should I push?"), "save_offer")

    def test_sentence_ends_inside_code_and_urls_do_not_count(self) -> None:
        self.assertEqual(question_cue("Should I run `make. it` again?"), "offer_q")
        self.assertEqual(question_cue("Should I deploy https://x.dev/a?b=1 now?"), "save_offer")  # the URL's ? ends nothing
        self.assertEqual(question_cue("Should I open https://x.dev/a?b=1 now?"), "offer_q")
        # a period right after a URL is sentence punctuation, not part of the URL
        self.assertEqual(question_cue("Should I open https://x.dev/a. Which env?"), "trailing_q")
        self.assertEqual(question_cue("Ran `should I?` here. Which env?"), "trailing_q")

    def test_abbreviations_do_not_end_a_sentence(self) -> None:
        self.assertEqual(question_cue("Should I use a helper, e.g. the one in utils?"), "offer_q")
        self.assertEqual(question_cue("Should I pin it, i.e. lock the version?"), "offer_q")
        self.assertEqual(question_cue("Done. Use the new one, i.e. the faster path?"), "trailing_q")

    def test_anything_else_is_ignored_first(self) -> None:
        self.assertEqual(question_cue("Done. Anything else?"), "offer_q")
        self.assertEqual(classify(summary("Done. Anything else?"), DEFAULTS), ("clean", "ignored_tail"))
        self.assertEqual(classify(summary("Done. Anything else?")), ("open_question", "offer_q"))
        self.assertEqual(classify(summary("Fixed. Does that help?"), DEFAULTS), ("clean", "ignored_tail"))

    def test_the_question_sentence_never_starts_before_the_window(self) -> None:
        text = "Should I " + "really " * 40 + "keep it or drop it?"
        self.assertGreater(len(text), C.TAIL_CHARS)
        # the "should i" opener is outside the window: the visible part is a plain choice
        self.assertEqual(question_cue(text), "trailing_q")
        span = C.question_sentence_span(text)
        self.assertEqual(span, (len(text) - C.TAIL_CHARS, len(text)))


class SpanTests(unittest.TestCase):
    def cut(self, text: str, span: Any) -> Any:
        return None if span is None else text[span[0]:span[1]]

    def test_question_sentence_span(self) -> None:
        cases = (
            ("Done. Should I push? Ok.", "Should I push?"),
            ("Ship it?   \n\n", "Ship it?"),
            ("**Want me to push?**", "**Want me to push?"),
            ("Built.\n\n  Which one?", "Which one?"),
            ("Is it live at https://x.dev/login?", "Is it live at https://x.dev/login?"),
            ("See https://x.dev/a?b=1. Which env?", "Which env?"),
            ("Use `a?` here. Then what?", "Then what?"),
            ("\u6e96\u5099\u3067\u304d\u307e\u3057\u305f\uff1f", "\u6e96\u5099\u3067\u304d\u307e\u3057\u305f\uff1f"),
            ("He said \"stop.\" Should I?", "Should I?"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.cut(text, C.question_sentence_span(text)), expected)

    def test_question_sentence_span_none(self) -> None:
        for text in (None, 5, "", "   ", "Done.", "Run `a?` now.", "See https://x.dev/?q=1", "Q?" + "x" * 200):
            with self.subTest(text=text):
                self.assertIsNone(C.question_sentence_span(text))

    def test_offsets_are_into_the_text_as_given(self) -> None:
        text = "Done.  Should I push?  **\n\n"
        start, end = C.question_sentence_span(text)
        self.assertEqual((text[start], text[end - 1]), ("S", "?"))
        self.assertEqual(end, text.index("?") + 1)

    def test_window_edge(self) -> None:
        text = "x" * 300 + "?"
        self.assertEqual(C.question_sentence_span(text), (101, 301))
        self.assertEqual(C.window_start(text), 101)
        self.assertEqual(C.window_start("short"), 0)
        self.assertEqual(C.window_start(None), 0)

    def test_phrase_sentence_span(self) -> None:
        cases = (
            ("Tests pass. Let me know if you want me to open the PR. Thanks", "Let me know if you want me to open the PR."),
            ("Done. Say the word", "Say the word"),
            ("Your call. Both work.", "Your call."),
            # the save sentence decides the reason (save_offer), so the span is that sentence
            ("Want me to push. Or say the word and I'll wait.", "Want me to push."),
            ("Done.\nYour call", "Your call"),
            ("Want me to tidy up. Your call on naming.", "Your call on naming."),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.cut(text, C.phrase_sentence_span(text)), expected)
        for text in (None, "", "Done.", "Phrase in code: `want me to`.", "want me to " + "x" * 195):
            with self.subTest(text=text):
                self.assertIsNone(C.phrase_sentence_span(text))

    def test_phrase_reason_and_save_check_come_from_the_same_match(self) -> None:
        for text, reason, sentence in (
            ("Say the word and I'll commit and push. Your call on the naming though.",
             C.SAVE_OFFER, "Say the word and I'll commit and push."),
            ("Your call on naming. Say the word and I will push.", C.SAVE_OFFER, "Say the word and I will push."),
            ("Want me to tidy the fixtures. Your call on the naming.", C.YOUR_CALL, "Your call on the naming."),
            ("Your call on the naming. Want me to tidy the fixtures", C.WANT_ME_TO, "Want me to tidy the fixtures"),
        ):
            with self.subTest(text=text):
                self.assertEqual(C.question_cue(text), reason)
                self.assertEqual(self.cut(text, C.phrase_sentence_span(text)), sentence)

    def test_final_sentence_span(self) -> None:
        self.assertEqual(self.cut("One. Two three.", C.final_sentence_span("One. Two three.")), "Two three.")
        self.assertEqual(self.cut("I need two decisions.", C.final_sentence_span("I need two decisions.")),
                         "I need two decisions.")
        self.assertEqual(self.cut("A.\nB", C.final_sentence_span("A.\nB")), "B")
        self.assertIsNone(C.final_sentence_span(""))
        self.assertIsNone(C.final_sentence_span(None))

    def test_question_cue_agrees_with_the_span(self) -> None:
        for s in OFFERS + DIRECT:
            text = "Built it.\n\n" + s
            span = C.question_sentence_span(text)
            if span is None:
                continue
            with self.subTest(sentence=s):
                sentence = text[span[0]:span[1]]
                if not C.is_offer(sentence):
                    expected = C.TRAILING_Q
                else:
                    expected = C.SAVE_OFFER if C.is_save_offer(sentence) else C.OFFER_Q
                self.assertEqual(question_cue(text), expected)

    def test_phrase_save_offer_is_judged_on_the_phrase_sentence(self) -> None:
        # question_cue's phrase path asks is_save_offer about phrase_sentence_span, the sentence
        # core.quote shows: a save verb in another sentence does not count.
        cases = (
            ("Pushed the fix. Let me know if you want me to tidy the README.", "want_me_to"),
            ("Let me know if you want me to tidy the README. Then I can push.", "want_me_to"),
            ("Tidied the README. Let me know if you want me to push it.", "save_offer"),
            ("If you want it in prod, say the word and I'll deploy.", "save_offer"),
            ("Merged the docs branch. Your call.", "your_call"),
            ("It is your call whether to merge.", "save_offer"),
            ("Say the word and I'll check it in.", "save_offer"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(question_cue(text), expected)
                span = C.phrase_sentence_span(text)
                self.assertEqual(C.is_save_offer(text[span[0]:span[1]]), expected == "save_offer")



class SaveOfferTests(unittest.TestCase):
    """classify.is_save_offer and the save_offer reason (contract 16.1 "Save offers")."""

    POSITIVES = (
        "Want me to commit?",
        "Want me to commit these 18 files?",
        "Should I push the branch?",
        "Want me to force-push it?",
        "Want me to open a pull request?",
        "Want me to open pull requests for both?",
        "Would you like me to open a PR?",
        "Want me to raise PRs for both repos?",
        "Ready to merge?",
        "Should I squash-merge it into main?",
        "Should I deploy it to staging?",
        "Want me to redeploy?",
        "Ship it?",
        "Should I publish the package?",
        "Want me to cut a release?",
        "Should I release 2.1 now?",
        "Should I land it?",
        "Want me to check it in?",
        "Want me to check these in and push?",
        "Should I check this in now?",
        "Want me to run `git push`?",
        "Want me to run `git commit -am wip`?",
        "Should I get these committed?",
        "Want me to keep merging the rest?",
        "SHOULD I PUSH?",
    )
    NEGATIVES = (
        "Want me to rename it?",
        "Should I add a test?",
        "Want the full log?",
        "Should I continue?",
        "Want me to switch to landscape mode?",
        "Want me to add a landmark to the map?",
        "Want me to make that commitment in the doc?",
        "Should I update the shipment table?",
        "Want me to check it in the browser?",
        "Should I check this in Safari first?",
        "Should I fetch and pull?",
        "Want me to print the prices?",
        "Should I re-run the suite?",
    )

    def test_positives(self) -> None:
        for s in self.POSITIVES:
            with self.subTest(sentence=s):
                self.assertTrue(C.is_offer(s))
                self.assertTrue(C.is_save_offer(s))
                self.assertEqual(question_cue("Built it.\n\n" + s), C.SAVE_OFFER)

    def test_negatives(self) -> None:
        for s in self.NEGATIVES:
            with self.subTest(sentence=s):
                self.assertTrue(C.is_offer(s))
                self.assertFalse(C.is_save_offer(s))
                self.assertEqual(question_cue("Built it.\n\n" + s), C.OFFER_Q)

    def test_odd_values(self) -> None:
        for v in (None, 42, "", "   ", ["commit"], b"commit"):
            with self.subTest(value=v):
                self.assertFalse(C.is_save_offer(v))

    def test_a_save_verb_in_a_direct_question_stays_waiting(self) -> None:
        # is_save_offer is asked only about offers: a choice or a direct question is trailing_q
        for s in ("Should the push notifications go out at 9am?",
                  "Push to main or a feature branch?",
                  "Should I merge it or rebase it?",
                  "Is the release branch protected?",
                  "Did the deploy work on your side?",
                  "Which PR should go first?"):
            with self.subTest(sentence=s):
                self.assertTrue(C.is_save_offer(s) or "push" not in s.lower())  # the verb is there ...
                self.assertEqual(question_cue("Built it.\n\n" + s), C.TRAILING_Q)  # ... and still waits

    def test_a_save_verb_inside_code_counts(self) -> None:
        # matched on the unmasked sentence (contract 16.1), unlike the offer phrases themselves
        self.assertEqual(question_cue("Ok to run `git push origin main`?"), C.SAVE_OFFER)
        self.assertEqual(question_cue("Ok to run `make release`?"), C.SAVE_OFFER)
        self.assertEqual(question_cue("Ok to run `make lint`?"), C.OFFER_Q)
        # but code still hides a phrase cue, with or without a save verb
        self.assertIsNone(question_cue("Logged `want me to push` as a phrase."))

    def test_classify_reports_save_offer(self) -> None:
        self.assertEqual(classify(summary("All tests pass. Want me to commit and push?"), DEFAULTS),
                         ("open_question", "save_offer"))
        self.assertEqual(classify(summary("Let me know if you want me to open the PR."), DEFAULTS),
                         ("open_question", "save_offer"))
        self.assertEqual(classify(summary("Want me to push?", human_after_final=True), DEFAULTS),
                         ("clean", "human_after_final"))
        self.assertEqual(classify(summary("Merged. Ship it?"), [r"Ship it\?$"]), ("clean", "ignored_tail"))

    def test_long_offers_are_fast(self) -> None:
        start = time.perf_counter()
        for junk in ("check it in " * 4000, "push" * 20000, "re-" * 20000 + "deploy?", "pull " * 20000):
            C.is_save_offer(junk)
            question_cue("Want me to " + junk + "?")
        self.assertLess(time.perf_counter() - start, 2.0)


class TierTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(C.CLASSIFIER_VERSION, 3)
        self.assertEqual((C.OFFER_Q, C.WAITING, C.SOFT), ("offer_q", "waiting", "soft"))
        self.assertEqual(C.TIERS, ("waiting", "soft"))
        self.assertFalse(C.WAITING_REASONS & C.SOFT_REASONS)
        self.assertEqual(C.SOFT_REASONS, {"offer_q", "want_me_to", "say_the_word", "your_call", "handoff"})
        self.assertEqual(C.SAVE_OFFER, "save_offer")
        self.assertEqual(C.CONDITIONAL_REASONS, {"save_offer"})
        self.assertFalse(C.CONDITIONAL_REASONS & (C.WAITING_REASONS | C.SOFT_REASONS))
        self.assertLessEqual({"ask_user", "plan_approval", "trailing_q", "limit_meta", "limit_text", "rate_limit"},
                             C.WAITING_REASONS)

    def test_tier_table(self) -> None:
        cases = (
            (("usage_limit", "limit_meta"), "waiting"),
            (("usage_limit", "limit_text"), "waiting"),
            (("usage_limit", "rate_limit"), "waiting"),
            (("open_question", "ask_user"), "waiting"),
            (("open_question", "plan_approval"), "waiting"),
            (("open_question", "trailing_q"), "waiting"),
            (("open_question", "offer_q"), "soft"),
            (("open_question", "want_me_to"), "soft"),
            (("open_question", "say_the_word"), "soft"),
            (("open_question", "your_call"), "soft"),
            (("open_question", "save_offer"), "soft"),         # unsaved unknown: soft
            (("open_question", "agent_blocked"), "waiting"),   # an unknown reason is never hidden
            (("open_question", None), "waiting"),
            (("clean", "no_question"), None),
            (("clean", "trailing_q"), None),
            (("clean", "save_offer"), None),
            (("api_error", "server_error"), None),
            (("empty", "no_final_text"), None),
            (("automated", "sdk-cli"), None),
            ((None, None), None),
        )
        for (verdict, reason), expected in cases:
            with self.subTest(verdict=verdict, reason=reason):
                self.assertEqual(C.tier(verdict, reason), expected)

    def test_direct_questions_setting(self) -> None:
        self.assertEqual(C.tier("open_question", "trailing_q", "soft"), "soft")
        self.assertEqual(C.tier("open_question", "trailing_q", "waiting"), "waiting")
        self.assertEqual(C.tier("open_question", "trailing_q", "bogus"), "waiting")   # unknown: never hide
        # the setting moves direct questions only
        self.assertEqual(C.tier("open_question", "ask_user", "soft"), "waiting")
        self.assertEqual(C.tier("usage_limit", "limit_meta", "soft"), "waiting")
        self.assertEqual(C.tier("open_question", "offer_q", "waiting"), "soft")

    def test_save_offer_tier_follows_the_repo(self) -> None:
        oq = "open_question"
        self.assertEqual(C.tier(oq, "save_offer", unsaved=True), "waiting")
        self.assertEqual(C.tier(oq, "save_offer", unsaved=False), "soft")
        self.assertEqual(C.tier(oq, "save_offer", unsaved=None), "soft")
        self.assertEqual(C.tier(oq, "save_offer"), "soft")
        self.assertEqual(C.tier(oq, "save_offer", "waiting", True), "waiting")  # positional order
        # only True counts (a truthy non-bool is not a git answer)
        for odd in (1, "yes", [1]):
            with self.subTest(unsaved=odd):
                self.assertEqual(C.tier(oq, "save_offer", unsaved=odd), "soft")
        # direct_questions moves trailing_q only, never a save offer
        self.assertEqual(C.tier(oq, "save_offer", "soft", True), "waiting")
        self.assertEqual(C.tier(oq, "save_offer", "soft", False), "soft")
        # unsaved moves save_offer only
        self.assertEqual(C.tier(oq, "offer_q", unsaved=True), "soft")
        self.assertEqual(C.tier(oq, "want_me_to", unsaved=True), "soft")
        self.assertEqual(C.tier(oq, "trailing_q", unsaved=False), "waiting")
        self.assertEqual(C.tier(oq, "ask_user", unsaved=False), "waiting")
        self.assertEqual(C.tier("usage_limit", "limit_meta", unsaved=False), "waiting")
        self.assertIsNone(C.tier("clean", "save_offer", unsaved=True))
        self.assertIsNone(C.tier("automated", "save_offer", unsaved=True))

    def test_every_classifier_reason_has_a_tier(self) -> None:
        for reason in (C.TRAILING_Q, C.OFFER_Q, C.SAVE_OFFER, C.WANT_ME_TO, C.SAY_THE_WORD, C.YOUR_CALL,
                       C.ASK_USER, C.PLAN_APPROVAL):
            self.assertIn(C.tier(C.OPEN_QUESTION, reason), C.TIERS)
            self.assertIn(C.tier(C.OPEN_QUESTION, reason, unsaved=True), C.TIERS)


if __name__ == "__main__":
    unittest.main()
