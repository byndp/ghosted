"""Push rules: core.schedule.decide against the shared vectors, plus the Python-side checks.

The vectors in spec/push-rules-vectors.json are shared with the Worker's rules.js. Every vector
whose engines include "py" runs here exactly as given, and again with the dead-man forced on
(shared vectors must not depend on it, so an engine that always runs the dead-man agrees). The
dead-man vectors are JS-only by contract; they also run here through Python's own dead-man path
so both implementations are held to the same expectations.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import schedule

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ROOT = Path(__file__).resolve().parent.parent
VECTORS_PATH = ROOT / "spec" / "push-rules-vectors.json"
SOURCE_PATH = ROOT / "core" / "schedule.py"

TZ = "America/Chicago"
CFG = {"digest_hour": 9, "quiet_start": 22, "quiet_end": 8, "daily_budget": 3,
       "deadman_hours": 3, "enable_deadman": False}
CFG_DM = dict(CFG, enable_deadman=True)
SEND_KEYS = {"kind", "dedup_key", "item_ids", "title", "body", "local_date", "priority"}
EXPECT_SEND_KEYS = {"kind", "dedup_key", "item_ids"}
VECTOR_KEYS = {"name", "why", "now", "tz", "cfg", "engines", "items", "notifications", "runs", "expect"}
INTERRUPTED_TITLE = re.compile(r"^[^:]+: session \d{2}-\d{2} ended on (usage limit|an open question)$")

# Scenarios the build task requires; renaming one must be deliberate.
REQUIRED_VECTORS = {
    "no-triggers-no-digest", "due-today-T1", "overdue-T2", "due-in-2-days-T3-and-3-days-excluded",
    "due-in-3-days-no-trigger", "new-interruption-since-last-digest-T4",
    "older-interruption-only-no-digest", "first-ever-digest-any-open-interruption-T4",
    "reopened-snooze-at-0915-joins-the-digest-T5", "review-at-today-T6", "review-at-plus-14-T6",
    "digest-too-early-0815", "digest-window-closed-1215", "digest-catch-up-1015",
    "second-digest-same-day-dedup", "budget-exhausted-three-today", "quiet-hours-2230-reopen-only",
    "quiet-hours-0730-reopen-only", "advance-at-due-minus-24h", "advance-before-the-window",
    "advance-after-due", "advance-already-sent", "date-only-items-never-get-advance-pushes",
    "ordering-T2-T1-T3-T4-T5-T6-then-by-due", "digest-body-cut-on-a-separator",
    "deadman-fires-1115-last-ok-0803", "deadman-morning-grace-0815-last-ok-2302",
    "deadman-fires-0915-last-ok-2302-before-the-digest", "deadman-does-not-repeat-with-the-same-key",
    "deadman-re-arms-after-a-new-ok-run", "deadman-failed-runs-are-not-heartbeats",
    "deadman-never-reported-uses-key-none", "deadman-suppressed-in-quiet-hours",
    "dst-cst-2026-11-02-1515z-is-0915", "dst-cdt-2026-10-30-1415z-is-0915",
    # Pinned by the verification pass: T4 against delivered digests, T6 on dated items, body length
    # in code points, and the DST gap/fold mapping the JS port must reproduce.
    "t4-measures-from-the-latest-delivered-digest", "t4-no-delivered-digest-counts-as-never",
    "review-at-on-a-dated-item-T6", "digest-body-counts-code-points-not-utf16-units",
    "dst-gap-snooze-maps-on-the-pre-transition-offset-not-yet", "dst-gap-snooze-reopens-at-0830z",
    "dst-fall-back-snooze-uses-the-earlier-instant",
    # Contract 12 after stage 1: T4 skips looks_resolved items, no double push for one item, and
    # snooze expiry keeps snooze_until (so T5's second branch occurs in real data).
    "t4-skips-interruptions-that-look-resolved", "t4-first-ever-digest-skips-interruptions-that-look-resolved",
    "digest-then-advance-in-the-same-tick", "advance-skipped-after-a-delivered-digest-carried-it",
    "advance-skip-counts-a-digest-sent-exactly-at-due-minus-24h",
    "advance-fires-when-the-digest-that-carried-it-failed",
    "advance-fires-when-the-delivered-digest-did-not-carry-it", "advance-stays-skipped-the-next-morning",
    "advance-skip-reads-item-ids-stored-as-json-text",
    "reopened-at-the-0730-quiet-tick-drives-the-0915-digest-T5",
    "cron-reopened-before-decide-T5-both-branches-sort-by-due",
}


def load_vectors():
    with VECTORS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)["vectors"]


def run_vector(vector, **cfg_overrides):
    cfg = dict(vector["cfg"], **cfg_overrides)
    return schedule.decide(vector["now"], vector["items"], vector["notifications"],
                           vector["runs"], vector["tz"], cfg)


def project(result):
    return {"reopen": result["reopen"],
            "send": [{key: push[key] for key in ("kind", "dedup_key", "item_ids")}
                     for push in result["send"]]}


def item(item_id, title="home: water the plants", **fields):
    row = {"id": item_id, "kind": "promise", "project": title.split(":", 1)[0], "title": title,
           "owner": "me", "status": "open", "due_at": None, "review_at": None,
           "snooze_until": None, "created_at": "2026-10-01T15:00:00Z"}
    row.update(fields)
    return row


def note(note_id, sent_at, kind, key, item_ids, local_date, ok=1):
    row = {"id": note_id, "sent_at": sent_at, "kind": kind, "dedup_key": key,
           "item_ids": item_ids, "ok": ok, "local_date": local_date}
    return {k: v for k, v in row.items() if v is not None}


def run(run_id, finished_at, ok=1):
    return {"run_id": run_id, "started_at": finished_at, "finished_at": finished_at, "ok": ok,
            "sessions_scanned": 1, "items_added": 0, "error": None}


def chicago(year, month, day, hour, minute=15):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(TZ))


YESTERDAY_DIGEST = note(1, "2026-10-05T14:15:30Z", "digest", "digest:2026-10-05", ["GS-0090"], "2026-10-05")
FRESH_RUN = run("run-hb", "2026-10-06T14:03:00Z")


def busy_tick():
    """09:15 Tue: a reopen, a stale Mac, a digest, and an advance the digest absorbs, in one tick."""
    items = [
        item("GS-0001", "shop: decide on the footer layout", kind="decision", status="snoozed",
             snooze_until="2026-10-06T09:00", review_at="2026-10-12"),
        item("GS-0002", "orbit: renew the license", due_at="2026-10-07T09:00"),
        item("GS-0003", "orbit: session 10-05 ended on usage limit", kind="interrupted",
             review_at="2026-10-12", created_at="2026-10-05T20:40:00Z"),
        item("GS-0004", "home: water the plants", status="done", due_at="2026-10-01"),
    ]
    notifications = [YESTERDAY_DIGEST]
    runs = [run("run-2302", "2026-10-06T04:02:00Z"), run("run-0003", "2026-10-06T08:03:00Z", ok=0)]
    return items, notifications, runs


class VectorFileTests(unittest.TestCase):
    """The shared file itself: shape, coverage, and what each engine may rely on."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = load_vectors()

    def test_format(self):
        with VECTORS_PATH.open(encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertIsInstance(doc.get("vectors"), list)
        for vector in self.vectors:
            with self.subTest(vector=vector.get("name")):
                self.assertTrue(VECTOR_KEYS <= set(vector), sorted(VECTOR_KEYS - set(vector)))
                self.assertTrue(set(vector) <= VECTOR_KEYS | {"expect_bodies"})
                self.assertEqual(vector["tz"], TZ)
                self.assertEqual(set(vector["cfg"]), set(CFG))
                self.assertTrue(vector["engines"])
                self.assertTrue(set(vector["engines"]) <= {"py", "js"})
                self.assertEqual(set(vector["expect"]), {"reopen", "send"})
                self.assertEqual(vector["expect"]["reopen"], sorted(vector["expect"]["reopen"]))
                for push in vector["expect"]["send"]:
                    self.assertEqual(set(push), EXPECT_SEND_KEYS)
                if "expect_bodies" in vector:
                    self.assertEqual(len(vector["expect_bodies"]), len(vector["expect"]["send"]))
                    for body in vector["expect_bodies"]:
                        self.assertLessEqual(len(body), schedule.BODY_LIMIT)

    def test_count_and_unique_names(self):
        names = [vector["name"] for vector in self.vectors]
        self.assertGreaterEqual(len(names), 30)
        self.assertEqual(len(names), len(set(names)))

    def test_required_scenarios_present(self):
        names = {vector["name"] for vector in self.vectors}
        self.assertEqual(REQUIRED_VECTORS - names, set())

    def test_js_only_vectors_are_exactly_the_deadman_vectors(self):
        for vector in self.vectors:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(vector["engines"] == ["js"], vector["cfg"]["enable_deadman"] is True)
                if vector["engines"] != ["js"]:
                    self.assertEqual(vector["engines"], ["py", "js"])
                    kinds = {push["kind"] for push in vector["expect"]["send"]}
                    self.assertNotIn("deadman", kinds)

    def test_vector_items_are_valid_public_items(self):
        for vector in self.vectors:
            for row in vector["items"]:
                with self.subTest(vector=vector["name"], item=row["id"]):
                    self.assertRegex(row["id"], r"^GS-\d{4}$")
                    self.assertTrue(1 <= len(row["title"]) <= 100)
                    self.assertNotIn("local_title", row)
                    self.assertIn(row["status"], ("open", "done", "snoozed", "dismissed"))
                    if row["kind"] == "interrupted":
                        self.assertRegex(row["title"], INTERRUPTED_TITLE)

    def test_vector_titles_pass_the_sanitizer(self):
        # Vector titles stand for uploaded public titles, so they must be ones the Mac would accept.
        from core import sanitize  # contract section 9 interface
        for vector in self.vectors:
            for row in vector["items"]:
                with self.subTest(vector=vector["name"], item=row["id"]):
                    self.assertEqual(sanitize.problems(row["title"]), [])


class SharedVectorTests(unittest.TestCase):
    """Every vector Python runs, exactly as the JS suite runs it."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = [vector for vector in load_vectors() if "py" in vector["engines"]]

    def check(self, vector, result):
        self.assertEqual(project(result), vector["expect"], vector["why"])
        if "expect_bodies" in vector:
            self.assertEqual([push["body"] for push in result["send"]], vector["expect_bodies"])

    def test_shared_vectors(self):
        self.assertTrue(self.vectors)
        for vector in self.vectors:
            with self.subTest(vector=vector["name"]):
                self.check(vector, run_vector(vector))

    def test_shared_vectors_hold_with_the_deadman_enabled(self):
        # The Worker always runs the dead-man; shared expectations must not depend on it.
        for vector in self.vectors:
            with self.subTest(vector=vector["name"]):
                self.check(vector, run_vector(vector, enable_deadman=True))

    def test_every_push_has_the_full_shape(self):
        for vector in self.vectors:
            result = run_vector(vector)
            now = datetime.fromisoformat(vector["now"].replace("Z", "+00:00"))
            local_date = now.astimezone(ZoneInfo(TZ)).date().isoformat()
            for push in result["send"]:
                with self.subTest(vector=vector["name"], key=push["dedup_key"]):
                    self.assertEqual(set(push), SEND_KEYS)
                    self.assertEqual(push["local_date"], local_date)
                    self.assertEqual(push["priority"], "normal")
                    self.assertTrue(push["body"])
                    if push["kind"] == "digest":
                        self.assertEqual(push["title"], schedule.DIGEST_TITLE)
                        self.assertTrue(push["body"].startswith("%d open: " % len(push["item_ids"])))
                    else:
                        self.assertEqual(push["kind"], "advance")
                        self.assertEqual(push["title"], "Due in 24 h")
                        titles = {row["id"]: row["title"] for row in vector["items"]}
                        self.assertEqual(push["body"], titles[push["item_ids"][0]])


class DeadmanTests(unittest.TestCase):
    """Python's dead-man path, so the reference and the Worker port agree."""

    def test_js_only_vectors_agree_in_python(self):
        vectors = [vector for vector in load_vectors() if vector["engines"] == ["js"]]
        self.assertGreaterEqual(len(vectors), 8)
        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                self.assertTrue(vector["cfg"]["enable_deadman"])
                self.assertEqual(project(run_vector(vector)), vector["expect"], vector["why"])

    def test_off_unless_enabled(self):
        runs = [run("run-0803", "2026-10-06T13:03:00Z")]
        now = "2026-10-06T16:15:00Z"
        for cfg in (None, CFG, dict(CFG, enable_deadman="false"), dict(CFG, enable_deadman=0)):
            with self.subTest(cfg=cfg):
                self.assertEqual(schedule.decide(now, [], [], runs, TZ, cfg)["send"], [])
        for cfg in (CFG_DM, dict(CFG, enable_deadman="true"), dict(CFG, enable_deadman=1)):
            with self.subTest(cfg=cfg):
                sends = schedule.decide(now, [], [], runs, TZ, cfg)["send"]
                self.assertEqual([push["dedup_key"] for push in sends], ["deadman:run-0803"])

    def test_push_shape(self):
        runs = [run("run-0703", "2026-10-06T12:03:00Z"), run("run-0803", "2026-10-06T13:03:00Z")]
        sends = schedule.decide("2026-10-06T16:15:00Z", [], [], runs, TZ, CFG_DM)["send"]
        self.assertEqual(sends, [{
            "kind": "deadman", "dedup_key": "deadman:run-0803", "item_ids": [],
            "title": schedule.DEADMAN_TITLE, "body": "No successful run since 8:03 AM",
            "local_date": "2026-10-06", "priority": "high",
        }])

    def test_body_names_the_day_when_the_last_run_was_not_today(self):
        runs = [run("run-2302", "2026-10-06T04:02:00Z")]  # Mon 23:02 CDT
        push = schedule.decide("2026-10-06T14:15:00Z", [], [], runs, TZ, CFG_DM)["send"][0]
        self.assertEqual(push["body"], "No successful run since Mon Oct 5, 11:02 PM")

    def test_never_reported(self):
        push = schedule.decide("2026-10-06T14:15:00Z", [], [], [], TZ, CFG_DM)["send"][0]
        self.assertEqual((push["dedup_key"], push["body"], push["priority"]),
                         ("deadman:none", "No successful run reported yet", "high"))

    def test_ok_true_counts_and_other_values_do_not(self):
        now = "2026-10-06T14:15:00Z"
        fresh = "2026-10-06T14:03:00Z"
        for ok in (1, True, 1.0):
            with self.subTest(ok=ok):
                self.assertEqual(schedule.decide(now, [], [], [run("r", fresh, ok=ok)], TZ, CFG_DM)["send"], [])
        for ok in (0, False, "1", None, 2):
            with self.subTest(ok=ok):
                sends = schedule.decide(now, [], [], [run("r", fresh, ok=ok)], TZ, CFG_DM)["send"]
                self.assertEqual([push["dedup_key"] for push in sends], ["deadman:none"])

    def test_deadman_hours_is_configurable(self):
        runs = [run("run-0803", "2026-10-06T13:03:00Z")]
        now = "2026-10-06T14:15:00Z"  # 1h12m later
        self.assertEqual(schedule.decide(now, [], [], runs, TZ, CFG_DM)["send"], [])
        sends = schedule.decide(now, [], [], runs, TZ, dict(CFG_DM, deadman_hours=1))["send"]
        self.assertEqual([push["dedup_key"] for push in sends], ["deadman:run-0803"])

    def test_window_edges(self):
        expected = {(8, 59): False, (9, 0): True, (21, 59): True, (22, 0): False}
        for (hour, minute), fires in expected.items():
            with self.subTest(local="%02d:%02d" % (hour, minute)):
                now = chicago(2026, 10, 6, hour, minute)
                sends = schedule.decide(now, [], [], [], TZ, CFG_DM)["send"]
                self.assertEqual(bool(sends), fires)

    def test_busy_tick_order_and_budget(self):
        items, notifications, runs = busy_tick()
        result = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, CFG_DM)
        self.assertEqual(result["reopen"], ["GS-0001"])
        # GS-0002's advance window is open (due tomorrow 09:00), but the digest carries it: no
        # double push, so the tick sends the dead-man and the digest only.
        self.assertEqual([(push["kind"], push["dedup_key"], push["item_ids"]) for push in result["send"]], [
            ("deadman", "deadman:run-2302", []),
            ("digest", "digest:2026-10-06", ["GS-0002", "GS-0003", "GS-0001"]),
        ])
        self.assertEqual(result["send"][1]["body"],
                         "3 open: orbit: renew the license · orbit: session 10-05 ended on usage limit"
                         " · shop: decide on the footer layout")
        tight = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ,
                                dict(CFG_DM, daily_budget=1))
        self.assertEqual([push["kind"] for push in tight["send"]], ["deadman"])
        # An hour later the digest row exists and carried GS-0002 after due - 24h: still no advance.
        delivered = note(2, "2026-10-06T14:15:20Z", "digest", "digest:2026-10-06",
                         ["GS-0002", "GS-0003", "GS-0001"], "2026-10-06")
        later = schedule.decide("2026-10-06T15:15:00Z", items, notifications + [delivered], runs, TZ, CFG)
        self.assertEqual(later["send"], [])


