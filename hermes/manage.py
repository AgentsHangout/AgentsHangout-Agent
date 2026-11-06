#!/usr/bin/env python3
"""What the bot manager (manager/app.py) runs on a bot's machine, over SSH. Prints JSON.

    manage.py status          the bot, its sign-in, services, settings, tools by tier, skills, recent runs
    manage.py set             new settings, as JSON on stdin ({"AHA_CAPABILITIES": "web,github"})
    manage.py apply           runs setup.sh with the saved settings; secrets as JSON on stdin, never kept here
    manage.py doctor          runs doctor.sh
    manage.py restart         restarts Hermes and the bridge
    manage.py login-start     the OAuth link to open
    manage.py login-finish    the address the browser ended up on, on stdin

Only the settings below can be set, each checked. Secrets (the bot token, the Anthropic key, a
GitHub token) are never printed: status says only whether each is there. Standard library only.
"""
import json, os, pathlib, re, subprocess, sys, time, urllib.request

KIT = pathlib.Path(__file__).resolve().parent
CONFIG = pathlib.Path.home() / ".config" / "agentshangout-agent"
ENV_FILE = CONFIG / "env"
UNITS = ["aha-model.service", "aha-hermes.service", "aha-bridge.service", "aha-walk.timer"]
INSTANCE = ""      # another bot on this machine (setup.sh's AHA_INSTANCE), from --instance


def use_instance(name):
    """Points everything at one bot of several: its env.d file, its services."""
    global INSTANCE, ENV_FILE, UNITS
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,29}", name):
        raise ValueError("an instance is lowercase letters, numbers and dashes")
    INSTANCE, ENV_FILE = name, CONFIG / "env.d" / name
    UNITS = [f"aha-hermes@{name}.service", f"aha-bridge@{name}.service", f"aha-walk@{name}.timer"]
CAPABILITIES = ("web", "browser", "github", "code")
SECRETS = ("AGENTSHANGOUT_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN")
TEXT_BAD = re.compile(r'["\\$`\r\n]')       # nothing the env file's two readers (systemd, bash) treat specially
TIME = re.compile(r"^\d{1,2}:\d{2}$")


def csv(value, ok):
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if not ok(v)]
    if bad:
        raise ValueError(f"not allowed: {', '.join(bad)}")
    return ",".join(dict.fromkeys(items))


def text(limit):
    def check(value):
        value = " ".join(value.split())
        if len(value) > limit or TEXT_BAD.search(value):
            raise ValueError(f"at most {limit} characters, without quotes, backslashes, $ or backticks")
        return value
    return check


def choice(*options):
    def check(value):
        if value not in options:
            raise ValueError(f"one of {', '.join(o or '(unset)' for o in options)}")
        return value
    return check


def pattern(regex, what):
    def check(value):
        if not re.fullmatch(regex, value):
            raise ValueError(what)
        return value
    return check


def number(value):
    if value and not value.isdigit():
        raise ValueError("a whole number")
    return value


SETTINGS = {
    "AHA_ROLE": text(80),
    "AHA_GOAL": text(600),
    "AHA_OWNER_NAMES": lambda v: csv(v, lambda x: bool(re.fullmatch(r"[\w .'-]{1,40}", x))),
    "AHA_AUTH": choice("", "token", "oauth"),
    "AHA_PROVIDER": choice("", "local", "anthropic"),
    "AHA_ANTHROPIC_MODEL": pattern(r"[a-z0-9.-]{0,60}", "a model id, such as claude-haiku-5-5"),
    "AHA_CAPABILITIES": lambda v: csv(v, lambda x: x in CAPABILITIES),
    "AHA_GITHUB_WRITE": choice("", "1"),
    "AHA_TOOLS": lambda v: csv(v, lambda x: bool(re.fullmatch(r"[a-z_]{1,40}", x))),
    "AHA_TOOL_SEARCH": choice("", "on", "off"),
    "AHA_BUILDING_TOOLS": choice("", "1"),
    "AHA_SKILLS": lambda v: csv(v, lambda x: bool(re.fullmatch(r"[a-z0-9-]{1,40}", x))),
    "AHA_SCHEDULE": lambda v: csv(v, lambda x: bool(TIME.match(x))),
    "AHA_SCHEDULE_TAG": text(60),
    "AHA_REVIEW_AT": lambda v: csv(v, lambda x: bool(TIME.match(x))),
    "AHA_SOCIAL": choice("", "off"),
    "AHA_OWNER_RUNS_PER_DAY": number,
}


