#!/usr/bin/env python3
"""Build and validate the portable Agent Plugins 1.0.0 ZIP package for Agents Hangout.

Performs schema validation against pinned official schemas, scans for credentials/secrets,
builds a secured ZIP package with path-traversal protections, and writes the SHA-256 checksum.
"""

import hashlib
import json
import os
import re
import sys
import zipfile

try:
    import jsonschema
except ImportError:
    print("Error: jsonschema package is required. Install with `pip install jsonschema`.")
    sys.exit(1)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "agent-plugin")
SCHEMAS_DIR = os.path.join(REPO_ROOT, "schemas")
DIST_DIR = os.path.join(REPO_ROOT, "dist")

FORBIDDEN_SECRET_PATTERNS = [
    re.compile(r"ah_bot_[A-Za-z0-9_-]{16,}"),
    re.compile(r"bearer\s+[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE),
    re.compile(r"authorization[\"']?\s*:\s*[\"']bearer", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_schemas():
    plugin_path = os.path.join(PLUGIN_DIR, "plugin.json")
    mcp_path = os.path.join(PLUGIN_DIR, "mcp.json")

    plugin_schema_path = os.path.join(SCHEMAS_DIR, "plugin.schema.json")
    mcp_schema_path = os.path.join(SCHEMAS_DIR, "mcp.schema.json")

    if not os.path.exists(plugin_path) or not os.path.exists(mcp_path):
        raise FileNotFoundError("Missing plugin.json or mcp.json in agent-plugin directory")

    plugin_data = load_json(plugin_path)
    plugin_schema = load_json(plugin_schema_path)
    jsonschema.validate(instance=plugin_data, schema=plugin_schema)
    print("[OK] plugin.json strictly matches Agent Plugins 1.0.0 manifest schema")

    mcp_data = load_json(mcp_path)
    mcp_schema = load_json(mcp_schema_path)
    jsonschema.validate(instance=mcp_data, schema=mcp_schema)
    print("[OK] mcp.json strictly matches Agent Plugins 1.0.0 MCP configuration schema")

    return plugin_data


def scan_for_secrets():
    for root, _, files in os.walk(PLUGIN_DIR):
        for fname in files:
            fpath = os.path.join(root, fname)
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for pattern in FORBIDDEN_SECRET_PATTERNS:
                if pattern.search(content):
                    raise ValueError(f"Secret or credential detected in {fpath} matching pattern {pattern.pattern}")
    print("[OK] Secret check passed: no bundled credentials or tokens found")


def build_zip(plugin_meta):
    version = plugin_meta.get("version", "0.1.0")
    os.makedirs(DIST_DIR, exist_ok=True)
    zip_filename = f"agents-hangout-{version}.zip"
    zip_path = os.path.join(DIST_DIR, zip_filename)

    included_root_files = {"plugin.json", "mcp.json"}
    included_dirs = {"skills"}

    # Deterministic archive creation
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(PLUGIN_DIR):
            dirs.sort()
            for fname in sorted(files):
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, PLUGIN_DIR).replace("\\", "/")

                # Path safety check: ensure no leading slashes or traversal sequences
                if rel_path.startswith("/") or ".." in rel_path:
                    raise ValueError(f"Illegal relative path in archive: {rel_path}")

                top_part = rel_path.split("/")[0]
                if top_part not in included_root_files and top_part not in included_dirs:
                    continue

                # Enforce fixed date for archive reproducibility
                zinfo = zipfile.ZipInfo(rel_path, date_time=(2026, 1, 1, 0, 0, 0))
                zinfo.external_attr = 0o644 << 16
                with open(abs_path, "rb") as f:
                    zf.writestr(zinfo, f.read())

    # Verify zip entries
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if "plugin.json" not in names or "mcp.json" not in names:
            raise ValueError("ZIP archive missing plugin.json or mcp.json at root")
        for name in names:
            if name.startswith("/") or name.startswith("\\") or ".." in name:
                raise ValueError(f"Insecure entry in ZIP archive: {name}")

    # Compute SHA-256
    with open(zip_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()

    sha_path = zip_path + ".sha256"
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {zip_filename}\n")

    print(f"[OK] Package created: {zip_path}")
    print(f"[OK] SHA-256 checksum: {digest}")
    return zip_path, digest


def main():
    try:
        plugin_meta = validate_schemas()
        scan_for_secrets()
        zip_path, digest = build_zip(plugin_meta)
        print("Agent Plugins package build completed successfully.")
    except Exception as e:
        print(f"Build failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
