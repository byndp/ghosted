"""Tests for core.quote.extract (contract 16.2). Synthetic text only."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from typing import Any, Optional

from core import classify as C
from core.quote import ELLIPSIS, extract

OQ = "open_question"


def s(final_text: Any = None, pending_question: Any = None) -> SimpleNamespace:
    return SimpleNamespace(final_text=final_text, pending_question=pending_question)


def q(text: Any, reason: str = "trailing_q", verdict: str = OQ, limit: int = 200,
      pending: Any = None) -> Optional[str]:
    return extract(s(text, pending), verdict, reason, limit)


class QuestionQuoteTests(unittest.TestCase):
    def test_the_mock(self) -> None:
        text = "Both layouts build.\n\n**Keep the sticky cart, or move it to the header?**\n"
        self.assertEqual(q(text), "Keep the sticky cart, or move it to the header?")

    def test_ends_at_the_last_real_question_mark(self) -> None:
        self.assertEqual(q("Should I push? I'll wait here.", "save_offer"), "Should I push?")
        self.assertEqual(q("Which DB? Want me to scaffold it?", "offer_q"), "Want me to scaffold it?")
        self.assertEqual(q("Did https://x.dev/search?q=1 work?"), "Did https://x.dev/search?q=1 work?")
        self.assertEqual(q("Run `a?` first. Then which env?"), "Then which env?")
        self.assertEqual(q("準備できました？"), "準備できました?")

    def test_whitespace_collapses(self) -> None:
        self.assertEqual(q("Done.\n\nKeep the cart\n  or move\tit?"), "or move it?")  # a newline ends a sentence
        self.assertEqual(q("Done. Keep   the cart,   or move it?"), "Keep the cart, or move it?")

    def test_emphasis_headings_and_quotes_trimmed(self) -> None:
        self.assertEqual(q("## Should I ship?", "save_offer"), "Should I ship?")
        self.assertEqual(q("_Ship it?_", "save_offer"), "Ship it?")
        self.assertEqual(q("Done. **Next:** should I push?", "save_offer"), "should I push?")
        self.assertEqual(q("“Should I push?”", "save_offer"), "Should I push?")
        self.assertEqual(q("- Should I push?", "save_offer"), "Should I push?")
        self.assertEqual(q("Should I run `npm test` *again*?", "offer_q"), "Should I run `npm test` *again*?")

    def test_inline_backticks_kept(self) -> None:
        self.assertEqual(q("Done. Should I run `make deploy`?", "save_offer"), "Should I run `make deploy`?")

    def test_long_sentence_gets_an_ellipsis(self) -> None:
        text = "Done. Should I " + " ".join("word%d" % i for i in range(80)) + " now?"
        out = q(text, "offer_q")
        self.assertTrue(out.startswith(ELLIPSIS), out[:10])
        self.assertTrue(out.endswith("now?"))
        self.assertLessEqual(len(out), 200)
        self.assertNotIn("  ", out)
        self.assertRegex(out[1:], r"^word\d+ ")  # cut forward to a word start, never mid-word

    def test_limit_is_respected(self) -> None:
        text = "Done. Should I " + "really " * 60 + "push?"
        for limit in (200, 80, 20, 5, 2, 1):
            with self.subTest(limit=limit):
                out = q(text, "save_offer", limit=limit)
                self.assertIsNotNone(out)
                self.assertLessEqual(len(out), limit)
        self.assertIsNone(q(text, "save_offer", limit=0))
        self.assertEqual(q("Ship it?", "save_offer", limit=8), "Ship it?")
        self.assertLessEqual(len(q("Ship it?", "save_offer", limit=7)), 7)

    def test_one_huge_word_is_hard_cut(self) -> None:
        out = q("Use " + "z" * 150 + "? ok", limit=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.startswith(ELLIPSIS) and out.endswith("?"))

    def test_a_sentence_cut_by_the_window_is_marked(self) -> None:
        # No sentence end inside the 200-char window: the quote starts at a word, after an ellipsis.
        text = "Okay " + "lorem ipsum " * 30 + "should I push?"
        out = q(text, "save_offer")
        self.assertTrue(out.startswith(ELLIPSIS + "lorem ") or out.startswith(ELLIPSIS + "ipsum "), out[:12])
        self.assertTrue(out.endswith("should I push?"))
        self.assertLessEqual(len(out), 200)

    def test_leading_whitespace_keeps_the_first_word(self) -> None:
        for lead in ("  ", " ", "\t", " \t "):
            with self.subTest(lead=repr(lead)):
                self.assertEqual(q(lead + "Should I commit?"), "Should I commit?")
                self.assertEqual(q(lead + "Should I commit?", "save_offer"), "Should I commit?")
                self.assertEqual(q(lead + "Done. Say the word", "say_the_word"), "Say the word")
        self.assertEqual(q("Done.\n\n  Should I commit?"), "Should I commit?")

    def test_masking_applies(self) -> None:
        key = "sk-ant-" + "aB3dE5gH7jK9mN1pQ" * 2
        out = q("Rotated it. Should I put %s in bob@example.com's vault for $480?" % key, "offer_q")
        self.assertEqual(out, "Should I put [key] in [email]'s vault for [amount]?")
        out = q("Call 512-555-0100 or ship to 123 Main St?")
        self.assertEqual(out, "Call [phone] or ship to [address]?")

    def test_a_key_cut_by_the_window_is_still_masked(self) -> None:
        key = "sk-ant-" + "aB3dE5gH7jK9mN1pQ" * 3
        text = "x " + "word " * 40 + key + " " + "and more " * 17 + "ok?"
        self.assertGreater(text.index(key) + len(key), len(text) - 200)  # the window cuts into the key
        self.assertLess(text.index(key), len(text) - 200)
        out = q(text)
        self.assertNotIn(key[-12:], out)
        self.assertNotIn("aB3dE5", out)

    def test_no_control_characters_reach_the_terminal(self) -> None:
        out = q("Done. Should I​ push‮ it\x1b[31m now?", "save_offer")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertNotIn("​", out)


class PhraseQuoteTests(unittest.TestCase):
    def test_phrase_sentence(self) -> None:
        text = "Tests pass. Let me know if you want me to open the PR. Thanks"
        self.assertEqual(q(text, "save_offer"), "Let me know if you want me to open the PR.")
        self.assertEqual(q("Tests pass. Let me know if you want me to tidy it. Thanks", "want_me_to"),
                         "Let me know if you want me to tidy it.")
        self.assertEqual(q("If you need prod too, say the word", "say_the_word"), "If you need prod too, say the word")
        self.assertEqual(q("Both options work. Your call.", "your_call"), "Your call.")

    def test_phrase_inside_code_is_not_quoted(self) -> None:
        self.assertIsNone(q("Matched `want me to` in the test.", "want_me_to"))


class SaveOfferQuoteTests(unittest.TestCase):
    """save_offer (contract 16.1) comes from a '?' offer or a phrase cue; the quote is that sentence."""

    def test_from_a_question(self) -> None:
        self.assertEqual(q("All 212 tests pass.\n\nWant me to commit these 18 files?", "save_offer"),
                         "Want me to commit these 18 files?")
        self.assertEqual(q("Built it. **Should I push it?** I'll leave it on the branch.", "save_offer"),
                         "Should I push it?")
        self.assertEqual(q("Done. Ok to run `git push origin main`?", "save_offer"), "Ok to run `git push origin main`?")

    def test_from_a_phrase_cue(self) -> None:
        text = "CI is green on all three jobs. Let me know if you want me to open the pull request."
        self.assertEqual(q(text, "save_offer"), "Let me know if you want me to open the pull request.")
        self.assertEqual(q("Staging is fine. If you want prod too, say the word and I'll deploy", "save_offer"),
                         "If you want prod too, say the word and I'll deploy")

    def test_the_question_wins_over_a_phrase(self) -> None:
        # question_cue judges the '?' sentence first; extract shows the same one
        text = "Say the word and I'll tidy up. Want me to push?"
        self.assertEqual(C.question_cue(text), C.SAVE_OFFER)
        self.assertEqual(q(text, "save_offer"), "Want me to push?")

    def test_masked_and_limited(self) -> None:
        out = q("Done. Want me to push the fix and mail bob@example.com the $480 invoice?", "save_offer")
        self.assertEqual(out, "Want me to push the fix and mail [email] the [amount] invoice?")
        text = "Done. Want me to commit " + "these files " * 40 + "now?"
        out = q(text, "save_offer", limit=60)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.startswith(ELLIPSIS) and out.endswith("now?"))

    def test_the_classifier_and_the_quote_agree(self) -> None:
        for text in ("Tests pass. Should I merge it?", "Green. Let me know if you want me to push.",
                     "Done. Should I deploy https://x.dev/a?b=1 now?"):
            with self.subTest(text=text):
                reason = C.question_cue(text)
                self.assertEqual(reason, C.SAVE_OFFER)
                self.assertTrue(C.is_save_offer(q(text, reason)))

    def test_nothing_usable(self) -> None:
        self.assertIsNone(q("Done.", "save_offer"))
        self.assertIsNone(q(None, "save_offer"))
        self.assertIsNone(q("`git push?`", "save_offer"))  # the only '?' is code, no phrase either


class PendingQuestionTests(unittest.TestCase):
    def test_ask_user_uses_the_pending_question(self) -> None:
        self.assertEqual(q("I need two decisions.", "ask_user", pending="Which DB should we use? (+2 more)"),
                         "Which DB should we use? (+2 more)")

    def test_plan_approval_uses_the_plan_title(self) -> None:
        self.assertEqual(q("Here is the plan.", "plan_approval", pending="Migrate billing to the job queue"),
                         "Migrate billing to the job queue")

    def test_long_pending_question_keeps_its_end(self) -> None:
        pending = "Given " + "many words " * 40 + "which DB should we use? (+1 more)"
        out = q("x", "ask_user", pending=pending)
        self.assertTrue(out.startswith(ELLIPSIS) and out.endswith("(+1 more)"))
        self.assertLessEqual(len(out), 200)

    def test_long_plan_keeps_its_start(self) -> None:
        pending = "Migrate billing " + "and more steps " * 40
        out = q("x", "plan_approval", pending=pending)
        self.assertTrue(out.startswith("Migrate billing") and out.endswith(ELLIPSIS))
        self.assertLessEqual(len(out), 200)

    def test_pending_is_masked(self) -> None:
        self.assertEqual(q("x", "ask_user", pending="Send it to bob@example.com?"), "Send it to [email]?")

    def test_falls_back_to_the_final_text(self) -> None:
        for pending in (None, "", "   ", "**", 42, ["Which DB?"]):
            with self.subTest(pending=pending):
                self.assertEqual(q("Done. Which DB should we use?", "ask_user", pending=pending),
                                 "Which DB should we use?")
        self.assertEqual(q("I need two decisions.", "ask_user"), "I need two decisions.")
        self.assertEqual(q("Read it all. Here is the plan", "plan_approval"), "Here is the plan")

    def test_summary_without_the_field(self) -> None:
        self.assertEqual(extract(SimpleNamespace(final_text="Which one?"), OQ, "ask_user"), "Which one?")
        self.assertIsNone(extract(object(), OQ, "ask_user"))


class NothingUsableTests(unittest.TestCase):
    def test_usage_limit_is_none(self) -> None:
        self.assertIsNone(q("You've hit your weekly limit", "limit_text", verdict="usage_limit"))
        self.assertIsNone(q("Should I retry?", "limit_meta", verdict="usage_limit"))
        self.assertIsNone(q("Should I retry?", "rate_limit", verdict="usage_limit"))
        self.assertIsNone(q("Should I retry?", "rate_limit", verdict=OQ))

    def test_nothing_usable(self) -> None:
        for text in (None, "", "   ", 42, "**", "?", "`a?`"):
            with self.subTest(text=text):
                self.assertIsNone(q(text))
        self.assertIsNone(q("z" * 400 + "?"))  # one word, cut by the window: nothing whole is left

    def test_question_reason_without_a_question_falls_back_to_a_phrase(self) -> None:
        self.assertEqual(q("Done. Your call.", "trailing_q"), "Your call.")
        self.assertIsNone(q("Done.", "trailing_q"))

    def test_unknown_reasons_use_the_last_sentence(self) -> None:
        self.assertEqual(q("Started the migration. It is at step 3 of 7.", "agent_blocked"), "It is at step 3 of 7.")
        self.assertEqual(q("Done. Should I push?", "agent_blocked"), "Should I push?")

    def test_odd_limit(self) -> None:
        self.assertEqual(q("Ship it?", "save_offer", limit="x"), "Ship it?")  # type: ignore[arg-type]


class ConsistencyTests(unittest.TestCase):
    def test_the_quote_is_the_sentence_the_classifier_judged(self) -> None:
        texts = ("Built it.\n\nShould I push? I'll wait.", "Done. Keep A, or B?", "x. Want me to commit, or wait?",
                 "Built it.\n\nShould I continue? I'll wait.", "x. Want me to tidy up, or wait?",
                 "Pushed it. Should I tidy the docs?", "Tidied. Let me know if you want me to push.",
                 "Pushed. Let me know if you want me to tidy it.", "If prod too, say the word and I'll deploy.")
        seen = set()
        for text in texts:
            with self.subTest(text=text):
                reason = C.question_cue(text)
                seen.add(reason)
                out = q(text, reason)
                self.assertIsNotNone(out)
                if "?" in out:  # a question sentence (a phrase-cue save offer has none)
                    self.assertEqual(C.is_offer(out), reason != C.TRAILING_Q)
                # the shown sentence holds the save verb exactly when the reason is save_offer
                self.assertEqual(C.is_save_offer(out), reason == C.SAVE_OFFER)
        self.assertEqual(seen, {C.TRAILING_Q, C.OFFER_Q, C.SAVE_OFFER, C.WANT_ME_TO})

    def test_long_input_is_fast(self) -> None:
        n = 50 * 1024
        for text in ("so, " * (n // 4) + "?", ". " * (n // 2) + "x?", "a." * (n // 2), "_" * n + "a?"):
            with self.subTest(head=text[:4]):
                t0 = time.perf_counter()
                reasons = ("trailing_q", "offer_q", "save_offer", "want_me_to", "ask_user", "plan_approval",
                           "agent_blocked")
                for reason in reasons:
                    extract(s(text, text), OQ, reason)
                self.assertLess(time.perf_counter() - t0, 0.3 * len(reasons))  # linear: quadratic takes seconds


if __name__ == "__main__":
    unittest.main()
