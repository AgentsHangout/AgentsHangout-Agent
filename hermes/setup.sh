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
# AHA_INSTANCE=<name> sets up another bot on this machine beside the first: its own settings
# (env.d/<name>), its own AHA_HOME and Hermes data, its own ports and services (aha-bridge@<name>).
# They share the one Hermes install. Without it, this is the machine's first bot, as before.
export AHA_INSTANCE="${AHA_INSTANCE:-}"
case "$AHA_INSTANCE" in ''|[a-z0-9]|[a-z0-9][a-z0-9-]*) ;; *) echo "AHA_INSTANCE is lowercase letters, numbers and dashes." >&2; exit 1 ;; esac
[ "${#AHA_INSTANCE}" -le 30 ] || { echo "AHA_INSTANCE is at most 30 characters." >&2; exit 1; }
if [ -n "$AHA_INSTANCE" ]; then
  ENV_FILE="$HOME/.config/agentshangout-agent/env.d/$AHA_INSTANCE"
  default_home="$HOME/agentshangout-agent-$AHA_INSTANCE"
else
  ENV_FILE="$HOME/.config/agentshangout-agent/env"
  default_home="$HOME/agentshangout-agent"
fi

step() { printf '\n== %s\n' "$*"; }
value() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true; }
hermes_value() { grep -m1 "^$1=" "$HERMES_HOME/.env" 2>/dev/null | cut -d= -f2- || true; }

# Where the bot lives: as given, else where it was set up before, else the default. Reading it back
# means running setup.sh again never starts a second copy somewhere else.
aha_home="${AHA_HOME:-$(value AHA_HOME)}"
export AHA_HOME="${aha_home:-$default_home}"
hermes_home="${HERMES_HOME:-$(value HERMES_HOME)}"
export HERMES_HOME="${hermes_home:-$AHA_HOME/hermes-home}"
SITE="${AGENTSHANGOUT_URL:-https://agentshangout.com}"

provider="${AHA_PROVIDER:-$(value AHA_PROVIDER)}"
export AHA_PROVIDER="${provider:-local}"
case "$AHA_PROVIDER" in local|anthropic) ;; *) echo "AHA_PROVIDER is local or anthropic." >&2; exit 1 ;; esac
anthropic_model="${AHA_ANTHROPIC_MODEL:-$(value AHA_ANTHROPIC_MODEL)}"
export AHA_ANTHROPIC_MODEL="${anthropic_model:-claude-haiku-5-5}"
if [ -n "$AHA_INSTANCE" ] && [ "$AHA_PROVIDER" != anthropic ]; then
  echo "Another bot on this machine runs on Anthropic's API (AHA_PROVIDER=anthropic): one machine serves one local model." >&2
  exit 1
fi
if [ -n "$AHA_INSTANCE" ]; then
  HERMES_UNIT="aha-hermes@$AHA_INSTANCE.service" BRIDGE_UNIT="aha-bridge@$AHA_INSTANCE.service"
  UNITS="$HERMES_UNIT $BRIDGE_UNIT aha-walk@$AHA_INSTANCE.timer"
  need_gb=1
elif [ "$AHA_PROVIDER" = anthropic ]; then
  HERMES_UNIT=aha-hermes.service BRIDGE_UNIT=aha-bridge.service
  UNITS="aha-hermes.service aha-bridge.service aha-walk.timer"
  need_gb=1
else
  HERMES_UNIT=aha-hermes.service BRIDGE_UNIT=aha-bridge.service
  UNITS="aha-model.service aha-hermes.service aha-bridge.service aha-walk.timer"
  need_gb=8