class BodyTests(unittest.TestCase):
    def test_all_titles_when_they_fit(self):
        self.assertEqual(schedule.digest_body(["home: a", "shop: b"]), "2 open: home: a · shop: b")
        self.assertEqual(schedule.digest_body(["orbit: c"]), "1 open: orbit: c")

    def test_exactly_at_the_limit_is_not_cut(self):
        body = schedule.digest_body(["x" * 100, "y" * 67])
        self.assertEqual(len(body), 178)
        self.assertEqual(body, "2 open: " + "x" * 100 + " · " + "y" * 67)

    def test_one_over_the_limit_is_cut_on_a_separator(self):
        body = schedule.digest_body(["x" * 100, "y" * 68])
        self.assertEqual(body, "2 open: " + "x" * 100 + " · …")

    def test_cut_keeps_whole_titles_and_counts_every_item(self):
        titles = ["t%02d: %s" % (n, "w" * 30) for n in range(12)]
        body = schedule.digest_body(titles)
        self.assertLessEqual(len(body), 178)
        self.assertTrue(body.startswith("12 open: "))
        self.assertTrue(body.endswith(" · …"))
        shown = body[len("12 open: "):-len(" · …")].split(" · ")
        self.assertEqual(shown, titles[:len(shown)])
        self.assertEqual(len(shown), 4)  # 35-char titles: 9 + 4*35 + 3*3 + 4 = 162; five would be 200

    def test_the_cut_mark_counts_toward_the_limit(self):
        # Two titles are 176 chars without the mark and 180 with it: only one fits.
        body = schedule.digest_body(["a" * 100, "b" * 65, "c" * 10])
        self.assertEqual(body, "3 open: " + "a" * 100 + " · …")

    def test_overlong_first_title_is_hard_cut(self):
        body = schedule.digest_body(["z" * 300, "y"])
        self.assertEqual(len(body), 178)
        self.assertEqual(body, "2 open: " + "z" * 169 + "…")

    def test_length_is_counted_in_code_points(self):
        # U+1F382 is one code point and two UTF-16 units; the limit counts code points.
        cake = "\U0001F382"
        body = schedule.digest_body([cake + "a" * 99, "b" * 67])
        self.assertEqual(len(body), 178)
        self.assertEqual(body, "2 open: " + cake + "a" * 99 + " · " + "b" * 67)
        self.assertEqual(len(body.encode("utf-16-le")) // 2, 179)
        cut = schedule.digest_body([cake * 300])
        self.assertEqual(cut, "1 open: " + cake * 169 + "…")  # the hard cut never splits a pair

    def test_custom_limit(self):
        self.assertEqual(schedule.digest_body(["abc", "def"], limit=17), "2 open: abc · def")
        self.assertEqual(schedule.digest_body(["abc", "def", "ghi"], limit=17), "3 open: abc · …")

    def test_digest_uses_public_titles_with_their_project_prefix(self):
        rows = [item("GS-0001", "home: return the library book", due_at="2026-10-03",
                     local_title="LOCAL ONLY never uploaded"),
                item("GS-0002", "shop: fix the checkout button", due_at="2026-10-06"),
                item("GS-0003", "", due_at="2026-10-06")]  # a blank title falls back to the id
        push = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)["send"][0]
        self.assertEqual(push["body"], "3 open: home: return the library book · shop: fix the checkout button · GS-0003")
        self.assertNotIn("LOCAL", push["body"])


