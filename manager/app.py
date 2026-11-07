#!/usr/bin/env python3
"""The bot manager: one web page to set up and look after every Hermes bot, on whatever machine it runs.

It reaches each bot's machine over SSH (your own keys, nothing new to open) and runs the kit's
hermes/manage.py there: to read the bot's status, change its settings, sign it in with OAuth, apply
the settings with setup.sh, and run doctor.sh. Bots stay independent: each holds its own OAuth
grant and hears the site itself. This page only configures them.

    MANAGER_PASSWORD=pick-one python3 manager/app.py      then open http://127.0.0.1:8660

Settings: MANAGER_PASSWORD (required), MANAGER_LISTEN (127.0.0.1:8660), MANAGER_HOME (~/.aha-manager,
where the list of machines is kept). Secrets typed into the page go straight to the machine's setup
and are never kept here. Standard library only; runs on Linux, macOS or Windows with OpenSSH.
"""
import hmac, html, http.server, json, os, pathlib, re, secrets, subprocess, sys, threading, time, urllib.parse

HOME = pathlib.Path(os.environ.get("MANAGER_HOME", pathlib.Path.home() / ".aha-manager"))
PASSWORD = os.environ.get("MANAGER_PASSWORD", "")
LISTEN = os.environ.get("MANAGER_LISTEN", "127.0.0.1:8660")
REPO = "https://github.com/AgentsHangout/AgentsHangout-Agent.git"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# user@host for SSH (never starting with "-", which ssh would read as an option), wsl:<distro> for
# WSL on the machine running this page, or local.
TARGET_RE = re.compile(r"^(local|wsl:[A-Za-z0-9._-]{1,40}|([A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.:_-]*)$")
INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/~:-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")
COMMANDS = ("status", "set", "apply", "doctor", "restart", "login-start", "login-finish")
SECRET_FIELDS = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "AGENTSHANGOUT_TOKEN")
CAPABILITIES = {"web": "Search and read web pages", "browser": "A full browser (Chromium, about 500 MB more)",
                "github": "GitHub through its MCP server, as far as its token allows", "code": "A terminal and files in a Docker sandbox"}


def now():
    return time.time()


# ---- Machines and the SSH that reaches them --------------------------------------------------------

class Machines:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.lock = threading.Lock()

    def all(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, name):
        return self.all().get(name)

    def put(self, name, machine):
        with self.lock:
            data = self.all()
            data[name] = machine
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2))

    def remove(self, name):
        with self.lock:
            data = self.all()
            data.pop(name, None)
            self.path.write_text(json.dumps(data, indent=2))


def shell_on(machine, script):
    """argv that runs a shell line on the machine: over SSH, in WSL, or here."""
    target = machine["target"]
    if target == "local":
        return ["bash", "-lc", script]
    if target.startswith("wsl:"):
        return ["wsl.exe", "-d", target[4:], "-e", "bash", "-lc", script]
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, script]


def argv_for(machine, cmd):
    """The command that runs manage.py on the machine, for its bot (instance) if it has one. Only
    fixed words reach the remote shell; what the owner types goes on stdin."""
    instance = machine.get("instance") or ""
    if (cmd not in COMMANDS or not PATH_RE.match(machine["kit"]) or not TARGET_RE.match(machine["target"])
            or (instance and not INSTANCE_RE.match(instance))):
        raise ValueError("refused")
    which = ["--instance", instance] if instance else []
    if machine["target"] == "local":
        kit = pathlib.Path(os.path.expanduser(machine["kit"]))
        return [sys.executable, "-I", str(kit / "hermes" / "manage.py"), *which, cmd]
    return shell_on(machine, f"python3 -I {machine['kit']}/hermes/manage.py {' '.join(which + [cmd])}")


def call(machine, cmd, payload=None, timeout=90):
    """Runs a short manage.py command and returns its JSON, or {"error": ...}."""
    try:
        done = subprocess.run(argv_for(machine, cmd), input=json.dumps(payload) if payload is not None else "",
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"no answer in {timeout} seconds"}
    except (OSError, ValueError) as e:
        return {"error": str(e)}
    lines = [l for l in done.stdout.strip().splitlines() if l.strip()]
    try:
        return json.loads(lines[-1]) if lines else {"error": (done.stderr.strip() or "no answer")[-400:]}
    except ValueError:
        return {"error": (done.stderr.strip() or lines[-1])[-400:]}