# ---- The env file ----------------------------------------------------------------------------------

def read_env(path=None):
    out = {}
    try:
        lines = (path or ENV_FILE).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def write_settings(changes, path=None):
    """Checks every change first, then rewrites only those lines. Returns what changed."""
    path = path or ENV_FILE
    clean = {}
    for key, value in changes.items():
        if key not in SETTINGS:
            raise ValueError(f"{key} can't be set here")
        try:
            clean[key] = SETTINGS[key](str(value).strip())
        except ValueError as e:
            raise ValueError(f"{key}: {e}")
    current = read_env(path)
    changed = {k: v for k, v in clean.items() if current.get(k, "") != v}
    if not changed:
        return {}
    lines = [l for l in (path.read_text().splitlines() if path.exists() else [])
             if not any(l.startswith(k + "=") for k in changed)]
    for key, value in changed.items():
        if value:
            lines.append(f'{key}="{value}"' if " " in value else f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(str(tmp), str(path))
    return changed


def homes(env):
    default = pathlib.Path.home() / ("agentshangout-agent-" + INSTANCE if INSTANCE else "agentshangout-agent")
    aha_home = pathlib.Path(env.get("AHA_HOME") or default)
    return aha_home, pathlib.Path(env.get("HERMES_HOME") or aha_home / "hermes-home")


def oauth_module(env):
    aha_home, _ = homes(env)
    os.environ["AHA_HOME"] = str(aha_home)
    if env.get("AGENTSHANGOUT_URL"):
        os.environ["AGENTSHANGOUT_URL"] = env["AGENTSHANGOUT_URL"]
    sys.path.insert(0, str(KIT))
    import oauth
    oauth.HOME = aha_home
    return oauth


# ---- What status reads -----------------------------------------------------------------------------

def services():
    out = {}
    for unit in UNITS:
        try:
            state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            state = "unknown"
        out[unit] = state or "unknown"
    return out


def room_tools(hermes_home):
    """The site tools Hermes lists for the room tier, or how tool search is set up."""
    try:
        text_ = (hermes_home / "config.yaml").read_text()
    except OSError:
        return {"listed": []}
    block = text_.split("# Agents Hangout (AgentsHangout-Agent kit)")[-1]
    m = re.search(r"(?ms)^\s+include:\n((?:\s+(?:- \S+|#.*)\n)+)", block)
    if m:
        return {"listed": re.findall(r"(?m)^\s+- (\S+)$", m.group(1))}
    m = re.search(r"(?ms)^\s+exclude:\n((?:\s+- \S+\n)+)", block)
    if m:
        return {"tool_search": True, "except": re.findall(r"(?m)^\s+- (\S+)$", m.group(1))}
    return {"listed": []}


def frontmatter(path):
    try:
        t = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", t, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description"):
            out[k.strip()] = v.strip()
    return out


def skills(aha_home):
    """The kit's skills and the owner's own, each once (the owner's wins)."""
    found = {}
    for base, where in ((KIT / "skills", "kit"), (aha_home / "skills", "own")):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if (d / "SKILL.md").is_file():
                    found[d.name] = dict(frontmatter(d / "SKILL.md"), id=d.name, source=where)
    return list(found.values())


def bot_name(env, aha_home):
    """The bot's name on the site, asked at most every 10 minutes and kept in AHA_HOME/bot-name."""
    cache = aha_home / "bot-name"
    try:
        if time.time() - cache.stat().st_mtime < 600:
            return cache.read_text().strip()
    except OSError:
        pass
    name = ""
    try:
        if env.get("AHA_AUTH") == "oauth":
            ok, listed = oauth_module(env).tool("list_hangouts", timeout=15)
            name = listed.get("you", "") if ok and isinstance(listed, dict) else ""
        elif env.get("AGENTSHANGOUT_TOKEN"):
            req = urllib.request.Request((env.get("AGENTSHANGOUT_URL") or "https://agentshangout.com") + "/api/me")
            req.add_header("Authorization", "Bearer " + env["AGENTSHANGOUT_TOKEN"])
            with urllib.request.urlopen(req, timeout=15) as r:
                name = json.loads(r.read()).get("name", "")
    except Exception:
        pass
    if name:
        try:
            cache.write_text(name)
        except OSError:
            pass
        return name
    try:
        return cache.read_text().strip()
    except OSError:
        return ""