class PurityTests(unittest.TestCase):
    def test_inputs_are_not_mutated(self):
        items, notifications, runs = busy_tick()
        cfg = dict(CFG_DM)
        before = copy.deepcopy((items, notifications, runs, cfg))
        result = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, cfg)
        self.assertEqual(len(result["send"]), 2)
        self.assertEqual((items, notifications, runs, cfg), before)

    def test_results_do_not_alias_inputs(self):
        items, notifications, runs = busy_tick()
        before = copy.deepcopy((items, notifications, runs))
        result = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, CFG_DM)
        for push in result["send"]:
            push["item_ids"].append("GS-9999")
            push["title"] = "changed"
        result["reopen"].append("GS-9999")
        self.assertEqual((items, notifications, runs), before)

    def test_vectors_are_not_mutated(self):
        for vector in load_vectors():
            with self.subTest(vector=vector["name"]):
                before = copy.deepcopy(vector)
                run_vector(vector, enable_deadman=True)
                self.assertEqual(vector, before)

    def test_deterministic_and_independent_of_row_order(self):
        items, notifications, runs = busy_tick()
        first = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, CFG_DM)
        again = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, CFG_DM)
        flipped = schedule.decide("2026-10-06T14:15:00Z", items[::-1], notifications[::-1], runs[::-1], TZ, CFG_DM)
        self.assertEqual(first, again)
        self.assertEqual(first, flipped)


