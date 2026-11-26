"""Unit tests for agents/availability_agent.py.

Pure stdlib (unittest) so they run without the project's venv:

    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents import availability_agent as aa  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0)  # Wednesday noon
TODAY = NOW.date()

RULES = {
    "default_min_nights": 3,
    "near_term": {"days": 14, "min_nights": 2},
    "weekday_min_nights": {"friday": 2},
    "gap_rules": {
        "two_night_gap_min_nights": 2,
        "one_night_gap": {
            "action": "turnover_toggle",
            "min_nights": 1,
            "open_at": "17:00",
            "close_at": "07:00",
            "open_all_day_on": ["saturday", "sunday"],
            "note": "Single-night turnover: bookable 5pm-7am only.",
        },
    },
}


def cal(booked_offsets: set[int], span: int = 60, start_offset: int = 0) -> list[dict]:
    """Calendar rows for `span` days from TODAY+start_offset; offsets in
    `booked_offsets` (relative to TODAY) are booked."""
    return [
        {
            "date": (TODAY + timedelta(days=start_offset + i)).isoformat(),
            "booked": (start_offset + i) in booked_offsets,
        }
        for i in range(span)
    ]


def at(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


class TestMinNightsRules(unittest.TestCase):
    def test_default_is_three_far_out(self):
        plans = aa.plan_listing(cal(set()), RULES, NOW)
        d = at(30)  # a Friday-free, gap-free date well past the near-term window
        while date.fromisoformat(d).weekday() == 4:
            d = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        self.assertEqual(plans[d]["min_nights"], 3)
        self.assertEqual(plans[d]["reasons"], [])

    def test_near_term_two_weeks_is_two(self):
        plans = aa.plan_listing(cal(set()), RULES, NOW)
        for offset in (0, 7, 14):
            self.assertEqual(plans[at(offset)]["min_nights"], 2, f"offset {offset}")
        # day 15 is outside the window; 3 again unless it's a Friday
        day15 = date.fromisoformat(at(15))
        if day15.weekday() != 4:
            self.assertEqual(plans[at(15)]["min_nights"], 3)

    def test_friday_checkin_is_two_outside_near_term(self):
        plans = aa.plan_listing(cal(set()), RULES, NOW)
        fridays = [d for d in plans if date.fromisoformat(d).weekday() == 4
                   and date.fromisoformat(d) > TODAY + timedelta(days=14)]
        self.assertTrue(fridays, "fixture should contain a far-out Friday")
        for d in fridays:
            self.assertEqual(plans[d]["min_nights"], 2, d)
            self.assertIn("Friday check-in", plans[d]["reasons"])

    def test_non_friday_far_out_unaffected_by_weekday_rule(self):
        plans = aa.plan_listing(cal(set()), RULES, NOW)
        d = at(20)
        if date.fromisoformat(d).weekday() != 4:
            self.assertNotIn("Friday check-in", plans[d]["reasons"])


class TestGapRules(unittest.TestCase):
    def test_two_night_gap_allows_two(self):
        # bookings at 30 and 33 -> nights 31,32 unbooked and bounded
        plans = aa.plan_listing(cal({30, 33}), RULES, NOW)
        for offset in (31, 32):
            self.assertEqual(plans[at(offset)]["min_nights"], 2, f"offset {offset}")
            self.assertIn("2-night gap between bookings", plans[at(offset)]["reasons"])

    def test_three_night_gap_keeps_default(self):
        plans = aa.plan_listing(cal({30, 34}), RULES, NOW)
        d = at(31)
        if date.fromisoformat(d).weekday() != 4:
            self.assertEqual(plans[d]["min_nights"], 3)

    def test_one_night_gap_gets_min_one(self):
        plans = aa.plan_listing(cal({30, 32}), RULES, NOW)
        self.assertEqual(plans[at(31)]["min_nights"], 1)

    def test_leading_run_is_not_a_gap(self):
        # first booking is at offset 2; nights 0,1 are unbounded on the left
        rows = cal({2, 5})
        plans = aa.plan_listing(rows, RULES, NOW)
        self.assertNotIn("2-night gap between bookings", plans[at(0)]["reasons"])

    def test_trailing_run_is_not_a_gap(self):
        rows = cal({57}, span=60)  # nights 58,59 trail the last booking
        plans = aa.plan_listing(rows, RULES, NOW)
        self.assertNotIn("2-night gap between bookings", plans[at(58)]["reasons"])

    def test_hole_in_scrape_does_not_bound_a_run(self):
        rows = [r for r in cal({30, 33}) if r["date"] != at(32)]
        plans = aa.plan_listing(rows, RULES, NOW)
        # night 31 is now bounded by a booking on the left and a MISSING day on
        # the right -- not a 2-night gap
        self.assertNotIn("2-night gap between bookings", plans[at(31)]["reasons"])


class TestTurnoverToggle(unittest.TestCase):
    # These all run "as of" TODAY (a Wednesday), so the clock window applies.
    # The orphan night's own weekday is irrelevant -- see TestOpenAllDayOn.
    ORPHAN = 34

    def setUp(self):
        self.assertLess(TODAY.weekday(), 5, "fixture 'now' must be a weekday")

    def _one_night_gap_plan(self, hour, minute=0):
        when = datetime.combine(TODAY, datetime.min.time()).replace(hour=hour, minute=minute)
        rows = cal({self.ORPHAN - 1, self.ORPHAN + 1})
        return aa.plan_listing(rows, RULES, when)[at(self.ORPHAN)]

    def test_open_in_the_evening(self):
        self.assertTrue(self._one_night_gap_plan(17, 0)["available"])
        self.assertTrue(self._one_night_gap_plan(23, 59)["available"])

    def test_open_after_midnight(self):
        self.assertTrue(self._one_night_gap_plan(0, 0)["available"])
        self.assertTrue(self._one_night_gap_plan(6, 59)["available"])

    def test_closed_during_the_day(self):
        self.assertFalse(self._one_night_gap_plan(7, 0)["available"])
        self.assertFalse(self._one_night_gap_plan(12, 0)["available"])
        self.assertFalse(self._one_night_gap_plan(16, 59)["available"])

    def test_window_and_note_reported(self):
        plan = self._one_night_gap_plan(18)
        self.assertEqual(plan["turnover_window"], "17:00-07:00")
        self.assertIn("Single-night turnover: bookable 5pm-7am only.", plan["flags"])

    def test_block_action_always_unavailable(self):
        rules = {**RULES, "gap_rules": {**RULES["gap_rules"],
                                        "one_night_gap": {"action": "block"}}}
        plan = aa.plan_listing(cal({30, 32}), rules, NOW)[at(31)]
        self.assertFalse(plan["available"])

    def test_open_action_always_available(self):
        rules = {**RULES, "gap_rules": {**RULES["gap_rules"],
                                        "one_night_gap": {"action": "open", "min_nights": 1}}}
        plan = aa.plan_listing(cal({30, 32}), rules, datetime.combine(TODAY, datetime.min.time()).replace(hour=12))[at(31)]
        self.assertTrue(plan["available"])
        self.assertEqual(plan["min_nights"], 1)


class TestOpenAllDayOn(unittest.TestCase):
    """open_all_day_on suspends the clock window on the weekday the agent RUNS,
    for every orphan night -- not based on the night's own weekday."""

    SAT = date(2026, 11, 28)  # a Saturday
    SUN = date(2026, 11, 29)
    MON = date(2026, 11, 30)

    def _plan(self, when, orphan=34, rules=RULES):
        """`when` is the moment the agent runs; the orphan night is fixed."""
        rows = [
            {"date": (when.date() + timedelta(days=i)).isoformat(),
             "booked": i in {orphan - 1, orphan + 1}}
            for i in range(60)
        ]
        night = (when.date() + timedelta(days=orphan)).isoformat()
        return aa.plan_listing(rows, rules, when)[night], night

    def test_weekend_run_opens_orphans_at_any_hour(self):
        for day in (self.SAT, self.SUN):
            for hour in (0, 7, 12, 16, 17, 23):
                plan, night = self._plan(datetime.combine(day, datetime.min.time()).replace(hour=hour))
                self.assertTrue(plan["available"], f"run {day:%a} {hour:02d}:00, night {night}")
                self.assertEqual(plan["min_nights"], 1)

    def test_weekday_run_still_closed_during_the_day(self):
        plan, _ = self._plan(datetime.combine(self.MON, datetime.min.time()).replace(hour=12))
        self.assertFalse(plan["available"])
        plan, _ = self._plan(datetime.combine(self.MON, datetime.min.time()).replace(hour=18))
        self.assertTrue(plan["available"])

    def test_applies_regardless_of_the_nights_own_weekday(self):
        """Every orphan night is open on a weekend run, including weekday ones."""
        saturday_noon = datetime.combine(self.SAT, datetime.min.time()).replace(hour=12)
        seen = set()
        for orphan in range(20, 34):
            plan, night = self._plan(saturday_noon, orphan=orphan)
            seen.add(date.fromisoformat(night).strftime("%a"))
            self.assertTrue(plan["available"], f"night {night} ({date.fromisoformat(night):%a})")
        self.assertGreaterEqual(len(seen), 7, "fixture should span every weekday")

    def test_reason_names_the_run_day_and_no_window_advertised(self):
        plan, _ = self._plan(datetime.combine(self.SAT, datetime.min.time()).replace(hour=12))
        self.assertIn("window suspended on Saturday (open all day)", plan["reasons"][-1])
        self.assertIsNone(plan["turnover_window"])

    def test_empty_list_means_no_exception(self):
        rules = {**RULES, "gap_rules": {**RULES["gap_rules"], "one_night_gap": {
            **RULES["gap_rules"]["one_night_gap"], "open_all_day_on": []}}}
        plan, _ = self._plan(
            datetime.combine(self.SAT, datetime.min.time()).replace(hour=12), rules=rules
        )
        self.assertFalse(plan["available"])  # Saturday run, but no exception configured