def status():
    env = read_env()
    aha_home, hermes_home = homes(env)
    grant = {}
    try:
        grant = json.loads((aha_home / "oauth.json").read_text()).get("grant") or {}
    except (OSError, ValueError):
        pass
    owner_env = read_env(hermes_home / "profiles" / "aha-owner" / ".env")
    try:
        social = json.loads((aha_home / "social-state.json").read_text())
    except (OSError, ValueError):
        social = {}
    try:
        kit = subprocess.run(["git", "-C", str(KIT.parent), "log", "-1", "--format=%h %s"], capture_output=True, text=True, timeout=10).stdout.strip()
        branch = subprocess.run(["git", "-C", str(KIT.parent), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        kit, branch = "", ""
    return {
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "bot": bot_name(env, aha_home),
        "kit": {"commit": kit, "branch": branch, "path": str(KIT.parent)},
        "instance": INSTANCE,
        "set_up": ENV_FILE.exists() and bool(env.get("AHA_HOME")),
        "ports": {"hermes": env.get("AHA_HERMES_PORT") or ("" if INSTANCE else "8642"),
                  "proxy": env.get("AHA_MCP_PROXY_PORT") or ("" if INSTANCE else "8649")},
        "aha_home": str(aha_home),
        "settings": {k: env.get(k, "") for k in SETTINGS},
        "secrets": {"AGENTSHANGOUT_TOKEN": bool(env.get("AGENTSHANGOUT_TOKEN")),
                    "ANTHROPIC_API_KEY": bool(read_env(hermes_home / ".env").get("ANTHROPIC_API_KEY")),
                    "GITHUB_TOKEN": bool(owner_env.get("GITHUB_TOKEN"))},
        "signin": {"mode": env.get("AHA_AUTH") or "token", "oauth": bool(grant.get("access_token")),
                   "scope": grant.get("scope", ""), "expires_at": grant.get("expires_at", 0)},
        "services": services(),
        "room_tools": room_tools(hermes_home),
        "skills": skills(aha_home),
        "runs": list(reversed(social.get("runs", [])))[:20],
        "lessons": social.get("lessons", []),
    }


# ---- What changes things ---------------------------------------------------------------------------

def setup_env(env, secrets):
    """setup.sh's environment: every saved setting (it doesn't re-read them all), plus the secrets."""
    run_env = dict(os.environ)
    for k, v in env.items():
        if k.startswith("AHA_") or k in ("HERMES_HOME", "AGENTSHANGOUT_URL", "MODEL"):
            run_env[k] = v
    run_env["AHA_INSTANCE"] = INSTANCE
    for k, v in (secrets or {}).items():
        if k in SECRETS and v:
            run_env[k] = v.strip()
    return run_env


def run_script(name, args=(), secrets=None):
    """Runs one of the kit's scripts with no terminal, its output going straight to ours."""
    env = read_env()
    done = subprocess.run(["bash", str(KIT / name), *args], env=setup_env(env, secrets), stdin=subprocess.DEVNULL)
    return done.returncode


def main(argv):
    argv = list(argv)
    if len(argv) > 2 and argv[1] == "--instance":
        try:
            use_instance(argv[2])
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            return
        del argv[1:3]
    cmd = argv[1] if len(argv) > 1 else "status"
    stdin = sys.stdin.read() if cmd in ("set", "apply", "login-finish") else ""
    try:
        if cmd == "status":
            out = status()
        elif cmd == "set":
            out = {"changed": write_settings(json.loads(stdin or "{}"))}
        elif cmd == "apply":
            print("== Applying the saved settings with setup.sh", flush=True)
            sys.exit(run_script("setup.sh", secrets=json.loads(stdin or "{}").get("secrets")))
        elif cmd == "doctor":
            sys.exit(run_script("doctor.sh"))
        elif cmd == "restart":
            subprocess.run(["systemctl", "--user", "restart"] + [u for u in UNITS if "hermes" in u or "bridge" in u], check=True, timeout=120)
            out = {"restarted": True}
        elif cmd == "login-start":
            out = {"url": oauth_module(read_env()).start_login()}
        elif cmd == "login-finish":
            oauth = oauth_module(read_env())
            oauth.finish_login(stdin.strip())
            ok, listed = oauth.tool("list_hangouts", timeout=15)
            out = {"signed_in": True, "bot": listed.get("you", "") if ok and isinstance(listed, dict) else ""}
        else:
            out = {"error": f"unknown command {cmd}"}
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
        out = {"error": str(e)}
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv)
