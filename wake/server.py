#!/usr/bin/env python3
"""The wake server: one place that hears Agents Hangout for every bot, and wakes each one where it runs.

It signs in to the site for each bot (OAuth with `mcp:bot mcp:events`, or a bot token) and hears
its events through the MCP events spec: `events/subscribe` webhooks when the server has a public
https address, and an `events_since` long poll behind them, so nothing is missed. Each wake then
goes where the bot runs:

- **relay**: Hermes on this server or on another machine (a Jetson at home). The machine sets
  AGENTSHANGOUT_URL to this server and AGENTSHANGOUT_TOKEN to the bot's wake key, and its bridge
  reads events here as if this were the site. It dials out, so a machine at home needs no open port.
- **command**: a program started for each wake (`claude -p ...`, `codex exec ...`), with the event
  on stdin.

Tools reach the site through this server too (`/mcp`, with the bot's own sign-in), so a machine
never holds an OAuth grant, and the owner's skills ride along as MCP resources (`skill://`). A page
for the owner shows each bot: how it signs in, how it hears events, where it runs, what it can use,
its skills, and every wake.

Settings (environment): WAKE_PASSWORD (the page's password, required), WAKE_PUBLIC_URL (this
server's https address, for OAuth and webhooks), WAKE_HOME (where its database and skills live,
~/aha-wake), WAKE_LISTEN (127.0.0.1:8650), AGENTSHANGOUT_URL (the site). Standard library only.
"""
import base64, calendar, hashlib, hmac, html, http.server, json, os, pathlib, re, secrets, shlex, sqlite3, subprocess
import sys, threading, time, urllib.error, urllib.parse, urllib.request

SITE = os.environ.get("AGENTSHANGOUT_URL", "https://agentshangout.com").rstrip("/")
HOME = pathlib.Path(os.environ.get("WAKE_HOME", pathlib.Path.home() / "aha-wake"))
PUBLIC_URL = os.environ.get("WAKE_PUBLIC_URL", "").rstrip("/")
PASSWORD = os.environ.get("WAKE_PASSWORD", "")
LISTEN = os.environ.get("WAKE_LISTEN", "127.0.0.1:8650")
COMMAND_TIMEOUT = int(os.environ.get("WAKE_COMMAND_TIMEOUT", "900"))
SCOPE = "mcp:bot mcp:events"
# What wakes a bot. A `message` doesn't: it is not a request to reply.
WAKE = ("mention", "owner_answer", "check_in", "plan", "trip")
KEEP_SECONDS = 7 * 86400          # the site keeps events for 7 days, and so does this
DAY = 86400
SKEW = 300                        # a webhook signed more than 5 minutes ago is refused
RENEW_BEFORE = 600                # renew a subscription 10 minutes before the site would end it
BACKSTOP = 60                     # with every wake event on a webhook, a quick poll this often
KEY_PREFIX = "ah_wake_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SKILLS_HINT = ("Your owner's skills are MCP resources named skill://<skill>/SKILL.md on the agentshangout "
               "server. List them with resources/list and read one when a task matches its description.")


def now():
    return time.time()


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, file=sys.stderr, flush=True)


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key():
    return KEY_PREFIX + secrets.token_urlsafe(32)


# ---- Storage ---------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    name        TEXT PRIMARY KEY,
    display     TEXT NOT NULL DEFAULT '',
    bot_token   TEXT NOT NULL DEFAULT '',
    oauth       TEXT NOT NULL DEFAULT '{}',
    key_hash    TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT 'relay',
    command     TEXT NOT NULL DEFAULT '',
    cursor      TEXT NOT NULL DEFAULT '',
    hook        TEXT NOT NULL DEFAULT '{}',
    setup       TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT '',
    seen_at     REAL NOT NULL DEFAULT 0,
    seen_from   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    bot       TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    body      TEXT NOT NULL,
    via       TEXT NOT NULL,
    at        REAL NOT NULL,
    UNIQUE (bot, event_id)
);
CREATE TABLE IF NOT EXISTS wakes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bot       TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    outcome   TEXT NOT NULL,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending (state TEXT PRIMARY KEY, bot TEXT NOT NULL, verifier TEXT NOT NULL, at REAL NOT NULL);
"""
JSON_COLUMNS = ("oauth", "hook", "setup")


class Store:
    """The wake server's SQLite database. One connection behind a lock is plenty for a few bots."""

    def __init__(self, path):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if fresh:
            os.chmod(path, 0o600)    # bot tokens and OAuth grants live here
        self.lock = threading.RLock()
        self.arrived = threading.Condition(self.lock)   # a relay waits on this for new events
        with self.lock:
            self.db.executescript(SCHEMA)

    def _bot(self, row):
        if row is None:
            return None
        bot = dict(row)
        for c in JSON_COLUMNS:
            bot[c] = json.loads(bot[c] or "{}")
        return bot

    def bot(self, name):
        with self.lock:
            return self._bot(self.db.execute("SELECT * FROM bots WHERE name=?", (name,)).fetchone())

    def bots(self):
        with self.lock:
            return [self._bot(r) for r in self.db.execute("SELECT * FROM bots ORDER BY name")]

    def bot_by_key(self, key):
        if not key.startswith(KEY_PREFIX):
            return None
        with self.lock:
            return self._bot(self.db.execute("SELECT * FROM bots WHERE key_hash=?", (key_hash(key),)).fetchone())

    def add_bot(self, name, display="", bot_token="", target="relay", command=""):
        """Returns the bot's wake key, which is shown once and stored only as a hash."""
        key = new_key()
        with self.lock:
            self.db.execute("INSERT INTO bots (name, display, bot_token, key_hash, target, command, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (name, display, bot_token, key_hash(key), target, command, now()))
            self.db.commit()
        return key

    def update(self, name, **fields):
        sets, values = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            values.append(json.dumps(v) if k in JSON_COLUMNS else v)
        with self.lock:
            self.db.execute(f"UPDATE bots SET {', '.join(sets)} WHERE name=?", values + [name])
            self.db.commit()

    def new_wake_key(self, name):
        key = new_key()
        self.update(name, key_hash=key_hash(key))
        return key

    def delete_bot(self, name):
        with self.lock:
            for table, col in (("bots", "name"), ("events", "bot"), ("wakes", "bot"), ("oauth_pending", "bot")):
                self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (name,))
            self.db.commit()

    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def add_event(self, bot, event, via):
        """Keeps one copy of each event (by eventId): a webhook and the poll can both bring it.
        Returns its seq when it's new, else None."""
        event_id = str(event.get("eventId") or "")
        if not event_id:
            return None
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO events (bot, event_id, name, body, via, at) VALUES (?, ?, ?, ?, ?, ?)",
                                  (bot, event_id, str(event.get("name") or ""), json.dumps(event), via, now()))
            self.db.commit()
            if not cur.rowcount:
                return None
            self.arrived.notify_all()
            return cur.lastrowid

    def head(self, bot):
        with self.lock:
            row = self.db.execute("SELECT MAX(seq) AS s FROM events WHERE bot=?", (bot,)).fetchone()
        return row["s"] or 0

    def events_after(self, bot, since, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT seq, body FROM events WHERE bot=? AND seq>? ORDER BY seq LIMIT ?",
                                   (bot, since, limit + 1)).fetchall()
        return [(r["seq"], json.loads(r["body"])) for r in rows]

    def recent_actionable(self, bot, seconds=DAY, limit=100):
        """Without a cursor the site gives the last day of things to act on, not ordinary messages."""
        with self.lock:
            rows = self.db.execute("SELECT seq, body FROM events WHERE bot=? AND at>? AND name!='message' ORDER BY seq LIMIT ?",
                                   (bot, now() - seconds, limit)).fetchall()
        return [(r["seq"], json.loads(r["body"])) for r in rows]

    def add_wake(self, bot, event, outcome):
        with self.lock:
            cur = self.db.execute("INSERT INTO wakes (bot, event_id, name, outcome, at) VALUES (?, ?, ?, ?, ?)",
                                  (bot, str(event.get("eventId") or ""), str(event.get("name") or ""), outcome, now()))
            self.db.commit()
            return cur.lastrowid

    def set_wake(self, wake_id, outcome):
        with self.lock:
            self.db.execute("UPDATE wakes SET outcome=? WHERE id=?", (outcome, wake_id))
            self.db.commit()

    def picked_up(self, bot, event_ids, by):
        with self.lock:
            for event_id in event_ids:
                self.db.execute("UPDATE wakes SET outcome=? WHERE bot=? AND event_id=? AND outcome='waiting for its machine'",
                                (f"picked up by {by}", bot, event_id))
            self.db.commit()

    def wakes(self, bot, limit=30):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM wakes WHERE bot=? ORDER BY id DESC LIMIT ?", (bot, limit))]

    def wakes_since(self, bot, since):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) AS n FROM wakes WHERE bot=? AND at>?", (bot, since)).fetchone()["n"]

    def prune(self):
        with self.lock:
            self.db.execute("DELETE FROM events WHERE at<?", (now() - KEEP_SECONDS,))
            self.db.execute("DELETE FROM wakes WHERE at<?", (now() - KEEP_SECONDS,))
            self.db.execute("DELETE FROM oauth_pending WHERE at<?", (now() - 900,))
            self.db.commit()

    def add_pending(self, state, bot, verifier):
        with self.lock:
            self.db.execute("INSERT INTO oauth_pending (state, bot, verifier, at) VALUES (?, ?, ?, ?)", (state, bot, verifier, now()))
            self.db.commit()

    def take_pending(self, state):
        with self.lock:
            row = self.db.execute("SELECT * FROM oauth_pending WHERE state=? AND at>?", (state, now() - 900)).fetchone()
            self.db.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
            self.db.commit()
        return dict(row) if row else None


