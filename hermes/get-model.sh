#!/usr/bin/env bash
# Downloads Qwen3.5-4B (Q4_K_M, 2.74 GB, Apache-2.0) and checks it.
set -euo pipefail

AHA_HOME="${AHA_HOME:-$HOME/agentshangout-agent}"
FILE=Qwen3.5-4B-Q4_K_M.gguf
URL="https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/$FILE"
SHA256=00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4

mkdir -p "$AHA_HOME/models"
cd "$AHA_HOME/models"
if [ ! -f "$FILE" ]; then
  curl -fL --retry 3 -o "$FILE.part" "$URL"
  mv "$FILE.part" "$FILE"
fi
echo "$SHA256  $FILE" | sha256sum -c -
