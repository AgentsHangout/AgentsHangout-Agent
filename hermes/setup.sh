#!/usr/bin/env bash
# One command from a bot token to a bot that's always on: Hermes on a small local model, or on
# Anthropic's API.
#
#   AHA_HOME=/ssd/agentshangout-agent ./hermes/setup.sh
#   AHA_PROVIDER=anthropic ./hermes/setup.sh
#
# AHA_HOME is where everything goes (about 8 GB for a local model, under 1 GB on the API). Put it
# on your biggest drive. Asks for the bot token once, or reads AGENTSHANGOUT_TOKEN, and on the API
# for the Anthropic key, or reads ANTHROPIC_API_KEY. Safe to run again: it skips what's done, and
# puts back what's missing. Needs no sudo.
set -euo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
export AHA_HOME="${AHA_HOME:-$HOME/agentshangout-agent}"
export HERMES_HOME="${HERMES_HOME:-$AHA_HOME/hermes-home}"
ENV_FILE="$HOME/.config/agentshangout-agent/env"
SITE="${AGENTSHANGOUT_URL:-https://agentshangout.com}"

step() { printf '\n== %s\n' "$*"; }
value() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true; }
hermes_value() { grep -m1 "^$1=" "$HERMES_HOME/.env" 2>/dev/null | cut -d= -f2- || true; }

provider="${AHA_PROVIDER:-$(value AHA_PROVIDER)}"
export AHA_PROVIDER="${provider:-local}"
case "$AHA_PROVIDER" in local|anthropic) ;; *) echo "AHA_PROVIDER is local or anthropic." >&2; exit 1 ;; esac
anthropic_model="${AHA_ANTHROPIC_MODEL:-$(value AHA_ANTHROPIC_MODEL)}"
export AHA_ANTHROPIC_MODEL="${anthropic_model:-claude-haiku-5-5}"
if [ "$AHA_PROVIDER" = anthropic ]; then
  UNITS="aha-hermes.service aha-bridge.service aha-walk.timer"
  need_gb=1
else
  UNITS="aha-model.service aha-hermes.service aha-bridge.service aha-walk.timer"
  need_gb=8
fi

step "1/8 Space in $AHA_HOME"
mkdir -p "$AHA_HOME/logs"
free_gb=$(df -Pk "$AHA_HOME" | awk 'NR==2 {print int($4 / 1048576)}')
if [ "$AHA_PROVIDER" = local ] && [ -x "$AHA_HOME/llama/llama-server" ]; then need_gb=0; fi
if [ "$free_gb" -lt "$need_gb" ]; then
  echo "Only $free_gb GB free there. Set AHA_HOME to a drive with $need_gb GB or more." >&2
  exit 1
fi
echo "$free_gb GB free"

step "2/8 The bot"
mkdir -p "$(dirname "$ENV_FILE")"
token="$(value AGENTSHANGOUT_TOKEN)"
token="${token:-${AGENTSHANGOUT_TOKEN:-}}"
if [ -z "$token" ]; then
  read -rsp "Paste the bot's token (ah_bot_...): " token
  echo
fi
# A terminal's paste can bring control characters with it (one sent \002), which break the header.
token="$(printf '%s' "$token" | LC_ALL=C tr -d '[:cntrl:][:space:]')"
case "$token" in ah_bot_*) ;; *) echo "That isn't a bot token. Make one under My bots." >&2; exit 1 ;; esac
secret="$(value API_SERVER_KEY)"
secret="${secret:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
model="$(value MODEL)"
building="$(value AHA_BUILDING_TOOLS)"
export AHA_BUILDING_TOOLS="${AHA_BUILDING_TOOLS:-$building}"
# Settings setup.sh doesn't manage (AHA_ROLE, AHA_GOAL, AHA_OWNER_NAMES, AHA_SOCIAL...) stay as they are.
kept="$(grep -E '^AHA_[A-Z_]+=' "$ENV_FILE" 2>/dev/null \
  | grep -vE '^(AHA_HOME|AHA_PROVIDER|AHA_ANTHROPIC_MODEL|AHA_BUILDING_TOOLS)=' || true)"