class TestFlagsAndHelpers(unittest.TestCase):
    def test_unbookable_checkin_is_flagged_not_relaxed(self):
        # 3-night gap, far out: checking in on the LAST night leaves 1 night
        plans = aa.plan_listing(cal({30, 34}), RULES, NOW)
        plan = plans[at(33)]
        self.assertTrue(any("unbookable as check-in" in f for f in plan["flags"]), plan)
        self.assertGreater(plan["min_nights"], 1)  # deliberately not relaxed

    def test_booked_nights_absent_from_plan(self):
        plans = aa.plan_listing(cal({30, 32}), RULES, NOW)
        self.assertNotIn(at(30), plans)
        self.assertIn(at(31), plans)

    def test_empty_calendar(self):
        self.assertEqual(aa.plan_listing([], RULES, NOW), {})

    def test_weekday_parsing(self):
        self.assertEqual(aa._parse_weekday("friday"), 4)
        self.assertEqual(aa._parse_weekday("Friday"), 4)
        self.assertEqual(aa._parse_weekday("fri"), 4)
        self.assertEqual(aa._parse_weekday(4), 4)
        with self.assertRaises(ValueError):
            aa._parse_weekday("funday")
        with self.assertRaises(ValueError):
            aa._parse_weekday(9)

    def test_window_wrap(self):
        from datetime import time
        self.assertTrue(aa._in_window(time(2, 0), time(17, 0), time(7, 0)))
        self.assertFalse(aa._in_window(time(9, 0), time(17, 0), time(7, 0)))
        # non-wrapping window still works
        self.assertTrue(aa._in_window(time(10, 0), time(9, 0), time(17, 0)))
        self.assertFalse(aa._in_window(time(18, 0), time(9, 0), time(17, 0)))