class Jobs:
    """Long runs (apply, doctor, install) in the background, their output kept for the page."""

    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, title, machine_name, argv, stdin=""):
        job_id = secrets.token_urlsafe(8)
        job = {"id": job_id, "title": title, "machine": machine_name, "lines": [], "done": False, "code": None, "at": now()}
        with self.lock:
            self.jobs[job_id] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["at"])[:-30]:
                self.jobs.pop(old["id"], None)

        def run():
            try:
                p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, encoding="utf-8", errors="replace")
                p.stdin.write(stdin)
                p.stdin.close()
                for line in p.stdout:
                    job["lines"].append(line.rstrip("\n"))
                    del job["lines"][:-2000]
                job["code"] = p.wait()
            except OSError as e:
                job["lines"].append(f"Couldn't start: {e}")
                job["code"] = -1
            job["done"] = True
        threading.Thread(target=run, daemon=True).start()
        return job_id

    def get(self, job_id):
        return self.jobs.get(job_id)

    def latest(self, machine_name):
        mine = [j for j in self.jobs.values() if j["machine"] == machine_name]
        return max(mine, key=lambda j: j["at"]) if mine else None


def install_argv(machine, branch=""):
    """Clones the kit on the machine (or updates it). With a branch, checks that branch out; without,
    keeps the branch it's on, so updating never moves a machine off the branch it was set up from."""
    if (branch and not BRANCH_RE.match(branch)) or not PATH_RE.match(machine["kit"]):
        raise ValueError("refused")
    kit = machine["kit"]
    switch = f"git checkout -q {branch}; " if branch else ""
    script = (f"set -e; if [ ! -d {kit}/.git ]; then git clone -q {REPO} {kit}; fi; cd {kit}; "
              f"git fetch -q origin; {switch}git pull -q --ff-only; git log -1 --format='Kit at %h %s (%D)'")
    if not TARGET_RE.match(machine["target"]):
        raise ValueError("refused")
    return shell_on(machine, script)


# ---- The page --------------------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--quiet:#6b6b66;--line:#e2e1dc;--accent:#2f5bd3;--good:#1f7a4d;--bad:#b3261e;--code:#f0efeb}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--quiet:#a3a29c;--line:#34332f;--accent:#7f9cf5;--good:#5cc28f;--bad:#f2786d;--code:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}a{color:var(--accent)}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.quiet{color:var(--quiet)}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:16px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}.card h3{margin:0 0 4px;font-size:16px}
.row{display:flex;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding:6px 0;font-size:14px}
.good{color:var(--good)}.bad{color:var(--bad)}code{background:var(--code);border-radius:6px;padding:1px 5px;font-size:13px}
pre{background:var(--code);border-radius:8px;padding:12px;overflow-x:auto;white-space:pre-wrap;font-size:13px;max-height:560px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--quiet);font-weight:500}
input,select,textarea{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;width:100%}
textarea{min-height:90px}label{display:block;margin:14px 0 4px;font-weight:500}.hint{font-size:13px;color:var(--quiet);margin-top:3px}
.checks label{display:flex;gap:8px;align-items:flex-start;font-weight:400;margin:6px 0}.checks input{width:auto;margin-top:4px}
button{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;margin:12px 8px 0 0}
button.plain{background:transparent;color:var(--accent);border:1px solid var(--line)}button.danger{background:var(--bad)}
form.inline{display:inline}.tag{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:0 8px;margin:2px 4px 2px 0;font-size:13px}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px;padding:10px 14px;margin:14px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}@media (max-width:640px){.grid{grid-template-columns:1fr}}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def ago(t):
    if not t:
        return "never"
    s = int(now() - t)
    future = s < 0
    s = abs(s)
    words = "just now" if s < 60 else f"{s // 60} min" if s < 3600 else f"{s // 3600} h" if s < 172800 else f"{s // 86400} days"
    return words if words == "just now" else (f"in {words}" if future else f"{words} ago")


def page(title, body, csrf="", refresh=None):
    nav = (f'<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}">'
           '<button class="plain" style="margin:0">Sign out</button></form>') if csrf else ""
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>{meta}"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>"
            f"<header><div><a href='/'>Bot manager</a></div>{nav}</header>{body}</main></body></html>")


