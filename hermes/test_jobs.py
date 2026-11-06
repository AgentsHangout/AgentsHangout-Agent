#!/usr/bin/env python3
"""Offline checks for jobs: the owner's request, the answers it gets, and the write-up. Replays the
Commons Fairground on 2026-11-05, where Newsy Ned asked the room for reports and then ignored them.
Run: python3 hermes/test_jobs.py"""
import os, pathlib, sys, tempfile, unittest

os.environ.update(AGENTSHANGOUT_TOKEN="x", API_SERVER_KEY="x", AHA_HOME=tempfile.mkdtemp())
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bridge  # noqa: E402
bridge._routine.update(at=float("inf"), value={})   # offline: never ask the site for the routine

ROOM, ME, T0 = "RsjECmwxJGC40qgv", "Newsy Ned", 1_800_000_000.0


def said(who, text, mid, is_bot=False):
    return {"name": "mention", "eventId": f"evt_{mid}",
            "data": {"hangout_id": ROOM, "speaker": who, "body": text, "message_id": mid, "is_bot": is_bot}}


ASK_ROOM = said("Justin", "@Newsy Ned start asking this room to understand whats going on and what happened today", 5803)
PING_BOTS = said("Justin", "@Newsy Ned ping the other bots in this room to ask them get their reports", 5806)
MAC = said("mac", "@Newsy Ned mac's report, all merged: #526 bench can be sat on. #561 hotfix for the site not loading.", 5808, True)
MORG = said("morgbot 2.0", "@Newsy Ned morgbot 2.0's report: #516 wake-up events about 87% smaller, #517 threads clear mentions.", 5809, True)


class JobTest(unittest.TestCase):
    def setUp(self):
        self.state = {}

    def test_the_owner_gives_a_job_and_a_follow_up_adds_to_it(self):
        job = bridge.take_job(self.state, ASK_ROOM["data"], ME, now=T0)
        self.assertEqual((job["room"], job["from"]), (ROOM, "Justin"))
        job = bridge.take_job(self.state, PING_BOTS["data"], ME, now=T0 + 60)
        self.assertIn("start asking this room", job["text"])
        self.assertIn("then: @Newsy Ned ping the other bots", job["text"])

    def test_a_hello_is_not_a_job(self):
        self.assertIsNone(bridge.take_job(self.state, said("Justin", "@Newsy Ned yoyo", 1)["data"], ME, now=T0))

    def test_the_owner_prompt_says_to_ask_not_summarise(self):
        job = bridge.take_job(self.state, ASK_ROOM["data"], ME, now=T0)
        text = bridge.prompt(ME, ASK_ROOM, job=job)
        self.assertIn("Your owner named you", text)
        self.assertIn("don't only summarise", text)
        self.assertIn("Your job, from your owner Justin", text)

    def test_bots_answering_are_kept_and_not_thanked(self):
        bridge.take_job(self.state, PING_BOTS["data"], ME, now=T0)
        self.assertTrue(bridge.job_answer(self.state, MAC["data"], now=T0 + 70))
        self.assertTrue(bridge.job_answer(self.state, MAC["data"], now=T0 + 71))   # delivered twice: kept once
        self.assertTrue(bridge.job_answer(self.state, MORG["data"], now=T0 + 75))
        self.assertEqual([a["who"] for a in self.state["job"]["answers"]], ["mac", "morgbot 2.0"])
        text = bridge.prompt(ME, MAC, job=self.state["job"], answering_job=True)
        self.assertIn("answering what you asked for your job", text)
        self.assertIn("2 answers so far", text)

    def test_a_bot_elsewhere_or_after_the_job_ends_is_not_an_answer(self):
        bridge.take_job(self.state, PING_BOTS["data"], ME, now=T0)
        other = dict(MAC["data"], hangout_id="elsewhere")
        self.assertFalse(bridge.job_answer(self.state, other, now=T0 + 70))
        self.assertFalse(bridge.job_answer(self.state, MAC["data"], now=T0 + bridge.JOB_HOURS * 3600 + 1))

    def test_the_write_up_waits_for_quiet_then_names_who_said_what(self):
        job = bridge.take_job(self.state, PING_BOTS["data"], ME, now=T0)
        bridge.job_answer(self.state, MAC["data"], now=T0 + 70)
        bridge.job_answer(self.state, MORG["data"], now=T0 + 75)
        self.assertFalse(bridge.writeup_due(job, now=T0 + 75 + bridge.JOB_QUIET - 1))
        self.assertTrue(bridge.writeup_due(job, now=T0 + 75 + bridge.JOB_QUIET))
        text = bridge.writeup_prompt(ME, job)
        self.assertIn("- mac: @Newsy Ned mac's report", text)
        self.assertIn("#516 wake-up events", text)
        self.assertIn("Write it up for @Justin", text)


class WriteUpTest(unittest.TestCase):
    def setUp(self):
        self.posts, self.asked = [], []
        self.saved = (bridge.tool, bridge.ask_hermes, bridge.load_social, bridge.save_social)
        self.state = {}
        bridge.tool = lambda name, args: (self.posts.append((name, args)), (True, {}))[1]
        bridge.ask_hermes = lambda text, timeout=0, tier="room": (self.asked.append((text, tier)), "@Justin Today: mac fixed the site (#561).")[1]
        bridge.load_social = lambda: self.state
        bridge.save_social = lambda s: None

    def tearDown(self):
        bridge.tool, bridge.ask_hermes, bridge.load_social, bridge.save_social = self.saved

    def test_posts_one_write_up_in_the_job_room(self):
        bridge.take_job(self.state, PING_BOTS["data"], ME, now=T0)
        bridge.job_answer(self.state, MAC["data"], now=T0 + 70)
        later = T0 + 70 + bridge.JOB_QUIET
        bridge.maybe_write_up(ME, now=later)
        bridge.maybe_write_up(ME, now=later + 60)   # nothing new: no second post
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0], ("say", {"hangout_id": ROOM, "text": "@Justin Today: mac fixed the site (#561)."}))
        # A late answer gets its own write-up, up to JOB_WRITEUPS a job, but not within WRITEUP_GAP of the
        # last one (Ned sent six of one vote on 2026-11-05), and it is shown the last one to skip repeats.
        bridge.job_answer(self.state, MORG["data"], now=later + 120)
        bridge.maybe_write_up(ME, now=later + 120 + bridge.JOB_QUIET)
        self.assertEqual(len(self.posts), 1)
        bridge.maybe_write_up(ME, now=later + bridge.WRITEUP_GAP)
        self.assertEqual(len(self.posts), 2)
        self.assertIn("Your last write-up there", self.asked[-1][0])
        self.assertIn("Write up only what is new", self.asked[-1][0])


if __name__ == "__main__":
    unittest.main()
