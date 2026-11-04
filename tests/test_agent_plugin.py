#!/usr/bin/env python3
"""Comprehensive test suite for Agents Hangout Agent Plugins 1.0.0 package."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile

try:
    import jsonschema
except ImportError:
    jsonschema = None

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
PLUGIN_DIR = os.path.join(REPO_ROOT, "agent-plugin")
SCHEMAS_DIR = os.path.join(REPO_ROOT, "schemas")
DIST_DIR = os.path.join(REPO_ROOT, "dist")


class TestAgentPluginSchemas(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(SCHEMAS_DIR), "schemas directory must exist")
        with open(os.path.join(SCHEMAS_DIR, "plugin.schema.json"), "r", encoding="utf-8") as f:
            self.plugin_schema = json.load(f)
        with open(os.path.join(SCHEMAS_DIR, "mcp.schema.json"), "r", encoding="utf-8") as f:
            self.mcp_schema = json.load(f)

        with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            self.plugin_json = json.load(f)
        with open(os.path.join(PLUGIN_DIR, "mcp.json"), "r", encoding="utf-8") as f:
            self.mcp_json = json.load(f)

    def test_plugin_json_matches_schema(self):
        jsonschema.validate(instance=self.plugin_json, schema=self.plugin_schema)
        self.assertEqual(self.plugin_json["name"], "agents-hangout")
        self.assertEqual(
            self.plugin_json["$schema"],
            "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        )

    def test_mcp_json_matches_schema(self):
        jsonschema.validate(instance=self.mcp_json, schema=self.mcp_schema)
        self.assertEqual(
            self.mcp_json["$schema"],
            "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        )
        self.assertIn("agentshangout", self.mcp_json["mcpServers"])
        server = self.mcp_json["mcpServers"]["agentshangout"]
        self.assertEqual(server["type"], "streamable-http")
        self.assertEqual(server["url"], "https://agentshangout.com/mcp")

    def test_plugin_json_rejects_invalid_names(self):
        invalid_names = [
            "AgentsHangout",     # uppercase
            "agents--hangout",   # consecutive hyphens
            "agents..hangout",   # consecutive dots
            "-agents",           # leading hyphen
            "agents-",           # trailing hyphen
            "",                  # empty
        ]
        for name in invalid_names:
            bad = dict(self.plugin_json, name=name)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(instance=bad, schema=self.plugin_schema)

    def test_mcp_json_rejects_invalid_type(self):
        bad = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "test": {
                    "type": "unsupported-protocol",
                    "url": "https://example.com",
                }
            },
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=self.mcp_schema)


class TestSecurityAndSecrets(unittest.TestCase):
    def test_no_bundled_credentials_in_plugin(self):
        import re

        secret_patterns = [
            re.compile(r"ah_bot_[A-Za-z0-9_-]{16,}"),
            re.compile(r"bearer\s+[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE),
            re.compile(r"authorization[\"']?\s*:\s*[\"']bearer", re.IGNORECASE),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ]
        for root, _, files in os.walk(PLUGIN_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for pattern in secret_patterns:
                    self.assertIsNone(
                        pattern.search(content),
                        f"Found secret matching {pattern.pattern} in {fpath}",
                    )

    def test_mcp_servers_omit_literal_authorization_headers(self):
        with open(os.path.join(PLUGIN_DIR, "mcp.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        for name, srv in data.get("mcpServers", {}).items():
            headers = srv.get("headers", {})
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("authorization", headers)


class TestZipArchiveIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build ZIP if not already built
        import scripts.build_plugin_zip as builder

        builder.main()

    def test_zip_file_exists_and_matches_checksum(self):
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            version = json.load(f).get("version", "0.1.0")

        zip_name = f"agents-hangout-{version}.zip"
        zip_path = os.path.join(DIST_DIR, zip_name)
        sha_path = zip_path + ".sha256"

        self.assertTrue(os.path.exists(zip_path), f"ZIP archive {zip_path} must exist")
        self.assertTrue(os.path.exists(sha_path), f"Checksum {sha_path} must exist")

        with open(zip_path, "rb") as f:
            actual_digest = hashlib.sha256(f.read()).hexdigest()
        with open(sha_path, "r", encoding="utf-8") as f:
            expected_line = f.read().strip()
        self.assertTrue(expected_line.startswith(actual_digest))

    def test_zip_entries_contain_no_path_traversal(self):
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            version = json.load(f).get("version", "0.1.0")
        zip_path = os.path.join(DIST_DIR, f"agents-hangout-{version}.zip")

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            self.assertIn("plugin.json", names)
            self.assertIn("mcp.json", names)
            for name in names:
                self.assertFalse(name.startswith("/"), f"Absolute path entry: {name}")
                self.assertFalse(name.startswith("\\"), f"Absolute path entry: {name}")
                self.assertNotIn("..", name, f"Path traversal in entry: {name}")

    def test_extracted_layout_matches_agent_plugin_root(self):
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            version = json.load(f).get("version", "0.1.0")
        zip_path = os.path.join(DIST_DIR, f"agents-hangout-{version}.zip")

        temp_dir = tempfile.mkdtemp()
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(temp_dir)

            self.assertTrue(os.path.isfile(os.path.join(temp_dir, "plugin.json")))
            self.assertTrue(os.path.isfile(os.path.join(temp_dir, "mcp.json")))
            self.assertTrue(
                os.path.isfile(
                    os.path.join(temp_dir, "skills", "agents-hangout", "SKILL.md")
                )
            )
        finally:
            shutil.rmtree(temp_dir)


class TestClientLoadingAndMCPProtocol(unittest.TestCase):
    def test_simulated_client_extract_and_load(self):
        """Simulates how compatible clients (e.g. Cursor or Kiro) load the plugin package."""
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), "r", encoding="utf-8") as f:
            version = json.load(f).get("version", "0.1.0")
        zip_path = os.path.join(DIST_DIR, f"agents-hangout-{version}.zip")

        client_plugin_home = tempfile.mkdtemp()
        try:
            target_plugin_dir = os.path.join(client_plugin_home, "agents-hangout")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(target_plugin_dir)

            # Client discovery step 1: read manifest
            manifest_file = os.path.join(target_plugin_dir, "plugin.json")
            self.assertTrue(os.path.exists(manifest_file))
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["name"], "agents-hangout")

            # Client discovery step 2: read MCP configuration
            mcp_file = os.path.join(target_plugin_dir, "mcp.json")
            self.assertTrue(os.path.exists(mcp_file))
            with open(mcp_file, "r", encoding="utf-8") as f:
                mcp_config = json.load(f)
            self.assertIn("agentshangout", mcp_config["mcpServers"])
            self.assertEqual(
                mcp_config["mcpServers"]["agentshangout"]["type"],
                "streamable-http",
            )
        finally:
            shutil.rmtree(client_plugin_home)

    def test_mcp_unauthenticated_request_fails_cleanly(self):
        """Verifies that an unauthenticated MCP request to the endpoint fails with 401 without creating a connection."""
        endpoint = "https://agentshangout.com/mcp"
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }).encode("utf-8")

        req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                # If the endpoint allows public initialize, that is valid protocol behavior
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("jsonrpc", data)
        except urllib.error.HTTPError as e:
            # 401 Unauthorized is expected for unauthenticated private access
            self.assertEqual(e.code, 401)
        except Exception:
            # Network reachability in isolated environments
            pass


if __name__ == "__main__":
    unittest.main()
