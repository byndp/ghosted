from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from core import timeutil as tu

CHI = "America/Chicago"
UTC = timezone.utc


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# Monday 2026-10-05 10:00 in Chicago (CDT, UTC-5).
MONDAY = utc(2026, 10, 5, 15, 0)


class TestIso(unittest.TestCase):
    def test_to_iso_is_utc_seconds_z(self):
        self.assertEqual(tu.to_iso(utc(2026, 10, 6, 14, 2, 0, 987654)), "2026-10-06T14:02:00Z")

    def test_to_iso_converts_offsets_and_naive_is_utc(self):
        chicago = datetime(2026, 10, 6, 9, 2, tzinfo=tu.get_tz(CHI))
        self.assertEqual(tu.to_iso(chicago), "2026-10-06T14:02:00Z")
        self.assertEqual(tu.to_iso(datetime(2026, 10, 6, 14, 2)), "2026-10-06T14:02:00Z")

    def test_parse_iso_forms(self):
        expected = utc(2026, 10, 6, 14, 2, 3)
        for text in ("2026-10-06T14:02:03Z", "2026-10-06T14:02:03z", "2026-10-06T14:02:03+00:00",
                     "2026-10-06T09:02:03-05:00", "2026-10-06T09:02:03-0500",
                     "2026-10-06T14:02:03", "2026-10-06 14:02:03"):
            with self.subTest(text=text):
                self.assertEqual(tu.parse_iso(text), expected)

    def test_parse_iso_fractions(self):
        self.assertEqual(tu.parse_iso("2026-10-06T14:02:03.123Z").microsecond, 123000)
        self.assertEqual(tu.parse_iso("2026-10-06T14:02:03.123456Z").microsecond, 123456)
        self.assertEqual(tu.parse_iso("2026-10-06T14:02:03.123456789+00:00").microsecond, 123456)

    def test_parse_iso_result_is_aware_utc(self):
        dt = tu.parse_iso("2026-10-06T09:02:00-05:00")
        self.assertEqual(dt.utcoffset(), timedelta(0))

    def test_roundtrip(self):
        self.assertEqual(tu.to_iso(tu.parse_iso("2026-03-08T07:59:59Z")), "2026-03-08T07:59:59Z")

    def test_parse_iso_rejects_bad_input(self):
        for bad in ("", "2026-10-06", "10/06/2026 14:02", "2026-13-01T00:00:00Z",
                    "2026-10-06T25:00:00Z", "2026-10-06T14:02:00+25:00", "soon"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    tu.parse_iso(bad)
        with self.assertRaises(ValueError):
            tu.parse_iso(None)  # type: ignore[arg-type]

    def test_now_utc_is_aware(self):
        self.assertEqual(tu.now_utc().utcoffset(), timedelta(0))


class TestLocal(unittest.TestCase):
    def test_local_date_across_midnight(self):
        # 03:00Z on Oct 6 is still Oct 5 (22:00) in Chicago.
        self.assertEqual(tu.local_date(utc(2026, 10, 6, 3, 0), CHI), date(2026, 10, 5))
        self.assertEqual(tu.local_date(utc(2026, 10, 6, 3, 0), "UTC"), date(2026, 10, 6))

    def test_local_dt(self):
        local = tu.local_dt(utc(2026, 10, 6, 14, 2), CHI)
        self.assertEqual((local.hour, local.minute), (9, 2))
        self.assertEqual(local.utcoffset(), timedelta(hours=-5))

    def test_mmdd(self):
        self.assertEqual(tu.mmdd(date(2026, 10, 1)), "10-01")
        self.assertEqual(tu.mmdd(datetime(2026, 1, 9, 23, 0)), "01-09")

    def test_get_tz(self):
        self.assertIs(tu.get_tz(None), UTC)
        self.assertTrue(tu.is_valid_timezone(CHI))
        self.assertFalse(tu.is_valid_timezone("America/Chicgo"))
        self.assertFalse(tu.is_valid_timezone(""))
        with self.assertRaises(ValueError):
            tu.get_tz("Not/AZone")

    def test_canonical_timezone(self):
        from zoneinfo import ZoneInfo
        self.assertEqual(tu.canonical_timezone(CHI), CHI)
        self.assertEqual(tu.canonical_timezone(" UTC "), "UTC")
        self.assertEqual(tu.canonical_timezone("Z"), "UTC")  # get_tz reads it; ZoneInfo can't
        self.assertIs(tu.get_tz("Z"), UTC)
        for bad in ("Not/AZone", "", "   ", None, "../etc/passwd"):
            with self.subTest(bad=bad):
                self.assertIsNone(tu.canonical_timezone(bad))
        for name in (CHI, "UTC", "Z", "Europe/Paris"):
            with self.subTest(name=name):
                ZoneInfo(tu.canonical_timezone(name))  # whatever it returns, ZoneInfo can load


class TestDst(unittest.TestCase):
    """2026-11-01 is the US fall-back day; 2026-03-08 is spring-forward."""

    def test_wall_to_utc_contract_cases(self):
        self.assertEqual(tu.wall_to_utc("2026-11-02", 9, 0, CHI), utc(2026, 11, 2, 15, 0))
        self.assertEqual(tu.wall_to_utc("2026-10-30", 9, 0, CHI), utc(2026, 10, 30, 14, 0))

    def test_fall_back_day(self):
        self.assertEqual(tu.wall_to_utc("2026-11-01", 0, 0, CHI), utc(2026, 11, 1, 5, 0))
        self.assertEqual(tu.wall_to_utc("2026-11-01", 9, 0, CHI), utc(2026, 11, 1, 15, 0))
        # 01:30 happens twice; fold=0 picks the first (CDT) occurrence.
        self.assertEqual(tu.wall_to_utc("2026-11-01", 1, 30, CHI), utc(2026, 11, 1, 6, 30))

    def test_spring_forward_day(self):
        self.assertEqual(tu.wall_to_utc("2026-03-07", 9, 0, CHI), utc(2026, 3, 7, 15, 0))
        self.assertEqual(tu.wall_to_utc("2026-03-08", 0, 0, CHI), utc(2026, 3, 8, 6, 0))
        self.assertEqual(tu.wall_to_utc("2026-03-08", 9, 0, CHI), utc(2026, 3, 8, 14, 0))
        # 02:30 does not exist; it maps onto the pre-transition offset (08:30Z = 03:30 CDT).
        self.assertEqual(tu.wall_to_utc("2026-03-08", 2, 30, CHI), utc(2026, 3, 8, 8, 30))

    def test_wall_to_utc_accepts_date_and_validates(self):
        self.assertEqual(tu.wall_to_utc(date(2026, 11, 2), 9, 0, CHI), utc(2026, 11, 2, 15, 0))
        with self.assertRaises(ValueError):
            tu.wall_to_utc("2026-11-02", 24, 0, CHI)
        with self.assertRaises(ValueError):
            tu.wall_to_utc("2026-02-30", 9, 0, CHI)

    def test_due_instant_across_dst(self):
        self.assertEqual(tu.due_instant("2026-11-01", CHI), utc(2026, 11, 1, 5, 0))
        self.assertEqual(tu.due_instant("2026-11-02", CHI), utc(2026, 11, 2, 6, 0))
        self.assertEqual(tu.due_instant("2026-11-02T09:00", CHI), utc(2026, 11, 2, 15, 0))
        self.assertEqual(tu.due_instant("2026-10-30T09:00", CHI), utc(2026, 10, 30, 14, 0))
        self.assertIsNone(tu.due_instant(None, CHI))

    def test_snooze_across_fall_back(self):
        saturday = utc(2026, 10, 31, 17, 0)  # Sat Oct 31, noon CDT
        until = tu.snooze_until("1d", saturday, CHI)
        self.assertEqual(until, "2026-11-01T09:00")
        self.assertEqual(tu.wall_to_utc(until[:10], 9, 0, CHI), utc(2026, 11, 1, 15, 0))

    def test_local_date_on_dst_days(self):
        self.assertEqual(tu.local_date(utc(2026, 11, 1, 5, 59), CHI), date(2026, 11, 1))
        self.assertEqual(tu.local_date(utc(2026, 11, 1, 4, 59), CHI), date(2026, 10, 31))
        self.assertEqual(tu.local_date(utc(2026, 3, 8, 6, 0), CHI), date(2026, 3, 8))
        self.assertEqual(tu.local_date(utc(2026, 3, 8, 5, 59), CHI), date(2026, 3, 7))


class TestParseDue(unittest.TestCase):
    def due(self, text: str, now: datetime = MONDAY, tz: str = CHI) -> str:
        return tu.parse_due(text, now, tz)

    def test_mm_dd_this_year(self):
        self.assertEqual(self.due("10-11"), "2026-10-11")
        self.assertEqual(self.due("12-31"), "2026-12-31")

    def test_mm_dd_year_rule(self):
        # More than 180 days in the past -> next year; up to 180 days back stays this year.
        self.assertEqual(self.due("01-15"), "2027-01-15")   # 263 days back
        self.assertEqual(self.due("05-01"), "2026-05-01")   # 157 days back: overdue, this year
        self.assertEqual(self.due("04-08"), "2026-04-08")   # exactly 180 days back
        self.assertEqual(self.due("04-07"), "2027-04-07")   # 181 days back

    def test_mm_dd_uses_local_year(self):
        # 2027-01-01 03:00Z is still Dec 31 2026 in Chicago.
        new_years_eve = utc(2027, 1, 1, 3, 0)
        self.assertEqual(self.due("12-31", new_years_eve), "2026-12-31")
        self.assertEqual(self.due("12-31", new_years_eve, "UTC"), "2027-12-31")

    def test_full_date_and_times(self):
        self.assertEqual(self.due("2026-12-01"), "2026-12-01")
        self.assertEqual(self.due("2026-10-11 14:00"), "2026-10-11T14:00")
        self.assertEqual(self.due("2026-10-11T14:00"), "2026-10-11T14:00")
        self.assertEqual(self.due("10-11 14:00"), "2026-10-11T14:00")
        self.assertEqual(self.due("10-11T09:05"), "2026-10-11T09:05")
        self.assertEqual(self.due("10-11 9:05"), "2026-10-11T09:05")

    def test_relative_forms(self):
        self.assertEqual(self.due("today"), "2026-10-05")
        self.assertEqual(self.due("Tomorrow"), "2026-10-06")
        self.assertEqual(self.due("+3d"), "2026-10-08")
        self.assertEqual(self.due("+0d"), "2026-10-05")
        self.assertEqual(self.due("tomorrow 09:00"), "2026-10-06T09:00")

    def test_relative_forms_use_local_today(self):
        late_sunday = utc(2026, 10, 5, 4, 0)  # Sun Oct 4, 23:00 CDT
        self.assertEqual(self.due("today", late_sunday), "2026-10-04")
        self.assertEqual(self.due("mon", late_sunday), "2026-10-05")

    def test_weekdays_next_occurrence_never_today(self):
        self.assertEqual(self.due("mon"), "2026-10-12")  # today is Monday
        self.assertEqual(self.due("monday"), "2026-10-12")
        self.assertEqual(self.due("tue"), "2026-10-06")
        self.assertEqual(self.due("fri"), "2026-10-09")
        self.assertEqual(self.due("sun"), "2026-10-11")
        self.assertEqual(self.due("Sat 10:30"), "2026-10-10T10:30")

    def test_errors_are_readable(self):
        for bad in ("", "   ", "someday", "13-01", "02-30", "2026-02-30", "10-11 25:00",
                    "10-11 14:60", "-1d", "next week", "10/11"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    self.due(bad)
                self.assertTrue(str(ctx.exception))

    def test_feb_29_needs_a_leap_year(self):
        with self.assertRaises(ValueError) as ctx:
            self.due("02-29")
        self.assertIn("2027", str(ctx.exception))  # 02-29 is > 180 days back, so next year
        self.assertEqual(self.due("2028-02-29"), "2028-02-29")

    def test_feb_29_rolls_into_the_next_leap_year(self):
        october_2027 = utc(2027, 10, 5, 15, 0)
        self.assertEqual(self.due("02-29", october_2027), "2028-02-29")
        march_2028 = utc(2028, 3, 20, 15, 0)  # 20 days after Feb 29, 2028: stays this year
        self.assertEqual(self.due("02-29", march_2028), "2028-02-29")


class TestDueHelpers(unittest.TestCase):
    def test_is_timed(self):
        self.assertTrue(tu.is_timed("2026-10-11T14:00"))
        self.assertFalse(tu.is_timed("2026-10-11"))
        self.assertFalse(tu.is_timed(None))

    def test_check_due(self):
        self.assertEqual(tu.check_due("2026-10-11"), "2026-10-11")
        self.assertEqual(tu.check_due("2026-10-11T14:00"), "2026-10-11T14:00")
        for bad in ("2026-10-11 14:00", "2026-13-01", "2026-10-11T24:00", "10-11", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    tu.check_due(bad)

    def test_check_wall_minute_and_date(self):
        self.assertEqual(tu.check_wall_minute("2026-10-06T09:00"), "2026-10-06T09:00")
        with self.assertRaises(ValueError):
            tu.check_wall_minute("2026-10-06")
        self.assertEqual(tu.check_date("2026-10-12"), "2026-10-12")
        with self.assertRaises(ValueError):
            tu.check_date("2026-10-12T09:00")

    def test_due_date(self):
        self.assertEqual(tu.due_date("2026-10-11T14:00"), date(2026, 10, 11))

    def test_stored_values_are_ascii_only(self):
        # core.schedule and the Worker read stored values as ASCII; a value in other digits
        # must be refused at the gate, not stored and then silently never pushed.
        arabic = "٢٠٢٦-10-11"
        fullwidth = "２０２６-10-11T09:00"
        for check, value in ((tu.check_due, arabic), (tu.check_due, fullwidth),
                             (tu.check_wall_minute, fullwidth), (tu.check_date, arabic),
                             (tu.parse_iso, "2026-10-11T09:00:00Ｚ"),
                             (tu.parse_iso, "２０２６-10-11T09:00:00Z")):
            with self.subTest(check=check.__name__, value=ascii(value)):
                with self.assertRaises(ValueError):
                    check(value)
        # what a person types may use other digits; the stored result is ASCII
        now = tu.parse_iso("2026-10-05T15:00:00Z")
        self.assertEqual(tu.parse_due("１０-１１", now, "America/Chicago"), "2026-10-11")


class TestSnooze(unittest.TestCase):
    def test_one_and_three_days(self):
        self.assertEqual(tu.snooze_until("1d", MONDAY, CHI), "2026-10-06T09:00")
        self.assertEqual(tu.snooze_until("3d", MONDAY, CHI), "2026-10-08T09:00")

    def test_mon_on_a_monday_is_the_following_monday(self):
        self.assertEqual(tu.snooze_until("mon", MONDAY, CHI), "2026-10-12T09:00")

    def test_mon_from_other_days(self):
        sunday = utc(2026, 10, 11, 15, 0)
        tuesday = utc(2026, 10, 6, 15, 0)
        self.assertEqual(tu.snooze_until("mon", sunday, CHI), "2026-10-12T09:00")
        self.assertEqual(tu.snooze_until("mon", tuesday, CHI), "2026-10-12T09:00")

    def test_uses_local_today(self):
        late_monday = utc(2026, 10, 6, 3, 0)  # still Monday 22:00 in Chicago
        self.assertEqual(tu.snooze_until("1d", late_monday, CHI), "2026-10-06T09:00")
        self.assertEqual(tu.snooze_until("mon", late_monday, CHI), "2026-10-12T09:00")

    def test_hour_and_errors(self):
        self.assertEqual(tu.snooze_until("1d", MONDAY, CHI, hour=7), "2026-10-06T07:00")
        for bad in ("2d", "", "week", "+1d"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    tu.snooze_until(bad, MONDAY, CHI)


class TestDisplay(unittest.TestCase):
    def test_human_date(self):
        self.assertEqual(tu.human_date(date(2026, 10, 18)), "Sun Oct 18")
        self.assertEqual(tu.human_date(date(2026, 10, 8)), "Thu Oct 8")

    def test_human_time(self):
        self.assertEqual(tu.human_time(utc(2026, 10, 6, 14, 2), CHI), "9:02 AM")
        self.assertEqual(tu.human_time(datetime(2026, 10, 6, 0, 5)), "12:05 AM")
        self.assertEqual(tu.human_time(datetime(2026, 10, 6, 12, 0)), "12:00 PM")
        self.assertEqual(tu.human_time(datetime(2026, 10, 6, 23, 59)), "11:59 PM")

    def test_human_due(self):
        self.assertEqual(tu.human_due("2026-10-11"), "Sun Oct 11")
        self.assertEqual(tu.human_due("2026-10-11T14:00"), "Sun Oct 11, 2:00 PM")
        self.assertEqual(tu.human_due(None), "")

    def test_aliases(self):
        self.assertIs(tu.fmt_date, tu.human_date)
        self.assertIs(tu.format_time, tu.human_time)


if __name__ == "__main__":
    unittest.main()
