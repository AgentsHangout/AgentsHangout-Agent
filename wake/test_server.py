"""Offline checks for the wake server: webhook signatures, skills over MCP, the relay a machine reads,
and a whole run against a fake site (subscribe with a real callback challenge, a webhook, the poll,
the MCP proxy, a command wake, and the owner's page)."""
import base64, http.cookiejar, http.server, json, os, pathlib, socket, sys, tempfile, threading, time, unittest
import urllib.error, urllib.parse, urllib.request


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SITE_PORT, WAKE_PORT = free_port(), free_port()
HOME = tempfile.mkdtemp()
os.environ.update({"AGENTSHANGOUT_URL": f"http://127.0.0.1:{SITE_PORT}", "WAKE_HOME": HOME,
                   "WAKE_PUBLIC_URL": f"http://127.0.0.1:{WAKE_PORT}", "WAKE_ALLOW_HTTP_HOOKS": "1",
                   "WAKE_PASSWORD": "letmein", "WAKE_LISTEN": f"127.0.0.1:{WAKE_PORT}"})
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import server  # noqa: E402

server.BACKSTOP = 0.5

SECRET = server.new_secret()


class SignatureTest(unittest.TestCase):
    def headers(self, secret, body, at, msg_id="evt_1"):
        return {"webhook-id": msg_id, "webhook-timestamp": str(int(at)),
                "webhook-signature": server.sign(secret, msg_id, int(at), body)}

    def test_a_fresh_signature_passes(self):
        body = b'{"eventId":"evt_1"}'
        self.assertTrue(server.verify(SECRET, self.headers(SECRET, body, time.time()), body))

    def test_another_secret_an_edited_body_or_an_old_stamp_fails(self):
        body = b'{"eventId":"evt_1"}'
        self.assertFalse(server.verify(SECRET, self.headers(server.new_secret(), body, time.time()), body))
        self.assertFalse(server.verify(SECRET, self.headers(SECRET, body, time.time()), body + b" "))
        self.assertFalse(server.verify(SECRET, self.headers(SECRET, body, time.time() - 600), body))

    def test_one_good_signature_among_several_passes(self):
        body = b"{}"
        h = self.headers(SECRET, body, time.time())
        h["webhook-signature"] = "v1,bm90IGl0 " + h["webhook-signature"]
        self.assertTrue(server.verify(SECRET, h, body))

    def test_the_secret_decodes_to_32_bytes(self):
        self.assertEqual(len(base64.b64decode(SECRET[len("whsec_"):])), 32)

    def test_pkce_matches_rfc_7636(self):
        self.assertEqual(server.pkce("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_refresh_before_is_read_as_utc(self):
        self.assertEqual(server.iso_epoch("2026-01-01T00:00:00Z"), 1767225600)


class SkillsTest(unittest.TestCase):
    def setUp(self):
        server.save_skill("ned", "economy-review", "---\nname: Economy review\ndescription: Look for farming and races\n---\n\nSteps")
        server.save_skill("_all", "house-rules", "---\nname: House rules\ndescription: For every bot\n---\n")
        server.save_skill("_all", "economy-review", "---\nname: Shared one\n---\n")

    def test_a_bot_gets_its_own_skills_then_everyones_and_its_own_wins_a_clash(self):
        names = {r["name"]: r for r in server.skill_resources("ned")}
        self.assertEqual(set(names), {"economy-review", "house-rules"})
        self.assertEqual(names["economy-review"]["title"], "Economy review")
        self.assertEqual(names["economy-review"]["uri"], "skill://economy-review/SKILL.md")
        self.assertEqual([r["name"] for r in server.skill_resources("other")], ["economy-review", "house-rules"])

    def test_reading_stays_inside_the_skill(self):
        self.assertIn("Steps", server.read_skill("ned", "skill://economy-review/SKILL.md")["text"])
        self.assertIsNone(server.read_skill("ned", "skill://economy-review/../../../wake.db"))
        self.assertIsNone(server.read_skill("ned", "skill://nope/SKILL.md"))

    def test_initialize_says_there_are_resources(self):
        result = server.patch_initialize({"capabilities": {"tools": {}}, "instructions": "Site rules."})
        self.assertIn("resources", result["capabilities"])
        self.assertTrue(result["instructions"].startswith("Site rules."))
        self.assertIn("skill://", result["instructions"])


class BodyTest(unittest.TestCase):
    def test_sse_round_trip(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}]
        raw = server.render_body("text/event-stream", msgs)
        self.assertEqual(server.parse_body("text/event-stream", raw), msgs)
        self.assertEqual(server.parse_body("application/json", server.render_body("application/json", msgs)), msgs)


class RelayStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = server.Store(pathlib.Path(tempfile.mkdtemp()) / "w.db")
        self.store.add_bot("ned")

    def test_an_event_is_kept_once(self):
        self.assertTrue(self.store.add_event("ned", {"eventId": "e1", "name": "mention"}, "poll"))
        self.assertIsNone(self.store.add_event("ned", {"eventId": "e1", "name": "mention"}, "webhook"))
        self.assertEqual(self.store.head("ned"), 1)

    def test_without_a_cursor_only_the_last_days_actionable_events(self):
        self.store.add_event("ned", {"eventId": "e1", "name": "message"}, "poll")
        self.store.add_event("ned", {"eventId": "e2", "name": "mention"}, "poll")
        self.assertEqual([e["eventId"] for _, e in self.store.recent_actionable("ned")], ["e2"])


# ---- A fake site -----------------------------------------------------------------------------------

class FakeSite(http.server.BaseHTTPRequestHandler):
    seen_auth = []
    challenges = []
    polls = {"n": 0}
    delivered = False

    def log_message(self, *a):
        pass

    def send(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeSite.seen_auth.append(self.headers.get("Authorization"))
        if self.path == "/api/me":
            return self.send({"name": "Newsy Ned", "routine": {"block": "social"}})
        self.send({}, 404)

    def do_POST(self):
        FakeSite.seen_auth.append(self.headers.get("Authorization"))
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/api/doorbell"):
            return self.send({})
        msg = json.loads(raw)
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        ok = lambda result, extra=None: self.send({"jsonrpc": "2.0", "id": rid, "result": result}, 200, extra)
        if method == "initialize":
            return ok({"protocolVersion": "2025-11-25", "capabilities": {"tools": {}, "events": {}}, "instructions": "Site rules."},
                      {"Mcp-Session-Id": "sess-1"})
        if method == "resources/list":
            return ok({"resources": [{"uri": "guide://agentshangout/how-to-play", "name": "how-to-play"}]})
        if method == "tools/list":
            return ok({"tools": [{"name": "inbox"}]})
        if method == "events/subscribe":
            # Check the callback the way the site does: a signed challenge it must echo.
            d = params["delivery"]
            challenge = "ch-" + params["name"]
            body = json.dumps({"type": "verification", "challenge": challenge}).encode()
            stamp = int(time.time())
            req = urllib.request.Request(d["url"], data=body, method="POST")
            for k, v in {"Content-Type": "application/json", "webhook-id": "msg_v_" + params["name"], "webhook-timestamp": str(stamp),
                         "webhook-signature": server.sign(d["secret"], "msg_v_" + params["name"], stamp, body),
                         "X-MCP-Subscription-Id": "sub_" + params["name"]}.items():
                req.add_header(k, v)
            echoed = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if echoed.get("challenge") != challenge:
                return self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32015, "message": "challenge failed"}})
            FakeSite.challenges.append((params["name"], d["url"], d["secret"]))
            until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return ok({"id": "sub_" + params["name"], "refreshBefore": until, "cursor": None, "truncated": False})
        if method == "tools/call":
            name, args = params["name"], params.get("arguments") or {}
            if name == "events_since":
                FakeSite.polls["n"] += 1
                if "since" not in args:
                    page = {"events": [{"eventId": "old", "name": "mention", "data": {}}], "cursor": "10"}
                elif args["since"] == "10" and not FakeSite.delivered:
                    FakeSite.delivered = True
                    page = {"events": [{"eventId": "e1", "name": "mention", "data": {"hangout_id": "r1"}}], "cursor": "11"}
                else:
                    time.sleep(0.2)
                    page = {"events": [], "cursor": args["since"]}
                return ok({"content": [{"type": "text", "text": json.dumps(page)}], "structuredContent": page})
            if name == "inbox":
                return ok({"content": [{"type": "text", "text": "{}"}], "structuredContent": {"routine": {"block": "work"}}})
            if name == "list_hangouts":
                return ok({"content": [{"type": "text", "text": "{}"}], "structuredContent": {"you": "Tess Tester", "hangouts": []}})
        self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}})


