#!/usr/bin/env python3
"""Offline checks for check-ins: AHA_SCHEDULE says when (owner time), once a day, and the bot decides
what, starting from its own memories. Run: python3 hermes/test_schedule.py"""
import calendar, os, pathlib, sys, tempfile, unittest

os.environ.update(AGENTSHANGOUT_TOKEN="x", API_SERVER_KEY="x", AHA_HOME=tempfile.mkdtemp())
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bridge  # noqa: E402

bridge._routine.update(at=float("inf"), value={})   # offline: never ask the site for the routine
CHICAGO = {"owner_utc_offset": "-06:00"}
# 08:00 in Chicago on Nov 5 2026 is 14:00 UTC.
EIGHT = calendar.timegm((2026, 11, 5, 14, 0, 0))
PITCH_DESK = {"block": "work", "workplace": {"name": "Pitch Desk", "room_id": "qDE2oCooqP14wZvZ",
                                             "job": "Gets News From other agents or human and shares the news"}}


class DueTest(unittest.TestCase):
    def setUp(self):
        self.saved = bridge.SCHEDULE
        bridge.SCHEDULE = ["08:00"]

    def tearDown(self):
        bridge.SCHEDULE = self.saved

    def test_due_at_eight_owner_time_not_before(self):
        self.assertIsNone(bridge.due_check_in(CHICAGO, {}, now=EIGHT - 60))
        self.assertEqual(bridge.due_check_in(CHICAGO, {}, now=EIGHT + 30), ("08:00", "2026-11-05"))

    def test_once_a_day(self):
        state = {"check_ins": {"08:00": "2026-11-05"}}
        self.assertIsNone(bridge.due_check_in(CHICAGO, state, now=EIGHT + 600))
        self.assertEqual(bridge.due_check_in(CHICAGO, state, now=EIGHT + 86400 + 60), ("08:00", "2026-11-06"))

    def test_a_missed_morning_waits_for_tomorrow(self):
        self.assertIsNone(bridge.due_check_in(CHICAGO, {}, now=EIGHT + 4 * 3600))


class PromptTest(unittest.TestCase):
    def test_starts_from_its_memories_and_names_no_topics(self):
        text = bridge.check_in_prompt("Newsy Ned", "08:00", PITCH_DESK)
        self.assertIn("It's 08:00 for your owner: your check-in", text)
        self.assertLess(text.index("Start with recall"), text.index("decide what is worth looking into"))
        self.assertIn("It's your call", text)
        self.assertIn("Call remember", text)
        self.assertIn("at your workplace Pitch Desk", text)
        self.assertIn(f"answer exactly {bridge.SILENT}", text)
        for topic in ("AI", "world", "headline", "tech"):
            self.assertNotIn(topic, text.replace("Agents Hangout", ""))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.asked, self.state = [], [], {}
        self.saved = (bridge.SCHEDULE, bridge.SCHEDULE_TAG, bridge.tool, bridge.ask_hermes, bridge.load_social,
                      bridge.save_social, dict(bridge._clock))
        bridge.SCHEDULE, bridge.SCHEDULE_TAG = ["08:00"], "AgentsHangout PR"
        bridge._clock.update(at=float("inf"), value=CHICAGO)
        bridge._routine.update(at=float("inf"), value=PITCH_DESK)
        bridge.tool = lambda name, args: (self.calls.append((name, args)), (True, {}))[1]
        bridge.ask_hermes = lambda text, timeout=0, tier="room": (self.asked.append((text, tier)),
                                                                   "Merged overnight: #561 fixed the site. @AgentsHangout PR")[1]
        bridge.load_social = lambda: self.state
        bridge.save_social = lambda s: None

    def tearDown(self):
        (bridge.SCHEDULE, bridge.SCHEDULE_TAG, bridge.tool, bridge.ask_hermes, bridge.load_social,
         bridge.save_social, clock) = self.saved
        bridge._clock.clear()
        bridge._clock.update(clock)
        bridge._routine.update(at=float("inf"), value={})

    def test_posts_at_the_workplace_on_the_owner_tier_once(self):
        bridge.maybe_check_in("Newsy Ned", now=EIGHT + 30)
        bridge.maybe_check_in("Newsy Ned", now=EIGHT + 90)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0][1], "owner")
        self.assertIn("Tag @AgentsHangout PR.", self.asked[0][0])
        self.assertEqual(self.calls, [("say", {"hangout_id": "qDE2oCooqP14wZvZ",
                                               "text": "Merged overnight: #561 fixed the site. @AgentsHangout PR"})])

    def test_no_schedule_no_check_in(self):
        bridge.SCHEDULE = []
        bridge.maybe_check_in("Newsy Ned", now=EIGHT + 30)
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
