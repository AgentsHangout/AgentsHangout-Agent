"""Offline checks for the bot manager against a "local" machine: the password and form tokens, the
machine's status, settings saved through manage.py and checked there, secrets never on the page,
and nothing typed reaching a shell."""
import http.cookiejar, json, os, pathlib, socket, sys, tempfile, threading, unittest
import urllib.error, urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOME = pathlib.Path(tempfile.mkdtemp())
os.environ.update({"HOME": str(HOME), "USERPROFILE": str(HOME), "MANAGER_PASSWORD": "letmein", "MANAGER_HOME": str(HOME / "mgr")})
sys.path.insert(0, str(ROOT / "manager"))
import app  # noqa: E402

ENV = HOME / ".config" / "agentshangout-agent" / "env"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ArgvTest(unittest.TestCase):
    def test_only_fixed_words_reach_the_remote_shell(self):
        argv = app.argv_for({"target": "cake@10.0.0.228", "kit": "~/AgentsHangout-Agent"}, "status")
        self.assertEqual(argv[:7], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "cake@10.0.0.228"])
        self.assertEqual(argv[-1], "python3 -I ~/AgentsHangout-Agent/hermes/manage.py status")
        for machine, cmd in (({"target": "a;rm -rf ~", "kit": "~/k"}, "status"), ({"target": "h", "kit": "~/k $(x)"}, "status"),
                             ({"target": "h", "kit": "~/k"}, "rm"), ({"target": "-oProxyCommand=x", "kit": "~/k"}, "status"),
                             ({"target": "-oProxyCommand", "kit": "~/k"}, "status"), ({"target": "wsl:Ub untu", "kit": "~/k"}, "status"),
                             ({"target": "h", "kit": "~/k", "instance": "Tester;x"}, "status")):
            with self.assertRaises(ValueError):
                app.argv_for(machine, cmd)
        with self.assertRaises(ValueError):
            app.install_argv({"target": "h", "kit": "~/k"}, "main; rm -rf ~")

    def test_a_bot_beside_others_and_wsl(self):
        argv = app.argv_for({"target": "wsl:Ubuntu", "kit": "~/AgentsHangout-Agent", "instance": "tester"}, "apply")
        self.assertEqual(argv[:6], ["wsl.exe", "-d", "Ubuntu", "-e", "bash", "-lc"])
        self.assertEqual(argv[-1], "python3 -I ~/AgentsHangout-Agent/hermes/manage.py --instance tester apply")
        self.assertEqual(app.install_argv({"target": "wsl:Ubuntu", "kit": "~/k"})[:3], ["wsl.exe", "-d", "Ubuntu"])

    def test_updating_the_kit_keeps_its_branch_unless_one_is_named(self):
        keep = app.install_argv({"target": "h", "kit": "~/k"})[-1]
        self.assertNotIn("git checkout", keep)
        self.assertIn("git pull -q --ff-only", keep)
        self.assertIn("git checkout -q wake-server;", app.install_argv({"target": "h", "kit": "~/k"}, "wake-server")[-1])


class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ENV.parent.mkdir(parents=True)
        (HOME / "aha").mkdir()
        # A dead site address, so asking for the bot's name fails at once instead of going online.
        ENV.write_text(f"AHA_HOME={(HOME / 'aha').as_posix()}\nAGENTSHANGOUT_TOKEN=ah_bot_topsecret\n"
                       "AGENTSHANGOUT_URL=http://127.0.0.1:9\nAHA_TOOLS=use_item\n")
        cls.machines = app.Machines(HOME / "mgr" / "machines.json")
        cls.machines.put("box", {"target": "local", "kit": ROOT.as_posix()})
        cls.port = free_port()
        cls.httpd = app.serve(app.App(cls.machines, app.Jobs()), f"127.0.0.1:{cls.port}")
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        jar = http.cookiejar.CookieJar()
        cls.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        cls.home_html = cls.web.open(cls.base + "/login", data=b"password=letmein").read().decode()
        cls.csrf = cls.home_html.split('name="csrf" value="')[1].split('"')[0]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def post(self, path, fields):
        try:
            r = self.web.open(self.base + path, data=urllib.parse.urlencode(fields, doseq=True).encode())
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_the_page_needs_the_password(self):
        r = urllib.request.urlopen(self.base + "/")       # no cookie
        self.assertIn("Password", r.read().decode())

    def test_the_home_page_shows_the_machine(self):
        self.assertIn("box", self.home_html)
        self.assertIn("Bot token", self.home_html)

    def test_the_machine_page_shows_settings_but_never_a_secret(self):
        html = self.web.open(self.base + "/m/box").read().decode()
        self.assertIn("Capabilities", html)
        self.assertIn("qa-checklist", html)          # the kit's skills, to tick
        self.assertIn("use_item", html)
        self.assertNotIn("topsecret", html)
        self.assertIn("Saved on the machine", html)  # the bot token is there, and that's all it says

    def test_a_form_without_its_token_is_refused(self):
        status, _ = self.post("/m/box/settings", {"AHA_CAPABILITIES": "code", "csrf": "nope"})
        self.assertEqual(status, 403)
        self.assertNotIn("code", app.call(self.machines.get("box"), "status")["settings"]["AHA_CAPABILITIES"])

    def test_saving_settings_writes_them_on_the_machine(self):
        status, html = self.post("/m/box/settings", {"csrf": self.csrf, "then": "save", "AHA_CAPABILITIES": ["web", "github"],
                                                      "AHA_SKILLS": ["economy-review"], "AHA_ROLE": "the second reviewer",
                                                      "AHA_TOOLS": "use_item", "AHA_SCHEDULE": "09:00,15:00"})
        self.assertEqual(status, 200)
        self.assertIn("Saved", html)
        text = ENV.read_text()
        self.assertIn("AHA_CAPABILITIES=web,github\n", text)
        self.assertIn("AHA_SKILLS=economy-review\n", text)
        self.assertIn('AHA_ROLE="the second reviewer"\n', text)
        self.assertIn("AGENTSHANGOUT_TOKEN=ah_bot_topsecret\n", text)    # untouched

    def test_a_bad_setting_is_refused_on_the_machine_and_nothing_changes(self):
        before = ENV.read_text()
        status, html = self.post("/m/box/settings", {"csrf": self.csrf, "then": "save", "AHA_GOAL": "costs $5",
                                                      "AHA_CAPABILITIES": ["web"]})
        self.assertEqual(status, 400)
        self.assertIn("Not saved", html)
        self.assertEqual(ENV.read_text(), before)

    def test_a_machine_with_a_bad_target_is_not_added(self):
        status, html = self.post("/m", {"csrf": self.csrf, "name": "evil", "target": "x;reboot", "kit": "~/k"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.machines.get("evil"))


if __name__ == "__main__":
    unittest.main()
