#!/usr/bin/env bash
# Builds llama.cpp's llama-server with CUDA for a Jetson Orin (JetPack 6, CUDA 12.6).
# Usage: ./build-llama.sh [install dir]   (default: $AHA_HOME/llama, AHA_HOME defaults to ~/agentshangout-agent)
set -euo pipefail

LLAMA_TAG="${LLAMA_TAG:-v0.8.0}"
AHA_HOME="${AHA_HOME:-$HOME/agentshangout-agent}"
DEST="${1:-$AHA_HOME/llama}"
SRC="$AHA_HOME/src/llama.cpp"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

if [ ! -x "$CUDA_HOME/bin/nvcc" ]; then
  echo "No nvcc in $CUDA_HOME/bin. Install JetPack's CUDA, or set CUDA_HOME." >&2
  exit 1
fi

mkdir -p "$AHA_HOME/src" "$DEST"
if [ ! -d "$SRC/.git" ]; then
  git clone --depth 1 --branch "$LLAMA_TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"
fi
git -C "$SRC" fetch --depth 1 origin tag "$LLAMA_TAG"
git -C "$SRC" checkout -q "$LLAMA_TAG"

# 87 is the Orin's compute capability. Four jobs keep nvcc inside 8 GB.
PATH="$CUDA_HOME/bin:$PATH" cmake -S "$SRC" -B "$SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF
PATH="$CUDA_HOME/bin:$PATH" cmake --build "$SRC/build" --target llama-server -j "${JOBS:-4}"

cp "$SRC"/build/bin/llama-server "$DEST"/
find "$SRC/build/bin" -name '*.so*' -exec cp -P {} "$DEST"/ \;
"$DEST/llama-server" --version