if __name__ == "__main__":
    unittest.main()


class TestActionLog(unittest.TestCase):
    """storage/db.py helpers the apply script relies on to tell an
    agent-blocked night from a guest reservation."""

    def setUp(self):
        import tempfile
        from storage import db
        self.db = db
        self._orig_path = db.DB_PATH
        db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        db.init_db()
        self.conn = db.get_conn()

    def tearDown(self):
        self.conn.close()
        self.db.DB_PATH = self._orig_path

    def test_latest_action_wins_and_dry_runs_ignored(self):
        add = self.db.insert_availability_action
        add(self.conn, "2026-11-25T07:00", "L", "2026-12-01", "block", None, False)
        add(self.conn, "2026-11-25T07:00", "L", "2026-12-09", "block", None, True)   # dry run
        add(self.conn, "2026-11-25T07:00", "L", "2026-12-05", "block", None, False)
        add(self.conn, "2026-11-25T17:00", "L", "2026-12-05", "open", None, False)   # reopened
        self.conn.commit()
        self.assertEqual(
            self.db.agent_blocked_dates(self.conn, "L", "2026-11-25"), {"2026-12-01"}
        )

    def test_blocked_dates_scoped_by_listing(self):
        self.db.insert_availability_action(self.conn, "2026-11-25T07:00", "L", "2026-12-01", "block", None, False)
        self.conn.commit()
        self.assertEqual(self.db.agent_blocked_dates(self.conn, "OTHER", "2026-11-25"), set())

    def test_last_applied_min_nights(self):
        add = self.db.insert_availability_action
        add(self.conn, "2026-11-25T07:00", "L", "2026-12-01", "min_nights", "3", False)
        add(self.conn, "2026-11-25T17:00", "L", "2026-12-01", "min_nights", "2", False)
        add(self.conn, "2026-11-25T17:00", "L", "2026-12-02", "min_nights", "junk", False)
        add(self.conn, "2026-11-25T17:00", "L", "2026-12-03", "min_nights", "1", True)  # dry run
        self.conn.commit()
        got = self.db.last_applied_min_nights(self.conn, "L", "2026-11-25")
        self.assertEqual(got, {"2026-12-01": 2})  # latest wins; junk and dry runs dropped


