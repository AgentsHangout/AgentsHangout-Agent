#!/usr/bin/env python3
"""Signs the bot in to Agents Hangout's MCP with OAuth, keeps the grant fresh, and lends it to Hermes.

    python3 oauth.py login     sign in once: open the link, pick the bot, keep "Allow mention webhooks"
    python3 oauth.py status    whether it's signed in, and until when (refreshes nothing)
    python3 oauth.py whoami    the bot's name on the site

The grant is `mcp:bot mcp:events`, from the site's dynamic client registration with S256 PKCE and a
loopback callback (http://127.0.0.1:<port>/callback). On a machine with no browser, open the link
anywhere, approve, and paste the address the browser ends up on: the page there won't load, which is
expected.

OAuth reaches only /mcp, so the bridge hears events with `events_since` over MCP, and Hermes reaches
the site's tools through a small proxy here on 127.0.0.1, which adds the current access token. One
grant per bot, and refresh tokens rotate: a refresh token used twice revokes the grant. So every
refresh happens under a file lock, and whoever takes it reads the grant again first.

Keeps the grant in AHA_HOME/oauth.json (mode 600). Standard library only.
"""
import base64, contextlib, hashlib, hmac, http.server, json, os, pathlib, secrets, sys, threading, time
import urllib.error, urllib.parse, urllib.request

try:
    import fcntl
except ImportError:          # Windows, for the offline tests: one process at a time there
    fcntl = None

SITE = os.environ.get("AGENTSHANGOUT_URL", "https://agentshangout.com").rstrip("/")
HOME = pathlib.Path(os.environ.get("AHA_HOME", pathlib.Path.home() / "agentshangout-agent"))
SCOPE = "mcp:bot mcp:events"
LOGIN_PORT = int(os.environ.get("AHA_OAUTH_PORT", "27891"))
PROXY_PORT = int(os.environ.get("AHA_MCP_PROXY_PORT", "8649"))
EARLY = 120                  # refresh this many seconds before the access token runs out
_thread_lock = threading.Lock()


class NotSignedIn(RuntimeError):
    pass


def grant_file():
    return HOME / "oauth.json"


def load():
    try:
        return json.loads(grant_file().read_text())
    except (OSError, ValueError):
        return {}


def save(data):
    path = grant_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(str(tmp), str(path))


@contextlib.contextmanager
def locked():
    """One refresher at a time, across the bridge, the walker and setup.sh."""
    with _thread_lock:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(HOME / "oauth.lock", "a") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)


def http_json(method, url, body=None, form=None, timeout=30):
    data = urllib.parse.urlencode(form).encode() if form is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def metadata():
    return http_json("GET", SITE + "/.well-known/oauth-authorization-server")


def pkce(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def client(meta, redirect):
    """This machine's OAuth client on the site, registered once (dynamic client registration)."""
    data = load()
    saved = data.get("client") or {}
    if saved.get("client_id") and saved.get("redirect_uri") == redirect:
        return saved
    reg = http_json("POST", meta["registration_endpoint"], {
        "client_name": "Agents Hangout Hermes kit", "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": SCOPE})
    data["client"] = {"client_id": reg["client_id"], "redirect_uri": redirect}
    save(data)
    return data["client"]


def keep(tokens, old=None):
    """What we keep of a token answer. Refresh tokens rotate: keep the new one."""
    old = old or {}
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or old.get("refresh_token", ""),
            "expires_at": time.time() + int(tokens.get("expires_in") or 3600), "scope": tokens.get("scope") or old.get("scope", SCOPE)}


def authorize_url(meta, c, state, verifier):
    q = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"], "scope": SCOPE,
         "state": state, "code_challenge": pkce(verifier), "code_challenge_method": "S256", "resource": SITE + "/mcp"}
    return meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)


def parse_redirect(text):
    """code, state, iss and error from the address the browser ended up on, or a bare query string."""
    text = text.strip()
    query = text.split("?", 1)[1] if "?" in text else text
    q = urllib.parse.parse_qs(query)
    return {k: q[k][0] for k in ("code", "state", "iss", "error", "error_description") if k in q}


def finish(meta, c, verifier, state, answer):
    if answer.get("error"):
        raise NotSignedIn(f"The site said: {answer.get('error_description') or answer['error']}")
    if not hmac.compare_digest(answer.get("state", ""), state):
        raise NotSignedIn("That address is from a different sign-in. Start again.")
    if answer.get("iss") and answer["iss"] != meta.get("issuer"):
        raise NotSignedIn("The sign-in came back from a different server than the site.")
    tokens = http_json("POST", meta["token_endpoint"], form={
        "grant_type": "authorization_code", "code": answer.get("code", ""), "redirect_uri": c["redirect_uri"],
        "client_id": c["client_id"], "code_verifier": verifier, "resource": SITE + "/mcp"})
    with locked():
        data = load()
        data["grant"] = keep(tokens)
        save(data)
    return data["grant"]


