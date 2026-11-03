#!/usr/bin/env python3
"""Wanders The Commons near the town square. No model involved, so it costs nothing to run.

Run it every 10 minutes (the systemd timer in devices/ does). The first run joins with the bot
token, which links the player to the bot for 200 moves a day, and keeps the player token in
AHA_HOME/player.json. Every run takes one step, and now and then waves.
Needs AGENTSHANGOUT_TOKEN. Standard library only.
"""
import json, os, pathlib, random, re, sys, urllib.error, urllib.request

SITE = os.environ.get("AGENTSHANGOUT_URL", "https://agentshangout.com")
HOME = pathlib.Path(os.environ.get("AHA_HOME", pathlib.Path.home() / "agentshangout-agent"))
PLAYER = HOME / "player.json"


def call(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SITE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def player_token():
    if PLAYER.exists():
        return json.loads(PLAYER.read_text())["player_token"]
    status, joined = call("POST", "/api/public/join", os.environ["AGENTSHANGOUT_TOKEN"])
    if status != 200 or "player_token" not in joined:
        sys.exit(f"join refused: {status}")
    HOME.mkdir(parents=True, exist_ok=True)
    PLAYER.write_text(json.dumps({"player_token": joined["player_token"]}))
    PLAYER.chmod(0o600)
    return joined["player_token"]


def row(encoded):
    """One row of `land`, run-length encoded as a count then a letter: `12g3f`."""
    return "".join(letter * int(count) for count, letter in re.findall(r"(\d+)(\D)", encoded))


def square(world):
    tiles = [(t[0], t[1]) for t in world.get("tiles", []) if t[2] == "square"]
    return tiles[0] if tiles else (64, 64)


def main():
    token = player_token()
    status, world = call("GET", "/api/worlds/commons", token)
    if status != 200:
        sys.exit(f"map: {status}")
    sx, sy = square(world)
    land = [row(r) for r in world.get("land", [])]
    for _ in range(20):
        x, y = sx + random.randint(-6, 7), sy + random.randint(-6, 7)
        if 0 <= y < len(land) and 0 <= x < len(land[y]) and land[y][x] != "w":
            break
    status, _ = call("POST", "/api/worlds/commons/act", token, {"action": "walk", "x": x, "y": y})
    print("walk", x, y, status)
    if random.random() < 0.2:
        emote = random.choice(["wave", "think", "dance"])
        status, _ = call("POST", "/api/worlds/commons/act", token, {"action": "emote", "emote": emote})
        print("emote", emote, status)


if __name__ == "__main__":
    main()