def services_line(svc):
    if not svc:
        return "<span class=quiet>unknown</span>"
    down = [u.split(".")[0] for u, s in svc.items() if s != "active" and not (u == "aha-model.service" and s == "inactive")]
    return "<span class=good>All running</span>" if not down else f"<span class=bad>Not running: {esc(', '.join(down))}</span>"


def signin_line(st):
    si = st.get("signin") or {}
    if si.get("mode") == "oauth":
        if si.get("oauth"):
            return f"<span class=good>OAuth</span> <span class=quiet>{esc(si.get('scope'))}</span>"
        return "<span class=bad>OAuth, not signed in</span>"
    return "<span class=good>Bot token</span>" + (" <span class=quiet>(OAuth grant ready too)</span>" if si.get("oauth") else "")


def tools_html(st):
    s = st.get("settings") or {}
    rt = st.get("room_tools") or {}
    if rt.get("tool_search"):
        room = "Every site tool through tool search, except " + " ".join(f"<span class=tag>{esc(t)}</span>" for t in rt.get("except", []))
    else:
        room = " ".join(f"<span class=tag>{esc(t)}</span>" for t in rt.get("listed", [])) or "<span class=quiet>not set up yet</span>"
    caps = [c for c in s.get("AHA_CAPABILITIES", "").split(",") if c]
    owner = (" ".join(f"<span class=tag>{esc(c)}</span>" for c in caps) + " on top of the room tier") if caps else "<span class=quiet>Off: no capabilities</span>"
    if "github" in caps:
        owner += f" <span class=quiet>(GitHub {'read and write' if s.get('AHA_GITHUB_WRITE') == '1' else 'read-only'})</span>"
    picked = [x for x in s.get("AHA_SKILLS", "").split(",") if x]
    skills = " ".join(f"<span class=tag>{esc(x)}</span>" for x in picked) or "<span class=quiet>none</span>"
    model = "Claude, " + (s.get("AHA_ANTHROPIC_MODEL") or "claude-haiku-5-5") if s.get("AHA_PROVIDER") == "anthropic" else "The local model"
    rows = [("Room tier", room, "Every mention, from anyone"), ("Owner tier", owner, "Your jobs and its check-ins only"),
            ("Skills", skills, "On owner-tier runs"), ("Model", esc(model), "")]
    return "<table>" + "".join(f"<tr><th>{k}</th><td>{v}<div class=hint>{h}</div></td></tr>" for k, v, h in rows) + "</table>"


def field(label, name, value, hint="", kind="text"):
    tag = (f"<textarea name={name}>{esc(value)}</textarea>" if kind == "area" else
           f"<input type={kind} name={name} value='{esc(value)}' autocomplete=off>")
    return f"<label>{label}</label>{tag}" + (f"<div class=hint>{hint}</div>" if hint else "")


def select(label, name, value, options, hint=""):
    opts = "".join(f"<option value='{esc(v)}' {'selected' if v == value else ''}>{esc(t)}</option>" for v, t in options)
    return f"<label>{label}</label><select name={name}>{opts}</select>" + (f"<div class=hint>{hint}</div>" if hint else "")


def checks(label, name, chosen, options, hint=""):
    # A lone on/off box (value "1") shows only its words; a list shows each item's name, then what it is.
    boxes = "".join(f"<label><input type=checkbox name={name} value='{esc(v)}' {'checked' if v in chosen else ''}><span>"
                    + (esc(t) if v == "1" else f"{esc(v)}<div class=hint>{esc(t)}</div>") + "</span></label>" for v, t in options)
    return f"<label>{label}</label><div class=checks>{boxes}</div>" + (f"<div class=hint>{hint}</div>" if hint else "")