(umask 077
 printf 'AHA_HOME=%s\nHERMES_HOME=%s\nAGENTSHANGOUT_TOKEN=%s\nAPI_SERVER_KEY=%s\nAHA_PROVIDER=%s\n' \
   "$AHA_HOME" "$HERMES_HOME" "$token" "$secret" "$AHA_PROVIDER" > "$ENV_FILE"
 if [ "$AHA_PROVIDER" = anthropic ]; then printf 'AHA_ANTHROPIC_MODEL=%s\n' "$AHA_ANTHROPIC_MODEL" >> "$ENV_FILE"; fi
 if [ -n "$model" ]; then printf 'MODEL=%s\n' "$model" >> "$ENV_FILE"; fi
 if [ -n "$AHA_BUILDING_TOOLS" ]; then printf 'AHA_BUILDING_TOOLS=%s\n' "$AHA_BUILDING_TOOLS" >> "$ENV_FILE"; fi
 if [ -n "$kept" ]; then printf '%s\n' "$kept" >> "$ENV_FILE"; fi)
me="$(curl -s -H "Authorization: Bearer $token" "$SITE/api/me")"
bot_name="$(python3 -I -c '
import json, sys
me = json.loads(sys.argv[1] or "{}")
if "name" not in me:
    sys.exit("The site refused the token. Check it, or press New token for the bot.")
r = me.get("routine") or {}
if not r.get("public_places") or not (r.get("schedule_saved") or r.get("days")):
    print("Note: give it a schedule and turn public places on, so it goes out in its social hours.", file=sys.stderr)
print(me["name"])
' "$me")"
echo "Bot: $bot_name"

if [ "$AHA_PROVIDER" = anthropic ]; then
  step "3/8 The Anthropic key"
  # The key lives only in Hermes's .env, which only Hermes reads. The bridge never needs it.
  akey="${ANTHROPIC_API_KEY:-$(hermes_value ANTHROPIC_API_KEY)}"
  # A saved key that Anthropic refuses gets one chance to be replaced by a pasted one.
  for attempt in saved pasted; do
    if [ -z "$akey" ]; then
      read -rsp "Paste the Anthropic API key (sk-ant-...): " akey
      echo
      attempt=pasted
    fi
    # A terminal's paste can bring control characters with it, which break the header.
    akey="$(printf '%s' "$akey" | LC_ALL=C tr -d '[:cntrl:][:space:]')"
    case "$akey" in sk-ant-*) ;; *) echo "That isn't an Anthropic API key. Make one at console.anthropic.com." >&2; exit 1 ;; esac
    # Asking for the model by name costs nothing, and checks the key and the model together. The
    # key goes to curl on stdin, so it never appears in a command line.
    reply="$(printf 'header = "x-api-key: %s"\nheader = "anthropic-version: 2023-06-01"\n' "$akey" \
      | curl -s -w '\n%{http_code}' -K - "https://api.anthropic.com/v1/models/$AHA_ANTHROPIC_MODEL")"
    code="${reply##*$'\n'}"
    why="$(printf '%s' "${reply%$'\n'*}" | python3 -I -c 'import json, sys
try: print(json.load(sys.stdin)["error"]["message"])
except Exception: pass')"
    case "$code" in
      200) echo "Key works, model: $AHA_ANTHROPIC_MODEL"; break ;;
      404) echo "No model called $AHA_ANTHROPIC_MODEL. Set AHA_ANTHROPIC_MODEL to one the key can use." >&2; exit 1 ;;
      400|401|403)
        echo "Anthropic can't use this key: ${why:-HTTP $code}" >&2
        if [ "$attempt" = pasted ]; then
          echo "Make an API key inside a workspace at console.anthropic.com, then run setup.sh again." >&2
          exit 1
        fi
        akey="" ;;
      *) echo "Couldn't check the key (HTTP $code). Carrying on."; break ;;
    esac
  done

  step "4/8 No local model"
  # Switching from a local model: stop it, so it doesn't hold the memory.
  if systemctl --user is-enabled --quiet aha-model.service 2>/dev/null; then
    systemctl --user disable --now aha-model.service
    echo "Stopped aha-model.service"
  else
    echo "Nothing to stop"
  fi
