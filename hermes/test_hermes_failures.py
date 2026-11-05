"""bridge.py posts only what Hermes actually answered, never Hermes's own error message."""
import http.server
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent   # the repo


class FakeHermes(http.server.BaseHTTPRequestHandler):
    reply = {}

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps(FakeHermes.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def chat(content, finish_reason="stop", **extra):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": finish_reason}], **extra}


class AskHermesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeHermes)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.home = tempfile.TemporaryDirectory()
        os.environ.update({
            "AGENTSHANGOUT_TOKEN": "ah_bot_test", "API_SERVER_KEY": "test", "AHA_HOME": cls.home.name,
            "AHA_HERMES_URL": f"http://127.0.0.1:{cls.server.server_port}/v1/chat/completions"})
        spec = importlib.util.spec_from_file_location("bridge", ROOT / "hermes" / "bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.home.cleanup()

    def test_an_answer_comes_back(self):
        FakeHermes.reply = chat("Hi @Justin, I'm here.")
        self.assertEqual(self.bridge.ask_hermes("hello", timeout=5), "Hi @Justin, I'm here.")

    def test_a_failed_run_is_not_an_answer(self):
        FakeHermes.reply = chat("Anthropic rejected this request as malformed, so the model didn't answer.",
                                finish_reason="error")
        with self.assertRaises(RuntimeError):
            self.bridge.ask_hermes("hello", timeout=5)

    def test_hermes_marks_a_failed_run(self):
        FakeHermes.reply = chat("Provider said: HTTP 400", finish_reason="stop",
                                hermes={"completed": False, "failed": True})
        with self.assertRaises(RuntimeError):
            self.bridge.ask_hermes("hello", timeout=5)

    def test_a_capped_run_keeps_its_wrap_up(self):
        FakeHermes.reply = chat("I looked around; nothing new.", finish_reason="length",
                                hermes={"completed": False, "failed": False})
        self.assertEqual(self.bridge.ask_hermes("hello", timeout=5), "I looked around; nothing new.")


if __name__ == "__main__":
    unittest.main()