def settings_form(name, st, csrf):
    s = st.get("settings") or {}
    sec = st.get("secrets") or {}
    caps = s.get("AHA_CAPABILITIES", "").split(",")
    skill_opts = [(k["id"], (k.get("description") or "") + (" (your own)" if k.get("source") == "own" else "")) for k in st.get("skills", [])]
    left = (field("Role", "AHA_ROLE", s.get("AHA_ROLE"), "A few words: the town's reporter. Empty uses the Town Crier.")
            + field("Goal", "AHA_GOAL", s.get("AHA_GOAL"), "A sentence or two about what it's for.", "area")
            + field("Owner names", "AHA_OWNER_NAMES", s.get("AHA_OWNER_NAMES"), "Comma-separated parts of your name on the site, so it knows your asks.")
            + select("Sign-in", "AHA_AUTH", s.get("AHA_AUTH") or "token", [("token", "Bot token"), ("oauth", "OAuth (mcp:bot mcp:events)")],
                     "OAuth needs a sign-in first, below.")
            + select("Model", "AHA_PROVIDER", s.get("AHA_PROVIDER") or "local", [("local", "The local model"), ("anthropic", "Claude on the Anthropic API")])
            + field("Claude model", "AHA_ANTHROPIC_MODEL", s.get("AHA_ANTHROPIC_MODEL"), "With Claude: claude-haiku-5-5 unless you set one.")
            + field("Check-ins", "AHA_SCHEDULE", s.get("AHA_SCHEDULE"), "Your local times it works on its own, such as 08:00,18:00.")
            + field("Check-in tag", "AHA_SCHEDULE_TAG", s.get("AHA_SCHEDULE_TAG"), "Who it tags when it posts what it found.")
            + field("Nightly review", "AHA_REVIEW_AT", s.get("AHA_REVIEW_AT"), "When it looks back on its day. Empty means 22:00."))
    right = (checks("Capabilities (owner tier)", "AHA_CAPABILITIES", caps, list(CAPABILITIES.items()),
                    "Your own asks and its check-ins can use these; who else can is set below.")
             + select("Who gets them", "AHA_TOOLS_FOR", s.get("AHA_TOOLS_FOR"),
                      [("", "Only you"), ("bots", "You and other bots"), ("everyone", "Everyone who mentions it")],
                      "Anyone it's open to can have it run code and search. Still capped by Owner runs a day.")
             + checks("", "AHA_GITHUB_WRITE", ["1"] if s.get("AHA_GITHUB_WRITE") == "1" else [],
                      [("1", "GitHub write: let it change repos, not just read them")])
             + checks("Skills", "AHA_SKILLS", s.get("AHA_SKILLS", "").split(","), skill_opts, "Added to owner-tier runs.")
             + field("Extra site tools", "AHA_TOOLS", s.get("AHA_TOOLS"), "Comma-separated, such as open_vote,cast_vote.")
             + select("Tool search", "AHA_TOOL_SEARCH", s.get("AHA_TOOL_SEARCH"), [("", "Automatic (on with Claude)"), ("on", "On"), ("off", "Off")])
             + checks("", "AHA_BUILDING_TOOLS", ["1"] if s.get("AHA_BUILDING_TOOLS") == "1" else [], [("1", "Building tools: start projects on The Commons")])
             + select("Social hours", "AHA_SOCIAL", s.get("AHA_SOCIAL"), [("", "On: hang out in its social hours"), ("off", "Off")])
             + field("Owner runs a day", "AHA_OWNER_RUNS_PER_DAY", s.get("AHA_OWNER_RUNS_PER_DAY"), "Cap on runs with capabilities. Empty means 40, 0 means no cap."))
    secret_fields = "".join(
        field(label, key, "", ("Saved on the machine. " if sec.get(key) else "Not set. ") + hint + " Leave empty to keep it.", "password")
        for key, label, hint in (("ANTHROPIC_API_KEY", "Anthropic API key", "Needed for Claude."),
                                 ("GITHUB_TOKEN", "GitHub token", "A fine-grained token for only the repos it may use. Needed for GitHub."),
                                 ("AGENTSHANGOUT_TOKEN", "Bot token", "Needed for token sign-in, and for the walk on the public map.")))
    return (f"<form method=post action='/m/{esc(name)}/settings'><input type=hidden name=csrf value='{esc(csrf)}'>"
            f"<div class=grid><div>{left}</div><div>{right}</div></div>"
            f"<h2>Secrets</h2><p class=quiet>Sent to the machine's setup only when you apply. Never kept here, never shown again.</p>"
            f"<div class=grid><div>{secret_fields}</div></div>"
            "<button name=then value=apply>Save and apply</button><button class=plain name=then value=save>Save only</button>"
            "<div class=hint>Applying runs setup.sh on the machine and restarts what changed: a minute or two.</div></form>")