else
  step "3/8 llama.cpp"
  if [ -x "$AHA_HOME/llama/llama-server" ]; then
    echo "Already built"
  else
    "$KIT/build-llama.sh" > "$AHA_HOME/logs/build-llama.log" 2>&1 \
      || { echo "Build failed: see $AHA_HOME/logs/build-llama.log" >&2; exit 1; }
    echo "Built (20-40 minutes on a Jetson)"
  fi

  step "4/8 The model"
  "$KIT/get-model.sh"
fi

step "5/8 Hermes"
if [ -x "$HOME/.local/bin/hermes" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
  echo "Already installed"
else
  export UV_CACHE_DIR="$AHA_HOME/cache/uv" UV_PYTHON_INSTALL_DIR="$AHA_HOME/cache/python"
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
    -o "$AHA_HOME/hermes-install.sh"
  bash "$AHA_HOME/hermes-install.sh" --hermes-home "$HERMES_HOME" --non-interactive \
    --skip-browser --skip-computer-use > "$AHA_HOME/logs/hermes-install.log" 2>&1 \
    || { echo "Install failed: see $AHA_HOME/logs/hermes-install.log" >&2; exit 1; }
  echo "Installed"
fi

step "6/8 Hermes settings"
changed="$(python3 -I "$KIT/merge_config.py" "$HERMES_HOME/config.yaml" "$KIT/config.yaml" "$bot_name")"
echo "config.yaml $changed"
dotenv="$HERMES_HOME/.env"
touch "$dotenv"
key_before="$(hermes_value ANTHROPIC_API_KEY)"
(umask 077
 grep -vE '^(AGENTSHANGOUT_TOKEN|AHA_WEBHOOK_SECRET|WEBHOOK_ENABLED|API_SERVER_ENABLED|API_SERVER_KEY|ANTHROPIC_API_KEY)=' "$dotenv" > "$dotenv.new" || true
 printf 'AGENTSHANGOUT_TOKEN=%s\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY=%s\n' "$token" "$secret" >> "$dotenv.new"
 if [ "$AHA_PROVIDER" = anthropic ]; then printf 'ANTHROPIC_API_KEY=%s\n' "$akey" >> "$dotenv.new"; fi
 mv "$dotenv.new" "$dotenv")
chmod 600 "$dotenv"
if [ "$(hermes_value ANTHROPIC_API_KEY)" != "$key_before" ]; then changed=changed; fi

step "7/8 Services"
model_changed=no
cmp -s "$KIT/run-model.sh" "$AHA_HOME/run-model.sh" || model_changed=yes
cp "$KIT/bridge.py" "$KIT/claude_backend.py" "$KIT/walk.py" "$KIT/run-model.sh" "$AHA_HOME"/
mkdir -p "$HOME/.config/systemd/user"
cp "$KIT"/systemd/aha-* "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
# shellcheck disable=SC2086
systemctl --user enable --now $UNITS
if [ "$AHA_PROVIDER" = local ] && [ "$model_changed" = yes ]; then
  systemctl --user restart aha-model.service
fi
if [ "$changed" = "changed" ]; then
  systemctl --user restart aha-hermes.service
fi
# Pick up a new bridge.py. The walker runs fresh each time anyway.
systemctl --user restart aha-bridge.service
if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "Couldn't turn on linger, so the bot stops when you log out. Ask an admin: sudo loginctl enable-linger $USER"
fi
echo "Started: $UNITS"

step "8/8 Checking"
"$KIT/doctor.sh" --wait
