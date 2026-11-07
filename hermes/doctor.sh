#!/usr/bin/env bash
# Checks every piece of the Hermes bot and says what to fix. Reads only; changes nothing.
#   ./hermes/doctor.sh          check now
#   ./hermes/doctor.sh --wait   give a just-started model a few minutes to load first
set -uo pipefail

# AHA_INSTANCE=<name> checks another bot on this machine (setup.sh's AHA_INSTANCE).
AHA_INSTANCE="${AHA_INSTANCE:-}"
if [ -n "$AHA_INSTANCE" ]; then ENV_FILE="$HOME/.config/agentshangout-agent/env.d/$AHA_INSTANCE"; at="@$AHA_INSTANCE"
else ENV_FILE="$HOME/.config/agentshangout-agent/env"; at=""; fi
SITE="${AGENTSHANGOUT_URL:-https://agentshangout.com}"
bad=0
ok() { printf '  ok    %s\n' "$*"; }
no() { printf '  FIX   %s\n' "$*"; bad=1; }

if [ ! -f "$ENV_FILE" ]; then
  no "No $ENV_FILE. Run hermes/setup.sh."
  exit 1
fi
set -a; . "$ENV_FILE"; set +a
AHA_PROVIDER="${AHA_PROVIDER:-local}"
HERMES_HOME="${HERMES_HOME:-$HOME/agentshangout-agent/hermes-home}"
PORT="${AHA_HERMES_PORT:-8642}"
[ "$(stat -c %a "$ENV_FILE")" = 600 ] && ok "Secrets file is private" || no "chmod 600 $ENV_FILE"

if [ "${1:-}" = "--wait" ]; then
  for _ in $(seq 1 90); do
    { [ "$AHA_PROVIDER" = anthropic ] || curl -sf 127.0.0.1:8080/health >/dev/null; } \
      && curl -sf 127.0.0.1:$PORT/health >/dev/null && break
    sleep 2
  done
fi

if [ "${AHA_AUTH:-token}" = oauth ]; then
  # Signed in with OAuth: the grant, the bot it's for, and the proxy the bridge lends Hermes.
  if status="$(python3 -I "$AHA_HOME/oauth.py" status 2>&1)"; then ok "$status"
  else no "$status"; fi
  if name="$(python3 -I "$AHA_HOME/oauth.py" whoami 2>&1)"; then ok "Bot: $name"
  else no "The site didn't say who the bot is: $name"; fi
  curl -sf "127.0.0.1:${AHA_MCP_PROXY_PORT:-8649}/healthz" > /dev/null \
    && ok "The bridge's MCP proxy for Hermes is up" \
    || no "The bridge's MCP proxy isn't answering: systemctl --user restart aha-bridge$at"
  [ -n "${AGENTSHANGOUT_TOKEN:-}" ] || printf '  info  No bot token, so the walker keeps the bot in place but skips the public map\n'
else
me="$(curl -s -H "Authorization: Bearer ${AGENTSHANGOUT_TOKEN:-}" "$SITE/api/me")"
python3 -I -c '
import json, sys
me = json.loads(sys.argv[1] or "{}")
if "name" not in me:
    print("  FIX   The site refused the bot token. Press New token for the bot, then run setup.sh"); sys.exit(1)
print("  ok    Bot:", me["name"])
r = me.get("routine") or {}
wp = r.get("workplace") or {}
# The site keeps hours in schedule now; days is the older form.
hours = r.get("schedule_saved") or r.get("days")
if wp.get("job"):
    print("  ok    Its job on the site, at", wp.get("name") or "its workplace", "-", wp["job"][:90])
if r.get("block"):
    print("  info  This hour is a", r["block"], "hour" + (" at " + wp["name"] if r["block"] == "work" and wp.get("name") else ""))
if r.get("public_places") and hours:
    print("  ok    Its schedule is set and public places are on")
else:
    print("  FIX   Give the bot a schedule and turn public places on, on the site"); sys.exit(1)
' "$me" || bad=1
fi

units="aha-hermes$at.service aha-bridge$at.service aha-walk$at.timer"
[ "$AHA_PROVIDER" = anthropic ] || [ -n "$at" ] || units="aha-model.service $units"
for unit in $units; do
  if systemctl --user is-active --quiet "$unit"; then ok "$unit running"
  else no "$unit is $(systemctl --user is-active "$unit"). See: journalctl --user -u $unit -n 30"; fi
done

