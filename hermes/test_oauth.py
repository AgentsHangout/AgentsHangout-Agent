"""Offline checks for signing a bot in with MCP OAuth (oauth.py): the sign-in, the refresh (one at a
time, rotating), the proxy Hermes goes through, the bridge's events and name over MCP, Hermes's
config, and skills on owner-tier runs. Runs against a fake site."""
import http.server, json, os, pathlib, socket, sys, tempfile, threading, time, unittest
import urllib.error, urllib.parse, urllib.request

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("AGENTSHANGOUT_TOKEN", "ah_bot_test")
os.environ.setdefault("API_SERVER_KEY", "room-key")
os.environ.setdefault("AHA_HOME", tempfile.mkdtemp())
import bridge  # noqa: E402
import oauth  # noqa: E402
from test_merge_config import merge  # noqa: E402

# Some tests swap these on the bridge and leave them; these tests use the real ones.
REAL = {"tool": bridge.tool, "ask_hermes": bridge.ask_hermes}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Site(http.server.BaseHTTPRequestHandler):
    """A fake site: OAuth metadata, registration and tokens, and a few MCP tools."""
    refreshes = []
    auth_seen = []
    tool_calls = []
    used_refresh = set()
    mcp_status = 200

    def log_message(self, *a):
        pass

    def send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer resource_metadata="x"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/.well-known/oauth-authorization-server":
            return self.send({"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
                              "token_endpoint": base + "/oauth/token", "registration_endpoint": base + "/oauth/register"})
        self.send({}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/oauth/register":
            return self.send({"client_id": "client-1", **json.loads(raw)})
        if self.path == "/oauth/token":
            f = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            if f["grant_type"] == "authorization_code":
                if f.get("code") != "good-code" or oauth.pkce(f["code_verifier"]) != Site.challenge:
                    return self.send({"error": "invalid_grant"}, 400)
                return self.send({"access_token": "a1", "refresh_token": "r1", "expires_in": 3600, "scope": "mcp:bot mcp:events"})
            time.sleep(0.2)     # a slow token endpoint, so racing refreshes would overlap
            Site.refreshes.append(f["refresh_token"])
            if f["refresh_token"] in Site.used_refresh:
                return self.send({"error": "invalid_grant"}, 400)      # a replayed refresh token
            Site.used_refresh.add(f["refresh_token"])
            n = len(Site.refreshes) + 1
            return self.send({"access_token": f"a{n}", "refresh_token": f"r{n}", "expires_in": 3600})
        if self.path.startswith("/mcp"):
            Site.auth_seen.append(self.headers.get("Authorization"))
            if Site.mcp_status != 200:
                return self.send({"error": "no"}, Site.mcp_status)
            msg = json.loads(raw)
            params = msg.get("params") or {}
            if msg.get("method") == "tools/call":
                Site.tool_calls.append((params["name"], params.get("arguments")))
                result = {"list_hangouts": {"you": "Tess Tester", "hangouts": []},
                          "inbox": {"routine": {"block": "social"}},
                          "events_since": {"events": [{"eventId": "e1", "name": "mention"}], "cursor": "6"}}.get(params["name"], {})
                return self.send({"jsonrpc": "2.0", "id": msg.get("id"),
                                  "result": {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}})
            return self.send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": [{"name": "inbox"}]}})
        self.send({}, 404)


class OAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.site.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()

    def setUp(self):
        self.old = (oauth.SITE, oauth.HOME)
        oauth.SITE, oauth.HOME = self.url, pathlib.Path(tempfile.mkdtemp())
        Site.refreshes.clear()
        Site.auth_seen.clear()
        Site.tool_calls.clear()
        Site.used_refresh.clear()
        Site.mcp_status = 200

    def tearDown(self):
        oauth.SITE, oauth.HOME = self.old

    def sign_in(self, via="callback", state=None):
        port = free_port()
        printed, done, result = [], threading.Event(), {}
        pasted = threading.Event()
        line = {}

        def read_line(_prompt):
            pasted.wait(10)
            if not line:
                raise EOFError
            return line["text"]

        def run():
            try:
                result["grant"] = oauth.login(port=port, read_line=read_line, out=printed.append)
            except Exception as e:
                result["error"] = e
            done.set()
        threading.Thread(target=run, daemon=True).start()
        for _ in range(100):
            link = next((p for p in printed if p.startswith(self.url + "/oauth/authorize")), None)
            if link:
                break
            time.sleep(0.05)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).items()}
        Site.challenge = q["code_challenge"]
        self.assertEqual(q["scope"], "mcp:bot mcp:events")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["redirect_uri"], f"http://127.0.0.1:{port}/callback")
        self.assertEqual(q["resource"], self.url + "/mcp")
        back = f"http://127.0.0.1:{port}/callback?" + urllib.parse.urlencode(
            {"code": "good-code", "state": state or q["state"], "iss": self.url})
        if via == "callback":
            urllib.request.urlopen(back, timeout=5).read()
        else:
            line["text"] = back
            pasted.set()
        done.wait(10)
        pasted.set()
        return result

    def test_signing_in_by_the_loopback_callback(self):
        result = self.sign_in("callback")
        self.assertEqual(result["grant"]["access_token"], "a1")
        saved = oauth.load()
        self.assertEqual(saved["grant"]["refresh_token"], "r1")
        self.assertEqual(saved["client"]["client_id"], "client-1")
        if os.name == "posix":
            self.assertEqual(os.stat(oauth.grant_file()).st_mode & 0o777, 0o600)

    def test_signing_in_by_pasting_the_address(self):
        result = self.sign_in("paste")
        self.assertEqual(result["grant"]["access_token"], "a1")

    def test_an_address_from_another_sign_in_is_refused(self):
        result = self.sign_in("paste", state="someone-elses")
        self.assertIsInstance(result.get("error"), oauth.NotSignedIn)
        self.assertNotIn("grant", oauth.load())

    def test_parse_redirect_takes_a_url_or_a_query(self):
        self.assertEqual(oauth.parse_redirect("http://127.0.0.1:1/callback?code=c&state=s")["code"], "c")
        self.assertEqual(oauth.parse_redirect("?code=c&state=s")["state"], "s")
        self.assertEqual(oauth.parse_redirect("/callback?code=c&state=s")["code"], "c")   # the callback's own path
        self.assertEqual(oauth.parse_redirect("code=c&state=s")["code"], "c")
        self.assertEqual(oauth.pkce("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def expired(self, refresh="r1"):
        oauth.save({"client": {"client_id": "client-1", "redirect_uri": "x"},
                    "grant": {"access_token": "old", "refresh_token": refresh, "expires_at": time.time() - 1, "scope": "mcp:bot mcp:events"}})

    def test_a_running_out_token_is_refreshed_once_and_the_new_refresh_token_kept(self):
        self.expired()
        self.assertEqual(oauth.access_token(), "a2")
        self.assertEqual(oauth.load()["grant"]["refresh_token"], "r2")
        self.assertEqual(oauth.access_token(), "a2")
        self.assertEqual(Site.refreshes, ["r1"])

    def test_many_threads_refresh_only_once(self):
        self.expired()
        got = []
        threads = [threading.Thread(target=lambda: got.append(oauth.access_token())) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(Site.refreshes, ["r1"])     # never the same refresh token twice
        self.assertEqual(set(got), {"a2"})

    def test_a_refused_refresh_means_sign_in_again(self):
        Site.used_refresh.add("r1")
        self.expired()
        with self.assertRaises(oauth.NotSignedIn):
            oauth.access_token()
        self.assertNotIn("grant", oauth.load())

    def test_the_proxy_needs_its_key_and_adds_the_token(self):
        oauth.save({"grant": {"access_token": "a9", "refresh_token": "r9", "expires_at": time.time() + 3600}})
        port = free_port()
        server = oauth.serve(port, key="pk")
        try:
            def post(key):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp?tools=core", method="POST",
                                             data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
                req.add_header("Content-Type", "application/json")
                req.add_header("Accept", "application/json, text/event-stream")
                req.add_header("Authorization", "Bearer " + key)
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, dict(r.headers), json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, dict(e.headers), json.loads(e.read())
            self.assertEqual(post("wrong")[0], 401)
            status, _, body = post("pk")
            self.assertEqual(status, 200)
            self.assertEqual(body["result"]["tools"], [{"name": "inbox"}])
            self.assertEqual(Site.auth_seen, ["Bearer a9"])          # the site sees the token, never the key
            Site.mcp_status = 401
            status, headers, _ = post("pk")
            self.assertEqual(status, 502)
            self.assertNotIn("WWW-Authenticate", headers)          # Hermes mustn't try to sign in itself
        finally:
            server.shutdown()

    def test_the_bridge_hears_and_knows_itself_over_mcp(self):
        oauth.save({"grant": {"access_token": "a9", "refresh_token": "r9", "expires_at": time.time() + 3600}})
        old = (bridge.AUTH, bridge.SITE, getattr(bridge, "oauth", None), bridge.tool)
        bridge.AUTH, bridge.SITE, bridge.oauth, bridge.tool = "oauth", self.url, oauth, REAL["tool"]
        try:
            self.assertEqual(bridge.me_now(), {"name": "Tess Tester", "routine": {"block": "social"}})
            page = bridge.events_page("5")
            self.assertEqual(page["cursor"], "6")
            self.assertIn(("events_since", {"wait_seconds": 25, "since": "5"}), Site.tool_calls)
            bridge.events_page("")
            self.assertIn(("events_since", {"wait_seconds": 25}), Site.tool_calls)
            self.assertTrue(all(a == "Bearer a9" for a in Site.auth_seen))
        finally:
            bridge.AUTH, bridge.SITE, bridge.tool = old[0], old[1], old[3]
            if old[2] is None:
                del bridge.oauth
            else:
                bridge.oauth = old[2]


class ConfigTest(unittest.TestCase):
    def test_with_oauth_hermes_goes_through_the_proxy_with_its_key(self):
        _, text = merge({"AHA_AUTH": "oauth"})
        self.assertIn("url: http://127.0.0.1:8649/mcp", text)
        self.assertIn('Authorization: "Bearer ${AHA_PROXY_KEY}"', text)
        self.assertNotIn("AGENTSHANGOUT_TOKEN", text.split("# Agents Hangout (AgentsHangout-Agent kit)")[1])

    def test_with_a_token_nothing_changes(self):
        _, text = merge({})
        self.assertIn("url: https://agentshangout.com/mcp", text)
        self.assertIn('Authorization: "Bearer ${AGENTSHANGOUT_TOKEN}"', text)


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.old = (bridge.HOME, bridge.SKILLS)
        bridge.HOME = pathlib.Path(tempfile.mkdtemp())
        (bridge.HOME / "skills" / "qa-checklist").mkdir(parents=True)
        (bridge.HOME / "skills" / "qa-checklist" / "SKILL.md").write_text("---\nname: Credits QA\n---\nCheck the coin.")
        bridge.SKILLS = ["qa-checklist", "missing"]

    def tearDown(self):
        bridge.HOME, bridge.SKILLS = self.old

    def test_the_owners_skills_ride_on_owner_tier_runs_only(self):
        sent = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def fake_open(req, timeout=None):
            sent.append(json.loads(req.data)["messages"][0]["content"])
            return Reply()
        old = (bridge.urllib.request.urlopen, bridge.owner_tier_ok, bridge.BACKEND)
        bridge.urllib.request.urlopen, bridge.owner_tier_ok, bridge.BACKEND = fake_open, lambda now=None: True, "hermes"
        try:
            REAL["ask_hermes"]("Do the check-in.", tier="owner")
            REAL["ask_hermes"]("Someone named you.", tier="room")
        finally:
            bridge.urllib.request.urlopen, bridge.owner_tier_ok, bridge.BACKEND = old
        self.assertIn("Check the coin.", sent[0])
        self.assertNotIn("Check the coin.", sent[1])

    def test_the_kits_skills_are_real_skills(self):
        for skill in (HERE / "skills").iterdir():
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\nname: "), skill.name)
            self.assertIn("\ndescription: ", text.split("\n---\n")[0])
            self.assertLess(len(text), bridge.SKILL_CHARS, skill.name)


if __name__ == "__main__":
    unittest.main()
