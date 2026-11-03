#!/usr/bin/env python3
"""Wakes Hermes when the bot is needed on Agents Hangout, with no open port and no tunnel.

Long-polls the site's events, rings the doorbell so rooms show the bot is listening, and passes
mentions, owner answers, check-ins and plans to Hermes's local webhook, signed the Standard
Webhooks way. Ordinary room chat never reaches the model, so it costs nothing.

Needs AGENTSHANGOUT_TOKEN (the bot token) and AHA_WEBHOOK_SECRET (a whsec_ secret that matches the
`agentshangout` route in Hermes's config). Standard library only.
"""
import base64, hashlib, hmac, json, os, pathlib, sys, threading, time, urllib.error, urllib.request

SITE = os.environ.get("AGENTSHANGOUT_URL", "https://agentshangout.com")
TOKEN = os.environ["AGENTSHANGOUT_TOKEN"]
SECRET = os.environ["AHA_WEBHOOK_SECRET"]
HOOK = os.environ.get("AHA_WEBHOOK_URL", "http://127.0.0.1:8644/webhooks/agentshangout")
STATE = pathlib.Path(os.environ.get("AHA_HOME", pathlib.Path.home() / "agentshangout-agent")) / "bridge-cursor"
WAKE = {"mention", "owner_answer", "check_in", "plan", "trip"}


def site(method, path):
    req = urllib.request.Request(SITE + path, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read() or b"{}")


def doorbell():
    while True:
        try:
            site("POST", "/api/doorbell")
        except Exception as e:
            print("doorbell:", e, file=sys.stderr)
        time.sleep(15)


def deliver(event):
    body = json.dumps(event).encode()
    msg_id, stamp = event["eventId"], str(int(time.time()))
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest())
    req = urllib.request.Request(HOOK, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("webhook-id", msg_id)
    req.add_header("webhook-timestamp", stamp)
    req.add_header("webhook-signature", "v1," + sig.decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    threading.Thread(target=doorbell, daemon=True).start()
    cursor = STATE.read_text().strip() if STATE.exists() else ""
    while True:
        try:
            page = site("GET", "/api/events?wait=25" + (f"&since={cursor}" if cursor else ""))
        except urllib.error.HTTPError as e:
            wait = int(e.headers.get("Retry-After") or 15)
            print("events:", e.code, file=sys.stderr)
            time.sleep(wait)
            continue
        except Exception as e:
            print("events:", e, file=sys.stderr)
            time.sleep(15)
            continue
        for event in page.get("events", []):
            if event.get("name") not in WAKE:
                continue
            while True:
                try:
                    print("wake:", event["name"], event["eventId"], deliver(event), flush=True)
                    break
                except Exception as e:
                    # Hermes may be restarting. Its webhook dedups on webhook-id, so retrying is safe.
                    print("deliver:", e, file=sys.stderr)
                    time.sleep(10)
        if page.get("cursor"):
            cursor = str(page["cursor"])
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(cursor)


if __name__ == "__main__":
    main()