def call(method, path, body=None, key=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{WAKE_PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


def wait_for(check, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", SITE_PORT), FakeSite)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.store = server.Store(pathlib.Path(HOME) / "wake.db")
        site = server.Site(cls.store)
        cls.wakes = server.Wakes(cls.store, site)
        cls.httpd = server.serve(cls.store, site, cls.wakes)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.key = cls.store.add_bot("ned", bot_token="ah_bot_ned")
        server.save_skill("ned", "economy-review", "---\nname: Economy review\ndescription: Farming and races\n---\nSteps")
        cls.wakes.start("ned")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.site.shutdown()

    def test_1_it_subscribes_every_wake_event_with_a_verified_callback(self):
        self.assertTrue(wait_for(lambda: len({c[0] for c in FakeSite.challenges}) == len(server.WAKE)))
        hook = self.store.bot("ned")["hook"]
        self.assertTrue(all(s.get("until", 0) > time.time() for s in hook["subs"].values()))

    def test_2_the_poll_starts_from_now_and_its_events_reach_the_machine_once(self):
        self.assertTrue(wait_for(lambda: self.store.head("ned") >= 1))
        status, _, page = call("GET", "/api/events?since=0&wait=1", key=self.key)
        self.assertEqual(status, 200)
        ids = [e["eventId"] for e in page["events"]]
        self.assertIn("e1", ids)
        self.assertNotIn("old", ids)        # the first page without a cursor is history, not a wake
        wakes = self.store.wakes("ned")
        self.assertTrue(any(w["event_id"] == "e1" and w["outcome"].startswith("picked up") for w in wakes))

    def test_3_a_webhook_wakes_it_and_a_repeat_is_dropped(self):
        self.assertTrue(wait_for(lambda: FakeSite.challenges))
        _, url, secret = FakeSite.challenges[0]
        event = {"eventId": "e2", "name": "owner_answer", "timestamp": "2026-11-05T12:00:00Z", "data": {"text": "go"}, "cursor": "12"}
        body = json.dumps(event).encode()
        for _ in range(2):
            stamp = int(time.time())
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("webhook-id", "e2")
            req.add_header("webhook-timestamp", str(stamp))
            req.add_header("webhook-signature", server.sign(secret, "e2", stamp, body))
            self.assertEqual(urllib.request.urlopen(req, timeout=10).status, 204)
        _, _, page = call("GET", "/api/events?since=0&wait=0", key=self.key)
        self.assertEqual([e["eventId"] for e in page["events"]].count("e2"), 1)
        bad = urllib.request.Request(url, data=body, method="POST")
        bad.add_header("webhook-id", "e2")
        bad.add_header("webhook-timestamp", str(int(time.time())))
        bad.add_header("webhook-signature", "v1,AAAA")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad, timeout=10)
        self.assertEqual(e.exception.code, 401)

    def test_4_a_long_poll_answers_when_an_event_arrives(self):
        head = self.store.head("ned")
        threading.Timer(0.5, lambda: self.wakes.take("ned", {"eventId": "e3", "name": "mention"}, "webhook")).start()
        started = time.time()
        _, _, page = call("GET", f"/api/events?since={head}&wait=10", key=self.key)
        self.assertLess(time.time() - started, 5)
        self.assertIn("e3", [e["eventId"] for e in page["events"]])
        self.assertEqual(page["cursor"], str(self.store.head("ned")))
        # A cursor from the site itself is far ahead of this server's: it means "from now".
        _, _, page = call("GET", "/api/events?since=99999999&wait=0", key=self.key)
        self.assertEqual(page["events"], [])

    def test_5_mcp_goes_to_the_site_as_the_bot_and_adds_its_skills(self):
        status, headers, init = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}, self.key)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Mcp-Session-Id"), "sess-1")
        self.assertIn("resources", init["result"]["capabilities"])
        self.assertIn("events", init["result"]["capabilities"])
        _, _, listed = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 8, "method": "resources/list"}, self.key)
        uris = [r["uri"] for r in listed["result"]["resources"]]
        self.assertEqual(uris, ["skill://economy-review/SKILL.md", "guide://agentshangout/how-to-play"])
        _, _, read = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 9, "method": "resources/read",
                                           "params": {"uri": "skill://economy-review/SKILL.md"}}, self.key)
        self.assertIn("Steps", read["result"]["contents"][0]["text"])
        _, _, tools = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}, self.key)
        self.assertEqual(tools["result"]["tools"], [{"name": "inbox"}])
        self.assertNotIn("Bearer " + self.key, FakeSite.seen_auth)   # the wake key never reaches the site
        self.assertIn("Bearer ah_bot_ned", FakeSite.seen_auth)

    def test_6_a_wrong_key_gets_nothing(self):
        for path in ("/mcp", "/api/events", "/api/me"):
            status, headers, _ = call("POST" if path == "/mcp" else "GET", path, {} if path == "/mcp" else None, "ah_wake_wrong")
            self.assertEqual(status, 401)
            self.assertNotIn("WWW-Authenticate", headers)

    def test_7_me_and_the_setup_report(self):
        _, _, me = call("GET", "/api/me", key=self.key)
        self.assertEqual(me["name"], "Newsy Ned")
        status, _, _ = call("POST", "/api/wake/setup", {"host": "jetson", "capabilities": ["web"]}, self.key)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.bot("ned")["setup"]["host"], "jetson")
        self.assertEqual(self.store.bot("ned")["display"], "Newsy Ned")

    def test_7b_an_oauth_bot_gets_its_name_and_routine_over_mcp(self):
        key = self.store.add_bot("tess")
        self.store.update("tess", oauth={"access_token": "at_tess", "refresh_token": "rt", "expires_at": time.time() + 3600})
        _, _, me = call("GET", "/api/me", key=key)
        self.assertEqual(me, {"name": "Tess Tester", "routine": {"block": "work"}})
        self.assertIn("Bearer at_tess", FakeSite.seen_auth)
        status, _, body = call("POST", "/api/doorbell", {}, key)
        self.assertEqual(status, 200)          # OAuth has no REST doorbell: it's only noted here
        status, _, body = call("GET", "/api/worlds/commons", key=key)
        self.assertEqual(status, 403)           # nor the walker's REST calls
        self.assertIn("OAuth", body["error"])

    def test_8_a_command_bot_runs_its_command_with_the_event(self):
        out = pathlib.Path(tempfile.mkdtemp()) / "event.json"
        script = f"import sys,os; open({out.as_posix()!r},'w').write(os.environ['AHA_EVENT_NAME'] + sys.stdin.read())"
        self.store.add_bot("cmd", bot_token="ah_bot_cmd", target="command",
                           command=f"'{pathlib.Path(sys.executable).as_posix()}' -c \"{script}\"")
        self.wakes.take("cmd", {"eventId": "c1", "name": "check_in"}, "webhook")
        self.assertTrue(wait_for(lambda: out.exists() and out.read_text()))
        self.assertTrue(out.read_text().startswith("check_in{"))
        self.assertTrue(wait_for(lambda: self.store.wakes("cmd")[0]["outcome"].startswith("ran, exit 0")))

    def test_9_the_page_needs_the_password_and_a_form_token(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        base = f"http://127.0.0.1:{WAKE_PORT}"
        self.assertIn("Password", opener.open(base + "/").read().decode())
        opener.open(base + "/login", data=b"password=wrong").read()
        self.assertFalse(list(jar))
        home = opener.open(base + "/login", data=b"password=letmein").read().decode()
        self.assertIn("Newsy Ned", home)
        with self.assertRaises(urllib.error.HTTPError) as e:
            opener.open(base + "/bots", data=b"name=sneaky&csrf=nope")
        self.assertEqual(e.exception.code, 403)
        csrf = home.split("name=\"csrf\" value=\"")[1].split('"')[0]
        added = opener.open(base + "/bots", data=urllib.parse.urlencode({"name": "tester", "csrf": csrf}).encode()).read().decode()
        self.assertIn("ah_wake_", added)
        self.assertIsNotNone(self.store.bot("tester"))
        page = opener.open(base + "/bots/ned").read().decode()
        self.assertIn("economy-review", page)
        self.assertIn("jetson", page)


if __name__ == "__main__":
    unittest.main()
