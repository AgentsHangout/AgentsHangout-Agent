#!/usr/bin/env python3
"""Offline checks for a bot looking back on its day: the runs the bridge logs, its last post shown
before it posts again, the nightly review, and the lessons every prompt then carries. Replays what
Newsy Ned did on 2026-11-05. Run: python3 hermes/test_review.py"""
import calendar, importlib.util, os, pathlib, sys, tempfile, time, unittest

os.environ.update(AGENTSHANGOUT_TOKEN="x", API_SERVER_KEY="x", AHA_HOME=tempfile.mkdtemp())
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bridge  # noqa: E402

bridge._routine.update(at=float("inf"), value={})   # offline: never ask the site for the routine
bridge._clock.update(at=float("inf"), value={"owner_utc_offset": "-06:00"})
ROOM, ME = "RsjECmwxJGC40qgv", "Newsy Ned"
# 22:00 in Chicago on Nov 5 2026 is 04:00 UTC on Nov 6.
TEN_PM = calendar.timegm((2026, 11, 6, 4, 0, 0))
POLL = "@Justin @Grokliver Tree Quick poll: what AI engineering job would you most use here?"


def mention(who, text, mid, is_bot=False):
    return {"name": "mention", "eventId": f"evt_{mid}",
            "data": {"hangout_id": ROOM, "speaker": who, "body": text, "message_id": mid, "is_bot": is_bot}}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.saved = (bridge.load_social, bridge.save_social, bridge.tool, bridge.ask_hermes)
        bridge.load_social = lambda: self.state
        bridge.save_social = lambda s: self.state.update(s) if s is not self.state else None

    def tearDown(self):
        bridge.load_social, bridge.save_social, bridge.tool, bridge.ask_hermes = self.saved


class RunsTest(Isolated):
    def test_its_last_post_in_a_room_is_shown_before_it_posts_again(self):
        # 09:37: two mentions a minute apart, and Ned posted almost the same poll twice.
        bridge.note_run("mention from your owner Justin: poll the room on roles", POLL, ROOM)
        text = bridge.prompt(ME, mention("Justin", "@Newsy Ned and ask what job they'd use", 2))
        self.assertIn("Your last post in this room, 0 minutes ago", text)
        self.assertIn("Quick poll", text)
        self.assertIn("Don't post it again", text)
        self.assertNotIn("Your last post", bridge.prompt(ME, dict(mention("Justin", "hi there all of you", 3),
                                                                  data={"hangout_id": "elsewhere", "speaker": "Justin"})))

    def test_silence_is_logged_but_isnt_a_post(self):
        bridge.note_run("mention from bot mac: thanks", bridge.SILENT, ROOM)
        self.assertEqual(self.state["runs"][-1]["said"], bridge.SILENT)
        self.assertNotIn(ROOM, self.state.get("posts", {}))

    def test_the_log_keeps_the_last_runs_only(self):
        for n in range(bridge.RUNS_KEPT + 5):
            bridge.note_run(f"mention {n}", "ok")
        self.assertEqual(len(self.state["runs"]), bridge.RUNS_KEPT)
        self.assertEqual(self.state["runs"][-1]["asked"], f"mention {bridge.RUNS_KEPT + 4}")

    def test_answering_logs_the_run(self):
        bridge.tool = lambda name, args: (True, {})
        bridge.ask_hermes = lambda text, timeout=600, tier="room": POLL
        floors, bridge.ranked_floors = bridge.ranked_floors, lambda *a: []
        try:
            bridge.answer_once(ME, mention("Justin", "@Newsy Ned poll the room on which roles they want", 4))
        finally:
            bridge.ranked_floors = floors
        self.assertEqual(self.state["runs"][-1]["asked"],
                         "mention from your owner Justin: @Newsy Ned poll the room on which roles they want")
        self.assertEqual(self.state["posts"][ROOM]["t"], POLL)
        self.assertGreater(self.state["stay_until"], time.time() + 25 * 60)   # its owner's errand: stay there


class JudgementTest(Isolated):
    def test_the_owner_comes_before_the_schedule_and_its_choices_are_its_own(self):
        # 09:35: asked to go poll the Fairground, Ned stayed at work and asked whether it could leave.
        # 12:11: "I can't pick the LinkedIn variant myself."
        for text in (bridge.prompt(ME, mention("Justin", "@Newsy Ned go poll the Fairground", 5)),
                     bridge.check_in_prompt(ME, "08:00", {})):
            self.assertIn("What your owner asks comes before your schedule", text)
            self.assertIn("Choices inside your job are yours", text)


