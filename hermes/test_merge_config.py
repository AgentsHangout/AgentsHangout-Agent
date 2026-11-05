"""merge_config.py puts the kit's settings into Hermes's config.yaml, for a local model or Anthropic's API."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent   # the repo
MERGE = ROOT / "hermes" / "merge_config.py"
KIT = ROOT / "hermes" / "config.yaml"

HERMES_DEFAULT = """model:
  default: some-model
  provider: openrouter

agent:
  max_turns: 500

display:
  skin: default
"""


def merge(env, existing=HERMES_DEFAULT):
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "config.yaml"
        target.write_text(existing)
        run_env = {k: v for k, v in os.environ.items() if not k.startswith("AHA_")}
        run_env.update(env)
        out = subprocess.run([sys.executable, "-I", str(MERGE), str(target), str(KIT), "Test Bot"],
                             env=run_env, capture_output=True, text=True, check=True)
        return out.stdout.strip(), target.read_text()


def model_block(text):
    lines = text.split("\n")
    start = lines.index("model:")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith(" "))
    return "\n".join(lines[start:end]).strip()


class MergeConfigTest(unittest.TestCase):
    def test_local_model_by_default(self):
        status, text = merge({})
        self.assertEqual(status, "changed")
        block = model_block(text)
        self.assertIn("provider: custom", block)
        self.assertIn("base_url: http://127.0.0.1:8080/v1", block)
        self.assertNotIn("anthropic", block)

    def test_anthropic_replaces_the_model_block(self):
        _, text = merge({"AHA_PROVIDER": "anthropic"})
        self.assertEqual(model_block(text), "model:\n  provider: anthropic\n  default: claude-haiku-5-5")
        self.assertNotIn("8080", text)
        self.assertNotIn("openrouter", text)

    def test_anthropic_model_can_be_chosen(self):
        _, text = merge({"AHA_PROVIDER": "anthropic", "AHA_ANTHROPIC_MODEL": "claude-sonnet-5-5"})
        self.assertIn("default: claude-sonnet-5-5", model_block(text))

    def test_anthropic_keeps_the_rest_of_the_kit(self):
        _, text = merge({"AHA_PROVIDER": "anthropic"})
        self.assertIn("  max_turns: 12", text)
        self.assertIn("  api_server: [mcp-agentshangout]", text)
        self.assertIn("url: https://agentshangout.com/mcp", text)
        self.assertIn("        - read_room", text)
        self.assertNotIn("        - say\n", text)
        self.assertIn("display:\n  skin: default", text)

    def test_hermes_memory_is_turned_off_in_place(self):
        existing = HERMES_DEFAULT + """
memory:
  # Agent's personal notes: environment facts, conventions, things learned
  memory_enabled: true

  # User profile: preferences, communication style, expectations
  user_profile_enabled: true
  memory_char_limit: 2200
"""
        _, text = merge({"AHA_PROVIDER": "anthropic"}, existing=existing)
        self.assertEqual(text.count("\nmemory:"), 1)
        self.assertIn("  memory_enabled: false", text)
        self.assertIn("  user_profile_enabled: false", text)
        self.assertNotIn("_enabled: true", text)
        self.assertIn("  memory_char_limit: 2200", text)
        self.assertIn("  max_turns: 12", text)

    def test_hermes_memory_is_added_when_missing(self):
        _, text = merge({})
        self.assertIn("memory:\n  user_profile_enabled: false\n  memory_enabled: false", text)

    def test_running_again_changes_nothing(self):
        _, once = merge({"AHA_PROVIDER": "anthropic"})
        status, twice = merge({"AHA_PROVIDER": "anthropic"}, existing=once)
        self.assertEqual(status, "unchanged")
        self.assertEqual(once, twice)

    def test_switching_back_to_local(self):
        _, api = merge({"AHA_PROVIDER": "anthropic"})
        status, local = merge({}, existing=api)
        self.assertEqual(status, "changed")
        self.assertIn("provider: custom", model_block(local))
        self.assertNotIn("anthropic", model_block(local))


if __name__ == "__main__":
    unittest.main()
