from __future__ import annotations

import unittest

from core import sanitize
from core.sanitize import (MSG_ADDRESS, MSG_BLANK, MSG_CONTROL, MSG_EMAIL, MSG_MONEY, MSG_PHONE,
                           MSG_SECRET, Refused, check, problems)


def fake(prefix: str, length: int, alphabet: str = "aB3dE5gH7jK9mN1pQ") -> str:
    """A synthetic token: built at runtime so no token-shaped literal sits in the source."""
    body = (alphabet * (length // len(alphabet) + 1))[:length]
    return prefix + body


HEX40 = "".join("0123456789abcdef"[(i * 7) % 16] for i in range(40))
JWT = ".".join([fake("ey" + "J", 18), fake("ey" + "J", 14), fake("", 22)])

OK_TITLES = [
    # spec examples
    "home: pay the overdue water bill",
    "boathouse: build 140 expires Oct 18",
    "chipmunk-tinta: 18 uncommitted files on round-2",
    "metronome: readout script cap/end date",
    # seed-style titles and the interrupted templates
    "orbit: sign the store agreement (unblocks build 141 + the review)",
    "boathouse: 91 uncommitted files",
    "home: follow up on the 09-12 budget call",
    "shop: Read.me first live send ~Oct 9",
    "shop: SPRING_CREDIT_30D pool Oct 7",
    "shop: promo codes revert/extend Nov 1",
    "shop: tooling audit waits on you",
    "boathouse: session 10-01 ended on usage limit",
    "chipmunk-tinta: session 10-02 ended on an open question",
    "x" * 100,
]


class TestSpecExamples(unittest.TestCase):
    def test_ok_titles_pass(self):
        for title in OK_TITLES:
            with self.subTest(title=title):
                self.assertEqual(problems(title), [])

    def test_spec_refusals(self):
        cases = {
            "home: pay the $480 hosting bill": MSG_MONEY,
            "home: discuss the $12k quote": MSG_MONEY,
            "home: reply to hello@example.com": MSG_EMAIL,
            "boathouse: revoke the sk-ant-... key": MSG_SECRET,
            "boathouse: rotate " + fake("sk-ant-api03-", 40): MSG_SECRET,
            "home: mail the form to 1600 Pennsylvania Avenue": MSG_ADDRESS,
        }
        for title, reason in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, problems(title))


class Classes(unittest.TestCase):
    """Positive and negative cases for every class."""

    def assertRefusedFor(self, reason: str, titles):
        for title in titles:
            with self.subTest(reason=reason, case=len(title)):
                self.assertIn(reason, problems(title))

    def assertPasses(self, titles):
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(problems(title), [])

    def test_blank_and_length(self):
        for blank in ("", " ", "\t ", None):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(problems(blank), [MSG_BLANK])
        self.assertEqual(problems("x" * 100), [])
        too_long = problems("x" * 101)
        self.assertEqual(len(too_long), 1)
        self.assertIn("101 characters", too_long[0])
        self.assertIn("100", too_long[0])
        self.assertEqual(problems(42), [sanitize.MSG_NOT_TEXT])

    def test_control_characters(self):
        self.assertRefusedFor(MSG_CONTROL, ["line one\nline two", "tab\tseparated", "bell\x07"])
        self.assertPasses(["plain words, punctuation: fine (really) ~ok"])

    def test_unicode_line_breaks_and_invisible_characters(self):
        self.assertRefusedFor(MSG_CONTROL, [
            "home: line one line two",       # LINE SEPARATOR
            "home: one two",                 # PARAGRAPH SEPARATOR
            "home: one\u0085two",                 # NEL (a C1 control)
            "home: escape \u009b31m",             # CSI (C1)
            "home: invoice ‮gnp.exe",        # RIGHT-TO-LEFT OVERRIDE
            "home: hidden​word",             # ZERO WIDTH SPACE
            "home: ﻿bom",                    # ZERO WIDTH NO-BREAK SPACE / BOM
        ])
        # An invisible character can't hide an address from the email check either.
        found = problems("home: mail bob​@example.com")
        self.assertIn(MSG_CONTROL, found)
        self.assertPasses(["metronome: café, guanábana and año notes",
                           "boathouse: ship it \U0001F389", "home: naïve façade"])

    def test_compatibility_forms_are_checked_like_plain_ones(self):
        self.assertRefusedFor(MSG_MONEY, ["home: pay ＄480", "home: pay ＄４８０"])
        self.assertRefusedFor(MSG_EMAIL, ["home: mail bob＠example．com"])
        self.assertRefusedFor(MSG_SECRET, ["key ｓｋ－abc"])  # fullwidth "sk-"
        self.assertPasses(["metronome: ４ blends ready"])  # a fullwidth digit alone is fine

    def test_non_ascii_email_domains(self):
        self.assertRefusedFor(MSG_EMAIL, ["home: mail bob@exämple.com",
                                          "home: mail jörg@example.de"])
        self.assertPasses(["shop: DM @inkwellshop about the reel", "shop: Read.me send"])

    def test_long_input_is_refused_quickly(self):
        import time
        for title in ("1" * 200000, "1," * 100000, "a." * 100000 + "@"):
            with self.subTest(size=len(title)):
                start = time.monotonic()
                found = problems(title)
                self.assertLess(time.monotonic() - start, 2.0)
                self.assertIn("characters long", found[0])
        # What falls inside the scanned head is still reported.
        found = problems("reply to hello@example.com " + "x" * 200)
        self.assertIn(MSG_EMAIL, found)

    def test_email(self):
        self.assertRefusedFor(MSG_EMAIL, [
            "send it to hello@example.com",
            "cc first.last+tag@mail.example.co.uk today",
            "a@b.io",
        ])
        self.assertPasses([
            "shop: DM @inkwellshop about the reel",
            "shop: Read.me first live send",
            "boathouse: ssh into the build box",
        ])

    def test_money(self):
        self.assertRefusedFor(MSG_MONEY, [
            "$480", "pay $480 to the vendor", "$12k raise", "$ 40 box",
            "€20 refund", "£5 fee", "480€ deposit", "20 £ fee",
            "480 usd wire", "480USD", "1,200 dollars", "1 dollar fee", "480 US dollars",
            "USD 480", "€ budget at 12k",
        ])
        self.assertPasses([
            "boathouse: 12k lines of logs",
            "boathouse: build 140",
            "boathouse: fix $HOME in the hook",
            "boathouse: walk 10k steps",
            "metronome: 4 pouches left",
        ])

    def test_secrets(self):
        self.assertRefusedFor(MSG_SECRET, [
            "rotate " + fake("sk-ant-api03-", 30),
            "key " + fake("sk-proj-", 20),
            "sk-short",
            "store " + fake("shp" + "at_", 8),
            "store " + fake("shp" + "ss_", 8),
            "token " + fake("gh" + "p_", 10),
            "token " + fake("gh" + "o_", 10),
            "token " + fake("gh" + "u_", 10),
            "token " + fake("gh" + "s_", 10),
            "token " + fake("gh" + "r_", 10),
            "token " + fake("github" + "_pat_", 12),
            "aws " + "AK" + "IA" + "QWERTYUIOPASDFGH",
            "slack " + fake("xo" + "xb-", 10),
            "slack " + fake("xo" + "xp-", 10),
            "google " + fake("AI" + "za", 35),
            "session " + JWT,
            "commit " + HEX40,
            "blob " + fake("", 32, "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe"),
            "env " + fake("GHOSTED_SERVICE_TOKEN_SECRET=", 20),
        ])
        self.assertPasses([
            "boathouse: task-runner fix",
            "metronome: risk-free rollback",
            "metronome: desk-check the copy",
            "boathouse: AKIA123 is too short",
            "boathouse: the 32 files",
            "boathouse: abcdef0123",
            "chipmunk-tinta: chipmunk-tinta-round-two-cleanup-pass",
            "boathouse: sketch the ghost_ page",
            "boathouse: UPPER lower 12345 separated by spaces",
        ])

    def test_lowercase_street_addresses(self):
        # Review finding spec-compliance-11: suffixes that are not ordinary words only, so plain
        # sentences ("on the way", "the road map") still pass. Mirrored in test/sanitize.test.js.
        self.assertRefusedFor(MSG_ADDRESS, [
            "home: ship to 123 main street", "home: 45 oak avenue", "home: drop it at 9 elm st",
            "home: 77 sunset blvd", "home: 12 old mill rd", "home: 3 pine ln", "home: 1 long pkwy",
            "home: 50 coast hwy", "١٢ main street",
        ])
        self.assertPasses([
            "shop: send 3 samples on the way", "home: fix 2 bugs in the road map", "shop: order 12 boxes",
            "x123 main street is a code name", "shop: 4 drive folders", "home: 2 lane changes",
        ])

    def test_street_addresses(self):
        self.assertRefusedFor(MSG_ADDRESS, [
            "1600 Pennsylvania Avenue",
            "mail it to 10 Downing Street",
            "123 N Main St",
            "350 5th Ave",
            "42 Elm Ct. by Friday",
            "7 Sunset Blvd",
            "88 Old Mill Rd",
        ])
        self.assertPasses([
            "boathouse: build 140 expires Oct 18",
            "chipmunk-tinta: 18 uncommitted files",
            "boathouse: fix 3 tests the right way",
            "metronome: 4 Blends ready",
        ])

    def test_phone_numbers(self):
        self.assertRefusedFor(MSG_PHONE, [
            "(312) 555-1234",
            "call +1 312-555-1234",
            "312.555.1234",
            "text 312 555 1234",
            "+1(312) 555-1234",
        ])
        self.assertPasses([
            "home: renew by 2026-10-05",
            "boathouse: build 140",
            "boathouse: GS-0012 follow-up",
            "metronome: due 10-11 14:00",
            "boathouse: 1234-567-890 is not a phone",
        ])


class TestCheck(unittest.TestCase):
    def test_check_returns_the_same_object(self):
        title = "home: pay the overdue water bill"
        self.assertIs(check(title), title)

    def test_check_raises_with_every_problem(self):
        with self.assertRaises(Refused) as ctx:
            check("pay $480 to hello@example.com")
        self.assertEqual(ctx.exception.problems, [MSG_EMAIL, MSG_MONEY])
        self.assertEqual(str(ctx.exception), MSG_EMAIL + "; " + MSG_MONEY)

    def test_refused_is_a_value_error(self):
        self.assertTrue(issubclass(Refused, ValueError))

    def test_messages_never_quote_the_input(self):
        secret = fake("sk-ant-api03-", 30)
        email = "hello@example.com"
        found = problems("rotate %s and tell %s about $480" % (secret, email))
        joined = " ".join(found)
        self.assertNotIn(secret, joined)
        self.assertNotIn(email, joined)
        self.assertNotIn("480", joined)
        for message in found:
            self.assertTrue(message[0].islower())  # plain phrases, e.g. "contains an email address"

    def test_never_edits(self):
        title = "  home: pay the overdue water bill  "
        self.assertEqual(problems(title), [])
        self.assertEqual(title, "  home: pay the overdue water bill  ")


class MaskTests(unittest.TestCase):
    """sanitize.mask (contract 16.2): masks, never refuses; problems()/check() are unchanged."""

    def test_secrets(self):
        cases = [
            fake("sk-ant-", 30), fake("sk-proj-", 24), fake("sk-", 20), fake("shpat_", 32), fake("shpss_", 32),
            fake("ghp_", 36), fake("gho_", 36), fake("github_pat_", 40), "AKIAIOSFODNN7EXAMPLE",  # AWS's own documented example key
            fake("xoxb-", 24), "AIza" + fake("", 35), JWT, HEX40, fake("", 40),
        ]
        for secret in cases:
            with self.subTest(prefix=secret[:4]):
                out = sanitize.mask("use %s now" % secret)
                self.assertEqual(out, "use [key] now")
                self.assertNotIn(secret[-8:], out)

    def test_plain_long_words_are_not_keys(self):
        for text in ("z" * 40, "internationalization_and_localization_x", "task-runner risk-free"):
            with self.subTest(text=text):
                self.assertEqual(sanitize.mask(text), text)

    def test_emails(self):
        self.assertEqual(sanitize.mask("mail bob.smith+x@example.co.uk today"), "mail [email] today")
        self.assertEqual(sanitize.mask("bob＠example.com"), "[email]")  # fullwidth at sign (NFKC)

    def test_addresses(self):
        self.assertEqual(sanitize.mask("ship to 123 Main St tomorrow"), "ship to [address] tomorrow")
        self.assertEqual(sanitize.mask("at 350 5th Ave"), "at [address]")
        self.assertEqual(sanitize.mask("at 9 N Elm Street"), "at [address]")
        self.assertEqual(sanitize.mask("ship to 123 main street"), "ship to [address]")
        self.assertEqual(sanitize.mask("send 3 samples on the way"), "send 3 samples on the way")

    def test_phones(self):
        for phone in ("512-555-0100", "(512) 555-0100", "+1 512.555.0100", "512 555 0100"):
            with self.subTest(phone=phone):
                self.assertEqual(sanitize.mask("call %s today" % phone), "call [phone] today")
        self.assertEqual(sanitize.mask("build 1234567890"), "build 1234567890")  # no separators: not a phone

    def test_money(self):
        cases = {
            "pay $480 now": "pay [amount] now",
            "pay $1,200.50 now": "pay [amount] now",
            "a $12k deal": "a [amount] deal",
            "costs € 20": "costs [amount]",
            "costs 480€": "costs [amount]",
            "costs 20 £": "costs [amount]",
            "pay 480 usd": "pay [amount]",
            "pay 40 dollars": "pay [amount]",
            "pay 1 dollar": "pay [amount]",
            "pay USD 30": "pay [amount]",
            "pay ＄480": "pay [amount]",  # fullwidth dollar (NFKC)
            "2 $HOME dirs": "2 $HOME dirs",
            "build 140 expires": "build 140 expires",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sanitize.mask(text), expected)

    def test_order_masks_each_span_once(self):
        # A key holding a phone-shaped run is one [key], never [phone].
        key = "sk-" + "512-555-0100-" + fake("", 12)
        self.assertEqual(sanitize.mask("key %s end" % key), "key [key] end")
        self.assertEqual(sanitize.mask(fake("", 20) + "5125550100" + fake("", 12)), "[key]")
        # An email holding digits is [email], not [phone].
        self.assertEqual(sanitize.mask("512.555.0100@example.com"), "[email]")
        # Placeholders are never re-masked.
        out = sanitize.mask("%s bob@example.com 123 Main St 512-555-0100 $480" % HEX40)
        self.assertEqual(out, "[key] [email] [address] [phone] [amount]")

    def test_nfkc_and_controls(self):
        self.assertEqual(sanitize.mask("a​b\nc\td e\x00f‮g"), "a b c d e f g")
        self.assertEqual(sanitize.mask("ＡＢＣ"), "ABC")  # fullwidth letters fold
        # a zero-width character cannot hide an amount or a phone from the patterns
        self.assertEqual(sanitize.mask("pay $​480"), "pay [amount]")
        self.assertEqual(sanitize.mask("call 512​555​0100"), "call [phone]")

    def test_key_split_by_a_format_character_is_masked_whole(self):
        # A zero-width space or soft hyphen inside a key used to become a space, so only the
        # first piece was masked and the rest (under 32 chars) printed in clear.
        tail = "EfGh12345678AbCdEfGh1234"
        for sep in ("​", "­", "⁠", "﻿"):
            with self.subTest(sep="U+%04X" % ord(sep)):
                key = "sk-ant-api03-" + "x" * 20 + sep + tail  # x-filled: a sample that cannot be real
                out = sanitize.mask("Should I use %s?" % key)
                self.assertEqual(out, "Should I use [key]?")
                self.assertNotIn("EfGh1234", out)
                self.assertEqual(sanitize.mask("ghp_" + "x" * 8 + sep + "EfGh5678IjKl9012MnOp now"), "[key] now")
                self.assertEqual(sanitize.mask("mail bob" + sep + "@example.com"), "mail [email]")

    def test_spacing_accent_cannot_split_a_token(self):
        # U+00B4 / U+00A8 / U+00B8 fold (NFKC) to a space plus a combining mark.
        for accent in ("´", "¨", "¸"):
            with self.subTest(accent="U+%04X" % ord(accent)):
                out = sanitize.mask("x AbCdEfGh12345678" + accent + "AbCdEfGh12345678AbCd y")
                self.assertEqual(out, "x [key] y")

    def test_non_str(self):
        for value in (None, 42, b"bytes", ["a"], {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(sanitize.mask(value), "")
        self.assertEqual(sanitize.mask(""), "")

    def test_does_not_change_problems(self):
        email = "bob@example.com"
        self.assertEqual(problems("ping " + email), [MSG_EMAIL])
        self.assertEqual(sanitize.mask("ping " + email), "ping [email]")
        with self.assertRaises(Refused):
            check("ping " + email)
        self.assertEqual(problems("ping [email]"), [])

    def test_long_input_is_fast_and_capped(self):
        import time
        hostile = ["a." * 30000, "a-" * 30000, "1 " * 30000, "_" * 60000, "a@" * 30000, "1 Aa " * 12000,
                   ("x" * 31 + " ") * 2000, "$1," * 20000]
        for text in hostile:
            with self.subTest(head=text[:6]):
                t0 = time.perf_counter()
                out = sanitize.mask(text)
                self.assertLess(time.perf_counter() - t0, 0.5)
                self.assertLessEqual(len(out), 4 * sanitize.MASK_SCAN_LIMIT)  # placeholders may be longer
        self.assertEqual(len(sanitize.mask("x " * 10000)), sanitize.MASK_SCAN_LIMIT)


if __name__ == "__main__":
    unittest.main()
