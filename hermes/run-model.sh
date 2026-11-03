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
# Two more settings keep a long run from eating the board's memory (Jetson's GPU and CPU share it):
#   -ctk/-ctv q8_0     the 64K-token KV cache in 8 bits instead of 16: about 1 GB less, same answers
#   --ctx-checkpoints  Qwen3.5 can't rewind its state, so llama.cpp saves a copy in RAM every few
#                      thousand tokens, up to 32 of them. Four is plenty for a bot that appends.
KV="${AHA_KV_TYPE:-q8_0}"
exec "$AHA_HOME/llama/llama-server" \
  -m "$MODEL" --alias qwen3.5-4b \
  -c 65536 -np 1 -ngl 99 -fa on --jinja --cache-ram 256 \
  -ctk "$KV" -ctv "$KV" --ctx-checkpoints 4 \
  -t 4 --host 127.0.0.1 --port 8080 --no-webui
