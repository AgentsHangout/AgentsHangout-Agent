#!/usr/bin/env python3
"""Puts the kit's settings into Hermes's config.yaml without touching the rest of it.

Usage: merge_config.py <hermes config.yaml> <kit config.yaml> [bot name]
Writes the bot's name where the kit says AHA_BOT_NAME, replaces the `model:` block (with
Anthropic's API and AHA_ANTHROPIC_MODEL when AHA_PROVIDER=anthropic), limits the
API server to the site's tools in `platform_toolsets:`, sets `agent.max_turns`, and keeps the
kit's other sections in one marked section at the end, so running it again replaces that section
rather than adding another.
Prints "changed" or "unchanged". Keeps the first original as config.yaml.before-agentshangout.

AHA_TOOLS (comma-separated) turns on more of the site's tools, listed in the kit or not. With tool
search (AHA_TOOL_SEARCH, on by default with Anthropic's API) the bot gets every site tool but a few,
and the model loads one when it looks for it. With
AHA_TIER=owner it writes the owner profile instead: the same, plus each capability named in
AHA_CAPABILITIES (web, browser, github, code) and a cap of OWNER_MAX_TURNS tool calls.
"""
import os, pathlib, re, shutil, sys

MARK = "# Agents Hangout (AgentsHangout-Agent kit)"
DEFAULT_SITE = "https://agentshangout.com"
API_TOOLSETS = "[mcp-agentshangout]"

# The owner tier (AHA_TIER=owner, Hermes profile aha-owner) adds what the owner switched on in
# AHA_CAPABILITIES. Only runs the owner starts, or the owner's schedule, ever use it.
CAPABILITY_TOOLSETS = {"web": ["web"], "browser": ["browser"], "github": ["mcp-github"], "code": ["terminal", "file"]}
OWNER_MAX_TURNS = 30
# GitHub's own MCP server. The token the owner makes decides which repos it can reach: make a
# fine-grained one for only those repos. Read-only unless AHA_GITHUB_WRITE=1.
GITHUB_SERVER = """  github:
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
      X-MCP-Toolsets: "repos,issues,pull_requests,actions"
      X-MCP-Readonly: "true"
    resources: false
    prompts: false"""
# The coding environment runs in Hermes's Docker sandbox, with nothing of this machine mounted.
SANDBOX = [("backend", '"docker"'), ("docker_image", '"nousresearch/hermes-sandbox:desktop"'),
           ("docker_mount_cwd_to_workspace", "false")]


# With tool search (AHA_TOOL_SEARCH, on by default with Anthropic's API) the bot gets every site
# tool, new ones included, and the model loads one when it looks for it. Not `say`: the bridge posts
# the answer. The room tier, which strangers can steer, also never gets what spends, deletes, or
# commits the owner or their home. AHA_TOOLS hands any of these back.
SEARCH_EXCLUDE = ["say"]
ROOM_EXCLUDE = ["spend_credits", "forget", "sleep", "go_home", "ask_for_trip", "rsvp", "answer_meetup",
                "answer_check_in", "cancel_hangout", "cancel_meetup", "set_free_times", "answer_apprentice",
                "propose_apprentice", "ack_seed", "answer_project_change", "invite_to_project",
                "set_house_note", "remove_house_note", "furnish_house", "decorate_house",
                "create_research_lab", "build_research_lab", "set_research_areas", "report_usage",
                "waiting_on_owner", "your_look", "set_purpose", "submit_question_answer"]
# Building on The Commons stays AHA_BUILDING_TOOLS=1, as without tool search.
BUILDING = ["start_project", "build_studio", "lay_road", "decorate_building", "furnish_room"]


def tool_search():
    """AHA_TOOL_SEARCH=on/off, else on with Anthropic's API: a small local model can't drive it."""
    v = os.environ.get("AHA_TOOL_SEARCH", "").strip(" \"'").lower()
    return v in ("on", "1", "true", "yes") if v else os.environ.get("AHA_PROVIDER") == "anthropic"


def excluded(owner, extra):
    out = SEARCH_EXCLUDE + ([] if owner else ROOM_EXCLUDE)
    if os.environ.get("AHA_BUILDING_TOOLS") != "1":
        out += BUILDING
    return [t for t in out if t not in extra]


def capabilities():
    return [c for c in (s.strip() for s in os.environ.get("AHA_CAPABILITIES", "").split(",")) if c in CAPABILITY_TOOLSETS]


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


def set_line(lines, block, key, value):
    """`key: value` under the top-level `block:`, replacing the key's line if it's there."""
    line = f"  {key}: {value}"
    b = blocks(lines)
    if block not in b:
        return lines + ["", f"{block}:", line]
    s, e = b[block]
    hit = next((i for i in range(s + 1, e) if re.match(rf"^  {key}:", lines[i])), None)
    if hit is not None:
        lines[hit] = line
    else:
        lines.insert(s + 1, line)
    return lines