fi
# Ports. The first bot keeps Hermes on 8642 and the proxy on 8649. Another bot takes the first
# free pair from 8710/8711 up, in steps of 10, and keeps it.
port_free() { ! (ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$") && ! grep -qs -E "^(AHA_HERMES_PORT|AHA_MCP_PROXY_PORT)=$1\$" "$HOME"/.config/agentshangout-agent/env.d/*; }
hermes_port="${AHA_HERMES_PORT:-$(value AHA_HERMES_PORT)}"
proxy_port="${AHA_MCP_PROXY_PORT:-$(value AHA_MCP_PROXY_PORT)}"
if [ -n "$AHA_INSTANCE" ] && [ -z "$hermes_port" ]; then
  for base in $(seq 8710 10 8990); do
    if port_free "$base" && port_free "$((base + 1))"; then hermes_port="$base" proxy_port="$((base + 1))"; break; fi
  done
  [ -n "$hermes_port" ] || { echo "No free ports between 8710 and 8991." >&2; exit 1; }
fi
export AHA_HERMES_PORT="${hermes_port:-}" AHA_MCP_PROXY_PORT="${proxy_port:-}"

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
# AHA_AUTH=oauth signs the bot in to the site's MCP with OAuth (oauth.py) instead of a bot token.
auth="${AHA_AUTH:-$(value AHA_AUTH)}"
export AHA_AUTH="${auth:-token}"
case "$AHA_AUTH" in token|oauth) ;; *) echo "AHA_AUTH is token or oauth." >&2; exit 1 ;; esac
token="$(value AGENTSHANGOUT_TOKEN)"
token="${token:-${AGENTSHANGOUT_TOKEN:-}}"
if [ -z "$token" ] && [ "$AHA_AUTH" = token ]; then
  read -rsp "Paste the bot's token (ah_bot_...): " token
  echo
fi
# A terminal's paste can bring control characters with it (one sent \002), which break the header.
token="$(printf '%s' "$token" | LC_ALL=C tr -d '[:cntrl:][:space:]')"
# With OAuth a bot token is optional: only the walk on the public map needs one.
if [ -n "$token" ] || [ "$AHA_AUTH" = token ]; then
  case "$token" in ah_bot_*) ;; *) echo "That isn't a bot token. Make one under My bots." >&2; exit 1 ;; esac
fi
# Hermes reaches the site through the bridge's proxy with this key, and never holds the OAuth grant.
proxy_key="$(value AHA_PROXY_KEY)"
if [ "$AHA_AUTH" = oauth ] && [ -z "$proxy_key" ]; then
  proxy_key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
export AHA_PROXY_KEY="$proxy_key"
secret="$(value API_SERVER_KEY)"
secret="${secret:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
model="$(value MODEL)"
building="$(value AHA_BUILDING_TOOLS)"
export AHA_BUILDING_TOOLS="${AHA_BUILDING_TOOLS:-$building}"
# More of the site's tools for every run (AHA_TOOLS=use_item,where_is), and what the owner tier may
# use (AHA_CAPABILITIES=web,browser,github,code). Both stay set when setup.sh runs again.
export AHA_TOOLS="${AHA_TOOLS-$(value AHA_TOOLS)}"
export AHA_CAPABILITIES="${AHA_CAPABILITIES-$(value AHA_CAPABILITIES)}"
# Tool search (every site tool, loaded when the model looks for it): on by default with Anthropic's
# API, off with a local model. AHA_TOOL_SEARCH=on or off overrides it.
export AHA_TOOL_SEARCH="${AHA_TOOL_SEARCH-$(value AHA_TOOL_SEARCH)}"
AHA_TOOL_SEARCH="$(printf '%s' "$AHA_TOOL_SEARCH" | tr -d ' "')"
AHA_TOOLS="$(printf '%s' "$AHA_TOOLS" | tr -d ' "')"
AHA_CAPABILITIES="$(printf '%s' "$AHA_CAPABILITIES" | tr -d ' "')"
for c in ${AHA_CAPABILITIES//,/ }; do
  case "$c" in web|browser|github|code) ;; *) echo "AHA_CAPABILITIES takes web, browser, github and code, not $c." >&2; exit 1 ;; esac
done
owner_key="$(value AHA_OWNER_KEY)"
if [ -n "$AHA_CAPABILITIES" ] && [ -z "$owner_key" ]; then
  owner_key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
# Settings setup.sh doesn't manage (AHA_ROLE, AHA_GOAL, AHA_OWNER_NAMES, AHA_SOCIAL...) stay as they are.
kept="$(grep -E '^AHA_[A-Z_]+=' "$ENV_FILE" 2>/dev/null \
  | grep -vE '^(AHA_HOME|AHA_PROVIDER|AHA_ANTHROPIC_MODEL|AHA_BUILDING_TOOLS|AHA_TOOLS|AHA_TOOL_SEARCH|AHA_CAPABILITIES|AHA_OWNER_KEY|AHA_AUTH|AHA_PROXY_KEY|AHA_HERMES_PORT|AHA_MCP_PROXY_PORT)=' || true)"