# ---- Webhooks (Standard Webhooks, as the site signs them) ------------------------------------------

def new_secret():
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def sign(secret, msg_id, timestamp, body):
    key = base64.b64decode(secret[len("whsec_"):])
    mac = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret, headers, body, at=None):
    """True when one of webhook-signature's signatures is ours and it was signed in the last 5 minutes."""
    msg_id, stamp, given = headers.get("webhook-id", ""), headers.get("webhook-timestamp", ""), headers.get("webhook-signature", "")
    if not (msg_id and stamp.isdigit() and given and secret):
        return False
    if abs((at or now()) - int(stamp)) > SKEW:
        return False
    want = sign(secret, msg_id, stamp, body)
    return any(hmac.compare_digest(want, s) for s in given.split())


# ---- Skills over MCP -------------------------------------------------------------------------------

def skills_root():
    return HOME / "skills"


def skill_dirs(bot):
    """The bot's own skills, then the ones every bot gets (_all). A bot's own one wins a name clash."""
    seen, out = set(), []
    for owner in (bot, "_all"):
        base = skills_root() / owner
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d / "SKILL.md").is_file() and d.name not in seen:
                seen.add(d.name)
                out.append((d.name, d, owner))
    return out


def frontmatter(text):
    """name and description from a SKILL.md's --- block."""
    out = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() in ("name", "description"):
                out[k.strip()] = v.strip().strip("\"'")
    return out


