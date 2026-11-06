"""Offline checks for manage.py, what the bot manager runs on a bot's machine: only listed settings,
each checked; secrets never printed; and the settings reach setup.sh."""
import json, os, pathlib, sys, tempfile, unittest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
import manage  # noqa: E402


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.env = pathlib.Path(tempfile.mkdtemp()) / "env"
        self.env.write_text("AHA_HOME=/ssd/aha\nAGENTSHANGOUT_TOKEN=ah_bot_secret\nAHA_TOOLS=use_item\n")

    def test_a_change_rewrites_only_its_line(self):
        changed = manage.write_settings({"AHA_CAPABILITIES": "web, github", "AHA_ROLE": "the town's reporter"}, self.env)
        self.assertEqual(changed, {"AHA_CAPABILITIES": "web,github", "AHA_ROLE": "the town's reporter"})
        text = self.env.read_text()
        self.assertIn("AGENTSHANGOUT_TOKEN=ah_bot_secret\n", text)
        self.assertIn('AHA_ROLE="the town\'s reporter"', text)
        self.assertEqual(manage.read_env(self.env)["AHA_ROLE"], "the town's reporter")
        if os.name == "posix":
            self.assertEqual(os.stat(self.env).st_mode & 0o777, 0o600)

    def test_empty_removes_the_setting(self):
        manage.write_settings({"AHA_TOOLS": ""}, self.env)
        self.assertNotIn("AHA_TOOLS", self.env.read_text())

    def test_nothing_unlisted_or_unsafe_is_written(self):
        before = self.env.read_text()
        for bad in ({"AGENTSHANGOUT_TOKEN": "x"}, {"AHA_HOME": "/tmp"}, {"AHA_CAPABILITIES": "web,shell"},
                    {"AHA_ROLE": 'a "quote"'}, {"AHA_GOAL": "costs $5"}, {"AHA_GOAL": "a `cmd`"},
                    {"AHA_SCHEDULE": "8am"}, {"AHA_AUTH": "password"}, {"AHA_TOOLS": "rm -rf"},
                    {"AHA_OWNER_RUNS_PER_DAY": "lots"}, {"AHA_ROLE": "ok", "AHA_SKILLS": "../x"}):
            with self.assertRaises(ValueError, msg=bad):
                manage.write_settings(bad, self.env)
        self.assertEqual(self.env.read_text(), before)     # all or nothing

    def test_a_new_line_in_text_becomes_a_space(self):
        manage.write_settings({"AHA_GOAL": "two\nlines"}, self.env)
        self.assertIn('AHA_GOAL="two lines"\n', self.env.read_text())

    def test_setup_gets_every_saved_setting_and_only_listed_secrets(self):
        run_env = manage.setup_env(manage.read_env(self.env), {"GITHUB_TOKEN": "github_pat_x ", "PATH_HIJACK": "x"})
        self.assertEqual(run_env["AHA_HOME"], "/ssd/aha")
        self.assertEqual(run_env["AHA_TOOLS"], "use_item")
        self.assertEqual(run_env["GITHUB_TOKEN"], "github_pat_x")
        self.assertNotIn("PATH_HIJACK", run_env)


class StatusTest(unittest.TestCase):
    def test_status_says_whether_secrets_are_there_but_never_what_they_are(self):
        home = pathlib.Path(tempfile.mkdtemp())
        aha = home / "aha"
        (aha / "hermes-home" / "profiles" / "aha-owner").mkdir(parents=True)
        (aha / "hermes-home" / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret\nAPI_SERVER_KEY=k-secret\n")
        (aha / "hermes-home" / "profiles" / "aha-owner" / ".env").write_text("GITHUB_TOKEN=github_pat_secret\n")
        (aha / "oauth.json").write_text(json.dumps({"grant": {"access_token": "ah_oauth_secret", "refresh_token": "ah_refresh_secret",
                                                              "expires_at": 1, "scope": "mcp:bot mcp:events"}}))
        (aha / "social-state.json").write_text(json.dumps({"runs": [{"at": 1, "asked": "a", "said": "b"}], "lessons": ["l"]}))
        (aha / "hermes-home" / "config.yaml").write_text(
            "x: 1\n\n# Agents Hangout (AgentsHangout-Agent kit)\nmcp_servers:\n  agentshangout:\n    tools:\n      include:\n        - inbox\n        # - open_vote\n        - recall\n      resources: false\n")
        env = home / "env"
        env.write_text(f"AHA_HOME={aha}\nAGENTSHANGOUT_TOKEN=ah_bot_secret\nAHA_AUTH=oauth\nAHA_CAPABILITIES=web\n")
        old = (manage.ENV_FILE, manage.bot_name)
        manage.ENV_FILE, manage.bot_name = env, lambda env, aha_home: "Town Square Hermes"
        try:
            st = manage.status()
        finally:
            manage.ENV_FILE, manage.bot_name = old
        text = json.dumps(st)
        for value in ("ah_bot_secret", "sk-ant-secret", "k-secret", "github_pat_secret", "ah_oauth_secret", "ah_refresh_secret"):
            self.assertNotIn(value, text)
        self.assertEqual(st["secrets"], {"AGENTSHANGOUT_TOKEN": True, "ANTHROPIC_API_KEY": True, "GITHUB_TOKEN": True})
        self.assertEqual(st["signin"]["mode"], "oauth")
        self.assertTrue(st["signin"]["oauth"])
        self.assertEqual(st["room_tools"], {"listed": ["inbox", "recall"]})
        self.assertEqual(st["settings"]["AHA_CAPABILITIES"], "web")
        self.assertEqual(st["bot"], "Town Square Hermes")
        self.assertEqual(st["runs"][0]["said"], "b")
        self.assertIn("qa-checklist", [s["id"] for s in st["skills"]])


if __name__ == "__main__":
    unittest.main()