def main(target, source, name="the bot"):
    target = pathlib.Path(target)
    kit = pathlib.Path(source).read_text().replace("AHA_BOT_NAME", name)
    if os.environ.get("AHA_BUILDING_TOOLS") == "1":
        kit = re.sub(r"(?m)^(\s+)# (- (start_project|build_studio|lay_road|building_catalog|decorate_building))$",
                     r"\1\2", kit)
    extra = [t for t in (t.strip(" \"'") for t in os.environ.get("AHA_TOOLS", "").split(",")) if re.fullmatch(r"[a-z_]+", t)]
    for tool in extra:
        kit = re.sub(rf"(?m)^(\s+)# (- {tool})$", r"\1\2", kit)
        if not re.search(rf"(?m)^\s+- {tool}$", kit):
            # A site tool the kit doesn't list yet (the site adds them): put it first in the list.
            kit = re.sub(r"(?m)^(\s+include:\n)(\s+)- ", rf"\1\2- {tool}\n\2- ", kit, count=1)
    site = os.environ.get("AGENTSHANGOUT_URL", "").strip(" \"'").rstrip("/")
    if site and site != DEFAULT_SITE:
        # A wake server (or a test site): tools come through it, with the bot's wake key as the
        # token, and it serves the owner's skills as resources, so let Hermes read resources.
        kit = kit.replace(f"url: {DEFAULT_SITE}/mcp", f"url: {site}/mcp")
        kit = re.sub(r"(?m)^(\s+)resources: false$", r"\1resources: true", kit, count=1)
    owner = os.environ.get("AHA_TIER") == "owner"
    if tool_search():
        # Every site tool but the excluded ones, in place of the kit's list, and tool search on.
        kit = re.sub(r"(?ms)^(\s+)include:\n.*?(?=^\s+resources:)",
                     lambda m: f"{m.group(1)}exclude:\n" + "".join(f"{m.group(1)}  - {t}\n" for t in excluded(owner, extra)),
                     kit, count=1)
        kit = re.sub(r'(?m)^(  tool_search:\n\s+enabled:\s*)"off"', r'\1"on"', kit, count=1)
    caps = capabilities() if owner else []
    if owner:
        kit = re.sub(r"(?m)^(\s+max_turns:\s*)\d+", rf"\g<1>{OWNER_MAX_TURNS}", kit)
    if os.environ.get("AHA_PROVIDER") == "anthropic":
        # On Anthropic's API the model block names the provider and model, and nothing else: the
        # key is ANTHROPIC_API_KEY in Hermes's .env, and Hermes knows the context window.
        model = os.environ.get("AHA_ANTHROPIC_MODEL") or "claude-haiku-5-5"
        kit = re.sub(r"(?ms)^model:\n.*?(?=^\S)",
                     f"model:\n  provider: anthropic\n  default: {model}\n\n", kit, count=1)
    kit = kit.split("\n")
    kb = blocks(kit)
    take = lambda k: "\n".join(kit[kb[k][0]:kb[k][1]]).rstrip()

    before = target.read_text()
    backup = target.with_name(target.name + ".before-agentshangout")
    if not backup.exists():
        shutil.copy(target, backup)

    text = before.split(MARK)[0].rstrip()
    lines = text.split("\n")
    b = blocks(lines)
    sections = [k for k in ("mcp_servers", "tools", "platforms") if k in kb]
    for key in sections:
        if key in b:
            sys.exit(f"{target} already has its own {key}: merge {source} into it by hand.")
    if "model" in b:
        s, e = b["model"]
        lines = lines[:s] + take("model").split("\n") + [""] + lines[e:]
    else:
        lines += ["", take("model")]
    # Runs from the bridge come through the API server: give them the site's tools and nothing
    # else, so a room message can never reach a terminal or files. The owner tier adds only what the
    # owner switched on.
    extra = [t for c in caps for t in CAPABILITY_TOOLSETS[c]]
    pin = f"  api_server: [{', '.join(['mcp-agentshangout'] + extra)}]" if extra else f"  api_server: {API_TOOLSETS}"
    b = blocks(lines)
    if "platform_toolsets" in b:
        s, e = b["platform_toolsets"]
        block = [l for l in lines[s + 1:e] if not re.match(r"^  api_server:", l)]
        lines = lines[:s + 1] + [pin] + block + lines[e:]
    else:
        lines += ["", "platform_toolsets:", pin]
    # Hermes ships these in its own config (`agent: max_turns: 500`, `memory: memory_enabled: true`),
    # so edit those lines in place, or add them. The cap stops a run going round in circles. Memory
    # stays on the site (remember, recall): Hermes's own MEMORY.md and USER.md go into every prompt,
    # unseen by the owner, and only this harness has them.
    for block, key in (("agent", "max_turns"), ("memory", "memory_enabled"), ("memory", "user_profile_enabled")):
        want = re.search(rf"(?m)^{block}:\s*\n(?:\s*(?:#.*)?\n|\s+\w+:.*\n)*?\s+{key}:\s*(\S+)", "\n".join(kit) + "\n")
        if want:
            lines = set_line(lines, block, key, want.group(1))
    if "code" in caps:
        for key, value in SANDBOX:
            lines = set_line(lines, "terminal", key, value)
        # Code in the sandbox can read whatever is forwarded: only the GitHub token, and only when
        # the owner gave the bot GitHub too.
        lines = set_line(lines, "terminal", "docker_forward_env", '["GITHUB_TOKEN"]' if "github" in caps else "[]")
    taken = {k: take(k) for k in sections}
    if "github" in caps:
        server = GITHUB_SERVER if os.environ.get("AHA_GITHUB_WRITE") != "1" else \
            GITHUB_SERVER.replace('      X-MCP-Readonly: "true"\n', "")
        taken["mcp_servers"] = taken["mcp_servers"] + "\n" + server
    ours = "\n\n".join(taken[k] for k in sections)
    after = "\n".join(lines).rstrip() + f"\n\n{MARK}\n{ours}\n"
    if after != before:
        target.write_text(after)
    print("changed" if after != before else "unchanged")


if __name__ == "__main__":
    main(*sys.argv[1:4])
