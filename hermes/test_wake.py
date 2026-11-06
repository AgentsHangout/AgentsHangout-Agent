"""Offline checks for running Hermes through a wake server (wake/server.py): config.yaml points at it
and reads its skills, and the bridge tells it what this machine gives the bot."""
import os, pathlib, sys, tempfile, unittest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("AGENTSHANGOUT_TOKEN", "ah_bot_test")
os.environ.setdefault("API_SERVER_KEY", "room-key")
os.environ["AHA_HOME"] = tempfile.mkdtemp()
import bridge  # noqa: E402
from test_merge_config import merge  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_through_a_wake_server_tools_and_skills_come_from_it(self):
        _, text = merge({"AGENTSHANGOUT_URL": "https://wake.example.com/"})
        self.assertIn("url: https://wake.example.com/mcp", text)
        self.assertNotIn("agentshangout.com/mcp", text)
        self.assertIn("      resources: true", text)
        self.assertIn('Authorization: "Bearer ${AGENTSHANGOUT_TOKEN}"', text)   # the wake key, from Hermes's .env

    def test_straight_to_the_site_nothing_changes(self):
        _, text = merge({})
        self.assertIn("url: https://agentshangout.com/mcp", text)
        self.assertIn("      resources: false", text)

    def test_tool_search_still_works_through_a_wake_server(self):
        _, text = merge({"AGENTSHANGOUT_URL": "https://wake.example.com", "AHA_PROVIDER": "anthropic"})
        self.assertIn("url: https://wake.example.com/mcp", text)
        self.assertIn("      exclude:", text)
        self.assertIn("      resources: true", text)


class ReportTest(unittest.TestCase):
    def test_room_tools_are_read_from_hermes_config(self):
        _, text = merge({"AHA_TOOLS": "use_item"})
        path = pathlib.Path(tempfile.mkdtemp()) / "config.yaml"
        path.write_text(text)
        tools = bridge.room_tools(path)
        self.assertIn("use_item", tools)         # AHA_TOOLS, uncommented in the kit's list
        self.assertIn("inbox", tools)
        self.assertIn("recall", tools)
        self.assertNotIn("open_vote", tools)     # commented out in the kit

    def test_the_report_says_tiers_and_never_a_secret(self):
        report = bridge.setup_report()
        for key in ("host", "capabilities", "room_tools", "tool_search", "extra_tools", "github_write"):
            self.assertIn(key, report)
        text = str(report)
        self.assertNotIn("ah_", text)            # no bot token or wake key
        self.assertNotIn("key", text.lower())

    def test_a_wake_key_turns_on_wake_mode(self):
        self.assertFalse(bridge.WAKE_SERVER)     # ah_bot_ in these tests


if __name__ == "__main__":
    unittest.main()