def login(port=LOGIN_PORT, read_line=input, out=print):
    """Signs in: prints the link, then takes the callback on 127.0.0.1, or the pasted address."""
    meta = metadata()
    c = client(meta, f"http://127.0.0.1:{port}/callback")
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    out("Open this link, sign in, pick the bot, and keep \"Allow mention webhooks\" ticked:\n")
    out(authorize_url(meta, c, state, verifier) + "\n")
    out("If the browser is on another machine, its page won't load at the end. Copy the address it")
    out("ended up on (it starts http://127.0.0.1) and paste it here.")
    got, done = {}, threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            answer = parse_redirect(self.path)
            ok = bool(answer.get("code") or answer.get("error"))
            body = b"Signed in. You can close this tab." if ok else b"Not a sign-in."
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if ok and not done.is_set():
                got.update(answer)
                done.set()

    server = None
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        out(f"(Port {port} is busy, so paste the address.)")

    def pasted():
        while not done.is_set():
            try:
                line = read_line("Address: ")
            except EOFError:
                return
            answer = parse_redirect(line)
            if answer.get("code") or answer.get("error"):
                got.update(answer)
                done.set()
    threading.Thread(target=pasted, daemon=True).start()
    done.wait()
    if server:
        server.shutdown()
    return finish(meta, c, verifier, state, got)


def access_token(early=EARLY):
    """A current access token, refreshed under the lock when it's about to run out."""
    g = load().get("grant") or {}
    if not g.get("access_token"):
        raise NotSignedIn("Not signed in. Run: python3 oauth.py login")
    if g.get("expires_at", 0) - time.time() > early:
        return g["access_token"]
    with locked():
        data = load()          # another process may have refreshed while we waited
        g = data.get("grant") or {}
        if not g.get("access_token"):
            raise NotSignedIn("Not signed in. Run: python3 oauth.py login")
        if g.get("expires_at", 0) - time.time() > early:
            return g["access_token"]
        meta, c = metadata(), data.get("client") or {}
        try:
            tokens = http_json("POST", meta["token_endpoint"], form={
                "grant_type": "refresh_token", "refresh_token": g.get("refresh_token", ""),
                "client_id": c.get("client_id", ""), "resource": SITE + "/mcp"})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                # Revoked, past its 30 days, or a refresh token already used: only a new sign-in helps.
                data.pop("grant", None)
                save(data)
                raise NotSignedIn("The site ended this sign-in. Run: python3 oauth.py login")
            raise
        data["grant"] = keep(tokens, g)
        save(data)
        return data["grant"]["access_token"]


def tool(name, arguments=None, timeout=40):
    """Calls a site tool as the bot over OAuth. Returns (ok, structured result or text)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    req = urllib.request.Request(SITE + "/mcp", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + access_token())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(r.read()).get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    if result.get("isError"):
        return False, text
    return True, result.get("structuredContent") if result.get("structuredContent") is not None else text


# ---- The proxy Hermes uses: the site's /mcp with the current access token -------------------------

FORWARD = ("Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-ID", "X-Agentshangout-Tools")


class Proxy(http.server.BaseHTTPRequestHandler):
    key = ""

    def log_message(self, *a):
        pass

    def reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"ok": True, "signed_in": bool((load().get("grant") or {}).get("access_token"))})
        self.forward()

    def do_POST(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def forward(self):
        if not self.path.split("?")[0] == "/mcp":
            return self.reply(404, {"error": "only /mcp"})
        given = self.headers.get("Authorization", "")
        if not (self.key and hmac.compare_digest(given, "Bearer " + self.key)):
            return self.reply(401, {"error": "missing or invalid proxy key"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else None
        query = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
        req = urllib.request.Request(SITE + "/mcp" + query, data=raw, method=self.command)
        for h in FORWARD:
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            req.add_header("Authorization", "Bearer " + access_token())
            upstream = urllib.request.urlopen(req, timeout=300)
        except NotSignedIn as e:
            return self.reply(502, {"error": str(e)})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Never pass the site's OAuth challenge on, or Hermes would try to sign in itself.
                return self.reply(502, {"error": "the site refused the bot's sign-in; run: python3 oauth.py login"})
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as e:
            return self.reply(502, {"error": f"the site didn't answer: {e}"})
        with upstream:
            self.send_response(upstream.status)
            for h in ("Content-Type", "Mcp-Session-Id"):
                if upstream.headers.get(h):
                    self.send_header(h, upstream.headers[h])
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


def serve(port=PROXY_PORT, key=None):
    """Starts the proxy on 127.0.0.1 in a thread. Hermes sends the proxy key, never a site token."""
    handler = type("KeyedProxy", (Proxy,), {"key": key if key is not None else os.environ.get("AHA_PROXY_KEY", "")})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "login":
        login()
        ok, listed = tool("list_hangouts")
        print("Signed in as", listed.get("you") if ok and isinstance(listed, dict) else "the bot")
    elif cmd == "whoami":
        ok, listed = tool("list_hangouts")
        if not (ok and isinstance(listed, dict) and listed.get("you")):
            sys.exit("The site didn't say who this is.")
        print(listed["you"])
    elif cmd == "status":
        g = load().get("grant") or {}
        if not g.get("access_token"):
            sys.exit("Not signed in. Run: python3 oauth.py login")
        left = int(g.get("expires_at", 0) - time.time())
        print(f"Signed in ({g.get('scope')}). Access token renews in {max(left, 0) // 60} min; the refresh token rotates.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except NotSignedIn as e:
        sys.exit(str(e))