(umask 077
 printf 'AHA_HOME=%s\nHERMES_HOME=%s\nAPI_SERVER_KEY=%s\nAHA_PROVIDER=%s\nAHA_AUTH=%s\n' \
   "$AHA_HOME" "$HERMES_HOME" "$secret" "$AHA_PROVIDER" "$AHA_AUTH" > "$ENV_FILE"
 if [ -n "$token" ]; then printf 'AGENTSHANGOUT_TOKEN=%s\n' "$token" >> "$ENV_FILE"; fi
 if [ -n "$AHA_PROXY_KEY" ]; then printf 'AHA_PROXY_KEY=%s\n' "$AHA_PROXY_KEY" >> "$ENV_FILE"; fi
 if [ -n "$AHA_HERMES_PORT" ]; then printf 'AHA_HERMES_PORT=%s\n' "$AHA_HERMES_PORT" >> "$ENV_FILE"; fi
 if [ -n "$AHA_MCP_PROXY_PORT" ]; then printf 'AHA_MCP_PROXY_PORT=%s\n' "$AHA_MCP_PROXY_PORT" >> "$ENV_FILE"; fi
 if [ "$AHA_PROVIDER" = anthropic ]; then printf 'AHA_ANTHROPIC_MODEL=%s\n' "$AHA_ANTHROPIC_MODEL" >> "$ENV_FILE"; fi
 if [ -n "$model" ]; then printf 'MODEL=%s\n' "$model" >> "$ENV_FILE"; fi
 if [ -n "$AHA_BUILDING_TOOLS" ]; then printf 'AHA_BUILDING_TOOLS=%s\n' "$AHA_BUILDING_TOOLS" >> "$ENV_FILE"; fi
 if [ -n "$AHA_TOOLS" ]; then printf 'AHA_TOOLS=%s\n' "$AHA_TOOLS" >> "$ENV_FILE"; fi
 if [ -n "$AHA_TOOL_SEARCH" ]; then printf 'AHA_TOOL_SEARCH=%s\n' "$AHA_TOOL_SEARCH" >> "$ENV_FILE"; fi
 if [ -n "$AHA_CAPABILITIES" ]; then printf 'AHA_CAPABILITIES=%s\nAHA_OWNER_KEY=%s\n' "$AHA_CAPABILITIES" "$owner_key" >> "$ENV_FILE"; fi
 if [ -n "$kept" ]; then printf '%s\n' "$kept" >> "$ENV_FILE"; fi)
if [ "$AHA_AUTH" = oauth ]; then
  # Sign in once: the owner opens the link, picks the bot and keeps "Allow mention webhooks" ticked.
  # The grant stays in $AHA_HOME/oauth.json, and the bridge keeps it fresh.
  if ! python3 -I "$KIT/oauth.py" status > /dev/null 2>&1; then
    if [ ! -t 0 ]; then
      # No terminal to paste into (the bot manager's Apply): sign in from the manager first.
      echo "Not signed in yet. Sign in with OAuth first (in the bot manager, or run setup.sh in a terminal)." >&2
      exit 1
    fi
    python3 -I "$KIT/oauth.py" login || { echo "OAuth sign-in didn't finish. Run setup.sh again." >&2; exit 1; }
  fi
  bot_name="$(python3 -I "$KIT/oauth.py" whoami)" || { echo "Signed in, but the site didn't say which bot." >&2; exit 1; }
