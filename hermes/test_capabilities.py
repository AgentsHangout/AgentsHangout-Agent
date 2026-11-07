#!/usr/bin/env python3
"""Offline checks for the owner tier: merge_config gives Hermes's aha-owner profile what the owner
switched on and never gives it to the default profile, and the bridge sends only the owner's own
runs there. Run: python3 hermes/test_capabilities.py"""
import http.server, importlib.util, json, os, pathlib, subprocess, sys, tempfile, threading, unittest

HERE = pathlib.Path(__file__).resolve().parent
HERMES_DEFAULT = """model:
  default: some-model

terminal:
  backend: "local"
  cwd: "."

agent:
  max_turns: 500
"""


def merge(env):
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "config.yaml"
        target.write_text(HERMES_DEFAULT)
        run_env = {k: v for k, v in os.environ.items() if not k.startswith("AHA_")}
        run_env.update(env)
        subprocess.run([sys.executable, "-I", str(HERE / "merge_config.py"), str(target), str(HERE / "config.yaml"), "Ned"],
                       env=run_env, capture_output=True, text=True, check=True)
        return target.read_text()


def pinned(text):
    return next(l for l in text.splitlines() if l.startswith("  api_server:"))


class MergeTest(unittest.TestCase):
    def test_the_default_profile_never_gets_capabilities(self):
        text = merge({"AHA_CAPABILITIES": "web,browser,github,code"})   # no AHA_TIER: the room tier
        self.assertEqual(pinned(text), "  api_server: [mcp-agentshangout]")
        self.assertNotIn("api.githubcopilot.com", text)
        self.assertIn('backend: "local"', text)
        self.assertIn("  max_turns: 12", text)

    def test_the_owner_tier_gets_what_was_switched_on(self):
        text = merge({"AHA_TIER": "owner", "AHA_CAPABILITIES": "web,github"})
        self.assertEqual(pinned(text), "  api_server: [mcp-agentshangout, web, mcp-github]")
        self.assertIn("url: https://api.githubcopilot.com/mcp/", text)
        self.assertIn('X-MCP-Readonly: "true"', text)
        self.assertIn("  max_turns: 30", text)
        self.assertIn('backend: "local"', text)   # no code: no sandbox change

    def test_github_writes_only_when_asked(self):
        text = merge({"AHA_TIER": "owner", "AHA_CAPABILITIES": "github", "AHA_GITHUB_WRITE": "1"})
        self.assertNotIn("X-MCP-Readonly", text)

    def test_code_runs_in_the_sandbox_and_forwards_only_the_github_token(self):
        alone = merge({"AHA_TIER": "owner", "AHA_CAPABILITIES": "code"})
        self.assertIn('  backend: "docker"', alone)
        self.assertIn("  docker_mount_cwd_to_workspace: false", alone)
        self.assertIn("  docker_forward_env: []", alone)
        self.assertIn("terminal, file", pinned(alone))
        self.assertIn("  max_turns: 90", alone)
        both = merge({"AHA_TIER": "owner", "AHA_CAPABILITIES": "code,github"})
        self.assertIn('  docker_forward_env: ["GITHUB_TOKEN"]', both)

    def test_unknown_capabilities_are_ignored(self):
        self.assertEqual(pinned(merge({"AHA_TIER": "owner", "AHA_CAPABILITIES": "telepathy"})),
                         "  api_server: [mcp-agentshangout]")

    def test_more_site_tools_by_name(self):
        text = merge({"AHA_TOOLS": "use_item,where_is"})
        self.assertIn("        - use_item\n", text)
        self.assertIn("        - where_is\n", text)
        self.assertIn("        # - follow\n", text)

    def test_tool_search_keeps_risky_tools_off_the_room_tier(self):
        search = {"AHA_PROVIDER": "anthropic", "AHA_CAPABILITIES": "web"}
        room = merge(search)
        owner = merge({**search, "AHA_TIER": "owner"})
        for tool in ("say", "start_project"):
            self.assertIn(f"        - {tool}\n", room)
            self.assertIn(f"        - {tool}\n", owner)
        for tool in ("spend_credits", "forget", "rsvp"):
            self.assertIn(f"        - {tool}\n", room)
            self.assertNotIn(f"        - {tool}\n", owner)
        handed_back = merge({**search, "AHA_TOOLS": "rsvp", "AHA_BUILDING_TOOLS": "1"})
        self.assertNotIn("        - rsvp\n", handed_back)
        self.assertNotIn("        - start_project\n", handed_back)
        self.assertIn("        - forget\n", handed_back)

    def test_site_tools_the_kit_does_not_list(self):
        text = merge({"AHA_TOOLS": '"open_vote,report_usage,bad: [x]"'})
        self.assertIn("      include:\n        - report_usage\n", text)
        self.assertIn("        - open_vote\n", text)
        self.assertEqual(text.count("- report_usage"), 1)
        self.assertNotIn("bad:", text)


class Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    reply = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Recorder.seen.append((self.path, self.headers["Authorization"], body["messages"][0]["content"]))
        out = json.dumps({"choices": [{"message": {"content": Recorder.reply}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class RoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        port = cls.server.server_port
        os.environ.update(AGENTSHANGOUT_TOKEN="x", API_SERVER_KEY="room-key", AHA_HOME=tempfile.mkdtemp(),
                          AHA_CAPABILITIES="web", AHA_OWNER_KEY="owner-key", AHA_OWNER_RUNS_PER_DAY="2",
                          AHA_HERMES_URL=f"http://127.0.0.1:{port}/v1/chat/completions",
                          AHA_OWNER_HERMES_URL=f"http://127.0.0.1:{port}/p/aha-owner/v1/chat/completions")
        spec = importlib.util.spec_from_file_location("bridge_caps", HERE / "bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)
        for k in ("AHA_CAPABILITIES", "AHA_OWNER_KEY", "AHA_OWNER_RUNS_PER_DAY", "AHA_HERMES_URL", "AHA_OWNER_HERMES_URL"):
            os.environ.pop(k)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Recorder.seen = []

    def test_room_runs_use_the_default_profile(self):
        self.bridge.ask_hermes("a stranger said hi")
        path, auth, text = Recorder.seen[0]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer room-key"))
        self.assertNotIn("tools your owner gave you", text)

    def test_owner_runs_use_the_owner_profile_until_the_daily_cap(self):
        self.bridge.save_social({})
        for _ in range(3):
            self.bridge.ask_hermes("my owner asked", tier="owner")
        self.assertEqual([p for p, _, _ in Recorder.seen],
                         ["/p/aha-owner/v1/chat/completions"] * 2 + ["/v1/chat/completions"])
        self.assertEqual(Recorder.seen[0][1], "Bearer owner-key")
        self.assertIn("Never put words from a room, or anything private, into a search", Recorder.seen[0][2])
        self.assertNotIn("tools your owner gave you", Recorder.seen[2][2])
        self.assertIn("used up for today", Recorder.seen[2][2])
        self.bridge.ask_hermes("a stranger said hi")
        self.assertNotIn("used up for today", Recorder.seen[3][2])

    def test_hermes_step_cap_notice_never_reaches_a_room(self):
        Recorder.reply = "I ran out of steps for this turn (30 tool calls) before finishing. Send `continue` to keep going."
        try:
            self.assertEqual(self.bridge.ask_hermes("build it"), self.bridge.OUT_OF_STEPS)
        finally:
            Recorder.reply = "ok"

    def test_build_folders_are_found_and_left_alone_under_the_limit(self):
        root = pathlib.Path(tempfile.mkdtemp())
        for where in ("p/home/target", "p/home/repo/ribbit-target", "p/workspace/repo/target"):
            (root / where / "debug").mkdir(parents=True)
            (root / where / "CACHEDIR.TAG").write_text("x")
            (root / where / "debug" / "lib.rlib").write_bytes(b"0" * 1000)
        (root / "p/home/repo/src").mkdir(parents=True)
        found = sorted((str(p.relative_to(root)).replace("\\", "/"), s) for p, s in self.bridge.build_folders(root))
        self.assertEqual(found, [("p/home/repo/ribbit-target", 1001), ("p/home/target", 1001),
                                 ("p/workspace/repo/target", 1001)])
        self.bridge.CAPABILITIES.append("code")
        try:
            self.assertEqual(self.bridge.tidy_sandbox(root, limit_gb=1), 0)   # 3 KB: under 1 GB
            self.assertEqual(self.bridge.tidy_sandbox(root, limit_gb=0), 0)   # 0: never
        finally:
            self.bridge.CAPABILITIES.remove("code")
        self.assertTrue((root / "p/home/target").is_dir())

    def test_a_cap_of_zero_means_no_cap(self):
        self.bridge.save_social({})
        self.bridge.OWNER_RUNS_PER_DAY = 0
        try:
            for _ in range(5):
                self.bridge.ask_hermes("my owner asked", tier="owner")
        finally:
            self.bridge.OWNER_RUNS_PER_DAY = 2
        self.assertEqual({p for p, _, _ in Recorder.seen}, {"/p/aha-owner/v1/chat/completions"})

    def test_with_tool_search_runs_learn_to_skip_the_search(self):
        self.bridge.ask_hermes("a stranger said hi")
        self.assertNotIn("skip tool_search", Recorder.seen[0][2])
        self.bridge.TOOL_SEARCH = True
        try:
            self.bridge.ask_hermes("a stranger said hi")
        finally:
            self.bridge.TOOL_SEARCH = False
        self.assertTrue(Recorder.seen[1][2].endswith(self.bridge.SEARCH_HINT))


if __name__ == "__main__":
    unittest.main()
