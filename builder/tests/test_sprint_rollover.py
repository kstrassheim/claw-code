"""Sprints roll over by CATCHING UP, not by being woken at the boundary.

This is the distinction the whole module exists for, and it is invisible from
the outside until it is wrong. A job that fires exactly at Saturday 13:00
misses the rollover COMPLETELY if the pod happens to be restarting in that
minute — and nothing says so. The sprint never ends, the next never begins, and
the first symptom is numbers that quietly stopped making sense weeks later.

So every tick asks "has a boundary passed since the running sprint started?".
Late is fine and self-correcting; missed is not. The tests below pin both
halves: that a pod which was down across the boundary still rolls over, and
that when it does it says it was late rather than pretending it was on time.
"""

import datetime as dt
import os
import shutil
import tempfile
import unittest

from harness import TMP_ROOT, load, temp_env

# UTC everywhere in the arithmetic tests: the point being measured is the
# catch-up logic, and a test that also depends on whether tzdata is installed
# fails for a reason that has nothing to do with what it is checking.
CFG = {"enabled": True, "weekday": 5, "hour": 13, "minute": 0,
       "timezone": "UTC", "lengthDays": 7}


def utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)


class RolloverIsACatchUpCheck(unittest.TestCase):
    def setUp(self):
        self.s = load("sprint_schedule")

    def test_a_pod_that_was_down_across_the_boundary_still_rolls_over(self):
        # THE test. The sprint started at the previous Saturday boundary; the
        # pod was down over the weekend and the first tick is on Monday. A
        # timer-based design has already lost this rollover for good.
        due, why = self.s.rollover_due("2026-08-01T13:00:00+00:00",
                                       utc("2026-08-10T09:00:00"), CFG)
        self.assertTrue(due)
        self.assertIn("late", why)

    def test_a_late_rollover_reports_how_late_it_is(self):
        # Silence here would make a two-day-old rollover indistinguishable
        # from a punctual one, and the sprint window would be wrong with
        # nothing in the log to explain it.
        _, why = self.s.rollover_due("2026-08-01T13:00:00+00:00",
                                     utc("2026-08-10T13:00:00"), CFG)
        self.assertRegex(why, r"\d+h late")
        self.assertIn("2026-08-08", why, "it should name the boundary it missed")

    def test_an_on_time_rollover_says_so(self):
        due, why = self.s.rollover_due("2026-08-01T13:00:00+00:00",
                                       utc("2026-08-08T13:04:00"), CFG)
        self.assertTrue(due)
        self.assertIn("on time", why)

    def test_no_sprint_running_is_itself_a_reason_to_roll(self):
        # The very first tick after this ships must open sprint 1 without
        # being asked; autonomy is the default, not an opt-in.
        due, why = self.s.rollover_due(None, utc("2026-08-10T09:00:00"), CFG)
        self.assertTrue(due)
        self.assertIn("no sprint", why)

    def test_a_current_sprint_does_not_roll(self):
        due, why = self.s.rollover_due("2026-08-08T13:00:00+00:00",
                                       utc("2026-08-10T09:00:00"), CFG)
        self.assertFalse(due)
        self.assertIn("current", why)

    def test_rolling_over_is_idempotent(self):
        # The tick runs every few minutes. A hundred ticks in a row must do
        # nothing, or every tick would open a new sprint.
        now = utc("2026-08-10T09:00:00")
        started, _ = self.s.sprint_window(now, CFG)
        for _ in range(5):
            due, _ = self.s.rollover_due(started, now, CFG)
            self.assertFalse(due)

    def test_switching_automatic_sprints_off_stops_it(self):
        due, why = self.s.rollover_due(None, utc("2026-08-10T09:00:00"),
                                       dict(CFG, enabled=False))
        self.assertFalse(due)
        self.assertIn("off", why)

    def test_an_unreadable_start_does_not_roll(self):
        # Rolling on an unparseable timestamp would open a new sprint on every
        # tick — a much worse failure than standing still and saying why.
        due, why = self.s.rollover_due("not a date",
                                       utc("2026-08-10T09:00:00"), CFG)
        self.assertFalse(due)
        self.assertIn("cannot read", why)

    def test_a_start_without_a_timezone_is_read_as_utc(self):
        # Markers written by an older build carry a naive timestamp. Treating
        # it as unreadable would stall rollovers on exactly those clusters.
        due, _ = self.s.rollover_due("2026-08-01T13:00:00",
                                     utc("2026-08-10T09:00:00"), CFG)
        self.assertTrue(due)


