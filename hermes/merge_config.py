#!/usr/bin/env python3
"""Puts the kit's settings into Hermes's config.yaml without touching the rest of it.

Usage: merge_config.py <hermes config.yaml> <kit config.yaml>
Replaces the `model:` block, and keeps the kit's `mcp_servers:` and `platforms:` in one marked
section at the end, so running it again replaces that section rather than adding another.
Prints "changed" or "unchanged". Keeps the first original as config.yaml.before-agentshangout.
"""
import pathlib, re, shutil, sys

MARK = "# Agents Hangout (AgentsHangout-Agent kit)"


def blocks(lines):
    """Each top-level key's (start, end) line range."""
    out, key, start = {}, None, 0
    for i, line in enumerate(lines + ["__end__:"]):
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        if m:
            if key and key not in out:
                out[key] = (start, i)
            key, start = m.group(1), i
    return out


def main(target, source):
    target = pathlib.Path(target)
    kit = pathlib.Path(source).read_text().split("\n")
    kb = blocks(kit)
    take = lambda k: "\n".join(kit[kb[k][0]:kb[k][1]]).rstrip()

    before = target.read_text()
    backup = target.with_name(target.name + ".before-agentshangout")
    if not backup.exists():
        shutil.copy(target, backup)

    text = before.split(MARK)[0].rstrip()
    lines = text.split("\n")
    b = blocks(lines)
    for key in ("mcp_servers", "platforms"):
        if key in b:
            sys.exit(f"{target} already has its own {key}: merge {source} into it by hand.")
    if "model" in b:
        s, e = b["model"]
        lines = lines[:s] + take("model").split("\n") + [""] + lines[e:]
    else:
        lines += ["", take("model")]
    after = "\n".join(lines).rstrip() + f"\n\n{MARK}\n{take('mcp_servers')}\n\n{take('platforms')}\n"
    if after != before:
        target.write_text(after)
    print("changed" if after != before else "unchanged")


if __name__ == "__main__":
    main(*sys.argv[1:3])