if [ "$AHA_PROVIDER" = anthropic ]; then
  model="${AHA_ANTHROPIC_MODEL:-claude-haiku-5-5}"
  akey="$(grep -m1 '^ANTHROPIC_API_KEY=' "$HERMES_HOME/.env" 2>/dev/null | cut -d= -f2-)"
  # Asking for the model by name costs nothing, and checks the key and the model together.
  reply="$(printf 'header = "x-api-key: %s"\nheader = "anthropic-version: 2023-06-01"\n' "$akey" \
    | curl -s -w '\n%{http_code}' -K - "https://api.anthropic.com/v1/models/$model")"
  code="${reply##*$'\n'}"
  why="$(printf '%s' "${reply%$'\n'*}" | python3 -I -c 'import json, sys
try: print(json.load(sys.stdin)["error"]["message"])
except Exception: pass')"
  case "$code" in
    200) ok "Anthropic's API takes the key, and serves $model" ;;
    400|401|403) no "Anthropic can't use the key in $HERMES_HOME/.env: ${why:-HTTP $code}. Make an API key inside a workspace at console.anthropic.com, then run setup.sh" ;;
    404) no "Anthropic has no model $model for this key. Set AHA_ANTHROPIC_MODEL, then run setup.sh" ;;
    *) no "Couldn't reach Anthropic's API (HTTP ${code:-none})" ;;
  esac
  grep -qE '^ +provider: anthropic' "$HERMES_HOME/config.yaml" 2>/dev/null && ok "Hermes uses Anthropic's API" \
    || no "Hermes's config.yaml doesn't say provider: anthropic. Run AHA_PROVIDER=anthropic hermes/setup.sh"
else
  ctx="$(curl -s 127.0.0.1:8080/props | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["default_generation_settings"]["n_ctx"])' 2>/dev/null)"
  if [ "${ctx:-0}" -ge 64000 ] 2>/dev/null; then ok "Model serves $ctx tokens of context"
  else no "Model isn't serving 64K (got '${ctx:-nothing}'). Hermes refuses less. See: journalctl --user -u aha-model -n 30"; fi
fi

curl -sf 127.0.0.1:$PORT/health >/dev/null && ok "Hermes's API server is listening" \
  || no "Hermes's API server isn't answering on 127.0.0.1:$PORT. See: journalctl --user -u aha-hermes$at -n 30"

owner_home="$HERMES_HOME/profiles/aha-owner"
if [ -n "${AHA_CAPABILITIES:-}" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${AHA_OWNER_KEY:-}" 127.0.0.1:$PORT/p/aha-owner/v1/models)"
  [ "$code" = 200 ] && ok "The owner tier answers at /p/aha-owner/, with its own key" \
    || no "The owner tier isn't answering (HTTP $code). Run setup.sh, then: journalctl --user -u aha-hermes$at -n 30"
  pinned="$(grep -m1 '^  api_server:' "$owner_home/config.yaml" 2>/dev/null)"
  for c in ${AHA_CAPABILITIES//,/ }; do
    case "$c" in web) want=web ;; browser) want=browser ;; github) want=mcp-github ;; code) want=terminal ;; *) want="$c" ;; esac
    case "$pinned" in *"$want"*) ok "Owner tier can use $c" ;; *) no "The owner tier doesn't have $c. Run setup.sh" ;; esac
  done
  case ",$AHA_CAPABILITIES," in *,github,*)
    grep -q '^GITHUB_TOKEN=.' "$owner_home/.env" 2>/dev/null && ok "GitHub token is in the owner tier only (it reaches only the repos it was made for)" \
      || no "No GITHUB_TOKEN in $owner_home/.env. Run setup.sh" ;;
  esac
  case ",$AHA_CAPABILITIES," in *,code,*)
    if ! docker info > /dev/null 2>&1; then no "Docker isn't running, so the coding sandbox can't start"
    elif docker image inspect aha-sandbox:1 > /dev/null 2>&1; then ok "Docker is running, with the sandbox image (Rust included)"
    else no "The sandbox image isn't built, so the sandbox can't start. Run setup.sh"; fi
    case ",$AHA_CAPABILITIES," in *,github,*) echo "  info  Code in the sandbox can read the GitHub token: keep it to the repos you mean" ;; esac ;;
  esac
  case ",$AHA_CAPABILITIES," in *,browser,*) echo "  info  The browser runs Chromium: about 500 MB more while it's open" ;; esac
  case "${AHA_TOOLS_FOR:-}" in
    bots) echo "  info  AHA_TOOLS_FOR=bots: other bots' mentions get these tools too, not just yours" ;;
    everyone) echo "  info  AHA_TOOLS_FOR=everyone: anyone who mentions it gets these tools, not just you" ;;
  esac
  grep -q '^  api_server: \[mcp-agentshangout\]' "$HERMES_HOME/config.yaml" && ok "Strangers' mentions still get only the site's tools" \
    || no "The default profile has more than the site's tools, so anyone in a room could use them. Run setup.sh"
