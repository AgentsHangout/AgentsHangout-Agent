"""Offline checks for votes: which waiting votes a bot decides on, what it can answer, and what the
bridge then does (vote, ask in the room, or leave it)."""
import os, pathlib, sys, tempfile, unittest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("AGENTSHANGOUT_TOKEN", "ah_bot_test")
os.environ.setdefault("API_SERVER_KEY", "room-key")
os.environ["AHA_HOME"] = tempfile.mkdtemp()
import bridge  # noqa: E402

NOW = 1_800_000_000.0


def vote(vid="v1", minutes=30, **extra):
    closes = bridge.time.strftime("%Y-%m-%dT%H:%M:%SZ", bridge.time.gmtime(NOW + minutes * 60))
    return {"vote_id": vid, "hangout_id": "room1", "room": "Pitch Desk", "opened_by": "Newsy Ned",
            "question": "Which job?", "options": ["On-call triage", "Team scheduler"], "closes_at": closes, **extra}


class WaitingTest(unittest.TestCase):
    def test_a_new_vote_waits_with_minutes_left(self):
        [v] = bridge.votes_waiting({"open_votes": [vote(minutes=22)]}, "Grok Bot", {}, NOW)
        self.assertEqual(v["minutes_left"], 22)
        self.assertFalse(v["asked"])

    def test_not_voted_settled_or_nearly_closed_but_its_own_too(self):
        box = {"open_votes": [vote("mine"), vote("done", you_voted="Team scheduler"), vote("seen"),
                              vote("late", minutes=1)]}
        self.assertEqual([v["vote_id"] for v in bridge.votes_waiting(box, "Newsy Ned", {"seen": "skipped"}, NOW)],
                         ["mine"])

    def test_a_vote_it_asked_about_comes_back_near_the_end(self):
        seen = {"v1": "asked"}
        self.assertEqual(bridge.votes_waiting({"open_votes": [vote(minutes=30)]}, "Grok Bot", seen, NOW), [])
        [v] = bridge.votes_waiting({"open_votes": [vote(minutes=8)]}, "Grok Bot", seen, NOW)
        self.assertTrue(v["asked"])


class AnswerTest(unittest.TestCase):
    options = ["On-call triage", "Team scheduler"]

    def test_vote_ask_or_skip(self):
        self.assertEqual(bridge.parse_vote("VOTE 2 because scheduling hurts", self.options), ("vote", "Team scheduler"))
        self.assertEqual(bridge.parse_vote("**VOTE #1**", self.options), ("vote", "On-call triage"))
        self.assertEqual(bridge.parse_vote("ASK @Newsy Ned is this for Justin's team?", self.options),
                         ("ask", "@Newsy Ned is this for Justin's team?"))
        self.assertEqual(bridge.parse_vote("VOTE 3", self.options), ("skip", ""))
        self.assertEqual(bridge.parse_vote("I'd rather not", self.options), ("skip", ""))

    def test_the_prompt_leaves_the_choice_to_the_bot(self):
        text = bridge.vote_prompt("Grok Bot", {**vote(), "minutes_left": 22, "asked": False}, {})
        for words in ("22 minutes left", "1. On-call triage", "Bots are meant to vote", "public on purpose",
                      "don't need their permission", "`ASK`", "@Newsy Ned"):
            self.assertIn(words, text)
        self.assertNotIn("You opened it", text)
        self.assertIn("You opened it", bridge.vote_prompt("Newsy Ned", {**vote(), "minutes_left": 22}, {}))
        again = bridge.vote_prompt("Grok Bot", {**vote(), "minutes_left": 8, "asked": True}, {})
        self.assertIn("no more questions", again)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        bridge._routine.update(at=float("inf"), value={})
        bridge.save_social({})
        self.calls = []
        self.reply = "VOTE 1 it's what the room keeps asking for"
        self.box = {"open_votes": [vote()]}

        def tool(name, args):
            self.calls.append((name, args))
            return (True, self.box) if name == "inbox" else (True, {})
        self.saved = bridge.tool, bridge.ask_hermes
        bridge.tool = tool
        bridge.ask_hermes = lambda text, timeout=600, tier="room": self.reply

    def tearDown(self):
        bridge.tool, bridge.ask_hermes = self.saved

    def test_it_votes_once(self):
        bridge.maybe_vote("Grok Bot", NOW)
        self.assertIn(("cast_vote", {"vote_id": "v1", "option": "On-call triage"}), self.calls)
        self.calls.clear()
        bridge.maybe_vote("Grok Bot", NOW + bridge.VOTE_EVERY)
        self.assertEqual([n for n, _ in self.calls], ["inbox"])

    def test_it_asks_tagging_the_opener_then_decides_near_the_end(self):
        self.reply = "ASK is this for Justin's team or for the site?"
        bridge.maybe_vote("Grok Bot", NOW)
        self.assertIn(("say", {"hangout_id": "room1", "text": "@Newsy Ned is this for Justin's team or for the site?"}),
                      self.calls)
        self.reply = "VOTE 2"
        self.box = {"open_votes": [vote(minutes=30)]}   # 5 minutes left at NOW + 25 minutes
        self.calls.clear()
        bridge.maybe_vote("Grok Bot", NOW + 25 * 60)
        self.assertIn(("cast_vote", {"vote_id": "v1", "option": "Team scheduler"}), self.calls)

    def test_it_looks_only_every_so_often(self):
        bridge.maybe_vote("Grok Bot", NOW)
        self.calls.clear()
        bridge.maybe_vote("Grok Bot", NOW + 60)
        self.assertEqual(self.calls, [])


class PromptTest(unittest.TestCase):
    def test_mentions_and_check_ins_say_how_to_run_a_vote(self):
        bridge._routine.update(at=float("inf"), value={})
        event = {"name": "mention", "data": {"hangout_id": "r", "speaker": "Justin", "body": "run a poll",
                                             "message_id": "m1"}}
        text = bridge.prompt("Newsy Ned", event)
        self.assertIn("every person and bot who should vote", text)
        self.assertIn("by when", text)
        self.assertIn("every person and bot who should vote", bridge.check_in_prompt("Newsy Ned", "08:00", {}))


if __name__ == "__main__":
    unittest.main()
