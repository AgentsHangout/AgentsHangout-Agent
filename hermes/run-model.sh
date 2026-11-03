#!/usr/bin/env bash
# Serves the model for Hermes on 127.0.0.1:8080 with the 64K context Hermes requires.
# Qwen3.5 is Apache-2.0. get-model.sh downloads it.
set -euo pipefail

AHA_HOME="${AHA_HOME:-$HOME/agentshangout-agent}"
MODEL="${MODEL:-$AHA_HOME/models/Qwen3.5-4B-Q4_K_M.gguf}"

# One slot (Hermes runs one turn at a time), every layer on the GPU, flash attention on, and
# --jinja so tool calls come back as structured calls. The slot keeps Hermes's long prompt
# cached by itself; llama.cpp's extra prompt cache in RAM defaults to 8 GB, which on an 8 GB
# board grew into 7 GB of swap within a few hours, so it's kept small.
exec "$AHA_HOME/llama/llama-server" \
  -m "$MODEL" --alias qwen3.5-4b \
  -c 65536 -np 1 -ngl 99 -fa on --jinja --cache-ram 256 \
  -t 4 --host 127.0.0.1 --port 8080 --no-webui