class WhereTheBoundaryFalls(unittest.TestCase):
    def setUp(self):
        self.s = load("sprint_schedule")

    def test_the_boundary_is_the_most_recent_configured_weekday_and_time(self):
        b = self.s.boundary_on_or_before(utc("2026-08-10T09:00:00"), CFG)
        self.assertEqual(b.weekday(), 5)
        self.assertEqual((b.hour, b.minute), (13, 0))
        self.assertEqual(b.date().isoformat(), "2026-08-08")

    def test_earlier_on_the_boundary_day_belongs_to_the_previous_sprint(self):
        # 09:00 on Saturday is BEFORE the 13:00 boundary. Rounding it forward
        # would end the sprint four hours early, every week.
        b = self.s.boundary_on_or_before(utc("2026-08-08T09:00:00"), CFG)
        self.assertEqual(b.date().isoformat(), "2026-08-01")

    def test_the_window_is_the_configured_length(self):
        start, end = self.s.sprint_window(utc("2026-08-10T09:00:00"), CFG)
        self.assertEqual(start[:10], "2026-08-08")
        self.assertEqual(end[:10], "2026-08-15")

    def test_a_shorter_sprint_length_is_honoured(self):
        start, end = self.s.sprint_window(utc("2026-08-10T09:00:00"),
                                          dict(CFG, lengthDays=3))
        self.assertEqual(start[:10], "2026-08-08")
        self.assertEqual(end[:10], "2026-08-11")

    def test_the_next_boundary_follows_the_current_one(self):
        n = self.s.next_boundary(utc("2026-08-10T09:00:00"), CFG)
        self.assertEqual(n.date().isoformat(), "2026-08-15")


class SayingWhenSprintsEnd(unittest.TestCase):
    """People say the boundary out loud, in whatever form comes to mind."""

    def setUp(self):
        self.s = load("sprint_schedule")

    def test_the_spellings_people_actually_use(self):
        for text, expected in (("Sat 13:00", (5, 13, 0)),
                               ("saturday 13", (5, 13, 0)),
                               ("Mo 9h", (0, 9, 0)),
                               ("Tuesday 9am", (1, 9, 0)),
                               ("friday 5pm", (4, 17, 0)),
                               ("samstag 13:30", (5, 13, 30))):
            with self.subTest(text=text):
                self.assertEqual(self.s.parse_spec(text), expected)

    def test_a_schedule_it_cannot_read_is_refused_not_guessed(self):
        # A MISREAD schedule is worse than a rejected one: it silently moves
        # every sprint boundary and nothing reports it.
        for text in ("", "sometime", "13:00", "Sat"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.s.parse_spec(text)


class ChangingTheScheduleMovesTheRunningSprint(unittest.TestCase):
    def setUp(self):
        self.s = load("sprint_schedule")

    def test_moving_the_boundary_later_extends_the_running_sprint(self):
        # Anchored on NOW, not on the sprint's start. Anchoring on the start
        # would end a sprint that began on Saturday the very next day when the
        # boundary moves Saturday -> Sunday, i.e. after twenty hours, while
        # the operator asked for it to run LATER.
        r = self.s.reschedule_active("2026-08-08T13:00:00+00:00",
                                     utc("2026-08-10T09:00:00"),
                                     dict(CFG, weekday=6))
        self.assertEqual(r["endsAt"][:10], "2026-08-16")
        self.assertFalse(r["overdue"])

    def test_a_weekday_that_has_already_passed_does_not_end_it_retroactively(self):
        r = self.s.reschedule_active("2026-08-08T13:00:00+00:00",
                                     utc("2026-08-12T09:00:00"),
                                     dict(CFG, weekday=0))     # Monday
        self.assertGreater(r["endsAt"], "2026-08-12")

    def test_an_unreadable_start_yields_nothing_rather_than_raising(self):
        self.assertEqual(self.s.reschedule_active("nonsense", utc("2026-08-12T09:00:00"), CFG), {})


class TheScheduleFile(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="sched-", dir=TMP_ROOT)
        self.path = os.path.join(self.dir, "sprint-schedule.conf")
        self._env = temp_env(SPRINT_SCHEDULE_CONF=self.path)
        self._env.__enter__()
        self.s = load("sprint_schedule")

    def tearDown(self):
        self._env.__exit__(None, None, None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_defaults_apply_when_there_is_no_file(self):
        # Autonomous by default: nothing has to be configured for sprints to
        # start rolling.
        cfg = self.s.load_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["weekday"], self.s.DEFAULT_WEEKDAY)
        self.assertEqual(cfg["lengthDays"], self.s.DEFAULT_LENGTH_DAYS)

    def test_a_saved_schedule_reads_back(self):
        self.s.save_config(dict(self.s.load_config(), weekday=0, hour=9,
                                minute=30))
        cfg = self.s.load_config()
        self.assertEqual((cfg["weekday"], cfg["hour"], cfg["minute"]), (0, 9, 30))

    def test_an_unparseable_line_falls_back_instead_of_raising(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("weekday = someday\nhour = later\nlengthDays = 0\n")
        cfg = self.s.load_config()
        self.assertEqual(cfg["weekday"], self.s.DEFAULT_WEEKDAY)
        self.assertEqual(cfg["hour"], self.s.DEFAULT_HOUR)
        self.assertEqual(cfg["lengthDays"], self.s.DEFAULT_LENGTH_DAYS)

    def test_describe_names_the_timezone(self):
        # A boundary quietly computed in the wrong zone is only ever noticed
        # as "the numbers stopped adding up", so the zone is always stated.
        text = self.s.describe(self.s.load_config())
        self.assertIn(self.s.DEFAULT_TZ, text)
        self.assertIn("Saturday", text)

    def test_a_timezone_that_cannot_be_loaded_is_reported(self):
        text = self.s.describe(dict(CFG, timezone="Mars/Olympus"))
        self.assertFalse(self.s.timezone_available("Mars/Olympus"))
        self.assertIn("WARNING", text)


if __name__ == "__main__":
    unittest.main()