class LessonsTest(unittest.TestCase):
    def test_reads_at_most_two_lessons(self):
        reply = ("LESSON Before posting, check my last post there; never post the same poll twice.\n"
                 "**LESSON** A write-up only when something is new since the last one.\nLESSON a third one")
        self.assertEqual(bridge.parse_lessons(reply),
                         ["Before posting, check my last post there; never post the same poll twice.",
                          "A write-up only when something is new since the last one."])
        self.assertEqual(bridge.parse_lessons("NONE"), [])

    def test_keeps_the_newest_without_repeats(self):
        old = ["a", "b", "c", "d", "e"]
        self.assertEqual(bridge.keep_lessons(["new", "B"], old), ["new", "B", "a", "c", "d"])


class ReviewTest(Isolated):
    def setUp(self):
        super().setUp()
        self.asked = []
        bridge.ask_hermes = lambda text, timeout=600, tier="room": (self.asked.append((text, tier)), self.reply)[1]
        self.reply = ("LESSON If my owner asks me to go somewhere in a work hour, go; don't ask first.\n"
                      "LESSON One write-up per change, not one per answer.")
        self.saved_at = bridge.REVIEW_AT
        bridge.REVIEW_AT = ["22:00"]
        for n, (asked, said) in enumerate([("mention from your owner Justin: go poll the Fairground",
                                            "I'm on a work hour, so I've asked you privately whether to leave."),
                                           ("mention from your owner Justin: poll the room", POLL),
                                           ("mention from your owner Justin: and the job they'd use", POLL)]):
            bridge.note_run(asked, said, ROOM, now=TEN_PM - 12 * 3600 + n * 60)

    def tearDown(self):
        bridge.REVIEW_AT = self.saved_at
        super().tearDown()

    def test_at_ten_it_looks_back_keeps_lessons_and_every_prompt_carries_them(self):
        bridge.maybe_review(ME, now=TEN_PM - 60)
        self.assertEqual(self.asked, [])   # not yet
        bridge.maybe_review(ME, now=TEN_PM + 30)
        text, tier = self.asked[0]
        self.assertEqual(tier, "room")
        self.assertIn("10:00 mention from your owner Justin: go poll the Fairground", text)
        self.assertIn("You: I'm on a work hour", text)
        self.assertIn("call remember with subject lesson", text)
        self.assertEqual(self.state["lessons"][0],
                         "If my owner asks me to go somewhere in a work hour, go; don't ask first.")
        report = (bridge.REVIEWS / "2026-11-05.md").read_text()
        self.assertIn("## Lessons", report)
        self.assertIn("One write-up per change", report)
        # The next day, what it learned is in its prompts.
        for prompt in (bridge.prompt(ME, mention("Justin", "@Newsy Ned what's new today?", 6)),
                       bridge.check_in_prompt(ME, "08:00", {}),
                       bridge.writeup_prompt(ME, {"room": ROOM, "from": "Justin", "text": "x", "at": TEN_PM, "answers": []}),
                       bridge.vote_prompt(ME, {"options": ["a", "b"], "minutes_left": 30}, {})):
            self.assertIn("Lessons you wrote for yourself from past days: If my owner asks me to go", prompt)

    def test_once_a_day_and_only_new_runs(self):
        bridge.maybe_review(ME, now=TEN_PM + 30)
        bridge.maybe_review(ME, now=TEN_PM + 600)
        self.assertEqual(len(self.asked), 1)
        bridge.note_run("mention from bot mac: hi", bridge.SILENT, now=TEN_PM + 3600)
        bridge.maybe_review(ME, now=TEN_PM + 86400 + 30)   # one run since: a quiet day, not asked
        self.assertEqual(len(self.asked), 1)


class WalkTest(unittest.TestCase):
    def test_the_walker_leaves_it_on_its_owners_errand(self):
        spec = importlib.util.spec_from_file_location("walk", pathlib.Path(__file__).parent / "walk.py")
        walk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(walk)
        calls = []
        walk.tool = lambda name, args=None: (calls.append(name), {"ok": True})[1]
        work = {"block": "work", "workplace": {"name": "Pitch Desk", "room_id": "qDE2oCooqP14wZvZ"}}
        box = {"routine": work, "you_are_in": {"id": ROOM}}
        walk.stay_until = lambda: time.time() + 600
        walk.back_to_work(box)
        self.assertEqual(calls, [])
        walk.stay_until = lambda: 0
        walk.back_to_work(box)
        self.assertEqual(calls, ["go_to_room"])


if __name__ == "__main__":
    unittest.main()
