#!/usr/bin/env python3
"""Offline checks that the bridge and the walker follow the routine the owner set on the site: the
workplace's job in every prompt, and in a work hour no social turns and no town square. Uses Newsy
Ned's routine on 2026-11-05, when its social turns took it to The Rusty Nail in a work hour.
Run: python3 hermes/test_routine.py"""
import importlib.util, os, pathlib, sys, tempfile, unittest

os.environ.update(AGENTSHANGOUT_TOKEN="x", API_SERVER_KEY="x", AHA_HOME=tempfile.mkdtemp())
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bridge  # noqa: E402

spec = importlib.util.spec_from_file_location("walk", pathlib.Path(__file__).parent / "walk.py")
walk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(walk)

PITCH_DESK = {"name": "Pitch Desk", "room_id": "qDE2oCooqP14wZvZ",
              "job": "Gets News From other agents or human and shares the news on agent's hangout pr bot"}
WORK = {"block": "work", "block_how": "A Work hour: stay at your workplace and do your job.",
        "social_now": True, "public_places": True, "workplace": PITCH_DESK}
SOCIAL = dict(WORK, block="social", block_how="A social hour: go out and hang out.")
MENTION = {"name": "mention", "eventId": "e1",
           "data": {"hangout_id": "R1", "speaker": "Kim", "body": "@Newsy Ned hi", "message_id": 1}}


class RoutineTest(unittest.TestCase):
    def tearDown(self):
        bridge._routine.update(at=float("inf"), value={})

    def test_work_hour_is_seen_though_the_site_says_social_now(self):
        self.assertEqual(bridge.at_work(WORK)["name"], "Pitch Desk")
        self.assertIsNone(bridge.at_work(SOCIAL))
        self.assertIsNone(bridge.at_work(dict(WORK, workplace={})))   # no workplace: nowhere to stay

    def test_the_sites_job_and_hour_are_in_every_prompt(self):
        bridge._routine.update(at=float("inf"), value=WORK)
        text = bridge.prompt("Newsy Ned", MENTION)
        self.assertIn("Your owner set your job on the site, at your workplace Pitch Desk", text)
        self.assertIn("shares the news on agent's hangout pr bot", text)
        self.assertIn("This hour of your schedule: A Work hour", text)

    def test_no_routine_adds_nothing(self):
        self.assertEqual(bridge.routine_line({}), "")

    def test_no_social_turn_in_a_work_hour(self):
        saved = bridge.tool
        bridge.tool = lambda name, args: (True, {"routine": WORK, "you_are_in": {"id": "R1"}})
        try:
            facts, why = bridge.look_around("Newsy Ned", {})
        finally:
            bridge.tool = saved
        self.assertIsNone(facts)
        self.assertEqual(why, "a work hour, at Pitch Desk")


class WalkerTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.saved = [], walk.tool
        walk.tool = lambda name, args=None: (self.calls.append((name, args)), {"ok": True})[1]

    def tearDown(self):
        walk.tool = self.saved

    def test_goes_back_to_work_not_the_square(self):
        walk.back_to_work({"routine": WORK, "you_are_in": {"id": "Bm4qhf3DwgntfHQF", "name": "Town Square"}})
        self.assertEqual(self.calls, [("go_to_room", {"hangout_id": "qDE2oCooqP14wZvZ"})])

    def test_stays_put_at_work(self):
        walk.back_to_work({"routine": WORK, "you_are_in": {"id": "qDE2oCooqP14wZvZ"}})
        self.assertEqual(self.calls, [])

    def test_in_a_social_hour_work_is_none_of_its_business(self):
        self.assertIsNone(walk.at_work({"routine": SOCIAL}))


if __name__ == "__main__":
    unittest.main()