class InputFormTests(unittest.TestCase):
    def test_now_forms_agree(self):
        items, notifications, runs = busy_tick()
        forms = [
            "2026-10-06T14:15:00Z", "2026-10-06T09:15:00-05:00", "2026-10-06T14:15:00.000+00:00",
            "2026-10-06T14:15:00.000000Z", "2026-10-06 14:15:00Z", "2026-10-06T14:15:00",
            datetime(2026, 10, 6, 14, 15, tzinfo=timezone.utc), chicago(2026, 10, 6, 9),
            datetime(2026, 10, 6, 14, 15),  # naive = UTC
        ]
        results = [schedule.decide(now, items, notifications, runs, TZ, CFG_DM) for now in forms]
        for now, result in zip(forms, results):
            with self.subTest(now=now):
                self.assertEqual(result, results[0])
        self.assertEqual(len(results[0]["send"]), 2)

    def test_tz_object_and_name_agree(self):
        items, notifications, runs = busy_tick()
        by_name = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, CFG_DM)
        by_zone = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, ZoneInfo(TZ), CFG_DM)
        self.assertEqual(by_name, by_zone)

    def test_utc_timezone(self):
        rows = [item("GS-0001", due_at="2026-10-06")]
        for tz in ("UTC", timezone.utc):
            with self.subTest(tz=tz):
                result = schedule.decide("2026-10-06T09:15:00Z", rows, [], [], tz, CFG)
                self.assertEqual([push["dedup_key"] for push in result["send"]], ["digest:2026-10-06"])

    def test_cfg_defaults_and_attribute_objects(self):
        self.assertEqual(schedule.DEFAULTS, CFG)
        items, notifications, runs = busy_tick()
        now = "2026-10-06T14:15:00Z"
        expected = schedule.decide(now, items, notifications, runs, TZ, CFG)
        # A config object without the dead-man keys (like core.config.Config) takes the defaults.
        config_like = types.SimpleNamespace(digest_hour=9, quiet_start=22, quiet_end=8, daily_budget=3)
        for cfg in (None, {}, config_like, {"digest_hour": None}):
            with self.subTest(cfg=cfg):
                self.assertEqual(schedule.decide(now, items, notifications, runs, TZ, cfg), expected)
        later = schedule.decide(now, items, notifications, runs, TZ, dict(CFG, digest_hour=10))
        self.assertNotIn("digest", [push["kind"] for push in later["send"]])

    def test_items_as_an_id_mapping(self):
        items, notifications, runs = busy_tick()
        as_list = schedule.decide("2026-10-06T14:15:00Z", items, notifications, runs, TZ, CFG_DM)
        as_map = schedule.decide("2026-10-06T14:15:00Z", {row["id"]: row for row in items},
                                 notifications, runs, TZ, CFG_DM)
        self.assertEqual(as_list, as_map)

    def test_bad_now_or_tz_raise(self):
        for now in ("not a time", "2026-13-01T00:00:00Z", None, 1759760100):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    schedule.decide(now, [], [], [], TZ, CFG)
        for tz in ("Mars/Olympus_Mons", "", None, 5):
            with self.subTest(tz=tz):
                with self.assertRaises(ValueError):
                    schedule.decide("2026-10-06T14:15:00Z", [], [], [], tz, CFG)

    def test_unparseable_fields_are_ignored(self):
        rows = [
            item("GS-0001", due_at="soon"),  # an unreadable due is no due trigger
            item("GS-0002", status="snoozed", snooze_until="later"),  # never reopens
            item("GS-0003", "orbit: session 10-05 ended on usage limit", kind="interrupted",
                 created_at="yesterday", review_at="2026-10-12"),  # no T4 against a real digest
            item("GS-0004", kind="decision", review_at="next week"),
            item("GS-0005", due_at="2026-02-30"),
            item("GS-0006", due_at="2026-10-06T25:00"),
        ]
        notifications = [YESTERDAY_DIGEST, {"kind": "advance", "sent_at": "garbage", "item_ids": []}]
        result = schedule.decide("2026-10-06T14:15:00Z", rows, notifications, [FRESH_RUN], TZ, CFG)
        self.assertEqual(result, {"reopen": [], "send": []})
        # An unreadable due hides nothing else: a review date of today still counts (T6).
        rows.append(item("GS-0007", due_at="soon", review_at="2026-10-06"))
        result = schedule.decide("2026-10-06T14:15:00Z", rows, notifications, [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0007"]])

    def test_out_of_range_dates_are_ignored_not_fatal(self):
        # Dates at the edge of datetime's range overflow when shifted between zones (ghosted add takes
        # any 4-digit year). One such value must not raise and stop every other push.
        rows = [
            item("GS-0001", due_at="9999-12-31T23:00"),
            item("GS-0002", status="snoozed", snooze_until="9999-12-31T23:30"),
            item("GS-0003", due_at="2026-10-06"),
        ]
        notifications = [YESTERDAY_DIGEST,
                         {"kind": "advance", "dedup_key": "advance:GS-0091", "sent_at": "0001-01-01T00:00:00Z"},
                         {"kind": "digest", "dedup_key": "digest:x", "sent_at": "9999-12-31T23:59:59-05:00", "ok": 1}]
        result = schedule.decide("2026-10-06T14:15:00Z", rows, notifications, [FRESH_RUN], TZ, CFG_DM)
        self.assertEqual(result["reopen"], [])
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0003"]])
        # The dead-man body falls back to the UTC instant when the local time is out of range.
        stale = schedule.decide("2026-10-06T16:15:00Z", [], [], [run("run-old", "0001-01-01T00:00:00Z")],
                                TZ, CFG_DM)
        self.assertEqual([(push["dedup_key"], push["body"]) for push in stale["send"]],
                         [("deadman:run-old", "No successful run since 0001-01-01T00:00:00Z")])
        # East of UTC the overflow is an underflow at year 1.
        east = schedule.decide("2026-10-06T00:15:00Z", [item("GS-0001", due_at="0001-01-01T03:00"),
                                                        item("GS-0002", due_at="2026-10-06")],
                               [], [], ZoneInfo("Asia/Tokyo"), CFG)
        self.assertEqual([push["item_ids"] for push in east["send"]], [["GS-0002"]])
        for now in ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    schedule.decide(now, [], [], [], TZ, CFG)

    def test_duplicate_ids_use_the_first_row(self):
        open_row = item("GS-0001", due_at="2026-10-06")
        done_row = item("GS-0001", status="done", due_at="2026-10-06")
        now = "2026-10-06T14:15:00Z"
        first_open = schedule.decide(now, [open_row, done_row], [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        first_done = schedule.decide(now, [done_row, open_row], [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["item_ids"] for push in first_open["send"]], [["GS-0001"]])
        self.assertEqual(first_done["send"], [])

    def test_rows_that_are_not_dicts_or_lack_an_id_are_skipped(self):
        rows = [None, "GS-0009", 7, {"title": "no id", "due_at": "2026-10-06", "status": "open"},
                item("GS-0001", due_at="2026-10-06")]
        result = schedule.decide("2026-10-06T14:15:00Z", rows, [None, YESTERDAY_DIGEST], [None, FRESH_RUN], TZ, CFG)
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0001"]])


class NotificationRowTests(unittest.TestCase):
    def test_rows_without_a_dedup_key_still_dedup(self):
        rows = [item("GS-0001", due_at="2026-10-06"), item("GS-0002", due_at="2026-10-07T09:00")]
        control = schedule.decide("2026-10-06T15:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        # The 10:15 catch-up digest carries GS-0002 (T3), which absorbs its advance.
        self.assertEqual([push["dedup_key"] for push in control["send"]], ["digest:2026-10-06"])
        digest_row = {"id": 2, "sent_at": "2026-10-06T14:15:20Z", "kind": "digest",
                      "item_ids": ["GS-0001"], "ok": 1, "local_date": "2026-10-06"}
        advance_row = {"id": 3, "sent_at": "2026-10-06T14:15:21Z", "kind": "advance",
                       "item_ids": ["GS-0002"], "ok": 1, "local_date": "2026-10-06"}
        # A keyless digest row still dedups today's digest; it did not carry GS-0002, so the advance is live.
        live = schedule.decide("2026-10-06T15:15:00Z", rows, [YESTERDAY_DIGEST, digest_row], [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["dedup_key"] for push in live["send"]], ["advance:GS-0002"])
        result = schedule.decide("2026-10-06T15:15:00Z", rows, [YESTERDAY_DIGEST, digest_row, advance_row],
                                 [FRESH_RUN], TZ, CFG)
        self.assertEqual(result["send"], [])

    def test_rows_without_local_date_count_by_local_sent_at(self):
        rows = [item("GS-0001", due_at="2026-10-06")]
        today = [{"id": n, "sent_at": "2026-10-06T13:15:%02dZ" % n, "kind": "advance",
                  "dedup_key": "advance:GS-00%02d" % (n + 10), "item_ids": [], "ok": 1} for n in range(3)]
        blocked = schedule.decide("2026-10-06T15:15:00Z", rows, [YESTERDAY_DIGEST] + today, [FRESH_RUN], TZ, CFG)
        self.assertEqual(blocked["send"], [])
        # 02:15Z on 10-06 is 21:15 on 10-05 in Chicago, so these rows belong to yesterday.
        late = [dict(row, sent_at="2026-10-06T02:15:%02dZ" % n) for n, row in enumerate(today)]
        sent = schedule.decide("2026-10-06T15:15:00Z", rows, [YESTERDAY_DIGEST] + late, [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["kind"] for push in sent["send"]], ["digest"])


class QuietHoursTests(unittest.TestCase):
    ROWS = [item("GS-0001", "orbit: renew the license", due_at="2026-10-07T00:00"),  # window spans 10-06
            item("GS-0002", status="snoozed", snooze_until="2026-10-06T00:00", review_at="2026-10-20")]

    def sends_by_hour(self, cfg):
        """Whether each hour sends anything: the advance, or in the digest window the digest that
        carries GS-0001 and so absorbs its advance."""
        out = {}
        for hour in range(24):
            result = schedule.decide(chicago(2026, 10, 6, hour), self.ROWS, [], [], TZ, cfg)
            self.assertEqual(result["reopen"], ["GS-0002"])  # reopen happens at every hour
            keys = [push["dedup_key"] for push in result["send"]]
            in_window = cfg["digest_hour"] <= hour < cfg["digest_hour"] + 3
            self.assertIn(keys, ([], ["digest:2026-10-06"] if in_window else ["advance:GS-0001"]))
            out[hour] = bool(keys)
        return out

    def test_default_quiet_hours(self):
        sent = self.sends_by_hour(CFG)
        self.assertEqual([hour for hour, fired in sent.items() if not fired], [0, 1, 2, 3, 4, 5, 6, 7, 22, 23])

    def test_same_day_quiet_window(self):
        sent = self.sends_by_hour(dict(CFG, quiet_start=1, quiet_end=6))
        self.assertEqual([hour for hour, fired in sent.items() if not fired], [1, 2, 3, 4, 5])

    def test_equal_bounds_mean_no_quiet_hours(self):
        sent = self.sends_by_hour(dict(CFG, quiet_start=0, quiet_end=0))
        self.assertTrue(all(sent.values()))


class TriggerTests(unittest.TestCase):
    def test_review_weekly_multiples(self):
        rows = [item("GS-0001", kind="decision", review_at="2026-09-29"),  # 7 days
                item("GS-0002", kind="decision", review_at="2026-09-15"),  # 21 days
                item("GS-0003", kind="decision", review_at="2026-09-30"),  # 6 days
                item("GS-0004", kind="decision", review_at="2026-09-28"),  # 8 days
                item("GS-0005", kind="decision", review_at="2026-10-07")]  # tomorrow
        result = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0001", "GS-0002"]])

    def test_t4_measures_from_the_latest_delivered_digest(self):
        # GS-0001 was created between the 10-04 and 10-05 digest rows; GS-0002 before both.
        rows = [item("GS-0001", "orbit: session 10-04 ended on usage limit", kind="interrupted",
                     review_at="2026-10-11", created_at="2026-10-05T03:00:00Z"),
                item("GS-0002", "inkwell: session 10-03 ended on an open question", kind="interrupted",
                     review_at="2026-10-10", created_at="2026-10-03T18:00:00Z")]
        delivered = note(1, "2026-10-04T14:15:30Z", "digest", "digest:2026-10-04", ["GS-0090"], "2026-10-04")
        now = "2026-10-06T14:15:00Z"
        for ok in (1, True):
            with self.subTest(latest_ok=ok):
                latest = dict(YESTERDAY_DIGEST, ok=ok)
                result = schedule.decide(now, rows, [delivered, latest], [FRESH_RUN], TZ, CFG)
                self.assertEqual(result["send"], [])  # both created before the 10-05 digest
        for ok in (0, False, None, "1"):
            with self.subTest(latest_ok=ok):
                latest = dict(YESTERDAY_DIGEST, ok=ok)
                result = schedule.decide(now, rows, [delivered, latest], [FRESH_RUN], TZ, CFG)
                self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0001"]])
        missing_ok = {k: v for k, v in YESTERDAY_DIGEST.items() if k != "ok"}
        result = schedule.decide(now, rows, [missing_ok], [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0001", "GS-0002"]])

    def test_review_at_counts_on_items_with_a_due_date(self):
        rows = [item("GS-0001", due_at="2026-10-30", review_at="2026-09-29"),  # 7 days: T6
                item("GS-0002", due_at="2026-10-30", review_at="2026-10-01")]  # 5 days: no
        result = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0001"]])

    def test_date_only_snooze_reopens_at_local_midnight(self):
        rows = [item("GS-0001", status="snoozed", snooze_until="2026-10-06")]
        self.assertEqual(schedule.decide(chicago(2026, 10, 5, 23, 59), rows, [], [], TZ, CFG)["reopen"], [])
        self.assertEqual(schedule.decide(chicago(2026, 10, 6, 0, 0), rows, [], [], TZ, CFG)["reopen"], ["GS-0001"])

    def test_a_reopened_overdue_item_sorts_as_overdue(self):
        rows = [item("GS-0001", due_at="2026-10-06"),
                item("GS-0002", status="snoozed", snooze_until="2026-10-06T09:00", due_at="2026-10-01")]
        result = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        self.assertEqual(result["reopen"], ["GS-0002"])
        self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0002", "GS-0001"]])

    def test_digest_window_follows_digest_hour(self):
        rows = [item("GS-0001", due_at="2026-10-06")]
        cfg = dict(CFG, digest_hour=7, quiet_end=6)
        fired = [hour for hour in range(24)
                 if schedule.decide(chicago(2026, 10, 6, hour), rows, [], [], TZ, cfg)["send"]]
        self.assertEqual(fired, [7, 8, 9])


INTERRUPTED = "orbit: session 10-05 ended on usage limit"
MISSING = object()


class LooksResolvedTests(unittest.TestCase):
    """T4 skips an interruption that looks resolved; only true or 1 count as resolved."""

    def digest_ids(self, looks_resolved, **fields):
        base = {"kind": "interrupted", "review_at": "2026-10-12", "created_at": "2026-10-05T20:40:00Z"}
        base.update(fields)
        row = item("GS-0001", INTERRUPTED, **base)
        if looks_resolved is not MISSING:
            row["looks_resolved"] = looks_resolved
        sends = schedule.decide("2026-10-06T14:15:00Z", [row], [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)["send"]
        return [push["item_ids"] for push in sends]

    def test_resolved_values(self):
        for value in (True, 1, 1.0):
            with self.subTest(looks_resolved=value):
                self.assertEqual(self.digest_ids(value), [])
        for value in (False, 0, None, MISSING, "1", "true", 2, [1]):
            with self.subTest(looks_resolved=value):
                self.assertEqual(self.digest_ids(value), [["GS-0001"]])

    def test_resolved_items_keep_their_other_triggers(self):
        self.assertEqual(self.digest_ids(True, review_at="2026-10-06"), [["GS-0001"]])  # T6
        self.assertEqual(self.digest_ids(True, snooze_until="2026-10-06T09:00"), [["GS-0001"]])  # T5

    def test_looks_resolved_on_other_kinds_changes_nothing(self):
        row = item("GS-0001", due_at="2026-10-06", looks_resolved=True)
        sends = schedule.decide("2026-10-06T14:15:00Z", [row], [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)["send"]
        self.assertEqual([push["item_ids"] for push in sends], [["GS-0001"]])


class NoDoublePushTests(unittest.TestCase):
    """An advance is skipped for an item a delivered digest carried at or after due - 24 h, or one
    the digest decided in the same tick carries."""

    DUE = "2026-10-07T09:00"  # 14:00Z 10-07, so due - 24 h is 14:00Z on 10-06
    NOW = "2026-10-06T15:15:00Z"  # 10:15 local: digest window, after today's digest

    def advances(self, rows, notes, now=None, cfg=CFG):
        sends = schedule.decide(now or self.NOW, rows, [YESTERDAY_DIGEST] + notes, [FRESH_RUN], TZ, cfg)["send"]
        return [push["dedup_key"] for push in sends if push["kind"] == "advance"]

    def digest_row(self, sent_at, item_ids, **fields):
        row = note(2, sent_at, "digest", "digest:2026-10-06", item_ids, "2026-10-06")
        row.update(fields)
        return row

    def test_carried_by_a_delivered_digest_after_the_window_opened(self):
        rows = [item("GS-0001", due_at=self.DUE)]
        for ok in (1, True):
            with self.subTest(ok=ok):
                self.assertEqual(self.advances(rows, [self.digest_row("2026-10-06T14:15:20Z", ["GS-0001"], ok=ok)]), [])

    def test_a_digest_that_is_not_delivered_does_not_count(self):
        rows = [item("GS-0001", due_at=self.DUE)]
        for ok in (0, False, None, "1", 2):
            with self.subTest(ok=ok):
                notes = [self.digest_row("2026-10-06T14:15:20Z", ["GS-0001"], ok=ok)]
                self.assertEqual(self.advances(rows, notes), ["advance:GS-0001"])
        unreadable = self.digest_row("not a time", ["GS-0001"])
        self.assertEqual(self.advances(rows, [unreadable]), ["advance:GS-0001"])

    def test_only_digest_rows_count(self):
        rows = [item("GS-0001", due_at=self.DUE)]
        noon = "2026-10-06T17:15:00Z"  # 12:15, after the digest window, so this tick sends no digest
        other = note(2, "2026-10-06T14:15:20Z", "deadman", "deadman:run-x", ["GS-0001"], "2026-10-06")
        self.assertEqual(self.advances(rows, [other], now=noon), ["advance:GS-0001"])
        # A row without kind is a digest by its key.
        keyed = {k: v for k, v in self.digest_row("2026-10-06T14:15:20Z", ["GS-0001"]).items() if k != "kind"}
        self.assertEqual(self.advances(rows, [keyed], now=noon), [])

    def test_boundary_is_inclusive_on_the_instant(self):
        rows = [item("GS-0001", due_at=self.DUE)]
        self.assertEqual(self.advances(rows, [self.digest_row("2026-10-06T14:00:00Z", ["GS-0001"])]), [])
        self.assertEqual(self.advances(rows, [self.digest_row("2026-10-06T13:59:59.999999Z", ["GS-0001"])]),
                         ["advance:GS-0001"])
        self.assertEqual(self.advances(rows, [self.digest_row("2026-10-06T09:00:00-05:00", ["GS-0001"])]), [])

    def test_item_ids_forms(self):
        rows = [item("GS-0001", due_at=self.DUE)]
        for ids in ('["GS-0001"]', ' [ "GS-0090", "GS-0001" ] ', ("GS-0001",)):
            with self.subTest(item_ids=ids):
                self.assertEqual(self.advances(rows, [self.digest_row("2026-10-06T14:15:20Z", ids)]), [])
        for ids in ("GS-0001", '["GS-0001"', "[NaN]", '{"GS-0001": 1}', None, 7, ["GS-0002"], [1]):
            with self.subTest(item_ids=ids):
                self.assertEqual(self.advances(rows, [self.digest_row("2026-10-06T14:15:20Z", ids)]),
                                 ["advance:GS-0001"])

    def test_same_tick_digest_absorbs_every_advance_candidate(self):
        # Any item inside its advance window is due within 24 h, so T1 or T3 always holds for it:
        # a tick that sends the digest never sends an advance.
        rows = [item("GS-0001", due_at="2026-10-06T20:00"), item("GS-0002", due_at=self.DUE),
                item("GS-0003", due_at="2026-10-07T09:14")]
        result = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)
        self.assertEqual([(push["kind"], push["item_ids"]) for push in result["send"]],
                         [("digest", ["GS-0001", "GS-0002", "GS-0003"])])

    def test_no_digest_this_tick_means_no_absorption(self):
        rows = [item("GS-0001", due_at=self.DUE)]
        # Budget for one push only, taken by the dead-man: no digest, and no slot for the advance.
        stale = [run("run-2302", "2026-10-06T04:02:00Z")]
        sends = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], stale, TZ,
                                dict(CFG_DM, daily_budget=1))["send"]
        self.assertEqual([push["kind"] for push in sends], ["deadman"])
        # Outside the digest window the advance goes out (nothing carried it).
        self.assertEqual(self.advances(rows, [], now="2026-10-06T17:15:00Z"), ["advance:GS-0001"])

    def test_skipped_items_do_not_use_the_budget(self):
        rows = [item("GS-0001", due_at=self.DUE), item("GS-0002", due_at="2026-10-07T10:00")]
        notes = [self.digest_row("2026-10-06T14:15:20Z", ["GS-0001"])]
        self.assertEqual(self.advances(rows, notes, cfg=dict(CFG, daily_budget=2)), ["advance:GS-0002"])

    def test_a_reschedule_makes_an_old_digest_too_early(self):
        # The digest carried the item before its new due - 24 h: it no longer stands in for it.
        rows = [item("GS-0001", due_at="2026-10-07T12:00")]
        notes = [self.digest_row("2026-10-06T14:15:20Z", ["GS-0001"])]
        self.assertEqual(self.advances(rows, notes, now="2026-10-06T17:15:00Z"), ["advance:GS-0001"])


class ReadingTests(unittest.TestCase):
    """Text is read the same way the JS port reads it: ASCII digits, ASCII whitespace."""

    def digest_ids(self, rows, notes=(YESTERDAY_DIGEST,), cfg=CFG, now="2026-10-06T14:15:00Z"):
        sends = schedule.decide(now, rows, list(notes), [FRESH_RUN], TZ, cfg)["send"]
        return [push["item_ids"] for push in sends]

    def test_ascii_whitespace_is_stripped_and_nothing_else(self):
        self.assertEqual(self.digest_ids([item("GS-0001", due_at=" \t2026-10-06\n")]), [["GS-0001"]])
        for odd in (" 2026-10-06", "2026-10-06　", "\x1c2026-10-06"):
            with self.subTest(due_at=odd):
                self.assertEqual(self.digest_ids([item("GS-0001", due_at=odd)]), [])
        self.assertEqual(schedule.decide(" 2026-10-06T14:15:00Z\n", [], [], [], " %s\t" % TZ, CFG)["send"], [])
        with self.assertRaises(ValueError):
            schedule.decide(" 2026-10-06T14:15:00Z", [], [], [], TZ, CFG)

    def test_digits_are_ascii(self):
        arabic = "٢٠٢٦-10-06"  # 2026 in Arabic-Indic digits
        self.assertEqual(self.digest_ids([item("GS-0001", due_at=arabic)]), [])
        with self.assertRaises(ValueError):
            schedule.decide("٢٠٢٦-10-06T14:15:00Z", [], [], [], TZ, CFG)

    def test_blank_title_check_uses_ascii_whitespace(self):
        rows = [item("GS-0001", " \t", due_at="2026-10-06"), item("GS-0002", "　", due_at="2026-10-06")]
        push = schedule.decide("2026-10-06T14:15:00Z", rows, [YESTERDAY_DIGEST], [FRESH_RUN], TZ, CFG)["send"][0]
        self.assertEqual(push["body"], "2 open: GS-0001 · 　")

    def test_integer_settings(self):
        rows = [item("GS-0001", due_at="2026-10-06")]
        now = "2026-10-06T15:15:00Z"  # 10:15
        for hour in (10, 10.9, "10", " +10 ", "010"):
            with self.subTest(digest_hour=hour):
                self.assertEqual(self.digest_ids(rows, cfg=dict(CFG, digest_hour=hour), now=now), [["GS-0001"]])
        for hour in ("1_0", "10.0", "١٠", "ten", "", float("nan"), float("inf"), [10], {}):
            with self.subTest(digest_hour=hour):
                with self.assertRaises(ValueError):
                    schedule.decide(now, rows, [], [], TZ, dict(CFG, digest_hour=hour))
        self.assertEqual(self.digest_ids(rows, cfg=dict(CFG, daily_budget=True), now=now), [["GS-0001"]])
        self.assertEqual(self.digest_ids(rows, cfg=dict(CFG, daily_budget=False), now=now), [])

    def test_deadman_hours_setting(self):
        runs = [run("run-0803", "2026-10-06T13:03:00Z")]
        now = "2026-10-06T14:15:00Z"  # 1h12m after the run
        for hours, fires in ((1, True), ("1", True), (" 1.2 ", False), ("1.19", True), (".5", True),
                             ("2e0", False), (1.2, False), (True, True)):
            with self.subTest(deadman_hours=hours):
                sends = schedule.decide(now, [], [], runs, TZ, dict(CFG_DM, deadman_hours=hours))["send"]
                self.assertEqual(bool(sends), fires)
        for hours in ("1h", "0x10", "1_0", "", "nan", "inf", float("nan"), float("-inf"), 1e30, 10 ** 400, [1]):
            with self.subTest(deadman_hours=hours):
                with self.assertRaises(ValueError):
                    schedule.decide(now, [], [], runs, TZ, dict(CFG_DM, deadman_hours=hours))

    def test_enable_deadman_strings_strip_ascii_only(self):
        now = "2026-10-06T14:15:00Z"
        self.assertTrue(schedule.decide(now, [], [], [], TZ, dict(CFG, enable_deadman=" ON\t"))["send"])
        self.assertFalse(schedule.decide(now, [], [], [], TZ, dict(CFG, enable_deadman=" true"))["send"])
        self.assertTrue(schedule.decide(now, [], [], [], TZ, dict(CFG, enable_deadman=[0]))["send"])
        self.assertFalse(schedule.decide(now, [], [], [], TZ, dict(CFG, enable_deadman={}))["send"])

    def test_ids_are_text(self):
        rows = [dict(item("GS-0001", due_at="2026-10-06"), id=7), item("GS-0002", due_at="2026-10-06")]
        self.assertEqual(self.digest_ids(rows), [["GS-0002"]])
        mapping = {"GS-0003": {k: v for k, v in item("x", due_at="2026-10-06").items() if k != "id"},
                   "GS-0004": dict(item("", due_at="2026-10-06"), id=None),
                   "GS-0005": dict(item("GS-0006", due_at="2026-10-06"))}
        self.assertEqual(self.digest_ids(mapping), [["GS-0003", "GS-0004", "GS-0006"]])

    def test_rows_may_come_from_any_iterable(self):
        rows = (item("GS-0001", due_at="2026-10-06"),)
        now = "2026-10-06T14:15:00Z"
        for items in (rows, (row for row in rows)):
            with self.subTest(items=type(items).__name__):
                result = schedule.decide(now, items, iter([YESTERDAY_DIGEST]), (FRESH_RUN,), TZ, CFG)
                self.assertEqual([push["item_ids"] for push in result["send"]], [["GS-0001"]])
        for odd in (5, "GS-0001", b"x", {"a": 1}):
            with self.subTest(rows=odd):
                self.assertEqual(schedule.decide(now, odd, odd, odd, TZ, CFG), {"reopen": [], "send": []})

    def test_run_ids(self):
        now = "2026-10-06T16:15:00Z"
        finished = "2026-10-06T13:03:00Z"
        cases = [({"run_id": "run-a"}, "deadman:run-a"), ({"run_id": "", "id": 7}, "deadman:7"),
                 ({"id": "row-7"}, "deadman:row-7"), ({"run_id": True, "id": False}, "deadman:unknown"),
                 ({"run_id": 3}, "deadman:3"), ({"run_id": 3.0}, "deadman:unknown"), ({}, "deadman:unknown")]
        for fields, key in cases:
            with self.subTest(fields=fields):
                row = dict({"finished_at": finished, "ok": 1}, **fields)
                sends = schedule.decide(now, [], [], [row], TZ, CFG_DM)["send"]
                self.assertEqual([push["dedup_key"] for push in sends], [key])

    def test_offsets_and_fractions(self):
        rows = [item("GS-0001", INTERRUPTED, kind="interrupted", review_at="2026-10-12",
                     created_at="2026-10-05T14:15:30.000001Z")]
        self.assertEqual(self.digest_ids(rows), [["GS-0001"]])  # a microsecond after the digest
        rows[0]["created_at"] = "2026-10-05T09:15:30.0000009-05:00"  # truncated to microseconds: equal
        self.assertEqual(self.digest_ids(rows), [])
        rows[0]["created_at"] = "2026-10-05T23:59:00+23:59"  # 00:00Z: before the digest
        self.assertEqual(self.digest_ids(rows), [])
        for created in ("2026-10-05T23:59:00+24:00", "2026-10-05T20:00:00+05", "2026-10-05T20:00:60Z"):
            with self.subTest(created_at=created):
                rows[0]["created_at"] = created  # unreadable: never newer than a real digest
                self.assertEqual(self.digest_ids(rows), [])
        rows[0]["created_at"] = "2026-10-06"  # a date reads as midnight UTC: after the digest
        self.assertEqual(self.digest_ids(rows), [["GS-0001"]])


class ModuleTests(unittest.TestCase):
    """core.schedule must stay a self-contained, clock-free, 3.9-compatible module."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))

    def test_imports_only_the_stdlib_it_needs(self):
        allowed = {"__future__", "json", "re", "collections.abc", "datetime", "zoneinfo"}
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative imports")
                found.add(node.module)
        self.assertEqual(found - allowed, set())

    def test_future_annotations_comes_first(self):
        body = self.tree.body
        first = body[1] if isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) else body[0]
        self.assertIsInstance(first, ast.ImportFrom)
        self.assertEqual((first.module, [alias.name for alias in first.names]), ("__future__", ["annotations"]))

    def test_reads_no_clock(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"now", "utcnow", "today", "time", "monotonic"})

    def test_no_syntax_newer_than_3_9(self):
        match_node = getattr(ast, "Match", None)
        for node in ast.walk(self.tree):
            if match_node is not None:
                self.assertNotIsInstance(node, match_node)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