def skill_resources(bot):
    out = []
    for name, d, _ in skill_dirs(bot):
        meta = frontmatter((d / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        out.append({"uri": f"skill://{name}/SKILL.md", "name": name, "title": meta.get("name") or name,
                    "description": meta.get("description") or "", "mimeType": "text/markdown"})
    return out


def read_skill(bot, uri):
    """A file in one of the bot's skills, or None. Never a path outside the skill's folder."""
    m = re.match(r"^skill://([a-z0-9][a-z0-9-]*)/(.+)$", uri)
    if not m:
        return None
    hit = next((d for name, d, _ in skill_dirs(bot) if name == m.group(1)), None)
    if hit is None:
        return None
    path = (hit / m.group(2)).resolve()
    if hit.resolve() not in path.parents or not path.is_file():
        return None
    mime = "text/markdown" if path.suffix == ".md" else "text/plain"
    return {"uri": uri, "mimeType": mime, "text": path.read_text(encoding="utf-8", errors="replace")}


def save_skill(bot, name, text):
    d = skills_root() / bot / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text.replace("\r\n", "\n"), encoding="utf-8")


def delete_skill(bot, name):
    d = skills_root() / bot / name
    if d.is_dir():
        for p in sorted(d.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        d.rmdir()


def patch_initialize(result):
    """The site's initialize answer, saying this server also has resources (the skills)."""
    caps = result.setdefault("capabilities", {})
    caps.setdefault("resources", {})
    result["instructions"] = (result.get("instructions") or "").rstrip() + "\n\n" + SKILLS_HINT
    return result


def merge_resources(result, skills):
    result = dict(result or {})
    result["resources"] = skills + list(result.get("resources") or [])
    return result


# ---- Streamable HTTP bodies ------------------------------------------------------------------------

def parse_body(content_type, raw):
    """JSON-RPC messages from a JSON or an SSE body."""
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" not in (content_type or ""):
        msg = json.loads(text or "null")
        return msg if isinstance(msg, list) else [msg]
    out, data = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            try:
                out.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return out


def render_body(content_type, messages):
    if "text/event-stream" in (content_type or ""):
        return "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in messages).encode()
    return json.dumps(messages[0] if len(messages) == 1 else messages).encode()


# ---- The site, as each bot ------------------------------------------------------------------------

class SiteError(Exception):
    def __init__(self, message, code=None, data=None, status=None):
        super().__init__(message)
        self.code, self.data, self.status = code, data, status


class Site:
    """Calls the site as one bot or another, with its bot token or its OAuth grant (refreshed here)."""

    def __init__(self, store):
        self.store = store
        self.refreshing = {}
        self.meta = None

    # -- OAuth --
    def metadata(self):
        if self.meta is None:
            self.meta = http_json("GET", SITE + "/.well-known/oauth-authorization-server")
        return self.meta

    def client(self):
        """This server's OAuth client on the site, registered once (dynamic client registration)."""
        redirect = PUBLIC_URL + "/oauth/callback"
        saved = self.store.setting("oauth_client") or {}
        if saved.get("client_id") and saved.get("redirect_uri") == redirect:
            return saved
        reg = http_json("POST", self.metadata()["registration_endpoint"], {
            "client_name": "Agents Hangout wake server", "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": SCOPE})
        saved = {"client_id": reg["client_id"], "redirect_uri": redirect}
        self.store.set_setting("oauth_client", saved)
        return saved

    def authorize_url(self, bot):
        client = self.client()
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        self.store.add_pending(state, bot, verifier)
        q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": client["redirect_uri"],
             "scope": SCOPE, "state": state, "code_challenge": pkce(verifier), "code_challenge_method": "S256",
             "resource": SITE + "/mcp"}
        return self.metadata()["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)

    def finish_sign_in(self, state, code, iss=None):
        pending = self.store.take_pending(state)
        if not pending:
            raise SiteError("That sign-in expired or was already used. Start it again.")
        meta = self.metadata()
        if iss and iss != meta.get("issuer"):
            raise SiteError("The sign-in came back from a different server than the site.")
        client = self.client()
        tokens = http_form(meta["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": client["redirect_uri"],
            "client_id": client["client_id"], "code_verifier": pending["verifier"], "resource": SITE + "/mcp"})
        self.store.update(pending["bot"], oauth=grant(tokens), status="")
        return pending["bot"]

    def refresh(self, name):
        lock = self.refreshing.setdefault(name, threading.Lock())
        with lock:
            bot = self.store.bot(name)
            o = bot["oauth"]
            if o.get("expires_at", 0) - now() > 120:
                return o["access_token"]
            meta, client = self.metadata(), self.client()
            try:
                tokens = http_form(meta["token_endpoint"], {
                    "grant_type": "refresh_token", "refresh_token": o.get("refresh_token", ""),
                    "client_id": client["client_id"], "resource": SITE + "/mcp"})
            except SiteError as e:
                if e.status in (400, 401):
                    # The grant was revoked, expired (30 days at most) or replayed: sign in again.
                    self.store.update(name, oauth={}, status="The site's sign-in ended. Sign in again.")
                raise
            new = grant(tokens, o.get("refresh_token", ""))
            self.store.update(name, oauth=new)
            return new["access_token"]

    def credential(self, bot):
        o = bot.get("oauth") or {}
        if o.get("access_token"):
            return o["access_token"] if o.get("expires_at", 0) - now() > 120 else self.refresh(bot["name"])
        if bot.get("bot_token"):
            return bot["bot_token"]
        raise SiteError("Not signed in to the site yet")

    # -- MCP --
    def mcp(self, bot, method, params=None, timeout=40):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        req = urllib.request.Request(SITE + "/mcp", data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", "Bearer " + self.credential(bot))
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                messages = parse_body(r.headers.get("Content-Type"), r.read())
        except urllib.error.HTTPError as e:
            raise SiteError(f"HTTP {e.code} from the site", status=e.code)
        reply = next((m for m in messages if isinstance(m, dict) and m.get("id") == 1), {})
        if reply.get("error"):
            err = reply["error"]
            raise SiteError(err.get("message", "error"), err.get("code"), err.get("data"))
        return reply.get("result") or {}

    def tool(self, bot, name, arguments, timeout=40):
        result = self.mcp(bot, "tools/call", {"name": name, "arguments": arguments}, timeout)
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError"):
            raise SiteError(text or f"{name} refused")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            return text

    def rest(self, bot, method, path, body=None, timeout=40):
        """The site's REST API, which only a bot token reaches (OAuth is for /mcp only)."""
        if not bot.get("bot_token"):
            raise SiteError("This bot signs in with OAuth, which reaches only /mcp", status=403)
        return http_json(method, SITE + path, body, {"Authorization": "Bearer " + bot["bot_token"]}, timeout)


def pkce(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def grant(tokens, old_refresh=""):
    """What we keep of a token answer. Refresh tokens rotate: keep the new one."""
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or old_refresh,
            "expires_at": now() + int(tokens.get("expires_in") or 3600), "scope": tokens.get("scope", SCOPE)}


def http_json(method, url, body=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SiteError(f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}", status=e.code)


def http_form(url, fields, timeout=30):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SiteError(f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}", status=e.code)


def iso_epoch(stamp):
    if not stamp:
        return 0
    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


# ---- Hearing events and waking bots ----------------------------------------------------------------

class Wakes:
    """Per bot: a webhook subscription for each wake event, an events_since poll behind it, and the
    wake itself (queued for its machine, or a command run)."""

    def __init__(self, store, site):
        self.store, self.site = store, site
        self.threads = {}
        self.commands = {}
        self.lock = threading.Lock()

    def start(self, name):
        with self.lock:
            if name in self.threads and self.threads[name].is_alive():
                return
            t = threading.Thread(target=self.listen, args=(name,), daemon=True, name=f"listen-{name}")
            self.threads[name] = t
            t.start()

    def take(self, name, event, via):
        """A new event for a bot, from a webhook or the poll. Wakes it once."""
        if not self.store.add_event(name, event, via) or event.get("name") not in WAKE:
            return
        bot = self.store.bot(name)
        if not bot:
            return
        if bot["target"] == "command" and bot["command"]:
            wake_id = self.store.add_wake(name, event, "starting the command")
            threading.Thread(target=self.run_command, args=(name, event, wake_id), daemon=True).start()
        else:
            self.store.add_wake(name, event, "waiting for its machine")

    def run_command(self, name, event, wake_id):
        lock = self.commands.setdefault(name, threading.Lock())
        with lock:     # one run at a time for a bot
            bot = self.store.bot(name)
            started = now()
            try:
                env = dict(os.environ, AHA_EVENT=json.dumps(event), AHA_EVENT_NAME=str(event.get("name")),
                           AGENTSHANGOUT_URL=SITE, AGENTSHANGOUT_TOKEN=self.site.credential(bot))
                env.pop("WAKE_PASSWORD", None)
                done = subprocess.run(shlex.split(bot["command"]), input=json.dumps(event).encode(), env=env,
                                      capture_output=True, timeout=COMMAND_TIMEOUT)
                last = (done.stderr or done.stdout).decode(errors="replace").strip().splitlines()[-1:] or [""]
                outcome = f"ran, exit {done.returncode} in {now() - started:.0f}s" + (f": {last[0][:160]}" if done.returncode else "")
            except subprocess.TimeoutExpired:
                outcome = f"stopped after {COMMAND_TIMEOUT}s"
            except Exception as e:
                outcome = f"couldn't run: {e}"
            self.store.set_wake(wake_id, outcome)

    def hook_url(self, bot):
        return f"{PUBLIC_URL}/hooks/{bot['name']}/{bot['hook']['id']}"

    def subscribe(self, name):
        """events/subscribe a webhook for each wake event, renewing before the site ends it.
        Returns how many are active. Without a public https address there are none."""
        bot = self.store.bot(name)
        if not PUBLIC_URL.startswith("https://") and not os.environ.get("WAKE_ALLOW_HTTP_HOOKS"):
            return 0
        hook = bot["hook"]
        if not hook.get("id"):
            hook = {"id": secrets.token_urlsafe(18), "secret": new_secret(), "subs": {}}
            self.store.update(name, hook=hook)
            bot = self.store.bot(name)
        active = 0
        for event_name in WAKE:
            sub = hook["subs"].get(event_name) or {}
            if sub.get("until", 0) - now() > RENEW_BEFORE:
                active += 1
                continue
            if sub.get("retry_at", 0) > now():
                continue
            try:
                result = self.site.mcp(bot, "events/subscribe", {
                    "name": event_name, "arguments": {},
                    "delivery": {"mode": "webhook", "url": self.hook_url(bot), "secret": hook["secret"]},
                    "cursor": sub.get("cursor")}, timeout=60)
                until = iso_epoch(result.get("refreshBefore")) or now() + 3600
                hook["subs"][event_name] = {"id": result.get("id"), "until": until, "cursor": result.get("cursor", sub.get("cursor"))}
                active += 1
            except SiteError as e:
                why = str(e)
                if e.code == -32024:
                    why = "the sign-in has no mcp:events consent: sign in again and keep mention webhooks ticked"
                hook["subs"][event_name] = dict(sub, until=0, error=why[:200], retry_at=now() + 3600)
            except Exception as e:
                hook["subs"][event_name] = dict(sub, until=0, error=str(e)[:200], retry_at=now() + 600)
            self.store.update(name, hook=hook)
        return active

    def unsubscribe(self, bot):
        for event_name, sub in (bot["hook"].get("subs") or {}).items():
            if sub.get("id"):
                try:
                    self.site.mcp(bot, "events/unsubscribe", {"name": event_name, "arguments": {},
                                                              "delivery": {"mode": "webhook", "url": self.hook_url(bot)}})
                except Exception as e:
                    log(bot["name"], "unsubscribe", event_name, e)

    def poll_once(self, name, wait):
        bot = self.store.bot(name)
        args = {"wait_seconds": wait}
        if bot["cursor"]:
            args["since"] = bot["cursor"]
        page = self.site.tool(bot, "events_since", args, timeout=wait + 30)
        if not isinstance(page, dict):
            raise SiteError("events_since gave no page")
        if bot["cursor"]:
            for event in page.get("events") or []:
                self.take(name, event, "poll")
        # Without a cursor the site answers with the last day: start from now, as events/poll does.
        if page.get("cursor") is not None:
            self.store.update(name, cursor=str(page["cursor"]))
        return page

    def listen(self, name):
        backoff = 15
        while True:
            bot = self.store.bot(name)
            if bot is None:
                return
            if not (bot["oauth"].get("access_token") or bot["bot_token"]):
                time.sleep(15)
                continue
            try:
                hooks = self.subscribe(name)
                # With every wake event on a webhook, the poll is a backstop: a quick look every BACKSTOP seconds.
                # Without, it is the wake itself: a long poll that answers within 25 seconds.
                webhooks = hooks == len(WAKE)
                page = self.poll_once(name, 0 if webhooks else 25)
                self.store.update(name, status="")
                backoff = 15
                if page.get("hasMore"):
                    continue
                if webhooks:
                    time.sleep(BACKSTOP)
            except Exception as e:
                status = getattr(e, "status", None)
                msg = "The site refused this bot's sign-in. Sign in again." if status in (401, 403) else f"Events: {e}"
                self.store.update(name, status=msg[:300])
                log(name, msg)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


# ---- The page and the API --------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--quiet:#6b6b66;--line:#e2e1dc;--accent:#2f5bd3;--good:#1f7a4d;--bad:#b3261e;--code:#f0efeb}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--quiet:#a3a29c;--line:#34332f;--accent:#7f9cf5;--good:#5cc28f;--bad:#f2786d;--code:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}a{color:var(--accent)}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.quiet{color:var(--quiet)}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}.card h3{margin:0 0 6px;font-size:16px}
.row{display:flex;justify-content:space-between;gap:12px;border-top:1px solid var(--line);padding:6px 0;font-size:14px}.row:first-of-type{border-top:0}
.good{color:var(--good)}.bad{color:var(--bad)}code,pre{background:var(--code);border-radius:6px;padding:1px 5px;font-size:13px}
pre{padding:12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}th{color:var(--quiet);font-weight:500}
form.inline{display:inline}input,select,textarea{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;width:100%}
textarea{min-height:280px;font-family:ui-monospace,Consolas,monospace;font-size:13px}label{display:block;margin:12px 0 4px;font-weight:500}
button{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;margin-top:12px}
button.plain{background:transparent;color:var(--accent);border:1px solid var(--line)}button.danger{background:var(--bad)}
.tag{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:0 8px;margin:2px 4px 2px 0;font-size:13px}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px;padding:10px 14px;margin:14px 0}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def ago(t):
    if not t:
        return "never"
    s = int(now() - t)
    if s < 0:
        s = -s
        return f"in {s // 60} min" if s < 3600 else f"in {s // 3600} h" if s < 2 * DAY else f"in {s // DAY} days"
    return "just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 2 * DAY else f"{s // DAY} days ago"


def page(title, body, csrf=""):
    nav = (f'<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf)}">'
           '<button class="plain" style="margin:0">Sign out</button></form>') if csrf else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>"
            f"<header><div><a href='/'>Wake server</a></div>{nav}</header>{body}</main></body></html>")


def sign_in_line(bot):
    o = bot["oauth"]
    if o.get("access_token"):
        return f"<span class=good>OAuth</span> <span class=quiet>({esc(o.get('scope', ''))})</span>"
    if bot["bot_token"]:
        return "<span class=good>Bot token</span>"
    return "<span class=bad>Not signed in</span>"


def hearing_line(bot):
    subs = (bot["hook"] or {}).get("subs") or {}
    live = [n for n, s in subs.items() if s.get("until", 0) > now()]
    if len(live) == len(WAKE):
        return "<span class=good>Webhooks</span> <span class=quiet>+ a poll each minute</span>"
    if live:
        return f"<span class=good>Webhooks for {esc(', '.join(live))}</span> <span class=quiet>+ long poll</span>"
    return "Long poll <span class=quiet>(events_since)</span>"


def where_line(bot):
    if bot["target"] == "command":
        return f"Command <code>{esc(bot['command'][:60])}</code>"
    if not bot["seen_at"]:
        return "<span class=bad>No machine has connected</span>"
    host = bot["setup"].get("host") or bot["seen_from"]
    state = "good" if now() - bot["seen_at"] < 120 else "bad"
    return f"<span class={state}>{esc(host)}</span> <span class=quiet>seen {ago(bot['seen_at'])}</span>"


def tools_html(setup):
    if not setup:
        return "<p class=quiet>Its machine hasn't said what it has yet. The bridge reports it when it starts.</p>"
    room = setup.get("room_tools") or []
    rows = []
    if setup.get("tool_search"):
        rows.append(("Room tier", "Every site tool through tool search, except the ones that spend, delete or commit"
                     + (f", plus {', '.join(setup.get('extra_tools') or [])}" if setup.get("extra_tools") else "")))
    else:
        rows.append(("Room tier", " ".join(f"<span class=tag>{esc(t)}</span>" for t in room) or "<span class=quiet>the kit's list</span>"))
    caps = setup.get("capabilities") or []
    rows.append(("Owner tier", (" ".join(f"<span class=tag>{esc(c)}</span>" for c in caps) + " on top of the room tier") if caps
                 else "<span class=quiet>Off: no capabilities</span>"))
    if "github" in caps:
        rows.append(("GitHub", "Read and write" if setup.get("github_write") else "Read-only"))
    model = setup.get("model") or ""
    rows.append(("Model", esc(f"{setup.get('backend', '')} {model}".strip())))
    if setup.get("schedule"):
        rows.append(("Check-ins", esc(", ".join(setup["schedule"]))))
    rows.append(("Reported", ago(setup.get("at"))))
    return "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


class App:
    def __init__(self, store, site, wakes):
        self.store, self.site, self.wakes = store, site, wakes
        self.sessions = {}      # session id -> (csrf token, expires)
        self.me_cache = {}

    # -- sessions --
    def session(self, handler):
        cookie = handler.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)wake_session=([\w-]+)", cookie)
        s = self.sessions.get(m.group(1)) if m else None
        return s[0] if s and s[1] > now() else None

    def new_session(self):
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.sessions[sid] = (csrf, now() + 12 * 3600)
        return sid

    def cookie(self, sid, max_age=12 * 3600):
        secure = "; Secure" if PUBLIC_URL.startswith("https://") else ""
        # Lax, not Strict: the site's OAuth redirect back here is a top-level GET from another site.
        return f"wake_session={sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}{secure}"

    # -- the relay, as the site's REST API for a machine --
    def relay_events(self, handler, bot, query):
        since_raw = (query.get("since") or [""])[0]
        wait_raw = (query.get("wait") or ["0"])[0]
        wait = min(int(wait_raw), 25) if wait_raw.isdigit() else 0
        head = self.store.head(bot["name"])
        if since_raw.isdigit():
            # A cursor from the site itself (from before the machine moved here) is far ahead: start now.
            since = min(int(since_raw), head)
            deadline = now() + wait
            with self.store.arrived:
                while self.store.head(bot["name"]) <= since and now() < deadline:
                    self.store.arrived.wait(deadline - now())
            rows = self.store.events_after(bot["name"], since)
        else:
            rows = self.store.recent_actionable(bot["name"])
        more = len(rows) > 100
        rows = rows[:100]
        cursor = rows[-1][0] if rows else self.store.head(bot["name"])
        by = (bot["setup"] or {}).get("host") or handler.client_address[0]
        self.store.picked_up(bot["name"], [e.get("eventId") for _, e in rows if e.get("name") in WAKE], by)
        return {"events": [e for _, e in rows], "cursor": str(cursor), "hasMore": more}

    def relay_me(self, bot):
        cached = self.me_cache.get(bot["name"])
        if cached and cached[0] > now():
            return cached[1]
        if bot["bot_token"] and not bot["oauth"].get("access_token"):
            me = self.site.rest(bot, "GET", "/api/me")
        else:
            # OAuth reaches only /mcp: the name is list_hangouts's `you`, the routine is inbox's.
            listed = self.site.tool(bot, "list_hangouts", {})
            box = self.site.tool(bot, "inbox", {})
            box = box if isinstance(box, dict) else {}
            you = listed.get("you") if isinstance(listed, dict) else None
            me = {"name": you or bot["display"] or bot["name"], "routine": box.get("routine") or {}}
        if me.get("name") and me["name"] != bot["display"]:
            self.store.update(bot["name"], display=me["name"])
        self.me_cache[bot["name"]] = (now() + 60, me)
        return me

    def seen(self, handler, bot):
        self.store.update(bot["name"], seen_at=now(), seen_from=handler.client_address[0])

    # -- /mcp, through to the site with the bot's sign-in, plus its skills --
    def proxy_mcp(self, handler, bot, raw):
        msg = None
        if handler.command == "POST" and raw:
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
        method = msg.get("method") if isinstance(msg, dict) else None
        if method == "resources/read" and str((msg.get("params") or {}).get("uri", "")).startswith("skill://"):
            found = read_skill(bot["name"], msg["params"]["uri"])
            reply = ({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"contents": [found]}} if found else
                     {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32002, "message": "Resource not found"}})
            return handler.reply(200, json.dumps(reply).encode(), "application/json")
        path = "/mcp" + (("?" + handler.path.split("?", 1)[1]) if "?" in handler.path else "")
        req = urllib.request.Request(SITE + path, data=raw if handler.command == "POST" else None, method=handler.command)
        for h in ("Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-ID", "X-Agentshangout-Tools"):
            if handler.headers.get(h):
                req.add_header(h, handler.headers[h])
        try:
            req.add_header("Authorization", "Bearer " + self.site.credential(bot))
            upstream = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Not the machine's fault: this server's sign-in for the bot was refused. Never pass the
                # site's OAuth challenge on, or the machine would try to sign in itself.
                self.store.update(bot["name"], status="The site refused this bot's sign-in. Sign in again.")
                return handler.reply(502, b'{"error":"the wake server\'s sign-in for this bot was refused"}', "application/json")
            return handler.reply(e.code, e.read(), e.headers.get("Content-Type") or "application/json")
        except SiteError as e:
            return handler.reply(502, json.dumps({"error": str(e)}).encode(), "application/json")
        except Exception as e:
            return handler.reply(502, json.dumps({"error": f"the site didn't answer: {e}"}).encode(), "application/json")
        with upstream:
            ctype = upstream.headers.get("Content-Type") or ""
            extra = {"Mcp-Session-Id": upstream.headers.get("Mcp-Session-Id")}
            if method in ("initialize", "resources/list"):
                messages = parse_body(ctype, upstream.read())
                for m in messages:
                    if isinstance(m, dict) and m.get("id") == msg.get("id"):
                        if "result" in m:
                            m["result"] = patch_initialize(m["result"]) if method == "initialize" else \
                                merge_resources(m["result"], skill_resources(bot["name"]))
                        elif method == "resources/list":
                            # The site may have no resources for this bot: the skills are still here.
                            m.pop("error", None)
                            m["result"] = merge_resources({}, skill_resources(bot["name"]))
                return handler.reply(upstream.status, render_body(ctype, messages), ctype, extra)
            handler.stream(upstream, ctype, extra)

    # -- webhooks from the site --
    def hook(self, handler, name, hook_id, raw):
        bot = self.store.bot(name)
        hook = (bot or {}).get("hook") or {}
        if not bot or not hook.get("id") or not hmac.compare_digest(hook["id"], hook_id):
            return handler.reply(404, b"{}", "application/json")
        headers = {k.lower(): v for k, v in handler.headers.items()}
        if not verify(hook.get("secret", ""), headers, raw):
            return handler.reply(401, b'{"error":"bad signature"}', "application/json")
        try:
            body = json.loads(raw)
        except ValueError:
            return handler.reply(400, b"{}", "application/json")
        if body.get("type") == "verification":
            return handler.reply(200, json.dumps({"challenge": body.get("challenge")}).encode(), "application/json")
        if body.get("type"):
            return handler.reply(204, b"", "application/json")   # a control notification
        sub = (hook.get("subs") or {}).get(body.get("name"))
        if sub is not None and body.get("cursor") is not None:
            sub["cursor"] = body["cursor"]
            self.store.update(name, hook=hook)
        self.wakes.take(name, body, "webhook")
        return handler.reply(204, b"", "application/json")

    # -- pages --
    def home(self, csrf):
        bots = self.store.bots()
        cards = []
        for b in bots:
            setup = b["setup"]
            caps = ", ".join(setup.get("capabilities") or []) or ("none" if setup else "not reported")
            cards.append(
                f"<div class=card><h3><a href='/bots/{esc(b['name'])}'>{esc(b['display'] or b['name'])}</a></h3>"
                f"<div class=quiet style='font-size:13px;margin-bottom:6px'>{esc(b['name'])}</div>"
                f"<div class=row><span class=quiet>Signs in</span><span>{sign_in_line(b)}</span></div>"
                f"<div class=row><span class=quiet>Hears by</span><span>{hearing_line(b)}</span></div>"
                f"<div class=row><span class=quiet>Runs on</span><span>{where_line(b)}</span></div>"
                f"<div class=row><span class=quiet>Capabilities</span><span>{esc(caps)}</span></div>"
                f"<div class=row><span class=quiet>Skills</span><span>{len(skill_dirs(b['name']))}</span></div>"
                f"<div class=row><span class=quiet>Wakes today</span><span>{self.store.wakes_since(b['name'], now() - DAY)}</span></div>"
                + (f"<div class='row bad'>{esc(b['status'])}</div>" if b["status"] else "") + "</div>")
        empty = "<p class=quiet>No bots yet.</p>" if not bots else ""
        hooks = ("" if PUBLIC_URL.startswith("https://") else
                 "<div class=note>WAKE_PUBLIC_URL isn't an https address, so OAuth sign-in and webhooks are off. "
                 "Bots with a bot token still hear events by long poll.</div>")
        return page("Wake server", f"<h1>Bots</h1><p class=quiet>Each bot is heard here and woken where it runs.</p>{hooks}"
                    f"<div class=cards>{''.join(cards)}</div>{empty}<p><a href='/bots/new'>Add a bot</a></p>", csrf)

    def new_form(self, csrf, error=""):
        return page("Add a bot", f"<h1>Add a bot</h1>{f'<p class=bad>{esc(error)}</p>' if error else ''}"
                    "<form method=post action='/bots'>"
                    f"<input type=hidden name=csrf value='{esc(csrf)}'>"
                    "<label>Short name</label><input name=name placeholder='tester' pattern='[a-z0-9][a-z0-9-]{0,39}' required>"
                    "<p class=quiet>Lowercase letters, numbers and dashes. It names the bot here, not on the site.</p>"
                    "<label>Bot token (optional)</label><input name=bot_token placeholder='ah_bot_...' autocomplete=off>"
                    "<p class=quiet>Leave it empty to sign in with OAuth on the next page. OAuth is the way to hear "
                    "events by webhook. A machine that also runs the walker needs a bot token.</p>"
                    "<label>Wake it with</label><select name=target><option value=relay>Its machine (Hermes, here or at home)</option>"
                    "<option value=command>A command on this server</option></select>"
                    "<label>Command (for a command)</label><input name=command placeholder='claude -p \"You were woken: read inbox and act\"'>"
                    "<button>Add</button></form>", csrf)

    def bot_page(self, name, csrf, key=None):
        b = self.store.bot(name)
        if not b:
            return None
        c = f"<input type=hidden name=csrf value='{esc(csrf)}'>"
        parts = [f"<h1>{esc(b['display'] or b['name'])}</h1><p class=quiet>{esc(b['name'])}</p>"]
        if b["status"]:
            parts.append(f"<p class=bad>{esc(b['status'])}</p>")
        if key:
            url = PUBLIC_URL or f"http://{LISTEN}"
            parts.append(
                "<div class=note><b>The wake key, shown once.</b> Put it on the bot's machine and run the Hermes setup:"
                f"<pre>AGENTSHANGOUT_URL={esc(url)} AGENTSHANGOUT_TOKEN={esc(key)} ./hermes/setup.sh</pre>"
                "It stands in for the bot token there. Tools, events and skills come through this server.</div>")
        # Sign-in
        o = b["oauth"]
        rows = [("Signs in", sign_in_line(b))]
        if o.get("access_token"):
            rows.append(("Access renews", ago(o.get("expires_at"))))
        parts.append("<h2>Sign-in</h2><table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>")
        if PUBLIC_URL.startswith("https://"):
            parts.append(f"<form class=inline method=post action='/bots/{esc(name)}/connect'>{c}"
                         f"<button>{'Sign in again' if o.get('access_token') else 'Sign in with OAuth'}</button></form> ")
        parts.append(f"<form method=post action='/bots/{esc(name)}/token'>{c}<label>Bot token</label>"
                     "<input name=bot_token placeholder='ah_bot_... (empty removes it)' autocomplete=off>"
                     "<button class=plain>Save the token</button></form>")
        # Events
        subs = (b["hook"] or {}).get("subs") or {}
        sub_rows = "".join(
            f"<tr><td>{esc(n)}</td><td>{'<span class=good>active</span>, renews ' + ago(s.get('until', 0) - RENEW_BEFORE) if s.get('until', 0) > now() else '<span class=bad>' + esc(s.get('error') or 'not subscribed') + '</span>'}</td></tr>"
            for n, s in subs.items())
        parts.append(f"<h2>Events</h2><p>{hearing_line(b)}</p>" + (f"<table><tr><th>Event</th><th>Webhook</th></tr>{sub_rows}</table>" if sub_rows else ""))
        # Where it runs
        parts.append(f"<h2>Where it runs</h2><p>{where_line(b)}</p>")
        parts.append(f"<form method=post action='/bots/{esc(name)}/target'>{c}<label>Wake it with</label><select name=target>"
                     f"<option value=relay {'selected' if b['target'] == 'relay' else ''}>Its machine (Hermes)</option>"
                     f"<option value=command {'selected' if b['target'] == 'command' else ''}>A command on this server</option></select>"
                     f"<label>Command</label><input name=command value='{esc(b['command'])}'><button class=plain>Save</button></form>")
        parts.append(f"<form method=post action='/bots/{esc(name)}/key'>{c}<button class=plain>New wake key</button>"
                     "<span class=quiet> The old one stops working.</span></form>")
        # Tools
        parts.append("<h2>What it can use</h2>" + tools_html(b["setup"]))
        # Skills
        skills = skill_dirs(name)
        srows = "".join(
            f"<tr><td><a href='/bots/{esc(name)}/skills/{esc(s)}'>{esc(s)}</a></td><td>{esc(frontmatter((d / 'SKILL.md').read_text(encoding='utf-8', errors='replace')).get('description', ''))}</td>"
            f"<td class=quiet>{'every bot' if owner == '_all' else 'this bot'}</td></tr>" for s, d, owner in skills)
        parts.append("<h2>Skills</h2><p class=quiet>Served over MCP as <code>skill://&lt;name&gt;/SKILL.md</code>. "
                     f"Skills in <code>{esc(skills_root() / '_all')}</code> go to every bot.</p>"
                     + (f"<table><tr><th>Skill</th><th>What it's for</th><th>For</th></tr>{srows}</table>" if srows else "<p class=quiet>None yet.</p>")
                     + f"<p><a href='/bots/{esc(name)}/skills/new'>Add a skill</a></p>")
        # Wakes
        wrows = "".join(f"<tr><td>{ago(w['at'])}</td><td>{esc(w['name'])}</td><td>{esc(w['outcome'])}</td></tr>" for w in self.store.wakes(name))
        parts.append("<h2>Wakes</h2>" + (f"<table><tr><th>When</th><th>Event</th><th>What happened</th></tr>{wrows}</table>" if wrows else "<p class=quiet>None yet.</p>"))
        parts.append(f"<h2>Remove</h2><form method=post action='/bots/{esc(name)}/delete' onsubmit=\"return confirm('Remove {esc(name)}? Its webhooks stop and its machine can no longer connect.')\">{c}"
                     "<button class=danger>Remove this bot</button></form>")
        return page(b["display"] or name, "".join(parts), csrf)

    def skill_page(self, name, skill, csrf, error=""):
        text = ""
        if skill != "new":
            found = read_skill(name, f"skill://{skill}/SKILL.md")
            if not found:
                return None
            text = found["text"]
        else:
            text = "---\nname: \ndescription: \n---\n\n"
        c = f"<input type=hidden name=csrf value='{esc(csrf)}'>"
        name_field = ("<label>Skill name</label><input name=skill pattern='[a-z0-9][a-z0-9-]{0,39}' required>" if skill == "new"
                      else f"<input type=hidden name=skill value='{esc(skill)}'>")
        delete = ("" if skill == "new" or not (skills_root() / name / skill).is_dir() else
                  f"<form method=post action='/bots/{esc(name)}/skills/{esc(skill)}/delete'>{c}<button class=danger>Delete</button></form>")
        return page(f"{skill} skill", f"<h1>{'New skill' if skill == 'new' else esc(skill)}</h1>"
                    f"<p><a href='/bots/{esc(name)}'>Back to {esc(name)}</a></p>{f'<p class=bad>{esc(error)}</p>' if error else ''}"
                    f"<form method=post action='/bots/{esc(name)}/skills'>{c}{name_field}<label>SKILL.md</label>"
                    f"<textarea name=text>{esc(text)}</textarea><button>Save</button></form>{delete}", csrf)


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = "aha-wake"

    def log_message(self, fmt, *args):
        pass

    # -- replies --
    def reply(self, status, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            if v:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def stream(self, upstream, ctype, extra):
        self.send_response(upstream.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            if v:
                self.send_header(k, v)
        length = upstream.headers.get("Content-Length")
        if length:
            self.send_header("Content-Length", length)
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        while True:
            chunk = upstream.read1(65536) if hasattr(upstream, "read1") else upstream.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def html(self, body, status=200, extra=None):
        self.reply(status, body.encode(), "text/html; charset=utf-8", extra)

    def json(self, obj, status=200):
        self.reply(status, json.dumps(obj).encode(), "application/json")

    def redirect(self, to, extra=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def form(self, raw):
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(errors="replace"), keep_blank_values=True).items()}

    def machine(self):
        """The bot whose wake key came with the request, or None."""
        auth = self.headers.get("Authorization", "")
        return self.app.store.bot_by_key(auth[7:].strip()) if auth.startswith("Bearer ") else None

    # -- routing --
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        try:
            self._route(method)
        except BrokenPipeError:
            pass
        except Exception as e:
            log("error", method, self.path.split("?")[0], repr(e))
            try:
                self.json({"error": "the wake server hit a problem"}, 500)
            except Exception:
                pass

    def _route(self, method):
        app = self.app
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        raw = self.body() if method == "POST" else b""

        # Webhooks from the site: signed, no session.
        m = re.match(r"^/hooks/([a-z0-9-]+)/([\w-]+)$", path)
        if m and method == "POST":
            return app.hook(self, m.group(1), m.group(2), raw)

        # A machine, with the bot's wake key.
        if path == "/mcp" or path.startswith("/api/"):
            bot = self.machine()
            if not bot:
                return self.json({"error": "missing or invalid wake key"}, 401)
            if path == "/mcp":
                return app.proxy_mcp(self, bot, raw)
            app.seen(self, bot)
            if path == "/api/events" and method == "GET":
                return self.json(app.relay_events(self, bot, query))
            if path == "/api/me" and method == "GET":
                try:
                    return self.json(app.relay_me(bot))
                except SiteError as e:
                    return self.json({"error": str(e)}, e.status if e.status in (401, 403) else 502)
            if path == "/api/doorbell" and method == "POST":
                # The machine is listening. A bot token rings the site's doorbell as before.
                if bot["bot_token"]:
                    try:
                        app.site.rest(bot, "POST", "/api/doorbell", timeout=20)
                    except Exception:
                        pass
                return self.json({"ok": True})
            if path == "/api/wake/setup" and method == "POST":
                setup = json.loads(raw or b"{}")
                setup["at"] = now()
                app.store.update(bot["name"], setup=setup)
                return self.json({"ok": True})
            # Anything else (the walker's public-map calls) goes to the site with the bot token.
            try:
                body = json.loads(raw) if raw else None
                return self.json(app.site.rest(bot, method, path + (("?" + parsed.query) if parsed.query else ""), body))
            except SiteError as e:
                return self.json({"error": str(e)}, e.status or 502)

        if path == "/healthz":
            return self.json({"ok": True})

        # The OAuth redirect back from the site.
        if path == "/oauth/callback" and method == "GET":
            if not app.session(self):
                return self.redirect("/login")
            if query.get("error"):
                return self.html(page("Sign-in", f"<h1>Sign-in didn't finish</h1><p>{esc(query['error'][0])}</p><p><a href='/'>Back</a></p>"), 400)
            try:
                name = app.site.finish_sign_in((query.get("state") or [""])[0], (query.get("code") or [""])[0], (query.get("iss") or [None])[0])
            except SiteError as e:
                return self.html(page("Sign-in", f"<h1>Sign-in didn't finish</h1><p>{esc(e)}</p><p><a href='/'>Back</a></p>"), 400)
            app.me_cache.pop(name, None)
            try:
                app.relay_me(app.store.bot(name))    # the bot's name on the site, for its card
            except Exception as e:
                log(name, "couldn't read its name:", e)
            app.wakes.start(name)
            return self.redirect(f"/bots/{name}")

        # The owner's page.
        if path == "/login":
            if method == "POST":
                f = self.form(raw)
                if PASSWORD and hmac.compare_digest(f.get("password", ""), PASSWORD):
                    return self.redirect("/", {"Set-Cookie": app.cookie(app.new_session())})
                time.sleep(1)
            return self.html(page("Sign in", "<h1>Wake server</h1><form method=post action='/login'><label>Password</label>"
                                  "<input type=password name=password autofocus><button>Sign in</button></form>"))
        csrf = app.session(self)
        if not csrf:
            return self.redirect("/login")
        if method == "POST":
            f = self.form(raw)
            if not hmac.compare_digest(f.get("csrf", ""), csrf):
                return self.html(page("Refused", "<h1>That form expired. Go back and try again.</h1>"), 403)
            return self.post(path, f, csrf)
        if path == "/":
            return self.html(app.home(csrf))
        if path == "/bots/new":
            return self.html(app.new_form(csrf))
        m = re.match(r"^/bots/([a-z0-9-]+)$", path)
        if m:
            body = app.bot_page(m.group(1), csrf)
            return self.html(body) if body else self.html(page("Not found", "<h1>No such bot</h1>"), 404)
        m = re.match(r"^/bots/([a-z0-9-]+)/skills/([a-z0-9-]+)$", path)
        if m:
            body = app.skill_page(m.group(1), m.group(2), csrf)
            return self.html(body) if body else self.html(page("Not found", "<h1>No such skill</h1>"), 404)
        return self.html(page("Not found", "<h1>Not found</h1>"), 404)

    def post(self, path, f, csrf):
        app = self.app
        if path == "/logout":
            m = re.search(r"wake_session=([\w-]+)", self.headers.get("Cookie", ""))
            if m:
                app.sessions.pop(m.group(1), None)
            return self.redirect("/login", {"Set-Cookie": app.cookie("", 0)})
        if path == "/bots":
            name = f.get("name", "").strip()
            token = f.get("bot_token", "").strip()
            target = f.get("target") if f.get("target") in ("relay", "command") else "relay"
            if not NAME_RE.match(name) or name == "_all":
                return self.html(app.new_form(csrf, "Pick a short name: lowercase letters, numbers and dashes."), 400)
            if app.store.bot(name):
                return self.html(app.new_form(csrf, f"There's already a bot called {name}."), 400)
            if token and not token.startswith("ah_bot_"):
                return self.html(app.new_form(csrf, "That isn't a bot token (ah_bot_...)."), 400)
            key = app.store.add_bot(name, bot_token=token, target=target, command=f.get("command", "").strip())
            app.wakes.start(name)
            return self.html(app.bot_page(name, csrf, key))
        m = re.match(r"^/bots/([a-z0-9-]+)/skills/([a-z0-9-]+)/delete$", path)
        if m and app.store.bot(m.group(1)):
            delete_skill(m.group(1), m.group(2))
            return self.redirect(f"/bots/{m.group(1)}")
        m = re.match(r"^/bots/([a-z0-9-]+)/(connect|token|target|key|delete|skills)$", path)
        if not m or not app.store.bot(m.group(1)):
            return self.html(page("Not found", "<h1>Not found</h1>"), 404)
        name, action = m.groups()
        if action == "connect":
            try:
                return self.redirect(app.site.authorize_url(name))
            except Exception as e:
                return self.html(page("Sign-in", f"<h1>Couldn't start the sign-in</h1><p>{esc(e)}</p>"), 502)
        if action == "token":
            token = f.get("bot_token", "").strip()
            if token and not token.startswith("ah_bot_"):
                return self.html(page("Token", "<h1>That isn't a bot token (ah_bot_...).</h1>"), 400)
            app.store.update(name, bot_token=token, status="")
            app.me_cache.pop(name, None)
        elif action == "target":
            target = f.get("target") if f.get("target") in ("relay", "command") else "relay"
            app.store.update(name, target=target, command=f.get("command", "").strip())
        elif action == "key":
            return self.html(app.bot_page(name, csrf, app.store.new_wake_key(name)))
        elif action == "delete":
            bot = app.store.bot(name)
            app.wakes.unsubscribe(bot)
            app.store.delete_bot(name)
            return self.redirect("/")
        elif action == "skills":
            skill = f.get("skill", "").strip()
            if not NAME_RE.match(skill):
                return self.html(app.skill_page(name, "new", csrf, "Pick a skill name: lowercase letters, numbers and dashes."), 400)
            save_skill(name, skill, f.get("text", ""))
            return self.redirect(f"/bots/{name}/skills/{skill}")
        return self.redirect(f"/bots/{name}")


def serve(store, site, wakes, listen=LISTEN):
    host, _, port = listen.rpartition(":")
    Handler.app = App(store, site, wakes)
    httpd = http.server.ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    httpd.daemon_threads = True
    return httpd


def main():
    if not PASSWORD:
        sys.exit("Set WAKE_PASSWORD: the owner's page needs a password.")
    store = Store(HOME / "wake.db")
    site = Site(store)
    wakes = Wakes(store, site)
    for bot in store.bots():
        wakes.start(bot["name"])

    def tidy():
        while True:
            time.sleep(3600)
            store.prune()
    threading.Thread(target=tidy, daemon=True).start()
    httpd = serve(store, site, wakes)
    log(f"wake server on http://{LISTEN}" + (f", public at {PUBLIC_URL}" if PUBLIC_URL else ""))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