def runs_html(st):
    runs = st.get("runs") or []
    if not runs:
        return "<p class=quiet>None yet. The bridge keeps its last 60.</p>"
    rows = "".join(f"<tr><td class=quiet>{ago(r.get('at'))}</td><td>{esc(r.get('asked'))}</td><td>{esc(r.get('said'))}</td></tr>" for r in runs)
    lessons = st.get("lessons") or []
    les = ("<h2>What it learned</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in lessons) + "</ul>") if lessons else ""
    return f"<table><tr><th>When</th><th>Asked</th><th>Said</th></tr>{rows}</table>{les}"


class App:
    def __init__(self, machines, jobs):
        self.machines, self.jobs = machines, jobs
        self.sessions = {}
        self.status_cache = {}
        self.login_links = {}

    def status(self, name, fresh=False):
        hit = self.status_cache.get(name)
        if hit and not fresh and now() - hit[0] < 30:
            return hit[1]
        st = call(self.machines.get(name), "status", timeout=60)
        self.status_cache[name] = (now(), st)
        return st

    def home(self, csrf):
        cards = []
        for name, m in sorted(self.machines.all().items()):
            st = self.status(name)
            if st.get("error"):
                body = f"<div class='row bad'>{esc(st['error'])}</div>"
            else:
                caps = (st.get("settings") or {}).get("AHA_CAPABILITIES") or "none"
                skills = (st.get("settings") or {}).get("AHA_SKILLS") or "none"
                last = (st.get("runs") or [{}])[0].get("at")
                body = (f"<div class=row><span class=quiet>Signs in</span><span>{signin_line(st)}</span></div>"
                        f"<div class=row><span class=quiet>Services</span><span>{services_line(st.get('services'))}</span></div>"
                        f"<div class=row><span class=quiet>Capabilities</span><span>{esc(caps)}</span></div>"
                        f"<div class=row><span class=quiet>Skills</span><span>{esc(skills)}</span></div>"
                        f"<div class=row><span class=quiet>Last run</span><span>{ago(last)}</span></div>")
            title = st.get("bot") or name
            cards.append(f"<div class=card><h3><a href='/m/{esc(name)}'>{esc(title)}</a></h3>"
                         f"<div class=quiet style='font-size:13px'>{esc(name)} · {esc(m['target'])}"
                         + (f" · bot {esc(m['instance'])}" if m.get("instance") else "") + f"</div>{body}</div>")
        empty = "" if cards else "<p class=quiet>No machines yet.</p>"
        return page("Bot manager", "<h1>Bots</h1><p class=quiet>Each bot runs on its own machine and signs in itself. "
                    f"This page sets them up.</p><div class=cards>{''.join(cards)}</div>{empty}"
                    "<p><a href='/m/new'>Add a machine</a></p>", csrf)

    def new_form(self, csrf, error=""):
        return page("Add a machine", f"<h1>Add a machine</h1>{f'<p class=bad>{esc(error)}</p>' if error else ''}"
                    f"<form method=post action='/m'><input type=hidden name=csrf value='{esc(csrf)}'>"
                    + field("Short name", "name", "", "Lowercase letters, numbers and dashes: jetson.")
                    + field("Where it runs", "target", "", "user@host, as you'd type after ssh (your SSH key must work without a password); "
                            "wsl:Ubuntu for WSL on this computer; or local.")
                    + field("Which bot there", "instance", "", "Empty for the machine's first bot. A short name (tester) for another bot on the same machine: "
                            "its own settings, ports and services, on Claude.")
                    + field("Kit folder on it", "kit", "~/AgentsHangout-Agent", "Where the kit is, or will be cloned.")
                    + field("Branch", "branch", "", "Empty keeps the branch the kit is on (main when cloning). "
                            "Name one only to switch the kit to it.")
                    + checks("", "install", [], [("1", "Clone or update the kit there now")])
                    + "<button>Add</button></form>", csrf)

    def machine_page(self, name, csrf, fresh=False, notice=""):
        m = self.machines.get(name)
        if not m:
            return None
        st = self.status(name, fresh)
        c = f"<input type=hidden name=csrf value='{esc(csrf)}'>"
        where = m["target"] + (f" · bot {m['instance']}" if m.get("instance") else "")
        parts = [f"<h1>{esc(st.get('bot') or name)}</h1><p class=quiet>{esc(name)} · {esc(where)}"
                 + (f" · {esc(st.get('host'))}" if st.get("host") else "") + "</p>"]
        if notice:
            parts.append(f"<div class=note>{notice}</div>")
        job = self.jobs.latest(name)
        if job:
            state = "running" if not job["done"] else ("finished" if job["code"] == 0 else f"failed (exit {job['code']})")
            parts.append(f"<div class=note>{esc(job['title'])}: {state}. <a href='/jobs/{job['id']}'>See the output</a></div>")
        actions = (f"<form class=inline method=post action='/m/{esc(name)}/refresh'>{c}<button class=plain>Refresh</button></form>"
                   f"<form class=inline method=post action='/m/{esc(name)}/doctor'>{c}<button class=plain>Run doctor</button></form>"
                   f"<form class=inline method=post action='/m/{esc(name)}/restart'>{c}<button class=plain>Restart the bot</button></form>"
                   f"<form class=inline method=post action='/m/{esc(name)}/install'>{c}"
                   "<button class=plain>Update the kit</button></form>")
        parts.append(actions)
        if st.get("error"):
            parts.append(f"<p class=bad>Couldn't read the machine: {esc(st['error'])}</p>"
                         "<p class=quiet>Check that ssh works without a password, and that the kit is there (Update the kit clones it).</p>")
            return page(name, "".join(parts), csrf)
        if not st.get("set_up"):
            parts.append("<div class=note><b>Not set up yet.</b> 1. Sign in with OAuth below and pick the bot. "
                         "2. In Settings choose OAuth and Claude, tick its capabilities and skills, and give the Anthropic key "
                         "(and a GitHub token if it has GitHub). 3. Save and apply: setup.sh puts it together and starts it.</div>")
        si = st.get("signin") or {}
        renew = f" Access renews {ago(si.get('expires_at'))}." if si.get("oauth") else ""
        link = self.login_links.get(name)
        signin = [f"<h2>Sign-in</h2><p>{signin_line(st)}.{renew}</p>"]
        if link and now() - link[0] < 900:
            signin.append(f"<div class=note><b>1.</b> Open <a href='{esc(link[1])}' target=_blank rel=noopener>this sign-in link</a>, pick the bot, "
                          "and keep \"Allow mention webhooks\" ticked.<br><b>2.</b> The last page won't load: that's expected. Copy its address "
                          f"(it starts http://127.0.0.1) and paste it here.<form method=post action='/m/{esc(name)}/login-finish'>{c}"
                          "<input name=address placeholder='http://127.0.0.1:27891/callback?code=...' autocomplete=off>"
                          "<button>Finish signing in</button></form></div>")
        else:
            signin.append(f"<form method=post action='/m/{esc(name)}/login'>{c}<button class=plain>"
                          f"{'Sign in again with OAuth' if si.get('oauth') else 'Sign in with OAuth'}</button></form>")
        if si.get("oauth") and si.get("mode") != "oauth":
            signin.append("<p class=quiet>Signed in, but the bot still uses its token. Set Sign-in to OAuth below and apply.</p>")
        parts.append("".join(signin))
        parts.append(f"<h2>What it can use</h2>{tools_html(st)}")
        parts.append("<h2>Services</h2><table>" + "".join(
            f"<tr><td>{esc(u)}</td><td class={'good' if v == 'active' else 'bad'}>{esc(v)}</td></tr>" for u, v in (st.get("services") or {}).items()) + "</table>")
        parts.append(f"<h2>Settings</h2>{settings_form(name, st, csrf)}")
        parts.append(f"<h2>Recent runs</h2>{runs_html(st)}")
        kit = st.get("kit") or {}
        parts.append(f"<h2>The kit</h2><p class=quiet>{esc(kit.get('path'))} · {esc(kit.get('branch'))} · {esc(kit.get('commit'))}</p>"
                     f"<form method=post action='/m/{esc(name)}/remove' onsubmit=\"return confirm('Remove {esc(name)} from this page? The bot keeps running.')\">{c}"
                     "<button class=danger>Remove from this page</button></form>")
        return page(st.get("bot") or name, "".join(parts), csrf)

    def job_page(self, job, csrf):
        state = "Running…" if not job["done"] else ("Finished." if job["code"] == 0 else f"Failed (exit {job['code']}).")
        return page(job["title"], f"<h1>{esc(job['title'])}</h1><p><a href='/m/{esc(job['machine'])}'>Back to {esc(job['machine'])}</a></p>"
                    f"<p class={'quiet' if not job['done'] else 'good' if job['code'] == 0 else 'bad'}>{state}</p>"
                    f"<pre>{esc(chr(10).join(job['lines'][-400:]))}</pre>", csrf, refresh=None if job["done"] else 3)

    # -- actions --
    def save_settings(self, name, form):
        multi = ("AHA_CAPABILITIES", "AHA_SKILLS", "AHA_GITHUB_WRITE", "AHA_BUILDING_TOOLS")
        keys = ("AHA_ROLE", "AHA_GOAL", "AHA_OWNER_NAMES", "AHA_AUTH", "AHA_PROVIDER", "AHA_ANTHROPIC_MODEL", "AHA_SCHEDULE",
                "AHA_SCHEDULE_TAG", "AHA_REVIEW_AT", "AHA_TOOLS", "AHA_TOOL_SEARCH", "AHA_SOCIAL", "AHA_OWNER_RUNS_PER_DAY", "AHA_TOOLS_FOR")
        changes = {k: (form.get(k) or [""])[0] for k in keys}
        changes.update({k: ",".join(v for v in form.get(k, []) if v) for k in multi})
        return call(self.machines.get(name), "set", changes)


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = "aha-manager"

    def log_message(self, *a):
        pass

    def send(self, status, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, extra=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def session(self):
        m = re.search(r"(?:^|;\s*)aha_manager=([\w-]+)", self.headers.get("Cookie", ""))
        s = self.app.sessions.get(m.group(1)) if m else None
        return s[0] if s and s[1] > now() else None

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        try:
            self._route(method)
        except BrokenPipeError:
            pass
        except Exception as e:
            print("error:", repr(e), file=sys.stderr, flush=True)
            try:
                self.send(500, page("Error", f"<h1>Something went wrong</h1><p>{esc(e)}</p>"))
            except Exception:
                pass

    def _route(self, method):
        app = self.app
        path = urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace"), keep_blank_values=True) if method == "POST" and n else {}
        one = lambda k: (form.get(k) or [""])[0]
        if path == "/login":
            if method == "POST":
                if PASSWORD and hmac.compare_digest(one("password"), PASSWORD):
                    sid = secrets.token_urlsafe(32)
                    app.sessions[sid] = (secrets.token_urlsafe(24), now() + 12 * 3600)
                    return self.redirect("/", {"Set-Cookie": f"aha_manager={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})
                time.sleep(1)
            return self.send(200, page("Sign in", "<h1>Bot manager</h1><form method=post action='/login'><label>Password</label>"
                                       "<input type=password name=password autofocus><button>Sign in</button></form>"))
        csrf = self.session()
        if not csrf:
            return self.redirect("/login")
        if method == "POST" and not hmac.compare_digest(one("csrf"), csrf):
            return self.send(403, page("Refused", "<h1>That form expired. Go back and try again.</h1>"))
        if method == "GET":
            if path == "/":
                return self.send(200, app.home(csrf))
            if path == "/m/new":
                return self.send(200, app.new_form(csrf))
            m = re.match(r"^/m/([a-z0-9-]+)$", path)
            if m:
                body = app.machine_page(m.group(1), csrf)
                return self.send(200, body) if body else self.send(404, page("Not found", "<h1>No such machine</h1>", csrf))
            m = re.match(r"^/jobs/([\w-]+)$", path)
            if m and app.jobs.get(m.group(1)):
                return self.send(200, app.job_page(app.jobs.get(m.group(1)), csrf))
            return self.send(404, page("Not found", "<h1>Not found</h1>", csrf))
        return self.post(path, form, one, csrf)

    def post(self, path, form, one, csrf):
        app = self.app
        if path == "/logout":
            m = re.search(r"aha_manager=([\w-]+)", self.headers.get("Cookie", ""))
            if m:
                app.sessions.pop(m.group(1), None)
            return self.redirect("/login", {"Set-Cookie": "aha_manager=; Path=/; Max-Age=0"})
        if path == "/m":
            name, target, kit, branch = one("name").strip(), one("target").strip(), one("kit").strip() or "~/AgentsHangout-Agent", one("branch").strip()
            instance = one("instance").strip()
            if instance and not INSTANCE_RE.match(instance):
                return self.send(400, app.new_form(csrf, "The bot's name there is lowercase letters, numbers and dashes."))
            if not NAME_RE.match(name):
                return self.send(400, app.new_form(csrf, "Pick a short name: lowercase letters, numbers and dashes."))
            if app.machines.get(name):
                return self.send(400, app.new_form(csrf, f"There's already a machine called {name}."))
            if not TARGET_RE.match(target) or not PATH_RE.match(kit) or (branch and not BRANCH_RE.match(branch)):
                return self.send(400, app.new_form(csrf, "That SSH target, folder or branch has characters this page won't pass on."))
            app.machines.put(name, dict({"target": target, "kit": kit}, **({"instance": instance} if instance else {})))
            if one("install") == "1":
                job = app.jobs.start(f"Cloning or updating the kit{f' ({branch})' if branch else ''}", name, install_argv(app.machines.get(name), branch))
                return self.redirect(f"/jobs/{job}")
            return self.redirect(f"/m/{name}")
        m = re.match(r"^/m/([a-z0-9-]+)/(refresh|doctor|restart|install|settings|login|login-finish|remove)$", path)
        if not m or not app.machines.get(m.group(1)):
            return self.send(404, page("Not found", "<h1>Not found</h1>", csrf))
        name, action = m.groups()
        machine = app.machines.get(name)
        if action == "refresh":
            return self.send(200, app.machine_page(name, csrf, fresh=True))
        if action == "doctor":
            return self.redirect(f"/jobs/{app.jobs.start('Doctor', name, argv_for(machine, 'doctor'))}")
        if action == "install":
            branch = one("branch").strip()
            if branch and not BRANCH_RE.match(branch):
                return self.send(400, page("Refused", "<h1>That branch name won't be passed on.</h1>", csrf))
            title = "Updating the kit" + (f" ({branch})" if branch else "")
            return self.redirect(f"/jobs/{app.jobs.start(title, name, install_argv(machine, branch))}")
        if action == "restart":
            out = call(machine, "restart", timeout=150)
            app.status_cache.pop(name, None)
            note = "Restarted Hermes and the bridge." if not out.get("error") else f"<span class=bad>{esc(out['error'])}</span>"
            return self.send(200, app.machine_page(name, csrf, notice=note))
        if action == "login":
            out = call(machine, "login-start")
            if out.get("error") or not out.get("url", "").startswith("https://"):
                return self.send(200, app.machine_page(name, csrf, notice=f"<span class=bad>Couldn't start the sign-in: {esc(out.get('error') or 'no link')}</span>"))
            app.login_links[name] = (now(), out["url"])
            return self.send(200, app.machine_page(name, csrf))
        if action == "login-finish":
            out = _finish(machine, one("address"))
            app.login_links.pop(name, None)
            app.status_cache.pop(name, None)
            note = (f"Signed in as {esc(out.get('bot') or 'the bot')}." if out.get("signed_in")
                    else f"<span class=bad>Sign-in didn't finish: {esc(out.get('error'))}</span>")
            return self.send(200, app.machine_page(name, csrf, fresh=True, notice=note))
        if action == "settings":
            out = app.save_settings(name, form)
            app.status_cache.pop(name, None)
            if out.get("error"):
                return self.send(400, app.machine_page(name, csrf, notice=f"<span class=bad>Not saved: {esc(out['error'])}</span>"))
            if one("then") == "apply":
                secrets_given = {k: one(k).strip() for k in SECRET_FIELDS if one(k).strip()}
                job = app.jobs.start("Applying the settings", name, argv_for(machine, "apply"), json.dumps({"secrets": secrets_given}))
                return self.redirect(f"/jobs/{job}")
            changed = ", ".join(out.get("changed") or {}) or "nothing"
            return self.send(200, app.machine_page(name, csrf, fresh=True, notice=f"Saved ({esc(changed)} changed). Apply to put it into effect."))
        if action == "remove":
            app.machines.remove(name)
            return self.redirect("/")
        return self.redirect(f"/m/{name}")


def _finish(machine, address):
    """login-finish takes the address on stdin as plain text, not JSON."""
    try:
        done = subprocess.run(argv_for(machine, "login-finish"), input=address.strip(), capture_output=True, text=True, timeout=60)
        lines = [l for l in done.stdout.splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else {"error": done.stderr.strip()[-300:] or "no answer"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}


def serve(app, listen=LISTEN):
    host, _, port = listen.rpartition(":")
    Handler.app = app
    httpd = http.server.ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    httpd.daemon_threads = True
    return httpd


def main():
    if not PASSWORD:
        sys.exit("Set MANAGER_PASSWORD: the page needs a password.")
    app = App(Machines(HOME / "machines.json"), Jobs())
    httpd = serve(app)
    print(f"Bot manager on http://{LISTEN}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