class TestShippedConfig(unittest.TestCase):
    """Pins the policy that config/listings.yaml actually ships, including the
    one documented difference between the two listings. Skipped if PyYAML
    isn't importable so the rest of the suite stays stdlib-only."""

    def setUp(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(Path(__file__).parent.parent / "config" / "listings.yaml") as f:
            self.cfg = yaml.safe_load(f)
        self.rules = {l["name"]: l.get("stay_rules") for l in self.cfg["listings"]}

    def test_both_listings_have_rules(self):
        self.assertEqual(set(self.rules), {"Cascade", "Dennison"})
        for name, r in self.rules.items():
            self.assertIsNotNone(r, f"{name} has no stay_rules")

    def test_shared_rules_are_identical(self):
        c, d = self.rules["Cascade"], self.rules["Dennison"]
        for key in ("default_min_nights", "near_term", "weekday_min_nights"):
            self.assertEqual(c[key], d[key], f"{key} should be shared via the anchor")
        self.assertEqual(c["gap_rules"]["two_night_gap_min_nights"],
                         d["gap_rules"]["two_night_gap_min_nights"])

    def test_the_one_documented_difference(self):
        self.assertEqual(self.rules["Cascade"]["gap_rules"]["one_night_gap"]["action"],
                         "turnover_toggle")
        self.assertEqual(self.rules["Dennison"]["gap_rules"]["one_night_gap"]["action"],
                         "open")

    def test_cascade_follows_the_clock_on_a_weekday_run(self):
        wednesday = datetime(2026, 11, 25, 0, 0)
        self.assertLess(wednesday.weekday(), 5)
        rows = cal({33, 35})
        for hour, expected in ((7, False), (12, False), (16, False), (17, True), (23, True), (3, True)):
            plan = aa.plan_listing(rows, self.rules["Cascade"], wednesday.replace(hour=hour))[at(34)]
            self.assertIs(plan["available"], expected, f"Cascade Wed {hour:02d}:00")
            self.assertEqual(plan["min_nights"], 1)

    def test_cascade_open_all_day_on_a_weekend_run(self):
        for day in (date(2026, 11, 28), date(2026, 11, 29)):  # Sat, Sun
            rows = [{"date": (day + timedelta(days=i)).isoformat(), "booked": i in {33, 35}}
                    for i in range(60)]
            night = (day + timedelta(days=34)).isoformat()
            for hour in (0, 7, 12, 16, 17, 23):
                plan = aa.plan_listing(
                    rows, self.rules["Cascade"],
                    datetime.combine(day, datetime.min.time()).replace(hour=hour),
                )[night]
                self.assertTrue(plan["available"], f"Cascade {day:%a} {hour:02d}:00")
                self.assertEqual(plan["min_nights"], 1)

    def test_dennison_orphan_night_always_open(self):
        for hour in (0, 7, 12, 17, 23):
            plan = aa.plan_listing(
                cal({33, 35}), self.rules["Dennison"],
                datetime.combine(TODAY, datetime.min.time()).replace(hour=hour),
            )[at(34)]
            self.assertTrue(plan["available"], f"Dennison at {hour:02d}:00")
            self.assertEqual(plan["min_nights"], 1)

    def test_dennison_has_no_weekend_exception(self):
        """Dennison is always open anyway, so open_all_day_on would be
        redundant config -- assert it isn't there, to catch a copy-paste."""
        self.assertNotIn("open_all_day_on",
                         self.rules["Dennison"]["gap_rules"]["one_night_gap"])

    def test_cascade_weekend_days_are_saturday_and_sunday(self):
        self.assertEqual(
            self.rules["Cascade"]["gap_rules"]["one_night_gap"]["open_all_day_on"],
            ["saturday", "sunday"],
        )