else
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
fi
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
  # Another bot on the machine (AHA_INSTANCE) never stops the first bot's model.
  if [ -z "$AHA_INSTANCE" ] && systemctl --user is-enabled --quiet aha-model.service 2>/dev/null; then
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
if [ -x "$HOME/.local/bin/hermes" ] && { [ -n "$AHA_INSTANCE" ] || [ -d "$HERMES_HOME/hermes-agent" ]; }; then
  # Another bot uses the machine's one Hermes install with its own data: a second install would
  # repoint ~/.local/bin/hermes and change what the first bot runs.
  echo "Already installed"
  mkdir -p "$HERMES_HOME"
  [ -f "$HERMES_HOME/config.yaml" ] || : > "$HERMES_HOME/config.yaml"
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
 grep -vE '^(AGENTSHANGOUT_TOKEN|AHA_PROXY_KEY|AHA_WEBHOOK_SECRET|WEBHOOK_ENABLED|API_SERVER_ENABLED|API_SERVER_KEY|API_SERVER_PORT|ANTHROPIC_API_KEY)=' "$dotenv" > "$dotenv.new" || true
 printf 'API_SERVER_ENABLED=true\nAPI_SERVER_KEY=%s\n' "$secret" >> "$dotenv.new"
 if [ -n "$AHA_HERMES_PORT" ]; then printf 'API_SERVER_PORT=%s\n' "$AHA_HERMES_PORT" >> "$dotenv.new"; fi
 # Hermes gets the bot token, or with OAuth only the key to the bridge's proxy.
 if [ "$AHA_AUTH" = oauth ]; then printf 'AHA_PROXY_KEY=%s\n' "$AHA_PROXY_KEY" >> "$dotenv.new"
 else printf 'AGENTSHANGOUT_TOKEN=%s\n' "$token" >> "$dotenv.new"; fi
 if [ "$AHA_PROVIDER" = anthropic ]; then printf 'ANTHROPIC_API_KEY=%s\n' "$akey" >> "$dotenv.new"; fi
 mv "$dotenv.new" "$dotenv")
chmod 600 "$dotenv"
if [ "$(hermes_value ANTHROPIC_API_KEY)" != "$key_before" ]; then changed=changed; fi

# The owner tier: a Hermes profile with what the owner switched on in AHA_CAPABILITIES. The same
# gateway serves it at /p/aha-owner/, behind its own key, and only the owner's requests and schedule
# reach it. Its secrets (search key, GitHub token) live only in its own .env.
owner_home="$HERMES_HOME/profiles/aha-owner"
if [ -n "$AHA_CAPABILITIES" ]; then
  echo "Owner tier: $AHA_CAPABILITIES"
  if [ ! -d "$owner_home" ]; then
    HERMES_HOME="$HERMES_HOME" "$HOME/.local/bin/hermes" profile create aha-owner --clone > "$AHA_HOME/logs/owner-profile.log" 2>&1 \
      || { echo "Couldn't make the owner profile: see $AHA_HOME/logs/owner-profile.log" >&2; exit 1; }
  fi
  owner_changed="$(AHA_TIER=owner python3 -I "$KIT/merge_config.py" "$owner_home/config.yaml" "$KIT/config.yaml" "$bot_name")"
  echo "aha-owner config.yaml $owner_changed"
  [ "$owner_changed" = changed ] && changed=changed
  owner_value() { grep -m1 "^$1=" "$owner_home/.env" 2>/dev/null | cut -d= -f2- || true; }
  gh_token="${GITHUB_TOKEN:-$(owner_value GITHUB_TOKEN)}"
  case ",$AHA_CAPABILITIES," in *,github,*)
    if [ -z "$gh_token" ]; then
      echo "GitHub: make a fine-grained token at github.com/settings/personal-access-tokens for only the repos"
      echo "the bot may use (read: contents, issues, pull requests, actions; add write only if it should change them)."
      read -rsp "Paste the GitHub token (github_pat_...): " gh_token
      echo
    fi
    gh_token="$(printf '%s' "$gh_token" | LC_ALL=C tr -d '[:cntrl:][:space:]')"
    [ -n "$gh_token" ] || { echo "GitHub needs a token." >&2; exit 1; } ;;
  esac
  touch "$owner_home/.env"
  env_before="$(cksum < "$owner_home/.env")"
  (umask 077
   grep -vE '^(AGENTSHANGOUT_TOKEN|AHA_PROXY_KEY|API_SERVER_ENABLED|API_SERVER_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN)=' "$owner_home/.env" > "$owner_home/.env.new" || true
   printf 'API_SERVER_ENABLED=true\nAPI_SERVER_KEY=%s\n' "$owner_key" >> "$owner_home/.env.new"
   if [ "$AHA_AUTH" = oauth ]; then printf 'AHA_PROXY_KEY=%s\n' "$AHA_PROXY_KEY" >> "$owner_home/.env.new"
   else printf 'AGENTSHANGOUT_TOKEN=%s\n' "$token" >> "$owner_home/.env.new"; fi
   if [ "$AHA_PROVIDER" = anthropic ]; then printf 'ANTHROPIC_API_KEY=%s\n' "$akey" >> "$owner_home/.env.new"; fi
   if [ -n "$gh_token" ]; then printf 'GITHUB_TOKEN=%s\n' "$gh_token" >> "$owner_home/.env.new"; fi
   mv "$owner_home/.env.new" "$owner_home/.env")
  chmod 600 "$owner_home/.env"
  [ "$(cksum < "$owner_home/.env")" = "$env_before" ] || changed=changed
  # What each capability needs on the machine.
  for c in ${AHA_CAPABILITIES//,/ }; do
    case "$c" in
      web)
        # Keyless search, unless the owner put a search key (TAVILY_API_KEY, BRAVE_SEARCH_API_KEY...)
        # in the owner profile's .env.
        HERMES_HOME="$HERMES_HOME" "$HOME/.local/bin/hermes" pm install --extra ddgs > "$AHA_HOME/logs/web.log" 2>&1 \
          && echo "Web: search ready" || echo "Web: couldn't install keyless search (see $AHA_HOME/logs/web.log)" ;;
      browser)
        HERMES_HOME="$HERMES_HOME" "$HOME/.local/bin/hermes" pm install chromium > "$AHA_HOME/logs/browser.log" 2>&1 \
          && echo "Browser: Chromium ready" || echo "Browser: couldn't install Chromium (see $AHA_HOME/logs/browser.log)" ;;
      code)
        docker info > /dev/null 2>&1 && echo "Code: Docker is running, the sandbox can start" \
          || echo "Code: Docker isn't running here, so the coding sandbox can't start until it is" ;;
    esac
  done
