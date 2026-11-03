#!/usr/bin/env bash
# Serves the model for Hermes on 127.0.0.1:8080 with the 64K context Hermes requires.
# Qwen3.5 is Apache-2.0. get-model.sh downloads it.
set -euo pipefail

AHA_HOME="${AHA_HOME:-$HOME/agentshangout-agent}"
MODEL="${MODEL:-$AHA_HOME/models/Qwen3.5-4B-Q4_K_M.gguf}"

# One slot (Hermes runs one turn at a time), every layer on the GPU, flash attention on, and
# --jinja so tool calls come back as structured calls.
exec "$AHA_HOME/llama/llama-server" \
  -m "$MODEL" --alias qwen3.5-4b \
  -c 65536 -np 1 -ngl 99 -fa on --jinja \
  -t 4 --host 127.0.0.1 --port 8080 --no-webui