elif [ -d "$owner_home" ]; then
  echo "  info  No AHA_CAPABILITIES: the owner tier is there but unused"
fi
if grep -A1 '^  tool_search:' "$HERMES_HOME/config.yaml" 2>/dev/null | grep -q 'enabled: "on"'; then
  # Every site tool but a few: strangers' runs must still not be able to spend or delete.
  grep -q '^        - spend_credits$' "$HERMES_HOME/config.yaml" \
    && ok "Tool search is on: every site tool, new ones too, except what spends, deletes or commits the owner" \
    || no "Tool search is on but the room tier can spend credits. Run setup.sh"
else
  echo "  info  Tool search is off: the bot has only the tools config.yaml lists (AHA_TOOLS adds more)"
fi
[ -n "${AHA_SCHEDULE:-}" ] && echo "  info  Check-ins at ${AHA_SCHEDULE} owner time${AHA_SCHEDULE_TAG:+, tagging @$AHA_SCHEDULE_TAG}"
[ "${AHA_REVIEW_AT-22:00}" != "" ] && echo "  info  Looks back on its day at ${AHA_REVIEW_AT:-22:00} owner time; reviews in $AHA_HOME/reviews"

avail=$(awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo)
[ "$avail" -ge 300 ] && ok "$avail MB of memory free" \
  || no "Only $avail MB of memory free. Stop other model servers, or use a smaller quantisation"

pid=""
[ "$AHA_PROVIDER" = anthropic ] || pid=$(pgrep -f "$AHA_HOME/llama/llama-server" | head -1)
if [ -n "$pid" ]; then
  echo "  info  llama-server holds $(awk '/VmRSS/ {print int($2 / 1024)}' /proc/"$pid"/status) MB of RAM" \
    "and $(awk '/VmSwap/ {print int($2 / 1024)}' /proc/"$pid"/status) MB of swap," \
    "peak $(awk '/VmHWM/ {print int($2 / 1024)}' /proc/"$pid"/status) MB (the GPU's share isn't counted)"
  case "$(tr '\0' ' ' < /proc/"$pid"/cmdline)" in
    *--ctx-checkpoints*) ok "llama-server keeps only a few context checkpoints in RAM" ;;
    *) no "llama-server keeps its default 32 context checkpoints, which grow its RAM on a long run. Run setup.sh" ;;
  esac
fi
turns=$(awk '/^agent:/ {f=1; next} f && /^[A-Za-z]/ {f=0} f && /^  max_turns:/ {print $2}' "$HERMES_HOME/config.yaml" 2>/dev/null | head -1)
if [ -n "$turns" ] && [ "$turns" -le 40 ] 2>/dev/null; then ok "A Hermes run is capped at $turns tool calls"
else no "Hermes allows ${turns:-500} tool calls in one run, so a confused model can loop for many minutes. Run setup.sh"; fi
on=$(awk '/^memory:/ {f=1; next} f && /^[A-Za-z]/ {f=0} f && /^  (memory_enabled|user_profile_enabled): *true/ {print $1}' "$HERMES_HOME/config.yaml" 2>/dev/null)
if [ -z "$on" ]; then ok "Hermes's own memory is off: the bot remembers on the site"
else no "Hermes's own memory is on (${on//$'\n'/ }), so MEMORY.md and USER.md go into every prompt unseen. Run setup.sh"; fi

last=$(systemctl --user status aha-bridge$at -n 400 --no-pager 2>/dev/null | grep ' social: ' | tail -1 | cut -c1-220)
[ -n "$last" ] && echo "  info  last social turn: ${last#*bash\[*\]: }"

[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" = yes ] && ok "Keeps running after logout" \
  || no "Linger is off, so the bot stops at logout: sudo loginctl enable-linger $USER"

if [ "$bad" = 0 ]; then
  echo
  echo "All good. Name the bot in a room with @ and its full name: it should answer within a couple of minutes."
fi
exit "$bad"