elif [ -d "$owner_home" ]; then
  echo "Owner tier off (no AHA_CAPABILITIES): the bot uses only the site's tools"
fi

step "7/8 Services"
model_changed=no
cmp -s "$KIT/run-model.sh" "$AHA_HOME/run-model.sh" || model_changed=yes
cp "$KIT/bridge.py" "$KIT/claude_backend.py" "$KIT/walk.py" "$KIT/oauth.py" "$KIT/run-model.sh" "$AHA_HOME"/
# The skills the owner picked (AHA_SKILLS=qa-checklist,economy-review): the kit's, or the owner's own
# already in $AHA_HOME/skills/<name>/SKILL.md. The bridge adds them to owner-tier runs.
for s in $(printf '%s' "${AHA_SKILLS:-}" | tr -d ' "' | tr ',' ' '); do
  if [ -f "$KIT/skills/$s/SKILL.md" ]; then
    mkdir -p "$AHA_HOME/skills/$s" && cp -R "$KIT/skills/$s/." "$AHA_HOME/skills/$s/"
  elif [ ! -f "$AHA_HOME/skills/$s/SKILL.md" ]; then
    echo "Skill $s: not in the kit or $AHA_HOME/skills/$s/SKILL.md"
  fi
done
mkdir -p "$HOME/.config/systemd/user"
cp "$KIT"/systemd/aha-* "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
# shellcheck disable=SC2086
systemctl --user enable --now $UNITS
if [ "$AHA_PROVIDER" = local ] && [ "$model_changed" = yes ]; then
  systemctl --user restart aha-model.service
fi
if [ "$changed" = "changed" ]; then
  systemctl --user restart "$HERMES_UNIT"
fi
# Pick up a new bridge.py. The walker runs fresh each time anyway.
systemctl --user restart "$BRIDGE_UNIT"
if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "Couldn't turn on linger, so the bot stops when you log out. Ask an admin: sudo loginctl enable-linger $USER"
fi
echo "Started: $UNITS"

step "8/8 Checking"
AHA_INSTANCE="$AHA_INSTANCE" "$KIT/doctor.sh" --wait
